"""Reviewed baseline for the Evolve plugin's declared capability surface.

**Generated — do not hand-edit.** Regenerate with::

    tools/plugin-capability-lint --update-baseline

A diff to this file is the reviewable record that someone accepted a new
privilege for the plugin. Read it that way: every added string is a capability
that ``--accept-capabilities`` will grant on every bot at the next deploy,
without a prompt. See :mod:`evolve_admin.plugin_capability_surface` and
[internal/spec-plugin-install-trust-2026-06-06.md](../../../internal/spec-plugin-install-trust-2026-06-06.md) §4.3.

A Python module rather than a JSON data file on purpose: the install-time check
in ``plugin_signature.verify_plugin_signature`` imports this, and an import that
cannot resolve is a hard failure. A data file could go missing from a built
package and fail open, which is the one thing a trust baseline must never do.
"""

from __future__ import annotations

BASELINE: dict[str, list[str]] = {
    "channels": [],
    "cliBackends": [],
    "cliCommands": [],
    "contracts": [
        "agentToolResultMiddleware: openclaw",
        "tools: board",
        "tools: calendar_create_event",
        "tools: calendar_list_events",
        "tools: channel_set_newcomer_mode",
        "tools: defer",
        "tools: directory_lookup",
        "tools: directory_upsert",
        "tools: drive_list_files",
        "tools: drive_read_file",
        "tools: drive_search",
        "tools: drive_write_file",
        "tools: evolve_help_read",
        "tools: evolve_help_search",
        "tools: expand_app",
        "tools: gmail_archive_message",
        "tools: gmail_delete_message",
        "tools: gmail_get_message",
        "tools: gmail_label_message",
        "tools: gmail_list_labels",
        "tools: gmail_list_messages",
        "tools: gmail_mark_read",
        "tools: gmail_mark_unread",
        "tools: gmail_send",
        "tools: gmail_trash_message",
        "tools: pod_state",
        "tools: record_application",
        "tools: roster_block",
        "tools: roster_set_role",
        "tools: roster_unblock",
        "tools: session_set_tier",
        "tools: submit_intake",
        "tools: users_list",
        "tools: users_whoami"
    ],
    "dangerousConfigFlags": [],
    "hookGrants": [
        "allowConversationAccess"
    ],
    "hooks": [],
    "mcpServers": [],
    "providers": [],
    "skills": [],
    "tools": [
        "board",
        "calendar_create_event",
        "calendar_list_events",
        "channel_set_newcomer_mode",
        "defer",
        "directory_lookup",
        "directory_upsert",
        "drive_list_files",
        "drive_read_file",
        "drive_search",
        "drive_write_file",
        "evolve_help_read",
        "evolve_help_search",
        "expand_app",
        "gmail_archive_message",
        "gmail_delete_message",
        "gmail_get_message",
        "gmail_label_message",
        "gmail_list_labels",
        "gmail_list_messages",
        "gmail_mark_read",
        "gmail_mark_unread",
        "gmail_send",
        "gmail_trash_message",
        "pod_state",
        "record_application",
        "roster_block",
        "roster_set_role",
        "roster_unblock",
        "session_set_tier",
        "submit_intake",
        "users_list",
        "users_whoami"
    ]
}
