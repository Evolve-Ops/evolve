"""tests/test_oc_runtime_controls.py — the registered fixture pairs for the
five OpenClaw runtime-coherence controls (D-CS7).

`tests/fixtures/controls/health__check_oc_*/` carries a `known_good.json`
and a `known_bad.json` for each of `_check_oc_config_valid_under_installed`,
`_check_oc_channel_packages`, `_check_oc_version_coherence`,
`_check_oc_preflight_gate_freshness` and `_check_breaker_notify_path`. THIS FILE is what replays them;
without it the pair is inert JSON that satisfies
`test_every_control_has_two_fixtures` while proving nothing — which is the
same absence-of-evidence pass the D-CS7 programme exists to end.

Each pair differs on EXACTLY ONE axis, named in the fixture's `_axis` field
and asserted below, so a green known_good and a red known_bad isolate the
control's own pass condition rather than two changes at once.

The controls reach their inputs through function-local imports
(`from .upstream_version import installed_package_version`, and so on), so
each is re-read from its module at call time — patching the module attribute
is enough, and nothing here touches a real pod, a real binary or the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin import health
from evolve_admin.health import HealthReport

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTROLS = _REPO_ROOT / "tests" / "fixtures" / "controls"


def _fixture(control: str, name: str) -> dict:
    return json.loads((_CONTROLS / control / name).read_text())


def _only(report: HealthReport, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} check, got {report.checks}"
    return matches[0]


# ── _check_oc_config_valid_under_installed ────────────────────────────────────

_CONFIG = "health__check_oc_config_valid_under_installed"


def _drive_config(monkeypatch, fx: dict) -> HealthReport:
    from evolve_admin import openclaw_config_validator as validator

    result = fx["validator_result"]

    def fake_validate(bot_id, network, **kw):
        return (result["valid"], result["issues"], result["error"])

    monkeypatch.setattr(validator, "validate_bot_openclaw_json", fake_validate)
    report = HealthReport()
    health._check_oc_config_valid_under_installed(report, fx["members"], fx["network"])
    return report


def test_config_valid_known_good_passes(monkeypatch):
    fx = _fixture(_CONFIG, "known_good.json")
    check = _only(_drive_config(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]


def test_config_valid_known_bad_warns(monkeypatch):
    fx = _fixture(_CONFIG, "known_bad.json")
    check = _only(_drive_config(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_config_valid_could_not_ask_warns(monkeypatch):
    """The third state, and the one the docstring is about: a validator that
    could not run is news about the validator, not about the config. D-CS7
    (hold-fix-4277-preflight-fails-closed-and-is-wired-to-the-upgrade): it
    must produce a WARN naming that it could not check — neither a PASS
    (the silent-control defect) nor silence (the same defect, one layer
    up: "we didn't look" reading identically to "everything is fine")."""
    fx = _fixture(_CONFIG, "could_not_ask.json")
    report = _drive_config(monkeypatch, fx)
    assert len(report.checks) == fx["expect"]["checks"]
    check = report.checks[0]
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_config_valid_pair_differs_on_exactly_one_axis():
    good = _fixture(_CONFIG, "known_good.json")
    bad = _fixture(_CONFIG, "known_bad.json")
    assert good["_axis"] == bad["_axis"] == "validator_result.valid"
    assert good["members"] == bad["members"]
    assert good["network"] == bad["network"]
    assert good["validator_result"]["valid"] != bad["validator_result"]["valid"]
    # `issues` is not a second axis: it is empty exactly when `valid` is true,
    # and the control reads it only on the invalid branch.
    assert good["validator_result"]["error"] == bad["validator_result"]["error"] == ""


# ── _check_oc_channel_packages ────────────────────────────────────────────────

_CHANNEL = "health__check_oc_channel_packages"


def _drive_channel(monkeypatch, fx: dict) -> HealthReport:
    from evolve_admin import oc_preflight, upstream_version

    monkeypatch.setattr(
        upstream_version, "installed_package_version", lambda *a, **k: fx["installed_version"]
    )
    monkeypatch.setattr(
        oc_preflight, "channel_packages_stale", lambda home, target: list(fx["stale_packages"])
    )
    monkeypatch.setattr(health, "_bot_home", lambda bot_id: Path("/nonexistent") / bot_id)
    report = HealthReport()
    health._check_oc_channel_packages(report, fx["members"], {})
    return report


def test_channel_packages_known_good_passes(monkeypatch):
    fx = _fixture(_CHANNEL, "known_good.json")
    check = _only(_drive_channel(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["installed_version"] in check.detail


def test_channel_packages_known_bad_warns(monkeypatch):
    fx = _fixture(_CHANNEL, "known_bad.json")
    check = _only(_drive_channel(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_channel_packages_pair_differs_on_exactly_one_axis():
    good = _fixture(_CHANNEL, "known_good.json")
    bad = _fixture(_CHANNEL, "known_bad.json")
    assert good["_axis"] == bad["_axis"] == "stale_packages"
    assert good["members"] == bad["members"]
    assert good["installed_version"] == bad["installed_version"]
    assert good["stale_packages"] == [] and bad["stale_packages"]


# ── _check_oc_version_coherence ───────────────────────────────────────────────

_VERSION = "health__check_oc_version_coherence"


class _State:
    def __init__(self, payload: dict) -> None:
        self.state = payload["state"]
        self.tested = payload["tested"]
        self.failing = payload["failing"]


def _drive_version(monkeypatch, fx: dict) -> HealthReport:
    from evolve_admin import oc_compat, upstream_version

    monkeypatch.setattr(
        upstream_version, "installed_package_version", lambda *a, **k: fx["installed_version"]
    )
    monkeypatch.setattr(
        oc_compat, "validation_state", lambda version, **kw: _State(fx["validation_state"])
    )
    report = HealthReport()
    health._check_oc_version_coherence(report)
    return report


def test_version_coherence_known_good_passes(monkeypatch):
    fx = _fixture(_VERSION, "known_good.json")
    check = _only(_drive_version(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]


def test_version_coherence_known_bad_warns(monkeypatch):
    """The fixture the control was written for: the runtime is installed and
    nobody has validated it. The check must SAY so — an unvalidated runtime
    that reads as a pass is the exact defect."""
    fx = _fixture(_VERSION, "known_bad.json")
    check = _only(_drive_version(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_version_coherence_pair_differs_on_exactly_one_axis():
    good = _fixture(_VERSION, "known_good.json")
    bad = _fixture(_VERSION, "known_bad.json")
    assert good["_axis"] == bad["_axis"] == "validation_state.state"
    assert good["installed_version"] == bad["installed_version"]
    assert good["validation_state"]["state"] != bad["validation_state"]["state"]


# ── _check_oc_preflight_gate_freshness (hold-fix-4392) ─────────────────────────

_GATE = "health__check_oc_preflight_gate_freshness"


def _drive_gate(monkeypatch, fx: dict) -> HealthReport:
    from datetime import datetime, timedelta, timezone

    from evolve_admin import oc_preflight_store, upstream_version
    from evolve_admin.oc_preflight import bot_set_hash as _bsh

    monkeypatch.setattr(
        upstream_version, "installed_package_version", lambda *a, **k: fx["installed_version"]
    )
    entry = fx.get("cached_entry")
    if entry is not None:
        checked_at = (
            datetime.now(timezone.utc) - timedelta(hours=entry["age_hours"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"checked_at": checked_at, "bot_set_hash": _bsh(fx["network"])}
    monkeypatch.setattr(
        oc_preflight_store, "latest_cache_for_installed",
        lambda installed, shared_dir=None: entry,
    )
    report = HealthReport()
    health._check_oc_preflight_gate_freshness(
        report, shared_dir=Path("/nonexistent"), network=fx["network"],
    )
    return report


def test_preflight_gate_known_good_passes(monkeypatch):
    fx = _fixture(_GATE, "known_good.json")
    check = _only(_drive_gate(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]


def test_preflight_gate_known_bad_warns(monkeypatch):
    fx = _fixture(_GATE, "known_bad.json")
    check = _only(_drive_gate(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_preflight_gate_pair_differs_on_exactly_one_axis():
    good = _fixture(_GATE, "known_good.json")
    bad = _fixture(_GATE, "known_bad.json")
    assert good["_axis"] == bad["_axis"] == "cached_entry"
    assert good["installed_version"] == bad["installed_version"]
    assert good["network"] == bad["network"]
    assert good["cached_entry"] is not None and bad["cached_entry"] is None


# ── _check_breaker_notify_path (breaker-notify-survives-openclaw-config- ──────
# validation item 5) ──────────────────────────────────────────────────────────

_NOTIFY = "health__check_breaker_notify_path"


def _drive_notify(monkeypatch, fx: dict) -> HealthReport:
    probe = fx["cli_probe"]
    # The control imports `check_openclaw_cli_can_start` function-locally from
    # .alerts.dispatcher, so patching the module attribute is enough — nothing
    # here runs the real CLI.
    monkeypatch.setattr(
        "evolve_admin.alerts.dispatcher.check_openclaw_cli_can_start",
        lambda: (probe["state"], probe["detail"]),
    )
    report = HealthReport()
    health._check_breaker_notify_path(report)
    return report


def test_breaker_notify_path_known_good_passes(monkeypatch):
    fx = _fixture(_NOTIFY, "known_good.json")
    check = _only(_drive_notify(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]


def test_breaker_notify_path_known_bad_fails(monkeypatch):
    """The one control in this file whose known_bad is a FAIL rather than a
    WARN. Its siblings report a coherence drift the operator can act on at
    leisure; this one reports that the breaker notice path is confirmed dead,
    which is the condition their own quietness would otherwise hide."""
    fx = _fixture(_NOTIFY, "known_bad.json")
    check = _only(_drive_notify(monkeypatch, fx), fx["expect"]["name"])
    assert check.status == fx["expect"]["status"]
    assert fx["expect"]["message_contains"] in check.detail


def test_breaker_notify_path_pair_differs_on_exactly_one_axis():
    good = _fixture(_NOTIFY, "known_good.json")
    bad = _fixture(_NOTIFY, "known_bad.json")
    assert good["_axis"] == bad["_axis"] == "cli_probe.state"
    assert good["cli_probe"]["state"] != bad["cli_probe"]["state"]
    # `detail` is not a second axis: the probe returns a reason only on the
    # broken branch, and the control reads it only there.
    assert good["expect"]["name"] == bad["expect"]["name"]


# ── the registry's own view of these five ───────────────────────────────────────


def test_the_five_controls_are_registered_and_unquarantined():
    """Belt and braces on the ratchet: these four must be discovered as
    controls, must have their pair, and must NOT be sitting in the
    quarantine — the escape hatch exists for the pre-registry backlog, and a
    control written after it may never use it."""
    import importlib.machinery
    import importlib.util
    import sys

    tools = _REPO_ROOT / "tools" / "control-registry"
    loader = importlib.machinery.SourceFileLoader("control_registry_fx", str(tools))
    spec = importlib.util.spec_from_loader("control_registry_fx", loader)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses' _is_type resolves a field's annotation through
    # sys.modules[cls.__module__]; register before exec or Control's
    # definition raises AttributeError on a None module.
    sys.modules["control_registry_fx"] = mod
    loader.exec_module(mod)

    controls = {c.id: c for c in mod.discover_controls()}
    quarantined = mod.load_quarantine()
    for cid in (
        "health:_check_oc_config_valid_under_installed",
        "health:_check_oc_channel_packages",
        "health:_check_oc_version_coherence",
        "health:_check_oc_preflight_gate_freshness",
        "health:_check_breaker_notify_path",
    ):
        assert cid in controls, f"{cid} is not discovered by tools/control-registry"
        assert controls[cid].has_fixtures(), f"{cid} has no known_good/known_bad pair"
        assert cid not in quarantined, f"{cid} must not be quarantined"
