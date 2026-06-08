"""
Forward Lark VC cloud recordings to configured group chats.

Primary path: **vc.meeting.recording_ready_v1** webhook (subscribe in developer console).

Fallback: after **vc.meeting.meeting_ended_v1**, poll ``GET .../meetings/{id}/recording`` a few times —
covers missing event subscription and delayed processing. Deduped per ``meeting.id``.

Note: very short meetings (often under ~5s) may produce **no** recording — Feishu/Lark may omit files.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_FANOUT_LOCK = threading.Lock()
_FANOUT_DONE: Set[str] = set()
_POLL_ACTIVE: Set[str] = set()

# Seconds to wait after meeting_ended before each poll attempt (total ~3.75 min spread).
_DEFAULT_POLL_GAPS_SEC = (15, 30, 60, 120)


def _usable_recording_url(u: str) -> bool:
    """
    Lark sometimes puts a placeholder in event ``url`` (e.g. \"Access restricted\") when the app
    cannot expose the real Minutes link yet. Only accept real HTTP(S) URLs with a hostname.
    """
    s = (u or "").strip()
    if not s:
        return False
    low = s.lower()
    for bad in (
        "access restricted",
        "restricted access",
        "no access permission",
        "无权限",
    ):
        if bad in low:
            return False
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    try:
        p = urlparse(s)
        if not p.netloc or "." not in p.netloc:
            return False
    except Exception:
        return False
    return True


def _fmt_duration_ms(raw: str) -> str:
    try:
        ms = int(str(raw).strip() or "0")
    except ValueError:
        return ""
    if ms <= 0:
        return ""
    sec = ms // 1000
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m2 = divmod(m, 60)
    return f"{h}h {m2}m"


def _resolve_recording_url(token: str, meeting_id: str, url_hint: str) -> str:
    """Event ``url`` when playable; else ``GET .../meetings/{id}/recording``."""
    hint = (url_hint or "").strip()
    if _usable_recording_url(hint):
        return hint
    if hint:
        log.info(
            "vc recording: event url unusable head=%r meeting_id=%s — fetching API",
            hint[:80],
            meeting_id[:20],
        )
    fetched = _lark.fetch_vc_meeting_recording_url(token, meeting_id)
    if _usable_recording_url(fetched):
        return fetched
    if fetched:
        log.info(
            "vc recording: API url unusable head=%r meeting_id=%s",
            fetched[:80],
            meeting_id[:20],
        )
    return ""


def _meeting_duration_sec_from_ended_evt(evt: Dict[str, Any]) -> float:
    """Best-effort duration from meeting_ended / join payload (Lark uses second unix timestamps in strings)."""
    meeting = evt.get("meeting") if isinstance(evt.get("meeting"), dict) else {}
    try:
        st = int(str(meeting.get("start_time") or "0").strip() or "0")
        en = int(str(meeting.get("end_time") or "0").strip() or "0")
    except ValueError:
        return -1.0
    if st <= 0 or en <= 0 or en < st:
        return -1.0
    return float(en - st)


def fanout_recording_to_chats(
    tenant_token: str,
    meeting_id: str,
    topic: str,
    meeting_no: str,
    *,
    url_hint: str = "",
    duration_ms_raw: str = "",
    source: str = "event",
) -> bool:
    """
    If fan-out targets are set and topic passes filter, notify each chat/DM with topic, ids,
    duration, and the playable recording URL (event url or ``GET .../recording``).

    Returns True when at least one Lark post succeeded. Uses ``meeting_id`` for deduplication.
    Poll attempts return False until a usable URL exists (retry on next poll).
    """
    token = (tenant_token or "").strip()
    mid = (meeting_id or "").strip()
    if not token or not mid:
        return False
    targets = _config.get_vc_recording_fanout_chat_ids()
    user_targets = _config.get_vc_recording_fanout_user_open_ids()
    if not targets and not user_targets:
        return False

    topic = (topic or "").strip()
    meeting_no = (meeting_no or "").strip()
    filt = _config.get_vc_recording_fanout_topic_substring_filter()
    if filt and filt.lower() not in topic.lower():
        log.info(
            "vc recording fan-out skipped (topic filter) source=%s topic_head=%r filter=%r",
            source,
            topic[:120],
            filt,
        )
        return False

    with _FANOUT_LOCK:
        if mid in _FANOUT_DONE:
            log.info("vc recording fan-out skip duplicate meeting_id=%s source=%s", mid[:24], source)
            return True

    recording_url = _resolve_recording_url(token, mid, url_hint)
    if not recording_url:
        log.info(
            "vc recording fan-out deferred (no playable URL yet) source=%s mid=%s",
            source,
            mid[:20],
        )
        return False

    duration_text = _fmt_duration_ms(duration_ms_raw)

    if _config.get_vc_recording_fanout_set_permission_enabled():
        grant_ok = _lark.grant_vc_recording_view_to_chat_groups(
            token, mid, targets, user_open_ids=user_targets
        )
        if not grant_ok:
            log.warning(
                "vc recording fan-out: set_permission grant failed mid=%s topic_head=%r",
                mid[:24],
                topic[:80],
            )

    body = _cards.build_recording_available_text(
        topic,
        meeting_no,
        meeting_id=mid,
        recording_url=recording_url,
        duration_text=duration_text,
    )

    ok_any = False
    for oc in targets:
        try:
            st, resp = _lark.post_text_to_chat(oc, token, body)
            if st == 200:
                ok_any = True
                log.info(
                    "vc recording fan-out ok source=%s chat_id_tail=%s mid=%s",
                    source,
                    oc[-12:] if len(oc) > 12 else oc,
                    mid[:20],
                )
            else:
                log.warning(
                    "vc recording fan-out HTTP=%s source=%s chat=%s body_head=%s",
                    st,
                    source,
                    oc[:24],
                    (resp or "")[:200],
                )
        except Exception as e:
            log.warning("vc recording fan-out exception source=%s chat=%s err=%s", source, oc[:24], e)

    for ou in user_targets:
        try:
            st, resp = _lark.post_text_to_open_id(ou, token, body)
            if st == 200:
                ok_any = True
                log.info(
                    "vc recording fan-out ok (DM) source=%s open_id_tail=%s mid=%s",
                    source,
                    ou[-12:] if len(ou) > 12 else ou,
                    mid[:20],
                )
            else:
                log.warning(
                    "vc recording fan-out DM HTTP=%s source=%s open_id=%s body_head=%s",
                    st,
                    source,
                    ou[:24],
                    (resp or "")[:200],
                )
        except Exception as e:
            log.warning("vc recording fan-out DM exception source=%s open_id=%s err=%s", source, ou[:24], e)

    if ok_any:
        with _FANOUT_LOCK:
            _FANOUT_DONE.add(mid)
    return ok_any


def handle_vc_recording_ready_fanout(evt: Dict[str, Any], tenant_token: str) -> None:
    meeting = evt.get("meeting") if isinstance(evt.get("meeting"), dict) else {}
    topic = str(meeting.get("topic") or "").strip()
    meeting_no = str(meeting.get("meeting_no") or "").strip()
    meeting_id = str(meeting.get("id") or "").strip()
    url = str(evt.get("url") or "").strip()
    duration_raw = str(evt.get("duration") or "").strip()
    fanout_recording_to_chats(
        tenant_token,
        meeting_id,
        topic,
        meeting_no,
        url_hint=url,
        duration_ms_raw=duration_raw,
        source="recording_ready",
    )


def schedule_recording_fanout_poll_after_meeting_end(
    tenant_token: str,
    evt: Dict[str, Any],
) -> None:
    """
    Poll recording API a few times — handles unsubscribed ``recording_ready`` and upload delay.
    Skips if fan-out targets unset or meeting already fan-out done.
    """
    token = (tenant_token or "").strip()
    if not token:
        return
    if not _config.get_vc_recording_fanout_chat_ids() and not _config.get_vc_recording_fanout_user_open_ids():
        return

    meeting = evt.get("meeting") if isinstance(evt.get("meeting"), dict) else {}
    topic = str(meeting.get("topic") or "").strip()
    meeting_no = str(meeting.get("meeting_no") or "").strip()
    meeting_id = str(meeting.get("id") or "").strip()
    if not meeting_id:
        return

    filt = _config.get_vc_recording_fanout_topic_substring_filter()
    if filt and filt.lower() not in topic.lower():
        return

    dur_sec = _meeting_duration_sec_from_ended_evt(evt)
    if 0 < dur_sec < 5.0:
        log.warning(
            "vc meeting_ended: duration≈%.1fs — cloud recording may be skipped by Lark (try ≥5–10s). mid=%s",
            dur_sec,
            meeting_id[:20],
        )

    with _FANOUT_LOCK:
        if meeting_id in _FANOUT_DONE or meeting_id in _POLL_ACTIVE:
            return
        _POLL_ACTIVE.add(meeting_id)

    gaps = _DEFAULT_POLL_GAPS_SEC

    def _worker() -> None:
        try:
            for i, gap in enumerate(gaps):
                time.sleep(float(gap))
                if fanout_recording_to_chats(
                    token,
                    meeting_id,
                    topic,
                    meeting_no,
                    url_hint="",
                    duration_ms_raw="",
                    source=f"poll#{i + 1}",
                ):
                    log.info(
                        "vc recording poll: success on attempt %s/%s meeting_id=%s",
                        i + 1,
                        len(gaps),
                        meeting_id[:24],
                    )
                    return
            log.info(
                "vc recording poll: no recording URL after %s attempts (too short, disabled, or admin-only?) mid=%s",
                len(gaps),
                meeting_id[:24],
            )
        finally:
            with _FANOUT_LOCK:
                _POLL_ACTIVE.discard(meeting_id)

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"vc-recpoll-{meeting_id[:8]}",
    ).start()
