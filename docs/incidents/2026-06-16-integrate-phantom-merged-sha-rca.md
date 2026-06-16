# Incident: integrate records phantom merged_sha + strands work
**Date:** 2026-06-16 | **Severity:** High | **Status:** Fixed

## Symptom

Topic `T-cockpit-watchdog-owner` (branch `cyc_AI`, real work commit `0171ca0`)
finished integration. `juggle integrate` recorded
`merged_sha=95e36e5e6e0f6a9a07c65a8311aacaa49b442c64` on the topic and tore
down the worktree — but:

- That SHA is a **bad object** (does not exist in the repo).
- Canonical `main` was **never advanced** (stayed at `d8e3777`).
- The branch was left **unmerged** and the worktree was gone.

Work was stranded: integrate could not re-run ("Missing worktree fields") and
the branch had been deleted.

## Defense-in-depth saved us

`graph_guards.topic_is_merged` → `sha_is_ancestor` correctly kept the topic at
`integrating` (not falsely `verified`) because the phantom SHA is not an
ancestor of main. The secondary guard held; the topic did not prematurely verify.

Recovery: manual `git merge cyc_AI → main` (e3c0cc7), re-recorded the real SHA,
topic verified.

## Root causes

### A. `_record_merged_sha` — no object or ancestry check

`_record_merged_sha(db, thread_uuid, repo, ref)` ran `git rev-parse ref` and
immediately wrote the result via `set_topic_merged_sha` with no further checks:

```python
sha = subprocess.run(["git", "-C", repo, "rev-parse", ref], ...)
if sha.returncode == 0 and sha.stdout.strip():
    db_topics.set_topic_merged_sha(db, topic["id"], sha.stdout.strip())
```

It did NOT verify:
- The SHA is a real object in the repo (`git cat-file -e <sha>`).
- The SHA is an ancestor of the canonical/origin main (`git merge-base --is-ancestor`).

A worktree-local, stale, or phantom SHA could be persisted.

### B. `ahead_count == 0` shortcut — no canonical-main guard

The idempotency shortcut in `_run_integrate`:

```python
if ahead_count == 0:
    _record_merged_sha(...)   # writes SHA
    # removes worktree, deletes branch, clears thread fields
```

Used only `git rev-list --count rebase_onto..worktree_branch == 0` as the
"already merged" signal. `rebase_onto` can be stale (fetch failed, local-only
main) or the shortcut can fire from a ref that is 0 commits ahead of a stale
`origin/main` while not actually present on the canonical remote. There was no
`merge-base --is-ancestor` guard against the true canonical ref before the
irreversible teardown.

## Fix

### A. `_record_merged_sha` — three-step gate

Before writing `merged_sha`:

1. Resolve SHA via `git rev-parse ref`.
2. Confirm object exists: `git cat-file -e <sha>`.
3. Resolve canonical main (`origin/<main>` after `git fetch origin <main>`;
   fall back to local `main`/`master` if origin unreachable).
4. Confirm ancestor: `git merge-base --is-ancestor <sha> <canonical>`.

Only on success of all four steps is `set_topic_merged_sha` called. Failures
are logged as warnings and `merged_sha` is left NULL (verified-gate stays
closed; integrate can be re-run since worktree/branch were not touched at this
point).

### B. `ahead_count == 0` shortcut — `merge-base --is-ancestor` guard

Before any teardown (worktree remove, branch delete, thread fields clear):

```python
shortcut_ancestor = subprocess.run(
    ["git", "-C", main_repo_path, "merge-base", "--is-ancestor",
     worktree_branch, canonical_for_shortcut],
    ...
).returncode == 0
if not shortcut_ancestor:
    return _fail("... work preserved; re-run integrate ...")
```

`canonical_for_shortcut` upgrades from a local `main` ref to `origin/main` if
available, so the check is against the pushed truth. If the guard fails the
shortcut aborts with a clear error; the real ff-merge/push path is used instead
and work is never stranded.

## Regression tests

`tests/test_integrate_phantom_sha.py` — all pins fail before fix, pass after:

| Test | What it pins |
|------|-------------|
| `test_record_merged_sha_non_ancestor_not_written` | A: side branch not on main → merged_sha NULL |
| `test_record_merged_sha_phantom_object_not_written` | A: cat-file -e rejects bad object → NULL |
| `test_shortcut_non_ancestor_preserves_worktree_and_branch` | B: forced ahead_count=0 + non-ancestor → no teardown |
| `test_record_merged_sha_genuine_ancestor_written` | GREEN: real merged SHA → written correctly |
| `test_shortcut_genuine_ancestor_cleans_up` | GREEN: real already-merged branch → teardown OK |
| `test_shortcut_genuine_ancestor_with_remote_cleans_up` | GREEN: pushed + merged on origin → teardown OK |
