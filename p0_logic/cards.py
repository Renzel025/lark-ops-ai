"""
Lark interactive card builders and P0 overview markdown (English + translated 中文 body).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config as _config
from . import groq_client as _groq
from features.session import session as _session
from . import text_processing as _text

PHT = _config.PHT
MEETING_TOPIC = _config.MEETING_TOPIC


def initial_datetime_for_picker(start_epoch: int) -> str:
    """``yyyy-MM-dd HH:mm`` in PHT for ``picker_datetime.initial_datetime`` (Lark card)."""
    try:
        ts = int(start_epoch) if int(start_epoch) > 0 else int(time.time())
        return datetime.fromtimestamp(ts, tz=PHT).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return datetime.fromtimestamp(time.time(), tz=PHT).strftime("%Y-%m-%d %H:%M")


def parse_lark_datetime_picker_value(raw: str) -> int:
    """
    Parse Lark ``picker_datetime`` form value to Unix epoch seconds.
    Accepts values like ``2025-06-10 19:19 +0800`` (with offset) or ``2026-04-02 14:55`` (interpreted as PHT).
    Returns 0 if empty or unparseable.
    """
    s = (raw or "").strip()
    if not s:
        return 0
    for fmt in ("%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=PHT)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _build_emergency_title(priority: str, topic_line: str = "") -> str:
    prio = (priority or "P0").strip().upper()
    tail = (topic_line or "").strip() or MEETING_TOPIC
    return f"🚨 {prio} — {tail}"


def _build_meeting_invite_footer(priority: str) -> str:
    """Bottom line on the red meeting card (after Join); same for P0 / P1 and all incident groups."""
    prio = (priority or "P0").strip().upper()
    return f"{prio} declared - created a meeting please help to join"


def _button_open_url(
    *,
    content: str,
    url: str,
    button_type: str = "primary",
    element_id: str = "",
) -> Dict[str, Any]:
    """Schema 2.0 URL button — ``multi_url`` on buttons causes Lark 230099."""
    link = (url or "").strip()
    btn: Dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": (content or "Open")[:100]},
        "type": button_type,
        "behaviors": [
            {
                "type": "open_url",
                "default_url": link,
                "pc_url": link,
                "ios_url": link,
                "android_url": link,
            }
        ],
    }
    eid = (element_id or "").strip()
    if eid:
        btn["element_id"] = eid
    return btn


def _title_group_suffix(source_chat_label: str, max_chars: int = 34) -> str:
    """Append incident group name to card titles (Lark plain_text stays short)."""
    s = (source_chat_label or "").strip()
    if not s:
        return ""
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return f" — {s}"


def format_duration(start_epoch: int, end_epoch: Optional[int] = None) -> str:
    try:
        start_epoch = int(start_epoch or 0)
    except Exception:
        start_epoch = 0
    try:
        end_epoch = int(end_epoch or time.time())
    except Exception:
        end_epoch = int(time.time())
    if start_epoch <= 0 or end_epoch < start_epoch:
        return "Not available"
    total_seconds = end_epoch - start_epoch
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


MEETING_JOIN_LINK_LABEL = "join in meeting link"


def build_p0_meeting_created_text(
    link: str,
    *,
    priority: str = "P0",
    emergency_topic: str = "",
) -> str:
    """Single plain-text message (no asterisks) + raw URL on its own line for Lark VC unfurl."""
    prio = (priority or "P0").strip().upper()
    topic = (emergency_topic or "").strip() or MEETING_TOPIC
    url = (link or "").strip()
    lines: List[str] = [topic, "", f"🚨 {prio} meeting created.", "", MEETING_JOIN_LINK_LABEL]
    if url:
        lines.append(url)
    return "\n".join(lines)


def build_meeting_link_notice_card(
    link: str,
    *,
    priority: str = "P0",
    emergency_topic: str = "",
    patchable: bool = False,
) -> Dict[str, Any]:
    """
    Minimal card: **topic in header**, plain-text body (no ``**`` markdown).
    Post the VC ``link`` as a **separate** plain-text message right after so Lark unfurls
    the native meeting preview (participants, timer, Joined/Ended).
    """
    _ = link
    prio = (priority or "P0").strip().upper()
    topic = (emergency_topic or "").strip() or MEETING_TOPIC
    title = topic[:100] if len(topic) > 100 else topic
    cfg: Dict[str, Any] = {"enable_forward": True}
    if patchable:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": f"🚨 {prio} meeting created."},
                },
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": MEETING_JOIN_LINK_LABEL},
                },
            ],
        },
    }


def build_p0_meeting_created_link_card(
    link: str,
    *,
    emergency_topic: str = "",
) -> Dict[str, Any]:
    """Alias for boss fan-out — same minimal link card (not patchable)."""
    return build_meeting_link_notice_card(
        link, priority="P0", emergency_topic=emergency_topic, patchable=False
    )


def build_meeting_link_ended_card(
    *,
    priority: str = "P0",
    duration_text: str = "Not available",
    meeting_no: str = "",
    emergency_topic: str = "",
    update_multi: bool = False,
) -> Dict[str, Any]:
    """In-place update of the link notice card when the meeting ends."""
    prio = (priority or "P0").strip().upper()
    topic = (emergency_topic or "").strip() or MEETING_TOPIC
    title = topic[:100] if len(topic) > 100 else topic
    dur = (duration_text or "Not available").strip() or "Not available"
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": f"✅ {prio} meeting ended."}},
    ]
    if meeting_no:
        elements.append(
            {"tag": "div", "text": {"tag": "plain_text", "content": f"Meeting ID: {meeting_no}"}}
        )
    elements.append({"tag": "div", "text": {"tag": "plain_text", "content": f"Duration: {dur}"}})
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
    }


def build_meeting_link_cancelled_card(
    *,
    priority: str = "P0",
    duration_text: str = "Not available",
    meeting_no: str = "",
    reason: str = "Unspecified",
    emergency_topic: str = "",
    update_multi: bool = False,
) -> Dict[str, Any]:
    """In-place update of the link notice card when the meeting is cancelled."""
    prio = (priority or "P0").strip().upper()
    topic = (emergency_topic or "").strip() or MEETING_TOPIC
    title = topic[:100] if len(topic) > 100 else topic
    dur = (duration_text or "Not available").strip() or "Not available"
    cancel_reason = (reason or "Unspecified").strip() or "Unspecified"
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": f"🛑 {prio} meeting cancelled."}},
    ]
    if meeting_no:
        elements.append(
            {"tag": "div", "text": {"tag": "plain_text", "content": f"Meeting ID: {meeting_no}"}}
        )
    elements.extend(
        [
            {"tag": "div", "text": {"tag": "plain_text", "content": f"Duration: {dur}"}},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"Reason: {cancel_reason}"}},
        ]
    )
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
    }


def _strip_video_meeting_prefix(raw: str) -> str:
    """Drop ``Video meeting—`` from Lark VC topic — show incident label only in chat."""
    t = (raw or "").strip()
    if not t:
        return ""
    prefix = (_config.VIDEO_MEETING_TOPIC_PREFIX or "Video meeting—").strip()
    if prefix and t.lower().startswith(prefix.lower()):
        return t[len(prefix) :].strip()
    for p in ("Video meeting—", "Video meeting-", "Video meeting:"):
        if t.lower().startswith(p.lower()):
            return t[len(p) :].strip()
    return t


def _recording_ready_meta_lines(
    *,
    topic: str = "",
    meeting_no: str = "",
    meeting_id: str = "",
    recording_url: str = "",
    duration_text: str = "",
) -> List[str]:
    """Machine-parseable key=value lines for downstream Minutes bots."""
    label = _strip_video_meeting_prefix(topic)
    no = (meeting_no or "").strip()
    mid = (meeting_id or "").strip()
    dur = (duration_text or "").strip()
    url = (recording_url or "").strip()
    meta: List[str] = ["RECORDING_READY"]
    if mid:
        meta.append(f"meeting_id={mid}")
    if no:
        meta.append(f"meeting_no={no}")
    if url:
        meta.append(f"recording_url={url}")
    if label:
        meta.append(f"topic={label}")
    if dur:
        meta.append(f"duration={dur}")
    return meta


def build_recording_ready_meta_text(
    topic: str = "",
    meeting_no: str = "",
    *,
    meeting_id: str = "",
    recording_url: str = "",
    duration_text: str = "",
) -> str:
    """Compact text block (``RECORDING_READY`` only) for downstream bot parsers."""
    meta = _recording_ready_meta_lines(
        topic=topic,
        meeting_no=meeting_no,
        meeting_id=meeting_id,
        recording_url=recording_url,
        duration_text=duration_text,
    )
    return "\n".join(meta) if len(meta) > 1 else ""


def build_recording_available_text(
    topic: str = "",
    meeting_no: str = "",
    *,
    meeting_id: str = "",
    recording_url: str = "",
    duration_text: str = "",
) -> str:
    """Plain group/DM text when VC cloud recording is ready (includes link + ids for Minutes bots)."""
    lines = ["☁️ Meeting recording ready · 会议录制可用", ""]
    label = _strip_video_meeting_prefix(topic)
    if label:
        lines.append(f"Topic · 主题: {label}")
    no = (meeting_no or "").strip()
    if no:
        lines.append(f"Meeting no · 会议号: {no}")
    mid = (meeting_id or "").strip()
    if mid:
        lines.append(f"Lark meeting_id · 会议ID: {mid}")
    dur = (duration_text or "").strip()
    if dur:
        lines.append(f"Duration · 时长: {dur}")
    url = (recording_url or "").strip()
    if url:
        lines.append(f"Recording · 录制链接: {url}")
    elif mid:
        lines.append(
            "Recording · 录制链接: (pending — Lark is still processing; use meeting_id or check Minutes)"
        )
    meta = _recording_ready_meta_lines(
        topic=topic,
        meeting_no=meeting_no,
        meeting_id=meeting_id,
        recording_url=recording_url,
        duration_text=duration_text,
    )
    if len(meta) > 1:
        lines.extend(["---", *meta])
    return "\n".join(lines).rstrip()


def build_recording_available_card(
    topic: str = "",
    meeting_no: str = "",
    *,
    meeting_id: str = "",
    recording_url: str = "",
    duration_text: str = "",
) -> Dict[str, Any]:
    """Interactive card when VC cloud recording is ready."""
    label = _strip_video_meeting_prefix(topic)
    no = (meeting_no or "").strip()
    mid = (meeting_id or "").strip()
    dur = (duration_text or "").strip()
    url = (recording_url or "").strip()

    md_parts: List[str] = []
    if label:
        md_parts.append(f"**Topic · 主题:** {label}")
    if no:
        md_parts.append(f"**Meeting no · 会议号:** {no}")
    if mid:
        md_parts.append(f"**Lark meeting_id · 会议ID:** {mid}")
    if dur:
        md_parts.append(f"**Duration · 时长:** {dur}")
    if url:
        md_parts.append(f"**Recording · 录制链接:** [Open Minutes · 打开录制]({url})")
    elif mid:
        md_parts.append(
            "**Recording · 录制链接:** pending — Lark is still processing; "
            "check Minutes or use meeting ID above."
        )
    else:
        md_parts.append("**Recording · 录制链接:** not available yet.")

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n\n".join(md_parts) if md_parts else "Cloud recording is ready.",
            },
        },
    ]
    if url:
        elements.extend(
            [
                {"tag": "hr"},
                _button_open_url(
                    content="Open recording · 打开录制",
                    url=url,
                    button_type="primary",
                    element_id="open_recording",
                ),
            ]
        )

    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "☁️ Meeting recording ready · 会议录制可用"},
        },
        "body": {"elements": elements},
    }


def build_meeting_card(
    link: str,
    meeting_no: str = "",
    priority: str = "P0",
    affected_players: str = "",
    emergency_topic: str = "",
    *,
    patchable: bool = True,
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    title_text = _build_emergency_title(prio, emergency_topic)
    footer_text = _build_meeting_invite_footer(prio)
    meeting_line = f"Meeting ID: {meeting_no}" if meeting_no else "Meeting has been created."
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": meeting_line}},
        {"tag": "hr"},
        _button_open_url(content="Join meeting", url=link, button_type="primary", element_id="join_meeting"),
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "plain_text", "content": footer_text}},
    ]
    cfg: Dict[str, Any] = {"enable_forward": True}
    if patchable:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "red", "title": {"tag": "plain_text", "content": title_text}},
        "body": {"elements": elements},
    }


def build_ongoing_meeting_card(
    meeting_no: str,
    participant_departments_line: str,
    priority: str = "P0",
    emergency_topic: str = "",
) -> Dict[str, Any]:
    """
    ``participant_departments_line`` = comma-separated unique departments from SUPPORT sheet
    (same logic as ``participants.departments_line_from_names``), e.g. ``FPMS, FE``.
    Rendered in ``plain_text`` so Lark does not treat it as markdown (unlike the body above).
    """
    prio = (priority or "P0").strip().upper()
    dept_line = (participant_departments_line or "").strip() or "No participant info yet"
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"📽 Ongoing {prio} video meeting"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{_build_emergency_title(prio, emergency_topic)}**\n\n"
                            f"**Meeting ID:** {meeting_no}\n\n"
                            "Meeting is already 10 minutes ongoing."
                        ),
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": f"Participants\n{dept_line}",
                    },
                },
            ],
        },
    }


def _p0_ongoing_root_cause_guidance(contact_names: str) -> str:
    """Formal escalation guidance for the ongoing P0 DM buzz."""
    contacts = (contact_names or "").strip() or "Greg, Eason and Rock"
    return (
        "Escalation guidance:\n"
        f"• If the root cause has not yet been identified, please contact {contacts}.\n"
        f"• If the root cause has already been identified, please confirm with the incident "
        f"commander whether {contacts} should be contacted."
    )


def build_p0_ongoing_dm_buzz_card(
    *,
    source_chat_label: str = "",
    meeting_no: str = "",
    duration_text: str = "10 minutes",
    contact_names: str = "Greg, Eason and Rock",
    severity_tier: str = "minor",
) -> Dict[str, Any]:
    """DM reminder when a P0 VC is still active — **Major** at 5 min, **Minor** at 10 min."""
    label = (source_chat_label or "").strip() or "incident group"
    mno = (meeting_no or "").strip() or "Not available"
    contacts = (contact_names or "").strip() or "Greg, Eason and Rock"
    tier = (severity_tier or "minor").strip().lower()
    if tier == "major":
        dur = (duration_text or "").strip() or "5 minutes"
        body_line = (
            "The meeting has been active for 5 minutes. "
            "If this is a Major issue, please review escalation below."
        )
        title = f"⏱ P0 meeting — {dur} ongoing (Major)"
    else:
        dur = (duration_text or "").strip() or "10 minutes"
        body_line = (
            "The meeting has been active for 10 minutes. "
            "If this is a Minor issue, please review escalation below."
        )
        title = f"⏱ P0 meeting — {dur} ongoing (Minor)"
    guidance = _p0_ongoing_root_cause_guidance(contacts)
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            f"P0 video meeting for {label} is still active.\n\n"
                            f"{body_line}\n\n"
                            f"{guidance}\n\n"
                            f"Meeting ID: {mno}"
                        ),
                    },
                },
            ]
        },
    }


def build_p1_fifteen_min_confirm_card(meeting_no: str) -> Dict[str, Any]:
    """After 15 min on P1: declare as P0, or **Still P1** (continue without escalating)."""
    m = (meeting_no or "").strip() or "Not available"
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "⏱ P1 15 mins meeting"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**Meeting ID:** {m}\n\n"
                            "Please confirm if the meeting will be declared as **P0**, "
                            "or tap **Still P1** if the incident stays **P1**."
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Declare as P0"},
                                    "type": "primary",
                                    "value": {"action": "p1_declare_p0_yes"},
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Still P1"},
                                    "type": "default",
                                    "value": {"action": "p1_declare_p0_no"},
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_p1_escalated_card(meeting_no: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "🚨 P1 escalated to P0"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**Meeting ID:** {meeting_no}\n\n"
                            "Since the issue is still not resolved within 15 minutes, it is now declared as **P0**."
                        ),
                    },
                }
            ],
        },
    }


def build_no_active_p0_session_card(mode: str = "end") -> Dict[str, Any]:
    """
    Shown when user types end/cancel but there is no session and no stored ended snapshot.
    Same grey style family as meeting-ended cards.
    """
    m = (mode or "end").strip().lower()
    if m == "cancel":
        title = "🛑 No active meeting"
        body = (
            "**There is no active P0/P1 session to cancel in this chat.**\n\n"
            "It may have already ended or been cancelled, or the bot was restarted."
        )
    else:
        title = "✅ No active meeting"
        body = (
            "**There is no active P0/P1 session to end in this chat.**\n\n"
            "The meeting may have already ended, or the bot was restarted since it started."
        )
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body},
                }
            ],
        },
    }


def build_ring_status_card(title: str, body_md: str, *, header_template: str = "blue") -> Dict[str, Any]:
    """
    Compact status card for duty-ring / SRE-game OUTPUT messages (previously plain text).

    ``title`` renders in the colored header; ``body_md`` is a single ``lark_md`` div so it
    renders mentions cleanly. NOTE: in ``lark_md`` a mention is ``<at id=ou_xxx></at>``
    (NOT the text-message ``<at user_id="...">`` form).
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": (title or "").strip()},
            "template": (header_template or "blue").strip() or "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": (body_md or "").strip()}},
        ],
    }


