"""Route-table golden — the spine of the routes_admin.py decomposition (4.1b).

Strategy memo: docs/design-routes-admin-decomposition-2026-06-12.md §4.1.

Every increment of the decomposition moves route handlers (and the helpers
they close over) between modules. The external HTTP contract must NOT change:
no route added, removed, renamed, re-pathed, or re-method-ed. This test pins
that contract by snapshotting, for every ``/api/admin``, ``/api/models``, and
``/api/skills`` rule, the tuple::

    (sorted HTTP methods, rule string, endpoint basename)

The *endpoint basename* (the function name, e.g. ``api_admin_config_get``) is
stable across a code move — only the endpoint's *module* changes, which the
basename strips out. So a clean code-motion increment leaves this snapshot
byte-identical.

Increment 0 moves no routes, so this snapshot is trivially identical; the
test exists now so every *later* increment is gated by it. If a future
increment legitimately changes the route table, the golden in
``_GOLDEN_ROUTE_TABLE`` is updated in that same PR with the diff visible in
review.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Prefixes whose rules this golden pins (the surfaces register_admin_routes
# and its decomposition siblings own).
_PINNED_PREFIXES = ("/api/admin", "/api/models", "/api/skills")

# Werkzeug adds these to every rule's methods; drop them so the snapshot
# reflects only the methods a handler declares.
_IMPLICIT_METHODS = {"HEAD", "OPTIONS"}


def _route_table(app) -> list[tuple[tuple[str, ...], str, str]]:
    """(sorted explicit methods, rule string, endpoint basename) for every
    pinned rule, sorted for a stable, diff-friendly snapshot."""
    rows: list[tuple[tuple[str, ...], str, str]] = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith(_PINNED_PREFIXES):
            continue
        methods = tuple(sorted((rule.methods or set()) - _IMPLICIT_METHODS))
        # endpoint is "<module>.<func>" or just "<func>"; keep only the func.
        basename = rule.endpoint.rsplit(".", 1)[-1]
        rows.append((methods, rule.rule, basename))
    return sorted(rows)


def _build_app():
    from evolve_admin.web.server import create_app, DEFAULT_NETWORK_CONFIG
    return create_app(network_path=DEFAULT_NETWORK_CONFIG)


def test_route_table_matches_golden():
    """The pinned route table equals the recorded golden. A decomposition
    increment that moves handlers must leave this unchanged; if it changes,
    the golden is updated in the same PR (review sees the diff)."""
    app = _build_app()
    actual = _route_table(app)

    # Self-heal helper: when this assert fails legitimately (a deliberate
    # route change), regenerate the golden with:
    #   uv run python -m pytest <thisfile> -q  # read the printed table, or:
    #   uv run python -c "from tests.test_routes_admin_table_golden import \
    #       _build_app,_route_table; import pprint; \
    #       pprint.pprint(_route_table(_build_app()))"
    assert actual == _GOLDEN_ROUTE_TABLE, (
        "Pinned /api/admin|/api/models|/api/skills route table drifted. "
        "If this is a deliberate route change, update _GOLDEN_ROUTE_TABLE "
        "in this file in the same PR. If this is a decomposition increment, "
        "it MUST be byte-identical — a route silently vanished or changed "
        "method/path/handler-name.\n"
        f"Added rows:   {sorted(set(actual) - set(_GOLDEN_ROUTE_TABLE))}\n"
        f"Removed rows: {sorted(set(_GOLDEN_ROUTE_TABLE) - set(actual))}"
    )


def test_route_table_is_nonempty_and_unique():
    """Guard against the golden silently degenerating to []. Also asserts no
    two pinned rules share a (methods, rule) pair — that would mean the
    snapshot can't distinguish two distinct registrations."""
    app = _build_app()
    rows = _route_table(app)
    assert len(rows) > 100, f"expected the full admin surface, got {len(rows)} rows"
    seen: set[tuple[tuple[str, ...], str]] = set()
    for methods, rule, _basename in rows:
        key = (methods, rule)
        assert key not in seen, f"duplicate pinned registration: {key}"
        seen.add(key)


