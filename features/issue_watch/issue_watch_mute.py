"""
Issue Watch mute — ``/off`` silences major-P0 detection everywhere, ``/on`` brings it back.

For when the classifier keeps flagging non-incident chatter: duty types ``/off`` in any detection
group (or in the alert DM) and no further alerts are evaluated or sent for ANY detection group,
until someone types ``/on``. One switch, so it does not matter which group the noise came from or
where the command was typed.

State is held in memory and mirrored to ``P0_SHARED_STATE_DIR`` when that is set, so
``systemctl restart`` does not silently un-mute detection that duty deliberately quieted.
An optional ``P0_ISSUE_WATCH_MUTE_MAX_MIN`` adds an auto-resume timer (default 0 = only ``/on``).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from p0_logic import config as _config
from features.overview import draft_store as _ds

log = logging.getLogger("lark-ops-ai")

_LOCK = threading.RLock()
# Empty dict = detection active. Otherwise {"muted_at": int, "until": int, "by": open_id}.
_MUTE: Dict[str, Any] = {}
_loaded = False
_disk_mtime = -1.0


def _path() -> str:
    base = _ds.shared_state_dir()
    return os.path.join(base, "issue_watch_mute.json") if base else ""


def _load_from_disk_if_changed() -> None:
    """Adopt the on-disk state on first use and whenever another worker rewrote it."""
    global _loaded, _disk_mtime, _MUTE
    path = _path()
    if not path:
        _loaded = True
        return
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _loaded = True
        return
    if _loaded and mtime == _disk_mtime:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            row = json.load(f)
        _MUTE = dict(row) if isinstance(row, dict) and row.get("muted_at") else {}
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_mute: load failed path=%s err=%s", path, e)
    _disk_mtime = mtime
    _loaded = True


def _save_to_disk() -> None:
    global _disk_mtime
    path = _path()
    if not path:
        return
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_MUTE, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _disk_mtime = os.stat(path).st_mtime
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_mute: save failed path=%s err=%s", path, e)


def mute(by_open_id: str = "") -> int:
    """Silence detection for every group. Returns the auto-resume epoch (0 = until ``/on``)."""
    max_min = _config.get_p0_issue_watch_mute_max_min()
    now = int(time.time())
    until = now + max_min * 60 if max_min > 0 else 0
    with _LOCK:
        _load_from_disk_if_changed()
        _MUTE.clear()
        _MUTE.update({"muted_at": now, "until": until, "by": (by_open_id or "").strip()})
        _save_to_disk()
    log.info(
        "issue_watch_mute: MUTED all detection groups by_tail=%s until=%s",
        (by_open_id or "")[-8:],
        until or "(no expiry)",
    )
    return until


def unmute() -> bool:
    """Resume detection everywhere. Returns True when it was actually muted."""
    with _LOCK:
        _load_from_disk_if_changed()
        was_muted = bool(_MUTE)
        if was_muted:
            _MUTE.clear()
            _save_to_disk()
    if was_muted:
        log.info("issue_watch_mute: RESUMED all detection groups")
    return was_muted


def is_muted() -> bool:
    with _LOCK:
        _load_from_disk_if_changed()
        if not _MUTE:
            return False
        until = int(_MUTE.get("until") or 0)
        if until and time.time() >= until:
            _MUTE.clear()
            _save_to_disk()
            log.info("issue_watch_mute: auto-resumed after P0_ISSUE_WATCH_MUTE_MAX_MIN")
            return False
        return True


def muted_until() -> int:
    """Auto-resume epoch (0 = no expiry / not muted)."""
    with _LOCK:
        _load_from_disk_if_changed()
        return int(_MUTE.get("until") or 0)


def describe_mute_window() -> str:
    """Auto-resume tail for the ``/off`` acknowledgement, e.g. ``auto-resumes in 2h``.

    Empty by default: a mute holds until ``/on``, and only an explicit
    ``P0_ISSUE_WATCH_MUTE_MAX_MIN`` adds a timer.
    """
    max_min = _config.get_p0_issue_watch_mute_max_min()
    if max_min <= 0:
        return ""
    if max_min % 60 == 0:
        return "auto-resumes in %dh" % (max_min // 60)
    return "auto-resumes in %dm" % max_min


def mute_hint_text() -> Optional[str]:
    """Footer line telling duty how to silence detection that keeps false-alarming."""
    if not _config.get_p0_issue_watch_mute_command_enabled():
        return None
    window = describe_mute_window()
    window = f" ({window})" if window else ""
    return (
        f"*Wrongly detected? Type **/off** here or in a detection group to mute major P0 detection"
        f"{window} — it stays off for every group until someone types **/on**.*"
    )
