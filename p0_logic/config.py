"""
P0 logic configuration: env reload, timeouts, API bases, regex patterns, timing constants.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

import logging

log = logging.getLogger("lark-ops-ai")

try:
    from dotenv import dotenv_values
except Exception:
    dotenv_values = None

ENV_PATH = os.getenv("ENV_PATH", "/home/ubuntu/lark-ops-ai/.env")


def reload_env_runtime() -> None:
    if not dotenv_values:
        return
    try:
        values = dotenv_values(ENV_PATH)
        for k, v in (values or {}).items():
            if v is None:
                continue
            os.environ[k] = str(v)
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
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def get_overview_post_chat_id() -> str:
    """
    If set, \"Send overview\" posts to this oc_ chat; otherwise posts to the group
    where the session started (per-chat_id session).
    """
    reload_env_runtime()
    return (os.getenv("OVERVIEW_TARGET_GROUP_CHAT_ID") or os.getenv("P0_OVERVIEW_POST_CHAT_ID") or "").strip()


def get_target_group_chat_id() -> str:
    """Backward-compatible alias: optional fixed overview destination (not incident routing)."""
    return get_overview_post_chat_id()


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
LARK_BASE = "https://open-sg.larksuite.com/open-apis"

SHEETS_BASES = [
    "https://open-sg.larksuite.com/open-apis",
    "https://open.larksuite.com/open-apis",
]
SHEETS_V2_BASES = SHEETS_BASES[:]

VC_BASES = [
    "https://open.larksuite.com/open-apis",
    "https://open-sg.larksuite.com/open-apis",
]

IM_BASES = [
    "https://open-sg.larksuite.com/open-apis",
    "https://open.larksuite.com/open-apis",
]

# Regex patterns
OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9]+$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ID_RE = re.compile(r"\b\d{6,}\b")
NOT_SPECIFIED_RE = re.compile(r"^\s*(not specified|n/?a|none|unknown|-)?\s*$", re.IGNORECASE)

CLEAR_RE = re.compile(r"^\s*(clear|reset|discard|cancel)\s*$", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*(status|draft|check)\s*$", re.IGNORECASE)
GENERATE_RE = re.compile(r"^\s*(generate|preview|create overview)\s*$", re.IGNORECASE)

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

# Timing
AUTO_PREVIEW_DELAY_SEC = float((os.getenv("AUTO_PREVIEW_DELAY_SEC", "6") or "6").strip())
ONGOING_CARD_DELAY_SEC = int((os.getenv("ONGOING_CARD_DELAY_SEC", "600") or "600").strip())
P1_TO_P0_ESCALATION_SEC = int((os.getenv("P1_TO_P0_ESCALATION_SEC", "900") or "900").strip())
P0_COOLDOWN_SEC = int((os.getenv("P0_COOLDOWN_SEC", "300") or "300").strip())
SUPPORT_MAP_TTL_SEC = int((os.getenv("SUPPORT_MAP_TTL_SEC", "600") or "600").strip())


def is_open_id(x: str) -> bool:
    return bool(OPEN_ID_RE.match((x or "").strip()))


def get_owner_ids() -> List[str]:
    """
    Lark VC reserve `owner_id` (organizer). Set in .env — required for creating meetings.

    P0_OWNER_OPEN_IDS — comma-separated open_ids (first id is primary owner).
    P0_INVITEE_OPEN_IDS — legacy alias for the same variable.
    """
    reload_env_runtime()
    raw = (os.getenv("P0_OWNER_OPEN_IDS") or os.getenv("P0_INVITEE_OPEN_IDS") or "").strip()
    if not raw:
        return []
    ids = [x.strip() for x in raw.split(",") if x and x.strip()]
    return [x for x in ids if is_open_id(x)]


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


def get_dm_instruction_open_ids() -> List[str]:
    """
    If non-empty, P0/P1 DM instruction cards are sent to these users instead of whoever typed p0/p1.

    P0_DM_INSTRUCTION_OPEN_IDS — comma-separated open_ids (ou_...), multiple recipients.
    P0_DM_INSTRUCTION_OPEN_ID — single open_id (legacy; use if OPEN_IDS is unset).
    """
    reload_env_runtime()
    raw_multi = (os.getenv("P0_DM_INSTRUCTION_OPEN_IDS") or "").strip()
    if raw_multi:
        parts = [x.strip() for x in raw_multi.split(",") if x.strip()]
        return [x for x in parts if is_open_id(x)]
    raw_single = (os.getenv("P0_DM_INSTRUCTION_OPEN_ID") or "").strip()
    if raw_single and is_open_id(raw_single):
        return [raw_single]
    return []


def get_dm_instruction_open_id() -> str:
    """First DM instruction recipient, or empty (for simple callers)."""
    ids = get_dm_instruction_open_ids()
    return ids[0] if ids else ""
