"""
Lark/Feishu API: tenant token, IM messages, VC reserves/meetings, Sheets, image download.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from . import config as _config
from .perf_log import perf_log

log = logging.getLogger("lark-ops-ai")

# Thread-local Session: keep-alive + pool per host. Safe under ThreadPoolExecutor (each thread has its own pool).
_HTTP_TLS = threading.local()


def _lark_http() -> requests.Session:
    s = getattr(_HTTP_TLS, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _HTTP_TLS.session = s
    return s

LARK_BASE = _config.LARK_BASE
VC_BASES = _config.VC_BASES
IM_BASES = _config.IM_BASES
SHEETS_BASES = _config.SHEETS_BASES
SHEETS_V2_BASES = _config.SHEETS_V2_BASES
MEETING_TOPIC = _config.MEETING_TOPIC

_TOKEN_CACHE: Dict[str, Any] = {"token": "", "exp": 0}
_TOKEN_LOCK = __import__("threading").Lock()


def _timeout_kw() -> Dict[str, Any]:
    return _config.timeout_kw()


def get_tenant_token(app_id: str, app_secret: str) -> str:
    t0 = time.perf_counter()
    now = int(time.time())
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["exp"]:
            perf_log("tenant_token cache_hit", t0)
            return _TOKEN_CACHE["token"]
    t_fetch = time.perf_counter()
    try:
        url = f"{LARK_BASE}/auth/v3/tenant_access_token/internal"
        resp = _lark_http().post(url, json={"app_id": app_id, "app_secret": app_secret}, **_timeout_kw())
        data = resp.json() if resp.text else {}
        if data.get("code") != 0:
            log.error("Tenant token API error: %s", data.get("msg"))
            return ""
        tok = (data.get("tenant_access_token") or "").strip()
        if not tok:
            return ""
        exp = int(data.get("expire") or 3600)
        with _TOKEN_LOCK:
            _TOKEN_CACHE["token"] = tok
            _TOKEN_CACHE["exp"] = now + exp - 120
        return tok
    except Exception as e:
        log.error("Tenant token fetch error: %s", e)
        return ""
    finally:
        perf_log("tenant_token fetch", t_fetch)


def post_text_to_chat(chat_id: str, token: str, text: str) -> Tuple[int, str]:
    url = f"{LARK_BASE}/im/v1/messages?receive_id_type=chat_id"
    payload = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    r = _lark_http().post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, **_timeout_kw())
    return r.status_code, (r.text or "")


def parse_im_message_id_from_response(body: str) -> str:
    """Extract ``message_id`` (om_...) from im/v1/messages create response JSON."""
    try:
        j = json.loads(body or "{}")
        if j.get("code") != 0:
            return ""
        data = j.get("data") or {}
        return str(data.get("message_id") or "").strip()
    except Exception:
        return ""


def post_card_to_chat(chat_id: str, token: str, card: Dict[str, Any]) -> Tuple[int, str, str]:
    t0 = time.perf_counter()
    try:
        url = f"{LARK_BASE}/im/v1/messages?receive_id_type=chat_id"
        payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        r = _lark_http().post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, **_timeout_kw())
        txt = r.text or ""
        return r.status_code, txt, parse_im_message_id_from_response(txt)
    finally:
        perf_log("lark post_card_to_chat", t0)


def recall_im_message(token: str, message_id: str) -> Tuple[int, str]:
    """
    Recall (delete) a message the bot sent. Same as user "recall" in chat.
    Requires scope e.g. ``im:message`` / ``im:message:recall`` per tenant policy.
    """
    message_id = (message_id or "").strip()
    if not message_id:
        return 400, "message_id empty"
    t0 = time.perf_counter()
    try:
        url = f"{LARK_BASE}/im/v1/messages/{quote(message_id, safe='')}"
        r = _lark_http().delete(url, headers={"Authorization": f"Bearer {token}"}, **_timeout_kw())
        return r.status_code, (r.text or "")
    finally:
        perf_log("lark recall_im_message", t0)


def patch_interactive_card(token: str, message_id: str, card: Dict[str, Any]) -> Tuple[int, str]:
    """
    Replace an interactive (card) message body. Card must have been sent with
    ``config.update_multi: true`` or the API returns an error.
    """
    message_id = (message_id or "").strip()
    if not message_id:
        return 400, "message_id empty"
    t0 = time.perf_counter()
    try:
        url = f"{LARK_BASE}/im/v1/messages/{quote(message_id, safe='')}"
        payload = {"content": json.dumps(card, ensure_ascii=False)}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        r = _lark_http().patch(url, headers=headers, json=payload, **_timeout_kw())
        return r.status_code, (r.text or "")
    finally:
        perf_log("lark patch_interactive_card", t0)


def post_text_to_open_id(open_id: str, token: str, text: str) -> Tuple[int, str]:
    url = f"{LARK_BASE}/im/v1/messages?receive_id_type=open_id"
    payload = {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    r = _lark_http().post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, **_timeout_kw())
    return r.status_code, (r.text or "")


def post_card_to_open_id(open_id: str, token: str, card: Dict[str, Any]) -> Tuple[int, str, str]:
    t0 = time.perf_counter()
    try:
        url = f"{LARK_BASE}/im/v1/messages?receive_id_type=open_id"
        payload = {"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        r = _lark_http().post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, **_timeout_kw())
        txt = r.text or ""
        return r.status_code, txt, parse_im_message_id_from_response(txt)
    finally:
        perf_log("lark post_card_to_open_id", t0)


def get_group_chat_name(chat_id: str, token: str) -> str:
    """Resolve a group chat's display name (for card titles)."""
    chat_id = (chat_id or "").strip()
    if not chat_id or not token:
        return ""
    url = f"{LARK_BASE}/im/v1/chats/{quote(chat_id, safe='')}"
    try:
        r = _lark_http().get(url, headers={"Authorization": f"Bearer {token}"}, **_timeout_kw())
        j, _ = safe_json(r)
        if j.get("code") != 0:
            log.warning("get_group_chat_name chat_id=%s code=%s msg=%s", chat_id, j.get("code"), j.get("msg"))
            return ""
        data = j.get("data") or {}
        return str(data.get("name") or "").strip()
    except Exception as e:
        log.warning("get_group_chat_name chat_id=%s err=%s", chat_id, e)
        return ""


