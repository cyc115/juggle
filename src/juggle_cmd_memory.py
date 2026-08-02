#!/usr/bin/env python3
"""juggle_cmd_memory — handlers for the `memory` resource group.

Pure extraction of ``cmd_retain`` out of ``juggle_cmd_context.py`` (a
grab-bag that sat at 298/300 lines, i.e. at the LOC gate). The ``memory``
resource now has a module of its own, matching the ``juggle_cmd_<domain>.py``
convention used by every other resource group.
"""

from __future__ import annotations

from juggle_cli_common import _get_hindsight_client


def cmd_retain(args):
    """Retain content as memory in Hindsight."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    context = getattr(args, "context", None)
    client.retain(args.content, context=context)
