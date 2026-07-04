# Watchdog Reconciliation Patterns — guaranteeing SQLite-state ⟺ git-reality convergence (2026-07-03)

**Author:** researcher (thread MP), READ-ONLY.
**Question:** How should Juggle's watchdog *systematically* guarantee convergence
between recorded orchestration state (SQLite topic/task states) and git reality
(is the branch tip actually merged to main?), so a missed one-shot action (an
integrate) can never wedge state permanently?
**Method:** primary sources only — git man pages (git-scm.com), Kubernetes
official docs + sample-controller source, Temporal docs, systemd man pages. Each
claim cited. Retrieved 2026-07-03.

---

## TL;DR (findings first)

1. **git already gives you a cheap, exact "is it merged?" oracle** —
   `git merge-base --is-ancestor <branch-tip> main` (exit 0 = merged, exit 1 =
   not, other = error). This is the correct ground-truth probe for a
   fast-forward / true-merge landing. It is a *level* check: it reads reality
   fresh every time, so it is safe to run on every watchdog tick.
   [git-merge-base]
2. **But ancestry has a documented blind spot the Juggle fix plan does not name:
   rebased or squash-merged branches.** After a rebase/squash, main contains an
   *equivalent* commit with a *different SHA*; the original branch tip is **not**
   an ancestor of main, so `--is-ancestor` returns "not merged" even though the
   work is landed. The git-native tool for this case is **`git cherry` /
   `git patch-id`**, which compares diffs (whitespace- and line-number-
   insensitive) rather than SHAs. [git-cherry][git-patch-id]
3. **The controller pattern (Kubernetes) is the exact shape the watchdog wants:**
   a *level-triggered* reconcile loop that reads desired vs. observed state every
   tick and drives them together, requeuing with backoff on failure, never
   trusting a one-shot edge event. Juggle's "one integrate, inline, no re-drive"
   is the anti-pattern this replaces. [k8s-controller][sample-controller]
4. **Durable one-shot effects are guaranteed by at-least-once + idempotency, not
   by "run it once and hope."** Temporal's "exactly once and to completion" is an
   *illusion built on top of* automatic at-least-once retry plus idempotent
   activities; systemd models a persistent one-shot with `Type=oneshot` +
   `RemainAfterExit=yes` + `Restart=on-failure`. Both say the same thing: the
   effecting action must be **safe to re-run** and something must **keep
   re-running it until the observed effect matches**. [temporal-durable]
   [temporal-retry][systemd-service]

---

## 1. git plumbing as ground truth

### 1a. The merged? oracle — `merge-base --is-ancestor`

> "`--is-ancestor` — Check if the first `<commit>` is an ancestor of the second
> `<commit>`, and exit with status 0 if true, or with status 1 if not. Errors are
> signaled by a non-zero status that is not 1." [git-merge-base]

Ancestry = reachability through parent pointers:

> "Given two commits A and B, `git merge-base A B` will output a commit which is
> reachable from both A and B through the parent relationship." [git-merge-base]

**Implication:** `git merge-base --is-ancestor <branch-tip> main` is the precise,
side-effect-free probe of "did this land on main via ff/true-merge?" The 3-way
exit (0/1/other) lets the watchdog distinguish merged / not-merged / git-error
and fail-closed on the error case (never treat an error as "merged"). This is
exactly the check Juggle already leans on in `_verified_allowed` /
`_heal_merged_sha` (per the RCA), so the primitive is right.

`git branch --merged <commit>` is the batch form of the same reachability test:

> "With `--merged`, only branches merged into the named commit (i.e. the branches
> whose tip commits are reachable from the named commit) will be listed." …
> "`--merged` is used to find all branches which can be safely deleted, since
> those branches are fully contained by HEAD." [git-branch]

So a single `git branch --merged main` enumerates every landed `cyc_*` branch in
one call — useful for a sweep that reconciles many topics per tick, and for the
worktree-cleanup half of integrate (a branch that is `--merged main` is safe to
delete).

### 1b. What ancestry CANNOT detect — the rebase/squash rewrite

The same reachability definition that makes `--is-ancestor` exact also makes it
**blind to rewritten history**. A rebase or squash-merge produces a *new* commit
object; the original tip is not among main's ancestors. `--is-ancestor` and
`--merged` will both report "not merged." This is not a bug — it is the
documented semantics (reachability of *that SHA*).

