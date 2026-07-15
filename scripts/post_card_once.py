#!/usr/bin/env python3
"""One-shot renderer/poster for **every** card & text prompt the bot sends.

Instead of a separate script per card, this dispatches to any builder in
``p0_logic/cards.py`` by name, fills sensible sample args, and either prints the
JSON (dry-run) or posts it to a chat (``oc_...``) or a DM (``ou_...``).

Usage:
  # list every prompt you can test
  python3 scripts/post_card_once.py --list

  # dry-run (prints the rendered card/text JSON, sends nothing)
  python3 scripts/post_card_once.py --card meeting_ended

  # actually post to a group or a DM
  python3 scripts/post_card_once.py --card meeting_ended --to oc_XXXX --post
  python3 scripts/post_card_once.py --card p1_meeting_confirm --to ou_XXXX --post

  # override any builder kwarg
  python3 scripts/post_card_once.py --card meeting_cancelled --set priority=P1 --set reason="rolled back" --to oc_XXXX --post
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config

config.apply_env_layers()

from p0_logic import cards as C
from p0_logic import lark_client as lark

# name -> (builder, kwargs, delivery)   delivery: "chat" (oc_) or "dm" (ou_)
REGISTRY = {
    # --- meeting lifecycle -------------------------------------------------
    "meeting_created_text":  (C.build_p0_meeting_created_text, dict(link="https://vc.example/abc", priority="P0", emergency_topic="CP site down"), "chat"),
    "meeting_link_notice":   (C.build_meeting_link_notice_card, dict(link="https://vc.example/abc", priority="P0", emergency_topic="CP site down"), "chat"),
    "meeting_created_link":  (C.build_p0_meeting_created_link_card, dict(link="https://vc.example/abc", emergency_topic="CP site down"), "chat"),
    "meeting_card":          (C.build_meeting_card, dict(link="https://vc.example/abc", meeting_no="885910443", priority="P0", affected_players="DOUBLE2", emergency_topic="CP site down"), "chat"),
    "ongoing_meeting":       (C.build_ongoing_meeting_card, dict(meeting_no="885910443", participant_departments_line="OM, SRE", priority="P0", emergency_topic="CP site down"), "chat"),
    "meeting_link_ended":    (C.build_meeting_link_ended_card, dict(priority="P0", duration_text="12m 30s", meeting_no="885910443"), "chat"),
    "meeting_link_cancelled":(C.build_meeting_link_cancelled_card, dict(priority="P0", duration_text="2m", meeting_no="885910443"), "chat"),
    "meeting_ended":         (C.build_meeting_ended_card, dict(meeting_no="885910443", duration_text="12m 30s", priority="P0"), "chat"),
    "meeting_cancelled":     (C.build_meeting_cancelled_card, dict(meeting_no="885910443", duration_text="2m", priority="P0"), "chat"),
    "no_active_p0_session":  (C.build_no_active_p0_session_card, dict(mode="end"), "chat"),

    # --- recording ----------------------------------------------------------
    "recording_ready_text":  (C.build_recording_ready_meta_text, dict(topic="P0 detection dev", meeting_no="885910443", meeting_id="7654187624064077532", recording_url="https://example.com/minutes", duration_text="30m"), "chat"),
    "recording_available_text": (C.build_recording_available_text, dict(topic="P0 detection dev", meeting_no="885910443", meeting_id="7654187624064077532", recording_url="https://example.com/minutes", duration_text="30m"), "chat"),
    "recording_available":   (C.build_recording_available_card, dict(topic="P0 detection dev", meeting_no="885910443", meeting_id="7654187624064077532", recording_url="https://example.com/minutes", duration_text="30m"), "chat"),

    # --- P0/P1 buzz & confirms ---------------------------------------------
    "ongoing_dm_buzz_major": (C.build_p0_ongoing_dm_buzz_card, dict(source_chat_label="P0 detection dev", meeting_no="885910443", duration_text="5 minutes", severity_tier="major"), "dm"),
    "ongoing_dm_buzz_minor": (C.build_p0_ongoing_dm_buzz_card, dict(source_chat_label="P0 detection dev", meeting_no="885910443", duration_text="10 minutes", severity_tier="minor"), "dm"),
    "p1_fifteen_min_confirm":(C.build_p1_fifteen_min_confirm_card, dict(meeting_no="885910443"), "chat"),
    "p1_escalated":          (C.build_p1_escalated_card, dict(meeting_no="885910443"), "chat"),
    "p1_meeting_confirm":    (C.build_p1_meeting_confirm_card, dict(confirm_nonce="test-nonce-123"), "dm"),
    "keyword_confirm_dm":    (C.build_p0_keyword_confirm_dm_card, dict(nonce="test-nonce-123", phrase="this is p0", source_chat_name="P0 detection dev"), "dm"),
    "keyword_confirm_result":(C.build_p0_keyword_confirm_result_card, dict(text="P0 meeting created.", title="P0 mention confirmation"), "dm"),
    "keyword_confirm_dismissed": (C.build_p0_keyword_confirm_dismissed_card, dict(nonce="test-nonce-123"), "dm"),
    "keyword_confirm_created": (C.build_p0_keyword_confirm_created_card, dict(source_chat_id="oc_example"), "dm"),

    # --- overview -----------------------------------------------------------
    "dm_instruction":        (C.build_dm_instruction_card, dict(priority="P0", source_chat_label="P0 detection dev", target_chat="oc_example"), "dm"),
    "overview_result":       (C.build_overview_result_card, dict(md="**Issue**: sample\n**Impact**: sample\n**Support**: sample", priority="P0", source_chat_label="P0 detection dev"), "dm"),
    "dm_overview_sent":      (C.build_dm_overview_sent_card, dict(priority="P0", source_chat_label="P0 detection dev"), "dm"),
    "preview":               (C.build_preview_card, dict(md="**Issue**: sample\n**Impact**: sample\n**Support**: sample", priority="P0", source_chat_label="P0 detection dev"), "dm"),
    "edit_overview":         (C.build_edit_overview_card, dict(current_issue="sample issue", current_impact="sample impact", current_support="sample support", priority="P0", source_chat_label="P0 detection dev"), "dm"),
    "adjustment_bitable":    (C.build_adjustment_bitable_card, dict(body_md="- svc-a → v1.2.3\n- svc-b → v0.9.1", count=2, title="Deployments", window_label="last 6h", hours=6), "chat"),

    # --- issue watch --------------------------------------------------------
    "issue_watch_declare_overview_hint": (C.build_issue_watch_declare_overview_hint_text, dict(), "chat"),
    "issue_watch_declare_followup":      (C.build_issue_watch_declare_followup_text, dict(), "chat"),
    "issue_watch_alert":     (C.build_issue_watch_alert_card, dict(group_label="CP OM", categories_md="- loading issue", summary="Users report CP site slow", concern="Multiple reports", alert_time="2026-07-14 10:41"), "chat"),
    "issue_watch_declare_manual": (C.build_issue_watch_declare_manual_card, dict(source_chat_label="P0 detection dev"), "chat"),

    # --- help & monitoring --------------------------------------------------
    "help_commands":         (C.build_help_commands_card, dict(), "chat"),
    "monitoring_duty":        (C.build_monitoring_duty_card, dict(text="Overview send blocked: missing Impact field", label="duty warning"), "chat"),
    "monitoring_log":         (C.build_monitoring_log_card, dict(message="Bitable post failed HTTP=500", level="ERROR", logger_name="p0_logic.handlers"), "chat"),
}


def _coerce(v: str):
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if v.strip().lstrip("-").isdigit():
        return int(v)
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", help="which prompt to render (see --list)")
    ap.add_argument("--list", action="store_true", help="list every testable prompt")
    ap.add_argument("--to", default="", help="destination oc_... (chat) or ou_... (DM)")
    ap.add_argument("--set", action="append", default=[], metavar="key=value", help="override a builder kwarg (repeatable)")
    ap.add_argument("--post", action="store_true", help="actually send (else dry-run prints JSON)")
    args = ap.parse_args()

    if args.list or not args.card:
        print("Testable prompts (--card NAME):\n")
        for name in sorted(REGISTRY):
            builder, _kw, delivery = REGISTRY[name]
            dest = "→ oc_ chat" if delivery == "chat" else "→ ou_ DM"
            print(f"  {name:<34} {dest}   [{builder.__name__}]")
        print("\nExample: python3 scripts/post_card_once.py --card meeting_ended --to oc_XXXX --post")
        return 0

    if args.card not in REGISTRY:
        print(f"Unknown card {args.card!r}. Run --list to see all.", file=sys.stderr)
        return 1

    builder, kwargs, delivery = REGISTRY[args.card]
    kwargs = dict(kwargs)
    for pair in args.set:
        if "=" not in pair:
            print(f"--set expects key=value, got {pair!r}", file=sys.stderr)
            return 1
        k, v = pair.split("=", 1)
        kwargs[k.strip()] = _coerce(v)

    payload = builder(**kwargs)
    is_text = isinstance(payload, str)

    print(f"card={args.card} builder={builder.__name__} delivery={delivery} kind={'text' if is_text else 'card'}")
    if not args.post:
        print("--- DRY RUN (no --post) ---")
        print(payload if is_text else json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    to = args.to.strip()
    if not to:
        print("--post needs --to oc_... (chat) or ou_... (DM)", file=sys.stderr)
        return 1

    token = lark.get_tenant_token_primary()
    if not token:
        print("No primary tenant token — check LARK_APP_ID/SECRET in .env", file=sys.stderr)
        return 1

    is_dm = to.startswith("ou_")
    if is_text:
        st, body = (lark.post_text_to_open_id(to, token, payload) if is_dm
                    else lark.post_text_to_chat(to, token, payload))
        mid = ""
    else:
        res = (lark.post_card_to_open_id(to, token, payload) if is_dm
               else lark.post_card_to_chat(to, token, payload))
        st, body, mid = res

    ok = st == 200
    print(f"{'SENT' if ok else 'FAILED'} to={to} HTTP={st} message_id={mid} body={(body or '')[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
