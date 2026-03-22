"""
P0 incident logic: session management, drafts/previews, card actions, and DM handling.

Use this package from your app (e.g. webhook server) by importing the public API below.
"""

from . import config
from . import cards
from . import drafts
from . import groq_client
from . import handlers
from . import issues
from . import lark_client
from . import participants
from . import session
from . import support
from . import text_processing

# Session (P0_SESSIONS is the in-memory session store used by lark_logic)
P0_SESSIONS = session.P0_SESSIONS
start_p0 = session.start_p0
end_p0_session = session.end_p0_session
end_p0_session_by_meeting_no = session.end_p0_session_by_meeting_no
end_p0_session_by_meeting_ref = session.end_p0_session_by_meeting_ref
cancel_p0_session = session.cancel_p0_session
cancel_p0_session_by_meeting_no = session.cancel_p0_session_by_meeting_no
bind_live_meeting_id = session.bind_live_meeting_id
p0_cooldown = session.p0_cooldown
p0_cooldown_remaining_sec = session.p0_cooldown_remaining_sec
find_session_by_meeting_ref = session.find_session_by_meeting_ref
find_session_by_meeting_no = session.find_session_by_meeting_no
find_session_by_target_chat = session.find_session_by_target_chat
get_active_session = session.get_active_session
get_active_target_chat = session.get_active_target_chat
get_p1_prompt_pending = session.get_p1_prompt_pending
set_p1_prompt_pending = session.set_p1_prompt_pending
pop_p1_prompt_pending = session.pop_p1_prompt_pending
consume_p1_prompt_for_confirm = session.consume_p1_prompt_for_confirm
request_p1_meeting_confirmation = session.request_p1_meeting_confirmation
apply_p1_escalation_after_confirm = session.apply_p1_escalation_after_confirm
decline_p1_escalation_end_as_p1 = session.decline_p1_escalation_end_as_p1
get_last_ended_snapshot = session.get_last_ended_snapshot

# Participants
list_meeting_participants = participants.list_meeting_participants
is_person_in_meeting = participants.is_person_in_meeting
add_meeting_participant = participants.add_meeting_participant
remove_meeting_participant = participants.remove_meeting_participant
strip_seeded_host_placeholder_for_open_id = participants.strip_seeded_host_placeholder_for_open_id
departments_line_from_names = participants.departments_line_from_names
format_participants_names_display = participants.format_participants_names_display

# Lark
get_tenant_token = lark_client.get_tenant_token
post_text_to_chat = lark_client.post_text_to_chat
post_card_to_chat = lark_client.post_card_to_chat
post_text_to_open_id = lark_client.post_text_to_open_id
post_card_to_open_id = lark_client.post_card_to_open_id

# Handlers (main entry points for events)
handle_dm_generate_overview = handlers.handle_dm_generate_overview
handle_lark_card_action = handlers.handle_lark_card_action
handle_p0_submit = handlers.handle_p0_submit

# Support map
get_support_map = support.get_support_map

# Cards (for custom usage)
build_meeting_card = cards.build_meeting_card
build_meeting_ended_card = cards.build_meeting_ended_card
build_meeting_cancelled_card = cards.build_meeting_cancelled_card
build_no_active_p0_session_card = cards.build_no_active_p0_session_card
build_dm_instruction_card = cards.build_dm_instruction_card
build_preview_card = cards.build_preview_card
build_edit_overview_card = cards.build_edit_overview_card
build_overview_result_card = cards.build_overview_result_card
build_bilingual_overview_md = cards.build_bilingual_overview_md

# Config (for callers that need env)
reload_env_runtime = config.reload_env_runtime
get_incident_group_chat_ids = config.get_incident_group_chat_ids
get_emergency_topic_for_source_chat = config.get_emergency_topic_for_source_chat
get_overview_post_chat_id = config.get_overview_post_chat_id
get_target_group_chat_id = config.get_target_group_chat_id
get_owner_ids = config.get_owner_ids
get_host_and_dm_open_id = config.get_host_and_dm_open_id
get_p0_trigger_ignore_open_ids = config.get_p0_trigger_ignore_open_ids
get_dm_instruction_open_id = config.get_dm_instruction_open_id
get_dm_instruction_open_ids = config.get_dm_instruction_open_ids
get_dm_repost_instruction_after_reset = config.get_dm_repost_instruction_after_reset
get_vc_reserve_end_offset_sec = config.get_vc_reserve_end_offset_sec

__all__ = [
    "P0_SESSIONS",
    "start_p0",
    "end_p0_session",
    "end_p0_session_by_meeting_no",
    "end_p0_session_by_meeting_ref",
    "cancel_p0_session",
    "cancel_p0_session_by_meeting_no",
    "bind_live_meeting_id",
    "p0_cooldown",
    "p0_cooldown_remaining_sec",
    "find_session_by_meeting_ref",
    "find_session_by_meeting_no",
    "find_session_by_target_chat",
    "get_active_session",
    "get_active_target_chat",
    "get_p1_prompt_pending",
    "set_p1_prompt_pending",
    "pop_p1_prompt_pending",
    "consume_p1_prompt_for_confirm",
    "request_p1_meeting_confirmation",
    "apply_p1_escalation_after_confirm",
    "decline_p1_escalation_end_as_p1",
    "get_last_ended_snapshot",
    "list_meeting_participants",
    "is_person_in_meeting",
    "add_meeting_participant",
    "remove_meeting_participant",
    "strip_seeded_host_placeholder_for_open_id",
    "departments_line_from_names",
    "format_participants_names_display",
    "get_tenant_token",
    "post_text_to_chat",
    "post_card_to_chat",
    "post_text_to_open_id",
    "post_card_to_open_id",
    "handle_dm_generate_overview",
    "handle_lark_card_action",
    "handle_p0_submit",
    "get_support_map",
    "build_meeting_card",
    "build_meeting_ended_card",
    "build_meeting_cancelled_card",
    "build_no_active_p0_session_card",
    "build_dm_instruction_card",
    "build_preview_card",
    "build_edit_overview_card",
    "build_overview_result_card",
    "build_bilingual_overview_md",
    "reload_env_runtime",
    "get_incident_group_chat_ids",
    "get_emergency_topic_for_source_chat",
    "get_overview_post_chat_id",
    "get_target_group_chat_id",
    "get_owner_ids",
    "get_host_and_dm_open_id",
    "get_p0_trigger_ignore_open_ids",
    "get_dm_instruction_open_id",
    "get_dm_instruction_open_ids",
    "get_dm_repost_instruction_after_reset",
    "get_vc_reserve_end_offset_sec",
]
