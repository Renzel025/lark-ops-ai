"""Build deployment / ops Lark cards (boss template layout, Lark API-safe)."""
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


def _text_tag_md(color: str, text: str) -> str:
    return f'<text_tag color="{color}">{_dash(text)}</text_tag>'


def _wrap_schema_v2(raw: Dict[str, Any], elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(raw.get("config") or {})
    cfg.setdefault("enable_forward", True)
    header = copy.deepcopy(raw.get("header") or {})
    return {
        "schema": "2.0",
        "config": cfg,
        "header": header,
        "body": {"elements": elements},
    }


def _substitute_obj(obj: Any, variables: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        out = obj
        for key, val in variables.items():
            out = out.replace(f"{{{{{key}}}}}", val)
        return out
    if isinstance(obj, list):
        return [_substitute_obj(x, variables) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute_obj(v, variables) for k, v in obj.items()}
    return obj


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
    """Boss card1: grey column_set blocks per field."""
    return [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {"tag": "markdown", "content": "<font color='grey'>执行时间</font>"},
                        {"tag": "markdown", "content": f"**{_dash(row.exec_time)}**"},
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {"tag": "markdown", "content": "<font color='grey'>完毕时间</font>"},
                        {"tag": "markdown", "content": f"**{_dash(row.done_time)}**"},
                    ],
                },
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"<font color='grey'>执行操作</font>\n{_dash(row.action)}",
                        }
                    ],
                }
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": "<font color='grey'>项目</font>"},
                        {"tag": "markdown", "content": f"**{_dash(row.project)}**"},
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": "<font color='grey'>操作人员</font>"},
                        {"tag": "markdown", "content": f"**{_dash(row.operator)}**"},
                    ],
                },
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"<font color='grey'>执行原因</font>\n{_dash(row.reason)}",
                        }
                    ],
                }
            ],
        },
    ]


def _changelog_collapsible_panel(changelog: str) -> Dict[str, Any]:
    """Boss template collapsible — padding must be 4-side in Schema 2.0 (not ``4px 0px``)."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "markdown",
                "content": "<font color='grey'>更新内容</font>",
            },
            "vertical_align": "center",
            "padding": "4px 0px 4px 0px",
        },
        "elements": [
            {"tag": "markdown", "content": _dash(changelog)},
        ],
    }


def _card_footer_note(text: str) -> Dict[str, Any]:
    """
    Schema 2.0 dropped ``note`` tag (Lark returns unsupported tag note).
    Same text via grey markdown — official v2 replacement.
    """
    return {"tag": "markdown", "content": f"<font color='grey'>{text}</font>"}


def _deploy_entry_elements(row: DeployCardRow) -> List[Dict[str, Any]]:
    """Boss card2 column_set + collapsible_panel (padding fixed for Schema 2.0)."""
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
                                f"<font color='grey'>BG</font>\n**{_dash(row.bg_time)}**\n"
                                f"<font color='grey'>Full</font>\n**{_dash(row.full_time)}**"
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
                                            "content": _text_tag_md("green", row.version),
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": _text_tag_md("neutral", row.project),
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        {
                                            "tag": "markdown",
                                            "content": _text_tag_md("purple", row.pm),
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
                        _changelog_collapsible_panel(row.changelog),
                    ],
                },
            ],
        }
    ]


def build_ops_summary_card(
    rows: List[OpsCardRow],
    *,
    window_start: str,
    window_end: str,
    total_in_window: int = 0,
) -> Dict[str, Any]:
    raw = json.loads((_TEMPLATES / "card1_ops.json").read_text(encoding="utf-8"))
    shown = len(rows)
    total = total_in_window or shown
    # Boss spec: total_count = all matching rows in window (not just rows on card).
    count_label = str(total)
    variables = {
        "window_start": window_start,
        "window_end": window_end,
        "total_count": count_label,
    }
    raw = _substitute_obj(raw, variables)
    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": "**操作记录**（按执行时间倒序 · 已过滤 Rejected）"},
        {"tag": "hr"},
    ]
    for i, row in enumerate(rows):
        elements.extend(_ops_entry_elements(row))
        if i < len(rows) - 1:
            elements.append({"tag": "hr"})
    # Boss footer text; Schema 2.0 uses grey markdown instead of deprecated ``note`` tag.
    elements.extend(
        [
            {"tag": "hr"},
            _card_footer_note("OSE 系统自动生成 · 如需完整记录请查阅 Lark Base"),
        ]
    )
    return _wrap_schema_v2(raw, elements)


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
    shown = len(rows)
    total = total_in_window or shown
    page_total = len(pages)
    cards: List[Dict[str, Any]] = []
    for page_idx, page in enumerate(pages):
        page_current = page_idx + 1
        item_start = page_idx * page_size + 1
        item_end = item_start + len(page) - 1
        header_template = "blue" if page_current == 1 else "wathet"
        header = {
            "template": header_template,
            "title": {"tag": "plain_text", "content": "📦 Deployment"},
            "subtitle": {
                "tag": "plain_text",
                "content": f"{window_start} — {window_end} MYT",
            },
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
        for i, row in enumerate(page):
            elements.extend(_deploy_entry_elements(row))
            if i < len(page) - 1:
                elements.append({"tag": "hr"})
        elements.append(
            _card_footer_note(
                f"OSE 系统自动生成 · 第 {page_current} 页 · "
                f"条目 {item_start}–{item_end} / {total}"
            )
        )
        cards.append(
            {
                "schema": "2.0",
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "header": header,
                "body": {"elements": elements},
            }
        )
    return cards
