"""tests/test_rsi_metrics.py — Metric registry + resolvers."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from metrics import (  # noqa: E402
    MetricSpec,
    MetricValue,
    UnknownMetricError,
    known,
    register,
    resolve,
)
from metrics.registry import unregister  # noqa: E402
from metrics.resolvers import acl as acl_mod  # noqa: E402  # isort: skip
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402
from metrics.resolvers import gateway as gateway_mod  # noqa: E402
from metrics.resolvers import launchd as launchd_mod  # noqa: E402
from metrics.resolvers import openclaw_config as config_mod  # noqa: E402
from metrics.resolvers import plugin as plugin_mod  # noqa: E402
from metrics.resolvers import users as users_mod  # noqa: E402
from metrics.resolvers import version as version_mod  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


def test_all_l2_metrics_registered():
    names = known()
    for expected in (
        "gateway.up",
        "gateway.consecutive_failures_24h",
        "openclaw_config.valid",
        "acl.evolve_read",
        "plugin.loaded",
        "launchd.service_loaded",
        "platform.user_exists",
        "version.currency_days_behind",
    ):
        assert expected in names


def test_resolve_unknown_metric_raises():
    with pytest.raises(UnknownMetricError):
        resolve("not.a.real.metric", "team_bot_a", _NOW)


def test_register_overrides_previous():
    try:
        register(
            MetricSpec("tempmetric", "test", "count", "test"),
            lambda b, t: MetricValue(value=7.0),
        )
        v = resolve("tempmetric", "team_bot_a", _NOW)
        assert v.value == 7.0
    finally:
        unregister("tempmetric")


# ─────────────────────────────────────────────────────────────────────────────
# ACL resolver
# ─────────────────────────────────────────────────────────────────────────────


def test_acl_resolver_ok_when_checker_returns_true():
    acl_mod.set_acl_checker(lambda b: (True, "readable"))
    try:
        v = resolve("acl.evolve_read", "team_bot_a", _NOW)
        assert v.value == 1.0
    finally:
        acl_mod.set_acl_checker(acl_mod._default_checker)


def test_acl_resolver_fails_when_checker_returns_false():
    acl_mod.set_acl_checker(lambda b: (False, "blocked"))
    try:
        v = resolve("acl.evolve_read", "team_bot_a", _NOW)
        assert v.value == 0.0
        assert "blocked" in v.source_note
    finally:
        acl_mod.set_acl_checker(acl_mod._default_checker)


# ─────────────────────────────────────────────────────────────────────────────
# OpenClaw config validity
# ─────────────────────────────────────────────────────────────────────────────


def test_openclaw_config_valid_returns_1_when_json_parses(tmp_path):
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({"ui": {"theme": "light"}}))
    config_mod.set_config_path_resolver(lambda b: path)
    try:
        v = resolve("openclaw_config.valid", "team_bot_a", _NOW)
        assert v.value == 1.0
    finally:
        config_mod.set_config_path_resolver(config_mod._default_path_resolver)


def test_openclaw_config_invalid_when_malformed(tmp_path):
    path = tmp_path / "openclaw.json"
    path.write_text("{not json")
    config_mod.set_config_path_resolver(lambda b: path)
    try:
        v = resolve("openclaw_config.valid", "team_bot_a", _NOW)
        assert v.value == 0.0
    finally:
        config_mod.set_config_path_resolver(config_mod._default_path_resolver)


def test_openclaw_config_invalid_when_missing(tmp_path):
    missing = tmp_path / "doesnotexist.json"
    config_mod.set_config_path_resolver(lambda b: missing)
    try:
        v = resolve("openclaw_config.valid", "team_bot_a", _NOW)
        assert v.value == 0.0
    finally:
        config_mod.set_config_path_resolver(config_mod._default_path_resolver)


# ─────────────────────────────────────────────────────────────────────────────
# launchd resolver (mocked runner)
# ─────────────────────────────────────────────────────────────────────────────


def test_launchd_resolver_default_label_matches_deployed_convention():
    """The default label MUST match what evolve_admin.deploy.install_bot_gateway_plist
    installs and what heal.restart_gateway probes — otherwise every probe
    returns "not loaded" against a healthy daemon and sysadmin_watchdog files
    a false-positive proposal per (bot × cycle). Pin this so it cannot drift."""
    assert launchd_mod._default_label("admin_bot") == "ai.openclaw.admin_bot-gateway"
    assert launchd_mod._default_label("team_bot_a") == "ai.openclaw.team_bot_a-gateway"
    # Bots whose macOS user differs from bot_id (e.g. team_bot_b → personal_bot_user) still
    # use bot_id in the label — the label is not user-derived.
    assert launchd_mod._default_label("team_bot_b") == "ai.openclaw.team_bot_b-gateway"


@pytest.fixture
def _macos_profile():
    """Pin the macOS profile so the launchd resolver takes the
    ``launchctl print`` branch regardless of the CI runner's OS (these
    tests assert the macOS unprivileged-print posture). Resets to
    autodetection on teardown — a leaked override would poison every later
    profile-sensitive test."""
    set_profile(MACOS)
    try:
        yield
    finally:
        set_profile(None)


@pytest.fixture
def _linux_profile():
    """Pin the Linux profile so the launchd resolver takes the
    ``get_scheduler().status()`` branch. Resets on teardown."""
    set_profile(LINUX)
    try:
        yield
    finally:
        set_profile(None)


def test_launchd_resolver_up_when_state_running(_macos_profile):
    """`launchctl print system/<label>` succeeds and reports state=running
    → loaded. Mirrors the actual stdout shape so a future refactor that
    parses different fields breaks here, not in production. The fake is a
    seam-shaped runner (argv → (rc, stdout, stderr)) — the Scheduler seam's
    LaunchdScheduler builds the argv; no launchctl process is spawned."""
    launchd_mod.set_launchctl_runner(
        lambda cmd: (
            0,
            (
                f"{cmd[-1]} = {{\n"
                "\tactive count = 1\n"
                "\tpath = /Library/LaunchDaemons/...\n"
                "\ttype = LaunchDaemon\n"
                "\tstate = running\n"
                "}\n"
            ),
            "",
        )
    )
    try:
        v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
        assert v.value == 1.0
    finally:
        launchd_mod.set_launchctl_runner(launchd_mod._default_runner)


def test_launchd_resolver_down_when_missing(_macos_profile):
    """`launchctl print system/<missing>` returns nonzero — not loaded."""
    launchd_mod.set_launchctl_runner(
        lambda cmd: (113, "Could not find service", "")  # noqa: ARG005
    )
    try:
        v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
        assert v.value == 0.0
    finally:
        launchd_mod.set_launchctl_runner(launchd_mod._default_runner)


def test_launchd_resolver_down_when_loaded_but_not_running(_macos_profile):
    """A daemon can be loaded (rc=0) but in a non-running state (waiting,
    crashed, etc.). Treat that as unloaded for sysadmin_watchdog's purpose
    — the gateway isn't actually serving requests."""
    launchd_mod.set_launchctl_runner(
        lambda cmd: (
            0,
            (
                f"{cmd[-1]} = {{\n"
                "\tactive count = 0\n"
                "\tstate = not running\n"
                "}\n"
            ),
            "",
        )
    )
    try:
        v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
        assert v.value == 0.0
    finally:
        launchd_mod.set_launchctl_runner(launchd_mod._default_runner)


