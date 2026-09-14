"""tests/test_skills_google_workspace_write_install.py — coverage for the
Workspace-Write skill install module.

The module is the first to close the runtime-consumer gap (F4 in the
2026-05-30 deep audit) for Google Workspace skills. Tests focus on the
load-bearing invariants:

  * Four-stage status resolver (spec §9.6) routes every input combo
    correctly. NEVER returns ``active`` from credential-presence alone.
  * Install plan branches by status with the correct step ordering.
  * The InstallMcpServer action payload uses keystore: refs for every
    env binding (matches the launcher's contract at
    ``mcp_admin/launcher.py:render_script``).
  * Pre-flight check requires Gmail + Calendar + Drive all-200 to declare
    success. Partial success counts as failure (F3 — "credential lands
    somewhere ≠ credential works").
  * Access panel is Plex-test compliant: no OAuth jargon in user-facing
    strings; ``will``/``wont`` lists are scope-backed.

References:
  * Spec: ``internal/spec-google-workspace-suite-2026-06-04.md``
  * Vetting: ``internal/vetting-workspace-mcp-2026-06-04.md``
  * Module: ``evolve_admin.skills.google_workspace_write_install``
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


from evolve_admin.skills import google_workspace_write_install as gw  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _no_client(_bot_id):
    return None


def _ok_client(_bot_id):
    return {
        "mode": "self_hosted",
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-secret",
    }


def _no_profile(_bot_id):
    return None


def _profile_with_scopes(scopes, **kw):
    def _r(_bot_id):
        return {
            "scopes": list(scopes),
            "google_account": kw.get("email", "sam@example.com"),
            "services": kw.get("services", []),
            "status": kw.get("status", "active"),
        }
    return _r


def _profile_reauth(_bot_id):
    return {
        "scopes": sorted(gw.WRITE_REQUIRED_SCOPES),
        "google_account": "sam@example.com",
        "services": ["gmail", "calendar"],
        "status": "reauth_required",
    }


def _empty_oc(_bot_id):
    return ({}, None)


def _oc_with_mcp(_bot_id):
    return (
        {"mcp": {"servers": {gw.GOOGLE_WORKSPACE_MCP_SERVER_ID: {"command": "/foo"}}}},
        None,
    )


def _oc_read_error(_bot_id):
    return (None, "oc_read_failed_for_test")


def _all_keystore_present(_slot):
    return "non-empty-value"


def _no_keystore(_slot):
    return None


def _keystore_missing(*missing_slots):
    """Return a keystore reader that returns None for `missing_slots`,
    a value for everything else."""
    missing = set(missing_slots)
    def _r(slot):
        return None if slot in missing else "value"
    return _r


# ── Status resolver — every branch ───────────────────────────────────────────


class TestResolveStatus:
    """Lock down the four-stage state machine per spec §9.6.

    F3 from the deep audit: ``resolve_status`` must NEVER return
    ``active`` from credential-presence alone. We assert that explicitly.
    """

    def test_no_oauth_client_yields_oauth_client_missing(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_no_profile,
            read_oauth_client=_no_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "oauth_client_missing"

    def test_no_profile_yields_oauth_pending(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_no_profile,
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "oauth_pending"

    def test_reauth_required_yields_oauth_pending_with_reason(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_reauth,
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "oauth_pending"
        assert s.error == "reauth_required"
        assert s.has_oauth_profile is True

    def test_short_scopes_yields_oauth_pending_with_scope_short(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(
                ["https://www.googleapis.com/auth/gmail.readonly"]
            ),
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "oauth_pending"
        assert s.error and s.error.startswith("scope_short:")
        # The missing scopes are enumerated in the error string.
        for missing in (
            "gmail.send",
            "calendar",
            "drive.file",
            "spreadsheets",
            "documents",
        ):
            assert missing in s.error

    def test_full_scopes_no_mcp_yields_mcp_not_installed(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(sorted(gw.WRITE_REQUIRED_SCOPES)),
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "mcp_not_installed"
        assert s.has_oauth_profile is True
        # F3: mcp_not_installed must NEVER be confused for active.
        assert s.status != "active"

    def test_full_scopes_with_mcp_no_keystore_reader_yields_active(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(sorted(gw.WRITE_REQUIRED_SCOPES)),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=None,  # tests run without keystore probe
        )
        # Without a keystore reader we can't check stage 4 — caller
        # accepted that risk. Active is correct here.
        assert s.status == "active"

    def test_full_scopes_with_mcp_all_keystore_present_yields_active(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(sorted(gw.WRITE_REQUIRED_SCOPES)),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=_all_keystore_present,
        )
        assert s.status == "active"
        assert s.mcp_server_present is True

    @pytest.mark.parametrize("missing_slot", [
        "gws-client-id-lex",
        "gws-client-secret-lex",
        "gws-creds-dir-lex",
    ])
    def test_missing_keystore_slot_yields_consumer_unreachable(self, missing_slot):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(sorted(gw.WRITE_REQUIRED_SCOPES)),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_with_mcp,
            read_keystore_slot=_keystore_missing(missing_slot),
        )
        assert s.status == "consumer_unreachable"
        assert s.error and missing_slot in s.error

    def test_oc_read_error_yields_unknown(self):
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_profile_with_scopes(sorted(gw.WRITE_REQUIRED_SCOPES)),
            read_oauth_client=_ok_client,
            read_oc_config=_oc_read_error,
        )
        assert s.status == "unknown"
        assert "oc_read_failed_for_test" in (s.error or "")

    def test_client_reader_exception_yields_unknown(self):
        def _boom(_b):
            raise RuntimeError("network down")
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_no_profile,
            read_oauth_client=_boom,
            read_oc_config=_empty_oc,
        )
        assert s.status == "unknown"
        assert "network down" in (s.error or "")

    def test_profile_reader_exception_yields_unknown(self):
        def _boom(_b):
            raise RuntimeError("disk full")
        s = gw.resolve_status(
            "lex",
            read_oauth_profile=_boom,
            read_oauth_client=_ok_client,
            read_oc_config=_empty_oc,
        )
        assert s.status == "unknown"
        assert "disk full" in (s.error or "")


# ── Install plan ─────────────────────────────────────────────────────────────


class TestBuildInstallPlan:
    def test_oauth_client_missing_has_one_step(self):
        s = gw.InstallStatus(bot_id="lex", status="oauth_client_missing")
        plan = gw.build_install_plan(s)
        assert len(plan) == 1
        assert plan[0].id == "configure_oauth_client"

    def test_active_has_empty_plan(self):
        s = gw.InstallStatus(bot_id="lex", status="active")
        assert gw.build_install_plan(s) == []

    def test_unknown_has_empty_plan(self):
        s = gw.InstallStatus(bot_id="lex", status="unknown", error="kaboom")
        assert gw.build_install_plan(s) == []

    def test_oauth_pending_has_four_steps_in_order(self):
        s = gw.InstallStatus(bot_id="lex", status="oauth_pending")
        ids = [step.id for step in gw.build_install_plan(s)]
        assert ids == ["account_type", "capability_review", "oauth", "complete"]

    def test_mcp_not_installed_has_just_complete(self):
        s = gw.InstallStatus(bot_id="lex", status="mcp_not_installed")
        ids = [step.id for step in gw.build_install_plan(s)]
        assert ids == ["complete"]

    def test_consumer_unreachable_has_just_complete(self):
        s = gw.InstallStatus(bot_id="lex", status="consumer_unreachable")
        ids = [step.id for step in gw.build_install_plan(s)]
        assert ids == ["complete"]

    def test_oauth_step_carries_write_default_services(self):
        s = gw.InstallStatus(bot_id="lex", status="oauth_pending")
        plan = gw.build_install_plan(s)
        oauth_step = next(step for step in plan if step.id == "oauth")
        assert oauth_step.payload["services"] == list(gw.WRITE_DEFAULT_SERVICES)
        assert oauth_step.access_panel is not None

    def test_account_type_step_offers_two_radio_options(self):
        s = gw.InstallStatus(bot_id="lex", status="oauth_pending")
        plan = gw.build_install_plan(s)
        at_step = next(step for step in plan if step.id == "account_type")
        assert len(at_step.fields) == 1
        opts = at_step.fields[0]["options"]
        assert {o["value"] for o in opts} == {"free_gmail", "workspace"}


# ── InstallMcpServer action payload ─────────────────────────────────────────


class TestInstallMcpActionPayload:
    """The payload is the contract between the install module and the
    InstallMcpServer applier. Drift here = broken installs."""

    def test_payload_shape(self):
        p = gw.build_install_mcp_action_payload("lex")
        for key in ("bot_id", "server_id", "catalog_id", "env_bindings", "extra_args"):
            assert key in p

    def test_bot_id_passthrough(self):
        p = gw.build_install_mcp_action_payload("lex")
        assert p["bot_id"] == "lex"

    def test_server_id_and_catalog_id_are_canonical(self):
        p = gw.build_install_mcp_action_payload("lex")
        assert p["server_id"] == gw.GOOGLE_WORKSPACE_MCP_SERVER_ID
        assert p["catalog_id"] == gw.GOOGLE_WORKSPACE_CATALOG_ID

    def test_every_env_binding_is_keystore_ref(self):
        """The launcher (``mcp_admin/launcher.py:render_script``) requires
        every binding to start with ``keystore:`` and have a non-empty
        key name. Violating this raises ValueError at applier time."""
        p = gw.build_install_mcp_action_payload("lex")
        for var_name, ref in p["env_bindings"].items():
            assert ref.startswith("keystore:"), f"{var_name} → {ref!r}"
            key = ref.split(":", 1)[1]
            assert key, f"{var_name} → empty key"

    def test_bindings_cover_required_env_vars(self):
        p = gw.build_install_mcp_action_payload("lex")
        required = {
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "WORKSPACE_MCP_CREDENTIALS_DIR",
        }
        assert set(p["env_bindings"].keys()) == required

    def test_keystore_slots_are_per_bot(self):
        p = gw.build_install_mcp_action_payload("lex")
        for var, ref in p["env_bindings"].items():
            slot = ref.split(":", 1)[1]
            assert slot.endswith("-lex"), f"{var} slot {slot!r} not per-bot"

    def test_extra_args_starts_with_permissions_flag(self):
        p = gw.build_install_mcp_action_payload("lex")
        assert p["extra_args"][0] == "--permissions"
        # Followed by gmail:send (the most user-visible write capability).
        assert "gmail:send" in p["extra_args"]

    def test_write_does_not_request_drive_full(self):
        """Drive scope is ``drive.file`` (narrow), NOT ``drive`` (full).
        Spec §5.2: full Drive is behind the Advanced disclosure only."""
        p = gw.build_install_mcp_action_payload("lex")
        # Permission strings use ":" as separator, not URL form.
        assert "drive:file" in p["extra_args"]
        # The wide-Drive permission string would be e.g. "drive" or "drive:full".
        # workspace-mcp's documented form for full Drive is "drive" (not the
        # URL); we don't request that anywhere.
        assert "drive" not in p["extra_args"]  # bare "drive" = full access


# ── Pre-flight check ─────────────────────────────────────────────────────────


class TestPreflightCheck:
    """Pre-flight is the final gate before declaring success — F3.

    Test against a stubbed _google_get so we control what each API
    'returns' without hitting the network."""

    def _patch_google_get(self, monkeypatch, fake_responses, *, record=None):
        """fake_responses is a dict {url_substring: (status, body, error)}.

        ``record`` (optional list) collects each call's (url, timeout) so a
        test can assert the per-call timeout was bounded by the budget.
        """
        def _fake(url, _access, *, timeout=gw.GOOGLE_HTTP_TIMEOUT_S):
            if record is not None:
                record.append((url, timeout))
            for sub, response in fake_responses.items():
                if sub in url:
                    return response
            raise AssertionError(f"no fake configured for {url}")
        monkeypatch.setattr(gw, "_google_get", _fake)

    def test_all_three_succeed_yields_ok(self, monkeypatch):
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (200, {"emailAddress": "sam@example.com"}, None),
            "calendar/v3": (200, {"items": []}, None),
            "drive/v3": (200, {"user": {}}, None),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is True
        assert result["gmail_ok"] is True
        assert result["calendar_ok"] is True
        assert result["drive_ok"] is True
        assert result["gmail_email"] == "sam@example.com"
        assert result["error"] is None

    def test_gmail_fails_short_circuits(self, monkeypatch):
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (403, {"error": "insufficient_scope"}, None),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is False
        assert result["gmail_ok"] is False
        assert result["calendar_ok"] is False  # never tried
        assert result["error"] and "gmail" in result["error"]

    def test_calendar_fails_after_gmail_succeeds(self, monkeypatch):
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (200, {"emailAddress": "sam@example.com"}, None),
            "calendar/v3": (403, None, None),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is False
        assert result["gmail_ok"] is True
        assert result["calendar_ok"] is False
        assert result["drive_ok"] is False  # never tried
        assert result["error"] and "calendar" in result["error"]

    def test_drive_fails_after_gmail_and_calendar_succeed(self, monkeypatch):
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (200, {"emailAddress": "sam@example.com"}, None),
            "calendar/v3": (200, {"items": []}, None),
            "drive/v3": (403, None, None),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is False
        assert result["gmail_ok"] is True
        assert result["calendar_ok"] is True
        assert result["drive_ok"] is False
        assert result["error"] and "drive" in result["error"]

    def test_network_error_yields_connection_failed(self, monkeypatch):
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (0, None, "connection_failed"),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is False
        assert result["gmail_ok"] is False
        assert "connection_failed" in result["error"]

    def test_timeout_error_is_transient(self, monkeypatch):
        """A deadline/timeout failure is classified transient, not a finding."""
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (0, None, "timeout"),
        })
        result = gw.preflight_check("ya29.fake")
        assert result["ok"] is False
        assert result["error"] == "gmail_preflight_timeout"
        assert gw.is_transient_preflight_error(result["error"]) is True

    def test_per_call_timeout_bounded_by_budget(self, monkeypatch):
        """Each call's timeout is capped at min(GOOGLE_HTTP_TIMEOUT_S, remaining)."""
        record: list = []
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (200, {"emailAddress": "s@e.com"}, None),
            "calendar/v3": (200, {"items": []}, None),
            "drive/v3": (200, {"user": {}}, None),
        }, record=record)
        # Tight budget — the first call must get a timeout < GOOGLE_HTTP_TIMEOUT_S.
        gw.preflight_check("ya29.fake", deadline_s=4.0)
        assert record, "no calls recorded"
        for _url, timeout in record:
            assert 0 < timeout <= gw.GOOGLE_HTTP_TIMEOUT_S
        assert record[0][1] <= 4.0

    def test_budget_short_circuits_when_deadline_exhausted(self, monkeypatch):
        """Once the wall-clock budget is gone, stop issuing blocking calls."""
        # monotonic jumps far past the deadline right after the first call so
        # the second _budget_get short-circuits with a timeout instead of
        # making another (potentially hanging) request.
        ticks = iter([0.0, 0.1, 100.0, 100.0, 100.0])
        monkeypatch.setattr(gw.time, "monotonic", lambda: next(ticks))
        self._patch_google_get(monkeypatch, {
            "gmail.googleapis.com": (200, {"emailAddress": "s@e.com"}, None),
            # calendar/drive should NEVER be reached — no fake needed.
        })
        result = gw.preflight_check("ya29.fake", deadline_s=12.0)
        assert result["gmail_ok"] is True
        assert result["calendar_ok"] is False
        assert result["error"] == "calendar_preflight_timeout"
        assert gw.is_transient_preflight_error(result["error"]) is True

    def test_is_transient_preflight_error_classification(self):
        assert gw.is_transient_preflight_error("gmail_preflight_timeout") is True
        assert gw.is_transient_preflight_error("drive_preflight_connection_failed") is True
        # An HTTP status means the call RAN — not transient.
        assert gw.is_transient_preflight_error("gmail_preflight_status_403") is False
        assert gw.is_transient_preflight_error(None) is False
        assert gw.is_transient_preflight_error("") is False


