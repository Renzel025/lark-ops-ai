"""
After P0 overview is sent, check a Lark Bitable for recent side deployments.

Only the **Blue Green Time** and **Full Release Time** columns are checked:
if either timestamp falls within the lookback window (default 24h from P0 send),
the row is included in the follow-up notice.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_SGT = timezone(timedelta(hours=8))


def _field_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, (int, float)):
        return str(int(val)) if float(val).is_integer() else str(val)
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        parts: List[str] = []
        for item in val:
            if isinstance(item, dict):
                t = str(item.get("text") or item.get("name") or "").strip()
                if t:
                    parts.append(t)
            elif item is not None:
                s = str(item).strip()
                if s:
                    parts.append(s)
        return ", ".join(parts)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or val.get("value") or "").strip()
    return str(val).strip()


def _field_epoch_ms(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        n = int(val)
        if n <= 0:
            return None
        if n < 1_000_000_000_000:
            n *= 1000
        return n
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if re.fullmatch(r"\d{10,13}", s):
            return _field_epoch_ms(int(s))
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=_SGT)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return None


def _pick_field(fields: Dict[str, Any], names: Tuple[str, ...]) -> str:
    if not fields or not names:
        return ""
    for name in names:
        if not name:
            continue
        if name in fields:
            return _field_text(fields[name])
    lower_map = {str(k).strip().lower(): k for k in fields}
    for name in names:
        key = lower_map.get((name or "").strip().lower())
        if key is not None:
            return _field_text(fields[key])
    return ""


def _pick_time_ms(fields: Dict[str, Any], names: Tuple[str, ...]) -> Optional[int]:
    for name in names:
        if name and name in fields:
            ts = _field_epoch_ms(fields[name])
            if ts:
                return ts
    lower_map = {str(k).strip().lower(): k for k in fields}
    for name in names:
        key = lower_map.get((name or "").strip().lower())
        if key is not None:
            ts = _field_epoch_ms(fields[key])
            if ts:
                return ts
    return None


def _fmt_ts_full(ms: int) -> str:
    """Match Bitable column format (YYYY-MM-DD HH:MM:SS, SGT)."""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_SGT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _display_image_tag(tag: str) -> str:
    """Full Image Tag from Bitable (no truncation)."""
    return (tag or "").strip()


class AdjustmentRow:
    __slots__ = (
        "service",
        "namespace",
        "image_tag",
        "project",
        "blue_green_ms",
        "full_release_ms",
        "sort_ms",
    )

    def __init__(
        self,
        *,
        service: str = "",
        namespace: str = "",
        image_tag: str = "",
        project: str = "",
        blue_green_ms: int = 0,
        full_release_ms: int = 0,
        sort_ms: int = 0,
    ) -> None:
        self.service = service
        self.namespace = namespace
        self.image_tag = image_tag
        self.project = project
        self.blue_green_ms = blue_green_ms
        self.full_release_ms = full_release_ms
        self.sort_ms = sort_ms

    def _detail_fields(self) -> List[Tuple[str, str]]:
        """All Bitable columns shown in the notice (matches table headers)."""
        tag = _display_image_tag(self.image_tag)
        return [
            ("Service", (self.service or "").strip()),
            ("Namespace", (self.namespace or "").strip()),
            ("Image Tag", tag),
            ("Blue Green Time", _fmt_ts_full(self.blue_green_ms) if self.blue_green_ms else ""),
            ("Full Release Time", _fmt_ts_full(self.full_release_ms) if self.full_release_ms else ""),
            ("Project", (self.project or "").strip()),
        ]

    def block_lines(self, *, index: int, cutoff_ms: int = 0) -> List[str]:
        """Plain-text block per deployment (fallback message)."""
        _ = cutoff_ms
        out = [f"{index}."]
        for label, value in self._detail_fields():
            out.append(f"   {label}: {value or '—'}")
        return out

    def block_md(self, *, index: int, cutoff_ms: int = 0) -> str:
        """Markdown block for interactive card body."""
        _ = cutoff_ms
        lines: List[str] = []
        for label, value in self._detail_fields():
            display = value or "—"
            if label == "Image Tag" and value:
                lines.append(f"- **{label}:** `{display}`")
            else:
                lines.append(f"- **{label}:** {display}")
        return f"**{index}.**\n" + "\n".join(lines)


def fetch_recent_adjustments(tenant_token: str) -> Tuple[List[AdjustmentRow], str]:
    """
    Rows where **Blue Green Time** or **Full Release Time** is within the lookback window.
    """
    if not _config.p0_adjustment_bitable_enabled():
        return [], ""
    app_token = _config.get_p0_adjustment_bitable_app_token()
    table_id = _config.get_p0_adjustment_bitable_table_id()
    if not tenant_token:
        return [], "tenant_token missing"
    if not app_token or not table_id:
        return [], "P0_ADJUSTMENT_BITABLE_APP_TOKEN or TABLE_ID not set in .env"

    cfg = _config.get_p0_adjustment_bitable_field_names()
    hours = _config.get_p0_adjustment_bitable_hours()
    cutoff_ms = int((time.time() - hours * 3600) * 1000)

    records, err = _lark.list_bitable_records(tenant_token, app_token, table_id)
    if err:
        return [], err

    log.info(
        "adjustment_bitable: fetched %s record(s) table_id_tail=%s hours=%s cutoff_ms=%s",
        len(records),
        table_id[-8:] if len(table_id) > 8 else table_id,
        hours,
        cutoff_ms,
    )

    rows: List[AdjustmentRow] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("fields")
        if not isinstance(fields, dict):
            fields = {}

        blue_green_ms = _pick_time_ms(fields, cfg["blue_green_time"]) or 0
        full_release_ms = _pick_time_ms(fields, cfg["full_release_time"]) or 0

        in_window = [
            t
            for t in (blue_green_ms, full_release_ms)
            if t and t >= cutoff_ms
        ]
        if not in_window:
            continue

        rows.append(
            AdjustmentRow(
                service=_pick_field(fields, cfg["service"]),
                namespace=_pick_field(fields, cfg["namespace"]),
                image_tag=_pick_field(fields, cfg["image_tag"]),
                project=_pick_field(fields, cfg["project"]),
                blue_green_ms=blue_green_ms,
                full_release_ms=full_release_ms,
                sort_ms=max(in_window),
            )
        )

    rows.sort(key=lambda r: r.sort_ms, reverse=True)
    max_rows = _config.get_p0_adjustment_bitable_max_rows()
    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[:max_rows]
    if not rows and records:
        sample_fields: List[str] = []
        for rec in records[:1]:
            fld = rec.get("fields") if isinstance(rec, dict) else None
            if isinstance(fld, dict):
                sample_fields = sorted(str(k) for k in fld.keys())[:12]
        log.info(
            "adjustment_bitable: 0 rows in %sh window (total_records=%s). "
            "Expect columns %s / %s. Sample field names: %s",
            hours,
            len(records),
            cfg["blue_green_time"][0],
            cfg["full_release_time"][0],
            sample_fields or "(none)",
        )
    return rows, ""


def build_adjustment_notice_md(rows: List[AdjustmentRow], *, cutoff_ms: int) -> str:
    """Card body markdown (header lives on the interactive card)."""
    if not rows:
        return ""
    parts: List[str] = []
    for i, row in enumerate(rows, start=1):
        parts.append(row.block_md(index=i, cutoff_ms=cutoff_ms))
        if i < len(rows):
            parts.append("---")
    return "\n\n".join(parts)


def build_adjustment_notice_text(rows: List[AdjustmentRow], *, cutoff_ms: int) -> str:
    """Plain-text fallback when card post fails."""
    if not rows:
        return ""
    hours = _config.get_p0_adjustment_bitable_hours()
    lines = [
        f"⚙️ Recent side deployments (last {hours}h)",
        f"{len(rows)} service(s) with Blue Green or Full Release in window:",
        "",
    ]
    for i, row in enumerate(rows, start=1):
        lines.extend(row.block_lines(index=i, cutoff_ms=cutoff_ms))
        if i < len(rows):
            lines.append("")
    return "\n".join(lines)


def maybe_post_adjustment_notice_after_overview(
    tenant_token: str,
    *,
    group_chat_id: str,
    overview_message_id: str,
    sender_open_id: str = "",
) -> Tuple[bool, str]:
    """
    Query Bitable and post a thread reply on the overview when recent rows exist.
    Returns (posted, dm_appendix) — ``dm_appendix`` is a short line for the operator DM.
    """
    if not overview_message_id or not group_chat_id:
        log.info(
            "adjustment_bitable: skipped (no overview message_id or group_chat_id) mid_tail=%s",
            (overview_message_id or "")[-12:] if overview_message_id else "(empty)",
        )
        return False, ""
    if not _config.p0_adjustment_bitable_enabled():
        log.info(
            "adjustment_bitable: skipped (disabled — set P0_ADJUSTMENT_BITABLE_ENABLED=1 and "
            "P0_ADJUSTMENT_BITABLE_APP_TOKEN + TABLE_ID in .env, then restart)"
        )
        return False, ""

    log.info(
        "adjustment_bitable: checking after send_preview dest_tail=%s mid_tail=%s",
        group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
    )
    hours = _config.get_p0_adjustment_bitable_hours()
    cutoff_ms = int((time.time() - hours * 3600) * 1000)

    rows, err = fetch_recent_adjustments(tenant_token)
    if err:
        log.warning(
            "adjustment_bitable: fetch failed open_id_tail=%s err=%s",
            sender_open_id[-12:] if len(sender_open_id) > 12 else sender_open_id,
            err[:300],
        )
        return False, ""
    if not rows:
        log.info(
            "adjustment_bitable: no Blue Green / Full Release rows in last %sh overview_mid_tail=%s",
            hours,
            overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
        )
        return False, ""

    body_md = build_adjustment_notice_md(rows, cutoff_ms=cutoff_ms)
    text_fallback = build_adjustment_notice_text(rows, cutoff_ms=cutoff_ms)
    if not body_md and not text_fallback:
        return False, ""

    reply_in_thread = _config.p0_adjustment_bitable_reply_in_thread()
    also_group = _config.p0_adjustment_bitable_also_send_to_group()
    card = _cards.build_adjustment_bitable_card(body_md, hours=hours, count=len(rows))

    def _post_flat_to_group(*, prefer_card: bool) -> bool:
        if prefer_card:
            st_f, body_f, _ = _lark.post_card_to_chat(group_chat_id, tenant_token, card)
            ok_f, code_f, msg_f = _lark.lark_im_message_create_ok(body_f)
            if st_f == 200 and ok_f:
                return True
            log.warning(
                "adjustment_bitable: flat card failed HTTP=%s lark_code=%s lark_msg=%r",
                st_f,
                code_f,
                msg_f,
            )
        st_f, body_f = _lark.post_text_to_chat(group_chat_id, tenant_token, text_fallback)
        ok_f, code_f, msg_f = _lark.lark_im_message_create_ok(body_f)
        if st_f != 200 or not ok_f:
            log.warning(
                "adjustment_bitable: flat text failed HTTP=%s lark_code=%s lark_msg=%r",
                st_f,
                code_f,
                msg_f,
            )
            return False
        return True

    thread_ok = False
    thread_used_card = True
    st, body = _lark.post_card_reply_to_message(
        overview_message_id,
        tenant_token,
        card,
        reply_in_thread=reply_in_thread,
    )
    ok, code, msg = _lark.lark_im_message_create_ok(body)
    if st == 200 and ok:
        thread_ok = True
    else:
        log.warning(
            "adjustment_bitable: card reply failed HTTP=%s lark_code=%s lark_msg=%r — trying text",
            st,
            code,
            msg,
        )
        thread_used_card = False
        st, body = _lark.post_text_reply_to_message(
            overview_message_id,
            tenant_token,
            text_fallback,
            reply_in_thread=reply_in_thread,
        )
        ok, code, msg = _lark.lark_im_message_create_ok(body)
        thread_ok = st == 200 and ok

    if not thread_ok:
        log.warning(
            "adjustment_bitable: thread post failed HTTP=%s lark_code=%s lark_msg=%r dest_tail=%s",
            st,
            code,
            msg,
            group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        )
        if not _post_flat_to_group(prefer_card=True):
            return False, ""
    elif also_group:
        if not _post_flat_to_group(prefer_card=thread_used_card):
            log.warning(
                "adjustment_bitable: also-send-to-group failed dest_tail=%s (thread ok)",
                group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
            )
        else:
            log.info(
                "adjustment_bitable: also sent deployment card to main group dest_tail=%s",
                group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
            )

    log.info(
        "adjustment_bitable: posted %s row(s) as card under overview mid_tail=%s also_group=%s",
        len(rows),
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
        also_group,
    )
    dm_line = (
        f"⚙️ Noted {len(rows)} side deployment(s) with Blue Green / Full Release "
        f"in the last {hours}h (posted under overview)."
    )
    return True, dm_line