def test_launchd_resolver_invocation_failure_is_low_confidence(_macos_profile):
    """launchctl couldn't be invoked at all (timeout/OSError) → value 0.0 at
    confidence 0.7 — distinct from the authoritative rc!=0 "not loaded"
    (confidence 1.0). The Scheduler seam's default runner would fold the
    exception into rc=1 and erase this tri-state; the resolver's dedicated
    propagating runner keeps it. Pin the contract here."""
    import subprocess as _sp

    def _boom(cmd):
        raise _sp.TimeoutExpired(cmd, 5)

    launchd_mod.set_launchctl_runner(_boom)
    try:
        v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
        assert v.value == 0.0
        assert v.confidence == 0.7
        assert "invocation failed" in v.source_note
    finally:
        launchd_mod.set_launchctl_runner(launchd_mod._default_runner)


# ── Linux branch: get_scheduler().status() — the false-signal regression ──────
#
# On Linux the resolver routes through the active get_scheduler() adapter
# (SystemdScheduler on a real pod; a FakeScheduler in these tests) via the
# platform-neutral status() verb. The bug this kills: before the platform
# branch, the resolver ALWAYS ran LaunchdScheduler.raw("print", …), which on
# a non-launchd adapter raised through get_launchd_scheduler's FAIL-FAST
# guard → MetricValue(0.0) → sysadmin_watchdog fired a false
# launchd_not_loaded Signal for every bot, every cycle. These pin the cure:
# a loaded+running gateway returns 1.0, and the probe never raises.