def safe_json(resp: requests.Response) -> Tuple[Dict[str, Any], str]:
    txt = resp.text or ""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype or txt.lstrip().startswith("{"):
        try:
            j = resp.json() if txt else {}
            return (j if isinstance(j, dict) else {}), ""
        except Exception as e:
            return {}, f"json_parse_error={e} head={txt[:200]}"
    return {}, f"non_json content_type={ctype} head={txt[:200]}"


def delete_vc_reserve(token: str, reserve_id: str) -> bool:
    reserve_id = (reserve_id or "").strip()
    if not reserve_id:
        return False
    headers = {"Authorization": f"Bearer {token}"}
    last_err = ""
    for base in VC_BASES:
        url = f"{base}/vc/v1/reserves/{quote(reserve_id, safe='')}"
        try:
            r = _lark_http().delete(url, headers=headers, **_timeout_kw())
            log.info("VC delete reserve try: %s -> %s", url, r.status_code)
            if r.status_code != 200:
                last_err = f"HTTP={r.status_code} body={(r.text or '')[:200]}"
                continue
            j = r.json() if r.text else {}
            if isinstance(j, dict) and j.get("code") == 0:
                return True
            last_err = f"code={j.get('code')} msg={j.get('msg')}"
        except Exception as e:
            last_err = str(e)
    log.error("Delete VC reserve failed reserve_id=%s err=%s", reserve_id, last_err)
    return False


