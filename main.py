import os
import json
import base64
import logging
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from Crypto.Cipher import AES
import lark_oapi as lark

from lark_logic import process_message
from p0_logic import (
    get_incident_group_chat_ids,
    get_tenant_token,
    handle_dm_generate_overview,
    handle_lark_card_action,
    handle_lark_card_action_show_participants_sync,
    card_action_name_from_payload,
    add_meeting_participant,
    remove_meeting_participant,
    end_p0_session_by_meeting_ref,
    bind_live_meeting_id,
    record_vc_external_join_for_meeting_ref,
)

from p0_logic.perf_log import perf_log

try:
    from p0_logic import strip_seeded_host_placeholder_for_open_id
except ImportError:

    def strip_seeded_host_placeholder_for_open_id(_open_id: str) -> None:
        """Older p0_logic on server: no-op. Deploy updated participants.py + __init__.py for full behavior."""
        pass


def _load_dotenv_early() -> None:
    """
    Load env via ``p0_logic.config.apply_env_layers``.

    **Dev:** ``ENV_PROFILE=dev`` merges repo ``.env`` + ``.env.dev`` (secrets once, dev routing in overlay).
    **Prod:** single ``ENV_PATH`` / ``.env`` (unchanged).
    """
    try:
        from p0_logic.config import apply_env_layers

        paths = apply_env_layers()
        if paths:
            logging.getLogger("lark-ops-ai").info("env layers loaded: %s", " → ".join(paths))
    except Exception as e:
        logging.getLogger("lark-ops-ai").warning("env load failed: %s", e)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("lark-ops-ai")