@pytest.fixture
def _seam_fake_scheduler():
    """Inject a FakeScheduler into the process-wide Scheduler seam and yield
    it for seeding. Resets the singleton via set_scheduler(None) on teardown
    — a leaked fake poisons every later test that calls get_scheduler()."""
    from runtime.scheduler import FakeScheduler, set_scheduler

    fake = FakeScheduler()
    set_scheduler(fake)
    try:
        yield fake
    finally:
        set_scheduler(None)


def test_launchd_resolver_linux_up_when_loaded_and_running(
    _linux_profile, _seam_fake_scheduler
):
    """REGRESSION: on Linux a loaded+running gateway must return 1.0. Before
    the platform branch the resolver raised (launchd-only raw() on a
    non-launchd adapter) → 0.0 → false launchd_not_loaded Signal on every
    Linux bot. Seed the gateway unit as running and assert truthful 1.0."""
    from runtime.scheduler import JobSpec

    label = launchd_mod._default_label("team_bot_a")
    _seam_fake_scheduler.seed_job(
        JobSpec(label=label, program_args=["/usr/bin/true"], keep_alive=True),
        running=True,
    )
    v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
    assert v.value == 1.0
    assert v.confidence == 1.0


def test_launchd_resolver_linux_down_when_not_managed(
    _linux_profile, _seam_fake_scheduler
):
    """On Linux an unknown/unregistered gateway → 0.0 (authoritative). The
    FakeScheduler reports managed=False for a label it never installed."""
    v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
    assert v.value == 0.0
    assert v.confidence == 1.0
    assert "not loaded" in v.source_note


def test_launchd_resolver_linux_down_when_loaded_but_not_running(
    _linux_profile, _seam_fake_scheduler
):
    """On Linux a loaded-but-stopped gateway → 0.0 (the gateway isn't
    actually serving), matching the macOS not-running semantics."""
    from runtime.scheduler import JobSpec

    label = launchd_mod._default_label("team_bot_a")
    _seam_fake_scheduler.seed_job(
        JobSpec(label=label, program_args=["/usr/bin/true"]),
        running=False,
    )
    v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
    assert v.value == 0.0
    assert v.confidence == 1.0
    assert "not running" in v.source_note


def test_launchd_resolver_linux_probe_failure_is_low_confidence(
    _linux_profile, _seam_fake_scheduler
):
    """On Linux a status-probe failure (sudo couldn't escalate, systemctl
    errored) → value 0.0 at confidence 0.7 — never asserts "not loaded"
    when the probe was unauthoritative. Mirrors the macOS invocation-failure
    tri-state."""
    from runtime.scheduler import JobSpec

    label = launchd_mod._default_label("team_bot_a")
    _seam_fake_scheduler.seed_job(
        JobSpec(label=label, program_args=["/usr/bin/true"], keep_alive=True),
        status_error="cannot_escalate",
    )
    v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
    assert v.value == 0.0
    assert v.confidence == 0.7
    assert "probe failed" in v.source_note


def test_launchd_resolver_linux_never_raises_on_default_systemd_adapter(
    _linux_profile,
):
    """The non-negotiable invariant: with the REAL SystemdScheduler active
    (no fake), the Linux probe must NOT raise — even when systemctl is
    absent. A FakeScheduler hides the original bug (it had raw()-free
    status); this pins that the genuine Linux adapter path is exception-free
    so a loaded gateway can never silently become a false 0.0-by-crash."""
    from runtime.scheduler import SystemdScheduler, set_scheduler

    # use_sudo=False + a runner that simulates "systemctl not found" the way
    # the adapter's default subprocess runner folds an OSError: rc=1. The
    # resolver must classify this as a clean 0.0/0.0-ish value, never raise.
    def _no_systemctl(argv):  # noqa: ARG001
        return 1, "", "systemctl: command not found"

    set_scheduler(SystemdScheduler(use_sudo=False, runner=_no_systemctl))
    try:
        v = resolve("launchd.service_loaded", "team_bot_a", _NOW)
        # rc=1 with non-"not-found"-rc → status_error="error" → low confidence,
        # but crucially: NO EXCEPTION propagated to the resolver.
        assert v.value == 0.0
    finally:
        set_scheduler(None)


# ─────────────────────────────────────────────────────────────────────────────
# users resolver (mocked account checker + user resolver)
#
# platform.user_exists probes account existence via pwd.getpwnam (POSIX —
# macOS Open Directory + Linux passwd/getent), NOT the macOS-only `dscl`
# binary. The dscl probe raised OSError for every bot on a Linux pod →
# value=0.0 → `user_missing` fired pod-wide; the tests below pin the
# portable behavior on BOTH platform profiles.
# ─────────────────────────────────────────────────────────────────────────────


