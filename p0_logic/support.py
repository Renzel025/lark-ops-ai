"""
Support map from spreadsheet, department matching, support request building.
"""
from __future__ import annotations

import logging
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config as _config
from . import lark_client as _lark
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")

SUPPORT_MAP_TTL_SEC = _config.SUPPORT_MAP_TTL_SEC

_SUPPORT_MAP: Dict[str, str] = {}
_SUPPORT_MAP_TS = 0
_SUPPORT_LOCK = threading.Lock()


def _env_support_cfg() -> Tuple[str, str, str]:
    _config.reload_env_runtime()
    spreadsheet_token = (os.environ.get("SUPPORT_SHEET_SPREADSHEET_TOKEN") or "").strip()
    sheet_name = (os.environ.get("SUPPORT_SHEET_NAME") or "Sheet1").strip()
    rng = _text.strip_env_quotes(os.environ.get("SUPPORT_SHEET_RANGE") or "A:B")
    log.info("SUPPORT cfg: spreadsheet_token=%s sheet_name=%s range=%s", spreadsheet_token, sheet_name, rng)
    return spreadsheet_token, sheet_name, rng


def _rows_to_map(rows: List[List[Any]]) -> Tuple[Dict[str, str], str]:
    mp: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        name = str(row[0] or "").strip()
        dept = str(row[1] or "").strip()
        if not name or not dept:
            continue
        if name.lower() == "name" and dept.lower() in ("department", "dept"):
            continue
        mp[_text.norm_name(name)] = dept
    if not mp:
        return {}, "parsed 0 rows (check Name/Department columns)"
    return mp, ""


def _fetch_support_map(tenant_token: str) -> Tuple[Dict[str, str], str]:
    import os
    spreadsheet_token, sheet_name, sheet_range = _env_support_cfg()
    if not spreadsheet_token:
        return {}, "SUPPORT_SHEET_SPREADSHEET_TOKEN empty"
    rng = _text.strip_env_quotes(sheet_range) or "A:B"
    sheet_id = ""
    for base in _config.SHEETS_BASES:
        try:
            qurl = f"{base}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
            r = requests.get(
                qurl,
                headers={"Authorization": f"Bearer {tenant_token}"},
                **_config.timeout_kw(),
            )
            j, _ = _lark.safe_json(r)
            if r.status_code == 200 and j.get("code") == 0:
                sheets = (j.get("data") or {}).get("sheets") or []
                if isinstance(sheets, list) and sheets:
                    for s in sheets:
                        if isinstance(s, dict) and (s.get("title") == sheet_name):
                            sheet_id = str(s.get("sheet_id") or "").strip()
                            break
                    if not sheet_id:
                        sheet_id = str((sheets[0] or {}).get("sheet_id") or "").strip()
            if sheet_id:
                break
        except Exception:
            pass
    if not sheet_id:
        return {}, "failed to resolve sheet_id (sheets/query)"
    title_range = f"{sheet_id}!{rng}"
    rows, e1 = _lark.read_sheets_values_batch(tenant_token, spreadsheet_token, title_range)
    if rows:
        return _rows_to_map(rows)
    return {}, f"read failed ({e1})"


def get_support_map(tenant_token: str) -> Dict[str, str]:
    global _SUPPORT_MAP, _SUPPORT_MAP_TS
    now = int(time.time())
    with _SUPPORT_LOCK:
        if _SUPPORT_MAP and (now - _SUPPORT_MAP_TS) < SUPPORT_MAP_TTL_SEC:
            return dict(_SUPPORT_MAP)
    mp, err = _fetch_support_map(tenant_token)
    if err:
        log.warning("Support map load failed: %s", err)
    with _SUPPORT_LOCK:
        _SUPPORT_MAP = dict(mp)
        _SUPPORT_MAP_TS = now
    log.info("SUPPORT map loaded size=%s", len(mp))
    return dict(mp)


def match_dept_from_name(mp: Dict[str, str], name: str) -> str:
    if not mp:
        return ""
    k = _text.norm_name(name)
    if k and k in mp:
        return (mp.get(k) or "").strip()
    target = _text.norm_person_name(name)
    if not target:
        return ""
    for sheet_name, dept in mp.items():
        if _text.norm_person_name(sheet_name) == target:
            return (dept or "").strip()
    return ""


def extract_names_from_support_map(text: str, mp: Dict[str, str]) -> List[str]:
    src = (text or "").strip()
    if not src or not mp:
        return []
    norm_src = _text.norm_name(src)
    compact_src = _text.norm_person_name(src)
    out: List[str] = []
    seen = set()
    candidates = sorted(
        [sheet_name for sheet_name in mp.keys() if isinstance(sheet_name, str) and len(sheet_name.strip()) >= 3],
        key=len,
        reverse=True,
    )
    for sheet_name in candidates:
        cand_norm = _text.norm_name(sheet_name)
        cand_compact = _text.norm_person_name(sheet_name)
        if not cand_norm or not cand_compact:
            continue
        matched = False
        if f"@{cand_norm}" in norm_src:
            matched = True
        elif re.search(rf"(?<![a-z0-9]){re.escape(cand_norm)}(?![a-z0-9])", norm_src):
            matched = True
        elif cand_compact in compact_src:
            matched = True
        if matched and cand_compact not in seen:
            seen.add(cand_compact)
            out.append(sheet_name)
    return out


def build_support_request(text: str, tenant_token: str, mention_names: Optional[List[str]] = None) -> str:
    mp = get_support_map(tenant_token)
    if not mp:
        return "Not specified"
    names = [n.strip() for n in (mention_names or []) if (n or "").strip()]
    depts: List[str] = []
    seen = set()
    for n in names:
        dept = match_dept_from_name(mp, n)
        if not dept:
            continue
        kk = dept.lower()
        if kk in seen:
            continue
        seen.add(kk)
        depts.append(dept)
    fallback_names = extract_names_from_support_map(text, mp)
    for n in fallback_names:
        dept = match_dept_from_name(mp, n)
        if not dept:
            continue
        kk = dept.lower()
        if kk in seen:
            continue
        seen.add(kk)
        depts.append(dept)
    return ", ".join(depts) if depts else "Not specified"


def normalize_support_request_text(text: str) -> str:
    src = (text or "").strip()
    if _text.is_not_specified(src):
        return "Not specified"
    src = re.sub(r"\band\b", ",", src, flags=re.IGNORECASE)
    src = src.replace("，", ",")
    src = src.replace(";", ",")
    parts = [p.strip() for p in src.split(",") if p.strip()]
    out: List[str] = []
    seen = set()
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p.upper() if re.fullmatch(r"[A-Za-z0-9/_ -]+", p) else p)
    return ", ".join(out) if out else "Not specified"