_load_dotenv_early()
_slack_tok = (os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT_USER_OAUTH_TOKEN") or "").strip()
log.info(
    "env loaded: SLACK_BOT_TOKEN %s (len=%s) — if len=0, Slack chat.postMessage will fail",
    "set" if _slack_tok else "MISSING",
    len(_slack_tok),
)

_ig = sorted(get_incident_group_chat_ids())
_ig_disp = ", ".join(
    (f"{x[:10]}…{x[-6:]}" if len(x) > 20 else x) for x in _ig
) or "(none)"
log.info(
    "incident groups: count=%s chat_ids=%s — must match webhook chat_id or messages log "
    "'Ignored message from non-allowed chat_id'",
    len(_ig),
    _ig_disp,
)

from p0_logic.config import get_p0_issue_watch_enabled, get_dm_instruction_open_ids

if get_p0_issue_watch_enabled():
    _iw_dm = get_dm_instruction_open_ids()
    log.info(
        "issue_watch: ENABLED — DM recipients=%s (P0_DM_INSTRUCTION_OPEN_IDS); needs ANTHROPIC_API_KEY",
        len(_iw_dm),
    )
else:
    log.info("issue_watch: disabled (set P0_ISSUE_WATCH_ENABLED=1)")

from p0_logic.config import (
    p0_adjustment_bitable_enabled,
    get_p0_adjustment_bitable_app_token,
    get_p0_adjustment_bitable_table_id,
    get_p0_adjustment_bitable_ops_table_id,
)

if p0_adjustment_bitable_enabled():
    _adj_app = get_p0_adjustment_bitable_app_token()
    _adj_tbl = get_p0_adjustment_bitable_table_id()
    _adj_ops = get_p0_adjustment_bitable_ops_table_id()
    log.info(
        "adjustment_bitable: ENABLED — app_token_tail=%s deploy_table_tail=%s ops_table_tail=%s "
        "(yesterday 12:00 AM SGT → now; posts after Send overview)",
        _adj_app[-8:] if len(_adj_app) > 8 else (_adj_app or "(empty)"),
        _adj_tbl[-8:] if len(_adj_tbl) > 8 else (_adj_tbl or "(empty)"),
        _adj_ops[-8:] if len(_adj_ops) > 8 else (_adj_ops or "(none)"),
    )
else:
    log.info(
        "adjustment_bitable: disabled — set P0_ADJUSTMENT_BITABLE_APP_TOKEN + "
        "P0_ADJUSTMENT_BITABLE_TABLE_ID in .env (and P0_ADJUSTMENT_BITABLE_ENABLED=1)"
    )

from p0_logic.config import get_p0_vc_ring_enabled, get_p0_vc_oauth_redirect_uri

if get_p0_vc_ring_enabled():
    log.info(
        "vc_ring: ENABLED — rings @mentions + P0_MAJOR_CHECK_PERSON_IDS when duty joins VC "
        "(needs duty OAuth: %s)",
        get_p0_vc_oauth_redirect_uri() or "(set P0_VC_OAUTH_REDIRECT_URI)",
    )
else:
    log.info("vc_ring: disabled (set P0_VC_RING_ENABLED=1)")

app = FastAPI()

LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
# Optional second app: severity / minor follow-up DMs (must match config.get_lark_severity_app_credentials).
LARK_SEVERITY_APP_ID = (
    os.getenv("LARK_SEVERITY_APP_ID")
    or os.getenv("LARK_APP_ID_SEVERITY")
    or os.getenv("LARK_APP_ID_2")
    or ""
).strip()
LARK_SEVERITY_APP_SECRET = (
    os.getenv("LARK_SEVERITY_APP_SECRET")
    or os.getenv("LARK_APP_SECRET_SEVERITY")
    or os.getenv("LARK_APP_SECRET_2")
    or ""
).strip()
LARK_ENCRYPT_KEY = os.getenv("LARK_ENCRYPT_KEY", "").strip()

log.info(
    "Lark severity DM bot: %s",
    (
        f"second app (app_id tail …{LARK_SEVERITY_APP_ID[-8:]})"
        if LARK_SEVERITY_APP_ID
        else "NOT configured — severity cards use PRIMARY app (same as overview). "
        "Set LARK_SEVERITY_APP_ID + LARK_SEVERITY_APP_SECRET (or LARK_APP_ID_2 + LARK_APP_SECRET_2)."
    ),
)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

lark_client = (
    lark.Client.builder()
    .app_id(LARK_APP_ID)
    .app_secret(LARK_APP_SECRET)
    .domain("https://open-sg.larksuite.com")
    .build()
)


def decrypt_lark_event(encrypted_b64: str, encrypt_key: str) -> Dict[str, Any]:
    key = hashlib.sha256((encrypt_key or "").encode("utf-8")).digest()
    raw = base64.b64decode(encrypted_b64)
    cipher = AES.new(key, AES.MODE_CBC, iv=raw[:16])
    pt = cipher.decrypt(raw[16:])
    return json.loads(pt[:-pt[-1]].decode("utf-8"))


def _lark_encrypt_keys_for_webhook() -> List[str]:
    """
    Each Lark app has its own **Event Encryption Key**. If two apps share the same Request URL,
    encrypted ``challenge`` payloads must be decrypted with the matching key — a single
    ``LARK_ENCRYPT_KEY`` only works for one app.

    Set ``LARK_ENCRYPT_KEY_2`` (and optionally ``LARK_ENCRYPT_KEY_3``) to the other app's key from
    Developer Console → your app → Event Configuration → Encryption Strategy.
    """
    out: List[str] = []
    for k in (
        LARK_ENCRYPT_KEY,
        (os.getenv("LARK_ENCRYPT_KEY_2") or "").strip(),
        (os.getenv("LARK_ENCRYPT_KEY_3") or "").strip(),
    ):
        if k and k not in out:
            out.append(k)
    return out


def _decrypt_lark_webhook_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt ``encrypt`` if present; try every configured key (multi-app same URL)."""
    if "encrypt" not in body:
        return body
    enc = body.get("encrypt")
    if not isinstance(enc, str) or not enc.strip():
        return body
    keys = _lark_encrypt_keys_for_webhook()
    if not keys:
        raise ValueError("LARK_ENCRYPT_KEY is empty but payload is encrypted")
    last_err: Optional[Exception] = None
    for i, ek in enumerate(keys):
        try:
            plain = decrypt_lark_event(enc, ek)
            if i > 0:
                log.info("lark webhook decrypt succeeded using encrypt key slot %s (not primary)", i)
            return plain
        except Exception as e:
            last_err = e
    raise last_err if last_err else ValueError("decrypt failed")


def _safe_json_loads(s: str) -> Dict[str, Any]:
    try:
        obj = json.loads(s or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _deep_get(d: Any, *path: str) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _tenant_token_for_card_action(payload: Dict[str, Any]) -> str:
    """
    Primary app: overview / Build overview / most cards.
    When ``LARK_SEVERITY_APP_ID`` is set, card actions from the severity bot use that app's token
    (Lark sets ``header.app_id`` on the webhook payload).
    """
    aid = _deep_get(payload, "header", "app_id")
    if (
        isinstance(aid, str)
        and aid.strip()
        and LARK_SEVERITY_APP_ID
        and aid.strip() == LARK_SEVERITY_APP_ID
    ):
        t = get_tenant_token(LARK_SEVERITY_APP_ID, LARK_SEVERITY_APP_SECRET)
        if t:
            return t
        log.error("Severity app tenant token empty; falling back to primary app.")
    return get_tenant_token(LARK_APP_ID, LARK_APP_SECRET)


def _first_non_empty_str(values: List[Any]) -> str:
    for x in values:
        if isinstance(x, str) and x.strip():
            return x.strip()
        if isinstance(x, int):
            return str(x)
    return ""


def _extract_group_chat_display_name(evt: Dict[str, Any]) -> str:
    """If the webhook payload includes a group name, pass it to P0 start (avoids extra API call)."""
    return _first_non_empty_str(
        [
            _deep_get(evt, "message", "chat_name"),
        ]
    )


def _extract_mention_names(msg: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    mentions = msg.get("mentions") or []
    if isinstance(mentions, list):
        for m in mentions:
            if isinstance(m, dict):
                name = (m.get("name") or "").strip()
                if name:
                    out.append(name)

    seen = set()
    uniq: List[str] = []
    for n in out:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    return uniq


def _mention_field_to_open_id(raw: Any) -> str:
    """
    Lark sometimes sends ``mentions[].id`` as a string ``ou_...``; newer payloads may nest
    an object (``id: { open_id: ... }``). Normalize to a single ``ou_`` string or ``""``.
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for k in ("open_id", "id", "user_id"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    return ""


def _extract_mention_open_ids(msg: Dict[str, Any]) -> List[str]:
    """Lark ``mentions[].id`` is typically the user's ``ou_...`` open_id (for @mentions in group text)."""
    out: List[str] = []
    mentions = msg.get("mentions") or []
    if isinstance(mentions, list):
        for m in mentions:
            if not isinstance(m, dict):
                continue
            oid = _mention_field_to_open_id(m.get("id")) or _mention_field_to_open_id(m.get("open_id"))
            if oid.startswith("ou_"):
                out.append(oid)
    seen = set()
    uniq: List[str] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def _find_all_image_keys_anywhere(x: Any) -> List[str]:
    out: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k in ("image_key", "imageKey"):
                iv = v.get(k)
                if isinstance(iv, str) and iv.strip().startswith("img"):
                    out.append(iv.strip())
            for child in v.values():
                walk(child)

        elif isinstance(v, list):
            for child in v:
                walk(child)

        elif isinstance(v, str):
            s = v.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    walk(json.loads(s))
                except Exception:
                    pass

    walk(x)

    seen = set()
    uniq = []
    for k in out:
        if k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return uniq


def _extract_texts_from_post_obj(obj: Any) -> List[str]:
    texts: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            tag = str(v.get("tag") or "").strip().lower()
            if tag == "text":
                txt = v.get("text")
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())

            for key in ("text", "content"):
                val = v.get(key)
                if isinstance(val, str) and val.strip():
                    if not val.strip().startswith("{") and not val.strip().startswith("["):
                        texts.append(val.strip())

            for child in v.values():
                walk(child)

        elif isinstance(v, list):
            for child in v:
                walk(child)

    walk(obj)

    seen = set()
    uniq = []
    for t in texts:
        k = t.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return uniq


def _extract_message_parts(msg: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    msg_type = (msg.get("message_type") or "").strip().lower()
    content = msg.get("content") or ""
    obj = _safe_json_loads(content)

    if msg_type == "text":
        return msg_type, str(obj.get("text") or "").strip(), []

    if msg_type == "image":
        image_key = str(obj.get("image_key") or "").strip()
        return msg_type, "", [image_key] if image_key else []

    if msg_type == "post":
        image_keys = _find_all_image_keys_anywhere(obj)
        texts = _extract_texts_from_post_obj(obj)
        combined_text = "\n".join([t for t in texts if t.strip()]).strip()
        return msg_type, combined_text, image_keys

    return msg_type, "", []


def _detect_callback_type(payload: Dict[str, Any]) -> str:
    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or "").strip()
    if event_type:
        return event_type

    if payload.get("action") or payload.get("open_id"):
        return "card.action.trigger"

    event = payload.get("event") or {}
    if isinstance(event, dict):
        if event.get("action") or event.get("operator") or event.get("open_id"):
            return "card.action.trigger"

    return ""


def _extract_vc_participant_name(evt: Dict[str, Any]) -> str:
    candidates = [
        # VC v1 payloads often nest user under join_user / leave_user (names optional; ids always for lookup).
        _deep_get(evt, "join_user", "name"),
        _deep_get(evt, "join_user", "display_name"),
        _deep_get(evt, "leave_user", "name"),
        _deep_get(evt, "leave_user", "display_name"),
        _deep_get(evt, "user", "name"),
        _deep_get(evt, "user", "display_name"),
        _deep_get(evt, "user", "user_name"),
        _deep_get(evt, "participant", "name"),
        _deep_get(evt, "participant", "display_name"),
        _deep_get(evt, "participant", "user_name"),
        _deep_get(evt, "attendee", "name"),
        _deep_get(evt, "attendee", "display_name"),
        _deep_get(evt, "operator", "name"),
        _deep_get(evt, "operator", "display_name"),
        _deep_get(evt, "host", "name"),
        _deep_get(evt, "host", "display_name"),
    ]
    return _first_non_empty_str(candidates)


def _extract_vc_user_refs(evt: Dict[str, Any]) -> Dict[str, str]:
    # Lark VC events use nested "id": { open_id, user_id, union_id } on join_user, leave_user, operator, etc.
    return {
        "open_id": _first_non_empty_str([
            _deep_get(evt, "join_user", "id", "open_id"),
            _deep_get(evt, "leave_user", "id", "open_id"),
            _deep_get(evt, "operator", "id", "open_id"),
            _deep_get(evt, "user", "open_id"),
            _deep_get(evt, "participant", "open_id"),
            _deep_get(evt, "attendee", "open_id"),
            _deep_get(evt, "operator", "open_id"),
            _deep_get(evt, "host", "open_id"),
            _deep_get(evt, "host_user", "id", "open_id"),
            _deep_get(evt, "meeting", "host_user", "id", "open_id"),
        ]),
        "user_id": _first_non_empty_str([
            _deep_get(evt, "join_user", "id", "user_id"),
            _deep_get(evt, "leave_user", "id", "user_id"),
            _deep_get(evt, "operator", "id", "user_id"),
            _deep_get(evt, "user", "user_id"),
            _deep_get(evt, "participant", "user_id"),
            _deep_get(evt, "attendee", "user_id"),
            _deep_get(evt, "operator", "user_id"),
            _deep_get(evt, "host", "user_id"),
            _deep_get(evt, "host_user", "id", "user_id"),
            _deep_get(evt, "meeting", "host_user", "id", "user_id"),
        ]),
        "union_id": _first_non_empty_str([
            _deep_get(evt, "join_user", "id", "union_id"),
            _deep_get(evt, "leave_user", "id", "union_id"),
            _deep_get(evt, "operator", "id", "union_id"),
            _deep_get(evt, "user", "union_id"),
            _deep_get(evt, "participant", "union_id"),
            _deep_get(evt, "attendee", "union_id"),
            _deep_get(evt, "operator", "union_id"),
            _deep_get(evt, "host", "union_id"),
            _deep_get(evt, "host_user", "id", "union_id"),
            _deep_get(evt, "meeting", "host_user", "id", "union_id"),
        ]),
    }


def _lookup_lark_user_name(tenant_token: str, open_id: str = "", user_id: str = "") -> str:
    headers = {"Authorization": f"Bearer {tenant_token}"}

    tries: List[Tuple[str, str]] = []
    if open_id:
        tries.append(("open_id", open_id))
    if user_id:
        tries.append(("user_id", user_id))

    for id_type, val in tries:
        url = f"https://open-sg.larksuite.com/open-apis/contact/v3/users/{val}"
        try:
            r = requests.get(url, headers=headers, params={"user_id_type": id_type}, timeout=15)
            if r.status_code != 200:
                log.warning(
                    "user lookup failed HTTP=%s id_type=%s val=%s body=%s",
                    r.status_code, id_type, val, (r.text or "")[:300]
                )
                continue

            j = r.json() if r.text else {}
            if j.get("code") != 0:
                log.warning(
                    "user lookup api error id_type=%s val=%s code=%s msg=%s",
                    id_type, val, j.get("code"), j.get("msg")
                )
                continue

            user = (j.get("data") or {}).get("user") or {}
            name = _first_non_empty_str([
                user.get("name"),
                user.get("en_name"),
                user.get("nickname"),
            ])
            if name:
                return name
        except Exception as e:
            log.warning("user lookup exception id_type=%s val=%s err=%s", id_type, val, e)

    return ""


def _extract_vc_meeting_ref(evt: Dict[str, Any]) -> str:
    """
    Lark VC payloads use ``meeting.id`` (long string) for API paths like ``/meetings/{id}/end``.
    Do not rely on ``meeting.meeting_id`` — that key is often absent; we used to fall through to
    ``meeting_no`` only and hit 404 on ``/meetings/{meeting_no}/end``.
    """
    candidates = [
        _deep_get(evt, "meeting", "id"),
        evt.get("meeting_id"),
        evt.get("meeting_no"),
        _deep_get(evt, "meeting", "meeting_id"),
        _deep_get(evt, "meeting", "meeting_no"),
    ]
    return _first_non_empty_str(candidates)


@app.get("/lark/oauth/start")
async def lark_oauth_start(open_id: str = ""):
    """Redirect duty to Lark OAuth (VC ring / recording fan-out user token)."""
    from p0_logic.vc_user_oauth import build_authorize_url

    url = build_authorize_url(open_id)
    if not url:
        return HTMLResponse(
            "VC OAuth is not configured. Set P0_VC_OAUTH_REDIRECT_URI and Lark app credentials.",
            status_code=500,
        )
    log.info("vc oauth start open_id_tail=%s", open_id[-8:] if len(open_id) > 8 else open_id)
    return RedirectResponse(url)


@app.get("/lark/oauth/callback")
async def lark_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """OAuth callback — store duty user token and retry pending VC rings."""
    if error:
        msg = (error_description or error or "unknown").strip()
        log.warning("vc oauth callback error=%s desc=%s", error, (error_description or "")[:200])
        return HTMLResponse(f"VC OAuth failed: {msg}", status_code=400)
    from p0_logic.vc_user_oauth import exchange_code_for_tokens
    from p0_logic.vc_ring import maybe_retry_pending_vc_ring_for_declarer

    ok, oid, detail = exchange_code_for_tokens(code, open_id_hint=state)
    if not ok or not oid:
        log.warning("vc oauth callback exchange failed hint_tail=%s detail=%s", state[-8:] if state else "", detail[:200])
        return HTMLResponse(f"VC OAuth failed: {detail or 'token exchange failed'}", status_code=400)
    tenant_token = get_tenant_token(LARK_APP_ID, LARK_APP_SECRET)
    if tenant_token:
        n = maybe_retry_pending_vc_ring_for_declarer(oid, tenant_token)
        log.info("vc oauth callback ok open_id_tail=%s retried_sessions=%s", oid[-8:], n)
    return HTMLResponse(
        "<html><body><h3>VC OAuth authorized</h3>"
        "<p>You can close this tab. When you join the P0 VC, configured users will be rung in.</p>"
        "</body></html>"
    )


@app.post("/lark/webhook")
async def lark_webhook(req: Request, background: BackgroundTasks):
    try:
        body = await req.json()
    except Exception:
        raw = await req.body()
        log.error("Webhook received non-JSON body head=%s", (raw or b"")[:200])
        return {"code": 400, "msg": "bad request"}

    if "encrypt" in body:
        try:
            body = _decrypt_lark_webhook_body(body)
        except Exception as e:
            log.error(
                "Failed decrypting lark event (add LARK_ENCRYPT_KEY_2 for a second app sharing this URL): %s",
                e,
                exc_info=True,
            )
            return {"code": 400, "msg": "decrypt failed"}

    log.info("RAW webhook body=%s", json.dumps(body, ensure_ascii=False)[:4000])

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    callback_type = _detect_callback_type(body)
    log.info("Detected webhook type=%s", callback_type or "unknown")

    # card.action.trigger: respond with toast in the same HTTP response (Lark docs).
    # Sending a DM via post_text_to_open_id in a background task adds a second server→Lark round trip (~300ms+).
    if callback_type == "card.action.trigger" and card_action_name_from_payload(body) == "show_participants":
        t_sp = time.perf_counter()
        tenant_token = get_tenant_token(LARK_APP_ID, LARK_APP_SECRET)
        if not tenant_token:
            log.error("No tenant token; cannot process show_participants.")
            return {"code": 0}
        resp = handle_lark_card_action_show_participants_sync(body, tenant_token)
        perf_log("lark_webhook card.action.trigger show_participants sync", t_sp)
        return {"code": 0, **resp}

    if callback_type == "card.action.trigger":
        background.add_task(_process_lark_payload, body, callback_type)
        return {"code": 0}

    background.add_task(_process_lark_payload, body, callback_type)
    return {"code": 0}


def _process_lark_payload(payload: Dict[str, Any], callback_type: str = "") -> None:
    try:
        evt = payload.get("event", {}) or {}
        event_type = (callback_type or "").strip()

        if event_type == "card.action.trigger":
            t_card = time.perf_counter()
            tenant_token = _tenant_token_for_card_action(payload)
            if not tenant_token:
                log.error("No tenant token; cannot process.")
                return
            log.info("card.action.trigger payload=%s", json.dumps(payload, ensure_ascii=False)[:4000])
            handle_lark_card_action(payload, tenant_token)
            perf_log("lark_webhook card.action.trigger token+handler", t_card)
            return

        tenant_token = get_tenant_token(LARK_APP_ID, LARK_APP_SECRET)
        if not tenant_token:
            log.error("No tenant token; cannot process.")
            return

        if event_type.startswith("vc."):
            log.info("VC EVENT type=%s raw=%s", event_type, json.dumps(evt, ensure_ascii=False)[:4000])

        if event_type == "vc.meeting.join_meeting_v1":
            meeting_ref = _extract_vc_meeting_ref(evt)
            participant_name = _extract_vc_participant_name(evt)
            refs = _extract_vc_user_refs(evt)

            if not participant_name:
                participant_name = _lookup_lark_user_name(
                    tenant_token=tenant_token,
                    open_id=refs.get("open_id", ""),
                    user_id=refs.get("user_id", ""),
                )

            log.info(
                "vc.meeting.join_meeting_v1 meeting_ref=%s participant_name=%s refs=%s raw=%s",
                meeting_ref,
                participant_name,
                refs,
                json.dumps(evt, ensure_ascii=False)[:4000],
            )

            if meeting_ref:
                bind_live_meeting_id(meeting_ref)

            oid = (refs.get("open_id") or "").strip()
            if meeting_ref:
                record_vc_external_join_for_meeting_ref(meeting_ref, oid)
            if oid:
                strip_seeded_host_placeholder_for_open_id(oid)

            if participant_name:
                add_meeting_participant(participant_name)
            else:
                log.warning("vc join event received but participant name is empty after fallback lookup")
            joiner_uid = (refs.get("user_id") or "").strip()
            if meeting_ref and (oid or joiner_uid):
                try:
                    from p0_logic.vc_ring import maybe_ring_on_vc_join

                    maybe_ring_on_vc_join(
                        meeting_ref,
                        oid,
                        tenant_token,
                        joiner_user_id=joiner_uid,
                    )
                except Exception as e_ring:
                    log.warning("vc_ring on join failed: %s", e_ring)
            try:
                from p0_logic.issue_watch_declare import maybe_prompt_major_check_person_joined

                maybe_prompt_major_check_person_joined(
                    meeting_ref=meeting_ref or "",
                    tenant_token=tenant_token,
                    joiner_open_id=oid,
                    joiner_user_id=joiner_uid,
                    participant_name=participant_name or "",
                )
            except Exception as e_cp:
                log.warning("major check-person join prompt hook failed: %s", e_cp)
            return

        if event_type == "vc.meeting.leave_meeting_v1":
            meeting_ref = _extract_vc_meeting_ref(evt)
            participant_name = _extract_vc_participant_name(evt)
            refs = _extract_vc_user_refs(evt)

            if not participant_name:
                participant_name = _lookup_lark_user_name(
                    tenant_token=tenant_token,
                    open_id=refs.get("open_id", ""),
                    user_id=refs.get("user_id", ""),
                )

            log.info(
                "vc.meeting.leave_meeting_v1 meeting_ref=%s participant_name=%s refs=%s raw=%s",
                meeting_ref,
                participant_name,
                refs,
                json.dumps(evt, ensure_ascii=False)[:4000],
            )

            if participant_name:
                remove_meeting_participant(participant_name)
            else:
                log.warning("vc leave event received but participant name is empty after fallback lookup")
            return

        if event_type == "vc.meeting.meeting_ended_v1":
            meeting_ref = _extract_vc_meeting_ref(evt)
            meeting_no_fb = str(_deep_get(evt, "meeting", "meeting_no") or "").strip()
            log.info("vc.meeting.meeting_ended_v1 meeting_ref=%s meeting_no=%s", meeting_ref, meeting_no_fb)
            end_p0_session_by_meeting_ref(
                meeting_ref, tenant_token, meeting_no_fallback=meeting_no_fb
            )
            from p0_logic.vc_recording_fanout import schedule_recording_fanout_poll_after_meeting_end

            schedule_recording_fanout_poll_after_meeting_end(tenant_token, evt)
            return

        if event_type == "vc.meeting.recording_ready_v1":
            from p0_logic.vc_recording_fanout import handle_vc_recording_ready_fanout

            handle_vc_recording_ready_fanout(evt, tenant_token)
            return

        msg = evt.get("message", {}) or {}
        if not msg.get("content"):
            return

        chat_id = (msg.get("chat_id") or "").strip()
        chat_type = (msg.get("chat_type") or "").strip().lower()
        message_id = (msg.get("message_id") or "").strip()
        message_create_time = (msg.get("create_time") or "").strip()
        parent_id = (msg.get("parent_id") or "").strip()
        root_id = (msg.get("root_id") or "").strip()

        sender_open_id = (
            (((evt.get("sender") or {}).get("sender_id") or {}).get("open_id") or "").strip()
        )
        # Tenant user_id (e.g. SNT0006) — stable across Lark apps; open_id is app-scoped (cross-app DM).
        sender_lark_user_id = (
            (((evt.get("sender") or {}).get("sender_id") or {}).get("user_id") or "").strip()
        )

        msg_type, text, image_keys = _extract_message_parts(msg)
        mention_names = _extract_mention_names(msg)
        mention_open_ids = _extract_mention_open_ids(msg)

        if chat_type == "p2p":
            log.info(
                "DM incoming msg_type=%s sender=%s has_text=%s image_count=%s message_id=%s mentions=%s",
                msg_type,
                sender_open_id,
                bool(text),
                len(image_keys),
                message_id,
                mention_names,
            )

            if text:
                t_dm = time.perf_counter()
                handle_dm_generate_overview(
                    sender_open_id=sender_open_id,
                    tenant_token=tenant_token,
                    text=text,
                    image_key=None,
                    mention_names=mention_names,
                    message_id=message_id,
                )
                perf_log("lark_webhook dm_generate_overview text", t_dm)

            for image_key in image_keys:
                if not image_key:
                    continue
                t_dm = time.perf_counter()
                handle_dm_generate_overview(
                    sender_open_id=sender_open_id,
                    tenant_token=tenant_token,
                    text=None,
                    image_key=image_key,
                    mention_names=mention_names,
                    message_id=message_id,
                )
                perf_log("lark_webhook dm_generate_overview image", t_dm)
            return

        # Group chat: Lark may send "text" or "post" (rich). Only "text" was handled before,
        # so "p0" in a post-style message never reached process_message / INCIDENT_GROUP_ID check.
        if not text.strip():
            return
        if msg_type not in ("text", "post"):
            log.info("Skipping non-text/post message for group routing msg_type=%s chat_id=%s", msg_type, chat_id)
            return

        process_message(
            text,
            chat_id,
            sender_open_id,
            tenant_token,
            lark_client,
            GROQ_API_KEY,
            message_type=msg_type,
            message_id=message_id,
            message_create_time=message_create_time,
            parent_id=parent_id,
            root_id=root_id,
            image_key=image_keys[0] if image_keys else "",
            mention_names=mention_names,
            chat_type=chat_type,
            source_chat_name=_extract_group_chat_display_name(evt),
            sender_lark_user_id=sender_lark_user_id,
            mention_open_ids=mention_open_ids,
        )

    except Exception as e:
        log.error("Background Process Error: %s", e, exc_info=True)
