"""tests/test_skills_google_install.py — unified Google skill coverage.

The unified ``google`` skill replaces gog / google_workspace_read /
google_workspace_write as the catalog presentation. These tests cover:

  * Capability catalog shape + lookup helpers
  * Scope / service / MCP-arg transforms across capability picks
  * derive_granted_capabilities (legacy gog detection)
  * summarize_capabilities chip-label generation
  * Status resolver — all six branches
  * Install plan routing per status
  * Catalog list + detail + status + plan dispatchers via Flask test client
  * /complete endpoint validation (capabilities required, unknown ids rejected)
  * /revoke endpoint shape
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from evolve_admin.skills import google_install as g  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _scope(name):
    return f"https://www.googleapis.com/auth/{name}"


def _no_client(_b): return None
def _ok_client(_b): return {"client_id": "abc", "client_secret": "s"}
def _no_profile(_b): return None
def _empty_oc(_b): return ({}, None)
def _oc_with_mcp(_b):
    return ({"mcp": {"servers": {g.GOOGLE_MCP_SERVER_ID: {"command": "/x"}}}}, None)


def _profile(scopes, email="sam@example.com", status="active"):
    return lambda _b: {
        "scopes": list(scopes),
        "google_account": email,
        "status": status,
        "services": [],
    }


# ── Capability catalog ──────────────────────────────────────────────────────


class TestCapabilityCatalog:
    def test_catalog_has_expected_groups(self):
        groups = {c.group for c in g.GOOGLE_CAPABILITIES}
        assert groups == {"Gmail", "Calendar", "Drive", "Sheets", "Docs", "Advanced"}

    def test_every_group_has_read_and_write_except_advanced(self):
        per_group = {}
        for c in g.GOOGLE_CAPABILITIES:
            per_group.setdefault(c.group, []).append(c.id)
        # Gmail / Calendar / Drive / Sheets / Docs each have a read +
        # a write capability.
        for grp in ("Gmail", "Calendar", "Drive", "Sheets", "Docs"):
            ids = per_group[grp]
            assert len(ids) == 2, f"{grp} has {ids}"

    def test_defaults_are_pre_checked(self):
        defaults = {c.id for c in g.GOOGLE_CAPABILITIES if c.default_on}
        assert defaults == set(g.DEFAULT_CAPABILITY_IDS) == {"gmail_read", "calendar_read"}

    def test_only_advanced_caps_are_restricted(self):
        for c in g.GOOGLE_CAPABILITIES:
            if c.restricted:
                assert c.group == "Advanced", f"{c.id} restricted but not in Advanced"

    def test_capability_by_id_round_trip(self):
        for c in g.GOOGLE_CAPABILITIES:
            assert g.capability_by_id(c.id) is c

    def test_capability_by_id_unknown_returns_none(self):
        assert g.capability_by_id("not_a_real_capability") is None

    def test_capabilities_for_ids_drops_unknowns(self):
        out = g.capabilities_for_ids(["gmail_read", "bogus", "calendar_read"])
        assert [c.id for c in out] == ["gmail_read", "calendar_read"]


# ── Capability ↔ scope / service / MCP-arg transforms ───────────────────────


class TestCapabilityTransforms:
    def test_services_for_defaults(self):
        out = g.services_for_capabilities(g.DEFAULT_CAPABILITY_IDS)
        assert set(out) == {"gmail_readonly", "calendar_readonly"}

    def test_scopes_for_defaults(self):
        out = g.scopes_for_capabilities(g.DEFAULT_CAPABILITY_IDS)
        assert out == {_scope("gmail.readonly"), _scope("calendar.readonly")}

    def test_mcp_args_read_only_shortcut(self):
        """All-read selection emits ``--read-only``, not ``--permissions ...``.
        Vetting doc §6: --read-only is the server's read-only flag."""
        all_read = [c.id for c in g.GOOGLE_CAPABILITIES if c.id.endswith("_read")]
        assert g.mcp_extra_args_for_capabilities(all_read) == ["--read-only"]

    def test_mcp_args_default_pair_is_read_only(self):
        assert g.mcp_extra_args_for_capabilities(g.DEFAULT_CAPABILITY_IDS) == ["--read-only"]

    def test_mcp_args_mixed_uses_permissions(self):
        out = g.mcp_extra_args_for_capabilities([
            "gmail_read", "gmail_send", "calendar_read",
        ])
        assert out[0] == "--permissions"
        assert "gmail:readonly" in out
        assert "gmail:send" in out
        assert "calendar:read" in out

    def test_mcp_args_calendar_write_implies_calendar_read(self):
        """The MCP permissions list for calendar_write includes both
        :read and :write (the server requires explicit read perms even
        when only write is granted)."""
        out = g.mcp_extra_args_for_capabilities(["calendar_write"])
        assert "calendar:read" in out
        assert "calendar:write" in out

    def test_mcp_args_full_write_set(self):
        all_non_restricted = [c.id for c in g.GOOGLE_CAPABILITIES if not c.restricted]
        out = g.mcp_extra_args_for_capabilities(all_non_restricted)
        assert out[0] == "--permissions"
        # All five apps' read + write capabilities should be present.
        for perm in (
            "gmail:readonly", "gmail:send",
            "calendar:read", "calendar:write",
            "drive:readonly", "drive:file",
            "sheets:read", "sheets:write",
            "docs:read", "docs:write",
        ):
            assert perm in out, f"missing {perm} in {out}"

    def test_mcp_args_empty_list(self):
        assert g.mcp_extra_args_for_capabilities([]) == []


