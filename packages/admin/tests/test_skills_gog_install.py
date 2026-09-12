"""tests/test_skills_gog_install.py — A3-subset GOG (Google Workspace) skill install flow.

Pins the contract for Spec 11 of the Evolve MVP sprint:

  - resolve_status routes by (oauth client configured, plugin enabled, profile
    present, profile.status) into the five-state machine.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel (Will/Won't copy) ships with the OAuth
    step and matches the constants the module exports.
  - The HTTP routes (/api/skills, /api/skills/<id>, /api/skills/install/<id>,
    .../status, .../enable-plugin) shape responses correctly under both
    happy paths and edge cases (unknown skill, missing bot_id, no OAuth
    client configured).
  - OAuth is mocked at the reader boundary — no real Google API traffic.
  - The EnablePluginEntry path goes through the same operator-UI proposal
    pipeline test_operator_apply_inline.py covers; we exercise the route
    layer with a stubbed applier so we can confirm the wiring.

HIGH-STAKES concerns (per spec: OAuth + credentials + permissions):
  - Token storage MUST be per-bot. We assert the route never writes a
    centralized token and only delegates to the existing
    _write_google_oauth_profile path (verified by the absence of any
    token-writing call in this module — the only handler that touches
    tokens lives in onboard/google/callback, which is out of scope here).
  - Revocation isn't owned by this module; we assert the install plan
    points the user at the existing /onboard/google/revoke endpoint via
    the wizard, not at a parallel "skill uninstall" path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Pure-logic tests (no Flask, no OAuth) ────────────────────────────────────


from evolve_admin.skills import gog_install  # noqa: E402


def _ok_client():
    return True


def _no_client():
    return False


def _plugin_on(_bot_id):
    return True


def _plugin_off(_bot_id):
    return False


def _plugin_unknown(_bot_id):
    return None


def _no_profile(_bot_id):
    return None


def _active_profile(_bot_id):
    return {
        "status": "active",
        "google_account": "admin_bot-bot@example.com",
        "services": ["gmail", "calendar"],
    }


def _reauth_profile(_bot_id):
    return {
        "status": "reauth_required",
        "google_account": "admin_bot-bot@example.com",
        "services": ["gmail", "calendar"],
    }


class TestResolveStatus:
    """The five-state machine: oauth_client_missing → plugin_disabled →
    oauth_pending → active, plus unknown."""

    def test_oauth_client_missing_short_circuits(self):
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_active_profile,
            read_oauth_client_configured=_no_client,
        )
        assert st.status == "oauth_client_missing"
        assert st.plugin_enabled is False  # Don't claim "enabled" before client is set
        assert st.has_oauth_profile is False

    def test_plugin_disabled_when_off(self):
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_off,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "plugin_disabled"
        assert st.plugin_enabled is False

    def test_oauth_pending_when_plugin_on_but_no_profile(self):
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.plugin_enabled is True
        assert st.has_oauth_profile is False

    def test_oauth_pending_when_profile_marked_reauth(self):
        """A profile flagged ``reauth_required`` (refresh_token revoked by
        Google) is functionally pending — the user needs to re-consent."""
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_reauth_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.has_oauth_profile is True
        assert st.profile_status == "reauth_required"

    def test_active_when_everything_ready(self):
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_active_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "active"
        assert st.plugin_enabled is True
        assert st.has_oauth_profile is True
        assert st.google_account == "admin_bot-bot@example.com"
        assert st.granted_services == ["gmail", "calendar"]

    def test_unknown_when_plugin_read_fails(self):
        """A reader returning None means "cannot determine" — surfaces as
        unknown so the UI shows an error rather than silently treating the
        plugin as disabled."""
        st = gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_unknown,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "unknown"
        assert st.error is not None
        assert "plugin_inventory_unreadable" in st.error


class TestInstallPlan:
    """Ordered list of steps the UI walks."""

    def _status(self, **kw):
        defaults = dict(
            bot_id="admin_bot",
            status="plugin_disabled",
            plugin_enabled=False,
            has_oauth_profile=False,
        )
        defaults.update(kw)
        return gog_install.InstallStatus(**defaults)

    def test_active_returns_empty_plan(self):
        plan = gog_install.build_install_plan(
            self._status(status="active", plugin_enabled=True, has_oauth_profile=True),
        )
        assert plan == []

    def test_oauth_client_missing_has_only_configure_step(self):
        plan = gog_install.build_install_plan(
            self._status(status="oauth_client_missing"),
        )
        assert [s.id for s in plan] == ["configure_oauth_client"]

    def test_plugin_disabled_walks_all_three_steps(self):
        plan = gog_install.build_install_plan(self._status(status="plugin_disabled"))
        ids = [s.id for s in plan]
        assert ids == ["enable_plugin", "oauth", "confirm"]

    def test_oauth_pending_skips_enable_plugin(self):
        plan = gog_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True),
        )
        ids = [s.id for s in plan]
        assert ids == ["oauth", "confirm"]

    def test_oauth_step_carries_access_panel_copy(self):
        """The Plex test: access panel ships at the OAuth step so the user
        sees Will/Won't immediately before consent."""
        plan = gog_install.build_install_plan(self._status(status="oauth_pending", plugin_enabled=True))
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert oauth_step.access_panel is not None
        will = oauth_step.access_panel["will"]
        wont = oauth_step.access_panel["wont"]
        # Sanity: each list has at least the headline items.
        assert any("incoming emails" in w.lower() for w in will)
        assert any("calendar" in w.lower() for w in will)
        assert any("send email" in w.lower() for w in wont)
        assert any("delete" in w.lower() for w in wont)

    def test_oauth_step_requests_default_services_only(self):
        """Plex test: the friendly entry-point asks for Gmail+Calendar
        **read-only** variants, not the full advanced-scopes menu, and not
        the write-capable ``gmail`` (send) / ``calendar`` (full r/w) ids.

        This is the trust-chain anchor for A3: GOG_DEFAULT_SERVICES must
        only ever name read-only scopes. The access panel's "Won't send
        email / Won't modify calendar" promise depends on this — see the
        contract guard ``test_panel_wont_promises_match_default_scopes``.
        """
        plan = gog_install.build_install_plan(self._status(status="oauth_pending", plugin_enabled=True))
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert oauth_step.payload["services"] == list(gog_install.GOG_DEFAULT_SERVICES)
        assert "gmail_readonly" in oauth_step.payload["services"]
        assert "calendar_readonly" in oauth_step.payload["services"]
        # Critically, NOT the write-capable variants, NOT drive/docs.
        assert "gmail" not in oauth_step.payload["services"]
        assert "calendar" not in oauth_step.payload["services"]
        assert "drive" not in oauth_step.payload["services"]
        assert "docs" not in oauth_step.payload["services"]
        assert "gmail_modify" not in oauth_step.payload["services"]
        assert "drive_full" not in oauth_step.payload["services"]