def build_meeting_ended_card(
    meeting_no: str,
    duration_text: str = "Not available",
    priority: str = "P0",
    emergency_topic: str = "",
    *,
    update_multi: bool = False,
) -> Dict[str, Any]:
    """Grey in-place update of the red invite card — no Join button, minimal body."""
    _ = emergency_topic
    prio = (priority or "P0").strip().upper()
    dur = (duration_text or "Not available").strip() or "Not available"
    meeting_line = f"Meeting ID: {meeting_no}" if meeting_no else "Meeting ended."
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"✅ {prio} meeting ended"}},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": meeting_line}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "plain_text", "content": f"Duration: {dur}"}},
            ],
        },
    }


def build_meeting_cancelled_card(
    meeting_no: str,
    duration_text: str = "Not available",
    priority: str = "P0",
    reason: str = "Unspecified",
    emergency_topic: str = "",
    *,
    update_multi: bool = False,
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    cancel_reason = (reason or "Unspecified").strip() or "Unspecified"
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"🛑 {prio} meeting cancelled"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{_build_emergency_title(prio, emergency_topic)}**\n\n"
                            f"**Meeting ID:** {meeting_no or 'Not available'}\n"
                            f"**Duration Before Cancel:** {duration_text}\n"
                            f"**Reason:** {cancel_reason}"
                        ),
                    },
                }
            ],
        },
    }


