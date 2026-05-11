"""
P0 logic configuration: env reload, timeouts, API bases, regex patterns, timing constants.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

import logging

log = logging.getLogger("lark-ops-ai")

try:
    from dotenv import dotenv_values
except Exception:
    dotenv_values = None

_REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_env_file_path() -> str:
    """
    Same rule as ``main._load_dotenv_early``: ``ENV_PATH`` if set, else repo ``.env`` if it exists,
    else legacy default. Avoids ``reload_env_runtime`` reading a *different* file than startup and
    wiping keys with empty placeholders.
    """
    raw = (os.getenv("ENV_PATH") or "").strip()
    if raw:
        return raw
    p = _REPO_ROOT / ".env"
    if p.is_file():
        return str(p)
    return "/home/ubuntu/lark-ops-ai/.env"


ENV_PATH = resolve_env_file_path()


def reload_env_runtime() -> None:
    if not dotenv_values:
        return
    try:
        path = resolve_env_file_path()
        values = dotenv_values(path)
        for k, v in (values or {}).items():
            if v is None:
                continue
            sv = str(v).strip()
            if not sv:
                continue  # do not overwrite with blank (common in a second .env file)
            os.environ[k] = sv
    except Exception as e:
        log.error("Failed to reload .env: %s", e)


# Default incident group if env unset (matches historical lark_logic default).
_DEFAULT_INCIDENT_GROUP_FALLBACK = "oc_f4e833c6744e55eb50dfcd8830fa913e"


def get_incident_group_chat_ids() -> FrozenSet[str]:
    """
    All group chat ids (oc_...) where P0/P1 keywords are handled.

    Set either:
    - ``INCIDENT_GROUP_IDS=oc_a,oc_b`` (preferred for multiple), or
    - ``INCIDENT_GROUP_ID=oc_a`` or ``INCIDENT_GROUP_ID=oc_a,oc_b`` (comma-separated).
    """
    reload_env_runtime()
    raw = (os.getenv("INCIDENT_GROUP_IDS") or "").strip()
    if not raw:
        raw = (os.getenv("INCIDENT_GROUP_ID") or "").strip()
    if not raw:
        return frozenset({_DEFAULT_INCIDENT_GROUP_FALLBACK})
    out = frozenset(x.strip() for x in raw.split(",") if x.strip())
    for x in out:
        if not x.startswith("oc_"):
            log.warning(
                "INCIDENT_GROUP_IDS has invalid entry %r — expect full Lark group ids (oc_...). "
                "Check for duplicate INCIDENT_GROUP_IDS / INCIDENT_GROUP_ID lines or a truncated value.",
                x,
            )
    return out


def get_overview_post_chat_id() -> str:
    """
    If set, \"Send overview\" posts to this oc_ chat; otherwise posts to the group
    where the session started (per-chat_id session).

    Per-detection-group routing takes precedence when ``INCIDENT_OVERVIEW_TARGET_MAP`` is set
    (see ``get_overview_target_chat_id_for_source_incident``).
    """
    reload_env_runtime()
    return (os.getenv("OVERVIEW_TARGET_GROUP_CHAT_ID") or os.getenv("P0_OVERVIEW_POST_CHAT_ID") or "").strip()


def _parse_incident_overview_target_map(raw: str) -> Dict[str, str]:
    """
    Comma-separated ``oc_detection=oc_prompt`` pairs (both sides must be ``oc_...`` group chat ids).
    """
    out: Dict[str, str] = {}
    if not (raw or "").strip():
        return out
    for segment in raw.split(","):
        segment = segment.strip()
        if "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        key, val = key.strip(), val.strip()
        if key.startswith("oc_") and val.startswith("oc_"):
            # Reject "=oc_" placeholders (copy-paste cut off); full Lark group ids are longer.
            if len(key) < 12 or len(val) < 12:
                log.warning(
                    "INCIDENT_OVERVIEW_TARGET_MAP: skip incomplete pair %r "
                    "(each side must be a full oc_... group chat id, e.g. oc_8c1c...=oc_f4e8...)",
                    segment,
                )
                continue
            out[key] = val
    return out


def get_incident_overview_target_map() -> Dict[str, str]:
    """Parsed ``INCIDENT_OVERVIEW_TARGET_MAP`` env (detection ``oc_`` -> mirror ``oc_`` for meeting cards when split)."""
    reload_env_runtime()
    return _parse_incident_overview_target_map(os.getenv("INCIDENT_OVERVIEW_TARGET_MAP") or "")


def get_incident_overview_send_map() -> Dict[str, str]:
    """
    Parsed ``INCIDENT_OVERVIEW_SEND_MAP``: ``source_incident_oc=overview_destination_oc``.

    Used **only** for the final **Send overview** Lark post (see ``handlers.send_preview``).
    Meeting invite / P1 cards follow ``get_session_meeting_card_post_chat_id`` instead.
    """
    reload_env_runtime()
    return _parse_incident_overview_target_map(os.getenv("INCIDENT_OVERVIEW_SEND_MAP") or "")


def get_incident_overview_send_chat_id(source_incident_chat_id: str) -> str:
    """Resolved overview **post** group for ``source_incident`` from ``INCIDENT_OVERVIEW_SEND_MAP``; empty if unmapped."""
    sid = (source_incident_chat_id or "").strip()
    if not sid:
        return ""
    m = get_incident_overview_send_map()
    if sid in m:
        return m[sid]
    return ""


def get_source_incident_chat_id_for_mirror_target(mirror_chat_id: str) -> str:
    """
    Reverse of ``INCIDENT_OVERVIEW_TARGET_MAP``: given prompt / mirror ``oc_``, return the detection ``oc_``
    key when that mapping is **unique**. Used when DM preview lost ``source_incident_chat_id`` but
    ``target_chat`` (session prompt) is known — so **Send overview** can still post to detection when
    ``P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT=1``.
    """
    mc = (mirror_chat_id or "").strip()
    if not mc.startswith("oc_"):
        return ""
    m = get_incident_overview_target_map()
    hits = [det for det, pr in m.items() if pr == mc]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        log.warning(
            "INCIDENT_OVERVIEW_TARGET_MAP: multiple detection chats map to the same prompt %s — "
            "cannot infer unique overview source",
            mc[:28],
        )
    return ""


def get_p0_overview_post_to_source_incident_chat() -> bool:
    """
    ``P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT`` — if ``1``, **Send overview** posts to the **detection**
    group (where P0 was declared: ``source_incident_chat_id``), not the prompt / mirror session ``target_chat``.

    Use with ``P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT=0`` and ``INCIDENT_OVERVIEW_TARGET_MAP`` so meeting
    cards, ended/cancelled, and cooldown bot text stay in the **prompt** group.

    If ``INCIDENT_OVERVIEW_SEND_MAP`` has an entry for this incident ``oc_``, that map still **wins** (broadcast override).
    """
    reload_env_runtime()
    v = (os.getenv("P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_overview_detection_fanout_chat_ids() -> List[str]:
    """
    Extra Lark group chat ids (``oc_...``) that receive a **duplicate** overview card when the primary
    post lands in a **detection** group (see ``is_overview_post_destination_detection``).

    Comma-separated ``OVERVIEW_DETECTION_FANOUT_CHAT_IDS`` (alias ``P0_OVERVIEW_DETECTION_FANOUT_CHAT_IDS``).
    Bot must be a member of each group. Empty = disabled.
    """
    reload_env_runtime()
    raw = (
        os.getenv("OVERVIEW_DETECTION_FANOUT_CHAT_IDS") or os.getenv("P0_OVERVIEW_DETECTION_FANOUT_CHAT_IDS") or ""
    ).strip()
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p.startswith("oc_") or len(p) < 12:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def is_overview_post_destination_detection(
    dest_chat_id: str, source_incident_chat_id: str, session_target_chat: str
) -> bool:
    """
    True when ``dest_chat_id`` is a **detection** room (source-side incident group), i.e. the same
    overview routing users would call “post to detection”.

    - If ``INCIDENT_OVERVIEW_TARGET_MAP`` is set: ``dest`` is a **key** (detection ``oc_``).
    - Else: ``dest`` equals the resolved source incident ``oc_`` and that id is in ``INCIDENT_GROUP_IDS``.
    """
    d = (dest_chat_id or "").strip()
    if not d.startswith("oc_"):
        return False
    sid = (source_incident_chat_id or "").strip()
    tc = (session_target_chat or "").strip()
    if not sid.startswith("oc_") and tc.startswith("oc_"):
        sid = get_source_incident_chat_id_for_mirror_target(tc) or sid
    m = get_incident_overview_target_map()
    if m:
        return d in m
    if sid.startswith("oc_") and d == sid and sid in get_incident_group_chat_ids():
        return True
    # DM ``create overview …`` uses a non-oc_ placeholder source; final dest equals ``target_chat``.
    if (
        (not sid.startswith("oc_"))
        and d == tc
        and tc.startswith("oc_")
        and tc in get_incident_group_chat_ids()
    ):
        return True
    return False


def get_vc_recording_fanout_chat_ids() -> List[str]:
    """
    When Lark emits **vc.meeting.recording_ready_v1** (Open API reserves only), post the recording link
    to these ``oc_`` group chats. Comma-separated ``VC_RECORDING_FANOUT_CHAT_IDS`` (alias
    ``P0_VC_RECORDING_FANOUT_CHAT_IDS``). Bot must be in each group. Empty = disabled.
    """
    reload_env_runtime()
    raw = (
        os.getenv("VC_RECORDING_FANOUT_CHAT_IDS") or os.getenv("P0_VC_RECORDING_FANOUT_CHAT_IDS") or ""
    ).strip()
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p.startswith("oc_") or len(p) < 12:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def get_vc_recording_fanout_topic_substring_filter() -> str:
    """
    If non-empty, only forward **recording_ready** when the meeting topic contains this substring
    (case-insensitive). Reduces noise if the same app creates other API meetings.

    Env: ``VC_RECORDING_FANOUT_TOPIC_SUBSTRING`` (e.g. ``Video meeting`` matching ``VIDEO_MEETING_TOPIC_PREFIX``).
    """
    reload_env_runtime()
    return (os.getenv("VC_RECORDING_FANOUT_TOPIC_SUBSTRING") or "").strip()


def get_vc_recording_fanout_set_permission_enabled() -> bool:
    """
    When true (default), after a recording URL exists, call **set_permission** so each fan-out **group**
    gets **view** on the file (Feishu type=2 group). Fixes \"bot posted the link but members cannot play\".

    Set ``VC_RECORDING_FANOUT_SET_PERMISSION=0`` to skip if your token cannot call that API.

    Requires **vc:record** (authorize/update recording), not only ``vc:record:readonly``.
    """
    reload_env_runtime()
    v = (os.getenv("VC_RECORDING_FANOUT_SET_PERMISSION") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_lark_overview_post_chat_id_for_send(source_incident_chat_id: str, session_target_chat: str) -> str:
    """
    Lark chat_id for the final **Send overview** card.

    1. ``INCIDENT_OVERVIEW_SEND_MAP[source]`` if set (explicit broadcast destination).
    2. Else **detection** ``source`` if ``P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT=1``.
    3. Else ``session_target_chat`` (prompt / mirror from session).

    If ``source_incident_chat_id`` is empty but ``session_target_chat`` is the prompt side of
    ``INCIDENT_OVERVIEW_TARGET_MAP``, reverse-resolve detection so overview can still land in the
    incident group.
    """
    sid = (source_incident_chat_id or "").strip()
    tc = (session_target_chat or "").strip()
    if not sid.startswith("oc_") and tc.startswith("oc_"):
        sid = get_source_incident_chat_id_for_mirror_target(tc) or sid
    mapped = get_incident_overview_send_chat_id(sid) if sid.startswith("oc_") else ""
    if mapped:
        return mapped
    if get_p0_overview_post_to_source_incident_chat() and sid.startswith("oc_"):
        return sid
    return tc


def get_p0_meeting_cards_in_source_incident_chat() -> bool:
    """
    ``P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT`` — if ``1``, meeting invite / P1 confirm / end summaries
    post in the **incident** group (where P0 was declared), not the mirror ``INCIDENT_OVERVIEW_TARGET_MAP`` room.

    Use with ``INCIDENT_OVERVIEW_SEND_MAP`` so **Send overview** still lands in emergency / game rooms.
    """
    reload_env_runtime()
    v = (os.getenv("P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_session_meeting_card_post_chat_id(source_incident_chat_id: str) -> str:
    """
    Lark group for **meeting** UX: invite card, P1 yes/no, ended/cancel notices, session ``target_chat``.

    Default: legacy mirror via ``get_overview_target_chat_id_for_source_incident`` (same as before).
    With ``P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT=1``: always ``source_incident_chat_id``.
    """
    sid = (source_incident_chat_id or "").strip()
    if not sid:
        return ""
    if get_p0_meeting_cards_in_source_incident_chat():
        return sid
    return get_overview_target_chat_id_for_source_incident(sid) or sid


def get_overview_target_chat_id_for_source_incident(source_incident_chat_id: str) -> str:
    """
    Legacy **mirror** target for meeting cards + DM ``target_chat`` when
    ``P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT`` is off (default).

    Resolution order:

    1. ``INCIDENT_OVERVIEW_TARGET_MAP[source_incident_chat_id]`` if that detection ``oc_`` is listed.
    2. Else ``OVERVIEW_TARGET_GROUP_CHAT_ID`` / ``P0_OVERVIEW_POST_CHAT_ID`` if set (single global prompt group).
    3. Else ``source_incident_chat_id`` (cards and overview stay in the detection group).

    For **Send overview** only, see ``get_incident_overview_send_chat_id`` / ``INCIDENT_OVERVIEW_SEND_MAP``.
    """
    sid = (source_incident_chat_id or "").strip()
    if not sid:
        return ""
    m = get_incident_overview_target_map()
    if sid in m:
        return m[sid]
    g = get_overview_post_chat_id()
    if g:
        return g
    return sid


def get_target_group_chat_id() -> str:
    """Backward-compatible alias: optional fixed overview destination (not incident routing)."""
    return get_overview_post_chat_id()


def get_dm_overview_target_chat_id() -> str:
    """
    Where DM drafts / \"Send overview\" attach when **no** active P0 session.

    Order: ``OVERVIEW_TARGET_GROUP_CHAT_ID`` / ``P0_OVERVIEW_POST_CHAT_ID`` if set;
    else if ``INCIDENT_OVERVIEW_TARGET_MAP`` has exactly one ``oc_=oc_`` pair, use the mirror-side ``oc_``;
    else if ``INCIDENT_OVERVIEW_SEND_MAP`` has exactly one pair, use the **send** destination ``oc_``;
    else if exactly **one** incident group is configured, use that ``oc_`` id (common single-group deploys);
    else empty (multiple groups — need env or a live session).
    """
    reload_env_runtime()
    env_id = get_overview_post_chat_id()
    if env_id:
        return env_id
    m = get_incident_overview_target_map()
    if len(m) == 1:
        return next(iter(m.values()))
    m_send = get_incident_overview_send_map()
    if len(m_send) == 1:
        return next(iter(m_send.values()))
    ids = list(get_incident_group_chat_ids())
    if len(ids) == 1:
        return ids[0]
    return ""


def _parse_standalone_overview_tags_env() -> Dict[str, str]:
    """
    ``P0_STANDALONE_OVERVIEW_TAGS`` / ``STANDALONE_OVERVIEW_TAGS``:
    ``emergency=oc_aaa,game=oc_bbb`` (comma-separated ``tag=oc_...``).
    """
    reload_env_runtime()
    raw = (os.getenv("P0_STANDALONE_OVERVIEW_TAGS") or os.getenv("STANDALONE_OVERVIEW_TAGS") or "").strip()
    out: Dict[str, str] = {}
    if not raw:
        return out
    for seg in raw.split(","):
        seg = seg.strip()
        if "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        k, v = k.strip().lower(), v.strip()
        if k in ("emergency", "game") and v.startswith("oc_"):
            out[k] = v
    return out


def get_standalone_overview_target_chat_id_for_tag(tag: str) -> str:
    """
    Resolve ``oc_`` for DM command ``create overview emergency|game`` (no live meeting).

    1. Explicit ``P0_STANDALONE_OVERVIEW_TAGS=emergency=oc_...,game=oc_...`` if set.
    2. Else match ``INCIDENT_GROUP_EMERGENCY_TOPICS`` labels: ``emergency`` → label contains
       ``emergency``; ``game`` → label contains ``game`` or ``游戏``.
    """
    t = (tag or "").strip().lower()
    if t not in ("emergency", "game"):
        return ""
    explicit = _parse_standalone_overview_tags_env()
    if t in explicit:
        return explicit[t]
    for oc_id in sorted(get_incident_group_chat_ids()):
        label = get_emergency_topic_for_source_chat(oc_id)
        lo = label.lower()
        if t == "emergency" and "emergency" in lo:
            return oc_id
        if t == "game" and ("game" in lo or "游戏" in label):
            return oc_id
    return ""


REQ_TIMEOUT_ENV = (os.getenv("REQ_TIMEOUT", "15") or "15").strip()
try:
    REQ_TIMEOUT = float(REQ_TIMEOUT_ENV)
except Exception:
    REQ_TIMEOUT = 15.0


def timeout_kw() -> Dict[str, Any]:
    return {} if REQ_TIMEOUT <= 0 else {"timeout": REQ_TIMEOUT}


# Timezone and meeting (zoneinfo is stdlib in 3.9+; use backport on 3.8)
from datetime import datetime  # noqa: E402

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

PHT = ZoneInfo("Asia/Manila")
MEETING_TOPIC = (os.getenv("MEETING_TOPIC") or "CP-Emergency feedback紧急问题反馈群").strip()


def get_emergency_topic_for_source_chat(chat_id: str) -> str:
    """
    Bilingual suffix for ``🚨 P0 — …`` (meeting cards + VC topic), per incident group.

    ``INCIDENT_GROUP_EMERGENCY_TOPICS=oc_aaa=CP-Emergency feedback紧急问题反馈群,oc_bbb=Game urgent-游戏紧急群``

    Comma-separated; each segment is ``oc_...=topic text``. If no match, uses ``MEETING_TOPIC``.
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    default = (os.getenv("MEETING_TOPIC") or "CP-Emergency feedback紧急问题反馈群").strip()
    if not cid:
        return default
    raw = (os.getenv("INCIDENT_GROUP_EMERGENCY_TOPICS") or "").strip()
    if not raw:
        return default
    for segment in raw.split(","):
        segment = segment.strip()
        if "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        key, val = key.strip(), val.strip()
        if key == cid and val:
            return val
    return default