# ── Golden snapshot ───────────────────────────────────────────────────────────
# Generated from `create_app(DEFAULT_NETWORK_CONFIG)` on the 4.1b checkpoint
# branch at Increment 0. Regenerate (and review the diff) only when a route is
# deliberately added/removed/re-pathed/re-methoded in a PR.
_GOLDEN_ROUTE_TABLE: list[tuple[tuple[str, ...], str, str]] = [
    (('DELETE',), '/api/admin/keys/<bot_id>/<provider>', 'api_admin_remove_key'),
    (('GET',), '/api/admin/_peer-identity', 'admin_peer_identity'),
    (('GET',), '/api/admin/bot/<bot_id>/backup-status', 'admin_bot_backup_status'),
    (('GET',), '/api/admin/bot/<bot_id>/openclaw-config', 'admin_bot_openclaw_config'),
    (('GET',), '/api/admin/bots/<bot_id>/channels', 'bot_channels_list'),
    (('GET',), '/api/admin/bots/<bot_id>/directory', 'bot_directory_list'),
    (('GET',), '/api/admin/bots/<bot_id>/pairing/lookup', 'pairing_lookup'),
    (('GET',), '/api/admin/bots/<bot_id>/pairing/state', 'pairing_state'),
    (('GET',), '/api/admin/bots/<bot_id>/person-link', 'person_link_get'),
    (('GET',), '/api/admin/bots/<bot_id>/setup-checklist', 'setup_checklist_get'),
    (('GET',), '/api/admin/bots/<bot_id>/users', 'bot_users_list'),
    (('GET',), '/api/admin/bots/<bot_id>/users/tier-prefs', 'bot_users_tier_prefs'),
    (('GET',), '/api/admin/config/<bot_id>', 'api_admin_config_get'),
    (('GET',), '/api/admin/config/pod/models', 'api_admin_config_pod_models_get'),
    (('GET',), '/api/admin/debug/paths/<bot_id>', 'api_admin_debug_paths'),
    (('GET',), '/api/admin/embedding-config/<bot_id>', 'api_admin_embedding_config_get'),
    (('GET',), '/api/admin/engine-tier-override', 'api_admin_engine_tier_override_get'),
    (('GET',), '/api/admin/github-dev/status', 'api_github_dev_status'),
    (('GET',), '/api/admin/https-setup/status', 'api_https_setup_status'),
    (('GET',), '/api/admin/identity', 'identity_overview'),
    (('GET',), '/api/admin/integration-token/<bot_id>/discord/check', 'api_admin_discord_integration_check'),
    (('GET',), '/api/admin/integration-token/<bot_id>/github/check', 'api_admin_github_integration_check'),
    (('GET',), '/api/admin/integration-token/<bot_id>/whatsapp/check', 'api_admin_whatsapp_integration_check'),
    (('GET',), '/api/admin/keys/<bot_id>', 'api_admin_get_keys'),
    (('GET',), '/api/admin/keys/<bot_id>/<provider>', 'api_admin_get_keys_one_provider'),
    (('GET',), '/api/admin/keys/<bot_id>/<provider>/config', 'api_admin_get_keys_config'),
    (('GET',), '/api/admin/keys/borrow-candidates', 'api_admin_keys_borrow_candidates'),
    (('GET',), '/api/admin/models/<bot_id>', 'api_admin_get_models'),
    (('GET',), '/api/admin/onboard/github/discover-default-pat', 'api_admin_onboard_discover_default_pat'),
    (('GET',), '/api/admin/onboard/google/callback', 'api_admin_onboard_google_callback'),
    (('GET',), '/api/admin/onboard/google/status', 'api_admin_onboard_google_status'),
    (('GET',), '/api/admin/pairing/config', 'pairing_config_index'),
    (('GET',), '/api/admin/remediation/job/<job_id>', 'remediation_job'),
    (('GET',), '/api/admin/remediation/jobs', 'remediation_jobs_list'),
    (('GET',), '/api/admin/service/logs', 'api_service_logs'),
    (('GET',), '/api/admin/service/status', 'api_service_status'),
    (('GET',), '/api/admin/usage/<bot_id>', 'api_admin_get_usage'),
    (('GET',), '/api/admin/version', 'api_admin_version'),
    (('GET',), '/api/models/auto-upgrade', 'api_models_auto_upgrade_get'),
    (('GET',), '/api/models/discoveries', 'api_models_discoveries'),
    (('GET',), '/api/models/freshness-status', 'api_models_freshness_status'),
    (('GET',), '/api/models/listings', 'api_models_listings'),
    (('GET',), '/api/models/tier-resolution', 'api_models_tier_resolution'),
    (('GET',), '/api/skills/<bot_id>', 'api_skills_bot'),
    (('GET',), '/api/skills/catalog', 'api_skills_catalog_list'),
    (('GET',), '/api/skills/catalog/<skill_id>', 'api_skills_catalog_get'),
    (('GET',), '/api/skills/install/<skill_id>/status', 'api_skills_install_status'),
    (('GET',), '/api/skills/install/discord/status', 'api_skills_discord_status'),
    (('GET',), '/api/skills/install/slack/oauth-callback', 'api_skills_slack_oauth_callback'),
    (('GET',), '/api/skills/install/slack/status', 'api_skills_slack_status'),
    (('GET',), '/api/skills/install/telegram/status', 'api_skills_telegram_status'),
    (('GET',), '/api/skills/pod', 'api_skills_pod'),
    (('PATCH',), '/api/admin/bots/<bot_id>/users/<channel>/<ext_id>', 'bot_users_patch'),
    (('POST',), '/api/admin/applications/coherence-check', 'admin_applications_coherence_check'),
    (('POST',), '/api/admin/bots/<bot_id>/channels/add', 'bot_channels_add'),
    (('POST',), '/api/admin/bots/<bot_id>/directory/<platform>/<stable_id>/contact', 'bot_directory_contact'),
    (('POST',), '/api/admin/bots/<bot_id>/directory/<platform>/<stable_id>/email', 'bot_directory_email'),
    (('POST',), '/api/admin/bots/<bot_id>/pairing/commit', 'pairing_commit'),
    (('POST',), '/api/admin/bots/<bot_id>/person-link/link', 'person_link_post'),
    (('POST',), '/api/admin/bots/<bot_id>/person-link/unlink', 'person_unlink_post'),
    (('POST',), '/api/admin/bots/<bot_id>/setup-checklist/items/<item_id>', 'setup_checklist_set_item'),
    (('POST',), '/api/admin/bots/<bot_id>/setup-checklist/reset', 'setup_checklist_reset'),
    (('POST',), '/api/admin/bots/<bot_id>/setup-checklist/suppress', 'setup_checklist_suppress'),
    (('POST',), '/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/block', 'bot_users_block'),
    (('POST',), '/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/ignore', 'bot_users_ignore'),
    (('POST',), '/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/unblock', 'bot_users_unblock'),
    (('POST',), '/api/admin/bots/<bot_id>/users/approve', 'bot_users_approve'),
    (('POST',), '/api/admin/bots/<bot_id>/users/group-allowlist/approve', 'bot_users_group_approve'),
    (('POST',), '/api/admin/bots/<bot_id>/users/group-allowlist/revoke', 'bot_users_group_revoke'),
    (('POST',), '/api/admin/bots/<bot_id>/users/reject', 'bot_users_reject'),
    (('POST',), '/api/admin/bots/<bot_id>/users/revoke', 'bot_users_revoke'),
    (('POST',), '/api/admin/breakers/reset', 'admin_breakers_reset'),
    (('POST',), '/api/admin/breakers/trip', 'admin_breakers_trip'),
    (('POST',), '/api/admin/gateway/<bot_id>/restart', 'api_admin_restart_gateway'),
    (('POST',), '/api/admin/gateway/<bot_id>/stop', 'api_admin_stop_gateway'),
    (('POST',), '/api/admin/identity/claim-admin', 'identity_claim_admin'),
    (('POST',), '/api/admin/identity/claim-primary', 'identity_claim_primary'),
    (('POST',), '/api/admin/identity/clear-primary', 'identity_clear_primary'),
    (('POST',), '/api/admin/identity/discover-primary', 'identity_discover_primary'),
    (('POST',), '/api/admin/identity/resolve-name', 'identity_resolve_name'),
    (('POST',), '/api/admin/identity/revoke-admin', 'identity_revoke_admin'),
    (('POST',), '/api/admin/identity/set-bot-passphrase', 'identity_set_bot_passphrase'),
    (('POST',), '/api/admin/identity/set-pod-passphrase', 'identity_set_pod_passphrase'),
    (('POST',), '/api/admin/infra/<daemon_id>/restart', 'admin_infra_daemon_restart'),
    (('POST',), '/api/admin/integration-token/<bot_id>/discord/rotate', 'api_admin_rotate_discord_integration_token'),
    (('POST',), '/api/admin/integration-token/<bot_id>/github/rotate', 'api_admin_rotate_github_integration_token'),
    (('POST',), '/api/admin/integration-token/<bot_id>/whatsapp/rotate', 'api_admin_rotate_whatsapp_integration_token'),
    (('POST',), '/api/admin/integration-token/<bot_id>/whatsapp/setup', 'api_admin_setup_whatsapp_integration'),
    (('POST',), '/api/admin/keys/<bot_id>/<provider>', 'api_admin_add_key'),
    (('POST',), '/api/admin/keys/<bot_id>/<provider>/borrow', 'api_admin_keys_borrow'),
    (('POST',), '/api/admin/keys/<bot_id>/<provider>/disconnect', 'api_admin_disconnect_provider'),
    (('POST',), '/api/admin/keys/<bot_id>/<provider>/rollback', 'api_admin_rollback_key'),
    (('POST',), '/api/admin/keys/<bot_id>/<provider>/rotate', 'api_admin_rotate_key'),
    (('POST',), '/api/admin/onboard/brave', 'api_admin_onboard_brave'),
    (('POST',), '/api/admin/onboard/brave/verify', 'api_admin_onboard_verify_brave'),
    (('POST',), '/api/admin/onboard/github', 'api_admin_onboard_github'),
    (('POST',), '/api/admin/onboard/github/verify', 'api_admin_onboard_verify_github'),
    (('POST',), '/api/admin/onboard/google/begin', 'api_admin_onboard_google_begin'),
    (('POST',), '/api/admin/onboard/google/configure', 'api_admin_onboard_google_configure'),
    (('POST',), '/api/admin/onboard/google/poll', 'api_admin_onboard_google_poll'),
    (('POST',), '/api/admin/onboard/google/revoke', 'api_admin_onboard_google_revoke'),
    (('POST',), '/api/admin/remediation/execute', 'remediation_execute'),
    (('POST',), '/api/admin/service/install', 'api_service_install'),
    (('POST',), '/api/admin/service/restart', 'api_service_restart'),
    (('POST',), '/api/admin/service/uninstall', 'api_service_uninstall'),
    (('POST',), '/api/models/adopt-all-discoveries-dormant', 'api_models_adopt_all_discoveries_dormant'),
    (('POST',), '/api/models/adopt-discovery', 'api_models_adopt_discovery'),
    (('POST',), '/api/models/apply-all-upgrades', 'api_models_apply_all_upgrades'),
    (('POST',), '/api/models/apply-upgrade', 'api_models_apply_upgrade'),
    (('POST',), '/api/models/check-freshness', 'api_models_check_freshness'),
    (('POST',), '/api/models/easy-setup', 'api_models_easy_setup'),
    (('POST',), '/api/models/freshness-advisory/dismiss', 'api_models_freshness_advisory_dismiss'),
    (('POST',), '/api/models/freshness-advisory/reset', 'api_models_freshness_advisory_reset'),
    (('POST',), '/api/models/ignore-discovery', 'api_models_ignore_discovery'),
    (('POST',), '/api/models/listings/refresh', 'api_models_listings_refresh'),
    (('POST',), '/api/models/update-tier', 'api_models_update_tier'),
    (('POST',), '/api/models/update-tier-bulk', 'api_models_update_tier_bulk'),
    (('POST',), '/api/models/validate-model', 'api_models_validate_model'),
    (('POST',), '/api/skills/install/<skill_id>', 'api_skills_install'),
    (('POST',), '/api/skills/install/<skill_id>/enable-plugin', 'api_skills_enable_plugin'),
    (('POST',), '/api/skills/install/discord', 'api_skills_discord_install_plan'),
    (('POST',), '/api/skills/install/discord/confirm', 'api_skills_discord_confirm'),
    (('POST',), '/api/skills/install/discord/poll', 'api_skills_discord_poll'),
    (('POST',), '/api/skills/install/discord/revoke', 'api_skills_discord_revoke'),
    (('POST',), '/api/skills/install/discord/set-token', 'api_skills_discord_set_token'),
    (('POST',), '/api/skills/install/discord/start-oauth', 'api_skills_discord_start_oauth'),
    (('POST',), '/api/skills/install/dropbox/revoke', 'api_skills_dropbox_revoke'),
    (('POST',), '/api/skills/install/dropbox/set-folder-path', 'api_skills_dropbox_set_folder_path'),
    (('POST',), '/api/skills/install/dropbox/set-mode', 'api_skills_dropbox_set_mode'),
    (('POST',), '/api/skills/install/github/install-mcp-server', 'api_skills_github_install_mcp_server'),
    (('POST',), '/api/skills/install/github/revoke-mcp-server', 'api_skills_github_revoke_mcp_server'),
    (('POST',), '/api/skills/install/google/complete', 'api_skills_google_complete'),
    (('POST',), '/api/skills/install/google/revoke', 'api_skills_google_revoke'),
    (('POST',), '/api/skills/install/google_workspace_write/complete', 'api_skills_gws_write_complete'),
    (('POST',), '/api/skills/install/google_workspace_write/revoke', 'api_skills_gws_write_revoke'),
    (('POST',), '/api/skills/install/imessage/check-tcc', 'api_skills_imessage_check_tcc'),
    (('POST',), '/api/skills/install/imessage/revoke', 'api_skills_imessage_revoke'),
    (('POST',), '/api/skills/install/imessage/set-handle', 'api_skills_imessage_set_handle'),
    (('POST',), '/api/skills/install/linear/revoke', 'api_skills_linear_revoke'),
    (('POST',), '/api/skills/install/linear/set-token', 'api_skills_linear_set_token'),
    (('POST',), '/api/skills/install/notion/revoke', 'api_skills_notion_revoke'),
    (('POST',), '/api/skills/install/notion/set-token', 'api_skills_notion_set_token'),
    (('POST',), '/api/skills/install/obsidian/revoke', 'api_skills_obsidian_revoke'),
    (('POST',), '/api/skills/install/obsidian/set-mode', 'api_skills_obsidian_set_mode'),
    (('POST',), '/api/skills/install/obsidian/set-vault-path', 'api_skills_obsidian_set_vault_path'),
    (('POST',), '/api/skills/install/runway/revoke', 'api_skills_runway_revoke'),
    (('POST',), '/api/skills/install/runway/set-token', 'api_skills_runway_set_token'),
    (('POST',), '/api/skills/install/signal/install-plugin', 'api_skills_signal_install_plugin'),
    (('POST',), '/api/skills/install/signal/pair/<session_id>', 'api_skills_signal_pair_poll'),
    (('POST',), '/api/skills/install/signal/pair/<session_id>/cancel', 'api_skills_signal_pair_cancel'),
    (('POST',), '/api/skills/install/signal/pair/start', 'api_skills_signal_pair_start'),
    (('POST',), '/api/skills/install/signal/revoke', 'api_skills_signal_revoke'),
    (('POST',), '/api/skills/install/signal/set-number', 'api_skills_signal_set_number'),
    (('POST',), '/api/skills/install/slack', 'api_skills_slack_install_plan'),
    (('POST',), '/api/skills/install/slack/poll', 'api_skills_slack_poll'),
    (('POST',), '/api/skills/install/slack/revoke', 'api_skills_slack_revoke'),
    (('POST',), '/api/skills/install/slack/start-oauth', 'api_skills_slack_start_oauth'),
    (('POST',), '/api/skills/install/telegram', 'api_skills_telegram_install_plan'),
    (('POST',), '/api/skills/install/telegram/revoke', 'api_skills_telegram_revoke'),
    (('POST',), '/api/skills/install/telegram/set-token', 'api_skills_telegram_set_token'),
    (('POST',), '/api/skills/install/whatsapp/install-plugin', 'api_skills_whatsapp_install_plugin'),
    (('POST',), '/api/skills/install/whatsapp/pair/<session_id>', 'api_skills_whatsapp_pair_poll'),
    (('POST',), '/api/skills/install/whatsapp/pair/<session_id>/cancel', 'api_skills_whatsapp_pair_cancel'),
    (('POST',), '/api/skills/install/whatsapp/pair/start', 'api_skills_whatsapp_pair_start'),
    (('POST',), '/api/skills/install/whatsapp/revoke', 'api_skills_whatsapp_revoke'),
    (('PUT',), '/api/admin/bot/<bot_id>/auto-memory', 'admin_bot_set_auto_memory'),
    (('PUT',), '/api/admin/bots/<bot_id>/channels/<channel>/newcomer_mode', 'channel_newcomer_mode_put'),
    (('PUT',), '/api/admin/config/<bot_id>/cascade', 'api_admin_config_set_cascade'),
    (('PUT',), '/api/admin/config/<bot_id>/catalog', 'api_admin_config_set_catalog'),
    (('PUT',), '/api/admin/config/<bot_id>/fallback', 'api_admin_config_set_fallback'),
    (('PUT',), '/api/admin/config/<bot_id>/routing', 'api_admin_config_set_routing'),
    (('PUT',), '/api/admin/config/<bot_id>/tier-mode', 'api_admin_config_set_tier_mode'),
    (('PUT',), '/api/admin/config/<bot_id>/tiers', 'api_admin_config_set_tiers'),
    (('PUT',), '/api/admin/config/<bot_id>/user-tier-override', 'api_admin_config_set_user_tier_override'),
    (('PUT',), '/api/admin/config/pod/models', 'api_admin_config_pod_models_set'),
    (('PUT',), '/api/admin/embedding-config/<bot_id>', 'api_admin_embedding_config_set'),
    (('PUT',), '/api/admin/engine-tier-override', 'api_admin_engine_tier_override_put'),
    (('PUT',), '/api/admin/keys/<bot_id>/order', 'api_admin_order_keys'),
    (('PUT',), '/api/admin/models/<bot_id>', 'api_admin_set_models'),
    (('PUT',), '/api/models/auto-upgrade', 'api_models_auto_upgrade_set'),
]
