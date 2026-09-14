"""Tests for evolve_admin.upstream_version.

V1.5-3 pillar: per-bot OpenClaw version detection + fleet-status rollup
+ exec-policy compliance evaluation. The module never raises on read
failure; all errors surface as ``BotVersionReading.read_error`` or
``ExecPolicyComplianceReading.compliant=None``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin import upstream_version as uv


# ─────────────────────────────────────────────────────────────────────────────
# parse_version / version_at_or_above / canonical_version
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("2026.4.12", (2026, 4, 12, 0)),
    ("2026.4.29", (2026, 4, 29, 0)),
    ("v2026.4.12", (2026, 4, 12, 0)),
    ("OpenClaw 2026.4.29 (a448042)", (2026, 4, 29, 0)),
    ("2026.5.12-beta.1", (2026, 5, 12, -1)),
    ("", None),
    (None, None),
    ("not-a-version", None),
    ("2026.4", None),       # missing patch
    ("garbage 2026.4.12 garbage", (2026, 4, 12, 0)),  # regex still finds it
])
def test_parse_version(raw, expected):
    assert uv.parse_version(raw) == expected


def test_version_at_or_above_simple():
    assert uv.version_at_or_above("2026.4.29", "2026.4.12") is True
    assert uv.version_at_or_above("2026.4.12", "2026.4.12") is True
    assert uv.version_at_or_above("2026.4.11", "2026.4.12") is False
    assert uv.version_at_or_above("2025.12.31", "2026.4.12") is False


def test_version_at_or_above_unparseable_is_below():
    """Defensive default: unknown reading is NOT 'at or above floor'."""
    assert uv.version_at_or_above(None, "2026.4.12") is False
    assert uv.version_at_or_above("garbage", "2026.4.12") is False
    assert uv.version_at_or_above("2026.4.12", "garbage") is False


def test_version_prerelease_below_release():
    """2026.5.12-beta.1 ranks below 2026.5.12 release."""
    assert uv.version_at_or_above("2026.5.12-beta.1", "2026.5.12") is False
    assert uv.version_at_or_above("2026.5.12", "2026.5.12-beta.1") is True


@pytest.mark.parametrize("raw,expected", [
    ("2026.4.29", "2026.4.29"),
    ("v2026.4.29", "2026.4.29"),
    ("OpenClaw 2026.4.29 (a448042)", "2026.4.29"),
    ("2026.5.12-beta.1", "2026.5.12"),
    ("garbage", None),
    (None, None),
])
def test_canonical_version(raw, expected):
    assert uv.canonical_version(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026.4.29", "2026.4.29"),
    ("v2026.4.29", "2026.4.29"),
    ("OpenClaw 2026.4.29 (a448042)", "2026.4.29"),
    # The tag npm actually publishes for a re-release — the whole reason this
    # function exists next to canonical_version.
    ("2026.7.1-2", "2026.7.1-2"),
    ("2026.5.12-beta.1", "2026.5.12-beta.1"),
    # A fourth component survives too — parse_version truncates it, and a
    # comparison built on that truncation reads two different builds as one.
    ("2026.7.0.5", "2026.7.0.5"),
    ("garbage", None),
    (None, None),
])
def test_comparable_version_keeps_the_prerelease_tag(raw, expected):
    assert uv.comparable_version(raw) == expected


def test_same_version_does_not_truncate_a_fourth_component():
    assert uv.same_version("2026.7.0.5", "2026.7.0") is False


def test_same_version_matches_across_cosmetic_differences():
    assert uv.same_version("2026.7.1-2", "v2026.7.1-2") is True
    assert uv.same_version("2026.7.1-2", "OpenClaw 2026.7.1-2 (abc)") is True


def test_same_version_treats_a_prerelease_as_a_different_build():
    """The regression this guards: a `-2` re-release is NOT the `.1` release,
    so canonicalizing one side of the comparison must never make them equal."""
    assert uv.same_version("2026.7.1", "2026.7.1-2") is False
    assert uv.same_version("2026.7.1-2", "2026.7.1-2") is True


def test_same_version_fails_closed_on_unparseable_input():
    assert uv.same_version(None, None) is False
    assert uv.same_version(None, "2026.7.1") is False
    assert uv.same_version("garbage", "2026.7.1") is False
    # Two identical unparseable strings are the only unparseable match.
    assert uv.same_version("garbage", "garbage") is True


# ─────────────────────────────────────────────────────────────────────────────
# _read_openclaw_json_meta_version (filesystem-backed)
# ─────────────────────────────────────────────────────────────────────────────


def _write_openclaw_json(tmp_path: Path, meta_version: str | None,
                         *, bot_id: str = "alice",
                         malformed: bool = False) -> Path:
    """Create a fake bot home with .openclaw/openclaw.json."""
    home = tmp_path / bot_id
    (home / ".openclaw").mkdir(parents=True, exist_ok=True)
    oc_path = home / ".openclaw" / "openclaw.json"
    if malformed:
        oc_path.write_text("{not json")
        return home
    data: dict = {}
    if meta_version is not None:
        data["meta"] = {"lastTouchedVersion": meta_version}
    oc_path.write_text(json.dumps(data))
    return home


def test_read_meta_version_happy(tmp_path):
    home = _write_openclaw_json(tmp_path, "2026.4.29")
    raw, err = uv._read_openclaw_json_meta_version("alice", home)
    assert err is None
    assert raw == "2026.4.29"


def test_read_meta_version_absent(tmp_path):
    home = _write_openclaw_json(tmp_path, None)
    raw, err = uv._read_openclaw_json_meta_version("alice", home)
    assert raw is None
    assert err == "lastTouchedVersion_absent"


def test_read_meta_version_file_missing(tmp_path):
    raw, err = uv._read_openclaw_json_meta_version("ghost", tmp_path / "ghost")
    assert raw is None
    assert err == "openclaw_json_not_found"


def test_read_meta_version_malformed_json(tmp_path):
    home = _write_openclaw_json(tmp_path, None, malformed=True)
    raw, err = uv._read_openclaw_json_meta_version("alice", home)
    assert raw is None
    assert err is not None and err.startswith("json_decode")


def test_read_meta_version_meta_not_dict(tmp_path):
    home = tmp_path / "alice"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text('{"meta": "not-an-object"}')
    raw, err = uv._read_openclaw_json_meta_version("alice", home)
    assert raw is None
    assert err == "meta_block_not_object"


# ─────────────────────────────────────────────────────────────────────────────
# read_bot_version (cheap path; cli fallback disabled to keep test offline)
# ─────────────────────────────────────────────────────────────────────────────


def test_read_bot_version_cheap_path(tmp_path):
    home = _write_openclaw_json(tmp_path, "2026.4.29")
    r = uv.read_bot_version("alice", bot_home=home, allow_cli_fallback=False)
    assert r.version == "2026.4.29"
    assert r.source == "openclaw_json_meta"
    assert r.read_error is None
    assert r.raw == "2026.4.29"


def test_read_bot_version_unavailable_no_fallback(tmp_path):
    """File missing + cli fallback disabled → unavailable, not raise."""
    r = uv.read_bot_version("ghost", bot_home=tmp_path / "ghost",
                            allow_cli_fallback=False)
    assert r.version is None
    assert r.source == "unavailable"
    assert r.read_error == "openclaw_json_not_found"


def test_read_bot_version_strips_banner(tmp_path):
    """The CLI fallback would return 'OpenClaw 2026.4.29 (a448042)' style strings;
    confirm read_bot_version handles that through the canonical_version path
    if the meta block carries it.
    """
    home = _write_openclaw_json(tmp_path, "OpenClaw 2026.4.29 (a448042)")
    r = uv.read_bot_version("alice", bot_home=home, allow_cli_fallback=False)
    assert r.version == "2026.4.29"


# ─────────────────────────────────────────────────────────────────────────────
# per_bot_versions + fleet_status
# ─────────────────────────────────────────────────────────────────────────────


def test_per_bot_versions_multi(tmp_path):
    h_alice = _write_openclaw_json(tmp_path, "2026.4.29", bot_id="alice")
    h_bob = _write_openclaw_json(tmp_path, "2026.4.11", bot_id="bob")
    # ghost has no file
    def resolve(bid):
        return {"alice": h_alice, "bob": h_bob, "ghost": tmp_path / "ghost"}[bid]
    readings = uv.per_bot_versions(
        ["alice", "bob", "ghost"],
        bot_home_resolver=resolve,
        allow_cli_fallback=False,
    )
    assert readings["alice"].version == "2026.4.29"
    assert readings["bob"].version == "2026.4.11"
    assert readings["ghost"].version is None


def test_fleet_status_floor_met(tmp_path):
    h_alice = _write_openclaw_json(tmp_path, "2026.4.29", bot_id="alice")
    h_bob = _write_openclaw_json(tmp_path, "2026.4.12", bot_id="bob")
    readings = uv.per_bot_versions(
        ["alice", "bob"],
        bot_home_resolver={"alice": h_alice, "bob": h_bob}.get,
        allow_cli_fallback=False,
    )
    fs = uv.fleet_status(readings, minimum_version="2026.4.12")
    assert fs.floor_met is True
    assert sorted(fs.bots_at_or_above) == ["alice", "bob"]
    assert fs.bots_below == []
    assert fs.bots_unknown == []


def test_fleet_status_one_below(tmp_path):
    h_alice = _write_openclaw_json(tmp_path, "2026.4.29", bot_id="alice")
    h_bob = _write_openclaw_json(tmp_path, "2026.4.11", bot_id="bob")
    readings = uv.per_bot_versions(
        ["alice", "bob"],
        bot_home_resolver={"alice": h_alice, "bob": h_bob}.get,
        allow_cli_fallback=False,
    )
    fs = uv.fleet_status(readings, minimum_version="2026.4.12")
    assert fs.floor_met is False
    assert "alice" in fs.bots_at_or_above
    assert "bob" in fs.bots_below


def test_fleet_status_unknown_flips_floor(tmp_path):
    """A single 'unknown' reading flips floor_met to False — defensive."""
    h_alice = _write_openclaw_json(tmp_path, "2026.4.29", bot_id="alice")
    readings = uv.per_bot_versions(
        ["alice", "ghost"],
        bot_home_resolver={"alice": h_alice, "ghost": tmp_path / "ghost"}.get,
        allow_cli_fallback=False,
    )
    fs = uv.fleet_status(readings, minimum_version="2026.4.12")
    assert fs.floor_met is False
    assert "ghost" in fs.bots_unknown


def test_fleet_status_serializes(tmp_path):
    h_alice = _write_openclaw_json(tmp_path, "2026.4.29", bot_id="alice")
    readings = uv.per_bot_versions(
        ["alice"],
        bot_home_resolver={"alice": h_alice}.get,
        allow_cli_fallback=False,
    )
    fs = uv.fleet_status(readings)
    d = fs.to_dict()
    assert d["minimum_version"] == uv.DEFAULT_MINIMUM_VERSION
    assert d["readings"]["alice"]["version"] == "2026.4.29"
    assert "observed_at" in d


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_exec_policy_compliance — chip state machine
# (internal/spec-app-derived-permissions-2026-05-24.md §7)
#
# Pivoted 2026-05-25 from "are exec permissions tight?" to "is the bot's
# posture coherent?". Default for member bots is GREEN; tightening is the
# operator's choice, celebrated when chosen, not demanded by the chip.
# ─────────────────────────────────────────────────────────────────────────────


def test_compliance_full_is_green_for_member_bot_default():
    """Member-bot default — full mode is the right answer, not a finding."""
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a",
        openclaw_config={"tools": {"exec": {"security": "full"}}},
        exec_approvals=None,
        role="member",
    )
    assert r.state == uv.STATE_GREEN
    assert r.compliant is True
    assert "member-bot default" in r.reason


def test_compliance_full_is_green_even_without_role():
    """Role defaults to member when unspecified — same green outcome."""
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a",
        openclaw_config={"tools": {"exec": {"security": "full"}}},
        exec_approvals=None,
    )
    assert r.state == uv.STATE_GREEN
    assert r.compliant is True


def test_compliance_allowlist_with_entries_is_green():
    """Operator opted into tighter posture — celebrated, not penalized."""
    r = uv.evaluate_exec_policy_compliance(
        "security_bot",
        openclaw_config={"tools": {"exec": {"security": "allowlist"}}},
        exec_approvals={"agents": {"main": {"allowlist": [
            {"pattern": "/usr/bin/curl*"},
            {"pattern": "/bin/ls*"},
        ]}}},
    )
    assert r.state == uv.STATE_GREEN
    assert r.compliant is True
    assert "allowlist" in r.reason


def test_compliance_allowlist_empty_is_red():
    """allowlist mode with no entries — nothing the bot declares can run."""
    r = uv.evaluate_exec_policy_compliance(
        "broken",
        openclaw_config={"tools": {"exec": {"security": "allowlist"}}},
        exec_approvals={"agents": {"main": {}}},  # no allowlist key
    )
    assert r.state == uv.STATE_RED
    assert r.compliant is False
    assert "empty" in r.reason.lower()


def test_compliance_deny_member_with_apps_is_red():
    """Original incident shape: team_bot_a/admin_bot/team_bot_b stuck at deny with installed apps."""
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        role="member",
        has_installed_apps=True,
    )
    assert r.state == uv.STATE_RED
    assert r.compliant is False
    assert "deny" in r.reason.lower()


def test_compliance_deny_primary_is_unknown_after_phase_e4():
    """Phase E.4 (2026-05-25) removed the primary-bot ``deny`` carve-out.
    The chip used to return GREEN for primary+deny ("intended posture");
    post-E.4 there's no intended deny posture for primary bots — they
    follow the same default (``full``) as every other bot. Operator-
    pinned deny is allowed but treated as an unusual state (UNKNOWN
    chip surfacing the override).
    """
    r = uv.evaluate_exec_policy_compliance(
        "evolve",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        role="primary",
        has_installed_apps=True,
    )
    assert r.state == uv.STATE_UNKNOWN
    assert r.compliant is None
    assert "phase e.4" in r.reason.lower()


def test_compliance_deny_member_no_apps_is_green():
    """No apps declared → deny is a coherent posture."""
    r = uv.evaluate_exec_policy_compliance(
        "lonely",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        role="member",
        has_installed_apps=False,
    )
    assert r.state == uv.STATE_GREEN
    assert r.compliant is True


def test_compliance_deny_member_unknown_apps_is_unknown():
    """When we can't read manifests, deny+member becomes inconclusive, not red."""
    r = uv.evaluate_exec_policy_compliance(
        "mystery",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        role="member",
        has_installed_apps=None,
    )
    assert r.state == uv.STATE_UNKNOWN
    assert r.compliant is None