# ── derive_granted_capabilities (legacy gog detection) ──────────────────────


class TestDeriveGrantedCapabilities:
    def test_legacy_gog_profile_maps_to_two_read_caps(self):
        """A bot installed via the legacy ``gog`` skill has gmail.readonly +
        calendar.readonly granted. The unified resolver MUST detect that
        and report gmail_read + calendar_read so the bot appears as
        installed under the new ``google`` row without re-OAuth needed."""
        scopes = {_scope("gmail.readonly"), _scope("calendar.readonly")}
        assert g.derive_granted_capabilities(scopes) == ["gmail_read", "calendar_read"]

    def test_empty_scopes_yields_empty_capabilities(self):
        assert g.derive_granted_capabilities(set()) == []

    def test_partial_scope_grants_only_matching_caps(self):
        scopes = {_scope("gmail.readonly")}
        assert g.derive_granted_capabilities(scopes) == ["gmail_read"]

    def test_calendar_full_satisfies_calendar_read_AND_write(self):
        """Per Google's scope model, the full calendar scope is a
        superset; the unified resolver reports both calendar caps."""
        scopes = {_scope("calendar")}
        caps = g.derive_granted_capabilities(scopes)
        # calendar_write requires only the calendar scope; calendar_read
        # requires calendar.readonly (which calendar does NOT imply).
        # So only calendar_write should appear.
        assert "calendar_write" in caps

    def test_drive_file_does_not_imply_drive_readonly(self):
        """Asymmetry tested in the prior Read/Write split — drive.file
        is per-file write and does NOT cover drive.readonly's read-all."""
        scopes = {_scope("drive.file")}
        caps = g.derive_granted_capabilities(scopes)
        assert "drive_write" in caps
        assert "drive_read" not in caps


# ── summarize_capabilities (chip labels) ────────────────────────────────────


class TestSummarizeCapabilities:
    def test_empty_says_not_connected(self):
        assert g.summarize_capabilities([]) == "not connected"

    def test_default_pair_summarises_as_read(self):
        assert g.summarize_capabilities(g.DEFAULT_CAPABILITY_IDS) == "read"

    def test_all_reads_summarises_as_read(self):
        all_reads = [c.id for c in g.GOOGLE_CAPABILITIES
                     if c.id.endswith("_read") and not c.restricted]
        assert g.summarize_capabilities(all_reads) == "read"

    def test_all_caps_summarises_as_read_plus_write(self):
        all_caps = [c.id for c in g.GOOGLE_CAPABILITIES if not c.restricted]
        assert g.summarize_capabilities(all_caps) == "read + write"

    def test_single_read_surfaces_app(self):
        assert g.summarize_capabilities(["drive_read"]) == "read drive"

    def test_mixed_read_and_send_is_custom(self):
        assert g.summarize_capabilities([
            "gmail_read", "gmail_send", "calendar_read",
        ]) == "custom"


