"""
VC auto-ring: invite @mentioned users when duty joins an ongoing P0 meeting.

Requires ``P0_VC_RING_ENABLED=1``, duty ``user_access_token`` (OAuth), and
``PATCH /vc/v1/meetings/{meeting_id}/invite``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from p0_logic import cards as _cards
from p0_logic import config as _config
from p0_logic import lark_client as _lark
from features.session import session as _session
from . import vc_user_oauth as _oauth

log = logging.getLogger("lark-ops-ai")

_DUTY_MENTION_LOCK = threading.Lock()
# detection chat_id -> {open_id, ids[], ts}
_DUTY_MENTIONS_BY_CHAT: Dict[str, Dict[str, Any]] = {}

# Re-ring scheduler (P0_VC_RING_RETRY_ENABLED): ring an invited-but-not-joined user again, up to
# P0_VC_RING_RETRY_MAX_ATTEMPTS total, spaced P0_VC_RING_RETRY_INTERVAL_SEC apart, stopping on join.
# chat_id -> {meeting_id, targets:{open_id:{attempts:int}}, joined:set[open_id], timer:threading.Timer}
_RERING_LOCK = threading.Lock()
_RERING_STATE: Dict[str, Dict[str, Any]] = {}


def _is_duty_open_id(open_id: str) -> bool:
    oid = (open_id or "").strip()
    if not oid:
        return False
    allowed = set(_config.get_dm_instruction_open_ids())
    return oid in allowed if allowed else True


def note_duty_mentions_in_chat(
    chat_id: str,
    duty_open_id: str,
    mention_open_ids: List[str],
    *,
    tenant_token: str = "",
) -> None:
    """Duty @mentioned users in detection group — merged on declare and during active P0."""
    if not _config.get_p0_vc_ring_enabled():
        return
    cid = (chat_id or "").strip()
    oid = (duty_open_id or "").strip()
    if not cid or not oid or not _is_duty_open_id(oid):
        return
    ids = _filter_ring_targets(mention_open_ids, operator_open_id=oid)
    if not ids:
        return
    with _DUTY_MENTION_LOCK:
        _DUTY_MENTIONS_BY_CHAT[cid] = {"open_id": oid, "ids": ids, "ts": time.time()}
    log.info(
        "vc_ring: duty mentions stored chat_tail=%s duty_tail=%s targets=%s",
        cid[-12:] if len(cid) > 12 else cid,
        oid[-8:],
        len(ids),
    )
    _merge_duty_mentions_into_active_session(
        cid, ids, operator_open_id=oid, tenant_token=tenant_token
    )


def _pending_ring_targets(sess: Dict[str, Any]) -> List[str]:
    """Targets not yet successfully invited this P0 session."""
    all_targets = list(sess.get("vc_ring_target_open_ids") or [])
    invited = {str(x).strip() for x in (sess.get("vc_ring_invited_open_ids") or []) if str(x).strip()}
    return [x for x in all_targets if x not in invited]


def _merge_duty_mentions_into_active_session(
    chat_id: str,
    new_ids: List[str],
    *,
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """If P0 is already live, append duty @mentions and ring new users when VC is active."""
    cid = (chat_id or "").strip()
    if not cid or not new_ids:
        return
    sess = _session.P0_SESSIONS.get(cid)
    if not isinstance(sess, dict):
        return
    existing = list(sess.get("vc_ring_target_open_ids") or [])
    trigger = str(sess.get("trigger_open_id") or operator_open_id or "").strip()
    merged = _filter_ring_targets(existing + list(new_ids), operator_open_id=trigger)
    if merged == existing:
        return
    newly_added = [x for x in merged if x not in existing]
    sess["vc_ring_target_open_ids"] = merged
    _session.P0_SESSIONS[cid] = sess
    if _session._session_disk.enabled():
        _session._session_disk.save_session(cid, sess)
    log.info(
        "vc_ring: updated active session ring targets count=%s (was %s) new=%s chat_tail=%s",
        len(merged),
        len(existing),
        len(newly_added),
        cid[-12:] if len(cid) > 12 else cid,
    )
    meeting_id = str(sess.get("meeting_id") or "").strip()
    if newly_added and meeting_id and trigger:
        _try_ring_session(
            cid,
            sess,
            declarer_open_id=trigger,
            meeting_ref=meeting_id,
            tenant_token=tenant_token,
        )
    elif newly_added:
        log.info(
            "vc_ring: new targets queued — will ring when declarer joins VC chat_tail=%s",
            cid[-12:] if len(cid) > 12 else cid,
        )


def pop_duty_mentions_for_chat(chat_id: str) -> List[str]:
    cid = (chat_id or "").strip()
    if not cid:
        return []
    with _DUTY_MENTION_LOCK:
        row = _DUTY_MENTIONS_BY_CHAT.pop(cid, None)
    if not row:
        return []
    # Expire after 2h
    if time.time() - float(row.get("ts") or 0) > 7200:
        return []
    return list(row.get("ids") or [])


def invite_open_ids_into_active_meeting(
    chat_id: str,
    target_open_ids: List[str],
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
) -> str:
    """Ring/invite ``target_open_ids`` into the chat's ALREADY-active P0 meeting.

    Used by the ``@bot`` ring commands (m / e / scpms / sfpms / sfe). Reuses the VC-ring
    merge path so each invitee's Lark app rings via ``invite_users_to_vc_meeting`` (which
    needs the declarer's OAuth token). Does NOT create a meeting.

    Returns a status string for the caller to render:
      ``disabled``     — P0_VC_RING_ENABLED is off
      ``no_session``   — no active P0 session / meeting for this chat
      ``no_targets``   — nothing valid to ring (empty or all filtered out)
      ``ringing``      — targets merged and the declarer is authorized → ring attempted now
      ``queued_oauth`` — targets merged but the declarer must finish OAuth first (DM sent)
    """
    if not _config.get_p0_vc_ring_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    if not cid:
        return "no_session"
    sess = _session.P0_SESSIONS.get(cid)
    if not isinstance(sess, dict):
        return "no_session"
    if not str(sess.get("meeting_id") or "").strip():
        return "no_session"
    raw = [str(x).strip() for x in (target_open_ids or []) if str(x).strip()]
    if not raw:
        return "no_targets"
    trigger = str(sess.get("trigger_open_id") or operator_open_id or "").strip()
    if not _filter_ring_targets(raw, operator_open_id=trigger):
        return "no_targets"
    _merge_duty_mentions_into_active_session(
        cid, raw, operator_open_id=operator_open_id, tenant_token=tenant_token
    )
    # The actual ring uses a fixed inviter's OAuth (or the declarer's when none is configured);
    # report whether it can fire now or is queued.
    inviter = _resolve_ring_inviter(sess, fallback=trigger)
    if inviter and _oauth.get_user_access_token(inviter):
        return "ringing"
    return "queued_oauth"


def force_reinvite_open_ids(
    chat_id: str,
    open_ids: List[str],
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
    track_rering: bool = False,
) -> str:
    """Re-send a VC invite to ``open_ids`` into the active meeting EVEN IF already invited.

    The normal ``invite_open_ids_into_active_meeting`` path dedupes (only rings NEWLY-added targets),
    so it no-ops when re-ringing the same person. This rings them DIRECTLY via the inviter's OAuth —
    used by the SRE-game escalation (/srebac … /r retry, /n next) so each step actually re-invites,
    and by the direct ``/c`` call command.

    ``track_rering`` registers the targets with the re-ring scheduler (P0_VC_RING_RETRY_*) so they are
    paged again if they don't join — set by ``/c`` but NOT by the SRE game (it runs its own escalation).

    Returns: ``disabled`` / ``no_session`` / ``no_targets`` / ``queued_oauth`` / ``ringing`` / ``failed``.
    """
    if not _config.get_p0_vc_ring_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    sess = _session.P0_SESSIONS.get(cid)
    if not isinstance(sess, dict):
        return "no_session"
    meeting_id = str(sess.get("meeting_id") or "").strip()
    if not meeting_id:
        return "no_session"
    trigger = str(sess.get("trigger_open_id") or operator_open_id or "").strip()
    raw = _filter_ring_targets(
        [str(x).strip() for x in (open_ids or []) if str(x).strip()], operator_open_id=trigger
    )
    if not raw:
        return "no_targets"
    inviter = _resolve_ring_inviter(sess, fallback=trigger)
    user_tok = _oauth.get_user_access_token(inviter)
    if not user_tok:
        maybe_prompt_oauth_dm(inviter, tenant_token)
        log.warning("vc_ring: force re-invite queued — no user_access_token for inviter_tail=%s", inviter[-8:])
        return "queued_oauth"
    ok, detail = _lark.invite_users_to_vc_meeting(user_tok, meeting_id, raw)
    log.info(
        "vc_ring: force re-invite count=%s ok=%s meeting_tail=%s detail=%s",
        len(raw), ok, meeting_id[-8:], (detail or "")[:200],
    )
    if ok and track_rering:
        _note_ring_attempt(cid, meeting_id, raw)
    return "ringing" if ok else "failed"


def _resolve_ring(
    c: str,
    tok: str,
    *,
    direct_open_ids: Optional[List[str]] = None,
    operator_open_id: str = "",
) -> Tuple[List[str], str, str, bool]:
    """Resolve a ring command to ``(targets, label, unset_hint, wired)``. ``wired`` is False for a
    RING_CMD_RE command with no parser/config yet (cpms/pms). Shared by the single and batch handlers."""
    from features.recording import duty_roster as _duty

    if c == "c":
        return (_filter_ring_targets(direct_open_ids or [], operator_open_id=operator_open_id),
                "the tagged people", "tag at least one person, e.g. /c @Name", True)
    if c == "m":
        return (_major_check_person_ring_open_ids(tok),
                "major-P0 check persons", "P0_MAJOR_CHECK_PERSON_IDS", True)
    if c == "e":
        return (_config.get_p0_vc_ring_escalation_open_ids(),
                "escalation contacts", "P0_VC_RING_ESCALATION_OPEN_IDS", True)
    if _duty.is_sre_command(c):
        team = _duty.COMMAND_TEAM[c]
        # Ring only the FIRST team member (sheet order) — matches the "1. …" shown in the card; the rest
        # are backups paged via /c. Fall back to the on-shift/env-stub resolver's 1st when unresolved.
        _nm, oid = _duty.resolve_sre_team_first_open_id(c, tok)
        targets = [oid] if oid else []
        if not targets:
            alt, _u = _duty.resolve_sre_duty_open_ids(c, tok)
            if not alt:
                fb = _duty.get_duty_open_id(team)
                alt = [fb] if fb else []
            targets = alt[:1]
        return (targets, f"duty SRE {team}",
                f"the SRE handler tab (DUTY_DIRECTORY_SRE_SHEET_ID) + the duty directory "
                f"(DUTY_DIRECTORY_SHEET_TOKEN), or the P0_VC_RING_DUTY_{team}_OPEN_ID fallback", True)
    if c == "dba":
        targets, _u = _duty.resolve_dba_duty_open_ids(tok)
        return (targets, "DBA duty",
                "DUTY_SHIFT_SHEET_TOKEN (OSE & SRE Duty Shift) + the duty directory "
                "(DUTY_DIRECTORY_SHEET_TOKEN); share the bot on the sheet", True)
    if c == "sosm":
        targets, _u = _duty.resolve_liveslot_duty_open_ids(tok)
        return (targets, "liveslot SRE duty",
                "DUTY_SHIFT_SHEET_TOKEN (OSE & SRE Duty Shift) + the duty directory "
                "(DUTY_DIRECTORY_SHEET_TOKEN); share the bot on the sheet", True)
    if _duty.is_roster_command(c):
        targets, _u = _duty.resolve_duty_open_ids(c, tok)
        return (targets, f"{c.upper()} duty",
                f"DUTY_ROSTER_{c.upper()}_SHEET_TOKEN/_SHEET_ID and the duty directory "
                f"(DUTY_DIRECTORY_SHEET_TOKEN)", True)
    return ([], f"/{c}", "", False)


# Reminder shown under the fe/fpms/pms contact list: the ring calls only the 1st contact (today's
# duty / First Level); use /c to page anyone else shown in the list.
_CONTACT_LIST_NOTE = (
    "**commands**\n**/c @name** — use this if you want to contact someone else in the provided list"
)


def _post_ring_card(
    reply_mid: str, notify_chat: str, token: str, title: str, body_md: str, *, template: str = "blue"
) -> None:
    """Post a duty-ring status as an interactive card (``build_ring_status_card``): title in the
    coloured header, ``body_md`` as one ``lark_md`` div. Threaded under ``reply_mid`` (the command
    message) when set, else posted to ``notify_chat``. No-op when there's nothing to say."""
    if not token or not (body_md or "").strip():
        return
    card = _cards.build_ring_status_card(title, body_md, header_template=template)
    mid = (reply_mid or "").strip()
    if mid:
        _lark.post_card_reply_to_message(mid, token, card, reply_in_thread=True)
    else:
        _lark.post_card_to_chat(notify_chat, token, card)


