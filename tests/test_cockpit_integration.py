import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Version bump test
# ---------------------------------------------------------------------------


def test_plugin_version_is_1_11_0():
    plugin_json = Path(__file__).parent.parent / ".claude-plugin" / "plugin.json"
    data = json.loads(plugin_json.read_text())
    version = data["version"]
    assert tuple(int(x) for x in version.split(".")) >= (1, 11, 0), (
        f"Expected version ≥ 1.11.0, got {data['version']}"
    )
