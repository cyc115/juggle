"""Class B attribution is CAUSAL (per tool_result), not temporal (per turn).

Incident 2026-08-02: "proximity-based Class B attribution filed unrelated shell
failures as Juggle-caused". `_attribute_tool_errors` searched the concatenated
inputs of the last N=10 tool_uses for a juggle path; one hit marked the WHOLE
turn, and then every errored tool_result in that turn was recorded as a Juggle
defect. All 23 open error_events rows in the prod ledger were false positives
(ssh host-key failures, `tofu not found`, a git rebase conflict in an unrelated
repo, `grep` exiting 1 on no match, ...). Attribution now keys off the erroring
tool_result's OWN originating tool_use input.
"""
import juggle_hooks_classb as hc


def _capture(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr("juggle_selfheal.record_orchestration_error",
                        lambda *a, **k: calls.append(a))
    return calls


def test_unrelated_failure_in_juggle_turn_is_not_recorded(monkeypatch):
    """REGRESSION pin (2026-08-02): proximity-based Class B attribution filed
    unrelated shell failures as Juggle-caused. A turn that merely CONTAINS a
    juggle tool_use must not launder an unrelated failure into the ledger."""
    calls = _capture(monkeypatch)
    uses = [
        # Juggle call — succeeds, so it is not the failure under attribution.
        {"id": "u1", "name": "Bash",
         "input": {"command": "uv run src/juggle_cli.py cockpit --out"}},
        # Unrelated homelab call — the one that actually failed.
        {"id": "u2", "name": "Bash",
         "input": {"command": "ssh services 'uptime'"}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "u1", "is_error": False,
         "content": "cockpit rendered"},
        {"type": "tool_result", "tool_use_id": "u2", "is_error": True,
         "content": "ssh: Host key verification failed."},
    ]
    assert hc._attribute_tool_errors(uses, results) == 0
    assert calls == [], "unrelated ssh failure must NOT be a Juggle defect"


def test_genuine_juggle_failure_is_still_recorded(monkeypatch):
    """True positives must survive the causal narrowing: the failing tool_use's
    OWN input references juggle machinery."""
    calls = _capture(monkeypatch)
    uses = [{"id": "u1", "name": "Skill", "input": {"skill": "juggle:doctor"}}]
    results = [{"type": "tool_result", "tool_use_id": "u1", "is_error": True,
                "content": "InputValidationError: 'command' is a required property"}]
    hc._attribute_tool_errors(uses, results)
    assert len(calls) == 1
    tool, tool_input, error_text, juggle_ref = calls[0]
    assert tool == "Skill"
    assert juggle_ref == "juggle:"


def test_mixed_turn_records_only_the_juggle_failure(monkeypatch):
    """Two failures in one turn: only the one whose own input names juggle."""
    calls = _capture(monkeypatch)
    uses = [
        {"id": "u1", "name": "Bash",
         "input": {"command": "uv run src/juggle_cli.py graph status"}},
        {"id": "u2", "name": "Bash", "input": {"command": "git rebase origin/main"}},
        {"id": "u3", "name": "Read",
         "input": {"file_path": "/home/mike/notes/missing.md"}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "u1", "is_error": True,
         "content": "Traceback (most recent call last): KeyError: 'graph'"},
        {"type": "tool_result", "tool_use_id": "u2", "is_error": True,
         "content": "CONFLICT (content): Merge conflict in app/main.py"},
        {"type": "tool_result", "tool_use_id": "u3", "is_error": True,
         "content": "File does not exist."},
    ]
    hc._attribute_tool_errors(uses, results)
    assert len(calls) == 1, f"expected only the juggle_cli failure, got {calls}"
    assert calls[0][3] == "juggle_cli.py"


def test_orphan_tool_result_is_not_recorded(monkeypatch):
    """A tool_result whose tool_use fell outside the transcript tail has no
    input to attribute — fail closed rather than guess from neighbours."""
    calls = _capture(monkeypatch)
    uses = [{"id": "u1", "name": "Bash",
             "input": {"command": "uv run src/juggle_cli.py doctor"}}]
    results = [{"type": "tool_result", "tool_use_id": "u-missing", "is_error": True,
                "content": "command not found: sqlite3"}]
    hc._attribute_tool_errors(uses, results)
    assert calls == []


def test_bare_commands_dir_no_longer_attributes(monkeypatch):
    """`_JUGGLE_PATHS` narrowing (2026-08-02): the bare "commands/" entry matched
    ANY repo with a commands/ directory, so an unrelated file error there was
    filed as a Juggle defect. Only juggle's own commands/ dir attributes now."""
    calls = _capture(monkeypatch)
    uses = [
        {"id": "u1", "name": "Read",
         "input": {"file_path": "/home/mike/workspace/other-repo/commands/deploy.py"}},
        {"id": "u2", "name": "Read",
         "input": {"file_path": "/home/mike/workspace/github/juggle/commands/init.md"}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "u1", "is_error": True,
         "content": "File does not exist."},
        {"type": "tool_result", "tool_use_id": "u2", "is_error": True,
         "content": "File does not exist."},
    ]
    hc._attribute_tool_errors(uses, results)
    assert len(calls) == 1, f"only juggle's own commands/ attributes, got {calls}"
    assert calls[0][3] == "juggle/commands/"


def test_scan_transcript_end_to_end_ignores_unrelated_failure(monkeypatch, tmp_path):
    """Same incident shape through the real transcript path, not just the helper."""
    calls = _capture(monkeypatch)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"check the homelab"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use",'
        '"id":"a1","name":"Skill","input":{"skill":"juggle:start"}}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"a1","is_error":false,"content":"juggle mode on"}]}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use",'
        '"id":"a2","name":"Bash","input":{"command":"ansible-playbook site.yml"}}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
        '"tool_use_id":"a2","is_error":true,"content":"An executable named '
        "'ansible-playbook' is not provided by package 'ansible'\"}]}}\n"
    )
    hc._do_class_b_scan(transcript)
    assert calls == []