def _duty_contact_list_md(c: str, tok: str) -> str:
    """Full numbered contact list for a duty command, e.g. for fe/fpms::

          1. Alice — today (Jul 24)
          2. Bob — tomorrow (Jul 25)
          3. Carol — Jul 26

    fe/fpms show today/tomorrow/day-after duty; pms shows First/Second/Third Level of this week; the
    SRE variants (sfpms/spms/scpms/sfe) list EVERYONE who covers that team (no dates — the SRE Handler
    tab is dateless). Numbered from 1 and includes the 1st contact (the one being called), so the block
    is the complete roster. Empty string when ``c`` has no list or nothing resolves (sheet miss)."""
    from features.recording import duty_roster as _duty

    try:
        if _duty.is_roster_command(c):
            items = [
                f"{n}. {(', '.join(names) if names else '(no one listed)')} — {label}"
                for n, (label, names) in enumerate(_duty.resolve_duty_contact_slots(c, tok), start=1)
            ]
        elif _duty.is_sre_command(c):
            items = [f"{n}. {nm}" for n, nm in enumerate(_duty.resolve_sre_team_person_names(c, tok), start=1)]
        else:
            return ""
    except Exception as e:  # pragma: no cover - never let a sheet hiccup break the ring
        log.warning("ring: contact-list failed cmd=%s err=%s", c, e)
        return ""
    return "\n".join(items)