def build_p1_meeting_confirm_card(confirm_nonce: str, source_chat_id: str = "") -> Dict[str, Any]:
    """Shown when someone says P1 — **Create meeting** or **Don't need** (typed ``create meeting`` still works)."""
    nonce = (confirm_nonce or "").strip()
    src = (source_chat_id or "").strip()
    val_yes: Dict[str, Any] = {"action": "p1_confirm_meeting_yes"}
    val_no: Dict[str, Any] = {"action": "p1_confirm_meeting_no"}
    if nonce:
        val_yes["p1_nonce"] = nonce
        val_no["p1_nonce"] = nonce
    if src:
        # Carried so a click from a DM (P0_P1_CONFIRM_DM) still resolves the source incident group.
        val_yes["source_chat_id"] = src
        val_no["source_chat_id"] = src
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "⚠️ P1 mentioned"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "P1 was mentioned in this chat. Do you want to create a Lark video meeting now?",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Create meeting"},
                                    "type": "primary",
                                    "value": val_yes,
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Don't need"},
                                    "type": "default",
                                    "value": val_no,
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_p0_keyword_confirm_dm_card(
    nonce: str,
    phrase: str,
    source_chat_name: str = "",
) -> Dict[str, Any]:
    """
    DM'd to duty when a group ``p0`` mention was NOT auto-declared (AI/Groq said not a fresh
    declaration). Yes → start a P0 meeting in the original incident group; No → dismiss.
    ``update_multi: true`` so the handler can PATCH this card in place after a click.
    """
    nonce = (nonce or "").strip()
    grp = (source_chat_name or "").strip() or "an incident group"
    quoted = (phrase or "").strip()
    if len(quoted) > 300:
        quoted = quoted[:300] + "…"
    val_yes: Dict[str, Any] = {"action": "p0_keyword_confirm_yes"}
    val_no: Dict[str, Any] = {"action": "p0_keyword_confirm_no"}
    if nonce:
        val_yes["kw_confirm_nonce"] = nonce
        val_no["kw_confirm_nonce"] = nonce
    body_lines = [
        "**P0 is being mentioned** in **{}**.".format(grp),
    ]
    if quoted:
        body_lines.append("> {}".format(quoted.replace("\n", " ")))
    body_lines.append("The bot did not auto-declare this. Create a P0 meeting?")
    return {
        "schema": "2.0",
        "config": {"enable_forward": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "⚠️ P0 mentioned — create meeting?"},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body_lines)}},
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Yes, create meeting"},
                                    "type": "primary",
                                    "value": val_yes,
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "No, dismiss"},
                                    "type": "default",
                                    "value": val_no,
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_p0_keyword_confirm_result_card(
    text: str,
    template: str = "grey",
    title: str = "P0 mention confirmation",
) -> Dict[str, Any]:
    """Grey (or coloured) outcome card that PATCHes the Yes/No confirm DM after a click."""
    return {
        "schema": "2.0",
        "config": {"enable_forward": True, "update_multi": True},
        "header": {
            "template": (template or "grey").strip() or "grey",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": (text or "").strip()}},
            ]
        },
    }