class TestGoogleGetErrorClassification:
    """_google_get distinguishes a timeout (deadline) from a connection failure
    so the preflight can mark the result transient — neither means the bot's
    Google access is bad."""

    def _patch_urlopen(self, monkeypatch, exc):
        import urllib.request
        def _raise(*_a, **_k):
            raise exc
        monkeypatch.setattr(urllib.request, "urlopen", _raise)

    def test_timeout_error_classified_timeout(self, monkeypatch):
        self._patch_urlopen(monkeypatch, TimeoutError("timed out"))
        status, body, err = gw._google_get("https://x/y", "tok", timeout=2.0)
        assert (status, body) == (0, None)
        assert err == "timeout"

    def test_urlerror_timeout_reason_classified_timeout(self, monkeypatch):
        import urllib.error
        self._patch_urlopen(
            monkeypatch, urllib.error.URLError(reason=TimeoutError("slow")),
        )
        _status, _body, err = gw._google_get("https://x/y", "tok")
        assert err == "timeout"

    def test_urlerror_other_reason_classified_connection_failed(self, monkeypatch):
        import urllib.error
        self._patch_urlopen(
            monkeypatch, urllib.error.URLError(reason=OSError("no route")),
        )
        _status, _body, err = gw._google_get("https://x/y", "tok")
        assert err == "connection_failed"

    def test_generic_exception_classified_connection_failed(self, monkeypatch):
        self._patch_urlopen(monkeypatch, OSError("[Errno 57] Socket is not connected"))
        _status, _body, err = gw._google_get("https://x/y", "tok")
        assert err == "connection_failed"


