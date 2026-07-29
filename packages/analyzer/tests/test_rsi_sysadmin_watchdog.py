"""tests/test_rsi_sysadmin_watchdog.py — Sysadmin Watchdog detectors.

Phase 1b architecture: every platform-failure detector emits a Signal
(via ``detect_signal``); only ACL drift also emits a Proposal (an
autonomous-eligible ConfigPatch with a back-link to its Signal).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.sysadmin_watchdog import observe, observe_signals  # noqa: E402
from generators.sysadmin_watchdog.observe import DetectorContext  # noqa: E402
from generators.sysadmin_watchdog.detectors.platform import (  # noqa: E402
    detect_acl,
    detect_acl_signal,
    detect_config_validity_signal,
    detect_gateway_signal,
    detect_launchd_signal,
    detect_plugin_signal,
    detect_users_signal,
    detect_version_signal,
)
from metrics.registry import MetricValue  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

import pytest  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _resolver(metric_values: dict[str, MetricValue]) -> Callable:
    """Return a resolve function that looks up values by name."""

    def r(name, bot_id, t):
        return metric_values.get(name, MetricValue(value=1.0, confidence=1.0))

    return r


def _ctx(
    metric_values: dict[str, MetricValue],
    *,
    audience: str = "pod_operator",
    shared_dir: Path | None = None,
) -> DetectorContext:
    return DetectorContext(
        bot_id="team_bot_a",
        now=_NOW,
        resolve=_resolver(metric_values),
        sysadmin_audience=audience,
        shared_dir=shared_dir,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gateway detector — Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_gateway_signal_quiet_when_up():
    ctx = _ctx({"gateway.up": MetricValue(1.0)})
    assert detect_gateway_signal(ctx) is None


def test_gateway_signal_quiet_below_threshold():
    ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(1.0),
        }
    )
    assert detect_gateway_signal(ctx) is None  # below default threshold 3


def test_gateway_signal_fires_at_threshold_with_warn_severity():
    ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(3.0),
        }
    )
    spec = detect_gateway_signal(ctx)
    assert spec is not None
    assert spec["type"] == "gateway_down"
    assert spec["flavor"] == "maintenance"
    assert spec["severity"] == "warn"
    assert spec["details"]["chronic"] is False


def test_gateway_signal_escalates_to_alert_when_chronic():
    ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(10.0),
        }
    )
    spec = detect_gateway_signal(ctx)
    assert spec is not None
    assert spec["severity"] == "alert"
    assert spec["details"]["chronic"] is True
    # The chronic body carries the kickstart runbook
    assert "launchctl kickstart" in spec["body"]


def test_gateway_signal_keeps_signature_stable_across_escalation():
    """warn-then-alert collapses into one incident on the Alerts page."""
    warn_ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(3.0),
        }
    )
    alert_ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(10.0),
        }
    )
    warn = detect_gateway_signal(warn_ctx)
    alert = detect_gateway_signal(alert_ctx)
    assert warn["signature"] == alert["signature"]


# ─────────────────────────────────────────────────────────────────────────────
# ACL detector — Signal + Proposal (the only dual-emit detector)
# ─────────────────────────────────────────────────────────────────────────────


def test_acl_signal_quiet_when_readable():
    ctx = _ctx({"acl.evolve_read": MetricValue(1.0)})
    assert detect_acl_signal(ctx) is None


def test_acl_signal_fires_on_drift():
    ctx = _ctx({"acl.evolve_read": MetricValue(0.0)})
    spec = detect_acl_signal(ctx)
    assert spec is not None
    assert spec["type"] == "acl_drift"
    assert spec["flavor"] == "maintenance"
    assert spec["severity"] == "warn"


def test_acl_proposal_quiet_when_readable():
    ctx = _ctx({"acl.evolve_read": MetricValue(1.0)})
    assert detect_acl(ctx) == []


def test_acl_proposal_fires_on_drift_without_shared_dir():
    """ACL detector must still emit a Proposal even when shared_dir is unavailable.

    The cross-link to the motivating Signal is best-effort; the Proposal
    itself fires regardless so the autonomous fix can be queued.
    """
    ctx = _ctx({"acl.evolve_read": MetricValue(0.0)})
    proposals = detect_acl(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.risk_tag.reversibility == "auto"  # autonomous-eligible
    assert p.urgency == "substrate_warn"
    assert p.claim.metric == "acl.evolve_read"
    # Without shared_dir, the link list is empty; that's the documented fallback.
    assert p.motivating_signals == []


def test_acl_proposal_links_to_signal_when_shared_dir_provided(tmp_path):
    """When the Signal exists in the store, the Proposal back-links to it."""
    from signals import store as signals_store

    # Pre-emit the matching Signal (simulates observe_signals running first
    # in the runner pipeline).
    spec = detect_acl_signal(_ctx({"acl.evolve_read": MetricValue(0.0)}))
    sig = signals_store.observe(tmp_path, **spec)

    ctx = _ctx({"acl.evolve_read": MetricValue(0.0)}, shared_dir=tmp_path)
    proposals = detect_acl(ctx)
    assert len(proposals) == 1
    assert proposals[0].motivating_signals == [sig.id]


# ─────────────────────────────────────────────────────────────────────────────
# Config validity detector — Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_config_validity_signal_fires_on_invalid():
    ctx = _ctx({"openclaw_config.valid": MetricValue(0.0)})
    spec = detect_config_validity_signal(ctx)
    assert spec is not None
    assert spec["type"] == "openclaw_config_invalid"
    assert spec["severity"] == "alert"


def test_config_validity_signal_quiet_when_valid():
    ctx = _ctx({"openclaw_config.valid": MetricValue(1.0)})
    assert detect_config_validity_signal(ctx) is None


# ─────────────────────────────────────────────────────────────────────────────
# Plugin detector — Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_plugin_signal_quiet_when_loaded():
    ctx = _ctx({"plugin.loaded": MetricValue(1.0), "gateway.up": MetricValue(1.0)})
    assert detect_plugin_signal(ctx) is None


def test_plugin_signal_fires_when_missing():
    ctx = _ctx(
        {
            "plugin.loaded": MetricValue(0.0),
            "gateway.up": MetricValue(1.0),
        }
    )
    spec = detect_plugin_signal(ctx)
    assert spec is not None
    assert spec["type"] == "plugin_missing"
    assert spec["severity"] == "alert"
    # Body carries the kickstart runbook so the Maintenance lane can render
    # an actionable card.
    assert "launchctl kickstart" in spec["body"]


def test_plugin_signal_silent_when_gateway_itself_down():
    # Gateway detector owns the higher-priority signal; plugin detector
    # defers to avoid surfacing duplicate noise.
    ctx = _ctx(
        {
            "plugin.loaded": MetricValue(0.0),
            "gateway.up": MetricValue(0.0),
        }
    )
    assert detect_plugin_signal(ctx) is None


# ─────────────────────────────────────────────────────────────────────────────
# LaunchD / systemd detector — Signal (platform-aware)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _macos_profile():
    """Pin MACOS so the gateway-service detector emits the launchd-named
    signal regardless of the CI runner's OS. Resets on teardown."""
    set_profile(MACOS)
    try:
        yield
    finally:
        set_profile(None)


