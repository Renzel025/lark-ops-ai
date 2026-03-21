"""
Meeting participants list and lookup (for the active P0 session).
"""
from __future__ import annotations

import logging
import re
from typing import Any, List

from . import session as _session
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")

# Label appended when someone joined VC but their display name is not in the support sheet.
_UNMAPPED_DEPT_LABEL = "Other"


def format_participants_names_display(names: List[Any]) -> str:
    """
    Bulleted list of unique VC display names (no support-sheet → department mapping).

    Used for DM \"Participants\" and ongoing-meeting card attendee list.
    """
    out: List[str] = []
    seen: set = set()
    for p in names:
        name = str(p or "").strip()
        if not name:
            continue
        key = _text.normalize_lookup_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    if not out:
        return ""
    return "\n".join(f"• {n}" for n in out)


def departments_line_from_names(names: List[Any], tenant_token: str) -> str:
    """
    Map participant display names → department using SUPPORT sheet (column A: name, B: dept).

    Many people from the same department still produce one token, e.g.:
    ``FPMS, CPMS, FE``
    """
    from . import support as _support

    mp = _support.get_support_map(tenant_token) if tenant_token else {}
    teams: List[str] = []
    seen: set = set()
    has_unmapped = False
    for p in names:
        name = str(p or "").strip()
        if not name:
            continue
        dept = _support.match_dept_from_name(mp, name) if mp else ""
        if dept:
            kk = dept.lower()
            if kk not in seen:
                seen.add(kk)
                teams.append(dept)
        else:
            has_unmapped = True
    if has_unmapped:
        key_other = _UNMAPPED_DEPT_LABEL.lower()
        if key_other not in seen:
            seen.add(key_other)
            teams.append(_UNMAPPED_DEPT_LABEL)
    if not teams:
        return "No participant info yet"
    return ", ".join(teams)


def list_meeting_participants() -> List[str]:
    sess = _session.get_active_session() or {}
    raw = sess.get("participants") or []
    out = []
    seen = set()
    for p in raw:
        name = str(p or "").strip()
        if not name:
            continue
        key = _text.normalize_lookup_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def is_person_in_meeting(name: str) -> bool:
    target = _text.normalize_lookup_name(name)
    if not target:
        return False
    for p in list_meeting_participants():
        cur = _text.normalize_lookup_name(p)
        if cur == target or target in cur or cur in target:
            return True
    return False


def strip_seeded_host_placeholder_for_open_id(open_id: str) -> None:
    """
    Remove ``Host (xxxxxx)`` seeded in start_p0 when the real user joins VC (same open_id suffix).
    """
    open_id = (open_id or "").strip()
    if len(open_id) < 6:
        return
    suf = open_id[-6:].lower()
    sess = _session.get_active_session()
    if not sess:
        return
    participants = sess.get("participants") or []

    def is_seeded_host(p: str) -> bool:
        s = str(p or "").strip()
        m = re.match(r"^Host\s*\(([a-fA-F0-9]{6})\)\s*$", s)
        return bool(m and m.group(1).lower() == suf)

    sess["participants"] = [p for p in participants if not is_seeded_host(str(p))]


def add_meeting_participant(name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    sess = _session.get_active_session()
    if not sess:
        return
    participants = sess.setdefault("participants", [])
    norm = _text.normalize_lookup_name(name)
    for p in participants:
        if _text.normalize_lookup_name(str(p)) == norm:
            return
    participants.append(name)
    log.info("Participant added: %s current=%s", name, participants)


def remove_meeting_participant(name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    sess = _session.get_active_session()
    if not sess:
        return
    norm = _text.normalize_lookup_name(name)
    participants = sess.get("participants") or []
    sess["participants"] = [p for p in participants if _text.normalize_lookup_name(str(p)) != norm]
    log.info("Participant removed: %s current=%s", name, sess.get("participants") or [])
