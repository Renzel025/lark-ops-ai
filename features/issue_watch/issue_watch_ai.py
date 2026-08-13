"""
Claude + Groq + keyword classification for detection-group issue signals.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional, Tuple

from p0_logic import config as _config
from p0_logic.groq_client import _parse_json_object, groq_chat_once

log = logging.getLogger("lark-ops-ai")

_ISSUE_WATCH_SYSTEM = (
    "You triage Lark **detection / emergency feedback** group chat for an on-call ops bot.\n"
    "Flag messages that may be **Major P0** — production/player-facing problems duty must see.\n\n"
    "**Major issue scope (prioritize these):**\n"
    "1 **Login** — cannot log in, OTP/credential failures\n"
    "2 **Games / events entering** — cannot enter or join games/events/lobbies (not one-table bet glitch)\n"
    "3 **Withdrawal** — withdraw fails, stuck, unavailable\n"
    "4 **Deposit** — deposit/top-up fails, stuck, unavailable\n"
    "5 **Promotion / voucher** — promo codes, vouchers, coupons, campaigns not applying or broken\n"
    "6 **Rebate** — rebate/cashback not credited, wrong amount, unavailable\n"
    "7 **LuckyCoin** — LuckyCoin balance, redemption, or rewards broken\n"
    "8 **Company loss / financial impact** — wrong payout, duplicate credit, overpayment, mass incorrect "
    "settlement, or any issue explicitly causing company/player financial loss\n"
    "Also major: website down, registration broken, FPMS/PMS backend down.\n\n"
    "Output ONLY valid JSON:\n"
    "{\n"
    '  "is_incident_signal": true|false,\n'
    '  "categories": ["login_issues"],\n'
    '  "confidence": 0.0-1.0,\n'
    '  "summary": "one concise English sentence",\n'
    '  "issue_fingerprint": "short_snake_case_cluster_key",\n'
    '  "players_mentioned_in_message": 0,\n'
    '  "reason": "one short sentence"\n'
    "}\n\n"
    "Categories (use exact keys, one or more):\n"
    "1 login_issues — login fails, credential/OTP errors\n"
    "2 gameplay_outage — games/events/lobbies cannot be entered or joined broadly (NOT one bet rejected on one table)\n"
    "3 withdrawal_issues — withdraw fails or unavailable\n"
    "4 deposit_issues — deposit/top-up fails or unavailable\n"
    "5 promotion_voucher — promotion, voucher, coupon, campaign problems\n"
    "6 rebate_issues — rebate/cashback problems\n"
    "7 luckycoin_issues — LuckyCoin balance, redemption, reward problems\n"
    "8 company_loss — wrong payout, duplicate credit, overpayment, financial/company loss\n"
    "9 website_downtime — official site cannot be accessed, infinite loading, site down\n"
    "10 registration_failures — new users cannot register\n"
    "11 backend_downtime — FPMS or PMS backend unreachable or unusable\n"
    "12 widespread_impact — ONLY if this single message itself mentions 3+ distinct players/users "
    "reporting the same issue (rare; bot also counts across messages separately)\n\n"
    "Rules:\n"
    "- is_incident_signal=TRUE when staff/OM report real symptoms in the **Major issue scope** above.\n"
    "- is_incident_signal=false for: pure greetings/thanks, jokes, meeting invites, "
    "declaring p0/p1 bridge, screenshot-only requests with no incident, status with NO problem.\n"
    "- is_incident_signal=FALSE for **maintenance** or **test during maintenance** (maintenance icon, "
    "game under maintenance, set back to maintenance, scheduled maintenance) — expected downtime, "
    "even if unable to enter/login/bet during the window.\n"
    "- Single-player **bet rejected** / one table error for **one** player is NOT a major outage — "
    "use is_incident_signal=false or very low confidence unless many players or all games affected.\n"
    "- gameplay_outage = enter-game/event broadly broken — NOT one live-table bet error.\n"
    "- promotion/voucher/rebate/LuckyCoin: TRUE when players cannot claim, redeem, or receive expected rewards.\n"
    "- company_loss: TRUE when message implies financial harm (duplicate credit, wrong settlement, company loss).\n"
    "- is_incident_signal=FALSE when staff confirms things work: \"able to withdraw without any issue\", "
    "\"we were able to withdraw realtime without encountering any issue\", \"deposit is working fine\", "
    "\"checked — no problem\", \"resolved / back to normal\". Words like withdraw/deposit/issue in the "
    "same message do NOT mean an incident if the meaning is success or no problem.\n"
    "- Staff asking the team to investigate a login/deposit/withdrawal/game/promo/rebate/LuckyCoin issue = TRUE (high confidence).\n"
    "- ``players can't deposit``, ``cant proceed to deposit``, ``deposit on cp website``, ``充值失败`` = deposit_issues (NOT login_issues).\n"
    "- If message mentions **deposit** / top-up / 充值, use deposit_issues even when **cp website** appears.\n"
    "- issue_fingerprint: stable key (login_otp_failure, deposit_failure_cp, promo_voucher_not_applied, rebate_not_credited, luckycoin_redemption_fail).\n"
    "- Multilingual input (English, Chinese, Tagalog) — classify by meaning.\n"
)

# Chat often uses ``cant`` without an apostrophe; ``proceed to deposit`` is common OM phrasing.
_NEGATED = r"(?:cannot|can't|cant|unable\s+to)"

_KEYWORD_RULES: Tuple[Tuple[re.Pattern[str], List[str], str, float, str], ...] = (
    (
        re.compile(
            r"(?is)"
            r"(?:\b(?:website|web\s*site|site|cp\s*website|official\s+site)\b.{0,80}"
            r"(?:loading|load(?:ing)?\s+(?:forever|continuously|non-?stop)|"
            r"cannot\s+access|can't\s+access|not\s+(?:open|working|loading)|down|unreachable|"
            r"打不开|无法访问|一直加载|无限加载))"
            r"|"
            r"(?:\b(?:loading|load(?:ing)?\s+(?:forever|continuously))\b.{0,80}"
            r"\b(?:website|web\s*site|site|cp)\b)"
        ),
        ["website_downtime"],
        "website_loading",
        0.93,
        "Website continuously loading or inaccessible",
    ),
    (
        re.compile(
            r"(?is)"
            rf"\b{_NEGATED}\s+(?:proceed\s+to\s+)?deposit\b|"
            rf"\bplayers?\b.{{0,140}}{_NEGATED}\s+(?:proceed\s+to\s+)?deposit\b|"
            rf"\bproceed\s+to\s+deposit\b|"
            rf"\bdeposit\b.{{0,100}}\b(?:on\s+)?(?:cp\s+)?(?:website|site)\b|"
            rf"\bdeposit\b.{{0,80}}\b(?:fail(?:ed|ure|ing)?|error|issue|issues|problem|cannot|can't|cant|"
            r"not\s+working|unavailable|stuck|pending|rejected|declined)\b|"
            rf"\b(?:fail(?:ed|ure|ing)?|error|issue|issues|problem|cannot|can't|cant|not\s+working)\b.{{0,80}}\bdeposit\b|"
            r"\b(?:top\s*up|topup|recharge|add\s+funds?)\b.{0,80}\b(?:fail|error|issue|cannot|can't|cant|not\s+working|problem)\b|"
            r"存款失败|无法存款|不能存款|充值失败|无法充值|充值不了|存款问题"
        ),
        ["deposit_issues"],
        "deposit_failure",
        0.93,
        "Players cannot deposit on CP website",
    ),
    (
        re.compile(
            r"(?is)"
            rf"\b{_NEGATED}\s+login\b|"
            rf"\bplayers?\b.{{0,140}}{_NEGATED}\s+login\b|"
            r"\blogin\b.{0,80}\b(?:on\s+)?(?:cp\s+)?(?:website|site)\b|"
            r"\blogin\s+(?:fail|error|issue|problem|broken)\b|"
            r"\botp\b.{0,40}\b(?:fail|not\s+received|invalid|error)\b|"
            r"无法登录|登录失败|验证码"
        ),
        ["login_issues"],
        "login_failure",
        0.9,
        "Players cannot login on CP website",
    ),
    (
        re.compile(r"(?is)\b(?:cannot|can't|unable\s+to)\s+register\b|注册失败|无法注册"),
        ["registration_failures"],
        "registration_failure",
        0.9,
        "Registration failure reported",
    ),
    (
        re.compile(
            r"(?is)"
            rf"\b{_NEGATED}\s+withdraw\b|"
            rf"\bplayers?\b.{{0,140}}{_NEGATED}\s+withdraw\b|"
            r"\bwithdraw(?:al)?\b.{0,60}\b(?:fail|error|issue|cannot|can't|cant|balance|fund|money|problem)\b|"
            rf"\b(\d+)\s+players?\b.{{0,120}}{_NEGATED}\s+withdraw|"
            r"提款失败|无法提款|不能提款|无法提现"
        ),
        ["withdrawal_issues"],
        "withdrawal_failure",
        0.93,
        "Withdrawal issue reported",
    ),
    (
        re.compile(r"(?is)\b(?:fpms|pms)\b.{0,50}\b(?:down|unreachable|cannot|can't|not\s+working|offline)\b|后台.{0,20}(?:挂|不可用|进不去)"),
        ["backend_downtime"],
        "backend_downtime",
        0.92,
        "FPMS/PMS backend issue reported",
    ),
    (
        re.compile(
            r"(?is)\b(?:all|every)\s+games?\b.{0,40}\b(?:down|cannot|can't|unplayable|not\s+working)\b|"
            rf"\b{_NEGATED}\s+(?:enter|join|access)\b.{0,60}\b(?:game|event|lobby|table|room)\b|"
            r"\b(?:game|event|lobby)\b.{0,50}\b(?:not\s+clickable|unable\s+to\s+enter|cannot\s+enter|"
            r"can't\s+enter|cant\s+enter|unplayable|not\s+working)\b|"
            r"无法进入游戏|无法进入|游戏.{0,10}(?:进不去|打不开|全部)|活动.{0,10}(?:进不去|打不开)"
        ),
        ["gameplay_outage"],
        "gameplay_entry_failure",
        0.9,
        "Players unable to enter games or events",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:promo(?:tion)?|voucher|coupon|campaign|bonus\s+code)\b.{0,80}\b(?:fail|error|issue|"
            r"problem|not\s+working|cannot|can't|cant|invalid|expired|not\s+applied|missing)\b|"
            r"\b(?:fail|error|issue|problem|not\s+working|cannot|can't|cant|invalid|not\s+applied)\b.{0,80}"
            r"\b(?:promo(?:tion)?|voucher|coupon|campaign)\b|"
            r"优惠(?:券|码)?.{0,20}(?:失败|无法|不能|领不到|用不了)|代金券|促销"
        ),
        ["promotion_voucher"],
        "promotion_voucher_failure",
        0.9,
        "Promotion or voucher issue reported",
    ),
    (
        re.compile(
            r"(?is)"
            r"\brebate\b.{0,80}\b(?:fail|error|issue|problem|not\s+received|missing|wrong|incorrect|"
            r"cannot|can't|cant|not\s+credited)\b|"
            r"\b(?:fail|error|issue|problem|not\s+received|missing|wrong|not\s+credited)\b.{0,80}\brebate\b|"
            r"\bcashback\b.{0,60}\b(?:fail|error|issue|not\s+received|missing|wrong)\b|"
            r"返水.{0,20}(?:失败|无法|没有|不对|未到账)|返利"
        ),
        ["rebate_issues"],
        "rebate_failure",
        0.9,
        "Rebate or cashback issue reported",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:lucky\s*coin|luckycoin)\b.{0,80}\b(?:fail|error|issue|problem|not\s+received|missing|"
            r"wrong|cannot|can't|cant|not\s+credited|redeem)\b|"
            r"\b(?:fail|error|issue|problem|not\s+received|missing|wrong|redeem)\b.{0,80}"
            r"\b(?:lucky\s*coin|luckycoin)\b|"
            r"幸运币.{0,20}(?:失败|无法|没有|不对|未到账|兑换)"
        ),
        ["luckycoin_issues"],
        "luckycoin_failure",
        0.9,
        "LuckyCoin issue reported",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:company|financial|player)\s+loss\b|"
            r"\b(?:duplicate|double|wrong|incorrect|over)\s+(?:credit|payout|payment|settlement|pay)\b|"
            r"\b(?:mass|multiple|many)\s+players?\b.{0,80}\b(?:wrong|incorrect|duplicate|over)\b.{0,40}"
            r"\b(?:credit|payout|payment|settlement)\b|"
            r"公司.{0,10}损失|重复.{0,10}(?:派发|到账|入账)|多.{0,6}(?:派|发|付)"
        ),
        ["company_loss"],
        "company_financial_loss",
        0.93,
        "Issue may cause company or player financial loss",
    ),
)

_ALLOWED_CATEGORIES = frozenset(
    {
        "website_downtime",
        "login_issues",
        "registration_failures",
        "withdrawal_issues",
        "deposit_issues",
        "backend_downtime",
        "gameplay_outage",
        "promotion_voucher",
        "rebate_issues",
        "luckycoin_issues",
        "company_loss",
        "widespread_impact",
    }
)


def _anthropic_configured() -> bool:
    from p0_logic.anthropic_client import has_anthropic_auth

    return has_anthropic_auth()


def _groq_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("GROQ_API_KEY") or "").strip()


def issue_watch_ai_providers_to_try() -> List[str]:
    """Public alias — the same ``P0_ISSUE_WATCH_AI_PROVIDER`` chain the classifier uses, so other
    Issue Watch AI calls (e.g. the declare thread reply) honour one provider setting."""
    return _issue_watch_ai_providers_to_try()


def _issue_watch_ai_providers_to_try() -> List[str]:
    """
    LLM providers to attempt (in order) before keyword fallback.

    ``P0_ISSUE_WATCH_AI_PROVIDER``:
    - ``auto`` (default): Claude if key set, else Groq; on Claude fail try Groq
    - ``claude``: Claude first, then Groq on fail
    - ``groq``: Groq first, then Claude on fail
    """
    _config.reload_env_runtime()
    raw = (os.getenv("P0_ISSUE_WATCH_AI_PROVIDER") or "auto").strip().lower()
    has_claude = _anthropic_configured()
    has_groq = bool(_groq_key())
    if raw == "groq":
        order: List[str] = []
        if has_groq:
            order.append("groq")
        if has_claude:
            order.append("claude")
        return order
    if raw == "claude":
        order = []
        if has_claude:
            order.append("claude")
        if has_groq:
            order.append("groq")
        return order
    # auto
    if has_claude:
        order = ["claude"]
        if has_groq:
            order.append("groq")
        return order
    if has_groq:
        return ["groq"]
    return []


def resolve_issue_watch_ai_provider() -> str:
    """Primary Issue Watch LLM provider, or empty when none configured."""
    providers = _issue_watch_ai_providers_to_try()
    return providers[0] if providers else ""


def _norm_confidence(raw: object) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if c > 1.0:
        c = c / 100.0
    return max(0.0, min(1.0, c))


def _norm_categories(raw: object) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if key in _ALLOWED_CATEGORIES and key not in out:
            out.append(key)
    return out


def _parse_classification(raw: str, provider: str) -> Optional[dict]:
    obj = _parse_json_object(raw or "")
    if not obj:
        log.warning("classify_issue_watch (%s): JSON parse failed head=%s", provider, (raw or "")[:200])
        return None
    signal = bool(obj.get("is_incident_signal"))
    categories = _norm_categories(obj.get("categories"))
    confidence = _norm_confidence(obj.get("confidence"))
    summary = str(obj.get("summary") or "").strip()[:400]
    fingerprint = str(obj.get("issue_fingerprint") or "").strip().lower()[:80]
    fingerprint = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in fingerprint).strip("_")
    try:
        players = int(obj.get("players_mentioned_in_message") or 0)
    except (TypeError, ValueError):
        players = 0
    players = max(0, min(players, 50))
    reason = str(obj.get("reason") or "").strip()[:300]
    if signal and not categories:
        log.warning("classify_issue_watch (%s): signal=true but no categories", provider)
        return None
    if signal and not fingerprint:
        fingerprint = categories[0] if categories else "unknown_issue"
    return {
        "is_incident_signal": signal,
        "categories": categories,
        "confidence": confidence,
        "summary": summary,
        "issue_fingerprint": fingerprint,
        "players_mentioned_in_message": players,
        "reason": reason,
        "provider": provider,
    }


def extract_player_ids(text: str) -> List[str]:
    """
    Sorted unique player/account IDs from detection chat.

    - Default: 10-digit IDs.
    - ``Account:`` / follow-up lists often mix 7–10 digit IDs on separate lines.
    """
    t = (text or "").strip()
    if not t:
        return []
    ids_10 = set(re.findall(r"\b\d{10}\b", t))
    if ids_10 or re.search(r"(?i)\baccount\b", t):
        loose = set(re.findall(r"\b\d{7,10}\b", t))
        return sorted(loose, key=lambda x: (len(x), x))
    return sorted(ids_10)


def _extract_player_mentions(text: str) -> int:
    """``4 players`` in prose, ID list count, or plural ``players`` without a number."""
    t = (text or "").strip()
    ids = extract_player_ids(t)
    if ids:
        return len(ids)
    m = re.search(r"(?is)\b(\d+)\s+players?\b", t)
    if m:
        try:
            return max(0, int(m.group(1)))
        except ValueError:
            pass
    if re.search(r"(?is)\bplayers\b", t):
        return 0
    if re.search(r"(?is)\bplayer\b", t):
        return 1
    return 0


def _summary_with_players(
    base_summary: str,
    categories: List[str],
    players: int,
    text: str,
) -> str:
    if players >= 1:
        if "login_issues" in categories:
            return "Players cannot login on CP website"
        if "withdrawal_issues" in categories:
            return "Players cannot withdraw"
        if "deposit_issues" in categories:
            return "Players cannot deposit on CP website"
        cleaned = re.sub(r"(?i)^\d+\s+players?\s+", "", (base_summary or "").strip())
        return cleaned or base_summary
    if re.search(r"(?is)\bplayers\b", text) and "deposit_issues" in categories:
        return "Players cannot deposit on CP website"
    if re.search(r"(?is)\bplayers\b", text) and "login_issues" in categories:
        return "Players cannot login on CP website"
    return base_summary


_MAINTENANCE_TEST_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?is)\btest\s+during\s+maintenance\b"),
    re.compile(r"(?is)\bduring\s+(?:\w+\s+){0,4}maintenance\b"),
    re.compile(r"(?is)\bmaintenance\s+(?:icon|mode|window|period|scheduled|test)\b"),
    re.compile(r"(?is)\bset\s+(?:back\s+)?to\s+maintenance\b"),
    re.compile(r"(?is)\b(?:back|put)\s+(?:on|to)\s+maintenance\b"),
    re.compile(r"(?is)\bstill\s+(?:on\s+)?maintenance\b"),
    re.compile(r"(?is)\bscheduled\s+maintenance\b"),
    re.compile(r"(?is)\bunder\s+maintenance\b"),
    re.compile(r"(?is)\bmaintenance\s+icon\b"),
    re.compile(r"(?is)\b(?:in|on)\s+maintenance\b"),
)


def is_maintenance_or_test_message(text: str) -> bool:
    """Planned maintenance / test chatter — never a Major P0 detection signal."""
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in _MAINTENANCE_TEST_PATTERNS)


def _is_non_incident_status_update(text: str) -> bool:
    """Staff/player confirms success or explicitly no problem — not a detection signal."""
    t = (text or "").strip()
    if not t:
        return True
    status_patterns = (
        r"(?is)\bwithout\s+(?:encountering\s+)?(?:any\s+)?(?:issue|issues|problem|problems)\b",
        r"(?is)\bno\s+(?:issue|issues|problem|problems)\b",
        r"(?is)\bnot\s+(?:encountering|experiencing|having)\s+(?:any\s+)?(?:issue|issues|problem|problems)\b",
        r"(?is)\b(?:were\s+)?able\s+to\s+(?:withdraw|deposit|login|register)\b",
        r"(?is)\b(?:withdraw|deposit|login|registration)\b.{0,50}\b(?:working|works|fine|ok|okay|successful|successfully)\b",
        r"(?is)\bworking\s+(?:fine|well|normally|as\s+expected)\b",
        r"(?is)\b(?:resolved|fixed|already\s+(?:fixed|resolved)|back\s+to\s+normal)\b",
    )
    return any(re.search(p, t) for p in status_patterns)


def _keyword_classify(message_text: str) -> Optional[dict]:
    t = (message_text or "").strip()
    if not t:
        return None
    if _is_non_incident_status_update(t):
        return None
    for pattern, categories, fingerprint, confidence, summary in _KEYWORD_RULES:
        if not pattern.search(t):
            continue
        players = _extract_player_mentions(t)
        player_ids = extract_player_ids(t)
        cats = list(categories)
        min_affected = _config.get_p0_issue_watch_min_affected_players()
        if players >= min_affected and "widespread_impact" not in cats:
            cats.append("widespread_impact")
        summ = _summary_with_players(summary, cats, players, t)
        out = {
            "is_incident_signal": True,
            "categories": cats,
            "confidence": confidence,
            "summary": summ,
            "issue_fingerprint": fingerprint,
            "players_mentioned_in_message": players,
            "reason": "keyword rule match",
            "provider": "keyword",
        }
        if player_ids:
            out["player_ids"] = player_ids
        return out
    return None


def _classify_via_claude(message_text: str) -> Optional[dict]:
    from p0_logic.anthropic_client import anthropic_chat_once

    raw = anthropic_chat_once(_ISSUE_WATCH_SYSTEM, f"MESSAGE:\n{message_text[:4500]}", max_tokens=500)
    if not (raw or "").strip():
        return None
    return _parse_classification(raw, "claude")


def _classify_via_groq(message_text: str) -> Optional[dict]:
    raw = groq_chat_once(_ISSUE_WATCH_SYSTEM, f"MESSAGE:\n{message_text[:4500]}", max_tokens=500)
    if not (raw or "").strip():
        return None
    return _parse_classification(raw, "groq")


def _classify_via_provider(provider: str, message_text: str) -> Optional[dict]:
    if provider == "claude":
        return _classify_via_claude(message_text)
    if provider == "groq":
        return _classify_via_groq(message_text)
    return None


_SOP_CHECK_SYSTEM = (
    "You are checking one candidate incident against THIS COMPANY'S OWN P0 SOP.\n"
    "You are given the SOP, the chat message, and a first-pass classification.\n"
    "Answer only: does the SOP treat this as a MAJOR P0 that on-call duty must be paged for?\n"
    "HARD RULE, overrides everything else including the SOP text: if 4 OR MORE players are "
    "affected, answer is_major_p0=true. Never downgrade a 4+ player issue for being limited to one "
    "provider, one channel or one payment method.\n"
    "Otherwise the SOP wins over your own judgement. If the SOP does not cover it, keep the "
    "first pass.\n"
    'Output ONLY valid JSON: {"is_major_p0": true|false, "reason": "one short sentence"}'
)


def _apply_sop_check(message_text: str, ai: dict, provider: str) -> dict:
    """RAG second stage — let the P0 SOP veto a positive classification.

    Returns ``ai`` unchanged when RAG is off, the SOP has nothing relevant to say, or anything
    fails: detection must never get *worse* because retrieval had a bad day.
    """
    if not _config.get_p0_issue_watch_rag_enabled():
        return ai
    # 4+ affected players is the company rule and is not up for debate — skip the SOP check
    # entirely so no retrieved passage ("one provider = Minor") can veto a confirmed major P0.
    min_affected = _config.get_p0_issue_watch_min_affected_players()
    try:
        stated = int(ai.get("players_mentioned_in_message") or 0)
    except (TypeError, ValueError):
        stated = 0
    players = max(stated, len(extract_player_ids(message_text)))
    if players >= min_affected:
        log.info(
            "issue_watch_rag: SOP check skipped — %s players affected (>= %s), major P0 by rule",
            players,
            min_affected,
        )
        return ai
    try:
        from . import issue_watch_rag as _rag

        sop = _rag.sop_context_for_message(message_text)
        if not sop:
            return ai
        user = (
            f"{sop}\n\nCHAT MESSAGE:\n{message_text[:2500]}\n\n"
            f"FIRST-PASS: categories={ai.get('categories')} "
            f"confidence={ai.get('confidence')} summary={ai.get('summary')!r}\n\n"
            "Per the SOP above, is this a MAJOR P0?"
        )
        raw = _run_classifier_provider(provider, _SOP_CHECK_SYSTEM, user, max_tokens=160)
        obj = _parse_json_object(raw or "")
        if not obj or "is_major_p0" not in obj:
            return ai
        if bool(obj.get("is_major_p0")):
            log.info("issue_watch_rag: SOP confirms major P0 — %s", (obj.get("reason") or "")[:120])
            return ai
        out = dict(ai)
        out["is_incident_signal"] = False
        out["confidence"] = 0.0
        out["reason"] = f"SOP says not a major P0: {(obj.get('reason') or '').strip()[:160]}"
        out["provider"] = f"{provider}+sop"
        log.info("issue_watch_rag: SOP VETO — %s", out["reason"][:160])
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_rag: SOP check failed (keeping first pass): %s", e)
        return ai


def _run_classifier_provider(provider: str, system: str, user: str, *, max_tokens: int) -> str:
    if provider == "claude":
        from p0_logic.anthropic_client import anthropic_chat_once

        return anthropic_chat_once(system, user, max_tokens=max_tokens)
    return groq_chat_once(system, user, max_tokens=max_tokens)


def classify_issue_watch_message(message_text: str) -> Optional[dict]:
    """
    LLM triage with failover: Claude and/or Groq (per env), then keyword rules.
    """
    t = (message_text or "").strip()
    if not t:
        return None
    if is_maintenance_or_test_message(t):
        return {
            "is_incident_signal": False,
            "categories": [],
            "confidence": 0.0,
            "summary": "",
            "issue_fingerprint": "",
            "players_mentioned_in_message": 0,
            "reason": "maintenance or test-during-maintenance — not a production incident",
            "provider": "maintenance_guard",
        }
    providers = _issue_watch_ai_providers_to_try()
    if not providers:
        log.warning("issue_watch_ai: no Claude/GROQ auth — keyword rules only (more false positives)")
    for i, provider in enumerate(providers):
        ai = _classify_via_provider(provider, t)
        if ai is not None:
            log.info(
                "issue_watch_ai: %s decision signal=%s categories=%s conf=%.2f reason=%r",
                provider,
                ai.get("is_incident_signal"),
                ai.get("categories"),
                float(ai.get("confidence") or 0),
                (ai.get("reason") or "")[:160],
            )
            # Second stage: only a POSITIVE gets checked against the SOP, so the embedding + extra
            # model call happen on the rare candidate rather than on every line of group chat.
            # It can only downgrade a signal, never create one.
            if ai.get("is_incident_signal"):
                ai = _apply_sop_check(t, ai, provider)
            return ai
        if i + 1 < len(providers):
            log.warning("issue_watch_ai: %s classify failed — trying %s", provider, providers[i + 1])
        else:
            log.warning("issue_watch_ai: %s classify failed — falling back to keyword rules", provider)
    kw = _keyword_classify(t)
    if kw and kw.get("is_incident_signal"):
        log.info(
            "issue_watch_ai: keyword fallback categories=%s fp=%s",
            kw.get("categories"),
            kw.get("issue_fingerprint"),
        )
        return kw
    if _is_non_incident_status_update(t):
        return {
            "is_incident_signal": False,
            "categories": [],
            "confidence": 0.0,
            "summary": "",
            "issue_fingerprint": "",
            "players_mentioned_in_message": 0,
            "reason": "status update: no issue / working normally",
            "provider": "negation_guard",
        }
    return kw