# Prefix for Lark VC / recorded meeting title (same string sent to ``create_vc_reserve``).
VIDEO_MEETING_TOPIC_PREFIX = (os.getenv("VIDEO_MEETING_TOPIC_PREFIX") or "Video meeting—").strip()


def get_vc_meeting_topic_for_source_chat(chat_id: str) -> str:
    """
    Topic string for Lark video conference reserve (shows on recorded / meeting UI).

    Format: ``{VIDEO_MEETING_TOPIC_PREFIX}{emergency label}``, e.g.
    ``Video meeting—CP-Emergency feedback紧急问题反馈群`` or
    ``Video meeting—Game urgent-游戏紧急群`` (from ``INCIDENT_GROUP_EMERGENCY_TOPICS`` / ``MEETING_TOPIC``).

    If the stored label already starts with ``video meeting`` (case-insensitive), it is returned unchanged.
    """
    reload_env_runtime()
    tail = get_emergency_topic_for_source_chat(chat_id).strip()
    if not tail:
        tail = (os.getenv("MEETING_TOPIC") or "CP-Emergency feedback紧急问题反馈群").strip()
    low = tail.lower()
    if low.startswith("video meeting"):
        return tail
    prefix = (os.getenv("VIDEO_MEETING_TOPIC_PREFIX") or VIDEO_MEETING_TOPIC_PREFIX).strip() or "Video meeting—"
    if not prefix.endswith("—") and not prefix.endswith("-"):
        prefix = prefix + "—"
    return f"{prefix}{tail}"


