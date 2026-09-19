"""tests/test_oc_runtime_controls.py — the registered fixture pairs for the
three OpenClaw runtime-coherence controls (D-CS7).

`tests/fixtures/controls/health__check_oc_*/` carries a `known_good.json`
and a `known_bad.json` for each of `_check_oc_config_valid_under_installed`,
`_check_oc_channel_packages` and `_check_oc_version_coherence`. THIS FILE is
what replays them; without it the pair is inert JSON that satisfies
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


def test_config_valid_could_not_ask_says_nothing(monkeypatch):
    """The third state, and the one the docstring is about: a validator that
    could not run is news about the validator. It must produce NO check —
    neither a PASS (which would be the silent-control defect) nor a WARN
    (which would blame the config for the validator's absence)."""
    fx = _fixture(_CONFIG, "could_not_ask.json")
    report = _drive_config(monkeypatch, fx)
    assert report.checks == [], f"expected silence, got {report.checks}"


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


# ── the registry's own view of these three ────────────────────────────────────


def test_the_three_controls_are_registered_and_unquarantined():
    """Belt and braces on the ratchet: these three must be discovered as
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
    ):
        assert cid in controls, f"{cid} is not discovered by tools/control-registry"
        assert controls[cid].has_fixtures(), f"{cid} has no known_good/known_bad pair"
        assert cid not in quarantined, f"{cid} must not be quarantined"