def test_users_resolver_present_when_account_exists():
    users_mod.set_user_resolver(lambda b: b)
    users_mod.set_account_checker(lambda u: True)
    try:
        v = resolve("platform.user_exists", "team_bot_a", _NOW)
        assert v.value == 1.0
        assert v.confidence == 1.0
        assert "team_bot_a" in v.source_note
    finally:
        users_mod.set_account_checker(users_mod._default_account_checker)
        users_mod.set_user_resolver(users_mod._default_user_resolver)


def test_users_resolver_missing_when_account_absent():
    users_mod.set_user_resolver(lambda b: b)
    users_mod.set_account_checker(lambda u: False)
    try:
        v = resolve("platform.user_exists", "missing_bot", _NOW)
        assert v.value == 0.0
        assert v.confidence == 1.0
        assert "missing_bot" in v.source_note
    finally:
        users_mod.set_account_checker(users_mod._default_account_checker)
        users_mod.set_user_resolver(users_mod._default_user_resolver)


def test_users_resolver_uses_mapped_username_for_aliased_bot():
    """Bots with a per-bot user override (e.g. team_bot_b → personal_bot_user) must probe the
    *mapped* account, not the bot_id. This is the regression case the legacy
    HealthCheckAdapter hit — it always probed ``user:team_bot_b`` and reported
    "user 'team_bot_b' not found" because personal_bot_user owns that account."""
    seen_users: list[str] = []

    def _capture(user: str) -> bool:
        seen_users.append(user)
        return True

    users_mod.set_user_resolver(lambda b: "personal_bot_user" if b == "team_bot_b" else b)
    users_mod.set_account_checker(_capture)
    try:
        v = resolve("platform.user_exists", "team_bot_b", _NOW)
        assert v.value == 1.0
        assert seen_users == ["personal_bot_user"]
    finally:
        users_mod.set_account_checker(users_mod._default_account_checker)
        users_mod.set_user_resolver(users_mod._default_user_resolver)


def test_users_resolver_default_checker_portable_on_both_profiles():
    """The DEFAULT account checker is ``pwd.getpwnam`` (portable), NOT the
    macOS-only ``dscl`` shell-out. On a fresh Linux pod the old probe raised
    OSError for EVERY bot → value=0.0 → ``user_missing`` fired pod-wide. Here
    we exercise the real default checker against the CI runner's own (existing)
    account and against an impossible name, under BOTH platform profiles, to
    prove the result no longer depends on a macOS binary."""
    import os as _os
    import pwd as _pwd

    real_user = _pwd.getpwuid(_os.getuid()).pw_name

    for profile in (MACOS, LINUX):
        set_profile(profile)
        try:
            users_mod.set_user_resolver(lambda b, _u=real_user: _u)
            v = resolve("platform.user_exists", "any_bot", _NOW)
            assert v.value == 1.0, f"existing account, profile={profile.name}"

            users_mod.set_user_resolver(lambda b: "nonexistent_account_zzzq")
            v = resolve("platform.user_exists", "any_bot", _NOW)
            assert v.value == 0.0, f"absent account, profile={profile.name}"
            assert v.confidence == 1.0, f"absent account, profile={profile.name}"
        finally:
            users_mod.set_user_resolver(users_mod._default_user_resolver)
            set_profile(None)


# ─────────────────────────────────────────────────────────────────────────────
# plugin resolver
# ─────────────────────────────────────────────────────────────────────────────


def test_plugin_resolver_returns_1_when_evolve_listed():
    plugin_mod.set_plugin_checker(
        lambda b: (True, "plugin 'evolve' loaded")
    )
    try:
        v = resolve("plugin.loaded", "team_bot_a", _NOW)
        assert v.value == 1.0
    finally:
        plugin_mod.set_plugin_checker(plugin_mod._default_checker)


def test_plugin_resolver_returns_0_when_missing():
    plugin_mod.set_plugin_checker(lambda b: (False, "no evolve plugin"))
    try:
        v = resolve("plugin.loaded", "team_bot_a", _NOW)
        assert v.value == 0.0
    finally:
        plugin_mod.set_plugin_checker(plugin_mod._default_checker)


# ── Plugin response-shape parser (pure function, no HTTP) ────────────────────
#
# The earlier parser looked for a `plugins[]` array with names containing
# "evolve". The actual /evolve/status response uses top-level keys
# (bot_id, plugin_version, status, ...) and has no `plugins` array, so
# every healthy probe was reported as "plugin not loaded". These tests pin
# the real response shape against synthetic but realistic bodies.


