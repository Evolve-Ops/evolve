"""tests/test_safety_summary.py — B2.a plain-language safety summary.

Covers both the pure ``safety_summary`` module and the wiring of
``/api/security/bot-summary/<bot>`` and ``/api/security/safety-summary``
in the Flask app.

Voice-rule tests (no jargon in the user-facing strings) are bundled
in here too — they're cheap to maintain and they're load-bearing for
the Plex test.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (dict, list)):
        path.write_text(json.dumps(obj))
    else:
        path.write_text(str(obj))


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    home = tmp_path / "team_bot_a"
    (home / ".openclaw").mkdir(parents=True)
    return home


# ─────────────────────────────────────────────────────────────────────
# build_summary — bare bot (no config)
# ─────────────────────────────────────────────────────────────────────

def test_build_summary_with_no_config_still_returns_universal_protections(bot_home: Path):
    """Even if openclaw.json is missing, the cannot-do list must
    surface the verifiable architectural guarantees (per-bot macOS user
    boundary + exec-approvals gating). Claims removed in 2026-05-13
    redesign (cost cap, banking, channel tautology) must NOT appear."""
    from evolve_admin import safety_summary as ss

    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    assert s.bot_id == "team_bot_a"
    # read_errors should mention the missing config
    assert any("main config" in e for e in s.read_errors)
    # Verifiable universal cannot-do claims still appear
    joined = " | ".join(s.cannot_do)
    assert "other bots' folders" in joined
    assert "without your approval" in joined
    # Removed claims must NOT appear (they were lies)
    assert "banking" not in joined.lower()
    assert "financial" not in joined.lower()
    assert "Spend" not in joined
    assert "channel it isn't connected" not in joined
    # All quiet headline when signals=[]
    assert s.this_week_headline == "All quiet"


# ─────────────────────────────────────────────────────────────────────
# build_summary — happy path with realistic config
# ─────────────────────────────────────────────────────────────────────

def test_build_summary_renders_realistic_config(bot_home: Path):
    from evolve_admin import safety_summary as ss

    _write(bot_home / ".openclaw" / "openclaw.json", {
        "tools": {
            "fs":  {"workspaceOnly": True},
            "web": {"search": {"enabled": True}, "fetch": {"enabled": False}},
        },
        "channels": {
            "slack":    {"botToken": "xoxb-…", "channel": "#ops", "enabled": True},
            "telegram": {"botToken": "…",      "enabled": False},
        },
        "plugins": {
            "entries": {
                "brave":  {"enabled": True},
                "github": {"enabled": True},
                "evolve": {"enabled": True},  # should be filtered out
            },
        },
    })
    _write(bot_home / ".openclaw" / "exec-approvals.json", {
        "defaults": {"git status": {}, "git log": {}},
        "agents": {"main": {"approvals": {"gh pr view <arg>": {}, "ls <path>": {}}}},
    })

    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[], cost_cap_usd=12.50)

    # Workspace-only translates plainly
    assert any("project folder" in p for p in s.can_do)
    # Web search yes, fetch no
    assert any("Search the web" in p for p in s.can_do)
    assert not any("Open pages" in p for p in s.can_do)
    # Slack enabled with target channel surfaces
    assert any("Slack" in p and "#ops" in p for p in s.can_do)
    # Disabled telegram does NOT appear
    assert not any("Telegram" in p for p in s.can_do)
    # Brave + GitHub plugins translate to recognisable capabilities;
    # the "evolve" admin plugin is filtered out.
    can_str = " | ".join(s.can_do).lower()
    assert "vendor websites" in can_str or "brave" not in can_str  # brave shouldn't appear by its name
    assert "github" in can_str
    assert "evolve" not in can_str  # the admin plugin should never leak as a user capability
    # exec-approvals collapses to a count
    assert any("approved" in p.lower() or "system command" in p.lower() for p in s.can_do)
    # cost_cap_usd is accepted but no longer generates a cannot_do claim
    # (budget_hawk only vetoes Arbiter proposals, not gateway billing)
    assert not any("$12.50" in p for p in s.cannot_do)
    assert not any("Spend" in p for p in s.cannot_do)
    assert not any("banking" in p.lower() for p in s.cannot_do)
    # Unknown plugins must NOT generate "Use the '<skill>' skill" entries
    assert not any(p.startswith('Use the "') for p in s.can_do)


# ─────────────────────────────────────────────────────────────────────
# build_summary — workspaceOnly defaults
# ─────────────────────────────────────────────────────────────────────

def test_workspace_only_absent_treated_as_unrestricted(bot_home: Path):
    """When tools.fs.workspaceOnly is absent the OpenClaw default is
    *unrestricted* (schema says `default: false`). The card MUST NOT
    claim the bot is workspace-bounded — that would be a false
    safety claim. The cross-bot boundary is still surfaced via the
    universal cannot-do ("Read your other bots' folders")."""
    from evolve_admin import safety_summary as ss

    _write(bot_home / ".openclaw" / "openclaw.json", {})  # bare openclaw.json
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    # Honest can-do: "in its own user account" (wider than project folder)
    assert any("user account" in p for p in s.can_do), (
        f"Expected wider 'user account' phrasing, got: {s.can_do}"
    )
    # Must NOT claim workspace-only constraint that doesn't exist
    assert not any("project folder" in p for p in s.can_do), (
        f"Must not claim workspace-restricted when default is unrestricted; got: {s.can_do}"
    )
    assert not any("outside its project folder" in p for p in s.cannot_do), (
        f"Must not claim 'cannot read outside project folder' when OC default is unrestricted; got: {s.cannot_do}"
    )
    # Cross-bot boundary still claimed (universal cannot-do)
    assert any("other bots'" in p for p in s.cannot_do)


def test_workspace_only_explicit_false_treated_as_unrestricted(bot_home: Path):
    """Explicit ``workspaceOnly: false`` renders identically to absent —
    both are 'unrestricted' per the OpenClaw schema (and per
    permissions/posture._axis_filesystem)."""
    from evolve_admin import safety_summary as ss

    _write(bot_home / ".openclaw" / "openclaw.json", {
        "tools": {"fs": {"workspaceOnly": False}},
    })
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    assert any("user account" in p for p in s.can_do)
    assert not any("outside its project folder" in p for p in s.cannot_do)


def test_workspace_only_explicit_true_treated_as_restricted(bot_home: Path):
    """Only an explicit ``workspaceOnly: true`` (team_bot_c's posture) renders
    as workspace-restricted. This is the load-bearing distinction from
    the inverted-default bug fixed in the B2.a follow-up."""
    from evolve_admin import safety_summary as ss

    _write(bot_home / ".openclaw" / "openclaw.json", {
        "tools": {"fs": {"workspaceOnly": True}},
    })
    s = ss.build_summary("team_bot_c", bot_home=bot_home, signals=[])
    assert any("project folder" in p for p in s.can_do)
    assert any("outside its project folder" in p for p in s.cannot_do)
    # Wider "user account" phrasing must NOT appear when restricted
    assert not any("user account" in p for p in s.can_do)


# ─────────────────────────────────────────────────────────────────────
# Signals → this week
# ─────────────────────────────────────────────────────────────────────

def test_signals_all_quiet_when_empty(bot_home: Path):
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    assert s.this_week_headline == "All quiet"
    assert s.this_week_items == []


def test_signals_translate_to_plain_language(bot_home: Path):
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sigs = [
        {
            "type": "credential_exposure",
            "severity": "alert",
            "producer": "security_warden",
            "title": "raw title — should not appear",
            "last_observed_at": now,
            "bot_id": "team_bot_a",
        },
    ]
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=sigs)
    assert s.this_week_headline.startswith("Notable")
    assert len(s.this_week_items) == 1
    assert "credentials" in s.this_week_items[0].headline.lower()
    # The raw signal title with em-dash is not what's rendered — the
    # plain-language headline replaces it.
    assert "raw title" not in s.this_week_items[0].headline


def test_signals_none_yields_couldnt_check_headline(bot_home: Path):
    """signals=None means we couldn't read the store — surfaces as
    'couldn't check' rather than misleadingly 'all quiet'."""
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=None)
    assert "couldn't check" in s.this_week_headline.lower() or \
           "couldn't check" in " | ".join(s.read_errors).lower()