def _ring_display_label(c: str) -> str:
    """Short label for the consolidated batch message (e.g. scpms -> 'SRE CPMS', fe -> 'FE')."""
    from features.recording import duty_roster as _duty

    if _duty.is_sre_command(c):
        return f"SRE {_duty.COMMAND_TEAM[c]}"
    return {
        "c": "Tagged people", "m": "Major check persons", "e": "Escalation contacts",
        "dba": "DBA", "sosm": "Liveslot SRE",
    }.get(c, c.upper())


def _expand_major_also_ring(cmds: List[str]) -> List[str]:
    """If ``/m`` is among ``cmds``, append the configured ``P0_MAJOR_ALSO_RING_COMMANDS`` (e.g. DBA + SRE
    duty) so a major-P0 ring ALSO pages today's duty in the SAME consolidated card. Order preserved,
    de-duped against what's already requested. No-op when ``/m`` isn't present or the env is unset."""
    lowered = [(c or "").strip().lower() for c in cmds]
    if "m" not in lowered:
        return cmds
    extras = _config.get_p0_major_also_ring_commands()
    if not extras:
        return cmds
    out = list(cmds)
    have = set(lowered)
    for e in extras:
        if e not in have:
            out.append(e)
            have.add(e)
    return out


def handle_ring_commands_batch(
    cmds: List[str],
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    operator_open_id: str = "",
    tenant_token: str = "",
    direct_open_ids: Optional[List[str]] = None,
    reply_to_message_id: str = "",
) -> None:
    """Ring MULTIPLE duty/direct commands and post ONE consolidated status ("Calling selected duty
    persons …") — one line per command — instead of a separate reply per command. Used for mixed
    commands like ``/srebac sfpms spms cpms fe`` so the duty rings collapse into a single prompt.
    When ``/m`` is present, P0_MAJOR_ALSO_RING_COMMANDS (DBA + SRE duty) are folded in too."""
    if not _config.get_p0_vc_ring_enabled():
        log.info("ring batch ignored (P0_VC_RING_ENABLED off) cmds=%s", cmds)
        return
    cmds = _expand_major_also_ring(list(cmds))
    tok = (tenant_token or token or "").strip()
    # TWO sections: the TOP groups every command's "who's being called NOW" line (team — @1st contact),
    # so the contacted people sit together; the BOTTOM lists each team's OTHER duties (2nd, 3rd …) for
    # reference, paged via /c.
    top_lines: List[str] = []
    bottom_blocks: List[str] = []
    any_ringing = False
    for raw in cmds:
        c = (raw or "").strip().lower()
        if not c:
            continue
        targets, label, _hint, wired = _resolve_ring(
            c, tok, direct_open_ids=direct_open_ids, operator_open_id=operator_open_id
        )
        disp = _ring_display_label(c)
        if not wired:
            top_lines.append(f"**{disp}** — not wired up yet")
            continue
        if not targets:
            top_lines.append(f"**{disp}** — no one on duty / not configured")
            continue
        if c == "m":
            _register_major_check_persons_for_join_prompt(session_source, targets, reply_mid=reply_to_message_id)
        else:
            _register_join_watch_open_ids(session_source, targets, reply_mid=reply_to_message_id)
        # Direct /c (tagged people) ALWAYS re-invites: a human explicitly asked to call them, so bypass
        # the merge-dedupe that would SILENTLY no-op a re-ring of someone already targeted this session
        # (yet still report "ringing"). Duty/roster rings keep the deduping path (don't spam on-call).
        if c == "c":
            status = force_reinvite_open_ids(
                session_source, targets, tenant_token=tok,
                operator_open_id=operator_open_id, track_rering=True,
            )
        else:
            status = invite_open_ids_into_active_meeting(
                session_source, targets, tenant_token=tok, operator_open_id=operator_open_id
            )
        log.info("ring batch cmd=%s targets=%s status=%s", c, len(targets), status)
        ats = " ".join(f"<at id={oid}></at>" for oid in targets)
        head = f"**{disp}** — {ats}" if ats else f"**{disp}**"
        if status == "no_session":
            top_lines.append(f"**{disp}** — no active meeting")
            continue
        if status == "disabled":
            top_lines.append(f"**{disp}** — ring disabled")
            continue
        if status == "failed":
            top_lines.append(f"{head} — ring failed (is the meeting host/inviter in the VC?)")
            continue
        if status == "queued_oauth":
            top_lines.append(f"{head} (queued; waiting for host authorization)")
        else:  # ringing
            top_lines.append(head)
            any_ringing = True
        # BOTTOM: this team's FULL duty list (all contacts, numbered from 1, incl. the one called above).
        roster_md = _duty_contact_list_md(c, tok)
        if roster_md:
            bottom_blocks.append(f"**{disp}**\n{roster_md}")
    if not top_lines or not token:
        return
    parts = ["\n".join(top_lines)]  # top: one line per command, grouped (no blank lines between)
    if bottom_blocks:
        parts.append("\n\n".join(bottom_blocks))
    if bottom_blocks or any_ringing:
        parts.append(_CONTACT_LIST_NOTE)
    header = "Calling today's selected duty persons into the meeting now" if any_ringing else "Selected duty commands:"
    body = "\n\n".join(parts)
    sess = _session.P0_SESSIONS.get(session_source) or {}
    mid = (reply_to_message_id or "").strip() or str(sess.get("meeting_invite_message_id") or "").strip()
    _post_ring_card(mid, notify_chat, token, header, body, template="blue" if any_ringing else "grey")