# Open Platform API root (…/open-apis). Default Singapore; override with LARK_OPEN_API_BASE if needed.
LARK_BASE = (os.getenv("LARK_OPEN_API_BASE") or "https://open-sg.larksuite.com/open-apis").strip().rstrip("/")
_LARK_GLOBAL_FALLBACK = "https://open.larksuite.com/open-apis"

SHEETS_BASES = [LARK_BASE, _LARK_GLOBAL_FALLBACK]
SHEETS_V2_BASES = SHEETS_BASES[:]

# VC / IM: primary = same host as tenant token (SG by default); fallback = global endpoint.
VC_BASES = [LARK_BASE, _LARK_GLOBAL_FALLBACK]
IM_BASES = [LARK_BASE, _LARK_GLOBAL_FALLBACK]

# Regex patterns
OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9]+$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ID_RE = re.compile(r"\b\d{6,}\b")
NOT_SPECIFIED_RE = re.compile(r"^\s*(not specified|n/?a|none|unknown|-)?\s*$", re.IGNORECASE)

CLEAR_RE = re.compile(r"^\s*(clear|reset|discard|cancel)\s*$", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*(status|draft|check)\s*$", re.IGNORECASE)

# DM whole line: ``create overview emergency|game`` — queue standalone overview (no meeting). Buttons-only for preview build.
STANDALONE_OVERVIEW_DM_RE = re.compile(
    r"^\s*create\s+overview\s+(emergency|game)\s*$",
    re.IGNORECASE,
)

WHO_IN_MEETING_RE = re.compile(
    r"^\s*(who\s+(is|are)\s+in\s+the\s+meeting|who\s+is\s+in\s+meeting|participants|list\s+participants|sino\s+nasa\s+meeting)\s*$",
    re.IGNORECASE,
)
IS_IN_MEETING_RE = re.compile(
    r"^\s*is\s+(.+?)\s+in\s+the\s+meeting\s*\??\s*$",
    re.IGNORECASE,
)

CountBuilder = Callable[[int], Tuple[Optional[int], str]]

PLAYER_COUNT_PATTERNS: List[Tuple[CountBuilder, re.Pattern[str]]] = [
    (lambda n: (n, f"Less than {n} affected players"), re.compile(r"\bless than\s+(\d+)\s+players?\b", re.IGNORECASE)),
    (lambda n: (n, f"more than {n} affected players"), re.compile(r"\bmore than\s+(\d+)\s+players?\b", re.IGNORECASE)),
    (lambda n: (n, f"more than {n} affected players"), re.compile(r"\bover\s+(\d+)\s+players?\b", re.IGNORECASE)),
    (lambda n: (n, f"at least {n} affected players"), re.compile(r"\bat least\s+(\d+)\s+players?\b", re.IGNORECASE)),
    (lambda n: (n, f"at least {n} affected players"), re.compile(r"\b(\d+)\s*\+\s*players?\b", re.IGNORECASE)),
    (lambda n: (n, f"{n} affected players"), re.compile(r"\b(\d+)\s+players?\b", re.IGNORECASE)),
    (lambda n: (n, f"{n} affected players"), re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{3,})\s+users?\b", re.IGNORECASE)),
]

PLAYER_VAGUE_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("multiple", re.compile(r"\bmultiple players\b", re.IGNORECASE)),
    ("many", re.compile(r"\bmany players\b", re.IGNORECASE)),
    ("several", re.compile(r"\bseveral players\b", re.IGNORECASE)),
    ("many", re.compile(r"\blarge volume of chats from players\b", re.IGNORECASE)),
    ("many", re.compile(r"\bhigh volume of chats from players\b", re.IGNORECASE)),
    ("many", re.compile(r"\blarge volume of player reports\b", re.IGNORECASE)),
    ("multiple", re.compile(r"\bmultiple affected players\b", re.IGNORECASE)),
]