# ─────────────────────────────────────────────────────────────────────
# signals_for_bot — store reader
# ─────────────────────────────────────────────────────────────────────

def test_signals_for_bot_returns_none_when_store_missing(tmp_path: Path):
    from evolve_admin import safety_summary as ss
    # No /signals dir created — store-not-yet-populated path
    assert ss.signals_for_bot(tmp_path, "team_bot_a") is None


def test_signals_for_bot_filters_by_bot_producer_age(tmp_path: Path):
    from evolve_admin import safety_summary as ss

    shared = tmp_path
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)

    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale  = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Phase B: records load through signals.store, so include the fields
    # Signal.from_dict requires (signature/flavor/scope) on each fixture.
    # Wanted: team_bot_a, security_warden, recent
    _write(shared / "signals" / "firing" / "a.json", {
        "id": "a", "type": "credential_exposure",
        "signature": "security_warden:credential_exposure:team_bot_a",
        "flavor": "security", "scope": "bot",
        "producer": "security_warden", "severity": "alert",
        "bot_id": "team_bot_a", "last_observed_at": recent,
    })
    # Skip: wrong bot
    _write(shared / "signals" / "firing" / "b.json", {
        "id": "b", "type": "credential_exposure",
        "signature": "security_warden:credential_exposure:admin_bot",
        "flavor": "security", "scope": "bot",
        "producer": "security_warden", "severity": "alert",
        "bot_id": "admin_bot", "last_observed_at": recent,
    })
    # Skip: wrong producer (cost monitor)
    _write(shared / "signals" / "firing" / "c.json", {
        "id": "c", "type": "cost_spike",
        "signature": "budget_hawk:cost_spike:team_bot_a",
        "flavor": "maintenance", "scope": "bot",
        "producer": "budget_hawk", "severity": "warn",
        "bot_id": "team_bot_a", "last_observed_at": recent,
    })
    # Skip: too old (archived 30 days ago)
    _write(shared / "signals" / "archived" / "d.json", {
        "id": "d", "type": "credential_exposure", "state": "resolved",
        "signature": "security_warden:credential_exposure:team_bot_a_old",
        "flavor": "security", "scope": "bot",
        "producer": "security_warden", "severity": "alert",
        "bot_id": "team_bot_a", "last_observed_at": stale,
    })

    out = ss.signals_for_bot(shared, "team_bot_a", days=7)
    assert out is not None
    assert [s["id"] for s in out] == ["a"]