def handle_ring_command(
    cmd: str,
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    operator_open_id: str = "",
    tenant_token: str = "",
    direct_open_ids: Optional[List[str]] = None,
    reply_to_message_id: str = "",
) -> None:
    """Handle a ring command (m / e / scpms / sfpms / sfe / fe / fpms, or ``c`` for a direct tag-ring).

    Pages the resolved people into the already-active meeting for ``session_source`` and
    posts a status reply to ``notify_chat``. Anyone in the group may run these. For ``c`` the
    targets come from ``direct_open_ids`` (the message @mentions) — no sheet/directory needed.

    ``reply_to_message_id`` overrides the reply target (default = the meeting-link message): a mixed
    command like ``/srebac sfpms cpms`` passes the command's own message id so every duty-ring status
    lands in the SAME thread as the /srebac escalation card, not the separate meeting-link thread.
    """
    from features.recording import duty_roster as _duty

    c = (cmd or "").strip().lower()
    tok = (tenant_token or token or "").strip()
    _reply_mid = (reply_to_message_id or "").strip()

    def _out(title: str, body: str, *, template: str = "grey") -> None:
        """Post a status card threaded under the command message when mixed-invoked, else to the group."""
        _post_ring_card(_reply_mid, notify_chat, token, title, body, template=template)

    # Master gate: with VC ring disabled (prod default), silently ignore — no sheet reads, no reply.
    # This is the single choke point for every ring command (single and multi), so prod stays a no-op.
    if not _config.get_p0_vc_ring_enabled():
        log.info("ring cmd ignored (P0_VC_RING_ENABLED off) cmd=%s", c)
        return

    # /m with P0_MAJOR_ALSO_RING_COMMANDS set → route through the batch so the check persons AND the
    # configured DBA/SRE duty ring together in ONE consolidated card.
    if c == "m" and _config.get_p0_major_also_ring_commands():
        handle_ring_commands_batch(
            ["m"], session_source, notify_chat, token,
            operator_open_id=operator_open_id, tenant_token=tenant_token,
            direct_open_ids=direct_open_ids, reply_to_message_id=reply_to_message_id,
        )
        return

    targets, label, unset_hint, wired = _resolve_ring(
        c, tok, direct_open_ids=direct_open_ids, operator_open_id=operator_open_id
    )
    if not wired:
        # Recognized by RING_CMD_RE (e.g. cpms / pms) but no roster parser/config wired yet.
        _out("Not wired up yet", f"'/{c}' is not wired up yet — its roster parser/sheet config is still pending.")
        return
    if not targets:
        _out("Not configured", f"No {label} configured yet ({unset_hint}). Ask an admin to set it.", template="orange")
        return

    if c == "m":
        # Register the major check persons on the session so the VC-join "joined" prompt fires for
        # this manual flow too (Issue Watch registers them on auto-declare; @bot m did not).
        _register_major_check_persons_for_join_prompt(session_source, targets, reply_mid=reply_to_message_id)
    elif _duty.is_roster_command(c) or _duty.is_sre_command(c) or c in ("c", "dba", "sosm"):
        # Watch today's fe/fpms/pms/SRE/DBA/liveslot duty (or the /c tagged people) so their VC-join
        # posts a "joined" reply in the meeting thread.
        _register_join_watch_open_ids(session_source, targets, reply_mid=reply_to_message_id)

    # Direct /c always re-invites (bypass merge-dedupe) so a manual re-ring actually re-fires and the
    # returned status reflects the real invite result; duty/roster keep the deduping path.
    if c == "c":
        status = force_reinvite_open_ids(
            session_source,
            targets,
            tenant_token=tok,
            operator_open_id=operator_open_id,
            track_rering=True,
        )
    else:
        status = invite_open_ids_into_active_meeting(
            session_source,
            targets,
            tenant_token=tok,
            operator_open_id=operator_open_id,
        )
    log.info(
        "ring cmd handled cmd=%s targets=%s status=%s session_tail=%s",
        c,
        len(targets),
        status,
        session_source[-8:] if session_source else "",
    )
    if not token:
        return
    is_ring_announcement = False
    if status == "disabled":
        title, body, template = "VC ring disabled", "Set P0_VC_RING_ENABLED=1 to enable ring commands.", "grey"
    elif status == "no_session":
        title, body, template = "No active meeting", "Start a P0 meeting first, then run this command.", "orange"
    elif status == "no_targets":
        title, body, template = "Nothing to call", f"No valid {label} to call.", "orange"
    elif status == "queued_oauth":
        title = "Queued — waiting for host authorization"
        body = (f"{label} will ring once the meeting host finishes the one-time Lark authorization "
                "(the host just got a DM).")
        template = "orange"
    elif status == "failed":
        title = "Ring failed"
        body = (f"Couldn't invite {label} into the meeting. Make sure the meeting host/inviter is "
                "in the VC, then try again.")
        template = "red"
    else:  # ringing
        # Title names the team; the body @mentions the FIRST contact being called now, then (for
        # fe/fpms/pms/SRE) the team's OTHER duties + the /c reminder. The ring calls only the resolved
        # duty (above); the rest are shown for visibility. <at id=ou_xxx></at> is the card mention form.
        template = "blue"
        when = "today " if (c in ("dba", "sosm") or _duty.is_roster_command(c) or _duty.is_sre_command(c)) else ""
        title = f"Calling {label} {when}into the meeting now"
        ats = " ".join(f"<at id={oid}></at>" for oid in targets)
        roster_md = _duty_contact_list_md(c, tok)
        parts = [ats] if ats else []
        if roster_md:
            parts.append(roster_md)
        if roster_md or _duty.is_roster_command(c) or _duty.is_sre_command(c):
            parts.append(_CONTACT_LIST_NOTE)
        body = "\n\n".join(parts) if parts else "Paging the duty into the meeting now."
        is_ring_announcement = True
    sess = _session.P0_SESSIONS.get(session_source) or {}
    # Reply target: the caller-supplied message (mixed command → the /srebac thread) wins; otherwise the
    # meeting-link message so standalone ring status still groups under the meeting notice.
    mid = (reply_to_message_id or "").strip() or str(sess.get("meeting_invite_message_id") or "").strip()
    # Dedupe the major-check-person "calling" announcement: auto-ring-on-join (_try_ring_session)
    # and this `@bot m` command both announce the same thing. Post it only once per meeting.
    if c == "m" and is_ring_announcement:
        if sess.get("check_persons_ring_announced"):
            return
        sess["check_persons_ring_announced"] = True
        _session.P0_SESSIONS[session_source] = sess
        if _session._session_disk.enabled():
            _session._session_disk.save_session(session_source, sess)
    # Status card (title in the coloured header, body a lark_md div where <at id=…> mentions render) —
    # threaded under the reply target: the command message for a mixed command, else the meeting-link one.
    _post_ring_card(mid, notify_chat, token, title, body, template=template)


