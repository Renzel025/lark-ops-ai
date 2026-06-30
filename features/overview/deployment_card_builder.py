"""Build deployment / ops Lark cards — boss card1_ops_builder + card2_deploy_builder (v2)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_EM = "—"
_DEPLOY_PAGE_SIZE = 8


def _dash(val: str) -> str:
    s = (val or "").strip()
    return s if s else _EM


def _wrap_schema_v2(header: Dict[str, Any], elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": header,
        "body": {"elements": elements},
    }


def _card_footer_note(text: str) -> Dict[str, Any]:
    """Schema 2.0: ``note`` rejected by IM API — use notation ``div`` (Lark v2 replacement)."""
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": (text or "").strip(),
            "text_size": "notation",
            "text_color": "grey",
        },
    }


@dataclass
class OpsCardRow:
    exec_time: str = ""
    done_time: str = ""
    action: str = ""
    project: str = ""
    operator: str = ""
    reason: str = ""
    sort_ms: int = 0


@dataclass
class DeployCardRow:
    bg_time: str = ""
    full_time: str = ""
    service: str = ""
    version: str = ""
    project: str = ""
    pm: str = ""
    image_tag: str = ""
    email: str = ""
    changelog: str = ""
    sort_ms: int = 0


def _ops_entry_elements(row: OpsCardRow) -> List[Dict[str, Any]]:
    """
    Compact ops block — one ``markdown`` + ``hr`` per row.

    The old 3× ``column_set`` layout hit Lark card schema limit 11310 (~200 elements)
    at 8 rows/page; deploy cards use fewer nested elements and still paginate fine.
    """
    exec_t = _dash(row.exec_time)
    done_raw = (row.done_time or "").strip()
    done_t = _dash(row.done_time)
    done_color = "blue" if done_raw and done_raw != _EM else "red"
    content = (
        f"**{_dash(row.action)}**\n"
        f"<font color='grey'>执行时间</font> <font color='blue'>{exec_t}</font>"
        f" · <font color='grey'>完毕时间</font> <font color='{done_color}'>{done_t}</font>\n"
        f"<font color='grey'>项目</font> {_dash(row.project)}"
        f" · <font color='grey'>操作人员</font> <font color='purple'>{_dash(row.operator)}</font>\n"
        f"<font color='grey'>原因</font> {_dash(row.reason)}"
    )
    return [
        {"tag": "markdown", "content": content},
        {"tag": "hr"},
    ]


def _deploy_entry_elements(row: DeployCardRow) -> List[Dict[str, Any]]:
    """Boss card2_deploy_builder DEP_BLOCK_TEMPLATE (blue times, plain_text collapsible header)."""
    bg = _dash(row.bg_time)
    full = _dash(row.full_time)
    return [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "vertical_align": "top",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"<font color='grey'>BG</font>\n<font color='blue'>{bg}</font>\n"
                                f"<font color='grey'>Full</font>\n<font color='blue'>{full}</font>"
                            ),
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 3,
                    "vertical_align": "top",
                    "elements": [
                        {"tag": "markdown", "content": f"**{_dash(row.service)}**"},
                        {
                            "tag": "column_set",
                            "flex_mode": "none",
                            "background_style": "default",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": f'<text_tag color="green">{_dash(row.version)}</text_tag>',
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": f'<text_tag color="neutral">{_dash(row.project)}</text_tag>',
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": f'<text_tag color="purple">{_dash(row.pm)}</text_tag>',
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "tag": "markdown",
                            "content": f"<font color='grey'>{_dash(row.image_tag)}</font>",
                        },
                        {
                            "tag": "markdown",
                            "content": f"<font color='grey'>{_dash(row.email)}</font>",
                        },
                        {
                            "tag": "collapsible_panel",
                            "expanded": False,
                            "header": {
                                "title": {"tag": "plain_text", "content": "更新内容"},
                            },
                            "elements": [
                                {"tag": "markdown", "content": _dash(row.changelog)},
                            ],
                        },
                    ],
                },
            ],
        },
        {"tag": "hr"},
    ]


def build_ops_summary_card(
    rows: List[OpsCardRow],
    *,
    window_start: str,
    window_end: str,
    total_in_window: int = 0,
) -> Dict[str, Any]:
    """Single-page ops card (legacy helper). Prefer ``build_ops_page_cards``."""
    cards = build_ops_page_cards(
        rows,
        window_start=window_start,
        window_end=window_end,
        total_in_window=total_in_window,
    )
    return cards[0] if cards else {}


def build_ops_page_cards(
    rows: List[OpsCardRow],
    *,
    window_start: str,
    window_end: str,
    page_size: int = _DEPLOY_PAGE_SIZE,
    total_in_window: int = 0,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    page_size = max(1, min(int(page_size or _DEPLOY_PAGE_SIZE), 8))
    pages: List[List[OpsCardRow]] = [
        rows[i : i + page_size] for i in range(0, len(rows), page_size)
    ]
    total = total_in_window or len(rows)
    page_total = len(pages)
    cards: List[Dict[str, Any]] = []
    for page_idx, page in enumerate(pages):
        page_current = page_idx + 1
        item_start = page_idx * page_size + 1
        item_end = item_start + len(page) - 1
        header: Dict[str, Any] = {
            "template": "red" if page_current == 1 else "carmine",
            "title": {"tag": "plain_text", "content": "🔴 线上操作汇总"},
            "subtitle": {"tag": "plain_text", "content": f"{window_start} — {window_end} MYT"},
        }
        if page_total > 1:
            header["text_tag_list"] = [
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": f"第 {page_current} 页 / 共 {page_total} 页",
                    },
                    "color": "neutral",
                }
            ]
        else:
            header["text_tag_list"] = [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": f"{total} 条"},
                    "color": "neutral",
                }
            ]
        elements: List[Dict[str, Any]] = []
        if page_total > 1:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"<font color='grey'>条目 {item_start}–{item_end} · {total} 条 · "
                        f"按执行时间倒序</font>"
                    ),
                }
            )
        else:
            elements.append(
                {"tag": "markdown", "content": "**操作记录**（按执行时间倒序）"},
            )
        elements.append({"tag": "hr"})
        for row in page:
            elements.extend(_ops_entry_elements(row))
        if page_total > 1:
            elements.append(
                _card_footer_note(
                    f"OSE 系统自动生成 · 第 {page_current} 页 · 条目 {item_start}–{item_end} / {total}"
                )
            )
        else:
            elements.append(_card_footer_note("OSE 系统自动生成"))
        cards.append(_wrap_schema_v2(header, elements))
    return cards


def build_ops_empty_card(*, window_start: str, window_end: str) -> Dict[str, Any]:
    """Ops card shell when 0 rows in the 48h window (same header as populated cards)."""
    header: Dict[str, Any] = {
        "template": "red",
        "title": {"tag": "plain_text", "content": "🔴 线上操作汇总"},
        "subtitle": {"tag": "plain_text", "content": f"{window_start} — {window_end} MYT"},
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": "0 条"},
                "color": "neutral",
            }
        ],
    }
    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": "**操作记录**（按执行时间倒序）"},
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "No records gathered from Base within 48 hrs.",
            },
        },
        _card_footer_note("OSE 系统自动生成"),
    ]
    return _wrap_schema_v2(header, elements)


def build_deploy_empty_card(*, window_start: str, window_end: str) -> Dict[str, Any]:
    """Deploy card shell when 0 rows in the 48h window (same header as populated cards)."""
    header: Dict[str, Any] = {
        "template": "blue",
        "title": {"tag": "plain_text", "content": "📦 部署流水"},
        "subtitle": {"tag": "plain_text", "content": f"{window_start} — {window_end} MYT"},
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": "0 条"},
                "color": "neutral",
            }
        ],
    }
    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": "<font color='grey'>部署记录 · Full Release 时间倒序</font>",
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "No records gathered from Base within 48 hrs.",
            },
        },
        _card_footer_note("OSE 系统自动生成"),
    ]
    return _wrap_schema_v2(header, elements)


def build_deploy_page_cards(
    rows: List[DeployCardRow],
    *,
    window_start: str,
    window_end: str,
    page_size: int = _DEPLOY_PAGE_SIZE,
    total_in_window: int = 0,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    page_size = max(1, min(int(page_size or _DEPLOY_PAGE_SIZE), 8))
    pages: List[List[DeployCardRow]] = [
        rows[i : i + page_size] for i in range(0, len(rows), page_size)
    ]
    total = total_in_window or len(rows)
    page_total = len(pages)
    cards: List[Dict[str, Any]] = []
    for page_idx, page in enumerate(pages):
        page_current = page_idx + 1
        item_start = page_idx * page_size + 1
        item_end = item_start + len(page) - 1
        header_color = "blue" if page_current == 1 else "wathet"
        header = {
            "template": header_color,
            "title": {"tag": "plain_text", "content": "📦 部署流水"},
            "subtitle": {"tag": "plain_text", "content": f"{window_start} — {window_end} MYT"},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": f"第 {page_current} 页 / 共 {page_total} 页",
                    },
                    "color": "neutral",
                }
            ],
        }
        elements: List[Dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": (
                    f"<font color='grey'>条目 {item_start}–{item_end} · {total} 条 · "
                    f"Full Release 时间倒序</font>"
                ),
            },
            {"tag": "hr"},
        ]
        for row in page:
            elements.extend(_deploy_entry_elements(row))
        elements.append(
            _card_footer_note(
                f"OSE 系统自动生成 · 第 {page_current} 页 · 条目 {item_start}–{item_end} / {total}"
            )
        )
        cards.append(_wrap_schema_v2(header, elements))
    return cards