# ─────────────────────────────────────────────────────────────────────
# Voice / Plex-test guards
# ─────────────────────────────────────────────────────────────────────

JARGON = (
    "exec-approvals", "exec_approvals", "ACL", "workspaceOnly",
    "manifest fingerprint", "eBPF", "tools.fs", "openclaw.json",
)


def test_universal_strings_have_no_jargon():
    from evolve_admin.safety_summary import _UNIVERSAL_CANNOT_DO, _PLUGIN_CAPABILITY, _FRIENDLY_HEADLINE
    for s in _UNIVERSAL_CANNOT_DO:
        for token in JARGON:
            assert token not in s, f"jargon {token!r} in universal-cannot-do: {s}"
    for cap in _PLUGIN_CAPABILITY.values():
        for token in JARGON:
            assert token not in cap, f"jargon {token!r} in plugin capability: {cap}"
    for headline in _FRIENDLY_HEADLINE.values():
        for token in JARGON:
            assert token not in headline, f"jargon {token!r} in signal headline: {headline}"


def test_built_summary_has_no_jargon_in_user_facing_strings(bot_home: Path):
    """Render a realistic summary, scan every user-facing string."""
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {
        "tools": {"fs": {"workspaceOnly": True},
                  "web": {"search": {"enabled": True}}},
        "channels": {"slack": {"botToken": "x", "channel": "#ops"}},
        "plugins": {"entries": {"brave": {"enabled": True}}},
    })
    _write(bot_home / ".openclaw" / "exec-approvals.json", {
        "agents": {"main": {"approvals": {"gh pr view <arg>": {}}}},
    })
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    user_facing = list(s.can_do) + list(s.cannot_do) + [s.this_week_headline]
    for phrase in user_facing:
        for token in JARGON:
            assert token not in phrase, f"jargon {token!r} surfaced in: {phrase}"