def build_p0_keyword_confirm_dismissed_card(nonce: str) -> Dict[str, Any]:
    """
    Dismissed outcome that STILL offers a "create the meeting after all" button — the duty can
    change their mind. The nonce stays alive (not consumed on dismiss) so Yes here still works.
    """
    nonce = (nonce or "").strip()
    val_yes: Dict[str, Any] = {"action": "p0_keyword_confirm_yes"}
    if nonce:
        val_yes["kw_confirm_nonce"] = nonce
    return {
        "schema": "2.0",
        "config": {"enable_forward": True, "update_multi": True},
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": "P0 mention confirmation"},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "✅ Dismissed — no meeting created."}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "Changed your mind? You can still create it:"}},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚨 Create the P0 meeting"},
                    "type": "primary",
                    "value": val_yes,
                },
            ]
        },
    }


def build_p0_keyword_confirm_created_card(source_chat_id: str) -> Dict[str, Any]:
    """
    Shown after Yes creates the meeting. Keeps a "cancel this meeting" button so an accidental
    create can be undone: ends the VC, removes the invite link card, and stops the session
    (no further interval screenshots / overview). Carries the source chat_id so cancel can act.
    """
    src = (source_chat_id or "").strip()
    val_cancel: Dict[str, Any] = {"action": "p0_keyword_confirm_cancel"}
    if src:
        val_cancel["cancel_chat_id"] = src
    return {
        "schema": "2.0",
        "config": {"enable_forward": True, "update_multi": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "P0 mention confirmation"},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "✅ P0 meeting created."}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "Created by accident? Cancel it (ends the meeting, removes the link, stops the session):"}},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🗑 Cancel this meeting"},
                    "type": "danger",
                    "value": val_cancel,
                },
            ]
        },
    }


def _dm_scope_button_fields(
    *,
    target_chat: str = "",
    source_incident_chat_id: str = "",
    draft_priority: str = "",
) -> Dict[str, Any]:
    """Embed in interactive ``value`` so any app instance can recover ``oc_`` (multi-replica)."""
    out: Dict[str, Any] = {}
    tc = (target_chat or "").strip()
    if tc.startswith("oc_"):
        out["target_chat"] = tc
    src = (source_incident_chat_id or "").strip()
    if src:
        out["source_incident_chat_id"] = src
    dp = (draft_priority or "").strip().upper()
    if dp in ("P0", "P1"):
        out["draft_priority"] = dp
    return out


def _dm_button_value(action: str, **scope: Any) -> Dict[str, Any]:
    v: Dict[str, Any] = {"action": action}
    v.update(
        _dm_scope_button_fields(
            target_chat=str(scope.get("target_chat") or ""),
            source_incident_chat_id=str(scope.get("source_incident_chat_id") or ""),
            draft_priority=str(scope.get("draft_priority") or ""),
        )
    )
    gcid = str(scope.get("group_chat_id") or "").strip()
    gmid = str(scope.get("group_message_id") or "").strip()
    if gcid.startswith("oc_"):
        v["group_chat_id"] = gcid
    if gmid:
        v["group_message_id"] = gmid
    alert_key = str(scope.get("issue_watch_alert_key") or "").strip()
    if alert_key:
        v["issue_watch_alert_key"] = alert_key
    src_mid = str(scope.get("issue_watch_source_message_id") or "").strip()
    if src_mid:
        v["issue_watch_source_message_id"] = src_mid
    return v