@pytest.fixture
def _linux_profile():
    """Pin LINUX so the detector emits the systemd-named signal. Resets on
    teardown — a leaked override poisons later profile-sensitive tests."""
    set_profile(LINUX)
    try:
        yield
    finally:
        set_profile(None)


def test_launchd_signal_quiet_when_loaded(_macos_profile):
    ctx = _ctx({"launchd.service_loaded": MetricValue(1.0)})
    assert detect_launchd_signal(ctx) is None


def test_launchd_signal_quiet_when_loaded_linux(_linux_profile):
    ctx = _ctx({"launchd.service_loaded": MetricValue(1.0)})
    assert detect_launchd_signal(ctx) is None


def test_launchd_signal_fires_when_not_loaded(_macos_profile):
    ctx = _ctx({"launchd.service_loaded": MetricValue(0.0)})
    spec = detect_launchd_signal(ctx)
    assert spec is not None
    assert spec["type"] == "launchd_not_loaded"
    assert spec["severity"] == "alert"
    assert "launchctl load" in spec["body"]


def test_launchd_signal_fires_systemd_on_linux(_linux_profile):
    """REGRESSION: on a Linux pod a not-loaded gateway must NOT emit the
    launchd-named signal with a macOS-only `launchctl load` runbook. It
    emits a distinct `systemd_not_loaded` signal with a `systemctl`
    runbook so the operator gets a runnable recovery command."""
    ctx = _ctx({"launchd.service_loaded": MetricValue(0.0)})
    spec = detect_launchd_signal(ctx)
    assert spec is not None
    assert spec["type"] == "systemd_not_loaded"
    assert spec["severity"] == "alert"
    assert "launchctl" not in spec["body"]
    assert "systemctl enable --now" in spec["body"]


