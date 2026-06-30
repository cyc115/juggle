"""juggle_cli_commands — the aggregated declarative COMMANDS table (P9 R3).

Concatenates the four per-domain command tables (split along the same domain
seams as the original register() walls, each kept under the LOC gate) into the
single ``COMMANDS`` tuple that ``juggle_cli_spec.build_parser`` consumes. Data
only; importing this pulls in the real handler modules (eager, as the walls do).
"""

from __future__ import annotations

from juggle_cli_spec import Cmd
from juggle_cli_commands_threads import THREAD_COMMANDS
from juggle_cli_commands_agents import AGENT_COMMANDS
from juggle_cli_commands_misc import MISC_COMMANDS
from juggle_cli_commands_selfheal import SELFHEAL_COMMANDS

COMMANDS: tuple[Cmd, ...] = (
    *THREAD_COMMANDS,
    *AGENT_COMMANDS,
    *MISC_COMMANDS,
    *SELFHEAL_COMMANDS,
)
