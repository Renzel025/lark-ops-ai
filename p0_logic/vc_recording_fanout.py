"""
Forward Lark **vc.meeting.recording_ready_v1** recording links to configured group chats.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")


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


def handle_vc_recording_ready_fanout(evt: Dict[str, Any], tenant_token: str) -> None:
    token = (tenant_token or "").strip()
    if not token:
        return
    targets = _config.get_vc_recording_fanout_chat_ids()
    if not targets:
        log.info("vc recording_ready: VC_RECORDING_FANOUT_CHAT_IDS empty; skip fan-out")
        return

    meeting = evt.get("meeting") if isinstance(evt.get("meeting"), dict) else {}
    topic = str(meeting.get("topic") or "").strip()
    meeting_no = str(meeting.get("meeting_no") or "").strip()
    meeting_id = str(meeting.get("id") or "").strip()

    filt = _config.get_vc_recording_fanout_topic_substring_filter()
    if filt and filt.lower() not in topic.lower():
        log.info(
            "vc recording_ready: skipped (topic_substring filter) topic_head=%r filter=%r",
            topic[:120],
            filt,
        )
        return

    url = str(evt.get("url") or "").strip()
    if not url and meeting_id:
        url = _lark.fetch_vc_meeting_recording_url(token, meeting_id)
    if not url:
        log.warning(
            "vc recording_ready: no recording url meeting_id=%s topic_head=%r",
            meeting_id[:28] if meeting_id else "",
            topic[:120],
        )
        return

    duration_raw = str(evt.get("duration") or "").strip()
    dur = _fmt_duration_ms(duration_raw)
    lines = [
        "☁️ **Meeting recording ready** / **会议录制已就绪**",
    ]
    if topic:
        lines.append(f"**Topic / 主题:** {topic}")
    if meeting_no:
        lines.append(f"**Meeting no / 会议号:** {meeting_no}")
    if dur:
        lines.append(f"**Duration / 时长:** {dur}")
    lines.append(f"**Link / 链接:** {url}")
    body = "\n".join(lines)

    for oc in targets:
        try:
            st, resp = _lark.post_text_to_chat(oc, token, body)
            if st == 200:
                log.info("vc recording_ready: fan-out ok chat_id_tail=%s", oc[-12:] if len(oc) > 12 else oc)
            else:
                log.warning(
                    "vc recording_ready: fan-out HTTP=%s chat=%s body_head=%s",
                    st,
                    oc[:24],
                    (resp or "")[:200],
                )
        except Exception as e:
            log.warning("vc recording_ready: fan-out exception chat=%s err=%s", oc[:24], e)