def _filter_ring_targets(
    raw_ids: List[str],
    *,
    operator_open_id: str = "",
    exclude: Optional[Set[str]] = None,
) -> List[str]:
    op = (operator_open_id or "").strip()
    skip = set(exclude or [])
    if op:
        skip.add(op)
    bot_oid = (_config.get_lark_bot_open_id() or "").strip()
    if bot_oid:
        skip.add(bot_oid)
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_ids or []:
        oid = (raw or "").strip()
        if not oid.startswith("ou_") or len(oid) < 12:
            continue
        if oid in skip or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
        if len(out) >= 10:
            break
    for fb in _config.get_p0_vc_ring_fallback_open_ids():
        if fb not in skip and fb not in seen:
            seen.add(fb)
            out.append(fb)
        if len(out) >= 10:
            break
    return out


def resolve_ring_targets_from_snapshot(
    snap: Optional[Dict[str, Any]],
    *,
    detection_chat_id: str,
    operator_open_id: str,
) -> List[str]:
    """Merge concern @mentions (on alert) + duty @mentions stored before declare."""
    concern: List[str] = []
    if snap:
        raw = snap.get("concern_mention_open_ids") or snap.get("mention_open_ids") or []
        if isinstance(raw, list):
            concern = [str(x).strip() for x in raw]
    duty_ids = pop_duty_mentions_for_chat(detection_chat_id)
    merged = duty_ids + [x for x in concern if x not in duty_ids]
    return _filter_ring_targets(merged, operator_open_id=operator_open_id)


def _major_check_person_ring_open_ids(
    tenant_token: str,
) -> List[str]:
    """``P0_MAJOR_CHECK_PERSON_IDS`` as ``ou_...`` for VC invite (resolves tenant user_id when needed)."""
    tok = (tenant_token or "").strip()
    out: List[str] = []
    for oid, uid in _config.get_p0_major_check_person_recipients():
        if oid and oid.startswith("ou_"):
            out.append(oid)
            continue
        if uid and tok:
            resolved = _lark.lookup_open_id_by_user_id(tok, uid)
            if resolved:
                out.append(resolved)
    return out


def _set_join_prompt_reply_mid(sess: Dict[str, Any], reply_mid: str) -> bool:
    """Record the message the VC-join "joined / already in the meeting" prompt should reply to (the
    command message when a ring command drove it) so that prompt lands in the SAME thread as the
    command, not the meeting-link thread. Returns True if it changed."""
    mid = (reply_mid or "").strip()
    if not mid or sess.get("join_prompt_reply_mid") == mid:
        return False
    sess["join_prompt_reply_mid"] = mid
    return True


def _register_major_check_persons_for_join_prompt(
    session_source: str, ring_open_ids: List[str], *, reply_mid: str = ""
) -> None:
    """
    Populate ``major_check_person_open_ids`` / ``_user_ids`` on the session so the VC-join "joined"
    prompt (``maybe_prompt_major_check_person_joined``) fires for the manual ``@bot m`` flow too.
    Merges with any existing (Issue Watch) registration and preserves the ``join_prompted`` dedupe.
    """
    sess = _session.P0_SESSIONS.get(session_source)
    if not sess:
        return
    recipients = _config.get_p0_major_check_person_recipients()
    oids = {oid for oid, _uid in recipients if oid}
    oids.update(x for x in (ring_open_ids or []) if x)
    uids = {uid for _oid, uid in recipients if uid}
    existing_oids = set(sess.get("major_check_person_open_ids") or [])
    existing_uids = set(sess.get("major_check_person_user_ids") or [])
    merged_oids = existing_oids | oids
    merged_uids = existing_uids | uids
    changed = _set_join_prompt_reply_mid(sess, reply_mid)
    if merged_oids != existing_oids or merged_uids != existing_uids:
        sess["major_check_person_open_ids"] = sorted(merged_oids)
        sess["major_check_person_user_ids"] = sorted(merged_uids)
        changed = True
    if "major_check_person_join_prompted" not in sess:
        sess["major_check_person_join_prompted"] = []
        changed = True
    if not changed:
        return
    _session.P0_SESSIONS[session_source] = sess
    if _session._session_disk.enabled():
        _session._session_disk.save_session(session_source, sess)


