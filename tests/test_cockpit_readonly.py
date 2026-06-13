"""Assert that CockpitApp._refresh() never calls reap_stale_agents."""
import ast
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

_COCKPIT_PATH = Path(__file__).parent.parent / "src" / "juggle_cockpit.py"


def _find_method(tree: ast.AST, class_name: str, method_name: str) -> ast.FunctionDef | None:
    for task in ast.walk(tree):
        if isinstance(task, ast.ClassDef) and task.name == class_name:
            for item in ast.walk(task):
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    return None


def _reap_calls_in(func: ast.FunctionDef) -> list[ast.Call]:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "reap_stale_agents"
    ]


def test_refresh_does_not_call_reap_stale_agents():
    """_refresh must not call reap_stale_agents — cockpit is display-only."""
    source = _COCKPIT_PATH.read_text()
    tree = ast.parse(source)

    refresh = _find_method(tree, "CockpitApp", "_refresh")
    assert refresh is not None, "CockpitApp._refresh not found"

    calls = _reap_calls_in(refresh)
    assert len(calls) == 0, (
        f"_refresh still contains {len(calls)} reap_stale_agents call(s) — "
        "cockpit must be read-only (remove the throttled-reap block)"
    )


def test_last_reap_attribute_removed():
    """_last_reap attribute must be removed from CockpitApp.__init__."""
    source = _COCKPIT_PATH.read_text()
    tree = ast.parse(source)

    init = _find_method(tree, "CockpitApp", "__init__")
    assert init is not None, "CockpitApp.__init__ not found"

    # Look for self._last_reap = ... (Assign) and self._last_reap: T = ... (AnnAssign)
    assigns = []
    for n in ast.walk(init):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Attribute) and t.attr == "_last_reap"
                        and isinstance(t.value, ast.Name) and t.value.id == "self"):
                    assigns.append(n)
        elif isinstance(n, ast.AnnAssign):
            t = n.target
            if (isinstance(t, ast.Attribute) and t.attr == "_last_reap"
                    and isinstance(t.value, ast.Name) and t.value.id == "self"):
                assigns.append(n)
    assert len(assigns) == 0, (
        "_last_reap is still assigned in __init__ — remove it along with the reap block"
    )
