"""Drift guard: every juggle CLI command a prompt names must actually RESOLVE.

INCIDENT 2026-08-02 — `commands/start.md` (the primary orchestration prompt)
mandated `juggle_cli.py recall <thread_id> "<query>"` in three places and
`commands/resume-topic.md` mandated `recall-bg`; both verbs had been deleted by
the resource-verb grammar migration. Every invocation exited 2 on an argparse
usage error, several of them piped through `2>&1 | head -100`, which masked the
non-zero exit — so the "never answer personal questions from training data
alone" rule was silently violated on every personal question for weeks.

`test_legacy_cli_name_lint.py` only checks a FIXED list of RENAMED legacy names
(tests/data/legacy_names.txt); `recall`/`recall-bg` were removed outright, never
renamed, so they were not on it. This guard inverts the check — it resolves what
the prompts say against the LIVE argparse tree, so any command a prompt names
and the CLI does not expose fails the build, whatever the reason it went away.

Three drift surfaces, all of which had a live defect when this guard was written:
  1. invocations   — `uv run …/juggle_cli.py <cmd> [sub]`   (recall, recall-bg,
                     update-summary)
  2. arity         — the same invocation, under-supplied         (`memory retain
                     "<fact>"` — one positional for a two-positional verb)
  3. reference tables — the ``| `cmd` | Signature | …`` tables that teach the
                     orchestrator its vocabulary   (notify, update-summary)

Scope: commands/ and skills/ — the surfaces whose text is handed to an
orchestrator or agent as executable instructions.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402

_SCAN_DIRS = ("commands", "skills")

# A juggle_cli.py invocation: a runner (`uv run`, `python3`), the script path
# (optionally quoted / ${VAR}-interpolated), then the rest of the command.
# Requiring the runner is what keeps prose that merely NAMES the script
# ("juggle_cli.py loads this automatically", commands/init.md) out of the scan.
_INVOKE_RE = re.compile(
    r"""(?:uv\s+run|python3?)\s+       # runner
        ['"]?\S*juggle_cli\.py['"]?    # script path, optionally quoted
        \s+(.*)$                       # everything after it
    """,
    re.VERBOSE,
)
# Shell command separators — a `!`-negated SEGMENT asserts the command is
# REJECTED (a removal proof), so it is not a live call. Mirrors the exemption in
# test_legacy_cli_name_lint.py.
_SEG_RE = re.compile(r"&&|\|\||;|\|")
# ``| `cmd sub` | Signature | …`` rows of a CLI reference table.
_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z0-9 -]*)`\s*\|")
# …only under a header whose first column is "Command".
_HEADER_RE = re.compile(r"^\|\s*Command\s*\|", re.IGNORECASE)


# ── the live parser, indexed ──────────────────────────────────────────────────

def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _index():
    """command -> {sub: leaf_parser} for groups, or {None: leaf_parser} for leaves."""
    root = _subparsers(build_cli_parser())
    assert root is not None, "build_cli_parser produced no subparsers"
    out = {}
    for name, child in root.choices.items():
        sub = _subparsers(child)
        out[name] = dict(sub.choices) if sub is not None else {None: child}
    return out


def _resolve(index, command: str, sub: str | None):
    """Return (leaf_parser, None) or (None, failure reason)."""
    if command not in index:
        return None, f"unknown command `{command}`"
    leaves = index[command]
    if None in leaves:
        return leaves[None], None  # leaf command — trailing tokens are its args
    if sub is None:
        return None, f"`{command}` is a resource group and needs a sub-command"
    if sub not in leaves:
        return None, f"unknown sub-command `{command} {sub}`"
    return leaves[sub], None


def _positional_spec(parser) -> tuple[int, set[str]]:
    """(required positional count, flags that consume a following value)."""
    required = 0
    valued: set[str] = set()
    for a in parser._actions:
        if a.option_strings:
            if a.nargs != 0:
                valued.update(a.option_strings)
        elif isinstance(a.nargs, int):
            required += a.nargs
        elif a.nargs in (None, "+"):
            required += 1  # '+' needs at least one
    return required, valued


def _count_positionals(tokens: list[str], valued: set[str]) -> int:
    n = i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i += 2 if (t in valued and "=" not in t) else 1
            continue
        n += 1
        i += 1
    return n


# ── scanning ──────────────────────────────────────────────────────────────────

# A real command token, as opposed to a usage-template placeholder (`<cmd>`).
_VERB_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _command_tail(segment: str) -> str | None:
    """The argv tail of a juggle_cli.py invocation in ``segment``, else None."""
    m = _INVOKE_RE.search(segment)
    if not m:
        return None
    # An inline-code invocation ends at its closing backtick; prose after it
    # (which routinely contains apostrophes) is not part of the command.
    tail = m.group(1).split("`")[0]
    # …and a `$( … )` command substitution ends at its closing paren.
    prefix = segment[: m.start()]
    if prefix.count("$(") > prefix.count(")"):
        tail = tail.split(")")[0]
    return tail.strip()


def invocations(text: str):
    """Yield (lineno, tokens, line) for each live invocation in ``text``.

    ``tokens`` is None when the tail cannot be tokenised (a `\\`-continued
    multi-line command, or unbalanced quotes) — the command/sub-command are still
    read off the front, only the arity check is skipped.
    """
    for lineno, line in enumerate(text.splitlines(), 1):
        for seg in _SEG_RE.split(line):
            if seg.lstrip().startswith("!"):
                continue  # negated → removal assertion, not a live call
            tail = _command_tail(seg)
            if not tail:
                continue
            if tail.endswith("\\"):
                tokens = None  # continued on the next line
            else:
                try:
                    tokens = shlex.split(tail)
                except ValueError:
                    tokens = None
            head = tail.split()
            if not head or not _VERB_RE.match(head[0]):
                continue  # usage template (`… juggle_cli.py <cmd> [args]`)
            yield lineno, tokens, head, line.strip()


def table_rows(text: str):
    """Yield (lineno, [command, sub…], line) for CLI reference-table rows."""
    in_command_table = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if _HEADER_RE.match(stripped):
            in_command_table = True
            continue
        if not stripped.startswith("|"):
            in_command_table = False
            continue
        if not in_command_table:
            continue
        m = _ROW_RE.match(stripped)
        if m:
            yield lineno, m.group(1).split(), stripped


def _files():
    for d in _SCAN_DIRS:
        for f in sorted((_ROOT / d).rglob("*.md")):
            try:
                yield f, f.read_text()
            except (UnicodeDecodeError, OSError):
                continue


# ── the gates ─────────────────────────────────────────────────────────────────

def test_scan_is_not_vacuous():
    """The guard is only as good as its regexes — fail loud if they stop matching."""
    seen_cmds, n_inv, n_rows = set(), 0, 0
    for _f, text in _files():
        for _ln, _tokens, head, _line in invocations(text):
            n_inv += 1
            seen_cmds.add(head[0])
        n_rows += sum(1 for _ in table_rows(text))
    assert n_inv >= 40, f"invocation scan collapsed to {n_inv} hits"
    assert n_rows >= 15, f"reference-table scan collapsed to {n_rows} rows"
    assert {"thread", "agent", "memory"} <= seen_cmds


def test_every_prompt_cli_invocation_resolves():
    """REGRESSION PIN (2026-08-02): commands/start.md mandated a `recall` verb
    deleted by the grammar migration; personal-question recall silently no-op'd.

    Fails on any prompt-mandated command the live CLI parser cannot resolve.
    """
    index = _index()
    broken = []
    for f, text in _files():
        for lineno, _tokens, head, line in invocations(text):
            _leaf, reason = _resolve(index, head[0], head[1] if len(head) > 1 else None)
            if reason:
                broken.append(f"{f.relative_to(_ROOT)}:{lineno}  {reason}\n    {line}")
    assert not broken, (
        "Prompt files invoke juggle CLI commands that do not resolve — the "
        "orchestrator hits an argparse usage error (exit 2):\n" + "\n".join(broken)
    )


def test_every_prompt_cli_invocation_supplies_required_positionals():
    """REGRESSION PIN (2026-08-02): the same drift by ARITY — commands/start.md's
    auto-retain rule called `memory retain "<fact>"`, one positional short of the
    two `memory retain` requires, so every auto-retain also exited 2.

    Only under-supply is flagged, and only for invocations that tokenise cleanly.
    """
    index = _index()
    broken = []
    for f, text in _files():
        for lineno, tokens, head, line in invocations(text):
            if tokens is None:
                continue  # multi-line / untokenisable — verb check still applied
            leaf, reason = _resolve(index, tokens[0], tokens[1] if len(tokens) > 1 else None)
            if reason or leaf is None:
                continue  # reported by the resolution gate above
            is_group = None not in index[tokens[0]]
            rest = tokens[2:] if is_group else tokens[1:]
            required, valued = _positional_spec(leaf)
            got = _count_positionals(rest, valued)
            if got < required:
                broken.append(
                    f"{f.relative_to(_ROOT)}:{lineno}  `{' '.join(head[:2])}` needs "
                    f"{required} positional(s), prompt supplies {got}\n    {line}"
                )
    assert not broken, (
        "Prompt files invoke juggle CLI commands with too few arguments — the "
        "orchestrator hits an argparse usage error (exit 2):\n" + "\n".join(broken)
    )


def test_cli_reference_tables_name_live_commands():
    """REGRESSION PIN (2026-08-02): commands/start.md's CLI Reference table still
    listed `notify` (renamed to `action notify`) and `update-summary` (removed),
    teaching the orchestrator a vocabulary the CLI no longer accepts."""
    index = _index()
    broken = []
    for f, text in _files():
        for lineno, cells, line in table_rows(text):
            _leaf, reason = _resolve(index, cells[0], cells[1] if len(cells) > 1 else None)
            if reason:
                broken.append(f"{f.relative_to(_ROOT)}:{lineno}  {reason}\n    {line}")
    assert not broken, (
        "CLI reference tables document commands that do not resolve:\n"
        + "\n".join(broken)
    )


# ── the guard's own teeth ─────────────────────────────────────────────────────

def test_guard_rejects_deleted_verb_and_accepts_its_replacement():
    index = _index()
    gone = list(invocations('uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall-bg B "q"'))
    assert gone, "regex failed to match a canonical invocation"
    assert _resolve(index, gone[0][2][0], gone[0][2][1])[1] is not None
    ok = list(invocations('uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py memory recall B "q"'))
    assert _resolve(index, ok[0][2][0], ok[0][2][1])[1] is None


def test_guard_rejects_under_supplied_arity():
    """The exact pre-fix start.md:138 line must be caught, prose and all."""
    index = _index()
    line = (
        '**Auto-retain personal data:** … call `uv run '
        '${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py memory retain "<fact>"` in '
        "background. Don't wait to be asked."
    )
    hits = list(invocations(line))
    assert hits, "inline-code invocation inside prose was not matched"
    tokens = hits[0][1]
    assert tokens == ["memory", "retain", "<fact>"], tokens
    leaf, reason = _resolve(index, tokens[0], tokens[1])
    assert reason is None
    required, valued = _positional_spec(leaf)
    assert _count_positionals(tokens[2:], valued) < required


def test_prose_mention_is_not_an_invocation():
    """A bare mention with no runner (commands/init.md prose) is not scanned."""
    prose = '# (KEY=VALUE format — no "export" prefix; juggle_cli.py loads this automatically)'
    assert list(invocations(prose)) == []


def test_negated_invocation_is_exempt():
    """`! uv run … <verb>` is a removal PROOF, not a live call."""
    assert list(invocations("! uv run src/juggle_cli.py recall A 'q' 2>/dev/null")) == []


def test_table_scan_ignores_non_command_tables():
    """Only tables whose first column is `Command` are treated as CLI vocabulary."""
    other = "| Setting | Default |\n| --- | --- |\n| `enabled` | false |"
    assert list(table_rows(other)) == []
    cli = "| Command | Signature |\n| --- | --- |\n| `thread create` | `<label>` |"
    assert [c for _, c, _ in table_rows(cli)] == [["thread", "create"]]