PLAYER_VAGUE_LABELS: Dict[str, str] = {
    "multiple": "Multiple affected players",
    "many": "Many affected players",
    "several": "Several affected players",
}

# Groq
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "llama-3.1-8b-instant").strip()
GROQ_VISION_MODEL = (os.getenv("GROQ_VISION_MODEL") or "llama-3.2-11b-vision-preview").strip()
# One Groq call for issue EN + zh_issue + zh_impact (faster than summarize + 2 translates). Set 0 to use legacy path.
GROQ_OVERVIEW_ONE_SHOT = (os.getenv("GROQ_OVERVIEW_ONE_SHOT", "1") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# Timing
AUTO_PREVIEW_DELAY_SEC = float((os.getenv("AUTO_PREVIEW_DELAY_SEC", "6") or "6").strip())
ONGOING_CARD_DELAY_SEC = int((os.getenv("ONGOING_CARD_DELAY_SEC", "600") or "600").strip())
P1_TO_P0_ESCALATION_SEC = int((os.getenv("P1_TO_P0_ESCALATION_SEC", "900") or "900").strip())
P0_COOLDOWN_SEC = int((os.getenv("P0_COOLDOWN_SEC", "300") or "300").strip())
SUPPORT_MAP_TTL_SEC = int((os.getenv("SUPPORT_MAP_TTL_SEC", "600") or "600").strip())

# Lark VC ``reserves/apply``: ``end_time`` must be set for multi-person meetings; official cap ~30 days.
_VC_RESERVE_MAX_OFFSET_SEC = 30 * 24 * 60 * 60
_VC_RESERVE_MIN_OFFSET_SEC = 60 * 60


def get_vc_reserve_end_offset_sec() -> int:
    """
    Seconds from **now** until the reserve ``end_time`` sent to Lark (not the same as “call must hang up”).

    Default **30 days** — longest window Feishu documents for ``/vc/v1/reserves/apply`` (no fixed 2h cap).

    Env: ``P0_VC_RESERVE_END_OFFSET_SEC`` (integer seconds), clamped between 1 hour and 30 days.
    """
    reload_env_runtime()
    default = _VC_RESERVE_MAX_OFFSET_SEC
    raw = (os.getenv("P0_VC_RESERVE_END_OFFSET_SEC") or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        log.warning("Invalid P0_VC_RESERVE_END_OFFSET_SEC=%r — using default %s", raw, default)
        return default
    if v < _VC_RESERVE_MIN_OFFSET_SEC or v > _VC_RESERVE_MAX_OFFSET_SEC:
        log.warning(
            "P0_VC_RESERVE_END_OFFSET_SEC=%s clamped to [%s, %s]",
            v,
            _VC_RESERVE_MIN_OFFSET_SEC,
            _VC_RESERVE_MAX_OFFSET_SEC,
        )
    return max(_VC_RESERVE_MIN_OFFSET_SEC, min(v, _VC_RESERVE_MAX_OFFSET_SEC))


def is_open_id(x: str) -> bool:
    return bool(OPEN_ID_RE.match((x or "").strip()))


def get_host_and_dm_open_id() -> str:
    """
    One ``ou_`` for **both** VC organizer (primary owner) **and** DM instruction recipient.

    Set ``P0_HOST_AND_DM_OPEN_ID=ou_...`` when a single duty user should host the meeting
    and receive the bot DM. Used only as a **fallback** when the more specific vars below
    are unset.
    """
    reload_env_runtime()
    v = (os.getenv("P0_HOST_AND_DM_OPEN_ID") or "").strip()
    return v if v and is_open_id(v) else ""


def get_owner_ids() -> List[str]:
    """
    Lark VC reserve `owner_id` (organizer). Set in .env — required for creating meetings.

    P0_OWNER_OPEN_IDS — comma-separated open_ids (first id is primary owner).
    P0_INVITEE_OPEN_IDS — legacy alias for the same variable.
    If both empty: ``P0_HOST_AND_DM_OPEN_ID`` (single user) is used as the only owner.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_OWNER_OPEN_IDS") or os.getenv("P0_INVITEE_OPEN_IDS") or "").strip()
    if not raw:
        one = get_host_and_dm_open_id()
        return [one] if one else []
    ids = [x.strip() for x in raw.split(",") if x and x.strip()]
    out = [x for x in ids if is_open_id(x)]
    return out


def get_p0_trigger_ignore_open_ids() -> FrozenSet[str]:
    """
    Senders in this set cannot start P0/P1 from the incident group (silent ignore).

    P0_TRIGGER_IGNORE_OPEN_IDS — comma-separated Lark user open_ids (ou_...).
    """
    reload_env_runtime()
    raw = (os.getenv("P0_TRIGGER_IGNORE_OPEN_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return frozenset(x for x in ids if is_open_id(x))


def get_p0_thread_confirm_asker_open_ids() -> FrozenSet[str]:
    """
    ``P0_THREAD_CONFIRM_ASKER_OPEN_IDS`` — comma-separated ``ou_...``.

    Only these users may post a question like **\"is this P0?\"** to **arm** a thread
    confirmation (someone else replies **yes** → ``start_p0``). If empty, this flow is off.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_THREAD_CONFIRM_ASKER_OPEN_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return frozenset(x for x in ids if is_open_id(x))


def get_p0_thread_confirm_target_open_ids() -> FrozenSet[str]:
    """
    ``P0_THREAD_CONFIRM_TARGET_OPEN_IDS`` — comma-separated ``ou_...`` (optional).

    If **non-empty**, a qualifying **\"is this P0?\"** message also **arms** when **at least one**
    of these users appears in Lark ``mentions`` (someone @'d them to confirm), even if the sender
    is **not** in ``P0_THREAD_CONFIRM_ASKER_OPEN_IDS``.

    Use with ``P0_THREAD_CONFIRM_ASKER_OPEN_IDS`` (OR): duty users can still arm without @'s;
    anyone can arm when @'ing a designated confirmer.

    If both this and ``P0_THREAD_CONFIRM_ASKER_OPEN_IDS`` are empty, thread confirm is off.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_THREAD_CONFIRM_TARGET_OPEN_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return frozenset(x for x in ids if is_open_id(x))


def get_p0_thread_confirm_responder_open_ids() -> FrozenSet[str]:
    """
    ``P0_THREAD_CONFIRM_RESPONDER_OPEN_IDS`` — optional comma-separated ``ou_...``.

    If **non-empty**, only these users may reply **yes** to confirm. If **empty**, any user
    except the asker may confirm.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_THREAD_CONFIRM_RESPONDER_OPEN_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return frozenset(x for x in ids if is_open_id(x))


def get_p0_thread_confirm_ttl_sec() -> int:
    """``P0_THREAD_CONFIRM_TTL_SEC`` — how long a question stays armed (default 3600)."""
    reload_env_runtime()
    raw = (os.getenv("P0_THREAD_CONFIRM_TTL_SEC") or "3600").strip()
    try:
        n = int(raw)
    except Exception:
        n = 3600
    return max(60, min(n, 86400 * 7))


def get_p0_thread_confirm_allow_toplevel_yes() -> bool:
    """
    ``P0_THREAD_CONFIRM_ALLOW_TOPLEVEL_YES`` — if ``1``, while a question is **armed**,
    a **top-level** message in the same group (no ``parent_id`` / ``root_id``) that starts
    with **yes** can confirm P0, not only a **Reply** to the question.

    Default ``0`` (stricter: must use Reply / thread so Lark ties the message to the question).

    When enabled, see also ``P0_THREAD_CONFIRM_TOPLEVEL_GRACE_SEC`` and @mention of the asker
    (from webhook ``mentions[].id``) to limit false positives.
    """
    reload_env_runtime()
    v = (os.getenv("P0_THREAD_CONFIRM_ALLOW_TOPLEVEL_YES") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_thread_confirm_allow_asker_self_yes() -> bool:
    """
    ``P0_THREAD_CONFIRM_ALLOW_ASKER_SELF_YES`` — if ``1``, the designated asker may reply **yes**
    to their own **\"is this P0?\"** thread to start the meeting (same person asks + confirms).

    Default ``0``: someone *else* must reply **yes** (reduces self-trigger abuse).
    """
    reload_env_runtime()
    v = (os.getenv("P0_THREAD_CONFIRM_ALLOW_ASKER_SELF_YES") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_thread_confirm_use_groq() -> bool:
    """
    ``P0_THREAD_CONFIRM_USE_GROQ`` — if ``1``, when thread-confirm is **armed** and the reply
    does **not** match the regex allowlist, call Groq once to classify whether the reply affirms P0.

    Regex hits still skip Groq (fast path). Default ``0`` (regex only).
    """
    reload_env_runtime()
    v = (os.getenv("P0_THREAD_CONFIRM_USE_GROQ") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_keyword_groq_gate() -> bool:
    """
    ``P0_KEYWORD_GROQ_GATE`` — if ``1``, after regex filters pass, call Groq once to decide whether
    the message is a **new** P0 bridge declaration vs a passing mention (e.g. status inside an
    existing P0 meeting). Default ``0``.

    This is the **semantic** alternative to growing a long list of hard-coded heuristics in code;
    enable it when informal chat phrasing keeps bypassing rule-based filters.
    """
    reload_env_runtime()
    v = (os.getenv("P0_KEYWORD_GROQ_GATE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_keyword_use_builtin_context_filters() -> bool:
    """
    ``P0_KEYWORD_USE_BUILTIN_CONTEXT_FILTERS`` — if ``1`` (default), apply built-in heuristics
    (issue prose / in-meeting / informational or past-date phrasing) before opening a VC.

    Set ``0`` to **disable** those code-based checks and rely on ``P0_KEYWORD_GROQ_GATE``
    and/or ``P0_KEYWORD_SUPPLEMENTAL_SKIP_REGEX`` instead (recommended: enable Groq if you turn this off).
    """
    reload_env_runtime()
    v = (os.getenv("P0_KEYWORD_USE_BUILTIN_CONTEXT_FILTERS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_p0_keyword_supplemental_skip_regex() -> Optional[re.Pattern[str]]:
    """
    ``P0_KEYWORD_SUPPLEMENTAL_SKIP_REGEX`` — optional Python regex (``re.IGNORECASE | re.DOTALL``).
    If it matches the message **anywhere** (after ``p0`` is already present), the keyword VC trigger
    is skipped. Lets you tune false positives via **config**, without a code deploy.

    Invalid patterns log a warning and are ignored.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_KEYWORD_SUPPLEMENTAL_SKIP_REGEX") or "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw, re.IGNORECASE | re.DOTALL)
    except re.error as e:
        log.warning("P0_KEYWORD_SUPPLEMENTAL_SKIP_REGEX invalid (ignored): %s", e)
        return None


def get_p0_thread_confirm_toplevel_grace_sec() -> float:
    """
    ``P0_THREAD_CONFIRM_TOPLEVEL_GRACE_SEC`` — after the duty user arms **\"is this P0?\"**,
    for this many seconds a **plain** top-level **yes** (no ``@`` to the asker) still counts
    as in-conversation confirmation. After the grace window, a top-level yes must **@mention**
    the asker's ``ou_...`` (as sent in the webhook ``mentions`` list) or the confirmer must
    use **Reply** to the question message.

    Default ``180``. Set ``0`` to require @mention (or thread reply) for **every** top-level yes.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_THREAD_CONFIRM_TOPLEVEL_GRACE_SEC") or "180").strip()
    try:
        n = float(raw)
    except Exception:
        n = 180.0
    return max(0.0, min(n, float(86400 * 7)))


def get_incident_group_command_open_ids() -> FrozenSet[str]:
    """
    Parsed from ``P0_INCIDENT_GROUP_COMMAND_OPEN_IDS`` (comma-separated ``ou_...``).
    **No longer used for gating** — incident-group controls are available to all chat members who can message the bot.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_INCIDENT_GROUP_COMMAND_OPEN_IDS") or "").strip()
    if not raw:
        return frozenset()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return frozenset(x for x in ids if is_open_id(x))


def can_use_incident_group_commands(user_open_id: str) -> bool:
    """Incident-group control commands are available to all members who can message the bot."""
    return True


def get_dm_instruction_open_ids() -> List[str]:
    """
    If non-empty, P0/P1 DM instruction cards are sent to these users instead of whoever typed p0/p1.

    P0_DM_INSTRUCTION_OPEN_IDS — comma-separated open_ids (ou_...), multiple recipients.
    P0_DM_INSTRUCTION_OPEN_ID — single open_id (legacy; use if OPEN_IDS is unset).
    If those are unset: ``P0_HOST_AND_DM_OPEN_ID`` (same as single host + DM user).
    """
    reload_env_runtime()
    raw_multi = (os.getenv("P0_DM_INSTRUCTION_OPEN_IDS") or "").strip()
    if raw_multi:
        parts = [x.strip() for x in raw_multi.split(",") if x.strip()]
        return [x for x in parts if is_open_id(x)]
    raw_single = (os.getenv("P0_DM_INSTRUCTION_OPEN_ID") or "").strip()
    if raw_single and is_open_id(raw_single):
        return [raw_single]
    one = get_host_and_dm_open_id()
    return [one] if one else []


def get_dm_instruction_open_id() -> str:
    """First DM instruction recipient, or empty (for simple callers)."""
    ids = get_dm_instruction_open_ids()
    return ids[0] if ids else ""


def get_dm_repost_instruction_after_reset() -> bool:
    """
    If True, after **Clear draft** (button or ``CLEAR_RE`` text) the bot reposts the DM
    instruction card. A **draft-cleared** text prompt (paste screenshots/text again) is
    **always** sent regardless of this flag. **Cancel preview** always recalls the
    preview message and posts a fresh instruction card (not gated by this flag). Default
    **False** for instruction-card repost on clear-draft; set
    ``P0_DM_REPOST_INSTRUCTION_AFTER_RESET=1`` to repost the card too. Older instruction
    cards in the thread usually still accept button clicks.
    """
    reload_env_runtime()
    v = (os.getenv("P0_DM_REPOST_INSTRUCTION_AFTER_RESET") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _parse_incident_keyed_url_map(raw: str) -> Dict[str, str]:
    """
    Comma-separated ``oc_...=value`` (value may contain ``=`` in URL — split on first ``=`` only).
    """
    out: Dict[str, str] = {}
    if not (raw or "").strip():
        return out
    for segment in raw.split(","):
        segment = segment.strip()
        if "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        key, val = key.strip(), val.strip()
        if key.startswith("oc_") and val:
            out[key] = val
    return out


def p0_graph_screenshot_enabled() -> bool:
    """
    When True, after a **P0** session starts the bot captures a Playwright screenshot of
    ``P0_GRAPH_SCREENSHOT_URL`` and posts it to ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID``.
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_ENABLED") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_graph_screenshot_url() -> str:
    reload_env_runtime()
    return (os.getenv("P0_GRAPH_SCREENSHOT_URL") or "").strip()


def get_p0_graph_screenshot_append_kiosk() -> bool:
    """
    When True (default), append Grafana ``kiosk`` / ``kiosk=tv`` to the dashboard URL if not already
    present — hides the left nav and yields a cleaner capture (ops-style “panels only”).
    Set ``P0_GRAPH_SCREENSHOT_KIOSK=0`` to disable.
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_KIOSK") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_p0_graph_screenshot_clip_selectors() -> List[str]:
    """
    Comma-separated CSS selectors (first **substantial** visible match) for the **dashboard body**.
    Narrow ``.scrollbar-view`` hits (side gutter ~400px wide) are skipped so screenshots are not blank gray.
    Playwright uses the element’s box + ``scrollHeight`` as the ``full_page`` clip — excluding
    most browser chrome; pair with ``P0_GRAPH_SCREENSHOT_KIOSK=1`` (default).

    Override with ``P0_GRAPH_SCREENSHOT_CLIP_SELECTOR=main`` (or several, comma-separated).
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_CLIP_SELECTOR") or "").strip()
    if raw.lower() in ("-", "none", "off", "0"):
        return []
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [
        '[data-testid="canvas-body"] .scrollbar-view',
        "main .scrollbar-view",
        "main",
        ".react-grid-layout",
    ]


def get_p0_graph_screenshot_target_chat_id() -> str:
    """Lark group ``oc_...`` to receive the screenshot (can differ from incident group)."""
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID") or "").strip()
    return v if v.startswith("oc_") and len(v) > 12 else ""


def get_p0_graph_screenshot_viewport_width() -> int:
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_VIEWPORT_WIDTH") or "1280").strip()
    try:
        n = int(raw)
    except Exception:
        n = 1280
    return max(320, min(n, 3840))


def get_p0_graph_screenshot_viewport_height() -> int:
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_VIEWPORT_HEIGHT") or "720").strip()
    try:
        n = int(raw)
    except Exception:
        n = 720
    return max(240, min(n, 2160))


def get_p0_graph_screenshot_device_scale_factor() -> float:
    """
    Playwright ``device_scale_factor`` (CSS pixel ratio for screenshots). ``2`` renders **2×** device
    pixels per CSS pixel — much sharper when Lark downscales wide dashboards; default ``1``.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_DEVICE_SCALE_FACTOR") or "1").strip()
    try:
        x = float(raw)
    except Exception:
        x = 1.0
    if x < 1.0:
        return 1.0
    if x > 3.0:
        return 3.0
    return x


def get_p0_graph_screenshot_wait_ms() -> int:
    """Extra wait after ``goto`` (and ``wait_until``) before ``screenshot`` — lets Grafana panels query/render."""
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_WAIT_MS") or "4000").strip()
    try:
        n = int(raw)
    except Exception:
        n = 4000
    return max(0, min(n, 120_000))


def get_p0_graph_screenshot_panel_ready_timeout_ms() -> int:
    """
    After navigation, wait up to this many ms for Grafana dashboard panel DOM (e.g. ``.react-grid-item``)
    before the fixed ``P0_GRAPH_SCREENSHOT_WAIT_MS`` sleep. Reduces **blank black** screenshots when
    ``load`` fires before React panels mount. Set **0** to skip (default). For heavy dashboards try
    **20000–35000**.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_PANEL_READY_TIMEOUT_MS") or "0").strip()
    try:
        n = int(raw)
    except Exception:
        n = 0
    return max(0, min(n, 120_000))


def get_p0_graph_screenshot_panel_content_ready_timeout_ms() -> int:
    """
    After panel scaffolding exists (``.react-grid-item`` / ``[data-panel-id]``), Grafana still needs
    time to run queries and paint **canvas / SVG** charts. This timeout drives ``page.wait_for_function``
    until graphs look present; set **0** to skip.

    If ``P0_GRAPH_SCREENSHOT_PANEL_CONTENT_READY_TIMEOUT_MS`` is **unset** but
    ``P0_GRAPH_SCREENSHOT_PANEL_READY_TIMEOUT_MS`` **> 0**, defaults to
    ``max(45000, panel_ready + 25000)`` (capped at **180000**) so enabling grid wait also waits for
    chart paint unless you explicitly set ``PANEL_CONTENT_READY_TIMEOUT_MS=0``.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_PANEL_CONTENT_READY_TIMEOUT_MS") or "").strip()
    if raw:
        try:
            return max(0, min(int(raw), 180_000))
        except ValueError:
            log.warning("P0_GRAPH_SCREENSHOT_PANEL_CONTENT_READY_TIMEOUT_MS=%r invalid — using cascade", raw)
    pr_raw = (os.getenv("P0_GRAPH_SCREENSHOT_PANEL_READY_TIMEOUT_MS") or "0").strip()
    try:
        pr = int(pr_raw)
    except ValueError:
        pr = 0
    if pr <= 0:
        return 0
    inferred = max(45_000, pr + 25_000)
    return min(inferred, 180_000)


def get_p0_graph_screenshot_nav_timeout_ms() -> int:
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_NAV_TIMEOUT_MS") or "60000").strip()
    try:
        n = int(raw)
    except Exception:
        n = 60000
    return max(5000, min(n, 300_000))


def get_p0_graph_screenshot_full_page() -> bool:
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_FULL_PAGE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_graph_screenshot_full_document() -> bool:
    """
    When ``1``: ``page.screenshot(full_page=True)`` over the **entire document** (no CSS clip).

    That can produce a **very tall** PNG (whole scroll), often with large empty bands if panels lazy-load —
    bad for Lark. For **two images that each show half of what you see in the browser window** (fixed
    viewport, like 1920×1080), use ``P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1`` + ``SPLIT_VERTICAL_HALVES=1``
    and keep this off.
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_FULL_DOCUMENT") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_graph_screenshot_split_vertical_halves() -> bool:
    """
    When True: post **multiple** PNGs along the dashboard scroll (see
    ``P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT``) or legacy Pillow / clip splits.
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_graph_screenshot_viewport_scroll_count() -> int:
    """
    With ``P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1``: number of **full-viewport** screenshots taken in a row,
    scrolling the main dashboard down ~one screen between each (less clutter per image for long boards).

    If ``P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT`` is **unset**: ``2`` when split-halves is on, else ``1``.
    Clamped to **1–8**.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 8))
        except ValueError:
            log.warning(
                "P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT=%r invalid — using split default",
                raw,
            )
    v = (os.getenv("P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES") or "0").strip().lower()
    split = v in ("1", "true", "yes", "on")
    return 2 if split else 1


def get_p0_graph_screenshot_goto_wait_until() -> str:
    """
    Playwright ``page.goto(..., wait_until=...)``.

    - ``load`` (default): wait for load event — good when you want charts to start rendering; pair with a
      higher ``P0_GRAPH_SCREENSHOT_WAIT_MS`` for dense Grafana dashboards.
    - ``domcontentloaded``: earlier — page shell before many panel queries finish (lighter / “before graphs”).
    - ``networkidle``: can hang on Grafana (WebSockets); avoid unless you know the site goes idle.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_GOTO_WAIT_UNTIL") or "load").strip().lower()
    allowed = frozenset(("load", "domcontentloaded", "networkidle", "commit"))
    if raw in allowed:
        return raw
    log.warning("P0_GRAPH_SCREENSHOT_GOTO_WAIT_UNTIL=%r invalid — using load", raw)
    return "load"


def get_p0_graph_screenshot_caption() -> str:
    """
    Optional text posted before the image (empty = a default line with capture time only).

    Placeholders: ``{label}`` = incident source chat display name; ``{captured_at}`` = date/time when
    the PNG was taken (default zone Malaysia ``Asia/Kuala_Lumpur``; see ``P0_GRAPH_SCREENSHOT_TIMEZONE``).
    """
    reload_env_runtime()
    return (os.getenv("P0_GRAPH_SCREENSHOT_CAPTION") or "").strip()


def get_p0_graph_screenshot_timezone_name() -> str:
    """
    IANA zone for ``{captured_at}`` timestamps. Default **Malaysia Time** (``Asia/Kuala_Lumpur``, MYT).

    Set to ``UTC``, ``Asia/Singapore``, etc. if you need a different zone.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_TIMEZONE") or "").strip()
    return raw if raw else "Asia/Kuala_Lumpur"


def get_p0_graph_screenshot_chromium_args() -> List[str]:
    """
    Comma-separated extra Chromium flags for Playwright on Linux/Docker, e.g.
    ``--no-sandbox,--disable-dev-shm-usage``. Empty = default ``--disable-dev-shm-usage`` only.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_CHROMIUM_ARGS") or "").strip()
    if not raw:
        return ["--disable-dev-shm-usage"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_p0_graph_screenshot_playwright_user_data_dir() -> str:
    """
    If set to an **existing** directory, Playwright uses ``launch_persistent_context`` so Chromium
    keeps cookies/local storage (e.g. after you log in to **Grafana** once in a headed browser using
    this same profile path). Same idea as Slack ``SESSION_DIR`` — without this, each run is a fresh
    session and Grafana will usually show the login page unless the dashboard is anonymous/public.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR") or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser().resolve()
    return str(p) if p.is_dir() else ""


def get_p0_graph_screenshot_playwright_headless() -> bool:
    """
    Default **headless** Chromium. Set ``P0_GRAPH_SCREENSHOT_HEADED=1`` for a visible window
    (e.g. manual debugging on VNC — same capture path as the bot).
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_HEADED") or "0").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return False
    return True


def get_p0_graph_screenshot_swiftshader() -> bool:
    """
    Append ANGLE + **SwiftShader** Chromium flags (software GL). Fixes **solid black** screenshots
    on many Linux/VPS headless setups where the GPU stack does not composite Web/canvas.

    * Explicit ``P0_GRAPH_SCREENSHOT_SWIFTSHADER=0`` → off.
    * Explicit ``1`` → on.
    * **Unset** → **on** when ``sys.platform`` is Linux (ose-bot style servers); off on macOS/Windows.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_SWIFTSHADER") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return sys.platform.startswith("linux")


def get_p0_graph_screenshot_viewport_only() -> bool:
    """If ``1``, skip CSS clip and capture **viewport** only (``full_page=False``). Debugging / GPU issues."""
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_p0_graph_screenshot_blank_fallback_viewport() -> bool:
    """
    If the PNGs look **uniformly blank** (near-black, no contrast), retry once with viewport-only
    capture. On by default; set ``P0_GRAPH_SCREENSHOT_BLANK_FALLBACK_VIEWPORT=0`` to disable.
    """
    reload_env_runtime()
    v = (os.getenv("P0_GRAPH_SCREENSHOT_BLANK_FALLBACK_VIEWPORT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def slack_automation_enabled() -> bool:
    """Gate Playwright Slack huddle subprocess (default on when env vars are set)."""
    reload_env_runtime()
    v = (os.getenv("SLACK_AUTOMATION_ENABLED") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def slack_huddle_on_p0_start() -> bool:
    """
    Run Playwright huddle automation when a P0/P1 VC session **starts** (``start_p0``).

    Set ``SLACK_HUDDLE_ON_P0_START=0`` if you only want huddle when **Send overview** fires.
    """
    reload_env_runtime()
    v = (os.getenv("SLACK_HUDDLE_ON_P0_START") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def slack_huddle_on_overview_send() -> bool:
    """
    Run Playwright huddle automation when **Send overview** to the Lark group succeeds
    (same moment as ``SLACK_OVERVIEW_WEBHOOK_MAP`` mirror).

    Set ``SLACK_HUDDLE_ON_OVERVIEW_SEND=0`` to post overview to Slack only (webhook) without huddle.
    """
    reload_env_runtime()
    v = (os.getenv("SLACK_HUDDLE_ON_OVERVIEW_SEND") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def slack_severity_prompt_enabled() -> bool:
    """
    When True (default): after a **P0** session starts, the **2nd** bot DMs **Major / Minor** while the
    **primary** bot DMs the green overview card in parallel. **Major** runs the usual Slack automation;
    **Minor** skips it.

    **P1** sessions do not use this prompt (only the primary bot sends the green overview DM).

    Set ``SLACK_SEVERITY_PROMPT_BEFORE_AUTOMATION=0`` to restore immediate Slack on meeting start
    (no severity DM prompt).
    """
    reload_env_runtime()
    v = (os.getenv("SLACK_SEVERITY_PROMPT_BEFORE_AUTOMATION") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def slack_notify_channel_on_p0_declare_when_severity_prompt() -> bool:
    """
    When True (default): if ``SLACK_SEVERITY_PROMPT_BEFORE_AUTOMATION`` is on, the Slack incident channel
    is notified as soon as P0 is declared (before Major/Minor). After **Major**, only huddle automation
    runs (no duplicate channel ping). Set ``SLACK_NOTIFY_CHANNEL_ON_P0_DECLARE_WITH_SEVERITY=0`` to keep
    the legacy behavior: first Slack channel ping only after **Major**.
    """
    reload_env_runtime()
    v = (os.getenv("SLACK_NOTIFY_CHANNEL_ON_P0_DECLARE_WITH_SEVERITY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_lark_primary_app_credentials() -> Tuple[str, str]:
    """Main bot: P0 meeting, green overview DM, most IM (``LARK_APP_ID`` / ``LARK_APP_SECRET``)."""
    reload_env_runtime()
    return ((os.getenv("LARK_APP_ID") or "").strip(), (os.getenv("LARK_APP_SECRET") or "").strip())


def _strip_lark_env_val(raw: str) -> str:
    """Strip whitespace and UTF-8 BOM (bad editor / copy-paste) from .env values."""
    s = (raw or "").strip().strip("\ufeff")
    return s.strip()


def get_lark_severity_app_credentials() -> Tuple[str, str]:
    """
    Optional second bot: severity Major/Minor + minor follow-up cards only.

    Set ``LARK_SEVERITY_APP_ID`` and ``LARK_SEVERITY_APP_SECRET``. Aliases:

    - ``LARK_APP_ID_SEVERITY`` / ``LARK_APP_SECRET_SEVERITY``
    - ``LARK_APP_ID_2`` / ``LARK_APP_SECRET_2`` (common when you name the second app this way)

    If either id or secret is empty, severity DMs use the **primary** app (automation bot).
    """
    reload_env_runtime()
    sid = _strip_lark_env_val(
        os.getenv("LARK_SEVERITY_APP_ID")
        or os.getenv("LARK_APP_ID_SEVERITY")
        or os.getenv("LARK_APP_ID_2")
        or ""
    )
    sec = _strip_lark_env_val(
        os.getenv("LARK_SEVERITY_APP_SECRET")
        or os.getenv("LARK_APP_SECRET_SEVERITY")
        or os.getenv("LARK_APP_SECRET_2")
        or ""
    )
    return sid, sec


def get_slack_channel_url_for_incident_chat(chat_id: str) -> str:
    """
    Slack channel deep link for ``scripts/slack_huddle_invite_all.py``.

    Preferred (multi-group): ``LARK_SLACK_CHANNEL_URL_MAP=oc_aaa=https://...,oc_bbb=...``

    Legacy (single channel, direct ``SLACK_CHANNEL_URL``): if the map has no entry
    for this chat, ``SLACK_CHANNEL_URL`` is used when ``chat_id`` is in ``INCIDENT_GROUP_IDS``.
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    if not cid:
        return ""
    raw = (os.getenv("LARK_SLACK_CHANNEL_URL_MAP") or "").strip()
    m = _parse_incident_keyed_url_map(raw)
    v = (m.get(cid) or "").strip()
    if v:
        return v
    fallback = (os.getenv("SLACK_CHANNEL_URL") or "").strip()
    if fallback and cid in get_incident_group_chat_ids():
        return fallback
    return ""


def get_slack_session_dir_for_incident_chat(chat_id: str) -> str:
    """
    Persistent Chromium profile for Slack (``SESSION_DIR`` in the huddle script).

    Per-chat override: ``LARK_SLACK_SESSION_DIR_MAP=oc_aaa=/path1,oc_bbb=/path2``.
    Fallback: ``SLACK_SESSION_DIR``, then ``SESSION_DIR`` (same name as old Puppeteer script).
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    raw_map = (os.getenv("LARK_SLACK_SESSION_DIR_MAP") or "").strip()
    if raw_map:
        m = _parse_incident_keyed_url_map(raw_map)
        v = (m.get(cid) or "").strip()
        if v:
            return v
    return (os.getenv("SLACK_SESSION_DIR") or os.getenv("SESSION_DIR") or "").strip()


def get_slack_overview_webhook_for_incident_chat(chat_id: str) -> str:
    """
    Incoming Webhook URL to mirror \"Send overview\" markdown to Slack for this incident ``oc_`` chat.

    Env: ``SLACK_OVERVIEW_WEBHOOK_MAP=oc_aaa=https://hooks.slack.com/services/...,oc_bbb=...``
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    if not cid:
        return ""
    raw = (os.getenv("SLACK_OVERVIEW_WEBHOOK_MAP") or "").strip()
    m = _parse_incident_keyed_url_map(raw)
    return (m.get(cid) or "").strip()


def get_slack_incident_notify_webhook_for_incident_chat(chat_id: str) -> str:
    """
    Incoming Webhook for **P0/P1 declared** alerts (\"triggered in Lark\" + huddle status).

    Prefer ``SLACK_INCIDENT_NOTIFY_WEBHOOK_MAP=oc_aaa=https://hooks...`` (same shape as overview map).
    If unset for this ``oc_``, falls back to ``SLACK_OVERVIEW_WEBHOOK_MAP`` (same URLs as overview channel).
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    if not cid:
        return ""
    raw = (os.getenv("SLACK_INCIDENT_NOTIFY_WEBHOOK_MAP") or "").strip()
    if raw:
        m = _parse_incident_keyed_url_map(raw)
        v = (m.get(cid) or "").strip()
        if v:
            return v
    return get_slack_overview_webhook_for_incident_chat(chat_id)


def get_slack_bot_token() -> str:
    """
    Slack **Bot User OAuth Token** (``xoxb-...``) for ``chat.postMessage``.

    Env: ``SLACK_BOT_TOKEN`` or ``SLACK_BOT_USER_OAUTH_TOKEN`` (either name).
    Scopes: at least ``chat:write``; bot must be in the target channel.
    """
    reload_env_runtime()
    return (os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT_USER_OAUTH_TOKEN") or "").strip()


def get_slack_app_id() -> str:
    """Optional ``App ID`` from api.slack.com (for your records only; not sent on every API call)."""
    reload_env_runtime()
    return (os.getenv("SLACK_APP_ID") or "").strip()


def get_slack_bot_user_id() -> str:
    """
    Bot **Member ID** (``U...``) for ``<@U...>`` mentions in outgoing messages.

    Slack: open the bot profile → **Copy member ID** (starts with ``U``).
    """
    reload_env_runtime()
    return (os.getenv("SLACK_BOT_USER_ID") or "").strip()


def get_slack_api_channel_id_for_incident_chat(chat_id: str) -> str:
    """
    Slack **channel ID** (``C...``) for ``chat.postMessage``, per Lark incident ``oc_``.

    Env: ``SLACK_API_CHANNEL_MAP=oc_aaa=C0AAAA,oc_bbb=C0BBBB``
    (same comma-separated shape as other maps).
    """
    reload_env_runtime()
    cid = (chat_id or "").strip()
    if not cid:
        return ""
    raw = (os.getenv("SLACK_API_CHANNEL_MAP") or "").strip()
    m = _parse_incident_keyed_url_map(raw)
    return (m.get(cid) or "").strip()