# ─────────────────────────────────────────────────────────────────────
# Flask endpoint wiring
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path: Path):
    """Spin up a Flask app with a 2-bot pod."""
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    (shared_dir / "signals" / "firing").mkdir(parents=True)

    # Synthesize bot homes. The server uses pwd.getpwnam, which won't
    # have entries for these test bots — bot_home falls back to
    # /Users/<id>, which won't exist either. We can't write under
    # /Users/, so we create config files under tmp_path matching the
    # /Users/<bot_id> shape using "user" overrides in network.json…
    # but pwd.getpwnam still won't know about them. The safety_summary
    # module handles missing files cleanly (read_errors populated,
    # universal-cannot-do still present), which is the contract we
    # want to test here.
    bots = {
        "team_bot_a":   {"port": 6101, "user": "team_bot_a"},
        "admin_bot": {"port": 6102, "user": "admin_bot"},
    }
    network = {"members": [], "bots": bots, "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def test_endpoint_returns_summary_for_known_bot(app):
    client = app.test_client()
    resp = client.get("/api/security/bot-summary/team_bot_a")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["bot_id"] == "team_bot_a"
    # Universal protections appear even without config files
    assert any("other bots'" in p for p in payload["cannot_do"])
    # Headline must be one of the documented strings
    assert payload["this_week_headline"] in {
        "All quiet",
        "Couldn't check — see Advanced view",
    }


def test_endpoint_404_for_unknown_bot(app):
    client = app.test_client()
    resp = client.get("/api/security/bot-summary/no-such-bot")
    assert resp.status_code == 404


def test_endpoint_lists_all_bots(app):
    client = app.test_client()
    resp = client.get("/api/security/safety-summary")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload["bots"].keys()) == {"team_bot_a", "admin_bot"}
    for bid, summary in payload["bots"].items():
        assert summary["bot_id"] == bid
        assert isinstance(summary.get("can_do"), list)
        assert isinstance(summary.get("cannot_do"), list)


def test_safety_summary_endpoint_includes_chip_fields(app):
    """The /api/security/safety-summary response must include the three
    fields consumed by the V1.5-3 audit-tab chips:

      * upstream_version      — str | None  (OC version chip)
      * exec_policy_compliant — bool | None (exec-policy chip)
      * exec_policy_reason    — str | None  (exec-policy tooltip)

    The bot home dirs don't exist in the test fixture, so all three will
    be None — but the keys must be present in the dict so the JS can
    distinguish "missing config" from "non-compliant".
    """
    client = app.test_client()
    resp = client.get("/api/security/safety-summary")
    assert resp.status_code == 200
    payload = resp.get_json()
    for bid, summary in payload["bots"].items():
        assert "upstream_version" in summary, (
            f"bot {bid}: upstream_version key missing from safety-summary response"
        )
        assert "exec_policy_compliant" in summary, (
            f"bot {bid}: exec_policy_compliant key missing from safety-summary response"
        )
        assert "exec_policy_reason" in summary, (
            f"bot {bid}: exec_policy_reason key missing from safety-summary response"
        )
        # Type contracts: None is valid (couldn't determine), but never a wrong type
        assert summary["upstream_version"] is None or isinstance(summary["upstream_version"], str), (
            f"bot {bid}: upstream_version must be str or None"
        )
        assert summary["exec_policy_compliant"] is None or isinstance(summary["exec_policy_compliant"], bool), (
            f"bot {bid}: exec_policy_compliant must be bool or None"
        )
        assert summary["exec_policy_reason"] is None or isinstance(summary["exec_policy_reason"], str), (
            f"bot {bid}: exec_policy_reason must be str or None"
        )