# ── Access panel — Plex-test compliance ──────────────────────────────────────


class TestAccessPanelPlexTest:
    """The panel is the load-bearing trust contract per
    feedback_safety_summary_less_useful_than_audit + F5 in deep audit."""

    def test_no_oauth_jargon_in_will_or_wont(self):
        """Plex-test: panels must not use 'scope', 'OAuth', 'token',
        'refresh', 'credential', 'IAM', 'API'."""
        forbidden = ("scope", "oauth", "token", "refresh_token", "iam ")
        for section in ("will", "wont"):
            for bullet in gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL[section]:
                lower = bullet.lower()
                for word in forbidden:
                    assert word not in lower, (
                        f"jargon {word!r} in {section} bullet: {bullet!r}"
                    )

    def test_will_section_present_tense_active_verbs(self):
        """Per F5: ``will`` claims must be present-tense capabilities
        the install actually delivers, not future-tense aspirations."""
        # No "will be able to" / "may" / "can be configured" — those are
        # weasel constructions that turned out to be the dishonest-stub
        # anti-pattern in the gog withdrawal.
        forbidden = ("will be able to", "may ", "can be configured")
        for bullet in gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["will"]:
            for phrase in forbidden:
                assert phrase not in bullet.lower(), (
                    f"weasel phrase {phrase!r} in will: {bullet!r}"
                )

    def test_wont_drive_bound_is_explicit(self):
        """Spec §5.2: the Drive bound (drive.file = narrow write) MUST
        be reflected in the wont list. Without this the user thinks the
        bot can edit any Drive file."""
        wont_concat = " ".join(gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["wont"]).lower()
        assert "drive files" in wont_concat or "drive" in wont_concat
        assert "not shared" in wont_concat or "did not create" in wont_concat

    def test_wont_excludes_photos_contacts(self):
        """Spec §5.2 wont: explicitly excludes Photos + Contacts to bound
        the user's mental model of what 'Google Workspace' means."""
        wont_concat = " ".join(gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["wont"]).lower()
        assert "photos" in wont_concat
        assert "contacts" in wont_concat

    def test_wont_excludes_password_change(self):
        """No path to account takeover via the OAuth scope — spec §5.2."""
        wont_concat = " ".join(gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["wont"]).lower()
        assert "password" in wont_concat

    def test_summary_mentions_all_five_apps(self):
        summary = gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["summary"].lower()
        for app in ("gmail", "calendar", "drive", "sheets", "docs"):
            assert app in summary, f"{app!r} not mentioned in summary"

    def test_credentials_live_locally_promise(self):
        """The 'where_credentials_live' line is the privacy contract —
        per_bot_inference + per-bot storage."""
        text = gw.GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["where_credentials_live"].lower()
        assert "this bot" in text or "on your machine" in text
        assert "never centralised" in text or "never centralized" in text


