"""
Duty / on-call roster lookup: team (CPMS / FPMS / FE) -> current duty SRE ``open_id``.

Used by the ``@bot scpms / sfpms / sfe`` ring commands to page whoever is on duty for
a team into the active meeting.

PHASE 1 (this file): a STUB that reads a per-team open_id from an env placeholder
(``P0_VC_RING_DUTY_<TEAM>_OPEN_ID``), so the commands work end-to-end before the real
roster sheet exists.

PHASE 2 (TODO): replace the body of ``get_duty_open_id`` with a Lark Sheet read — same
TTL-cached pattern as ``p0_logic/support.py`` — once the actual duty sheet is dropped
into the repo and its layout (team column -> duty open_id) is known. Nothing else in
the feature needs to change; only this one function.
"""
from __future__ import annotations

import logging
import os

from p0_logic import config as _config

log = logging.getLogger("lark-ops-ai")

# Ring-command keyword -> team code.
COMMAND_TEAM = {
    "scpms": "CPMS",
    "sfpms": "FPMS",
    "sfe": "FE",
}


def get_duty_open_id(team: str) -> str:
    """Return the current duty SRE's Lark ``open_id`` for ``team`` (CPMS/FPMS/FE), or "".

    PHASE 1: reads ``P0_VC_RING_DUTY_<TEAM>_OPEN_ID`` from the environment.
    PHASE 2: swap this for the roster-sheet lookup.
    """
    t = (team or "").strip().upper()
    if not t:
        return ""
    _config.reload_env_runtime()
    env_name = f"P0_VC_RING_DUTY_{t}_OPEN_ID"
    oid = (os.getenv(env_name) or "").strip()
    if oid.startswith("ou_"):
        return oid
    if oid:
        log.warning("duty_roster: %s is set but is not an ou_ open_id: %r", env_name, oid[:16])
    return ""