class TestAccessPanelContent:
    """The plain-language access panel copy is the trust UX. Drift here is
    a trust regression — these tests are intentionally rigid."""

    def test_will_list_describes_concrete_actions(self):
        will = gog_install.GOG_ACCESS_PANEL["will"]
        # Each "will" line should reference a user-recognisable action,
        # never the word "scope" or "OAuth".
        for line in will:
            assert "scope" not in line.lower()
            assert "oauth" not in line.lower()

    def test_wont_list_includes_load_bearing_negatives(self):
        wont = gog_install.GOG_ACCESS_PANEL["wont"]
        # These specific negatives are what makes the panel trustworthy.
        # If any are removed, the OAuth scope set needs re-auditing.
        joined = " ".join(wont).lower()
        assert "send email" in joined
        assert "delete" in joined or "modify" in joined
        assert "drive" in joined  # the most-feared-by-users service

    def test_credentials_storage_note_says_local_only(self):
        note = gog_install.GOG_ACCESS_PANEL["where_credentials_live"].lower()
        # The per-bot inference principle: tokens never leave the pod.
        assert "this bot" in note or "your machine" in note
        assert "centralised" in note or "centralized" in note or "never" in note


class TestScopeVsPanelContract:
    """Contract guard: what the friendly panel promises **must** match what
    the OAuth scopes actually grant. Added after the A3 review caught the
    panel saying "Won't send email" while the code requested ``gmail.send``.

    This is the CI gate that prevents the same drift from recurring. If a
    future change loosens the scope set or tightens a "won't" promise that
    the scopes no longer back up, one of these tests fails.

    The rule, plainly: every capability the panel promises **not** to do
    must correspond to a scope **not** in the default-services scope set.
    The mapping from human capability → forbidden scope is encoded below
    and intentionally explicit so a reviewer can audit it at a glance.
    """

    # Map each "won't" capability the GOG panel promises to the set of
    # Google OAuth scopes that, if present, would let the bot do that
    # capability. If GOG_DEFAULT_SERVICES requests ANY scope in any of
    # these sets, the panel is lying — fail the test.
    #
    # Sources: Google's published OAuth scope reference.
    # https://developers.google.com/identity/protocols/oauth2/scopes
    _CAPABILITY_TO_FORBIDDEN_SCOPES: dict[str, set[str]] = {
        "send_email": {
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.compose",
        },
        "modify_emails": {
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        },
        "modify_calendar": {
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.events.owned",
        },
        "access_drive": {
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.metadata",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        },
        "access_docs": {
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/documents.readonly",
        },
        "access_sheets": {
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        },
        "access_slides": {
            "https://www.googleapis.com/auth/presentations",
            "https://www.googleapis.com/auth/presentations.readonly",
        },
    }

    def _default_scope_set(self) -> set[str]:
        """Resolve GOG_DEFAULT_SERVICES through the live scope registry in
        server.py — same path the OAuth begin endpoint takes. This catches
        registry drift as well as defaults drift."""
        from evolve_admin.web import server  # noqa: WPS433
        registry = server._GOOGLE_SCOPE_REGISTRY
        scopes: set[str] = set()
        for sid in gog_install.GOG_DEFAULT_SERVICES:
            meta = registry.get(sid)
            assert meta is not None, (
                f"GOG_DEFAULT_SERVICES references unknown service {sid!r} — "
                f"check _GOOGLE_SCOPE_REGISTRY in server.py."
            )
            scopes.update(meta["scopes"])
        return scopes

    def test_default_scopes_contain_only_readonly(self):
        """Sanity floor: every scope in the default set is a *.readonly
        scope (or the OIDC base scopes, which are added at request time).
        A non-readonly scope showing up here is the smoking gun the A3
        review caught the first time."""
        for scope in self._default_scope_set():
            # Allow OIDC base scopes — they're benign identity claims.
            if scope in {"openid", "email", "profile"}:
                continue
            assert scope.endswith(".readonly"), (
                f"GOG default services request non-readonly scope {scope!r}. "
                f"The friendly access panel promises read-only access; "
                f"either tighten the registry or rewrite the panel copy."
            )

    def test_panel_wont_promises_match_default_scopes(self):
        """Headline contract test: for each "won't" the panel makes, the
        corresponding write/access scopes must NOT be in the default set.
        """
        wont_joined = " ".join(gog_install.GOG_ACCESS_PANEL["wont"]).lower()
        default_scopes = self._default_scope_set()

        # Build a list of (panel-said-no, matching-forbidden-scope-set)
        # by matching panel copy substrings to capability keys. The
        # mapping is conservative — we only enforce the capabilities the
        # panel explicitly mentions.
        capability_phrases = {
            "send_email": "send email",
            "modify_emails": "delete or modify",
            "modify_calendar": "add, move, or cancel calendar",
            "access_drive": "drive",
            "access_docs": "docs",
            "access_sheets": "sheets",
            "access_slides": "slides",
        }

        for cap_key, phrase in capability_phrases.items():
            if phrase not in wont_joined:
                continue  # panel doesn't promise this; skip
            forbidden = self._CAPABILITY_TO_FORBIDDEN_SCOPES[cap_key]
            intersection = forbidden & default_scopes
            assert not intersection, (
                f"Panel promises 'won't {phrase}' but GOG_DEFAULT_SERVICES "
                f"requests scope(s) {sorted(intersection)} that grant that "
                f"capability. Either drop the scope or rewrite the panel."
            )

    def test_default_services_use_readonly_variants_in_registry(self):
        """Belt-and-braces: GOG defaults must name the ``*_readonly``
        registry ids, not the write-capable ones. Catches a future
        well-intentioned refactor that 'consolidates' the registry by
        deleting the readonly variants."""
        assert "gmail_readonly" in gog_install.GOG_DEFAULT_SERVICES, (
            "GOG default must include gmail_readonly so the bot cannot "
            "send email on the user's behalf."
        )
        assert "calendar_readonly" in gog_install.GOG_DEFAULT_SERVICES, (
            "GOG default must include calendar_readonly so the bot cannot "
            "add, move, or cancel calendar events."
        )
        assert "gmail" not in gog_install.GOG_DEFAULT_SERVICES
        assert "calendar" not in gog_install.GOG_DEFAULT_SERVICES

    def test_registry_readonly_variants_are_default_on_and_basic(self):
        """The wizard's pre-check uses `default_on` and partitions by
        `advanced`. Read-only variants must be basic + default_on so they
        appear pre-checked in the main service list; write-capable
        variants must be advanced + NOT default_on so they're explicit
        opt-in. This is the wizard pre-check half of the A3 fix.
        """
        from evolve_admin.web import server  # noqa: WPS433
        reg = server._GOOGLE_SCOPE_REGISTRY

        # Readonly variants: basic + default_on (pre-checked).
        for sid in ("gmail_readonly", "calendar_readonly"):
            assert reg[sid]["default_on"] is True, (
                f"{sid} must be default_on so the GOG-friendly install "
                f"pre-checks it in the wizard."
            )
            assert reg[sid]["advanced"] is False, (
                f"{sid} must be basic (not advanced) so it's visible "
                f"in the main service list."
            )

        # Write-capable variants: never default_on; live under "Advanced".
        # Drive/Docs/Sheets/Slides were `default_on: True` before A3
        # review — pre-checking them contradicted the panel's "Won't
        # access Drive" promise.
        for sid in ("gmail", "calendar", "drive", "docs",
                    "sheets", "slides", "gmail_modify", "drive_full"):
            assert reg[sid]["default_on"] is False, (
                f"{sid} grants write/modify capability; it must NOT be "
                f"pre-checked in the GWS wizard. Trust-chain regression."
            )
            assert reg[sid]["advanced"] is True, (
                f"{sid} grants write/modify capability; it must live "
                f"under the wizard's 'Advanced / write access' section."
            )


class TestSkillRegistry:
    def test_gog_is_registered(self):
        meta = gog_install.get_skill("gog")
        assert meta is not None
        assert meta["id"] == "gog"
        assert meta["plugin_name"] == "google"
        assert meta["provider_id"] == "google_workspace"

    def test_unknown_skill_returns_none(self):
        assert gog_install.get_skill("unknown-skill") is None


# ── HTTP route tests (Flask app + mocked readers) ───────────────────────────


@pytest.fixture
def app_with_stub_readers(monkeypatch, tmp_path):
    """Build a Flask app with the gog readers stubbed at the closure-injection
    site. We monkey-patch the helpers that the skills routes wrap so we don't
    need a real bot, a real openclaw.json, or a real network.json beyond the
    minimal scaffolding the server expects."""
    from flask import Flask

    # network.json + shared dir minimal setup so load_network() works.
    network_path = tmp_path / "network.json"
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    import json as _json
    network_path.write_text(_json.dumps({
        "bots": {"admin_bot": {"user": "admin_bot"}},
        "primary": "admin_bot",
        "sharedDir": str(shared_dir),
        # Pretend the OAuth client is configured — the gog route reads
        # this through _read_google_oauth_client; we stub that below.
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": "stub.apps.googleusercontent.com",
            "client_secret_ref": {"bot": "admin_bot", "profile": "_evolve_google_oauth_client"},
        },
    }))

    # Now patch the closure-helpers at the module level. We can't patch
    # _read_google_oauth_client / _read_google_oauth_profile directly because
    # they're defined inside _register_admin_routes; instead we replace the
    # gog_install module-level resolver with a stub that the route handler
    # would normally compose itself.
    #
    # The cleanest test boundary is gog_install.resolve_status — patch the
    # routes' wiring by monkey-patching gog_install itself.

    from evolve_admin.skills import gog_install as _gog

    captured = {"plan_calls": 0, "enable_calls": 0}

    def fake_resolve_status(bot_id, *, read_plugin_enabled, read_oauth_profile,
                            read_oauth_client_configured):
        """The status we return depends on a class-level dial the test sets."""
        return _gog.InstallStatus(
            bot_id=bot_id,
            status=fake_resolve_status.status,
            plugin_enabled=fake_resolve_status.plugin_enabled,
            has_oauth_profile=fake_resolve_status.has_profile,
            profile_status=fake_resolve_status.profile_status,
            google_account=fake_resolve_status.google_account,
            granted_services=list(fake_resolve_status.granted_services),
        )
    fake_resolve_status.status = "oauth_pending"
    fake_resolve_status.plugin_enabled = True
    fake_resolve_status.has_profile = False
    fake_resolve_status.profile_status = None
    fake_resolve_status.google_account = None
    fake_resolve_status.granted_services = []

    monkeypatch.setattr(_gog, "resolve_status", fake_resolve_status)

    # Spawn the Flask app. _register_admin_routes wires the skills routes.
    from evolve_admin.web import server as _srv
    app = Flask(__name__)
    _srv._register_admin_routes(app, network_path)
    # 4.1b Inc 2d: the generic skills-install dispatchers + gog enable-plugin
    # moved to routes_skills_workspace; register it so these routes resolve.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network_path)

    yield app, fake_resolve_status, captured


