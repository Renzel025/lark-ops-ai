"""
After P0 overview is sent, check Lark Bitable tables for recent side deployments / ops.

**Deployments** table: Blue Green Time / Full Release Time in window.
**线上操作** table: 执行操作时间 / 执行完毕时间 in window.

Window: **yesterday 00:00 MYT** through **end of today 23:59 MYT** (full 48h / two calendar days).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore


def _bitable_tz() -> ZoneInfo:
    return ZoneInfo(_config.get_p0_adjustment_bitable_timezone_name())


def _tz_label() -> str:
    return _config.get_p0_adjustment_bitable_tz_label()


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
                dt = datetime.strptime(s, fmt).replace(tzinfo=_bitable_tz())
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


def _format_field_display(val: Any) -> str:
    """Human-readable cell value; timestamps use MYT ``YYYY-MM-DD HH:MM:SS``."""
    ts = _field_epoch_ms(val)
    if ts:
        return _fmt_ts_full(ts)
    return _field_text(val)


def _all_detail_fields(fields: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every Bitable column on the row (sorted by header name)."""
    if not fields:
        return []
    out: List[Tuple[str, str]] = []
    for key in sorted(fields.keys(), key=lambda k: str(k).casefold()):
        label = str(key).strip()
        if not label:
            continue
        out.append((label, _format_field_display(fields[key])))
    return out


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
    """Match Bitable column format (YYYY-MM-DD HH:MM:SS, MYT)."""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_bitable_tz()).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _window_bounds_ms() -> Tuple[int, int, str]:
    """
    Full 48h window: yesterday 00:00 MYT through end of today 23:59:59 MYT.
    """
    tz = _bitable_tz()
    label = _tz_label()
    now = datetime.now(tz=tz)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_midnight = today_midnight - timedelta(days=1)
    today_end = today_midnight + timedelta(days=1) - timedelta(seconds=1)
    cutoff_ms = int(yesterday_midnight.timestamp() * 1000)
    end_ms = int(today_end.timestamp() * 1000)
    window_label = (
        f"{yesterday_midnight.strftime('%Y-%m-%d %H:%M')} – "
        f"{today_end.strftime('%Y-%m-%d %H:%M')} {label}"
    )
    return cutoff_ms, end_ms, window_label


def _card_subtitle(*, kind: str, count: int, window_label: str) -> str:
    n = max(0, int(count or 0))
    win = (window_label or f"yesterday 00:00 – end of today {_tz_label()}").strip()
    if kind == "online_ops":
        return f"{n} 条线上操作记录（执行操作时间 / 执行完毕时间在窗口内）({win})"
    return f"{n} service(s) with Blue Green or Full Release ({win})"


def _summary_line(*, kind: str, count: int) -> str:
    n = max(0, int(count or 0))
    if kind == "online_ops":
        return f"{n} 条线上操作记录（执行操作时间 / 执行完毕时间在窗口内）:"
    return f"{n} service(s) with Blue Green or Full Release in window:"


class BitableNoticeRow:
    __slots__ = ("detail_fields", "sort_ms")

    def __init__(
        self,
        *,
        detail_fields: List[Tuple[str, str]],
        sort_ms: int = 0,
    ) -> None:
        self.detail_fields = list(detail_fields)
        self.sort_ms = sort_ms

    def block_lines(self, *, index: int) -> List[str]:
        out = [f"{index}."]
        for label, value in self.detail_fields:
            out.append(f"   {label}: {value or '—'}")
        return out

    def block_md(self, *, index: int) -> str:
        lines: List[str] = []
        for label, value in self.detail_fields:
            lines.append(f"- **{label}:** {value or '—'}")
        return f"**{index}.**\n" + "\n".join(lines)


def _all_fields_for_table(table_id: str) -> bool:
    """True when this Bitable table should show every column (per-table allowlist)."""
    tid = (table_id or "").strip()
    if not tid:
        return False
    return tid in _config.get_p0_adjustment_bitable_all_fields_table_ids()


