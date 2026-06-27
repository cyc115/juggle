#!/usr/bin/env python3
"""P8-completion Step 1 watchdog done-gate. See scripts/p8_verify_common.py.

verify-cmd: uv run python scripts/p8_verify_step1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p8_verify_common import run  # noqa: E402

run(1)
