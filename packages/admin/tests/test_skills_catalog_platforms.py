"""Channel-matrix platform awareness (design-linux-port-2026-06-10.md §8).

Upstream OpenClaw requires a macOS host for the iMessage channel — a hard
constraint, not a missing feature. The channel catalog therefore carries a
``platforms`` field as DATA on each skill's ``SKILL_REGISTRY_ENTRY`` (absent
means platform-neutral), and every surface that OFFERS channels filters
through ``evolve_admin.skills.supported_on_host()``:

  - ``GET /api/skills/catalog``            — list never includes iMessage on Linux
  - ``GET /api/skills/catalog/imessage``   — deep-link 404s on Linux
  - ``POST /api/skills/install/imessage/set-handle`` — refused (409) on Linux

The product-defaults litmus: a fresh Linux pod must not render a dead
iMessage affordance (show-and-fail is the anti-pattern these tests pin
against). Inventory/status surfaces stay unfiltered — they report what is
actually installed.

The admin conftest pins the MACOS profile per test (autouse); Linux-behavior
tests call ``set_profile(LINUX)`` inside the test body and teardown restores
MACOS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin.skills import supported_on_host  # noqa: E402
from evolve_admin.skills import imessage_install as _imessage  # noqa: E402
from evolve_admin.skills import discord_install as _discord  # noqa: E402
from evolve_admin.skills import signal_install as _signal  # noqa: E402
from evolve_admin.skills import slack_install as _slack  # noqa: E402
from evolve_admin.skills import telegram_install as _telegram  # noqa: E402
from evolve_admin.skills import whatsapp_install as _whatsapp  # noqa: E402


# ── supported_on_host (the single filter primitive) ──────────────────────────

class TestSupportedOnHost:
    def test_entry_without_platforms_is_neutral_on_both(self):
        entry = {"id": "anychannel"}
        assert supported_on_host(entry, profile=MACOS) is True
        assert supported_on_host(entry, profile=LINUX) is True

    def test_empty_platforms_list_is_neutral(self):
        """Empty list means 'no constraint declared', not 'nowhere'."""
        entry = {"id": "anychannel", "platforms": []}
        assert supported_on_host(entry, profile=LINUX) is True

    def test_macos_only_entry_filters_by_profile(self):
        entry = {"id": "imessage", "platforms": ["macos"]}
        assert supported_on_host(entry, profile=MACOS) is True
        assert supported_on_host(entry, profile=LINUX) is False

    def test_default_profile_respects_set_profile_override(self):
        entry = {"id": "imessage", "platforms": ["macos"]}
        # conftest pins MACOS — the default-arg path resolves it.
        assert supported_on_host(entry) is True
        set_profile(LINUX)
        assert supported_on_host(entry) is False
        # conftest teardown restores MACOS.


# ── Catalog data: the constraint lives on the registry entries ───────────────

class TestRegistryPlatformData:
    def test_imessage_is_macos_only(self):
        assert _imessage.SKILL_REGISTRY_ENTRY.get("platforms") == ["macos"]

    @pytest.mark.parametrize("mod", [
        _slack, _telegram, _discord, _whatsapp, _signal,
    ], ids=["slack", "telegram", "discord", "whatsapp", "signal"])
    def test_every_other_channel_is_platform_neutral(self, mod):
        """Slack/Telegram/Discord/WhatsApp/Signal run anywhere OpenClaw
        does — they must not declare a platforms constraint (design §8:
        everything but iMessage is platform-neutral)."""
        assert not mod.SKILL_REGISTRY_ENTRY.get("platforms")


# ── Flask routes: every offering surface filters ─────────────────────────────

@pytest.fixture
def app_client(tmp_path):
    from flask import Flask
    from evolve_admin.web import server as _srv

    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"admin_bot": {"user": "admin_bot"}},
    }))
    app = Flask(__name__)
    _srv._register_admin_routes(app, network)
    # 4.1b Inc 2d: the imessage install handlers + generic dispatchers moved to
    # routes_skills_workspace; register it so the install-route guard resolves.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network)
    return app.test_client()


class TestCatalogListRoute:
    def test_macos_pod_offers_imessage(self, app_client):
        r = app_client.get("/api/skills/catalog")
        ids = [s["id"] for s in r.get_json()["skills"]]
        assert "imessage" in ids

    def test_linux_pod_never_lists_imessage(self, app_client):
        set_profile(LINUX)
        r = app_client.get("/api/skills/catalog")
        ids = [s["id"] for s in r.get_json()["skills"]]
        assert "imessage" not in ids

    def test_imessage_is_the_only_platform_filtered_entry(self, app_client):
        """The Linux catalog is the macOS catalog minus exactly iMessage —
        pins that the filter never silently eats a platform-neutral
        channel (and that no new macOS-only entry lands unnoticed)."""
        macos_ids = {s["id"] for s in
                     app_client.get("/api/skills/catalog").get_json()["skills"]}
        set_profile(LINUX)
        linux_ids = {s["id"] for s in
                     app_client.get("/api/skills/catalog").get_json()["skills"]}
        assert macos_ids - linux_ids == {"imessage"}
        assert linux_ids <= macos_ids

    def test_imessage_entry_carries_platforms_data(self, app_client):
        """The constraint rides through the API as data so consumers
        (UI pills, CLI tooling) can render 'macOS-only' without a
        hardcoded skill list."""
        r = app_client.get("/api/skills/catalog")
        entry = next(s for s in r.get_json()["skills"] if s["id"] == "imessage")
        assert entry["platforms"] == ["macos"]


class TestCatalogDetailRoute:
    def test_macos_pod_serves_imessage_detail(self, app_client):
        r = app_client.get("/api/skills/catalog/imessage")
        assert r.status_code == 200
        body = r.get_json()
        assert body["id"] == "imessage"
        assert body["platforms"] == ["macos"]

    def test_linux_pod_404s_imessage_deep_link(self, app_client):
        set_profile(LINUX)
        r = app_client.get("/api/skills/catalog/imessage")
        assert r.status_code == 404
        assert r.get_json()["error"] == "skill_unavailable_on_platform"

    def test_linux_pod_still_serves_neutral_channel_detail(self, app_client):
        set_profile(LINUX)
        r = app_client.get("/api/skills/catalog/telegram")
        assert r.status_code == 200
        assert r.get_json()["id"] == "telegram"


class TestInstallRouteGuard:
    def test_linux_pod_refuses_imessage_set_handle(self, app_client):
        """The mutating install route refuses BEFORE any config write —
        a curl past the hidden catalog card must not half-wire a channel
        upstream can't run on this host."""
        set_profile(LINUX)
        r = app_client.post(
            "/api/skills/install/imessage/set-handle",
            json={"bot_id": "admin_bot", "handle": "+15551234567"},
        )
        assert r.status_code == 409
        assert r.get_json()["error"] == "skill_unavailable_on_platform"
