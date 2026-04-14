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
clear_p0_cooldown = session.clear_p0_cooldown
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
get_last_ended_snapshot = session.get_last_ended_snapshot
get_dm_target_chat_for_operator = session.get_dm_target_chat_for_operator
enqueue_dm_instruction_if_needed = session.enqueue_dm_instruction_if_needed
slack_cross_post_slack_enabled_for_incident_chat = session.slack_cross_post_slack_enabled_for_incident_chat
release_dm_after_overview_sent = session.release_dm_after_overview_sent
release_dm_slots_for_incident_chat = session.release_dm_slots_for_incident_chat
release_standalone_overview_cancel = session.release_standalone_overview_cancel
chat_has_active_session = session.chat_has_active_session
dm_preview_allowed_for_incident = session.dm_preview_allowed_for_incident
STANDALONE_DM_SOURCE_CHAT_ID = session.STANDALONE_DM_SOURCE_CHAT_ID

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
get_tenant_token_primary = lark_client.get_tenant_token_primary
get_tenant_token_for_severity_dm = lark_client.get_tenant_token_for_severity_dm
post_text_to_chat = lark_client.post_text_to_chat
post_card_to_chat = lark_client.post_card_to_chat
post_text_to_open_id = lark_client.post_text_to_open_id
post_card_to_open_id = lark_client.post_card_to_open_id

# Handlers (main entry points for events)
handle_dm_generate_overview = handlers.handle_dm_generate_overview
handle_lark_card_action = handlers.handle_lark_card_action
handle_lark_card_action_show_participants_sync = handlers.handle_lark_card_action_show_participants_sync
card_action_name_from_payload = handlers.card_action_name_from_payload
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
build_slack_severity_prompt_card = cards.build_slack_severity_prompt_card
build_slack_minor_role_prompt_card = cards.build_slack_minor_role_prompt_card
build_slack_minor_backend_team_card = cards.build_slack_minor_backend_team_card
build_slack_minor_fe_reach_card = cards.build_slack_minor_fe_reach_card
build_bilingual_overview_md = cards.build_bilingual_overview_md
apply_slack_minor_card_action = session.apply_slack_minor_card_action

# Config (for callers that need env)
reload_env_runtime = config.reload_env_runtime
get_incident_group_chat_ids = config.get_incident_group_chat_ids
get_emergency_topic_for_source_chat = config.get_emergency_topic_for_source_chat
get_vc_meeting_topic_for_source_chat = config.get_vc_meeting_topic_for_source_chat
get_overview_post_chat_id = config.get_overview_post_chat_id
get_overview_target_chat_id_for_source_incident = config.get_overview_target_chat_id_for_source_incident
get_incident_overview_target_map = config.get_incident_overview_target_map
get_dm_overview_target_chat_id = config.get_dm_overview_target_chat_id
get_standalone_overview_target_chat_id_for_tag = config.get_standalone_overview_target_chat_id_for_tag
get_target_group_chat_id = config.get_target_group_chat_id
get_owner_ids = config.get_owner_ids
get_host_and_dm_open_id = config.get_host_and_dm_open_id
get_p0_trigger_ignore_open_ids = config.get_p0_trigger_ignore_open_ids
get_incident_group_command_open_ids = config.get_incident_group_command_open_ids
can_use_incident_group_commands = config.can_use_incident_group_commands
get_dm_instruction_open_id = config.get_dm_instruction_open_id
get_dm_instruction_open_ids = config.get_dm_instruction_open_ids
get_dm_repost_instruction_after_reset = config.get_dm_repost_instruction_after_reset
get_vc_reserve_end_offset_sec = config.get_vc_reserve_end_offset_sec
slack_automation_enabled = config.slack_automation_enabled
slack_huddle_on_p0_start = config.slack_huddle_on_p0_start
slack_huddle_on_overview_send = config.slack_huddle_on_overview_send
slack_severity_prompt_enabled = config.slack_severity_prompt_enabled
get_lark_primary_app_credentials = config.get_lark_primary_app_credentials
get_lark_severity_app_credentials = config.get_lark_severity_app_credentials
get_slack_channel_url_for_incident_chat = config.get_slack_channel_url_for_incident_chat
get_slack_session_dir_for_incident_chat = config.get_slack_session_dir_for_incident_chat
get_slack_overview_webhook_for_incident_chat = config.get_slack_overview_webhook_for_incident_chat
get_slack_incident_notify_webhook_for_incident_chat = config.get_slack_incident_notify_webhook_for_incident_chat
get_slack_bot_token = config.get_slack_bot_token
get_slack_app_id = config.get_slack_app_id
get_slack_bot_user_id = config.get_slack_bot_user_id
get_slack_api_channel_id_for_incident_chat = config.get_slack_api_channel_id_for_incident_chat

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
    "clear_p0_cooldown",
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
    "get_last_ended_snapshot",
    "get_dm_target_chat_for_operator",
    "enqueue_dm_instruction_if_needed",
    "slack_cross_post_slack_enabled_for_incident_chat",
    "release_dm_after_overview_sent",
    "release_dm_slots_for_incident_chat",
    "release_standalone_overview_cancel",
    "chat_has_active_session",
    "dm_preview_allowed_for_incident",
    "STANDALONE_DM_SOURCE_CHAT_ID",
    "list_meeting_participants",
    "is_person_in_meeting",
    "add_meeting_participant",
    "remove_meeting_participant",
    "strip_seeded_host_placeholder_for_open_id",
    "departments_line_from_names",
    "format_participants_names_display",
    "get_tenant_token",
    "get_tenant_token_primary",
    "get_tenant_token_for_severity_dm",
    "get_lark_primary_app_credentials",
    "get_lark_severity_app_credentials",
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
    "build_slack_severity_prompt_card",
    "build_slack_minor_role_prompt_card",
    "build_slack_minor_backend_team_card",
    "build_slack_minor_fe_reach_card",
    "build_bilingual_overview_md",
    "apply_slack_minor_card_action",
    "reload_env_runtime",
    "get_incident_group_chat_ids",
    "get_emergency_topic_for_source_chat",
    "get_vc_meeting_topic_for_source_chat",
    "get_overview_post_chat_id",
    "get_overview_target_chat_id_for_source_incident",
    "get_incident_overview_target_map",
    "get_dm_overview_target_chat_id",
    "get_standalone_overview_target_chat_id_for_tag",
    "get_target_group_chat_id",
    "get_owner_ids",
    "get_host_and_dm_open_id",
    "get_p0_trigger_ignore_open_ids",
    "get_incident_group_command_open_ids",
    "can_use_incident_group_commands",
    "get_dm_instruction_open_id",
    "get_dm_instruction_open_ids",
    "get_dm_repost_instruction_after_reset",
    "get_vc_reserve_end_offset_sec",
    "slack_automation_enabled",
    "slack_huddle_on_p0_start",
    "slack_huddle_on_overview_send",
    "slack_severity_prompt_enabled",
    "get_slack_channel_url_for_incident_chat",
    "get_slack_session_dir_for_incident_chat",
    "get_slack_overview_webhook_for_incident_chat",
    "get_slack_incident_notify_webhook_for_incident_chat",
    "get_slack_bot_token",
    "get_slack_app_id",
    "get_slack_bot_user_id",
    "get_slack_api_channel_id_for_incident_chat",
]