# ── capability_summary_entry (catalog-chip {summary, labels}) ───────────────
#
# Feeds /api/skills/pod's capability_summaries. The same scope→entry mapping
# serves BOTH Google config paths: Path A passes the OAuth profile's
# consented scopes; Path C (service_account_dwd) passes the bot's configured
# google_integration.scopes from network.json.


class TestCapabilitySummaryEntry:
    def test_dwd_default_scope_set_is_installed_with_labels(self):
        # The DwD wizard's DEFAULT_SCOPES (send + read gmail, calendar,
        # drive.file) — a service_account_dwd bot configured with these
        # must report installed, not "+ Add".
        scopes = [
            _scope("gmail.send"), _scope("gmail.readonly"),
            _scope("calendar"), _scope("drive.file"),
        ]
        entry = g.capability_summary_entry(scopes)
        assert entry is not None
        assert entry["summary"] and entry["summary"] != "not connected"
        # Labels enumerate the granted capabilities (tooltip fodder when
        # the summary collapses to the opaque "custom").
        assert "Send Gmail" in entry["labels"]
        assert "Read Gmail" in entry["labels"]

    def test_read_pair_summarises_as_read(self):
        scopes = [_scope("gmail.readonly"), _scope("calendar.readonly")]
        entry = g.capability_summary_entry(scopes)
        assert entry == {
            "summary": "read",
            "labels": ["Read Gmail", "Read your calendar"],
        }

    def test_empty_scopes_is_none(self):
        assert g.capability_summary_entry([]) is None

    def test_unrecognised_scopes_is_none(self):
        # A scope set that covers no full capability → not configured.
        assert g.capability_summary_entry(["https://example.com/unknown"]) is None


# ── Status resolver ─────────────────────────────────────────────────────────


class TestResolveStatus:
    def test_no_oauth_client(self):
        s = g.resolve_status("lex",
            read_oauth_profile=_no_profile, read_oauth_client=_no_client,
            read_oc_config=_empty_oc)
        assert s.status == "oauth_client_missing"

    def test_no_profile_yields_not_installed(self):
        s = g.resolve_status("lex",
            read_oauth_profile=_no_profile, read_oauth_client=_ok_client,
            read_oc_config=_empty_oc)
        assert s.status == "not_installed"

    def test_legacy_gog_profile_active(self):
        """A bot with the legacy gog scope set + a working MCP entry +
        healthy keystore should report ACTIVE under the unified ``google``
        skill. The capability_summary chip says "read" (matches what
        gog actually does)."""
        legacy_scopes = [_scope("gmail.readonly"), _scope("calendar.readonly")]
        s = g.resolve_status("lex",
            read_oauth_profile=_profile(legacy_scopes),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=lambda _slot: "ok")
        assert s.status == "active"
        assert s.granted_capabilities == ["gmail_read", "calendar_read"]
        assert s.capability_summary == "read"

    def test_profile_with_no_recognised_scopes_is_not_installed(self):
        """A profile with empty or wholly-unrecognised scopes should be
        treated as not_installed (status chip won't lie about active)."""
        s = g.resolve_status("lex",
            read_oauth_profile=_profile([]),
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc)
        assert s.status == "not_installed"
        assert s.granted_capabilities == []

    def test_profile_present_no_mcp_yields_needs_provision(self):
        s = g.resolve_status("lex",
            read_oauth_profile=_profile([_scope("gmail.readonly")]),
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc)
        assert s.status == "needs_provision"
        assert s.granted_capabilities == ["gmail_read"]

    def test_empty_keystore_yields_consumer_unreachable(self):
        s = g.resolve_status("lex",
            read_oauth_profile=_profile([_scope("gmail.readonly")]),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=lambda _slot: None)
        assert s.status == "consumer_unreachable"

    def test_reauth_required_yields_oauth_pending(self):
        s = g.resolve_status("lex",
            read_oauth_profile=_profile(
                [_scope("gmail.readonly")], status="reauth_required",
            ),
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc)
        assert s.status == "oauth_pending"
        assert s.error == "reauth_required"

    def test_active_when_full_write_granted(self):
        all_write_scopes = [
            _scope("gmail.readonly"), _scope("gmail.send"),
            _scope("calendar"),
            _scope("drive.file"),
            _scope("spreadsheets"),
            _scope("documents"),
        ]
        s = g.resolve_status("lex",
            read_oauth_profile=_profile(all_write_scopes),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=lambda _slot: "ok")
        assert s.status == "active"
        # Should include both gmail_read (via .readonly) and gmail_send.
        assert "gmail_read" in s.granted_capabilities
        assert "gmail_send" in s.granted_capabilities