def _register_join_watch_open_ids(session_source: str, open_ids: List[str], *, reply_mid: str = "") -> None:
    """Add ``open_ids`` to the session join-watch set so ``maybe_prompt_major_check_person_joined``
    posts a "joined" thread reply when any of them joins the VC. Used by the fe/fpms duty ring
    (open_id only — resolved from the roster directory). Preserves the ``join_prompted`` dedupe.
    ``reply_mid`` (the command message) routes that join prompt to the command thread."""
    sess = _session.P0_SESSIONS.get(session_source)
    if not sess:
        return
    add = {x for x in (open_ids or []) if x}
    existing = set(sess.get("major_check_person_open_ids") or [])
    merged = existing | add
    changed = _set_join_prompt_reply_mid(sess, reply_mid)
    if merged != existing:
        sess["major_check_person_open_ids"] = sorted(merged)
        changed = True
    if "major_check_person_join_prompted" not in sess:
        sess["major_check_person_join_prompted"] = []
        changed = True
    if not changed:
        return
    _session.P0_SESSIONS[session_source] = sess
    if _session._session_disk.enabled():
        _session._session_disk.save_session(session_source, sess)


def resolve_declare_ring_targets(
    snap: Optional[Dict[str, Any]],
    *,
    detection_chat_id: str,
    operator_open_id: str,
    tenant_token: str = "",
) -> List[str]:
    """
    Ring targets at Issue Watch declare: concern/duty @mentions + ``P0_MAJOR_CHECK_PERSON_IDS``.
    """
    base = resolve_ring_targets_from_snapshot(
        snap,
        detection_chat_id=detection_chat_id,
        operator_open_id=operator_open_id,
    )
    major = _major_check_person_ring_open_ids(tenant_token)
    if not major:
        return base
    return _filter_ring_targets(major + base, operator_open_id=operator_open_id)


def format_declare_reply_with_mentions(reply_text: str, ring_targets: List[str]) -> str:
    """Prepend Lark ``<at>`` tags so tagged users are notified in the thread."""
    body = (reply_text or "").strip()
    if not ring_targets:
        return body
    at_parts = [f'<at user_id="{oid}"></at>' for oid in ring_targets]
    prefix = " ".join(at_parts)
    return f"{prefix} {body}".strip() if body else prefix


def maybe_prompt_oauth_dm(operator_open_id: str, tenant_token: str) -> None:
    if not _config.get_p0_vc_ring_enabled():
        return
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return
    if _oauth.has_user_token(oid):
        return
    url = _oauth.build_authorize_url(oid)
    if not url:
        log.warning("vc_ring: OAuth URL not configured — set P0_VC_OAUTH_REDIRECT_URI + public base")
        return
    _lark.post_text_to_open_id(
        oid,
        tok,
        "To auto-ring tagged users into the P0 VC, authorize this bot once (VC invite permission):\n"
        f"{url}\n\n"
        "Authorize first, then join the P0 VC — tagged users will be rung when you enter.",
    )


def _resolve_declarer_open_id(
    joiner_open_id: str,
    joiner_user_id: str,
    tenant_token: str,
) -> str:
    oid = (joiner_open_id or "").strip()
    if oid.startswith("ou_"):
        return oid
    uid = (joiner_user_id or "").strip()
    if uid and tenant_token:
        resolved = _lark.lookup_open_id_by_user_id(tenant_token, uid)
        if resolved:
            log.info("vc_ring: resolved joiner open_id from user_id uid_tail=%s", uid[-6:])
            return resolved
    return ""


def _is_session_declarer(
    sess: Dict[str, Any],
    *,
    joiner_open_id: str,
    joiner_user_id: str,
    tenant_token: str,
) -> bool:
    trigger_oid = str(sess.get("trigger_open_id") or "").strip()
    if trigger_oid and joiner_open_id and joiner_open_id == trigger_oid:
        return True
    trigger_uid = str(sess.get("trigger_lark_user_id") or "").strip()
    if trigger_uid and joiner_user_id and joiner_user_id == trigger_uid:
        return True
    if trigger_uid and joiner_open_id and tenant_token:
        joiner_uid = _lark.get_tenant_user_id_by_open_id(tenant_token, joiner_open_id)
        if joiner_uid and joiner_uid == trigger_uid:
            return True
    return False


def _cancel_rering_timer_locked(st: Dict[str, Any]) -> None:
    """Cancel a scheduler entry's pending timer. Caller must hold ``_RERING_LOCK``."""
    tmr = st.get("timer")
    if tmr is not None:
        try:
            tmr.cancel()
        except Exception:
            pass
        st["timer"] = None


def _note_ring_attempt(chat_id: str, meeting_id: str, target_open_ids: List[str]) -> None:
    """Record that ``target_open_ids`` were just rung (attempt #1) into ``meeting_id`` and arm the
    re-ring timer. No-op unless P0_VC_RING_RETRY_ENABLED. Idempotent per (chat, target): a target
    already tracked keeps its attempt count (so re-registering the same session does not reset it)."""
    if not _config.get_p0_vc_ring_retry_enabled():
        return
    cid = (chat_id or "").strip()
    mid = (meeting_id or "").strip()
    fresh = [str(x).strip() for x in (target_open_ids or []) if str(x).strip()]
    if not cid or not mid or not fresh:
        return
    with _RERING_LOCK:
        st = _RERING_STATE.get(cid)
        if not isinstance(st, dict):
            st = {"meeting_id": mid, "targets": {}, "joined": set(), "timer": None}
            _RERING_STATE[cid] = st
        st["meeting_id"] = mid
        targets: Dict[str, Any] = st["targets"]
        joined: set = st["joined"]
        added = False
        for oid in fresh:
            if oid in joined:
                continue
            if oid not in targets:
                targets[oid] = {"attempts": 1}  # the first ring already went out
                added = True
        if added and st.get("timer") is None:
            _arm_rering_timer_locked(cid)


def _arm_rering_timer_locked(chat_id: str) -> None:
    """Arm the next re-ring pass for ``chat_id``. Caller must hold ``_RERING_LOCK``."""
    st = _RERING_STATE.get(chat_id)
    if not isinstance(st, dict):
        return
    _cancel_rering_timer_locked(st)
    interval = _config.get_p0_vc_ring_retry_interval_sec()
    tmr = threading.Timer(interval, _rering_pass, args=(chat_id,))
    tmr.daemon = True
    st["timer"] = tmr
    tmr.start()