git's own answer is content-equivalence via patch-id:

> "The equivalence test is based on the diff, after removing whitespace and line
> numbers. git-cherry therefore detects when commits have been 'copied' by means
> of git-cherry-pick, git-am or git-rebase." [git-cherry]

> "Outputs the SHA1 of every commit in `<limit>..<head>`, prefixed with `-` for
> commits that have an equivalent in `<upstream>`, and `+` for commits that do
> not." [git-cherry]

> "A 'patch ID' is nothing but a sum of SHA-1 of the file diffs associated with a
> patch, with line numbers ignored." … "two patches that have the same 'patch
> ID' are almost guaranteed to be the same thing." … "The main usecase for this
> command is to look for likely duplicate commits." [git-patch-id]

**Implication for Juggle (load-bearing):** A robust "is this topic's work on
main?" check is **two-tier**:
1. Fast path: `git merge-base --is-ancestor <tip> main` → merged (SHA-identical
   landing).
2. Fallback: `git cherry main <tip>` (or compare `git patch-id` of the branch's
   commits against main's recent commits) → if every branch commit is prefixed
   `-` (has an equivalent upstream), the work **is** landed under a different SHA
   — do **not** re-integrate; instead record the equivalent merged SHA and let
   reconcile complete `→verified`.

Without tier 2, a re-integrate driver that only checks ancestry will *re-merge
already-landed rebased work* — creating duplicate commits or a spurious
conflict, i.e. trading one wedge for another. **This is the specific blind spot
the RCA fix plan (fix 1) does not name** (see §4).

Caveat (state as a limitation, not fabricate certainty): patch-id equivalence is
"almost guaranteed" [git-patch-id], not a cryptographic identity — a squash that
*combines* N branch commits into one main commit will not match per-commit patch
ids one-to-one. For squash-merges the reliable signal is a *range-diff / tree
comparison* (does `git diff <tip> main` restricted to the branch's touched paths
come back empty?) rather than per-commit cherry. Juggle lands work by **ff-merge**
by policy (CLAUDE.md landing policy: "verified-green work lands on main by
ff-merge"), so tier-1 ancestry is the common case and tier-2 cherry covers the
rebase-before-ff case; a squash path is out of policy and can be fail-closed
(refuse + escalate) rather than auto-resolved.

---

## 2. Level-triggered reconciliation (Kubernetes controller pattern)

The controller pattern is a control loop that continuously drives observed state
toward desired state — never a one-shot reaction to an event:

> "In Kubernetes, controllers are control loops that watch the state of your
> cluster, then make or request changes where needed. Each controller tries to
> move the current cluster state closer to the desired state." [k8s-controller]

> "These objects have a spec field that represents the desired state. The
> controller(s) for that resource are responsible for making the current state
> come closer to that desired state." [k8s-controller]

Crucially, it does not assume convergence ever "finishes" — it keeps reconciling:

> "Your cluster could be changing at any point as work happens and control loops
> automatically fix failures. This means that, potentially, your cluster never
> reaches a stable state. As long as the controllers … are running and able to
> make useful changes, it doesn't matter if the overall state is stable or not."
> [k8s-controller]

The sample-controller source shows the concrete requeue-on-failure mechanic — a
failed reconcile is **retried with backoff**, a successful one is forgotten:

```go
func (c *Controller) processNextWorkItem(ctx context.Context) bool {
    objRef, shutdown := c.workqueue.Get()
    err := c.syncHandler(ctx, objRef)
    if err == nil {
        c.workqueue.Forget(objRef)   // success: stop retrying this item
        return true
    }
    c.workqueue.AddRateLimited(objRef) // failure: requeue with backoff
    return true
}
```
[sample-controller]

`syncHandler` is idempotent by construction — each pass **reads actual state and
converges**: get the target Deployment; "if the resource doesn't exist, we'll
create it"; if the replica spec "does not equal the current desired replicas …
we should update"; then write status back. [sample-controller] Running it 1× or
100× yields the same end state.

**Mapping onto the watchdog tick:**
- **Desired state** = SQLite: a topic whose member tasks are all `verified` should
  end at topic-state `verified` **with a recorded `merged_sha` that is real**.
- **Observed state** = git: is `<branch-tip>` merged into main (§1 two-tier
  probe)?
- **Reconcile action** = if desired≠observed (all-tasks-verified but not on
  main), *idempotently run integrate*; if it fails, **requeue with backoff**
  (the `AddRateLimited` analogue) rather than wedging.
- **Forget** = once merged_sha is real and ancestry/cherry confirms it, stop
  driving that topic.
- **Level, not edge:** the tick must re-derive "is it merged?" from git **every
  tick**, not trust the one `agent_complete` edge event that fired once. The RCA
  root cause ("topic advanced to `integrating` at mark-task time, decoupled from
  whether integrate ever ran; only one inline lander, no re-drive") is precisely
  the *edge-triggered, no-requeue* anti-pattern this replaces.

---

## 3. Durable one-shot actions (Temporal + systemd)

Two independent production systems converge on the **same** recipe for "this
effect must happen despite crashes": **at-least-once execution + idempotency**,
with a driver that keeps retrying until the effect is observed.

### Temporal

The headline guarantee is durability of *progress*, not a magic single execution:

> "Durable Execution … refers to the ability of a Workflow Execution to maintain
> its state and progress even in the face of failures, crashes, or server
> outages." … "If a failure occurs, the Workflow Execution can resume from the
> last recorded event, ensuring that progress isn't lost." [temporal-durable]

Temporal markets "exactly once and to completion" [temporal-durable] — but the
retry-policy docs make explicit that the underlying *activity* mechanism is
**automatic at-least-once retry**, and that idempotency is therefore required:

> "Temporal automatically retries failed Activities … Activities continue
> retrying 'until it either succeeds or is canceled.'" [temporal-retry]

> "Because Activities may be retried multiple times, they must be designed for
> idempotency. … an Activity may execute more than once due to retries or
> infrastructure issues." [temporal-retry]

**Reading (fact vs. framing):** "exactly once" is a *workflow-level abstraction*
built **on top of** at-least-once activity execution plus developer-supplied
idempotency + a durable event log for dedup. It is not exactly-once *delivery* of
side effects. The honest guarantee an orchestrator can rely on is: *keep retrying
an idempotent effecting action until the durable log shows it took*.

### systemd

systemd models a persistent one-shot effect with three directives:

> "`Type=oneshot` — the service manager will consider the unit up after the main
> process exits." … "if this option is used without `RemainAfterExit=` the
> service will never enter 'active' … but will directly transition from
> 'activating' to 'deactivating' or 'dead'." [systemd-service]

> "`RemainAfterExit=` — whether the service shall be considered active even when
> all its processes exited. Defaults to no." [systemd-service]

> "`Restart=` — Configures whether the service shall be restarted when the
> service process exits, is killed, or a timeout is reached." Values incl.
> `on-failure` (restart on non-zero exit/signal/timeout) and `always`.
> [systemd-service]

And the idempotency property, verbatim:

> "When `RemainAfterExit=yes` is added, the unit stays active after completion,
> and invoking `systemctl start` on that unit again will cause no action to be
> taken." … "This design prevents re-execution of configuration or setup tasks
> whose effects should persist." [systemd-service]

**Reading:** `Type=oneshot` + `RemainAfterExit=yes` = "this action has a durable
effect; once the effect is present, re-invocation is a no-op" — an idempotent
one-shot. `Restart=on-failure` = "if the action *fails*, keep re-running until it
succeeds." Together: at-least-once + idempotent + fail-closed, the same recipe as
Temporal.

**Mapping onto Juggle's integrate:**
- integrate is the "oneshot": its durable effect is a real `merged_sha` +
  branch-on-main.
- Make integrate **idempotent** (RemainAfterExit analogue): before doing work, it
  must detect "already landed" via the §1 two-tier probe and no-op —
  *including the rebase/cherry case* — recording the existing merged SHA rather
  than re-merging.
- Give it a **Restart=on-failure driver** (the watchdog re-integrate sweep): a
  topic that is all-verified-but-not-on-main is re-driven each tick until either
  git shows it merged (→ Forget) or integrate returns a *real* failure (→ write
  `fail_envelope`, route to repair — the fail-closed branch).

---

## 4. Mapping onto Juggle's fix plan (research/2026-07-03-integrate-wedge-rca.md)

Confirm/refute each proposed fix against the patterns above. Fixes quoted from
the RCA's "Proposed fix plan."

**Fix 1 — "Add a watchdog re-integrate driver."** ✅ **CONFIRMED, with a gap.**
This *is* the k8s reconcile loop (level-triggered re-drive with requeue) and the
Temporal/systemd `Restart=on-failure` driver. Correct core. **Gap the plan does
not name:** the driver's trigger condition is stated as "(a) worktree branch
still has commits ahead of main, (b) `merged_sha` empty, (c) bound agent not
live." Condition (a) as written ("commits ahead of main") is an **ancestry**
test — it will fire on a **rebased-then-merged** branch whose work is *already on
main under a different SHA* (§1b), causing the driver to **re-merge landed work**
(duplicate commits / spurious conflict). The RCA even records the trigger
worktrees as "1 commit ahead of main, NOT an ancestor of main" — for genuinely
unmerged work that's correct, but the *same signature* is produced by
rebased-merged work, and the driver cannot tell them apart without a
content-equivalence check. **Required addition:** before re-integrating, run the
§1 tier-2 probe (`git cherry main <tip>` / patch-id) — if every branch commit has
an upstream equivalent (`-` prefix), treat as **already landed**: record the
equivalent merged SHA, let reconcile finish `→verified`, do **not** re-merge.
Only re-integrate when the work is *both* not-an-ancestor *and* not
patch-equivalent-present.

**Fix 2 — "Guard failure verdicts in `reconcile_topic_state`."** ✅ **CONFIRMED.**
Consistent with the controller pattern: observed-state derivation must not
*erase* a recorded failure verdict (`failed-integration`/`failed-verify`) just
because member tasks are all terminal-verified. In k8s terms, status is written
by the reconciler as an outcome, not silently recomputed from spec into a
success. Add these states to the terminal-guard alongside `verified`. No
refutation.

**Fix 3 — "Make completion effects unconditional on a live binding."** ✅
**CONFIRMED** — this is the idempotency requirement (§3). Temporal/systemd both
demand the effecting action succeed regardless of transient binding/liveness
churn (spool-replay-after-rebind). Closing the ledger by `thread_id` and reaping
by recorded `agent_id` (not via the still-live `get_agent_by_thread` binding)
makes the completion effect idempotent and binding-independent. Aligned.

**Fix 4 — "Fix the auto-dismiss window."** ✅ **CONFIRMED, orthogonal but
essential.** Not a reconciliation-pattern item per se; it is the *observability*
guarantee that makes the fail-closed branch real. Temporal/systemd both surface a
persistent failure (event history / `failed` unit state) rather than swallowing
it. A finalization-failure action item auto-dismissed before it is seen violates
"fail loudly." Snapshot `items_to_dismiss` before creating new items. Aligned
with the project's own "detect, refuse, preserve" philosophy.

**Fix 5 — "Make the G5 orphan guard actually surface this."** ✅ **CONFIRMED.**
The controller pattern tolerates never-stable state *only because* the loop keeps
making progress; when it *cannot* (a >1 h wedge with no live agent and no repair
path), that is exactly the "needs judgment" event the triage ladder escalates. A
HIGH action item after the grace window is the escalation. Verify grace/dedup
does not suppress it.

**Fix 6 — "Root-fix the dispatch trigger (idempotent worktree/branch creation)."**
✅ **CONFIRMED** — same idempotency principle applied one layer up. "branch
cyc_GS/GT already exists → worktree auto-create failed" is a non-idempotent
side-effect (systemd's anti-case: an action that errors instead of no-op'ing when
its effect is already present). Reuse-or-clean on collision = the
`RemainAfterExit` "re-invocation is a no-op" property.

### What the fix plan misses (net)

1. **Rebase/squash ancestry blind spot (primary).** No fix names it; fix 1's
   trigger will mis-fire on rebased-merged branches. Add the patch-id / `git
   cherry` tier-2 check to *both* the re-integrate trigger and integrate's own
   idempotency guard (`_heal_merged_sha` currently only heals when the tip *is*
   already an ancestor — per the RCA's Defect-C note — so it structurally cannot
   heal a rebased landing). This is the one place the plan could trade a wedge
   for a duplicate-merge.
2. **Explicit "Forget" condition.** The plan adds a re-drive (Restart=) but does
   not state the *termination* predicate crisply. Per the controller pattern the
   driver must stop (Forget) precisely when git confirms landed (ancestry OR
   patch-equivalent) AND `merged_sha` is recorded — otherwise a driver that
   re-derives `integrating` every tick can hot-loop. Pair fix 1 with fix 2's
   terminal-guard so a confirmed-merged or confirmed-failed topic is never
   re-derived back into the driver's queue.
3. **Backoff / rate-limit on the re-drive.** k8s uses `AddRateLimited`
   (exponential backoff) so a repeatedly-failing reconcile doesn't hot-loop
   [sample-controller]; Temporal uses capped exponential backoff [temporal-retry].
   The plan's re-integrate sweep should likewise back off per-topic (and after N
   failures write `fail_envelope` → repair/escalate) rather than retry every tick
   forever.

---

## Implications for Juggle

- **Ground truth is git, read every tick (level-triggered), never a stored edge.**
  Reconcile derives topic "merged?" from a live two-tier git probe:
  `merge-base --is-ancestor` (fast, ff/true-merge) → `git cherry`/`patch-id`
  (rebased-merged fallback). Fail-closed on git error. This is a pure,
  agent-verifiable function (deterministic CLI, 3-way exit) — no human eyeball
  needed. [git-merge-base][git-cherry][git-patch-id]
- **integrate must be idempotent (systemd `oneshot`+`RemainAfterExit`).** Detect
  "already landed" (incl. rebased) and no-op-record instead of re-merging; only
  do real merge work when genuinely unmerged. [systemd-service]
- **The watchdog owns a Restart=on-failure re-integrate driver (k8s reconcile +
  requeue).** All-verified-but-not-on-main topics are re-driven with per-topic
  backoff until git shows landed (Forget) or integrate returns a real failure
  (write `fail_envelope` → repair → escalate). This closes A9a — the missing
  "durable reconcile-repair path." [k8s-controller][sample-controller]
  [temporal-retry]
- **Fail-closed + observable at the boundary.** On unresolvable wedge, surface a
  HIGH action item (fix 4/5) — the triage-ladder escalation the CLAUDE.md
  philosophy already mandates.
- **Net new work vs. the RCA plan:** add patch-id/`git cherry` equivalence to
  fix 1's trigger and to `_heal_merged_sha`; state the Forget predicate; add
  backoff. Everything else in the plan is confirmed by the patterns.

---

## Sources (retrieved 2026-07-03, primary only)

- [git-merge-base] `git merge-base` man page — https://git-scm.com/docs/git-merge-base
- [git-branch] `git branch` man page (`--merged`/`--no-merged`) — https://git-scm.com/docs/git-branch
- [git-cherry] `git cherry` man page — https://git-scm.com/docs/git-cherry
- [git-patch-id] `git patch-id` man page — https://git-scm.com/docs/git-patch-id
- [k8s-controller] Kubernetes docs, "Controllers" — https://kubernetes.io/docs/concepts/architecture/controller/
- [sample-controller] kubernetes/sample-controller `controller.go` (`processNextWorkItem`/`syncHandler`) — https://github.com/kubernetes/sample-controller/blob/master/controller.go
- [temporal-durable] Temporal docs, "Temporal / Durable Execution" — https://docs.temporal.io/temporal
- [temporal-retry] Temporal docs, "Retry Policies" — https://docs.temporal.io/encyclopedia/retry-policies
- [temporal-activities] Temporal docs, "Activities" — https://docs.temporal.io/activities
- [systemd-service] `systemd.service(5)` man page (`Type=oneshot`, `RemainAfterExit=`, `Restart=`) — https://man7.org/linux/man-pages/man5/systemd.service.5.html

**Confidence:** [HIGH CONFIDENCE] on all git-plumbing claims (official man pages),
the k8s controller loop (official docs + source), and systemd directives (man
page). Temporal "exactly once" framing vs. underlying at-least-once is
[HIGH CONFIDENCE] cross-referenced across the durable-execution and retry-policy
pages. The squash-merge patch-id caveat (§1b) is stated as a documented
*limitation* of patch-id, not a fabricated guarantee.