def test_plugin_response_recognizes_real_status_response_shape():
    """Captured from the live mini on 2026-04-30: openclaw's /evolve/status
    returns a flat object with bot_id / plugin_version / status. The parser
    must accept this as proof the plugin is loaded."""
    real_body = (
        '{"bot_id":"admin_bot","role":"member","network_id":"my-pod",'
        '"plugin_version":"0.1.0","status":"ok",'
        '"today_summary":{"sessions":0,"turns":0,"maintenance_ratio":0,"api_key_turns":0},'
        '"generated_at":"2026-05-01T06:08:45.283Z"}'
    )
    loaded, note = plugin_mod._response_indicates_loaded(real_body)
    assert loaded is True, f"real response should indicate loaded; note={note!r}"
    assert "0.1.0" in note  # surface the version for log readability


def test_plugin_response_minimal_shape_with_just_bot_id():
    """Forward-compat: if openclaw trims the response in the future, a
    single recognizable plugin key (bot_id) is still sufficient evidence."""
    loaded, _ = plugin_mod._response_indicates_loaded('{"bot_id": "admin_bot"}')
    assert loaded is True


def test_plugin_response_rejects_non_plugin_json():
    """If /evolve/status somehow returns valid JSON but without any of our
    plugin marker keys (e.g. an unrelated handler caught the path), don't
    falsely claim the plugin is loaded."""
    loaded, note = plugin_mod._response_indicates_loaded('{"unrelated": "data"}')
    assert loaded is False
    assert "lacks any of" in note


def test_plugin_response_rejects_invalid_json():
    loaded, note = plugin_mod._response_indicates_loaded("not-json-at-all")
    assert loaded is False
    assert "not JSON" in note


def test_plugin_response_rejects_non_object_json():
    """A JSON array or scalar at the top level is not a plugin response."""
    loaded, _ = plugin_mod._response_indicates_loaded('["bot_id", "plugin_version"]')
    assert loaded is False


# ─────────────────────────────────────────────────────────────────────────────
# version resolver
# ─────────────────────────────────────────────────────────────────────────────


def test_version_currency_days_behind():
    version_mod.set_version_sources(
        lambda b: version_mod.VersionSources(
            deployed_date=date(2026, 5, 1),
            latest_date=date(2026, 5, 15),
        )
    )
    try:
        v = resolve("version.currency_days_behind", "team_bot_a", _NOW)
        assert v.value == 14.0
        assert v.confidence == 1.0
    finally:
        version_mod.set_version_sources(version_mod._default_sources)


def test_version_low_confidence_when_no_data():
    version_mod.set_version_sources(
        lambda b: version_mod.VersionSources(None, None)
    )
    try:
        v = resolve("version.currency_days_behind", "team_bot_a", _NOW)
        assert v.confidence < 0.5
    finally:
        version_mod.set_version_sources(version_mod._default_sources)


# ─────────────────────────────────────────────────────────────────────────────
# gateway resolvers
# ─────────────────────────────────────────────────────────────────────────────


def test_gateway_failures_count_reads_incidents(tmp_path):
    gateway_mod.set_shared_dir(tmp_path)
    try:
        # Seed today's incidents
        today = _NOW.date().isoformat()
        inc_dir = tmp_path / "incidents" / today
        inc_dir.mkdir(parents=True)

        for i, incident in enumerate(
            [
                {"type": "gateway_down", "bot_id": "team_bot_a"},
                {"type": "gateway_timeout", "bot_id": "team_bot_a"},
                {"type": "gateway_down", "bot_id": "team_bot_a"},
                {"type": "other_thing", "bot_id": "team_bot_a"},  # not counted
            ]
        ):
            (inc_dir / f"team_bot_a-{i}.json").write_text(json.dumps(incident))

        v = resolve("gateway.consecutive_failures_24h", "team_bot_a", _NOW)
        assert v.value == 3.0
    finally:
        gateway_mod.set_shared_dir(Path("/Users/Shared/evolve"))


def test_gateway_up_returns_low_confidence_when_no_port(tmp_path):
    gateway_mod.set_shared_dir(tmp_path)
    try:
        v = resolve("gateway.up", "nonexistent_bot", _NOW)
        assert v.value == 0.0
        assert v.confidence < 1.0
    finally:
        gateway_mod.set_shared_dir(Path("/Users/Shared/evolve"))