def _group_overview_button_value(
    action: str,
    *,
    group_chat_id: str,
    group_message_id: str,
    target_chat: str = "",
    source_incident_chat_id: str = "",
    draft_priority: str = "P0",
) -> Dict[str, Any]:
    return _dm_button_value(
        action,
        group_chat_id=group_chat_id,
        group_message_id=group_message_id,
        target_chat=target_chat,
        source_incident_chat_id=source_incident_chat_id,
        draft_priority=draft_priority,
    )


def _help_commands_md() -> str:
    """Command reference for DM Help button and typed ``help``."""
    return (
        "commands help for overview automation\n\n"
        "Typed in DM or incident group\n"
        '• type "h" or "help" — show this card\n'
        '• type "commands" — same as help\n\n'
        "Manually create overview\n"
        '• type "coe" — standalone overview, emergency group (no meeting)\n'
        '• type "cog" — standalone overview, game group (no meeting)\n'
        '• type "c" — abort coe/cog on the green card; with a preview open, use **Cancel** on the preview card\n\n'
        "Tap — green instruction card (DM) / 私聊绿色卡片按钮\n"
        "• Build overview — generate preview from draft\n"
        "• Clear draft — reset pasted text/images\n"
        "• Participants — list meeting attendees\n"
        "• Help — show this card\n\n"
        "Tap — preview card (DM) / 预览卡片按钮\n"
        "• Send to group — post overview to incident group\n"
        "• Generate — refresh Issue from draft\n"
        "• Edit — open Issue / Impact / Support form\n"
        "• Cancel — discard preview and restart\n\n"
        "Tap — edit card (DM) / 编辑卡片按钮\n"
        "• Save — apply edits to preview\n"
        "• Back — close edit form, return to preview\n\n"
        "Tap — overview sent card (DM) / 已发送概览卡片按钮\n"
        "• Edit overview — change the group overview after Send to group (form opens in DM)\n"
        "• Save — update the posted group message (overview bot copy too, when enabled)\n"
        "• Back — close edit form, keep the sent card\n"
        "• Done — dismiss the sent card in DM\n\n"
        "Tap — group P1 cards / 群内 P1 卡片按钮\n"
        "• Create meeting / Don't need — answer P1 create meeting?\n"
        "• Declare as P0 / Still P1 — answer P1 15-min escalation card"
    )


def build_help_commands_card() -> Dict[str, Any]:
    """Reference card listing P0/P1 bot commands (DM button or typed ``help``)."""
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Incident Bot — Commands"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _help_commands_md()},
                },
            ]
        },
    }


def _ring_commands_guide_md() -> str:
    """VC ring-command cheat-sheet (DM guide, shown before the overview card). Rendered by the card's
    markdown component; section titles are **bold** (not # headings) so the font matches the overview
    card — Lark renders markdown headings much larger than body text."""
    return """**Call Commands**

Type these commands in the incident group **while the P0 meeting is running** to call people into the meeting. The bot will automatically contact the **current on-duty** members of the respective teams.

**Developer Duty**

* `/fe` — Frontend
* `/fpms` — FPMS
* `/pms` — PMS
* `/cpms` — CPMS

**OM Duty**

* `/scpms`, `/sfpms`, `/sfe`, `/spms` — CPMS / FPMS / FE / PMS SRE
* `/dba` — DBA
* `/sosm` — LiveSlot SRE

**Direct Commands**

* `/c @Name` — Calls the tagged person(s).
* `/m` — Calls the Major P0 contact list **@Bk @Yang @Koo @YC @Wennie @Eden @Jun Meng @Jayden Liu**
* `/e` — No response → Escalate to @Wei Siong @Adrian Chong

---

**Additional Commands for Game urgent-游戏紧急群**

**SRE Game (rings the primary SRE contact)**

* `/srebac` — Baccarat
* `/srer` — Roulette
* `/sredt` — Dragon Tiger
* `/sresic` — Sic Bo
* `/srebl` — Blackjack
* `/srepai` — Pai Gow
* `/srecg` — Color Game
* `/srepp` — Pula Puti
* `/sredb` — Drop Ball
* `/sreib` — In Between
* `/sre <game>` — any game by its name (e.g. `/sre Baccarat`)

**PO Product Manager (rings the primary Product Manager)**

* `/pobac` — Baccarat
* `/pobt` — Baccarat Tournament
* `/por` — Roulette
* `/podt` — Dragon Tiger
* `/posic` — Sic Bo
* `/pobl` — Blackjack
* `/popai` — Pai Gow
* `/pocg` — Color Game
* `/popp` — Pula Puti
* `/podb` — Drop Ball
* `/poib` — In Between
* `/poht` — Hantak
* `/poosm` — OSM
* `/poegs` — EGS
* `/poev` — Evo Live Games
* `/poez` — EEZE Live Game
* `/pogm` — Marble Race: Las Vegas / Marble 5vs5: Monaco
* `/popt` — Playtech Live Game
* `/posb` — SportBet/Ebet
* `/pogz` — Tongits Plus/Texas Poker / Tongits Joker/Pusoy Plus/Lucky 9 Plus
* `/po <game>` — any other game by its exact name (e.g. `/po Baccarat (if all tables are maintenance)`)

**EGAME SRE**

* `/segame <game>` — For example: `/segame Bakunawa` (works with any EGAME title using its exact game name).

---

**Reply Thread**

Use `/c @Name` in the reply thread to call additional people from the contact list. Once a contact joins the meeting, the bot automatically confirms their attendance—no reply is required.

**Combining Commands**

You can combine multiple commands in a single message — just one leading slash. For example:

* `/cpms fpms sfpms fe`
* `/c @Name1 @Name2 @Name3`
* `/cpms fpms /c @Name1 @Name2`

**If a Command Doesn't Work**

If any command is not working, you can always use the `/c` command to call people directly. For example:

`/c @Renzel Hernandez @Name1 @Name2`"""


def build_ring_commands_guide_card() -> Dict[str, Any]:
    """DM cheat-sheet of the VC ring commands, posted just before the green Build-overview card."""
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "P0 Invite Commands — call and invite people into the meeting"},
        },
        "body": {
            "elements": [
                # schema-2.0 markdown component renders headings / lists / inline `code` (lark_md does not).
                {"tag": "markdown", "content": _ring_commands_guide_md()},
            ]
        },
    }