def test_bot_summary_endpoint_includes_chip_fields(app):
    """The per-bot /api/security/bot-summary/<bot> endpoint must also
    include the chip fields. The audit JS fetches safety-summary (all
    bots) but having the fields on the per-bot endpoint keeps the contract
    symmetric and makes unit testing each bot easier.
    """
    client = app.test_client()
    resp = client.get("/api/security/bot-summary/team_bot_a")
    assert resp.status_code == 200
    payload = resp.get_json()
    for key in ("upstream_version", "exec_policy_compliant", "exec_policy_reason"):
        assert key in payload, (
            f"bot-summary/team_bot_a: '{key}' key missing — JS chips will silently show nothing"
        )


def test_security_page_html_has_chip_rendering_logic():
    """The Security page HTML must contain the _secChipsFor function and
    chip-relevant JS identifiers introduced in V1.5-3 (PR #1046).

    This is a static guard: if index.html is edited and the chip renderer
    is accidentally removed, this test fails before the regression reaches
    the mini.
    """
    # The V1.5-3 chip helpers (_secChipsFor + _SEC_VER_FLOOR + exec_policy
    # chip) moved to pages/pod-config.js (Phase 3x). Concat so the chip
    # invariant assertions stay valid.
    html_path = _ADMIN_DIR / "evolve_admin" / "web" / "index.html"
    pod_config_path = _ADMIN_DIR / "evolve_admin" / "web" / "static" / "js" / "pages" / "pod-config.js"
    html = html_path.read_text() + "\n" + pod_config_path.read_text()

    assert "_secChipsFor" in html, (
        "_secChipsFor function missing from index.html — V1.5-3 chips regressed"
    )
    assert "_SEC_VER_FLOOR" in html, (
        "_SEC_VER_FLOOR constant missing — OC version floor check regressed"
    )
    assert "exec_policy_compliant" in html, (
        "exec_policy_compliant reference missing — exec-policy chip regressed"
    )
    assert "exec-policy: scoped" in html, (
        "'exec-policy: scoped' label missing from index.html"
    )
    assert "exec-policy: permissive" in html, (
        "'exec-policy: permissive' label missing from index.html"
    )
    # The chip strip must be injected inside _botCard via _secChipsFor call
    assert "_secChipsFor(bot)" in html, (
        "_secChipsFor(bot) call not found inside _botCard — chips won't render"
    )
    # The parallel fetch of safety-summary must exist inside loadSecurityAudit
    assert "/api/security/safety-summary" in html, (
        "safety-summary fetch missing from index.html — chip data source gone"
    )


# ─────────────────────────────────────────────────────────────────────
# Cost cap fallback
# ─────────────────────────────────────────────────────────────────────

def test_no_cost_cap_no_spend_claim(bot_home: Path):
    """cost_cap_usd=None (and any value) no longer generates a cannot_do entry.
    The cost-cap claim was removed in 2026-05-13 because budget_hawk only
    vetoes new Arbiter proposals; it does not gate gateway billing."""
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[], cost_cap_usd=None)
    joined = " | ".join(s.cannot_do)
    assert "Spend" not in joined
    assert "cost cap" not in joined
    assert "$" not in joined