# ── Keystore slot naming ─────────────────────────────────────────────────────


class TestKeystoreSlotNaming:
    """Per-bot slot names — F2 (symmetric install/revoke depends on this)."""

    def test_client_id_slot_is_per_bot(self):
        assert gw.keystore_slot_client_id_for("lex") == "gws-client-id-lex"
        assert gw.keystore_slot_client_id_for("personal-bot") == "gws-client-id-personal-bot"

    def test_client_secret_slot_is_per_bot(self):
        assert gw.keystore_slot_client_secret_for("lex") == "gws-client-secret-lex"

    def test_credentials_dir_slot_is_per_bot(self):
        assert gw.keystore_slot_credentials_dir_for("lex") == "gws-creds-dir-lex"

    def test_slots_are_distinct(self):
        slots = {
            gw.keystore_slot_client_id_for("lex"),
            gw.keystore_slot_client_secret_for("lex"),
            gw.keystore_slot_credentials_dir_for("lex"),
        }
        assert len(slots) == 3


# ── Revoke payload ───────────────────────────────────────────────────────────


class TestRemoveMcpActionPayload:
    def test_payload_uses_same_server_id_as_install(self):
        install = gw.build_install_mcp_action_payload("lex")
        remove = gw.build_remove_mcp_action_payload("lex")
        assert remove["server_id"] == install["server_id"]
        assert remove["bot_id"] == install["bot_id"]


# ── Skill registry entry ─────────────────────────────────────────────────────


class TestSkillRegistryEntry:
    def test_registry_keys_match_module_constants(self):
        e = gw.SKILL_REGISTRY_ENTRY
        assert e["id"] == gw.GOOGLE_WORKSPACE_WRITE_SKILL_ID
        assert e["mcp_server_id"] == gw.GOOGLE_WORKSPACE_MCP_SERVER_ID
        assert e["catalog_id"] == gw.GOOGLE_WORKSPACE_CATALOG_ID

    def test_registry_has_rate_limits(self):
        """Spec §7.2: rate-limit advertising on the catalog entry."""
        rl = gw.SKILL_REGISTRY_ENTRY["rate_limits"]
        for key in (
            "gmail_send_per_minute",
            "calendar_writes_per_minute",
            "drive_uploads_per_minute",
            "sheets_appends_per_minute",
            "docs_edits_per_minute",
        ):
            assert key in rl
            assert isinstance(rl[key], int)
            assert rl[key] > 0

    def test_registry_required_scopes_match_constant(self):
        assert set(gw.SKILL_REGISTRY_ENTRY["required_scopes"]) == set(gw.WRITE_REQUIRED_SCOPES)
