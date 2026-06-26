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

from p0_logic import cards as _cards
from p0_logic import config as _config
from p0_logic import lark_client as _lark

from .deployment_card_builder import DeployCardRow, OpsCardRow, build_deploy_page_cards, build_ops_summary_card

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


def _fmt_ts_short(ms: int) -> str:
    """Deploy card time format e.g. ``Jun 26 00:42``."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=_bitable_tz()).strftime("%b %d %H:%M")
    except Exception:
        return ""


def _window_start_end_labels(window_label: str) -> tuple[str, str]:
    """Parse ``2026-06-25 00:00 – 2026-06-26 23:59 MYT`` → start/end without TZ suffix."""
    label = (window_label or "").strip()
    if " – " in label:
        left, right = label.split(" – ", 1)
        right = re.sub(r"\s+MYT\s*$", "", right.strip(), flags=re.I)
        return left.strip(), right.strip()
    return label, label


def _is_rejected_status(status: str) -> bool:
    return (status or "").strip().lower() == "rejected"


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


def _ops_card_row_from_record(
    fields: Dict[str, Any],
    cfg: Dict[str, Tuple[str, ...]],
    *,
    cutoff_ms: int,
    end_ms: int,
) -> Optional[OpsCardRow]:
    status = _pick_field(fields, cfg.get("status", ("执行状况阶段",)))
    if _is_rejected_status(status):
        return None
    start_ms = _pick_time_ms(fields, cfg["op_start_time"]) or 0
    done_ms = _pick_time_ms(fields, cfg["op_done_time"]) or 0
    in_window = [t for t in (start_ms, done_ms) if t and cutoff_ms <= t <= end_ms]
    if not in_window:
        return None
    exec_display = _fmt_ts_full(start_ms) if start_ms else ""
    if start_ms and len(exec_display) >= 16:
        exec_display = exec_display[5:16]  # MM-DD HH:MM for ops card
    done_display = _fmt_ts_full(done_ms) if done_ms else ""
    if done_ms and len(done_display) >= 16:
        done_display = done_display[5:16]
    return OpsCardRow(
        exec_time=exec_display,
        done_time=done_display,
        action=_pick_field(fields, cfg["operation"]),
        project=_pick_field(fields, cfg["project"]),
        operator=_pick_field(fields, cfg.get("operator", ("操作人员",))),
        reason=_pick_field(fields, cfg["reason"]),
        sort_ms=max(in_window),
    )


def _deploy_card_row_from_record(
    fields: Dict[str, Any],
    cfg: Dict[str, Tuple[str, ...]],
    *,
    cutoff_ms: int,
    end_ms: int,
) -> Optional[DeployCardRow]:
    blue_green_ms = _pick_time_ms(fields, cfg["blue_green_time"]) or 0
    full_release_ms = _pick_time_ms(fields, cfg["full_release_time"]) or 0
    in_window = [
        t for t in (blue_green_ms, full_release_ms) if t and cutoff_ms <= t <= end_ms
    ]
    if not in_window:
        return None
    image_tag = _pick_field(fields, cfg["image_tag"])
    version = _pick_field(fields, cfg.get("version", cfg["image_tag"]))
    return DeployCardRow(
        bg_time=_fmt_ts_short(blue_green_ms),
        full_time=_fmt_ts_short(full_release_ms),
        service=_pick_field(fields, cfg["service"]),
        version=version,
        project=_pick_field(fields, cfg["project"]),
        pm=_pick_field(fields, cfg.get("pm", ("PM",))),
        image_tag=image_tag,
        email=_pick_field(fields, cfg.get("email", ("Email", "Release Title"))),
        changelog=_pick_field(fields, cfg.get("changelog", ("Changelog", "更新内容"))),
        sort_ms=full_release_ms or blue_green_ms or max(in_window),
    )


def _fetch_bitable_records(
    tenant_token: str,
    *,
    app_token: str,
    table_id: str,
    source_id: str,
) -> Tuple[List[Dict[str, Any]], str, str, int, int]:
    if not tenant_token or not app_token or not table_id:
        return [], "missing token or table id", "", 0, 0
    cutoff_ms, end_ms, window_label = _window_bounds_ms()
    records, err = _lark.list_bitable_records(tenant_token, app_token, table_id)
    if err:
        return [], err, window_label, cutoff_ms, end_ms
    log.info(
        "adjustment_bitable: fetched %s record(s) source=%s table_id_tail=%s window=%s",
        len(records),
        source_id,
        table_id[-8:] if len(table_id) > 8 else table_id,
        window_label,
    )
    return records, "", window_label, cutoff_ms, end_ms


def fetch_ops_card_rows(
    tenant_token: str,
    *,
    app_token: str = "",
    table_id: str = "",
    field_names: Optional[Dict[str, Tuple[str, ...]]] = None,
    source_id: str = "online_ops",
) -> Tuple[List[OpsCardRow], str, str]:
    app_token = (app_token or _config.get_p0_adjustment_bitable_app_token()).strip()
    table_id = (table_id or _config.get_p0_adjustment_bitable_ops_table_id()).strip()
    cfg = field_names or _config.get_p0_adjustment_bitable_ops_field_names()
    records, err, window_label, cutoff_ms, end_ms = _fetch_bitable_records(
        tenant_token, app_token=app_token, table_id=table_id, source_id=source_id
    )
    if err:
        return [], err, window_label
    rows: List[OpsCardRow] = []
    for rec in records:
        fields = rec.get("fields") if isinstance(rec, dict) else None
        if not isinstance(fields, dict):
            continue
        row = _ops_card_row_from_record(fields, cfg, cutoff_ms=cutoff_ms, end_ms=end_ms)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r.sort_ms, reverse=True)
    max_rows = _config.get_p0_adjustment_bitable_max_rows()
    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[:max_rows]
    return rows, "", window_label


def fetch_deploy_card_rows(
    tenant_token: str,
    *,
    app_token: str = "",
    table_id: str = "",
    field_names: Optional[Dict[str, Tuple[str, ...]]] = None,
    source_id: str = "deployments",
) -> Tuple[List[DeployCardRow], str, str]:
    app_token = (app_token or _config.get_p0_adjustment_bitable_app_token()).strip()
    table_id = (table_id or _config.get_p0_adjustment_bitable_table_id()).strip()
    cfg = field_names or _config.get_p0_adjustment_bitable_field_names()
    records, err, window_label, cutoff_ms, end_ms = _fetch_bitable_records(
        tenant_token, app_token=app_token, table_id=table_id, source_id=source_id
    )
    if err:
        return [], err, window_label
    rows: List[DeployCardRow] = []
    for rec in records:
        fields = rec.get("fields") if isinstance(rec, dict) else None
        if not isinstance(fields, dict):
            continue
        row = _deploy_card_row_from_record(fields, cfg, cutoff_ms=cutoff_ms, end_ms=end_ms)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (r.sort_ms or 0), reverse=True)
    max_rows = _config.get_p0_adjustment_bitable_max_rows()
    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[:max_rows]
    return rows, "", window_label


def _post_cards_to_chat(tenant_token: str, group_chat_id: str, cards: List[Dict[str, Any]]) -> bool:
    ok_any = False
    for card in cards:
        st, body, _mid = _lark.post_card_to_chat(group_chat_id, tenant_token, card)
        ok, code, msg = _lark.lark_im_message_create_ok(body)
        if st == 200 and ok:
            ok_any = True
        else:
            log.warning(
                "adjustment_bitable: card post failed dest_tail=%s HTTP=%s code=%s msg=%r",
                group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
                st,
                code,
                msg,
            )
    return ok_any


def _post_boss_style_notices(
    tenant_token: str,
    *,
    group_chat_id: str,
    overview_message_id: str = "",
    trigger: str = "overview",
) -> Tuple[bool, List[str]]:
    """Post ops summary card then paginated deployment cards. Returns (posted, dm_lines)."""
    if not group_chat_id:
        return False, []
    app_token = _config.get_p0_adjustment_bitable_app_token()
    dm_lines: List[str] = []
    posted_any = False
    window_label = ""

    ops_tbl = _config.get_p0_adjustment_bitable_ops_table_id()
    if ops_tbl:
        ops_rows, err, window_label = fetch_ops_card_rows(
            tenant_token,
            app_token=app_token,
            table_id=ops_tbl,
            source_id="online_ops",
        )
        if err:
            log.warning("adjustment_bitable: ops fetch failed trigger=%s err=%s", trigger, err[:200])
        elif ops_rows:
            w_start, w_end = _window_start_end_labels(window_label)
            ops_card = build_ops_summary_card(ops_rows, window_start=w_start, window_end=w_end)
            cards = [ops_card]
            if _post_cards_to_chat(tenant_token, group_chat_id, cards):
                posted_any = True
                dm_lines.append(f"🔴 **线上操作汇总**: {len(ops_rows)} row(s) ({window_label})")
                log.info(
                    "adjustment_bitable: posted ops card trigger=%s rows=%s dest_tail=%s",
                    trigger,
                    len(ops_rows),
                    group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
                )

    deploy_tbl = _config.get_p0_adjustment_bitable_table_id()
    if deploy_tbl:
        dep_rows, err, win2 = fetch_deploy_card_rows(
            tenant_token,
            app_token=app_token,
            table_id=deploy_tbl,
            source_id="deployments",
        )
        window_label = win2 or window_label
        if err:
            log.warning("adjustment_bitable: deploy fetch failed trigger=%s err=%s", trigger, err[:200])
        elif dep_rows:
            w_start, w_end = _window_start_end_labels(window_label)
            page_size = _config.get_p0_adjustment_bitable_deploy_page_size()
            dep_cards = build_deploy_page_cards(
                dep_rows,
                window_start=w_start,
                window_end=w_end,
                page_size=page_size,
            )
            if _post_cards_to_chat(tenant_token, group_chat_id, dep_cards):
                posted_any = True
                pages = len(dep_cards)
                dm_lines.append(
                    f"📦 **Deployment**: {len(dep_rows)} row(s), {pages} card(s) ({window_label})"
                )
                log.info(
                    "adjustment_bitable: posted deploy cards trigger=%s rows=%s pages=%s dest_tail=%s",
                    trigger,
                    len(dep_rows),
                    pages,
                    group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
                )

    _ = overview_message_id  # boss cards post to group feed; thread reply optional later
    return posted_any, dm_lines


def _mark_session_bitable_posted(source_chat_id: str) -> None:
    cid = (source_chat_id or "").strip()
    if not cid.startswith("oc_"):
        return
    from features.session import session as _session
    from features.session import session_disk as _session_disk

    sess = _session.P0_SESSIONS.get(cid)
    if not sess:
        return
    sess["adjustment_bitable_posted"] = True
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess)


def _session_bitable_already_posted(source_chat_id: str) -> bool:
    cid = (source_chat_id or "").strip()
    if not cid.startswith("oc_"):
        return False
    from features.session import session as _session

    sess = _session.P0_SESSIONS.get(cid) or {}
    return bool(sess.get("adjustment_bitable_posted"))


def maybe_post_adjustment_notice_on_p0_declare(
    tenant_token: str,
    *,
    source_chat_id: str,
    priority: str = "P0",
) -> None:
    """Post ops + deployment cards when P0 is declared (once per session)."""
    cid = (source_chat_id or "").strip()
    if priority != "P0" or not cid.startswith("oc_"):
        return
    if not _config.p0_adjustment_bitable_enabled():
        log.warning(
            "adjustment_bitable: skipped on P0 declare — not enabled. "
            "Set P0_ADJUSTMENT_BITABLE_ENABLED=1 + P0_ADJUSTMENT_BITABLE_APP_TOKEN + "
            "TABLE_ID / OPS_TABLE_ID in .env (or .env.dev), restart service."
        )
        return
    if not _config.p0_adjustment_bitable_on_p0_declare():
        log.info("adjustment_bitable: on_p0_declare disabled (P0_ADJUSTMENT_BITABLE_ON_P0_DECLARE=0)")
        return
    if _session_bitable_already_posted(cid):
        return
    dest = _config.get_session_meeting_card_post_chat_id(cid)
    if not dest.startswith("oc_"):
        dest = cid
    log.info(
        "adjustment_bitable: P0 declare trigger source_tail=%s dest_tail=%s",
        cid[-12:] if len(cid) > 12 else cid,
        dest[-12:] if len(dest) > 12 else dest,
    )
    try:
        posted, _lines = _post_boss_style_notices(
            tenant_token,
            group_chat_id=dest,
            trigger="p0_declare",
        )
        if posted:
            _mark_session_bitable_posted(cid)
        else:
            log.warning(
                "adjustment_bitable: P0 declare — enabled but no cards posted "
                "(0 rows in 48h window, fetch error, or missing TABLE_ID). "
                "source_tail=%s dest_tail=%s — grep adjustment_bitable in journalctl",
                cid[-12:] if len(cid) > 12 else cid,
                dest[-12:] if len(dest) > 12 else dest,
            )
    except Exception as e:
        log.warning("adjustment_bitable: on_p0_declare failed source=%s err=%s", cid[:24], e)


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
    source_chat_id: str = "",
) -> Tuple[bool, str]:
    """
    Query Bitable and post ops + deployment boss-style cards when rows exist.
    Skips if already posted on P0 declare for this session.
    Returns (posted, dm_appendix).
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

    src = (source_chat_id or "").strip()
    if src and _session_bitable_already_posted(src):
        log.info(
            "adjustment_bitable: skipped after overview (already posted on P0 declare) source_tail=%s",
            src[-12:] if len(src) > 12 else src,
        )
        return False, ""

    log.info(
        "adjustment_bitable: checking after send_preview dest_tail=%s mid_tail=%s sources=%s",
        group_chat_id[-12:] if len(group_chat_id) > 12 else group_chat_id,
        overview_message_id[-12:] if len(overview_message_id) > 12 else overview_message_id,
        ",".join(s[0] for s in sources),
    )

    try:
        posted_any, dm_lines = _post_boss_style_notices(
            tenant_token,
            group_chat_id=group_chat_id,
            overview_message_id=overview_message_id,
            trigger="overview",
        )
        if posted_any and src:
            _mark_session_bitable_posted(src)
        return posted_any, "\n".join(dm_lines)
    except Exception as e:
        log.warning(
            "adjustment_bitable: after_overview failed open_id_tail=%s err=%s",
            sender_open_id[-12:] if len(sender_open_id) > 12 else sender_open_id,
            e,
        )
        return False, ""