def end_vc_meeting(token: str, meeting_id: str) -> bool:
    """
    ``POST /vc/v1/meetings/{meeting_id}/end``.

    ``meeting_id`` may be the join-event id **or** the numeric ``meeting_no`` from reserve,
    depending on tenant — callers try both (see ``end_p0_session``).
    """
    meeting_id = (meeting_id or "").strip()
    if not meeting_id:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last_err = ""
    for base in VC_BASES:
        url = f"{base}/vc/v1/meetings/{quote(meeting_id, safe='')}/end"
        try:
            r = _lark_http().post(url, headers=headers, json={}, **_timeout_kw())
            log.info("VC end meeting try: %s -> %s", url, r.status_code)
            if r.status_code != 200:
                last_err = f"HTTP={r.status_code} body={(r.text or '')[:200]}"
                continue
            j = r.json() if r.text else {}
            if isinstance(j, dict) and j.get("code") == 0:
                return True
            last_err = f"code={j.get('code')} msg={j.get('msg')}"
        except Exception as e:
            last_err = str(e)
    log.error("End VC meeting failed meeting_id=%s err=%s", meeting_id, last_err)
    return False


def get_primary_owner_id() -> str:
    from . import config as _config
    ids = _config.get_owner_ids()
    return ids[0] if ids else ""


def create_vc_reserve(token: str, meeting_topic: str = "") -> Dict[str, str]:
    import time as _time
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    now_sec = int(_time.time())
    end_sec = now_sec + _config.get_vc_reserve_end_offset_sec()
    owner_id = get_primary_owner_id()
    if not owner_id:
        log.error("No owner id configured.")
        return {"link": "", "reserve_id": "", "meeting_no": "", "app_link": ""}
    topic = (meeting_topic or "").strip() or MEETING_TOPIC
    payload = {
        "end_time": str(end_sec),
        "owner_id": owner_id,
        "meeting_settings": {
            "topic": topic,
            "meeting_initial_type": 1,
            "auto_record": True,
        },
    }
    last_err = ""
    for base in VC_BASES:
        url = f"{base}/vc/v1/reserves/apply?user_id_type=open_id"
        try:
            r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
            log.info("VC Create(reserves) try: %s -> %s", url, r.status_code)
            if r.status_code != 200:
                last_err = (r.text or "")[:200]
                continue
            j = r.json() if r.text else {}
            if not isinstance(j, dict) or j.get("code") != 0:
                last_err = (j.get("msg") if isinstance(j, dict) else "invalid json") or ""
                continue
            reserve = (j.get("data") or {}).get("reserve") or {}
            link = (reserve.get("url") or "").strip()
            app_link = (reserve.get("app_link") or "").strip()
            reserve_id = str(reserve.get("id") or "").strip()
            meeting_no = str(reserve.get("meeting_no") or "").strip()
            if link:
                return {
                    "link": link,
                    "reserve_id": reserve_id,
                    "meeting_no": meeting_no,
                    "meeting_id": "",
                    "app_link": app_link,
                }
        except Exception as e:
            last_err = str(e)
    log.error("VC reserve/apply failed. Last error: %s", last_err)
    return {"link": "", "reserve_id": "", "meeting_no": "", "meeting_id": "", "app_link": ""}


def read_sheets_values_batch(tenant_token: str, spreadsheet_token: str, range_str: str) -> Tuple[List[List[Any]], str]:
    headers = {"Authorization": f"Bearer {tenant_token}"}
    last = ""
    for base in SHEETS_V2_BASES:
        url = f"{base}/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_get"
        log.info("SUPPORT values_batch_get v2 url=%s ranges=%s", url, range_str)
        try:
            r = _lark_http().get(url, headers=headers, params={"ranges": range_str}, **_timeout_kw())
        except Exception as e:
            last = f"{url} exception={e}"
            continue
        j, jerr = safe_json(r)
        if r.status_code in (403, 404):
            last = f"{url} HTTP={r.status_code}"
            continue
        if r.status_code != 200:
            last = f"{url} HTTP={r.status_code} {jerr}"
            continue
        if j.get("code") != 0:
            last = f"{url} API error code={j.get('code')} msg={j.get('msg')} {jerr}"
            continue
        data = j.get("data") or {}
        vrs = data.get("valueRanges") or data.get("value_ranges") or []
        if isinstance(vrs, list) and vrs:
            vr0 = vrs[0] if isinstance(vrs[0], dict) else {}
            values = vr0.get("values")
            if isinstance(values, list):
                return values, ""
        last = f"{url} code=0 but empty valueRanges"
    return [], last or "values_batch_get failed"