def test_compliance_off_is_green():
    """tools.exec.security='off' — exec disabled at the source; coherent."""
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a",
        openclaw_config={"tools": {"exec": {"security": "off"}}},
        exec_approvals=None,
    )
    assert r.state == uv.STATE_GREEN
    assert r.compliant is True


def test_compliance_unknown_when_oc_missing():
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a", openclaw_config=None, exec_approvals=None,
    )
    assert r.state == uv.STATE_UNKNOWN
    assert r.compliant is None


def test_compliance_unknown_when_unrecognized_mode():
    r = uv.evaluate_exec_policy_compliance(
        "team_bot_a",
        openclaw_config={"tools": {"exec": {"security": "weird"}}},
        exec_approvals=None,
    )
    assert r.state == uv.STATE_UNKNOWN
    assert r.compliant is None


# ─────────────────────────────────────────────────────────────────────────────
# installed_package_version — authoritative reader for the npm-installed binary
# ─────────────────────────────────────────────────────────────────────────────


def test_installed_package_version_reads_canonical_path(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "openclaw", "version": "2026.5.20"}))
    assert uv.installed_package_version(package_json=pkg) == "2026.5.20"


def test_installed_package_version_keeps_the_prerelease_tag(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": "2026.7.1-2"}))
    # The tag SURVIVES. This value's only consumers compare it against npm's
    # version strings, which carry the tag — truncating it here reported a
    # fully-current host as "installed 2026.7.1, latest 2026.7.1-2" and flagged
    # every safety report stale the moment it was written.
    assert uv.installed_package_version(package_json=pkg) == "2026.7.1-2"


def test_installed_package_version_still_normalizes_cosmetics(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": "v2026.5.20"}))
    assert uv.installed_package_version(package_json=pkg) == "2026.5.20"


def test_installed_package_version_missing_file(tmp_path):
    assert uv.installed_package_version(package_json=tmp_path / "missing") is None


def test_installed_package_version_corrupt_json(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text("{ not json")
    assert uv.installed_package_version(package_json=pkg) is None


def test_installed_package_version_missing_version_key(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "openclaw"}))
    assert uv.installed_package_version(package_json=pkg) is None


def test_installed_package_version_non_string_version(tmp_path):
    """Defensive: a numeric ``version`` (technically valid JSON, technically
    not valid per npm) shouldn't crash the caller — return None instead."""
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": 20260520}))
    assert uv.installed_package_version(package_json=pkg) is None


def test_installed_package_version_unparseable_version(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": "not-a-calver"}))
    assert uv.installed_package_version(package_json=pkg) is None


def test_installed_package_version_diverges_from_lasttouched(tmp_path):
    """The motivating case: gateway is running the new binary on disk but
    its lastTouchedVersion (read elsewhere) hasn't caught up yet. The two
    readers disagree, and ``installed_package_version`` is the authoritative
    "what's installed" answer."""
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": "2026.5.20"}))
    # Bot's openclaw.json still says lastTouchedVersion=2026.5.19 — proven
    # elsewhere; this test just pins that the package.json read returns the
    # truth regardless.
    assert uv.installed_package_version(package_json=pkg) == "2026.5.20"


def test_installed_package_version_default_path_is_platform_keyed(monkeypatch, tmp_path):
    """#3194 follow-up: the default package.json (when ``package_json`` is not
    passed) resolves through ``platform_profile.openclaw_pkg_json_candidates``
    — the first candidate that exists — NOT a hardcoded macOS Homebrew path.

    Pin it by pointing the candidate list at a temp file: the default-path read
    must pick it up, proving the resolver no longer hardcodes /opt/homebrew."""
    import platform_profile

    pkg = tmp_path / "openclaw" / "package.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text(json.dumps({"version": "2026.6.8"}))

    class _Prof:
        openclaw_pkg_json_candidates = (pkg,)

    monkeypatch.setattr(platform_profile, "get_profile", lambda: _Prof())
    # No package_json kwarg → must resolve via the (patched) profile candidates.
    assert uv.installed_package_version() == "2026.6.8"


def test_resolve_openclaw_package_json_byte_identical_on_macos():
    """On the macOS profile the resolved default must be the exact prior
    hardcode — byte-identity guard for the #3194-follow-up migration."""
    import platform_profile

    if platform_profile.get_profile() is platform_profile.MACOS:
        resolved = str(uv._resolve_openclaw_package_json())
        assert resolved in (
            "/opt/homebrew/lib/node_modules/openclaw/package.json",
            "/usr/local/lib/node_modules/openclaw/package.json",
        )
        # No Linux path leaks regardless of which candidate exists locally.
        assert "node_modules" in resolved