def dm_escalation_reminder_plain() -> str:
    """P0 SOP escalation reminder (plain text for DM lines)."""
    return (
        "Escalation category:\n\n"
        "Major Issues:\n"
        "Login, games/events entering, withdrawal, deposit problems.\n"
        "• Need to send the P0 overview to the WhatsApp group as well\n\n"
        "Minor Issues:\n"
        "All other issues, including cases where it's unclear whether the problem is on our side "
        "or limited to a specific provider (especially if only one provider is affected).\n"
        "• No need to send the P0 overview to the WhatsApp group\n\n"
        "Note: Every time you call, please provide them with a brief update on what is happening"
    )


def _dm_escalation_reminder_md() -> str:
    return (
        "**Escalation category:**\n\n"
        "**Major Issues:**\n"
        "Login, games/events entering, withdrawal, deposit problems.\n"
        "• Need to send the P0 overview to the WhatsApp group as well\n\n"
        "**Minor Issues:**\n"
        "All other issues, including cases where it's unclear whether the problem is on our side "
        "or limited to a specific provider (especially if only one provider is affected).\n"
        "• No need to send the P0 overview to the WhatsApp group\n\n"
        "**Note:** Every time you call, please provide them with a brief update on what is happening."
    )


def build_issue_watch_declare_overview_hint_text() -> str:
    """DM hint above the suggested overview preview card after Issue Watch declare."""
    return (
        "P0 has been declared - A suggested overview is being generated\n\n"
        "Select Send to group - to post it to the incident group.\n\n"
        "Select Cancel - to skip this auto-generated overview and proceed with the standard "
        "Build overview flow using your own text and screenshots."
    )


def build_issue_watch_declare_followup_text() -> str:
    """DM after declare: SOP reminder (meeting card is in the detection group)."""
    return (
        "P0 has been declared. Please follow the guidelines and SOP for handling a P0 incident.\n\n"
        + dm_escalation_reminder_plain()
    )


def build_dm_instruction_card(
    priority: str = "P0",
    source_chat_label: str = "",
    *,
    target_chat: str = "",
    source_incident_chat_id: str = "",
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    sc = dict(target_chat=target_chat, source_incident_chat_id=source_incident_chat_id, draft_priority=prio)
    title = f"🧾 Send {prio} incident details (DM){_title_group_suffix(source_chat_label)}"
    standalone = str(source_incident_chat_id or "").strip() == _session.STANDALONE_DM_SOURCE_CHAT_ID
    tips = (
        "Tap Build overview when ready. Clear draft resets input. Participants lists attendees. Help shows all commands."
    )
    if standalone:
        tips += ' Type "c" to abort and switch coe or cog.'
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": "You may send screenshots and pasted text in any order."}},
        {"tag": "div", "text": {"tag": "plain_text", "content": tips}},
    ]
    if prio == "P0":
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": _dm_escalation_reminder_md()}},
            ]
        )
    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Build overview"},
                                "type": "primary",
                                "value": _dm_button_value("generate_preview", **sc),
                            },
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Clear draft"},
                                "type": "default",
                                "value": _dm_button_value("clear_draft", **sc),
                            },
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Participants"},
                                "type": "default",
                                "value": _dm_button_value("show_participants", **sc),
                            },
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Help"},
                                "type": "default",
                                "value": _dm_button_value("show_help", **sc),
                            },
                        ],
                    },
                ],
            },
        ]
    )
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "green", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
    }


def build_adjustment_bitable_card(
    body_md: str,
    *,
    count: int,
    title: str = "Deployments",
    subtitle: str = "",
    window_label: str = "",
    hours: int = 0,
) -> Dict[str, Any]:
    """Orange card for recent Bitable rows (reply under P0 overview)."""
    _ = hours  # legacy callers may still pass hours; window is calendar-based
    n = max(0, int(count or 0))
    safe_md = (body_md or "").strip()[:12000]
    card_title = (title or "Deployments").strip() or "Deployments"
    sub = (subtitle or "").strip()
    if not sub:
        win = (window_label or f"yesterday 00:00 – end of today {_config.get_p0_adjustment_bitable_tz_label()}").strip()
        sub = f"{n} service(s) with Blue Green or Full Release ({win})"
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": sub}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": safe_md or "_No rows_"}},
    ]
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": card_title},
        },
        "body": {"elements": elements},
    }


def build_overview_result_card(
    md: str,
    priority: str = "P0",
    source_chat_label: str = "",
    *,
    group_chat_id: str = "",
    group_message_id: str = "",
    allow_group_edit: bool = False,
    target_chat: str = "",
    source_incident_chat_id: str = "",
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    safe_md = (md or "").strip()[:3500]
    cfg: Dict[str, Any] = {"enable_forward": True, "update_multi": True}
    elements: List[Dict[str, Any]] = [{"tag": "div", "text": {"tag": "lark_md", "content": safe_md}}]
    gcid = (group_chat_id or "").strip()
    gmid = (group_message_id or "").strip()
    if allow_group_edit and gcid.startswith("oc_") and gmid:
        elements.append({"tag": "hr"})
        sc = dict(
            target_chat=target_chat,
            source_incident_chat_id=source_incident_chat_id,
            draft_priority=prio,
        )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Edit overview"},
                                "type": "default",
                                "value": _group_overview_button_value(
                                    "edit_group_overview",
                                    group_chat_id=gcid,
                                    group_message_id=gmid,
                                    **sc,
                                ),
                            },
                        ],
                    },
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"📝 {prio} Overview{_title_group_suffix(source_chat_label)}"},
        },
        "body": {"elements": elements},
    }


def build_dm_overview_sent_card(
    priority: str = "P0",
    source_chat_label: str = "",
    *,
    group_chat_id: str = "",
    group_message_id: str = "",
    target_chat: str = "",
    source_incident_chat_id: str = "",
    forwarder_warning: str = "",
    group_updated: bool = False,
) -> Dict[str, Any]:
    """
    DM card after Send to group — Edit overview opens the form in DM; Done dismisses this card.
    """
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    sc = dict(
        target_chat=target_chat,
        source_incident_chat_id=source_incident_chat_id,
        draft_priority=prio,
    )
    label = (source_chat_label or "").strip() or "target group"
    body_text = f"Posted to {label}."
    if group_updated:
        body_text = f"{body_text} Updated just now."
    if (forwarder_warning or "").strip():
        body_text = f"{forwarder_warning.strip()}\n{body_text}"
    cfg: Dict[str, Any] = {"enable_forward": True, "update_multi": True}
    gcid = (group_chat_id or "").strip()
    gmid = (group_message_id or "").strip()
    buttons: List[Dict[str, Any]] = []
    if gcid.startswith("oc_") and gmid:
        buttons.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Edit overview"},
                        "type": "primary",
                        "value": _group_overview_button_value(
                            "edit_group_overview",
                            group_chat_id=gcid,
                            group_message_id=gmid,
                            **sc,
                        ),
                    },
                ],
            }
        )
    buttons.append(
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Done"},
                    "type": "default",
                    "value": _dm_button_value("dismiss_sent_overview", **sc),
                },
            ],
        }
    )
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": f"📤 {prio} Overview sent{_title_group_suffix(source_chat_label)}",
            },
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": body_text}},
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": buttons,
                },
            ]
        },
    }