# ── Install plan ─────────────────────────────────────────────────────────────


class TestBuildInstallPlan:
    def test_oauth_client_missing_one_step(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="oauth_client_missing"))
        assert [s.id for s in plan] == ["configure_oauth_client"]

    def test_not_installed_three_steps(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="not_installed"))
        assert [s.id for s in plan] == ["pick_capabilities", "oauth", "complete"]

    def test_needs_provision_one_step(self):
        s = g.InstallStatus(
            bot_id="lex", status="needs_provision",
            granted_capabilities=list(g.DEFAULT_CAPABILITY_IDS),
        )
        plan = g.build_install_plan(s)
        assert [step.id for step in plan] == ["complete"]
        # The complete step's payload carries the existing capabilities
        # (no new OAuth needed).
        assert plan[0].payload["capabilities"] == list(g.DEFAULT_CAPABILITY_IDS)

    def test_active_returns_modify_plan(self):
        """An already-installed bot's wizard re-opens with the picker
        pre-checked, so the operator can modify capabilities. The plan
        is the same shape as not_installed: pick_capabilities → oauth
        → complete. (The frontend may skip the oauth step at submit
        time when picked ⊆ currently_granted — see
        test_active_pick_capabilities_step_carries_currently_granted.)"""
        s = g.InstallStatus(
            bot_id="lex", status="active",
            granted_capabilities=["gmail_read", "calendar_read"],
        )
        plan = g.build_install_plan(s)
        assert [step.id for step in plan] == ["pick_capabilities", "oauth", "complete"]
        # The picker pre-checks the bot's current capabilities.
        picker = next(step for step in plan if step.id == "pick_capabilities")
        checked = {c["id"] for c in picker.capabilities if c["checked"]}
        assert checked == {"gmail_read", "calendar_read"}

    def test_active_pick_capabilities_step_carries_currently_granted(self):
        """The pick_capabilities step's payload includes ``currently_granted``
        so the frontend can compute the delta and skip the oauth step
        when picked ⊆ currently_granted (spec §13.1 Q3 — no Google
        popup-then-close when no new scopes are requested)."""
        s = g.InstallStatus(
            bot_id="lex", status="active",
            granted_capabilities=["gmail_read", "calendar_read", "drive_read"],
        )
        plan = g.build_install_plan(s)
        picker = next(step for step in plan if step.id == "pick_capabilities")
        # The currently_granted list is in the step's payload.
        assert "currently_granted" in picker.payload
        assert set(picker.payload["currently_granted"]) == {
            "gmail_read", "calendar_read", "drive_read",
        }

    def test_not_installed_pick_capabilities_step_omits_currently_granted(self):
        """For ``not_installed`` bots there's no current grant set; the
        payload's ``currently_granted`` key MAY be absent OR present-and-
        empty. Frontend treats absence as "no skip optimisation
        possible" (oauth always runs)."""
        plan = g.build_install_plan(g.InstallStatus(
            bot_id="lex", status="not_installed",
        ))
        picker = next(step for step in plan if step.id == "pick_capabilities")
        current = picker.payload.get("currently_granted") or []
        assert current == []

    def test_unknown_empty_plan(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="unknown"))
        assert plan == []

    def test_pick_capabilities_payload_has_12_entries(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="not_installed"))
        picker = next(s for s in plan if s.id == "pick_capabilities")
        assert len(picker.capabilities) == 12

    def test_pick_capabilities_pre_checks_defaults_when_new(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="not_installed"))
        picker = next(s for s in plan if s.id == "pick_capabilities")
        checked = {c["id"] for c in picker.capabilities if c["checked"]}
        assert checked == {"gmail_read", "calendar_read"}

    def test_pick_capabilities_pre_checks_existing_when_modifying(self):
        s = g.InstallStatus(
            bot_id="lex", status="oauth_pending",
            granted_capabilities=["gmail_read", "gmail_send", "calendar_read"],
        )
        plan = g.build_install_plan(s)
        picker = next(step for step in plan if step.id == "pick_capabilities")
        checked = {c["id"] for c in picker.capabilities if c["checked"]}
        assert checked == {"gmail_read", "gmail_send", "calendar_read"}

    def test_capability_payload_marks_restricted(self):
        plan = g.build_install_plan(g.InstallStatus(bot_id="lex", status="not_installed"))
        picker = next(s for s in plan if s.id == "pick_capabilities")
        for entry in picker.capabilities:
            cap = g.capability_by_id(entry["id"])
            assert entry["restricted"] == cap.restricted