def _row_from_record(
    fields: Dict[str, Any],
    cfg: Dict[str, Tuple[str, ...]],
    *,
    kind: str,
    cutoff_ms: int,
    end_ms: int,
    table_id: str = "",
) -> Optional[BitableNoticeRow]:
    if kind == "online_ops":
        start_ms = _pick_time_ms(fields, cfg["op_start_time"]) or 0
        done_ms = _pick_time_ms(fields, cfg["op_done_time"]) or 0
        in_window = [
            t for t in (start_ms, done_ms) if t and cutoff_ms <= t <= end_ms
        ]
        if not in_window:
            return None
        if _all_fields_for_table(table_id):
            detail_fields = _all_detail_fields(fields)
        else:
            detail_fields = [
                ("执行操作", _pick_field(fields, cfg["operation"])),
                ("执行操作时间", _fmt_ts_full(start_ms) if start_ms else ""),
                ("项目", _pick_field(fields, cfg["project"])),
                ("执行原因", _pick_field(fields, cfg["reason"])),
                ("执行完毕时间", _fmt_ts_full(done_ms) if done_ms else ""),
            ]
        return BitableNoticeRow(detail_fields=detail_fields, sort_ms=max(in_window))

    blue_green_ms = _pick_time_ms(fields, cfg["blue_green_time"]) or 0
    full_release_ms = _pick_time_ms(fields, cfg["full_release_time"]) or 0
    in_window = [
        t
        for t in (blue_green_ms, full_release_ms)
        if t and cutoff_ms <= t <= end_ms
    ]
    if not in_window:
        return None
    if _all_fields_for_table(table_id):
        detail_fields = _all_detail_fields(fields)
    else:
        tag = (_pick_field(fields, cfg["image_tag"]) or "").strip()
        detail_fields = [
            ("Service", _pick_field(fields, cfg["service"])),
            ("Namespace", _pick_field(fields, cfg["namespace"])),
            ("Image Tag", tag),
            ("Blue Green Time", _fmt_ts_full(blue_green_ms) if blue_green_ms else ""),
            ("Full Release Time", _fmt_ts_full(full_release_ms) if full_release_ms else ""),
            ("Project", _pick_field(fields, cfg["project"])),
        ]
    return BitableNoticeRow(detail_fields=detail_fields, sort_ms=max(in_window))


def _expected_time_columns(cfg: Dict[str, Tuple[str, ...]], *, kind: str) -> Tuple[str, str]:
    if kind == "online_ops":
        return cfg["op_start_time"][0], cfg["op_done_time"][0]
    return cfg["blue_green_time"][0], cfg["full_release_time"][0]


def fetch_recent_adjustments(
    tenant_token: str,
    *,
    app_token: str = "",
    table_id: str = "",
    field_names: Optional[Dict[str, Tuple[str, ...]]] = None,
    source_id: str = "",
    kind: str = "deployments",
) -> Tuple[List[BitableNoticeRow], str, str]:
    """Rows with time columns in yesterday 00:00 MYT through end of today MYT."""
    if not _config.p0_adjustment_bitable_enabled():
        return [], "", ""
    app_token = (app_token or _config.get_p0_adjustment_bitable_app_token()).strip()
    table_id = (table_id or _config.get_p0_adjustment_bitable_table_id()).strip()
    if not tenant_token:
        return [], "tenant_token missing", ""
    if not app_token or not table_id:
        return [], "P0_ADJUSTMENT_BITABLE_APP_TOKEN or TABLE_ID not set in .env", ""

    cfg = field_names or _config.get_p0_adjustment_bitable_field_names()
    row_kind = (kind or source_id or "deployments").strip() or "deployments"
    cutoff_ms, end_ms, window_label = _window_bounds_ms()

    records, err = _lark.list_bitable_records(tenant_token, app_token, table_id)
    if err:
        return [], err, window_label

    log.info(
        "adjustment_bitable: fetched %s record(s) source=%s kind=%s table_id_tail=%s "
        "window=%s cutoff_ms=%s end_ms=%s",
        len(records),
        source_id or row_kind,
        row_kind,
        table_id[-8:] if len(table_id) > 8 else table_id,
        window_label,
        cutoff_ms,
        end_ms,
    )

    rows: List[BitableNoticeRow] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        row = _row_from_record(
            fields,
            cfg,
            kind=row_kind,
            cutoff_ms=cutoff_ms,
            end_ms=end_ms,
            table_id=table_id,
        )
        if row:
            rows.append(row)

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
        t1, t2 = _expected_time_columns(cfg, kind=row_kind)
        log.info(
            "adjustment_bitable: 0 rows in window %s (source=%s total_records=%s). "
            "Expect columns %s / %s. Sample field names: %s",
            window_label,
            source_id or row_kind,
            len(records),
            t1,
            t2,
            sample_fields or "(none)",
        )
    return rows, "", window_label


def build_adjustment_notice_md(rows: List[BitableNoticeRow], *, cutoff_ms: int) -> str:
    """Card body markdown (header lives on the interactive card)."""
    _ = cutoff_ms
    if not rows:
        return ""
    parts: List[str] = []
    for i, row in enumerate(rows, start=1):
        parts.append(row.block_md(index=i))
        if i < len(rows):
            parts.append("---")
    return "\n\n".join(parts)


def build_adjustment_notice_text(
    rows: List[BitableNoticeRow],
    *,
    cutoff_ms: int,
    card_title: str,
    window_label: str,
    kind: str = "deployments",
) -> str:
    """Plain-text fallback when card post fails."""
    _ = cutoff_ms
    if not rows:
        return ""
    title = (card_title or "Deployments").strip() or "Deployments"
    win = (window_label or f"yesterday 00:00 – end of today {_tz_label()}").strip()
    lines = [
        f"⚙️ {title} ({win})",
        _summary_line(kind=kind, count=len(rows)),
        "",
    ]
    for i, row in enumerate(rows, start=1):
        lines.extend(row.block_lines(index=i))
        if i < len(rows):
            lines.append("")
    return "\n".join(lines)