def build_preview_card(
    md: str,
    priority: str = "P0",
    source_chat_label: str = "",
    *,
    update_multi: bool = True,
    target_chat: str = "",
    source_incident_chat_id: str = "",
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    sc = dict(target_chat=target_chat, source_incident_chat_id=source_incident_chat_id, draft_priority=prio)
    safe_md = (md or "").strip()[:3500]
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    # Incident time lives inside ``md`` (bilingual overview); no extra line above the body.
    preview_elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": safe_md}},
        {"tag": "hr"},
    ]
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"📝 {prio} Overview Preview{_title_group_suffix(source_chat_label)}"}},
        "body": {
            "elements": preview_elements
                + [
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Send to group"},
                                    "type": "primary",
                                    "value": _dm_button_value("send_preview", **sc),
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Generate"},
                                    "type": "default",
                                    "value": _dm_button_value("generate_again", **sc),
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Edit"},
                                    "type": "default",
                                    "value": _dm_button_value("edit_preview", **sc),
                                },
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Cancel"},
                                    "type": "danger",
                                    "value": _dm_button_value("cancel_preview", **sc),
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_edit_overview_card(
    current_issue: str = "",
    current_impact: str = "",
    current_support: str = "",
    priority: str = "P0",
    source_chat_label: str = "",
    *,
    update_multi: bool = True,
    start_epoch: int = 0,
    editing_group_overview: bool = False,
) -> Dict[str, Any]:
    """Single card to edit Issue, Impact Scope, and Support Request."""
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    # Prefill each field with generated text; leave a field empty only when that value is the
    # sentinel (e.g. "Not specified") so operators see a blank box just for that section.
    default_issue = "" if _text.is_not_specified(current_issue) else (current_issue or "").strip()
    default_impact = "" if _text.is_not_specified(current_impact) else (current_impact or "").strip()
    default_support = "" if _text.is_not_specified(current_support) else (current_support or "").strip()
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    if editing_group_overview:
        intro = "Tap Save to update the group overview. Back closes this form."
    else:
        intro = (
            "Only Incident start uses the calendar/date-time picker. "
            "Issue, Impact, and Support are plain text. Tap Save when done."
        )
    form_elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": intro}},
    ]
    # Calendar picker applies to incident start only; issue/impact/support stay ``input`` fields below.
    form_elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "🕒 Incident start (PHT) — calendar & time",
            },
        }
    )
    form_elements.append(
        {
            "tag": "picker_datetime",
            "element_id": "inc_start_pick",
            "name": "incident_start_datetime",
            "required": False,
            "width": "fill",
            "initial_datetime": initial_datetime_for_picker(start_epoch),
            "placeholder": {"tag": "plain_text", "content": "Select date & time"},
            "behaviors": [{"type": "callback", "value": {"scope": "incident_start"}}],
        }
    )
    form_elements.extend(
        [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "🔥 Issue (short summary)"},
            },
            {
                "tag": "input",
                "name": "issue_input",
                "placeholder": {"tag": "plain_text", "content": "What is wrong?"},
                "value": default_issue,
            },
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "🎯 Impact scope (e.g. 2 players, EU shard)"},
            },
            {
                "tag": "input",
                "name": "impact_input",
                "placeholder": {"tag": "plain_text", "content": "e.g. 2 players"},
                "value": default_impact,
            },
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "👥 Support request (e.g. FPMS, FE, CPMS)"},
            },
            {
                "tag": "input",
                "name": "support_input",
                "placeholder": {"tag": "plain_text", "content": "e.g. FPMS"},
                "value": default_support,
            },
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "button",
                                "name": "save_edit_btn",
                                "text": {"tag": "plain_text", "content": "Save"},
                                "type": "primary",
                                "action_type": "form_submit",
                                "value": {"action": "save_edit"},
                            },
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Back"},
                                "type": "default",
                                "value": {
                                    "action": "back_group_edit"
                                    if editing_group_overview
                                    else "back_to_preview"
                                },
                            },
                        ],
                    },
                ],
            },
        ]
    )
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"✏️ Edit {prio} Overview{_title_group_suffix(source_chat_label)}"},
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "overview_edit_form",
                    "elements": form_elements,
                },
            ]
        },
    }


def build_bilingual_overview_md(
    start_epoch: int,
    issue: str,
    impact: str,
    support: str,
    priority: str = "P0",
    *,
    zh_issue_precomputed: Optional[str] = None,
    zh_impact_precomputed: Optional[str] = None,
) -> str:
    """EN + 中文 overview. Prefer zh_* from one-shot Groq in draft build; else two parallel translate calls."""
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    start_time = datetime.fromtimestamp(start_epoch, tz=PHT).strftime("%Y-%m-%d %H:%M")
    na_en = "Not specified"
    en_issue = (issue or "").strip() or na_en
    en_impact = (impact or "").strip() or na_en
    en_support = (support or "").strip() or na_en

    if zh_issue_precomputed is not None and zh_impact_precomputed is not None:
        zh_issue = _text.normalize_gaming_zh(_text.clean_single_line_translation(zh_issue_precomputed)) or "未指定"
        zh_impact = _text.normalize_gaming_zh(_text.clean_single_line_translation(zh_impact_precomputed)) or "未指定"
    else:
        # Edit/regenerate path: no precomputed zh — translate via the overview provider chain
        # (Claude → Groq), same as the one-shot generation, so edits stay all-Claude when configured.
        from features.overview import overview_ai as _overview_ai

        zh_issue, zh_impact = _overview_ai.translate_issue_impact_pair(en_issue, en_impact)
        zh_issue = _text.normalize_gaming_zh(_text.clean_single_line_translation(zh_issue)) or "未指定"
        zh_impact = _text.normalize_gaming_zh(_text.clean_single_line_translation(zh_impact)) or "未指定"
    # Dept / team codes (FPMS, CPMS, OSE, …) must not be “translated” in 中文.
    zh_support = "未指定" if en_support == na_en else en_support

    # Lark lark_md: one \n between lines (tight). Extra \n\n ONLY before the Chinese
    # header so the last EN line (e.g. "…FPMS") does not glue to the Chinese title.
    # Emoji prefixes here are for the *overview text* only (readable in chat), not card buttons.
    return (
        f"**{prio} Incident Overview**\n"
        f"🕒 **Time**: {start_time} - Incident Start\n"
        f"🔥 **Issue**: {en_issue}\n"
        f"🎯 **Impact scope**: {en_impact}\n"
        f"👥 **Support request**: {en_support}\n\n"
        f"**{prio} 事故概览**\n"
        f"🕒 **时间**: {start_time} (事故开始)\n"
        f"🔥 **问题**: {zh_issue}\n"
        f"🎯 **影响范围**: {zh_impact}\n"
        f"👥 **支援请求**: {zh_support}"
    )