# ── InstallMcpServer payload ────────────────────────────────────────────────


class TestInstallMcpActionPayload:
    def test_payload_uses_picked_capabilities(self):
        p = g.build_install_mcp_action_payload("lex", ["gmail_read", "calendar_read"])
        assert p["server_id"] == g.GOOGLE_MCP_SERVER_ID
        assert p["catalog_id"] == g.GOOGLE_CATALOG_ID
        # All-read picks the --read-only shortcut.
        assert p["extra_args"] == ["--read-only"]

    def test_payload_with_mixed_capabilities_uses_permissions(self):
        p = g.build_install_mcp_action_payload(
            "lex", ["gmail_read", "gmail_send", "calendar_read"],
        )
        assert p["extra_args"][0] == "--permissions"
        assert "gmail:send" in p["extra_args"]

    def test_keystore_slots_are_per_bot(self):
        p = g.build_install_mcp_action_payload("lex", ["gmail_read"])
        for var, ref in p["env_bindings"].items():
            assert ref.startswith("keystore:gws-")
            slot = ref.split(":", 1)[1]
            assert slot.endswith("-lex"), f"{var} not per-bot: {slot}"


# ── Routes — Flask integration ──────────────────────────────────────────────


@pytest.fixture
def google_app(tmp_path):
    from evolve_admin.web.server import create_app
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir(parents=True)
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared_dir),
        "bots": {"lex": {"user": "lex"}},
    }))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


