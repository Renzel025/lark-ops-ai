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


def _build_emergency_title(priority: str, topic_line: str = "") -> str:
    prio = (priority or "P0").strip().upper()
    tail = (topic_line or "").strip() or MEETING_TOPIC
    return f"🚨 {prio} — {tail}"


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
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": title_text,
            },
        },
    ]
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content": title_text}},
        "body": {"elements": elements},
    }


def build_ongoing_meeting_card(
    meeting_no: str, participant_teams_text: str, priority: str = "P0", emergency_topic: str = ""
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
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
                            "Meeting is already 10 minutes ongoing.\n"
                            "Kindly ask them if need to contact Sir David and Sir Eason.\n\n"
                            f"**Participants:**\n\n{participant_teams_text}"
                        ),
                    },
                }
            ],
        },
    }


def build_p1_fifteen_min_confirm_card(meeting_no: str) -> Dict[str, Any]:
    """After 15 min on P1: ask whether to declare as P0 (Yes) or close as P1 (No)."""
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
                            "Please confirm if the meeting will be declared as **P0**."
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
                                    "text": {"tag": "plain_text", "content": "Close as P1"},
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


def build_meeting_ended_card(
    meeting_no: str,
    duration_text: str = "Not available",
    priority: str = "P0",
    emergency_topic: str = "",
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "green", "title": {"tag": "plain_text", "content": f"✅ {prio} meeting ended"}},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{_build_emergency_title(prio, emergency_topic)}**\n\n"
                            f"**Meeting ID:** {meeting_no}\n"
                            f"**Duration:** {duration_text}\n\n"
                            "The emergency meeting has concluded."
                        ),
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
) -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    cancel_reason = (reason or "Unspecified").strip() or "Unspecified"
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
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
    """Shown in the incident group when someone says P1 — create VC meeting or not."""
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
                        "tag": "plain_text",
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


def build_dm_instruction_card(priority: str = "P0", source_chat_label: str = "") -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    title = f"🧾 Send {prio} incident details (DM){_title_group_suffix(source_chat_label)}"
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "green", "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "You may send screenshots and pasted text in any order."}},
                {"tag": "div", "text": {"tag": "plain_text", "content": "Tap Build overview when ready. Clear draft resets input. Tap Participants to list meeting attendees by name."}},
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
                                    "value": {"action": "generate_preview"},
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
                                    "value": {"action": "clear_draft"},
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
                                    "value": {"action": "show_participants"},
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


def build_preview_card(md: str, priority: str = "P0", source_chat_label: str = "") -> Dict[str, Any]:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    safe_md = (md or "").strip()[:3500]
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"📝 {prio} Overview Preview{_title_group_suffix(source_chat_label)}"}},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": safe_md}},
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
                                    "text": {"tag": "plain_text", "content": "Send to group"},
                                    "type": "primary",
                                    "value": {"action": "send_preview"},
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
                                    "value": {"action": "generate_again"},
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
                                    "value": {"action": "edit_preview"},
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
                                    "value": {"action": "cancel_preview"},
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
) -> Dict[str, Any]:
    """Single card to edit Issue, Impact Scope, and Support Request."""
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    default_issue = "" if _text.is_not_specified(current_issue) else (current_issue or "")
    default_impact = "" if _text.is_not_specified(current_impact) else (current_impact or "")
    default_support = "" if _text.is_not_specified(current_support) else (current_support or "")
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"✏️ Edit {prio} Overview{_title_group_suffix(source_chat_label)}"},
        },
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "overview_edit_form",
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": "Update Issue, Impact Scope, and Support Request below, then tap Save.",
                            },
                        },
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
                    ],
                },
            ]
        },
    }


def build_bilingual_overview_md(
    start_epoch: int, issue: str, impact: str, support: str, priority: str = "P0"
) -> str:
    """Full EN overview plus 中文 block (Groq `translate_to_zh` for issue / impact only; support stays as-is)."""
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    start_time = datetime.fromtimestamp(start_epoch, tz=PHT).strftime("%Y-%m-%d %H:%M")
    na_en = "Not specified"
    en_issue = (issue or "").strip() or na_en
    en_impact = (impact or "").strip() or na_en
    en_support = (support or "").strip() or na_en

    zh_issue = _groq.translate_to_zh(en_issue)
    zh_impact = _groq.translate_to_zh(en_impact)
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