def _rering_pass(chat_id: str) -> None:
    """Timer callback: re-ring every tracked target that has NOT joined and is under the attempt cap,
    then re-arm if any remain pending. Drops the scheduler entry once everyone joined or maxed out."""
    cid = (chat_id or "").strip()
    if not cid:
        return
    try:
        if not _config.get_p0_vc_ring_retry_enabled():
            with _RERING_LOCK:
                _RERING_STATE.pop(cid, None)
            return
        max_attempts = _config.get_p0_vc_ring_retry_max_attempts()
        with _RERING_LOCK:
            st = _RERING_STATE.get(cid)
            if not isinstance(st, dict):
                return
            st["timer"] = None
            meeting_id = str(st.get("meeting_id") or "").strip()
            joined: set = st["joined"]
            targets: Dict[str, Any] = st["targets"]
            due = [
                oid for oid, meta in targets.items()
                if oid not in joined and int(meta.get("attempts") or 0) < max_attempts
            ]
        if not meeting_id or not due:
            with _RERING_LOCK:
                # Nothing left to ring (all joined or all maxed) — clean up.
                st2 = _RERING_STATE.get(cid)
                if isinstance(st2, dict) and st2.get("timer") is None:
                    _RERING_STATE.pop(cid, None)
            return

        sess = _session.P0_SESSIONS.get(cid)
        tenant_token = _lark.get_tenant_token_primary()
        inviter = _resolve_ring_inviter(sess if isinstance(sess, dict) else None)
        user_tok = _oauth.get_user_access_token(inviter) if inviter else ""
        if not user_tok:
            if inviter:
                maybe_prompt_oauth_dm(inviter, tenant_token)
            log.warning("vc_ring: re-ring queued — no inviter token cid_tail=%s", cid[-8:])
            with _RERING_LOCK:
                if cid in _RERING_STATE:
                    _arm_rering_timer_locked(cid)  # retry the same pass next interval
            return

        ok, detail = _lark.invite_users_to_vc_meeting(user_tok, meeting_id, due)
        with _RERING_LOCK:
            st = _RERING_STATE.get(cid)
            if isinstance(st, dict):
                for oid in due:
                    meta = st["targets"].get(oid)
                    if isinstance(meta, dict):
                        meta["attempts"] = int(meta.get("attempts") or 0) + 1
                still = [
                    oid for oid, meta in st["targets"].items()
                    if oid not in st["joined"] and int(meta.get("attempts") or 0) < max_attempts
                ]
                if still:
                    _arm_rering_timer_locked(cid)
                else:
                    _cancel_rering_timer_locked(st)
                    _RERING_STATE.pop(cid, None)
        log.info(
            "vc_ring: re-ring pass count=%s ok=%s meeting_tail=%s cid_tail=%s detail=%s",
            len(due), ok, meeting_id[-8:], cid[-8:], (detail or "")[:160],
        )
    except Exception as e:
        log.warning("vc_ring: re-ring pass error cid_tail=%s err=%s", cid[-8:], e)


def mark_vc_ring_target_joined(meeting_ref: str, joiner_open_id: str, tenant_token: str = "") -> None:
    """VC-join hook: mark ``joiner_open_id`` as answered so the re-ring scheduler stops paging them.
    Safe/cheap no-op when the retry feature is off or the joiner was never a re-ring target."""
    if not _config.get_p0_vc_ring_retry_enabled():
        return
    oid = (joiner_open_id or "").strip()
    if not oid:
        return
    chat_id, _sess = _session.find_session_by_meeting_ref((meeting_ref or "").strip())
    cid = (chat_id or "").strip()
    if not cid:
        return
    with _RERING_LOCK:
        st = _RERING_STATE.get(cid)
        if not isinstance(st, dict):
            return
        if oid not in st.get("targets", {}):
            return
        st["joined"].add(oid)
        remaining = [
            t for t in st["targets"]
            if t not in st["joined"]
            and int(st["targets"][t].get("attempts") or 0) < _config.get_p0_vc_ring_retry_max_attempts()
        ]
        if not remaining:
            _cancel_rering_timer_locked(st)
            _RERING_STATE.pop(cid, None)
    log.info("vc_ring: re-ring target joined — stop paging oid_tail=%s cid_tail=%s", oid[-8:], cid[-8:])


def _resolve_ring_inviter(
    sess: Optional[Dict[str, Any]],
    *,
    prefer_open_id: str = "",
    fallback: str = "",
) -> str:
    """Pick which account's OAuth token drives the VC invite.

    ``P0_VC_RING_INVITER_OPEN_ID`` may list one OR MORE accounts. Whichever listed account is
    actually in the VC is the one that can invite (Lark requires the inviter be a participant), so:
      1. ``prefer_open_id`` (the account that just joined) when it is a configured inviter,
      2. else the inviter already recorded as having joined this session,
      3. else the first configured inviter holding a valid user_access_token,
      4. else the first configured inviter (so an OAuth-prompt DM targets a real account),
      5. else ``fallback`` — legacy: no inviter configured, use the declarer's own token.
    With exactly ONE configured inviter this returns that account, matching the original behavior.
    """
    inviters = _config.get_p0_vc_ring_inviter_open_ids()
    if not inviters:
        return (fallback or "").strip()
    prefer = (prefer_open_id or "").strip()
    if prefer and prefer in inviters:
        return prefer
    if isinstance(sess, dict):
        active = str(sess.get("vc_ring_active_inviter") or "").strip()
        if active and active in inviters:
            return active
    for oid in inviters:
        if _oauth.get_user_access_token(oid):
            return oid
    return inviters[0]


