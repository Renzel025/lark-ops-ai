"""
Lark interactive card builders and P0 overview markdown (English + translated 中文 body).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config as _config
from . import groq_client as _groq
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


def build_meeting_card(
    link: str,
    meeting_no: str = "",
    priority: str = "P0",
    affected_players: str = "",
    emergency_topic: str = "",
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    title_text = _build_emergency_title(prio, emergency_topic)
    footer_text = _build_meeting_invite_footer(prio)
    meeting_line = f"Meeting ID: {meeting_no}" if meeting_no else "Meeting has been created."
    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": meeting_line}},
        {"tag": "hr"},
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "Join meeting"},
            "type": "primary",
            "multi_url": {"url": link, "pc_url": link},
        },
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "plain_text", "content": footer_text}},
    ]
    return {
        "schema": "2.0",
        # Required so we can PATCH this message when the meeting ends (remove Join button).
        "config": {"enable_forward": True, "update_multi": True},
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


def build_meeting_ended_card(
    meeting_no: str,
    duration_text: str = "Not available",
    priority: str = "P0",
    emergency_topic: str = "",
    *,
    update_multi: bool = False,
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    em_line = _build_emergency_title(prio, emergency_topic)
    # Title carries ✅ line; body starts at 🚨 line (P0 / P1 both use ``prio``).
    body_md = (
        f"{em_line}\n\n"
        f"Meeting ID: {meeting_no}\n"
        f"Duration: {duration_text}\n\n"
        "The emergency meeting has concluded."
    )
    cfg: Dict[str, Any] = {"enable_forward": True}
    if update_multi:
        cfg["update_multi"] = True
    return {
        "schema": "2.0",
        "config": cfg,
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"✅ {prio} meeting ended"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": body_md,
                    },
                }
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


def build_p1_meeting_confirm_card(confirm_nonce: str) -> Dict[str, Any]:
    """Shown when someone says P1 — **Create meeting** or **Don't need** (typed ``create meeting`` still works)."""
    nonce = (confirm_nonce or "").strip()
    val_yes: Dict[str, Any] = {"action": "p1_confirm_meeting_yes"}
    val_no: Dict[str, Any] = {"action": "p1_confirm_meeting_no"}
    if nonce:
        val_yes["p1_nonce"] = nonce
        val_no["p1_nonce"] = nonce
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


def build_slack_severity_prompt_card(
    *,
    source_incident_chat_id: str,
    target_chat: str,
    group_label: str,
    priority: str = "P0",
) -> Dict[str, Any]:
    """
    DM card: Major vs Minor before Slack notify + huddle. ``source_incident_chat_id`` must be ``oc_...``.
    """
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    label = (group_label or "").strip() or "incident group"
    sc = dict(
        target_chat=str(target_chat or ""),
        source_incident_chat_id=str(source_incident_chat_id or ""),
        draft_priority=prio,
    )
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"⚠️ {prio} declared — severity"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{prio}** is being declared in **{label}**.\n\n"
                            "Please clarify if the issue is **major** or **minor**:\n"
                            "• **Major** — will notify all OM members on Slack channel.\n"
                            "• **Minor** — will notify specific duty SRE to check the issue."
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
                                    "text": {"tag": "plain_text", "content": "Major"},
                                    "type": "primary",
                                    "value": _dm_button_value("slack_severity_major", **sc),
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
                                    "text": {"tag": "plain_text", "content": "Minor"},
                                    "type": "default",
                                    "value": _dm_button_value("slack_severity_minor", **sc),
                                },
                            ],
                        },
                    ],
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
    return v


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
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "green", "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "You may send screenshots and pasted text in any order."}},
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "Tap Build overview when ready. Clear draft resets input. Tap Participants to list meeting attendees by name.",
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
                    ],
                },
            ]
        },
    }


def build_overview_result_card(md: str, priority: str = "P0", source_chat_label: str = "") -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"📝 {prio} Overview{_title_group_suffix(source_chat_label)}"}},
        "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}]},
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
    form_elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "Only Incident start uses the calendar/date-time picker. Issue, Impact, and Support are plain text fields. Tap Save when done.",
            },
        },
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
                                "value": {"action": "back_to_preview"},
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
        zh_issue, zh_impact = _groq.translate_issue_impact_pair_to_zh(en_issue, en_impact)
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