def download_image_bytes(tenant_token: str, image_key: str) -> bytes:
    image_key = (image_key or "").strip()
    if not image_key:
        raise ValueError("image_key empty")
    safe_key = quote(image_key, safe="")
    headers = {"Authorization": f"Bearer {tenant_token}", "Accept": "*/*"}
    attempts = [({"type": "message"}, "type=message"), ({"type": "origin"}, "type=origin")]
    last_status = None
    last_head = ""
    last_url = ""
    for base in IM_BASES:
        url = f"{base}/im/v1/images/{safe_key}"
        for params, label in attempts:
            last_url = url
            try:
                r = _lark_http().get(url, headers=headers, params=params, **_timeout_kw())
            except Exception as e:
                last_status = None
                last_head = f"exception={e}"
                log.warning("Image download try %s %s exception=%s", url, label, e)
                continue
            last_status = r.status_code
            ctype = (r.headers.get("content-type") or "").lower()
            if "application/json" in ctype or (r.text or "").lstrip().startswith("{"):
                head = (r.text or "")[:220]
                last_head = head
                log.warning("Image download try %s %s got JSON HTTP=%s head=%s", url, label, r.status_code, head)
                continue
            if r.status_code == 200 and r.content:
                return r.content
            head = (r.text or "")[:220]
            last_head = head
            log.warning("Image download try %s %s failed HTTP=%s ctype=%s head=%s", url, label, r.status_code, ctype, head)
    raise RuntimeError(f"Image download failed: url={last_url} HTTP={last_status} head={last_head}")


def download_message_resource_bytes(tenant_token: str, message_id: str, file_key: str) -> bytes:
    message_id = (message_id or "").strip()
    file_key = (file_key or "").strip()
    if not message_id:
        raise ValueError("message_id empty")
    if not file_key:
        raise ValueError("file_key empty")
    safe_mid = quote(message_id, safe="")
    safe_key = quote(file_key, safe="")
    headers = {"Authorization": f"Bearer {tenant_token}", "Accept": "*/*"}
    last_status = None
    last_head = ""
    last_url = ""
    for base in IM_BASES:
        url = f"{base}/im/v1/messages/{safe_mid}/resources/{safe_key}"
        last_url = url
        try:
            r = _lark_http().get(url, headers=headers, params={"type": "image"}, **_timeout_kw())
        except Exception as e:
            last_status = None
            last_head = f"exception={e}"
            log.warning("Resource download exception url=%s err=%s", url, e)
            continue
        last_status = r.status_code
        ctype = (r.headers.get("content-type") or "").lower()
        if "application/json" in ctype or (r.text or "").lstrip().startswith("{"):
            head = (r.text or "")[:220]
            last_head = head
            log.warning("Resource download got JSON url=%s HTTP=%s head=%s", url, r.status_code, head)
            continue
        if r.status_code == 200 and r.content:
            return r.content
        head = (r.text or "")[:220]
        last_head = head
        log.warning("Resource download failed url=%s HTTP=%s ctype=%s head=%s", url, r.status_code, ctype, head)
    raise RuntimeError(f"Resource download failed: url={last_url} HTTP={last_status} head={last_head}")


def lookup_user_name_by_open_id(tenant_token: str, open_id: str) -> str:
    open_id = (open_id or "").strip()
    if not tenant_token or not open_id:
        return ""
    headers = {"Authorization": f"Bearer {tenant_token}"}
    url = f"{LARK_BASE}/contact/v3/users/{quote(open_id, safe='')}"
    try:
        r = _lark_http().get(url, headers=headers, params={"user_id_type": "open_id"}, **_timeout_kw())
        if r.status_code != 200:
            log.warning("lookup host name failed HTTP=%s body=%s", r.status_code, (r.text or "")[:300])
            return ""
        j = r.json() if r.text else {}
        if j.get("code") != 0:
            log.warning("lookup host name api error code=%s msg=%s", j.get("code"), j.get("msg"))
            return ""
        user = (j.get("data") or {}).get("user") or {}
        for k in ("name", "en_name", "nickname"):
            v = str(user.get(k) or "").strip()
            if v:
                return v
    except Exception as e:
        log.warning("lookup host name exception open_id=%s err=%s", open_id, e)
    return ""
