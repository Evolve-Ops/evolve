"""tests/test_applications_shared_manifests.py

Regression guard for the **PR #1176 manifest migration** to per-bot
storage. Before #1176, manifests could live at either:

  * ``/Users/<bot>/.openclaw/workspace/manifests/<app>.json`` (bot-side), OR
  * ``{shared_dir}/applications/<bot>/<app>.json`` (shared-side, used by
    first-party Evolve apps like ``security-cve-scan``).

The UI helpers ``_list_manifests_as_bot`` / ``_read_manifest_as_bot``
merged both locations.  PR #1176 moved every manifest bot-side, and the
helpers were simplified to read **only** from the bot's workspace.

These tests pin the new contract: shared-side manifests are NOT picked
up — locking in the migration so we don't regress to the dual-location
code path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import server  # noqa: E402


@pytest.fixture
def fake_shared(tmp_path, monkeypatch):
    """Redirect DEFAULT_SHARED_DIR to a tmp path so the shared-apps lookup
    doesn't hit /Users/Shared/evolve during the test."""
    shared = tmp_path / "shared"
    (shared / "applications" / "evolve").mkdir(parents=True)
    monkeypatch.setattr(server, "DEFAULT_NETWORK_CONFIG", shared / "network.json")
    # Also redirect the config module's DEFAULT_SHARED_DIR via
    # _shared_apps_dir's import — patch in place at the import site.
    from evolve_admin import config as _cfg
    monkeypatch.setattr(_cfg, "DEFAULT_SHARED_DIR", shared)
    return shared


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Stub _bot_manifests_dir to return a tmp path so the test doesn't
    hit /Users/<bot>/ on the real filesystem."""
    workspaces: dict[str, Path] = {}

    def fake_dir(bot_id, user=None):
        wp = tmp_path / "workspaces" / bot_id / "manifests"
        wp.mkdir(parents=True, exist_ok=True)
        workspaces[bot_id] = wp
        return wp

    monkeypatch.setattr(server, "_bot_manifests_dir", fake_dir)
    monkeypatch.setattr(server, "_resolve_bot_user", lambda bot_id: bot_id)
    return workspaces


# ─────────────────────────────────────────────────────────────────────────────
# _list_manifests_as_bot
# ─────────────────────────────────────────────────────────────────────────────


def test_list_ignores_shared_side_post_pr1176(fake_shared, fake_workspace):
    """PR #1176 contract: shared-side manifests are NOT merged into the listing.

    Workspace has one manifest; shared has another. Only the workspace one
    shows up — the shared-side location is dead to the helper.
    """
    server._bot_manifests_dir("evolve")
    wp = fake_workspace["evolve"]
    (wp / "app_in_workspace.json").write_text('{"id": "app_in_workspace"}')

    # Shared: would have been picked up pre-#1176, ignored now.
    sp = fake_shared / "applications" / "evolve"
    (sp / "security-cve-scan.json").write_text('{"id": "security-cve-scan"}')

    paths = server._list_manifests_as_bot("evolve")
    names = sorted(Path(p).name for p in paths)
    assert names == ["app_in_workspace.json"], (
        "shared-side manifest leaked into listing — the dual-location "
        "merge was supposed to be removed in PR #1176"
    )


def test_list_returns_empty_when_workspace_empty_even_if_shared_has_apps(
    fake_shared, fake_workspace,
):
    """Pre-#1176 the listing would have returned the shared app. Now it
    returns nothing — the workspace is authoritative."""
    server._bot_manifests_dir("evolve")  # materialize empty workspace
    sp = fake_shared / "applications" / "evolve"
    (sp / "security-cve-scan.json").write_text('{"id": "security-cve-scan"}')

    paths = server._list_manifests_as_bot("evolve")
    assert paths == [], (
        "shared-side manifest leaked into listing — the bot-side-only "
        "lookup post-#1176 should return empty when workspace is empty"
    )


def test_list_workspace_only_when_shared_absent(fake_shared, fake_workspace):
    """Most bots: workspace has manifests, shared/applications/<bot>/
    doesn't exist. Behavior must be the same as before this change."""
    server._bot_manifests_dir("admin_bot")
    wp = fake_workspace["admin_bot"]
    (wp / "gmail_fetcher.json").write_text('{"id": "gmail_fetcher"}')
    (wp / "app_email_assistant.json").write_text('{"id": "app_email_assistant"}')

    paths = server._list_manifests_as_bot("admin_bot")
    names = sorted(Path(p).name for p in paths)
    assert names == ["app_email_assistant.json", "gmail_fetcher.json"]


