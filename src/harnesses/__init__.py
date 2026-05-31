"""Self-contained harness adapters.

Each module here owns ONE harness end-to-end (launch + restriction
materialization + context delivery + capabilities) and self-registers with the
framework via ``juggle_harness.register_adapter`` at import time. Importing this
package imports every adapter, triggering their registration.

To add a harness: drop ``harnesses/<name>.py`` that subclasses
``juggle_harness.HarnessAdapter`` and calls ``register_adapter`` at import, then
add it to the import list below. The conformance suite
(``tests/test_harness_conformance.py``) will auto-discover and gate it.
"""

# Importing each module runs its register_adapter() call.
from . import claude  # noqa: F401
from . import codex  # noqa: F401