# ─────────────────────────────────────────────────────────────────────────────
# Users detector — Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_users_signal_quiet_when_user_exists():
    ctx = _ctx({"platform.user_exists": MetricValue(1.0)})
    assert detect_users_signal(ctx) is None


def test_users_signal_fires_when_user_missing():
    ctx = _ctx({"platform.user_exists": MetricValue(0.0)})
    spec = detect_users_signal(ctx)
    assert spec is not None
    assert spec["type"] == "user_missing"
    assert spec["severity"] == "alert"
    # The body should reference the deploy command since that creates the account.
    assert "evolve-admin deploy" in spec["body"]
    assert spec["details"].get("expected_user")


# ─────────────────────────────────────────────────────────────────────────────
# Version detector — Signal
# ─────────────────────────────────────────────────────────────────────────────


def test_version_signal_fires_above_threshold():
    ctx = _ctx(
        {
            "version.currency_days_behind": MetricValue(20.0, confidence=1.0),
        }
    )
    spec = detect_version_signal(ctx)
    assert spec is not None
    assert spec["type"] == "version_behind"
    assert spec["severity"] == "warn"
    assert spec["details"]["days_behind"] == 20


def test_version_signal_quiet_below_threshold():
    ctx = _ctx(
        {
            "version.currency_days_behind": MetricValue(5.0, confidence=1.0),
        }
    )
    assert detect_version_signal(ctx) is None


def test_version_signal_silent_on_low_confidence():
    ctx = _ctx(
        {
            "version.currency_days_behind": MetricValue(30.0, confidence=0.3),
        }
    )
    assert detect_version_signal(ctx) is None


# ─────────────────────────────────────────────────────────────────────────────
# Full observe_signals() / observe() roll-up
# ─────────────────────────────────────────────────────────────────────────────


def test_observe_signals_all_clean():
    ctx = _ctx({})  # defaults to 1.0 for all metrics → healthy
    assert observe_signals(ctx) == []


def test_observe_signals_multiple_detectors_fire():
    ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(3.0),
            "acl.evolve_read": MetricValue(0.0),
            "openclaw_config.valid": MetricValue(0.0),
        }
    )
    specs = observe_signals(ctx)
    types = {s["type"] for s in specs}
    # gateway_down + acl_drift + openclaw_config_invalid; plugin defers
    # because gateway is down; launchd/users/version are healthy by default.
    assert types == {"gateway_down", "acl_drift", "openclaw_config_invalid"}


def test_observe_proposals_only_acl_path(tmp_path):
    """Per Phase 1b + Phase 6c, observe() emits at most one
    CandidateProposal (the ACL-drift ConfigPatch). observe() itself
    returns []; we read from the candidate store."""
    ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(10.0),
            "acl.evolve_read": MetricValue(0.0),
            "openclaw_config.valid": MetricValue(0.0),
            "platform.user_exists": MetricValue(0.0),
            "launchd.service_loaded": MetricValue(0.0),
        },
        shared_dir=tmp_path,
    )
    # The ACL-restore Proposal fires in lock-step with its Signal (flap
    # hysteresis, docs/spec-transient-signal-suppression-2026-06-23.md): mirror
    # the runner, which writes observe_signals() output before running
    # observe(). Pre-emit the (already-promoted) acl_drift Signal so the
    # proposal pass sees it active.
    from generators.sysadmin_watchdog.signals import acl_drift_signal_kwargs
    from signals import store as _signals_store

    _signals_store.observe(tmp_path, **acl_drift_signal_kwargs(ctx.bot_id))

    assert observe(ctx) == []
    from proposal_synthesizer.store import iter_candidates as _iter

    cands = list(_iter(tmp_path, subdirs=("pending",)))
    assert len(cands) == 1
    assert cands[0].draft_action.__class__.__name__ == "ConfigPatch"
    assert cands[0].trigger_observations == [f"acl_drift:{ctx.bot_id}"]


def test_observe_handles_detector_exception():
    """A raising signal detector shouldn't crash observe_signals()."""
    from generators.sysadmin_watchdog import detectors

    original = detectors.platform.ALL_SIGNAL_DETECTORS

    def bad(ctx):
        raise RuntimeError("boom")

    detectors.platform.ALL_SIGNAL_DETECTORS = tuple(list(original) + [bad])
    try:
        ctx = _ctx({})
        # Doesn't raise; other detectors still run.
        assert observe_signals(ctx) == []
    finally:
        detectors.platform.ALL_SIGNAL_DETECTORS = original