def _try_ring_session(
    chat_id: str,
    sess: Dict[str, Any],
    *,
    declarer_open_id: str,
    meeting_ref: str,
    tenant_token: str,
) -> bool:
    """Attempt VC invite for pending (not yet invited) targets. Returns True when ring succeeded."""
    declarer = (declarer_open_id or "").strip()
    if not declarer:
        return False
    targets = _pending_ring_targets(sess)
    if not targets:
        if list(sess.get("vc_ring_target_open_ids") or []):
            log.info("vc_ring: all targets already invited chat_id=%s", chat_id[:24])
        else:
            log.warning("vc_ring: no ring targets on session chat_id=%s", chat_id[:24])
        return False

    meeting_id = str(sess.get("meeting_id") or meeting_ref or "").strip()
    if not meeting_id:
        log.warning("vc_ring: no meeting_id on session chat_id=%s", chat_id[:24])
        return False

    # Fixed inviter (an authorized account rings for ALL declarers) — falls back to the declarer's
    # own token when P0_VC_RING_INVITER_OPEN_ID is unset (legacy). Either way the inviter must be a
    # participant in the meeting for Lark to accept the invite. When called on a VC join, ``declarer``
    # IS the joiner, so prefer their token (they are the account actually in the meeting).
    inviter = _resolve_ring_inviter(sess, prefer_open_id=declarer, fallback=declarer)
    user_tok = _oauth.get_user_access_token(inviter)
    if not user_tok:
        maybe_prompt_oauth_dm(inviter, tenant_token)
        log.warning(
            "vc_ring: no user_access_token for inviter_tail=%s — complete OAuth first",
            inviter[-8:],
        )
        return False

    ok, detail = _lark.invite_users_to_vc_meeting(user_tok, meeting_id, targets)
    if ok:
        invited = list(sess.get("vc_ring_invited_open_ids") or [])
        seen = set(invited)
        for oid in targets:
            if oid not in seen:
                invited.append(oid)
                seen.add(oid)
        sess["vc_ring_invited_open_ids"] = invited
        sess["vc_ring_done"] = not _pending_ring_targets(
            {**sess, "vc_ring_invited_open_ids": invited}
        )
        _session.P0_SESSIONS[chat_id] = sess
        if _session._session_disk.enabled():
            _session._session_disk.save_session(chat_id, sess)
        log.info(
            "vc_ring: invited count=%s meeting_id_tail=%s inviter_tail=%s detail=%s",
            len(targets),
            meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
            inviter[-8:],
            (detail or "")[:200],
        )
        # Track for re-ring: page each newly-invited user again if they don't join (P0_VC_RING_RETRY_*).
        _note_ring_attempt(chat_id, meeting_id, targets)
        # Auto-ring on VC join invites the targets SILENTLY — the "calling check persons" chat
        # notice is posted only by the explicit `@bot m` command (actual major P0 check persons).
        return True

    log.warning(
        "vc_ring: invite failed meeting_id_tail=%s targets=%s detail=%s",
        meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
        len(targets),
        (detail or "")[:300],
    )
    return False


def maybe_ring_on_vc_join(
    meeting_ref: str,
    joiner_open_id: str,
    tenant_token: str,
    *,
    joiner_user_id: str = "",
) -> None:
    """
    When the P0 declarer joins the VC, invite ``vc_ring_target_open_ids`` from the session.
    """
    if not _config.get_p0_vc_ring_enabled():
        return
    ref = (meeting_ref or "").strip()
    if not ref:
        return

    joiner = _resolve_declarer_open_id(joiner_open_id, joiner_user_id, tenant_token)
    if not joiner:
        log.warning(
            "vc_ring: join event missing open_id and could not resolve from user_id ref_tail=%s",
            ref[-12:] if len(ref) > 12 else ref,
        )
        return

    chat_id, sess = _session.find_session_by_meeting_ref(ref)
    if not chat_id or not sess:
        log.warning(
            "vc_ring: no session for meeting_ref_tail=%s joiner_tail=%s",
            ref[-12:] if len(ref) > 12 else ref,
            joiner[-8:],
        )
        return

    inviters = _config.get_p0_vc_ring_inviter_open_ids()
    if inviters:
        # Fixed-inviter mode: anyone can declare, but only a listed account's join triggers the ring
        # (only then is that account a meeting participant, as Lark requires). The declarer's join is
        # irrelevant — their token is never used. With more than one configured inviter, whichever
        # listed account joins first fires the ring using its own token.
        if joiner not in inviters:
            log.info(
                "vc_ring: joiner not a fixed inviter — waiting for an inviter to join joiner_tail=%s inviters=%s",
                joiner[-8:],
                [i[-8:] for i in inviters],
            )
            return
        # Record which listed inviter is in the VC so later @bot ring commands use its token.
        sess["vc_ring_active_inviter"] = joiner
        _session.P0_SESSIONS[chat_id] = sess
    elif not _is_session_declarer(
        sess,
        joiner_open_id=joiner,
        joiner_user_id=joiner_user_id,
        tenant_token=tenant_token,
    ):
        log.warning(
            "vc_ring: joiner not declarer — skip ring joiner_tail=%s trigger_tail=%s trigger_uid=%s",
            joiner[-8:],
            str(sess.get("trigger_open_id") or "")[-8:],
            str(sess.get("trigger_lark_user_id") or "")[-6:],
        )
        return

    _try_ring_session(
        chat_id,
        sess,
        declarer_open_id=joiner,
        meeting_ref=ref,
        tenant_token=tenant_token,
    )


def maybe_retry_pending_vc_ring_for_declarer(declarer_open_id: str, tenant_token: str) -> int:
    """
    After OAuth, retry ring on active sessions where this user declared P0 but ring did not run
    (e.g. they joined VC before authorizing).
    """
    if not _config.get_p0_vc_ring_enabled():
        return 0
    user = (declarer_open_id or "").strip()
    if not user:
        return 0
    if not _oauth.has_user_token(user):
        return 0
    # Fixed-inviter mode: only a configured inviter's OAuth can drive a retry, and it rings for
    # EVERY pending session (their token invites regardless of who declared). Legacy: the declarer's
    # OAuth retries only the sessions they declared.
    inviters = _config.get_p0_vc_ring_inviter_open_ids()
    if inviters and user not in inviters:
        return 0

    n_ok = 0
    for chat_id, sess in list(_session.P0_SESSIONS.items()):
        if not isinstance(sess, dict):
            continue
        if not inviters and str(sess.get("trigger_open_id") or "").strip() != user:
            continue
        if sess.get("vc_ring_done"):
            continue
        if not list(sess.get("vc_ring_target_open_ids") or []):
            continue
        meeting_ref = str(sess.get("meeting_id") or sess.get("meeting_no") or "").strip()
        if not meeting_ref:
            continue
        if _try_ring_session(
            chat_id,
            sess,
            declarer_open_id=user,
            meeting_ref=meeting_ref,
            tenant_token=tenant_token,
        ):
            n_ok += 1
    if n_ok:
        log.info("vc_ring: OAuth retry rang %s session(s) for user_tail=%s", n_ok, user[-8:])
    return n_ok
