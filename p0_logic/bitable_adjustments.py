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


def _fmt_ts(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_SGT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _truncate(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)] + "…"


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

    def line(self, *, cutoff_ms: int) -> str:
        parts: List[str] = []
        if self.service:
            parts.append(self.service)
        if self.namespace:
            parts.append(f"({self.namespace})")
        head = " ".join(parts) if parts else "(entry)"
        tail_bits: List[str] = []
        if self.image_tag:
            tail_bits.append(_truncate(self.image_tag, 48))
        if self.blue_green_ms and self.blue_green_ms >= cutoff_ms:
            tail_bits.append(f"Blue Green {_fmt_ts(self.blue_green_ms)}")
        if self.full_release_ms and self.full_release_ms >= cutoff_ms:
            tail_bits.append(f"Full Release {_fmt_ts(self.full_release_ms)}")
        if self.project:
            tail_bits.append(self.project)
        tail = " — ".join(x for x in tail_bits if x)
        return f"• {head}" + (f" — {tail}" if tail else "")


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


def build_adjustment_notice_text(rows: List[AdjustmentRow], *, cutoff_ms: int) -> str:
    if not rows:
        return ""
    hours = _config.get_p0_adjustment_bitable_hours()
    url = _config.get_p0_adjustment_bitable_doc_url()
    lines = [
        f"⚙️ Side deployments (last {hours}h)",
        f"对方侧 Blue Green / Full Release 时间在过去 {hours} 小时内：",
        "",
    ]
    lines.extend(r.line(cutoff_ms=cutoff_ms) for r in rows)
    if url:
        lines.extend(["", f"📋 Bitable: {url}"])
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

    text = build_adjustment_notice_text(rows, cutoff_ms=cutoff_ms)
    if not text:
        return False, ""

    reply_in_thread = _config.p0_adjustment_bitable_reply_in_thread()
    st, body = _lark.post_text_reply_to_message(
        overview_message_id,
        tenant_token,
        text,
        reply_in_thread=reply_in_thread,
    )
    ok, code, msg = _lark.lark_im_message_create_ok(body)
    if st != 200 or not ok:
        log.warning(
            "adjustment_bitable: post failed HTTP=%s lark_code=%s lark_msg=%r dest_tail=%s",
            st,
            code,
            msg,
            group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        )
        st2, body2 = _lark.post_text_to_chat(group_chat_id, tenant_token, text)
        ok2, code2, msg2 = _lark.lark_im_message_create_ok(body2)
        if st2 != 200 or not ok2:
            log.warning(
                "adjustment_bitable: flat post also failed HTTP=%s lark_code=%s lark_msg=%r",
                st2,
                code2,
                msg2,
            )
            return False, ""
    log.info(
        "adjustment_bitable: posted %s row(s) (Blue Green / Full Release window) mid_tail=%s",
        len(rows),
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
    )
    dm_line = (
        f"⚙️ Noted {len(rows)} side deployment(s) with Blue Green / Full Release "
        f"in the last {hours}h (posted under overview)."
    )
    return True, dm_line