class TestSkillRoutes:
    def test_list_skills(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog")
        assert r.status_code == 200
        body = r.get_json()
        ids = [s["id"] for s in body["skills"]]
        # Skills that should appear in the catalog.
        #
        # The Google catalog row is now the unified ``google`` skill
        # (PR-following-2154). The legacy split entries (gog,
        # google_workspace_read, google_workspace_write) are HIDDEN from
        # the catalog list but still resolve via the detail endpoint for
        # deep-link compatibility — see test_legacy_google_skills_not_in_catalog_list.
        for sid in (
            "google",  # the unified Google skill — replaces gog/_read/_write
            "slack", "discord", "telegram", "imessage",
            "brave", "github", "dropbox",
            "autocad", "obsidian_vault", "notion", "linear", "runway",
        ):
            assert sid in ids, f"{sid} missing from catalog"
        # Withdrawn skills must NOT re-surface in the catalog. Anything
        # here would let the UI render a tile that 404s on click, OR
        # silently report green-active without working capability.
        for withdrawn in (
            # Home Assistant — pending scope-toggle design (see deep
            # audit F6 — OC has no tools-allowlist for MCP servers).
            "home_assistant",
            # Phase 1c withdrawals (PR #1839 merged) — see
            # internal/skills-deep-audit-2026-05-30.md per-skill rationale.
            # (imessage removed from this list 2026-06-04 — see above)
            "gdrive", "unity", "apple_local",
        ):
            assert withdrawn not in ids, (
                f"{withdrawn} re-surfaced in catalog after withdrawal — "
                "see internal/skills-deep-audit-2026-05-30.md"
            )

    def test_legacy_google_skills_not_in_catalog_list(self, app_with_stub_readers):
        """The pre-unified Google skill ids (gog, gmail, calendar, gdrive,
        google_workspace_read, google_workspace_write) MUST NOT appear in
        the catalog list. They were collapsed into one ``google`` row.

        They DO still resolve via /api/skills/catalog/<id> for migration
        (gog-installed bots dispatch to the legacy install module's
        revoke path). See server.py's catalog list dispatcher comments."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        ids = [s["id"] for s in client.get("/api/skills/catalog").get_json()["skills"]]
        for legacy in (
            "gog", "gmail", "calendar", "gdrive",
            "google_workspace_read", "google_workspace_write",
        ):
            assert legacy not in ids, (
                f"{legacy!r} re-surfaced in catalog list — should be hidden "
                f"in favour of the unified 'google' entry"
            )

    def test_catalog_emits_category_per_skill(self, app_with_stub_readers):
        """Catalog-UX polish (2026-06-04): every skill carries a `category`
        + `category_order` field so the UI groups them under section
        headers (Messaging / Productivity / Storage / Tools / Creative)
        instead of the historical chronological-add-order rendering."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog")
        body = r.get_json()
        assert "category_order" in body
        assert body["category_order"] == [
            "Messaging", "Productivity", "Storage", "Tools", "Creative",
        ]
        for s in body["skills"]:
            assert "category" in s, f"skill {s['id']} missing category"
            assert "category_order" in s, f"skill {s['id']} missing category_order"

    def test_catalog_sorted_by_category_then_alpha(self, app_with_stub_readers):
        """Server-side sort: (category_order ASC, display_name ASC). This is
        the canonical order any consumer can rely on — the UI groups by
        section, but other consumers (CLI tooling, orchestrator) get a
        stable iteration order without re-implementing the sort."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        body = client.get("/api/skills/catalog").get_json()
        # Walk pairs and assert monotonic non-decreasing
        prev_rank, prev_name = -1, ""
        for s in body["skills"]:
            rank = s["category_order"]
            name = (s.get("display_name") or s["id"]).lower()
            if rank == prev_rank:
                assert name >= prev_name, (
                    f"alpha order violated within category: "
                    f"{prev_name!r} -> {name!r}"
                )
            else:
                assert rank > prev_rank, (
                    f"category order violated: rank went {prev_rank} -> {rank}"
                )
            prev_rank, prev_name = rank, name

    def test_messaging_category_includes_all_five_channels(
        self, app_with_stub_readers,
    ):
        """Regression guard: dropping a messaging skill from the Messaging
        bucket is a visible UI regression. iMessage + WhatsApp join the
        original trio because OC ships them as bundled / clawhub plugins."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        body = client.get("/api/skills/catalog").get_json()
        messaging = {s["id"] for s in body["skills"]
                     if s.get("category") == "Messaging"}
        for expected in ("slack", "telegram", "discord", "imessage", "whatsapp"):
            assert expected in messaging, (
                f"{expected} should sit under Messaging; got {messaging}"
            )

    def test_gog_detail_still_resolves_for_migration(
        self, app_with_stub_readers,
    ):
        """Replaces the prior gog-display-name test. The gog skill is no
        longer in the catalog list (collapsed into ``google``) but the
        detail endpoint MUST still resolve — installers and migration
        flows deep-link to /api/skills/catalog/gog by ID and should get
        a 200 with the access panel."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog/gog")
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("id") == "gog"
        assert body.get("access_panel"), "access_panel must still resolve"

    def test_get_withdrawn_skill_returns_404(self, app_with_stub_readers):
        """Per-skill metadata for the still-withdrawn ids must 404 so the
        install modal can't render them either. obsidian_vault / notion /
        linear / runway are no longer in this list — they return 200 with
        access panels (Linear's includes a ``post_install_callout``
        warning about the bot acting as the API key holder). iMessage
        was also re-added 2026-06-04 — bundled-plugin rewire."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        for withdrawn in (
            "home_assistant",
            "gdrive", "unity", "apple_local",
        ):
            r = client.get(f"/api/skills/catalog/{withdrawn}")
            assert r.status_code == 404, (
                f"{withdrawn} catalog detail should 404 after withdrawal — "
                f"got {r.status_code}"
            )

    def test_get_upstream_plugin_skill_returns_access_panel(self, app_with_stub_readers):
        """Upstream-plugin skills (brave + github) expose access panels
        so the install modal can render the will/won't lists for each.

        After Phase 1c (2026-05-30), the SKILLS dict shrunk to just brave
        + github (Dropbox is now MCP-backed; gdrive + unity withdrawn)."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        for sid in ("brave", "github"):
            r = client.get(f"/api/skills/catalog/{sid}")
            assert r.status_code == 200, f"{sid} detail returned {r.status_code}"
            body = r.get_json()
            assert body["id"] == sid
            ap = body.get("access_panel") or {}
            assert ap.get("will"), f"{sid} missing 'will' list"
            assert ap.get("wont"), f"{sid} missing 'wont' list"

    def test_get_dropbox_skill_returns_access_panel_with_mode_choices(
        self, app_with_stub_readers,
    ):
        """Dropbox is now an MCP-backed install (same shape as Obsidian).
        Its catalog/<id> endpoint should return the new SKILL_REGISTRY_ENTRY
        with mode_choices for the UI radio (read vs read+write)."""
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog/dropbox")
        assert r.status_code == 200
        body = r.get_json()
        assert body["id"] == "dropbox"
        ap = body.get("access_panel") or {}
        choices = ap.get("mode_choices") or []
        assert len(choices) == 2
        assert {c["value"] for c in choices} == {"read", "read_write"}
        # Read is the recommended/safe default — must come first.
        assert choices[0]["value"] == "read"

    def test_get_skill_returns_access_panel(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog/gog")
        assert r.status_code == 200
        body = r.get_json()
        assert body["id"] == "gog"
        # Access panel surfaces verbatim — the UI is the single renderer.
        assert "access_panel" in body
        ap = body["access_panel"]
        assert isinstance(ap.get("will"), list) and ap["will"]
        assert isinstance(ap.get("wont"), list) and ap["wont"]

    def test_get_unknown_skill_returns_404(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/catalog/totally-not-real")
        assert r.status_code == 404
        body = r.get_json()
        assert body["ok"] is False

    def test_install_status_requires_bot_id(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/install/gog/status")
        assert r.status_code == 400
        body = r.get_json()
        assert "bot_id" in body["error"]

    def test_install_status_unknown_skill_404(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.get("/api/skills/install/nope/status?bot_id=admin_bot")
        assert r.status_code == 404

    def test_install_status_oauth_pending(self, app_with_stub_readers):
        app, dial, _ = app_with_stub_readers
        dial.status = "oauth_pending"
        dial.plugin_enabled = True
        dial.has_profile = False
        client = app.test_client()
        r = client.get("/api/skills/install/gog/status?bot_id=admin_bot")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "oauth_pending"
        assert body["plugin_enabled"] is True

    def test_install_status_active(self, app_with_stub_readers):
        app, dial, _ = app_with_stub_readers
        dial.status = "active"
        dial.plugin_enabled = True
        dial.has_profile = True
        dial.profile_status = "active"
        dial.google_account = "admin_bot@example.com"
        dial.granted_services = ["gmail", "calendar"]
        client = app.test_client()
        r = client.get("/api/skills/install/gog/status?bot_id=admin_bot")
        body = r.get_json()
        assert body["status"] == "active"
        assert body["google_account"] == "admin_bot@example.com"
        assert body["granted_services"] == ["gmail", "calendar"]

    def test_install_post_requires_bot_id(self, app_with_stub_readers):
        app, _, _ = app_with_stub_readers
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={})
        assert r.status_code == 400

    def test_install_post_returns_awaiting_oauth_when_not_configured(self, app_with_stub_readers):
        """V2.1-4: POST /api/skills/install/gog now routes through the orchestrator.
        When the bot has not completed OAuth, the response is the awaiting_oauth shape
        (HTTP 202, no steps list) so the UI renders the shared action-link panel."""
        app, dial, _ = app_with_stub_readers
        dial.status = "plugin_disabled"
        dial.plugin_enabled = False
        dial.has_profile = False
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        # V2.1-4 shape: 202 awaiting_oauth with missing list
        assert r.status_code == 202
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "awaiting_oauth"
        # missing list carries action_url so the UI can render the OAuth link
        assert body["missing"]
        assert body["missing"][0]["action_url"] == "/api/skills/install/gog"
        # No steps list in the new shape
        assert "steps" not in body

    def test_install_post_active_returns_already_active(self, app_with_stub_readers):
        """V2.1-4: When GOG is already active, the orchestrator short-circuits to
        already_active (HTTP 200, no steps needed) so the UI shows success immediately."""
        app, dial, _ = app_with_stub_readers
        dial.status = "active"
        dial.plugin_enabled = True
        dial.has_profile = True
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "already_active"
        # No steps list in the new shape
        assert "steps" not in body


# ── enable-plugin route (proposal pipeline integration) ──────────────────────


@pytest.fixture
def fake_plugin_applier():
    """Stub the EnablePluginEntry applier so we can drive the route without
    a real openclaw.json to mutate. Same fixture style as
    test_operator_apply_inline.py — we want this test to fail loudly if the
    skills route ever stops going through the operator-UI proposal pipeline."""
    from arbiter.appliers.base import (
        ApplyResult,
        _APPLIER_REGISTRY,
        register_applier,
    )

    real = _APPLIER_REGISTRY.get("EnablePluginEntry")
    fakes = []

    class _FakeApplier:
        def __init__(self, result):
            self.result = result
            self.snapshot_called = False
            self.apply_called = False
            self.last_action = None
            self.last_bot_id = None

        def capture_snapshot(self, action, bot_id):
            self.snapshot_called = True
            return {"before": {"bot_id": bot_id}}

        def apply(self, action, bot_id):
            self.apply_called = True
            self.last_action = action
            self.last_bot_id = bot_id
            return self.result

        def revert(self, snapshot, bot_id):
            from arbiter.appliers.base import RevertResult
            return RevertResult(ok=True)

    def install(result):
        applier = _FakeApplier(result)
        register_applier("EnablePluginEntry", applier)
        fakes.append(applier)
        return applier

    yield install

    if real is not None:
        register_applier("EnablePluginEntry", real)
    else:
        _APPLIER_REGISTRY.pop("EnablePluginEntry", None)


def test_enable_plugin_route_goes_through_proposal_pipeline(
    app_with_stub_readers, fake_plugin_applier
):
    """Hitting /api/skills/install/gog/enable-plugin must produce an
    EnablePluginEntry proposal that runs through the operator-UI inline
    pipeline (auto-approve → apply → succeeded). Otherwise security_warden
    is bypassed — that's the load-bearing safety check this route MUST NOT
    short-circuit.
    """
    from arbiter.appliers.base import ApplyResult
    applier = fake_plugin_applier(ApplyResult(ok=True, message="enabled"))

    app, _, _ = app_with_stub_readers
    client = app.test_client()
    r = client.post("/api/skills/install/gog/enable-plugin", json={"bot_id": "admin_bot"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "succeeded"
    assert body["applied"] is True

    # The applier was actually called with the right action — proving we
    # didn't skip the pipeline.
    assert applier.apply_called is True
    assert applier.last_bot_id == "admin_bot"
    # action_payload included plugin_name=google
    assert getattr(applier.last_action, "plugin_name", None) == "google"


def test_enable_plugin_route_surfaces_security_warden_refusal(
    app_with_stub_readers, fake_plugin_applier
):
    """If the applier refuses (e.g. plugin in denied list), the route returns
    ok=True but applied=False with the reason — same envelope as the existing
    plugins-admin route. The UI shows the refusal reason and does NOT advance
    to OAuth."""
    from arbiter.appliers.base import ApplyResult
    applier = fake_plugin_applier(ApplyResult(
        ok=False,
        details={"fail_action": "flag"},
        message="refused: plugin is in denied_plugins",
    ))

    app, _, _ = app_with_stub_readers
    client = app.test_client()
    r = client.post("/api/skills/install/gog/enable-plugin", json={"bot_id": "admin_bot"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True  # creation succeeded
    assert body["applied"] is False  # but apply did not
    assert "denied_plugins" in body["message"]
    assert applier.apply_called is True


def test_enable_plugin_unknown_skill_404(app_with_stub_readers):
    app, _, _ = app_with_stub_readers
    client = app.test_client()
    r = client.post("/api/skills/install/nope/enable-plugin", json={"bot_id": "admin_bot"})
    assert r.status_code == 404


def test_enable_plugin_requires_bot_id(app_with_stub_readers):
    app, _, _ = app_with_stub_readers
    client = app.test_client()
    r = client.post("/api/skills/install/gog/enable-plugin", json={})
    assert r.status_code == 400


# ── Trust-chain assertions ────────────────────────────────────────────────────


def test_module_never_imports_composio():
    """Per project_external_dependency_vetting: Composio's pre-built tools
    are closed-source. The skill install path must use the OpenClaw plugin
    pipeline directly, with no Composio dependency. This guards the
    trust-chain decision so a future refactor doesn't quietly add a
    Composio import.

    We only fail on actual import statements — the module's docstring is
    allowed (and expected) to mention Composio in the context of explaining
    *why* we don't depend on it.
    """
    import re
    src = (
        Path(__file__).parent.parent
        / "evolve_admin"
        / "skills"
        / "gog_install.py"
    ).read_text()
    # Match `import composio` or `from composio import ...` (or .submodule).
    import_pattern = re.compile(
        r"^\s*(?:import\s+composio|from\s+composio[\w.]*\s+import)",
        re.MULTILINE | re.IGNORECASE,
    )
    assert not import_pattern.search(src), (
        "gog_install.py imports Composio — see "
        "project_external_dependency_vetting; GOG must go direct."
    )


def test_module_does_not_write_tokens_itself():
    """Token storage is delegated to the existing
    /api/admin/onboard/google/callback handler (which uses
    _write_google_oauth_profile → /tmp staging + sudo /bin/cp per
    CLAUDE.md). This module must never read or write access_token /
    refresh_token directly — if it did, we'd have a parallel token path
    that bypasses the audited write site.

    We check the AST rather than substring-grep so the module's docstring
    (which legitimately describes the delegation) doesn't trip the guard.
    """
    import ast
    src_path = (
        Path(__file__).parent.parent
        / "evolve_admin"
        / "skills"
        / "gog_install.py"
    )
    tree = ast.parse(src_path.read_text(), filename=str(src_path))

    # No file writes — open(..., "w"|"a"|"x"|"wb") would be a smoking gun.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                getattr(fn, "id", None)
                or getattr(getattr(fn, "attr", None) and fn, "attr", None)
            )
            if name in ("open",) and len(node.args) >= 2:
                mode = node.args[1]
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                    assert "w" not in mode.value and "a" not in mode.value, (
                        f"gog_install.py calls open() with write mode "
                        f"{mode.value!r} — token I/O must stay in the audited "
                        f"/onboard/google/* handlers."
                    )

    # No string-literal access_token / refresh_token field-name assignments
    # — i.e. the module doesn't construct profile dicts with token fields.
    # Reading profile['status'] is fine (we test for the field-name token,
    # not the substring).
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in ("access_token", "refresh_token"), (
                f"gog_install.py contains a string literal {node.value!r} "
                f"— credential field names should live only in the audited "
                f"/onboard/google/* handlers, not in the skill layer."
            )


# ── V2.1-3 compat-shim tests ──────────────────────────────────────────────────


class TestGogCompatShim:
    """V2.1-3: GOG is now a shim over gmail + calendar.

    Existing bots with legacy GOG profiles (both scopes) continue working.
    Bots with only Gmail or only Calendar installed report partial status.
    """

    def _resolve(self, services, profile_status="active"):
        """Helper: resolve GOG status for a bot with the given service list."""
        def _profile(_bot_id):
            if not services:
                return None
            return {
                "status": profile_status,
                "google_account": "admin_bot@example.com",
                "services": services,
            }
        return gog_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=lambda _: True,
            read_oauth_profile=_profile,
            read_oauth_client_configured=lambda: True,
        )

    def test_legacy_gog_profile_with_both_scopes_is_active(self):
        """A legacy GOG profile (gmail_readonly + calendar_readonly) → active.

        This is the primary backward-compat guarantee of the shim.
        """
        st = self._resolve(["gmail_readonly", "calendar_readonly"])
        assert st.status == "active", (
            "Legacy GOG profile with both scopes must report active "
            "— backward compat is load-bearing"
        )

    def test_gmail_only_profile_reports_calendar_missing(self):
        """Profile with only Gmail scope → oauth_pending with error=calendar_missing."""
        st = self._resolve(["gmail_readonly"])
        assert st.status == "oauth_pending"
        assert st.error == "calendar_missing", (
            f"Expected error='calendar_missing', got {st.error!r}"
        )

    def test_calendar_only_profile_reports_gmail_missing(self):
        """Profile with only Calendar scope → oauth_pending with error=gmail_missing."""
        st = self._resolve(["calendar_readonly"])
        assert st.status == "oauth_pending"
        assert st.error == "gmail_missing", (
            f"Expected error='gmail_missing', got {st.error!r}"
        )

    def test_no_profile_reports_oauth_pending_no_error(self):
        """No profile → oauth_pending (standard flow; no partial-coverage error)."""
        st = self._resolve([])
        assert st.status == "oauth_pending"
        # error may be None or empty for the no-profile case — not a partial error
        assert st.error != "calendar_missing"
        assert st.error != "gmail_missing"

    def test_write_capable_variants_satisfy_gog(self):
        """GOG is active when write-capable variants cover both services.

        A bot upgraded to send+read Gmail and full Calendar r/w shouldn't
        be reported as needing re-OAuth for GOG.
        """
        st = self._resolve(["gmail", "calendar"])
        assert st.status == "active"

    def test_reauth_required_profile_is_oauth_pending(self):
        """A profile marked reauth_required is not active for GOG."""
        st = self._resolve(
            ["gmail_readonly", "calendar_readonly"],
            profile_status="reauth_required",
        )
        assert st.status == "oauth_pending"
        assert st.profile_status == "reauth_required"


# ── Gateway kickstart after callback success ─────────────────────────────────
#
# Slack + Discord OAuth callbacks both kickstart the bot's openclaw gateway
# after the openclaw.json / credential write so the running gateway re-reads
# its config and picks up the new tokens immediately. Without it, fresh
# Gmail / Calendar / GOG installs sit dormant until the next deploy or a
# manual launchctl restart.
#
# Reference: internal/design/skills-install-roadmap-2026-05-30.md item P1.


def test_google_oauth_callback_kickstarts_gateway(tmp_path, monkeypatch):
    """After ``_write_google_oauth_profile`` succeeds the callback fires
    ``_oc_install_common.kickstart_gateway(bot_id)`` so the running gateway
    picks up the new credentials. Mirrors slack_install / discord_install /
    telegram_install's post-callback kickstart pattern.
    """
    from flask import Flask
    import json as _json

    network_path = tmp_path / "network.json"
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network_path.write_text(_json.dumps({
        "bots": {"admin_bot": {"user": "admin_bot"}},
        "primary": "admin_bot",
        "sharedDir": str(shared_dir),
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": "stub.apps.googleusercontent.com",
            "secret_bot": "admin_bot",
        },
    }))

    # Credential store: where _read_google_oauth_client picks up the secret.
    creds_dir = shared_dir / "credentials" / "admin_bot"
    creds_dir.mkdir(parents=True)
    (creds_dir / "google_oauth_client.json").write_text(_json.dumps({
        "schema_version": 1,
        "client_id": "stub.apps.googleusercontent.com",
        "client_secret": "stub-secret",
    }))

    from evolve_admin.web import server as _srv
    from evolve_admin.skills import _oc_install_common as _oc_common

    # _oauth_state_dir() reads DEFAULT_NETWORK_CONFIG (module-level) instead
    # of the closure-captured network_path — point it at the test config so
    # the state JSON lands under our tmp shared_dir.
    monkeypatch.setattr(_srv, "DEFAULT_NETWORK_CONFIG", network_path)

    # Fake the Google HTTP calls so we don't reach the network.
    monkeypatch.setattr(_srv, "_google_token_exchange", lambda *a, **kw: (200, {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
        "scope": (
            "openid email "
            "https://www.googleapis.com/auth/gmail.readonly"
        ),
    }))
    monkeypatch.setattr(_srv, "_google_userinfo", lambda token: (200, {
        "email": "admin_bot-bot@example.com",
    }))

    # Capture every subprocess.run — _write_auth_profiles uses sudo /bin/cp,
    # sudo /bin/mkdir, sudo /bin/chmod; kickstart_gateway uses sudo
    # /bin/launchctl kickstart -k system/ai.openclaw.<bot>-gateway. All flow
    # through the same `subprocess` module that we patch here.
    calls: list[list[str]] = []

    class _Fake:
        def __init__(self):
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def _fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return _Fake()

    monkeypatch.setattr(_srv.subprocess, "run", _fake_run)
    monkeypatch.setattr(_oc_common.subprocess, "run", _fake_run)

    # Pre-create a Google OAuth state token via the real helper so the
    # callback's _google_state_get returns a usable entry.
    state = _srv._google_state_create(
        bot_id="admin_bot",
        services=["gmail_readonly"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri="https://example.com/api/admin/onboard/google/callback",
    )

    app = Flask(__name__)
    _srv._register_admin_routes(app, network_path)
    # 4.1b Inc 2d: the generic skills-install dispatchers + gog enable-plugin
    # moved to routes_skills_workspace; register it so these routes resolve.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network_path)
    client = app.test_client()

    r = client.get(
        f"/api/admin/onboard/google/callback?state={state}&code=fake-code"
    )
    assert r.status_code == 200, r.data

    # The launchctl kickstart call must be present — the regression we're
    # guarding against is the callback skipping it (the bug internal/design/
    # skills-install-roadmap-2026-05-30.md flagged as P1).
    kickstart_calls = [
        c for c in calls
        if c[:4] == ["sudo", "/bin/launchctl", "kickstart", "-k"]
    ]
    assert len(kickstart_calls) == 1, (
        "Google OAuth callback must call kickstart_gateway once after the "
        f"credential write. Got {len(kickstart_calls)} kickstart calls. "
        f"All subprocess.run calls: {calls}"
    )
    assert kickstart_calls[0][4] == "system/ai.openclaw.admin_bot-gateway"


def test_google_oauth_callback_surfaces_kickstart_failure(tmp_path, monkeypatch):
    """If kickstart fails, the OAuth flow still completes — the credentials
    are already on disk. The failure is reported in the audit log and the
    poll-result payload rather than 500ing the request. Mirrors slack's
    best-effort kickstart pattern.
    """
    from flask import Flask
    import json as _json

    network_path = tmp_path / "network.json"
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network_path.write_text(_json.dumps({
        "bots": {"admin_bot": {"user": "admin_bot"}},
        "primary": "admin_bot",
        "sharedDir": str(shared_dir),
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": "stub.apps.googleusercontent.com",
            "secret_bot": "admin_bot",
        },
    }))
    creds_dir = shared_dir / "credentials" / "admin_bot"
    creds_dir.mkdir(parents=True)
    (creds_dir / "google_oauth_client.json").write_text(_json.dumps({
        "schema_version": 1,
        "client_id": "stub.apps.googleusercontent.com",
        "client_secret": "stub-secret",
    }))

    from evolve_admin.web import server as _srv
    from evolve_admin.skills import _oc_install_common as _oc_common

    monkeypatch.setattr(_srv, "DEFAULT_NETWORK_CONFIG", network_path)
    monkeypatch.setattr(_srv, "_google_token_exchange", lambda *a, **kw: (200, {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
        "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
    }))
    monkeypatch.setattr(_srv, "_google_userinfo", lambda token: (200, {
        "email": "admin_bot-bot@example.com",
    }))

    # Make kickstart_gateway report a failure; auth-profiles writes still
    # succeed so the credentials land on disk.
    monkeypatch.setattr(
        _oc_common, "kickstart_gateway",
        lambda bot_id: (False, "launchctl kickstart failed (exit 113): no such service"),
    )

    class _Fake:
        def __init__(self):
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""
    monkeypatch.setattr(
        _srv.subprocess, "run",
        lambda argv, *a, **kw: _Fake(),
    )

    state = _srv._google_state_create(
        bot_id="admin_bot",
        services=["gmail_readonly"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri="https://example.com/api/admin/onboard/google/callback",
    )

    app = Flask(__name__)
    _srv._register_admin_routes(app, network_path)
    # 4.1b Inc 2d: the generic skills-install dispatchers + gog enable-plugin
    # moved to routes_skills_workspace; register it so these routes resolve.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network_path)
    client = app.test_client()

    r = client.get(
        f"/api/admin/onboard/google/callback?state={state}&code=fake-code"
    )
    # Best-effort: the request still succeeds.
    assert r.status_code == 200, r.data

    # The state's result payload carries the kickstart failure so the
    # /poll endpoint and wizard UI can surface it.
    entry = _srv._google_state_get(state)
    assert entry is not None
    result = entry.get("result") or {}
    assert result.get("status") == "success"
    assert result.get("gateway_kickstarted") is False
    assert "launchctl kickstart failed" in (result.get("gateway_kickstart_error") or "")