def test_list_returns_empty_when_both_empty(fake_shared, fake_workspace):
    server._bot_manifests_dir("personal_bot")  # materialize empty workspace
    # No shared subdir for personal_bot either
    paths = server._list_manifests_as_bot("personal_bot")
    assert paths == []


def test_list_filters_hidden_and_history_files(fake_shared, fake_workspace):
    """``.scan-status.json`` and ``*_history*`` files must not leak into
    the listing — the workspace path still has the same filter rules
    after #1176."""
    server._bot_manifests_dir("evolve")
    wp = fake_workspace["evolve"]
    (wp / ".scan-status.json").write_text('{"phase": 4}')
    (wp / "_history").mkdir(exist_ok=True)
    (wp / "app_history.json").write_text('{"id": "x"}')  # contains "_history"
    (wp / "real-app.json").write_text('{"id": "real-app"}')

    paths = server._list_manifests_as_bot("evolve")
    names = [Path(p).name for p in paths]
    assert names == ["real-app.json"]


def test_list_dedups_when_same_filename_in_both(fake_shared, fake_workspace):
    """If a manifest happens to exist in BOTH locations, the workspace
    copy wins (the bot's own tools touch it; shared is the fallback)."""
    server._bot_manifests_dir("evolve")
    wp = fake_workspace["evolve"]
    (wp / "dup.json").write_text('{"id": "dup", "src": "workspace"}')

    sp = fake_shared / "applications" / "evolve"
    (sp / "dup.json").write_text('{"id": "dup", "src": "shared"}')

    paths = server._list_manifests_as_bot("evolve")
    assert len(paths) == 1
    # Workspace copy wins
    assert "workspaces" in paths[0]


# ─────────────────────────────────────────────────────────────────────────────
# _read_manifest_as_bot
# ─────────────────────────────────────────────────────────────────────────────


def test_read_returns_none_when_workspace_missing_even_if_shared_has_app(
    fake_shared, fake_workspace,
):
    """Pre-#1176 this would have read from the shared path. Post-#1176
    the read is bot-side-only — if the workspace doesn't have it, the
    helper returns None even when the shared path does."""
    server._bot_manifests_dir("evolve")
    sp = fake_shared / "applications" / "evolve"
    (sp / "security-cve-scan.json").write_text(
        json.dumps({"id": "security-cve-scan", "name": "Security CVE Scan"})
    )

    manifest = server._read_manifest_as_bot("evolve", "security-cve-scan")
    assert manifest is None, (
        "shared-side manifest was returned — the dual-location read "
        "was supposed to be removed in PR #1176"
    )


def test_read_prefers_workspace_when_both_have_same_app(
    fake_shared, fake_workspace
):
    """Workspace wins on duplicate — same as listing semantics."""
    server._bot_manifests_dir("evolve")
    wp = fake_workspace["evolve"]
    (wp / "dup.json").write_text(json.dumps({"id": "dup", "src": "workspace"}))
    sp = fake_shared / "applications" / "evolve"
    (sp / "dup.json").write_text(json.dumps({"id": "dup", "src": "shared"}))

    manifest = server._read_manifest_as_bot("evolve", "dup")
    assert manifest["src"] == "workspace"


def test_read_returns_none_when_neither_exists(fake_shared, fake_workspace):
    server._bot_manifests_dir("personal_bot")
    assert server._read_manifest_as_bot("personal_bot", "nonexistent") is None