# ─────────────────────────────────────────────────────────────────────
# 2026-05-13 redesign: guard tests for removed lies + removed skill bullets
# ─────────────────────────────────────────────────────────────────────

def test_cannot_do_never_contains_removed_claims(bot_home: Path):
    """cannot_do must NOT contain the three claims removed in the
    2026-05-13 redesign. Each was removed because it was unverifiable
    or outright false:
      - "Spend …" — budget_hawk vetoes proposals, not gateway billing
      - "banking / financial" — no scan, just a developer assertion
      - "channel it isn't connected to" — tautological
    """
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[], cost_cap_usd=5.00)
    joined = " | ".join(s.cannot_do).lower()
    assert "spend" not in joined, f"Spend claim must not appear; got: {s.cannot_do}"
    assert "banking" not in joined, f"Banking claim must not appear; got: {s.cannot_do}"
    assert "financial" not in joined, f"Financial claim must not appear; got: {s.cannot_do}"
    assert "channel it isn't connected" not in joined, (
        f"Channel tautology must not appear; got: {s.cannot_do}"
    )


def test_can_do_never_contains_use_the_skill_prefix(bot_home: Path):
    """Unknown plugins (those not in _PLUGIN_CAPABILITY) must NOT
    generate 'Use the \"<skill>\" skill' entries in can_do.
    Those bullets belong in Integrations, not Security.
    """
    from evolve_admin import safety_summary as ss
    _write(bot_home / ".openclaw" / "openclaw.json", {
        "plugins": {
            "entries": {
                "slack":       {"enabled": True},   # known → has a real description
                "anthropic":   {"enabled": True},   # unknown → must be silently skipped
                "xai":         {"enabled": True},   # unknown → must be silently skipped
                "somerandomplugin": {"enabled": True},  # unknown → must be silently skipped
            }
        }
    })
    s = ss.build_summary("team_bot_a", bot_home=bot_home, signals=[])
    bad = [p for p in s.can_do if p.startswith('Use the "') or p.startswith("Use the '")]
    assert not bad, f"'Use the <skill>' entries must not appear in can_do; found: {bad}"


# ─────────────────────────────────────────────────────────────────────
# Security page HTML structure guard
# ─────────────────────────────────────────────────────────────────────

def test_security_page_html_has_audit_as_default_tab():
    """The Security page HTML must have Audit as the default active tab
    and must NOT have a Safety summary tab (removed 2026-05-13).
    This is a static content test on the rendered template file.
    """
    html_path = _ADMIN_DIR / "evolve_admin" / "web" / "index.html"
    html = html_path.read_text()

    # The "Safety summary" tab element must be gone
    assert 'Safety summary' not in html or 'sec-main-tab-safety' not in html, (
        "Safety summary tab must be removed from Security page"
    )
    # "Safety summary" can appear in comments but not as a rendered tab label
    # Check the actual tab div is gone
    assert 'id="sec-main-tab-safety"' not in html, (
        "sec-main-tab-safety element must be removed"
    )

    # The Audit tab must be the default-active tab (class="subtab active")
    # and it must appear before all other sec-main tabs
    import re
    audit_active = re.search(
        r'class="subtab active"[^>]*id="sec-main-tab-audit"|id="sec-main-tab-audit"[^>]*class="subtab active"',
        html
    )
    assert audit_active, (
        "sec-main-tab-audit must have class 'subtab active' (Audit is the default tab)"
    )

    # The "Advanced view" toggle must be gone
    assert 'sec-main-tab-advanced-toggle' not in html, (
        "Advanced view toggle must be removed — tabs are now flat"
    )
    assert 'sec-advanced-tabs' not in html, (
        "sec-advanced-tabs span wrapper must be removed — tabs are now flat"
    )