def build_issue_watch_alert_card(
    *,
    group_label: str,
    categories_md: str,
    summary: str,
    concern: str,
    alert_time: str,
    player_ids_md: str = "",
    source_message_link: str = "",
    source_message_time: str = "",
    supplemental_player_ids: bool = False,
    issue_watch_alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
    issue_watch_source_message_id: str = "",
    declare_p0_buttons: bool = False,
    auto_overview_buttons: bool = False,
) -> Dict[str, Any]:
    """DM card when Claude/keyword detects a player-facing issue in a detection group."""
    title_group = (group_label or "").strip() or "detection group"
    if len(title_group) > 40:
        title_group = title_group[:39] + "…"
    header_title = (
        f"🚨 Player IDs — {title_group}"
        if supplemental_player_ids
        else f"🚨 Major P0 Detection alert — {title_group}"
    )
    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**Category:**\n{(categories_md or '• (unspecified)').strip()}",
            },
        },
    ]
    if (summary or "").strip():
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**Summary:** {(summary or '').strip()}"},
            }
        )
    if (player_ids_md or "").strip():
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Player IDs:**\n{(player_ids_md or '').strip()}",
                },
            }
        )
    if not supplemental_player_ids:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": f"Concern: 「{(concern or '').strip()}」",
                },
            }
        )
    elif (concern or "").strip():
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Related report:** {(concern or '').strip()}",
                },
            }
        )
    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**Time:** {(alert_time or '').strip()}"},
        }
    )
    src_time = (source_message_time or "").strip()
    msg_link = (source_message_link or "").strip()
    if src_time or msg_link:
        if msg_link and src_time:
            source_line = f"**Source message:** [{src_time}]({msg_link})"
        elif msg_link:
            source_line = "**Source message:** [Open in detection group]({url})".format(url=msg_link)
        else:
            source_line = f"**Source message time:** {src_time}"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": source_line}})
    if msg_link:
        elements.append(
            _button_open_url(
                content="Open source message",
                url=msg_link,
                button_type="primary",
                element_id="open_src_msg",
            )
        )
    elements.append({"tag": "hr"})
    if declare_p0_buttons and (source_incident_chat_id or "").strip():
        sc = dict(
            target_chat=(target_chat or "").strip(),
            source_incident_chat_id=(source_incident_chat_id or "").strip(),
            draft_priority="P0",
            issue_watch_alert_key=(issue_watch_alert_key or "").strip(),
            issue_watch_source_message_id=(issue_watch_source_message_id or "").strip(),
        )
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "This may be a major P0. Declare from here to reply on the concern in the "
                        "detection group, react on that message, start the P0 meeting, and auto-generate "
                        "the overview preview in DM."
                    ),
                },
            }
        )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Declare as P0"},
                                "type": "danger",
                                "value": _dm_button_value("issue_watch_declare_p0", **sc),
                            },
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Not now"},
                                "type": "default",
                                "value": _dm_button_value("issue_watch_declare_p0_dismiss", **sc),
                            },
                        ],
                    },
                ],
            }
        )
    else:
        footer = (
            "*Issue might be P0 — declare in the group if a bridge meeting is needed. "
            "After declare, duty gets a suggested overview in DM.*"
        )
        if auto_overview_buttons:
            footer = (
                "*After you declare P0 in the detection group, duty gets a suggested overview in DM.*"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": footer}})
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "red",
            "title": {
                "tag": "plain_text",
                "content": header_title,
            },
        },
        "body": {"elements": elements},
    }


def build_issue_watch_declare_manual_card(
    *,
    issue_watch_alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
    source_chat_label: str = "",
) -> Dict[str, Any]:
    """DM card after P0 declare — duty can discard suggested preview and build manually."""
    sc = dict(
        target_chat=(target_chat or "").strip(),
        source_incident_chat_id=(source_incident_chat_id or "").strip(),
        draft_priority="P0",
        issue_watch_alert_key=(issue_watch_alert_key or "").strip(),
    )
    suffix = _title_group_suffix(source_chat_label)
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": f"📝 Build overview manually{suffix}"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "Prefer to write the overview yourself? This clears the suggested preview and opens the usual **Build overview** flow.",
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Build overview manually"},
                    "type": "primary",
                    "value": _dm_button_value("issue_watch_manual_overview", **sc),
                },
            ]
        },
    }


def build_monitoring_duty_card(
    text: str,
    *,
    duty_open_id: str = "",
    label: str = "duty warning",
) -> Dict[str, Any]:
    """Orange card when a duty DM warning is mirrored to the monitoring group."""
    kind = (label or "duty warning").strip()
    title = f"🔔 Ops monitor · {kind}"
    oid = (duty_open_id or "").strip()
    who = f"To: {oid[-12:]}" if oid else "To: duty DM"
    body = (text or "").strip()
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": who[:500]}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "plain_text", "content": body[:12000] or "(empty)"}},
    ]
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title[:100]},
        },
        "body": {"elements": elements},
    }


def build_monitoring_log_card(
    message: str,
    *,
    level: str = "ERROR",
    logger_name: str = "",
) -> Dict[str, Any]:
    """Red card when ERROR+ logs are forwarded to the monitoring group."""
    lvl = (level or "ERROR").upper()
    name = (logger_name or "logger").strip()
    title = f"🔴 Ops monitor · log {lvl}"
    body = (message or "").strip()
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": name[:500]}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "plain_text", "content": body[:12000] or "(empty)"}},
    ]
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": title[:100]},
        },
        "body": {"elements": elements},
    }
