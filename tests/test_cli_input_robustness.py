"""CLI input-robustness regression pins (don't crash on bad input).

Each test reproduces a specific incident where bad user input raised an
uncaught exception (traceback) instead of a clean error + non-zero exit.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_project_create_invalid_json_no_traceback(capsys):
    """Incident 64a4b608 (project create): `--success-criteria` with empty /
    non-JSON input raised json.decoder.JSONDecodeError instead of a clean error.
    """
    from juggle_cmd_projects import cmd_project_create

    args = SimpleNamespace(
        force=True,
        name="Demo",
        objective="Do a thing",
        success_criteria="not json",
        out_of_scope="",
    )
    with pytest.raises(SystemExit) as exc:
        cmd_project_create(args)
    assert exc.value.code != 0
    out = capsys.readouterr()
    assert "json" in (out.out + out.err).lower()


def test_ack_action_non_numeric_id_no_traceback(capsys):
    """Incident 01a161e4 (ack-action): a short-hash id (e.g. 'ce80ef') hit
    int(args.action_id) and raised ValueError instead of a clean error.
    """
    from juggle_cmd_agents import cmd_ack_action

    args = SimpleNamespace(action_id="ce80ef")
    with pytest.raises(SystemExit) as exc:
        cmd_ack_action(args)
    assert exc.value.code != 0