def _post_one_adjustment_notice(
    tenant_token: str,
    *,
    group_chat_id: str,
    overview_message_id: str,
    rows: List[BitableNoticeRow],
    cutoff_ms: int,
    card_title: str,
    source_id: str,
    window_label: str,
    kind: str = "deployments",
) -> bool:
    body_md = build_adjustment_notice_md(rows, cutoff_ms=cutoff_ms)
    text_fallback = build_adjustment_notice_text(
        rows,
        cutoff_ms=cutoff_ms,
        card_title=card_title,
        window_label=window_label,
        kind=kind,
    )
    if not body_md and not text_fallback:
        return False

    reply_in_thread = _config.p0_adjustment_bitable_reply_in_thread()
    also_group = _config.p0_adjustment_bitable_also_send_to_group()
    card = _cards.build_adjustment_bitable_card(
        body_md,
        count=len(rows),
        title=card_title,
        subtitle=_card_subtitle(kind=kind, count=len(rows), window_label=window_label),
    )

    def _post_flat_to_group(*, prefer_card: bool) -> bool:
        if prefer_card:
            st_f, body_f, _ = _lark.post_card_to_chat(group_chat_id, tenant_token, card)
            ok_f, code_f, msg_f = _lark.lark_im_message_create_ok(body_f)
            if st_f == 200 and ok_f:
                return True
            log.warning(
                "adjustment_bitable: flat card failed source=%s HTTP=%s lark_code=%s lark_msg=%r",
                source_id,
                st_f,
                code_f,
                msg_f,
            )
        st_f, body_f = _lark.post_text_to_chat(group_chat_id, tenant_token, text_fallback)
        ok_f, code_f, msg_f = _lark.lark_im_message_create_ok(body_f)
        if st_f != 200 or not ok_f:
            log.warning(
                "adjustment_bitable: flat text failed source=%s HTTP=%s lark_code=%s lark_msg=%r",
                source_id,
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
            "adjustment_bitable: card reply failed source=%s HTTP=%s lark_code=%s lark_msg=%r — trying text",
            source_id,
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
            "adjustment_bitable: thread post failed source=%s HTTP=%s lark_code=%s lark_msg=%r dest_tail=%s",
            source_id,
            st,
            code,
            msg,
            group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        )
        if not _post_flat_to_group(prefer_card=True):
            return False
    elif also_group:
        if not _post_flat_to_group(prefer_card=thread_used_card):
            log.warning(
                "adjustment_bitable: also-send-to-group failed source=%s dest_tail=%s (thread ok)",
                source_id,
                group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
            )
        else:
            log.info(
                "adjustment_bitable: also sent card to main group source=%s dest_tail=%s",
                source_id,
                group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
            )

    log.info(
        "adjustment_bitable: posted %s row(s) source=%s title=%r under overview mid_tail=%s also_group=%s",
        len(rows),
        source_id,
        card_title,
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
        also_group,
    )
    return True


def maybe_post_adjustment_notice_after_overview(
    tenant_token: str,
    *,
    group_chat_id: str,
    overview_message_id: str,
    sender_open_id: str = "",
) -> Tuple[bool, str]:
    """
    Query configured Bitable tables and post thread replies on the overview when rows exist.
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

    sources = _config.get_p0_adjustment_bitable_sources()
    if not sources:
        log.info(
            "adjustment_bitable: skipped (no table IDs — set P0_ADJUSTMENT_BITABLE_TABLE_ID "
            "and/or P0_ADJUSTMENT_BITABLE_OPS_TABLE_ID)"
        )
        return False, ""

    cutoff_ms, _end_ms, window_label = _window_bounds_ms()
    app_token = _config.get_p0_adjustment_bitable_app_token()

    log.info(
        "adjustment_bitable: checking after send_preview dest_tail=%s mid_tail=%s "
        "window=%s sources=%s",
        group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
        window_label,
        ",".join(s[0] for s in sources),
    )

    posted_any = False
    dm_lines: List[str] = []
    for source_id, table_id, card_title, kind, field_names in sources:
        rows, err, win_label = fetch_recent_adjustments(
            tenant_token,
            app_token=app_token,
            table_id=table_id,
            field_names=field_names,
            source_id=source_id,
            kind=kind,
        )
        win_label = win_label or window_label
        if err:
            log.warning(
                "adjustment_bitable: fetch failed source=%s open_id_tail=%s err=%s",
                source_id,
                sender_open_id[-12:] if len(sender_open_id) > 12 else sender_open_id,
                err[:300],
            )
            continue
        if not rows:
            log.info(
                "adjustment_bitable: no rows source=%s in window %s overview_mid_tail=%s",
                source_id,
                win_label,
                overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
            )
            continue
        if _post_one_adjustment_notice(
            tenant_token,
            group_chat_id=group_chat_id,
            overview_message_id=overview_message_id,
            rows=rows,
            cutoff_ms=cutoff_ms,
            card_title=card_title,
            source_id=source_id,
            window_label=win_label,
            kind=kind,
        ):
            posted_any = True
            dm_lines.append(
                f"⚙️ **{card_title}**: {len(rows)} row(s) in window {win_label} "
                f"(posted under overview)."
            )

    return posted_any, "\n".join(dm_lines)