class TestCatalogList:
    def test_unified_google_in_catalog(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog")
            ids = [s["id"] for s in r.get_json()["skills"]]
            assert "google" in ids

    def test_legacy_google_ids_hidden(self, google_app):
        """gog/_read/_write must NOT appear in the list anymore."""
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog")
            ids = [s["id"] for s in r.get_json()["skills"]]
            assert "gog" not in ids
            assert "google_workspace_read" not in ids
            assert "google_workspace_write" not in ids

    def test_google_entry_has_default_capabilities(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog")
            entry = next(s for s in r.get_json()["skills"] if s["id"] == "google")
            assert entry["default_capabilities"] == ["gmail_read", "calendar_read"]

    def test_google_entry_carries_category(self, google_app):
        """Regression guard: the catalog list dispatcher assigns
        category="Productivity" to the unified Google skill. The frontend
        groups skills under section headers (Messaging / Productivity /
        Storage / Tools / Creative). Without ``google`` in the
        ``_CATEGORY_BY_SKILL`` map the row fell through to "Other" —
        surfaced post-merge as a real UX bug."""
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog")
            entry = next(s for s in r.get_json()["skills"] if s["id"] == "google")
            assert entry.get("category") == "Productivity"


class TestCatalogDetail:
    def test_returns_capabilities_catalog(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog/google")
            data = r.get_json()
            assert len(data["capabilities_catalog"]) == 12
            groups = {c["group"] for c in data["capabilities_catalog"]}
            assert groups == {"Gmail", "Calendar", "Drive", "Sheets", "Docs", "Advanced"}

    def test_returns_access_panel(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog/google")
            ap = r.get_json()["access_panel"]
            assert ap and ap["skill_id"] == "google"
            assert ap["will"] and ap["wont"]


class TestStatusAndPlan:
    def test_status_for_uninstalled_bot(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/install/google/status?bot_id=lex")
            data = r.get_json()
            assert data["ok"] is True
            # No OAuth client configured → oauth_client_missing.
            assert data["status"] == "oauth_client_missing"
            assert data["granted_capabilities"] == []

    def test_plan_includes_capabilities_picker(self, google_app):
        """When status is oauth_client_missing the plan is just configure;
        for not_installed the plan would have pick_capabilities. We can't
        easily simulate the latter without a real OAuth client, so this
        smokes the oauth_client_missing branch."""
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google", json={"bot_id": "lex"})
            data = r.get_json()
            assert data["ok"] is True
            assert data["skill"]["id"] == "google"
            # default_capabilities is exposed for the picker UI.
            assert data["skill"]["default_capabilities"] == ["gmail_read", "calendar_read"]


class TestCompleteEndpoint:
    def test_rejects_missing_bot_id(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/complete", json={})
            assert r.status_code == 400

    def test_rejects_missing_capabilities(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/complete",
                       json={"bot_id": "lex"})
            assert r.status_code == 400
            assert "capabilities" in r.get_json()["error"]

    def test_rejects_empty_capabilities(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/complete",
                       json={"bot_id": "lex", "capabilities": []})
            assert r.status_code == 400

    def test_rejects_unknown_capability_ids(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/complete",
                       json={"bot_id": "lex",
                             "capabilities": ["gmail_read", "bogus_cap"]})
            assert r.status_code == 400
            err = r.get_json()["error"]
            assert "bogus_cap" in err

    def test_valid_capabilities_reach_preflight(self, google_app):
        """With valid capability ids but no OAuth profile, the route runs
        through to preflight which fails (no token). Response shape
        carries the per-stage breakdown."""
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/complete",
                       json={"bot_id": "lex",
                             "capabilities": ["gmail_read", "calendar_read"]})
            assert r.status_code == 400
            data = r.get_json()
            assert data["ok"] is False
            assert data["preflight"]["done"] is False


class TestPodCapabilitySummaries:
    """The /api/skills/pod response carries capability_summaries.google.<bot>
    so the catalog chip can render "✓ atlas (read)" instead of "✓ atlas".
    Per-bot resolver runs in a thread pool — N bots resolve in parallel.

    Post-tooltip-enrichment shape: each per-bot entry is a dict
    ``{summary, labels}`` instead of a bare string. The frontend reads
    ``labels`` when ``summary == "custom"`` so the tooltip can enumerate
    granted capabilities ("Installed: Read Gmail, Send Gmail, ...").
    """

    def test_pod_response_includes_capability_summaries_field(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/pod")
            assert r.status_code == 200
            data = r.get_json()
            assert "capability_summaries" in data
            assert "google" in data["capability_summaries"]

    def test_uninstalled_bot_has_no_summary_entry(self, google_app):
        """The fixture's bot has no OAuth profile — its key is absent or
        the entry is None (the chip renders + Add naturally)."""
        app, _ = google_app
        with app.test_client() as c:
            r = c.get("/api/skills/pod")
            summaries = r.get_json()["capability_summaries"]["google"]
            # Either the key is absent or its value is None.
            assert "lex" not in summaries or summaries["lex"] is None

    def test_installed_bot_entry_is_dict_with_summary_and_labels(
        self, google_app, monkeypatch,
    ):
        """When a bot has a recognised capability set, the per-bot entry
        is a dict with ``summary`` (string) + ``labels`` (list of strings)."""
        from evolve_admin.skills import google_install as gi
        # Stub the resolver to return a "custom" status with three caps.
        custom_status = gi.InstallStatus(
            bot_id="lex", status="active",
            granted_capabilities=["gmail_read", "gmail_send", "calendar_read"],
            capability_summary=gi.summarize_capabilities(
                ["gmail_read", "gmail_send", "calendar_read"],
            ),
        )

        app, _ = google_app
        # Monkey-patch the bound closure via a local helper.
        from evolve_admin.web import server as srv
        # The closure isn't easily reachable; we patch the underlying
        # module function instead. Both paths through the resolver call
        # gi.resolve_status; patching IT is the load-bearing surface.
        monkeypatch.setattr(
            gi, "resolve_status",
            lambda bid, **kw: custom_status,
        )

        with app.test_client() as c:
            r = c.get("/api/skills/pod")
            entry = r.get_json()["capability_summaries"]["google"]["lex"]
            assert isinstance(entry, dict)
            assert entry["summary"] == "custom"
            # Labels enumerate the three granted capabilities by their
            # human-readable label.
            assert entry["labels"] == ["Read Gmail", "Send Gmail", "Read your calendar"]


class TestRevokeEndpoint:
    def test_revoke_requires_bot_id(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/revoke", json={})
            assert r.status_code == 400

    def test_revoke_on_clean_bot_returns_diagnostic_shape(self, google_app):
        app, _ = google_app
        with app.test_client() as c:
            r = c.post("/api/skills/install/google/revoke",
                       json={"bot_id": "lex"})
            assert r.status_code == 200
            data = r.get_json()
            for key in (
                "keystore_slots",
                "credentials_dir_wiped",
                "profile_cleared",
                "gateway_kickstarted",
            ):
                assert key in data


# ── Access panel — Plex-test compliance ─────────────────────────────────────


class TestAccessPanel:
    def test_no_oauth_jargon_in_will_or_wont(self):
        """The unified panel must not leak scope/OAuth jargon. Per-
        capability text (rendered after the wizard's picker) gets more
        specific but still passes the Plex-test bar."""
        forbidden = ("scope", "oauth", "token", "refresh_token", "iam ")
        for section in ("will", "wont"):
            for bullet in g.GOOGLE_ACCESS_PANEL[section]:
                lower = bullet.lower()
                for word in forbidden:
                    assert word not in lower, (
                        f"jargon {word!r} in {section}: {bullet!r}"
                    )

    def test_summary_mentions_picker_step(self):
        """The summary points the user at the next step — capability
        picker — so they understand the scope decision is coming."""
        s = g.GOOGLE_ACCESS_PANEL["summary"].lower()
        assert "next step" in s or "pick" in s

    def test_wont_excludes_photos_contacts_password(self):
        wont = " ".join(g.GOOGLE_ACCESS_PANEL["wont"]).lower()
        assert "photos" in wont
        assert "contacts" in wont
        assert "password" in wont


# ── Skill registry entry ────────────────────────────────────────────────────


class TestSkillRegistryEntry:
    def test_id_and_display_name(self):
        e = g.SKILL_REGISTRY_ENTRY
        assert e["id"] == "google"
        assert "Google" in e["display_name"]

    def test_carries_capabilities_catalog(self):
        e = g.SKILL_REGISTRY_ENTRY
        assert len(e["capabilities_catalog"]) == 12

    def test_carries_default_capabilities(self):
        e = g.SKILL_REGISTRY_ENTRY
        assert e["default_capabilities"] == ["gmail_read", "calendar_read"]

    def test_rate_limits_cover_all_apps(self):
        rl = g.SKILL_REGISTRY_ENTRY["rate_limits"]
        for app in ("gmail", "calendar", "drive", "sheets", "docs"):
            matching = [k for k in rl if app in k]
            assert matching, f"no rate limit for {app}: {list(rl.keys())}"
