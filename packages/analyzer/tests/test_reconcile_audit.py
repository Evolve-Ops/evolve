"""Tests for ``reconcile_audit`` — the daily Signal producer for
scheduled_actions[] drift.

Covers four shapes:

  1. Spec building (``_spec_for_drift_report``) — pure function. Tests
     the signature key embeds bot + app, the body carries the fix
     command, and missing_in_gallery is handled with the "not
     auto-remediated" framing.

  2. ``collect`` — reconcile_actions is mocked; verify only the
     reportable classifications produce specs.

  3. ``run`` end-to-end against a tmp_path-backed signal store — verify
     signals land in firing/, re-runs with the same drift don't dupe
     (signature dedup), drift cleared on disk → signal moves to
     archived/.

  4. ``main`` (the CLI/daemon entry) — argument parsing + return code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from signals import store as signals_store  # noqa: E402

import reconcile_audit  # noqa: E402
from reconcile_audit import (  # noqa: E402
    PRODUCER,
    SIGNAL_TYPE,
    _spec_for_drift_report,
    collect,
    main,
    run,
)


# ── Spec building ────────────────────────────────────────────────────────────


def _report(bot="atlas", app="atlas-daily-digest", cls="shape_drift",
            **kwargs) -> dict:
    """Default-shaped drift report dict, override fields per test."""
    base = {
        "bot_id":               bot,
        "app_id":               app,
        "pkg_id":               "p-7b26ba5e",
        "classification":       cls,
        "detail":               "test detail",
        "drifted_action_ids":   ["a"],
        "installed_action_ids": ["a"],
        "gallery_action_ids":   ["a"],
    }
    base.update(kwargs)
    return base


def test_spec_signature_embeds_bot_and_app() -> None:
    """Same drift on different bots / different apps gets different
    Signal signatures so the Alerts UI doesn't collapse them. Same
    (bot, app) drift across runs gets the same signature so observe()
    deduplicates."""
    s1 = _spec_for_drift_report(_report(bot="atlas", app="atlas-daily-digest"))
    s2 = _spec_for_drift_report(_report(bot="admin_bot", app="atlas-daily-digest"))
    s3 = _spec_for_drift_report(_report(bot="atlas", app="morning-briefing"))
    s4 = _spec_for_drift_report(_report(bot="atlas", app="atlas-daily-digest"))
    assert s1["signature"] != s2["signature"]
    assert s1["signature"] != s3["signature"]
    assert s1["signature"] == s4["signature"]   # same (bot, app) → same sig


def test_spec_has_remediation_command_in_body() -> None:
    """Operator should not need a doc trip to fix the drift — the
    Signal body must include the apply-actions command."""
    spec = _spec_for_drift_report(_report(cls="shape_drift"))
    assert "apply-actions atlas atlas-daily-digest" in spec["body"]
    assert "--from-gallery" in spec["body"]


def test_spec_missing_in_gallery_says_not_auto_remediated() -> None:
    """Orphan-in-installed drift is ambiguous; the Signal must say so
    instead of pointing at an apply-actions command that would
    delete the orphan."""
    spec = _spec_for_drift_report(_report(cls="missing_in_gallery"))
    assert "not auto-remediated" in spec["body"]
    # No apply-actions command in body when auto-remediation is unsafe.
    assert "apply-actions" not in spec["body"]


def test_spec_severity_and_scope_match_monitor_convention() -> None:
    spec = _spec_for_drift_report(_report())
    assert spec["producer"] == PRODUCER
    assert spec["type"] == SIGNAL_TYPE
    assert spec["severity"] == "warn"
    assert spec["scope"] == "pod"
    assert spec["flavor"] == "maintenance"


def test_spec_truncates_long_drifted_id_list() -> None:
    """Defensive: a huge drift list shouldn't pollute the Alerts UI
    with a 50-line body."""
    spec = _spec_for_drift_report(_report(drifted_action_ids=[f"a{i}" for i in range(20)]))
    body = spec["body"]
    # First 10 listed, then "+N more" line.
    assert "+10 more" in body
    # Last one (a19) shouldn't appear individually.
    assert "`a19`" not in body


def test_spec_details_carry_full_drift_data() -> None:
    """The Alerts UI shows the title + body, but Proposal generators
    and downstream tools read ``details``. Don't truncate there."""
    spec = _spec_for_drift_report(_report(
        drifted_action_ids=[f"a{i}" for i in range(20)],
    ))
    assert len(spec["details"]["drifted_action_ids"]) == 20


# ── collect — filters to reportable classifications ──────────────────────────


def test_collect_only_returns_reportable_classifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ok`` / ``skipped_*`` / ``error`` should NOT produce Signals —
    they're not drift in the actionable sense. Only the three drift
    classifications should round-trip into specs."""
    from evolve_admin.applications.reconcile_actions import ReconcileResult, AppDriftReport

    fake_result = ReconcileResult(reports=[
        AppDriftReport(bot_id="atlas", app_id="ok-app", pkg_id="p",
                       classification="ok"),
        AppDriftReport(bot_id="atlas", app_id="drift-app", pkg_id="p",
                       classification="shape_drift",
                       drifted_action_ids=["a"]),
        AppDriftReport(bot_id="atlas", app_id="missing-app", pkg_id="p",
                       classification="missing_in_installed",
                       drifted_action_ids=["b"]),
        AppDriftReport(bot_id="atlas", app_id="orphan-app", pkg_id="p",
                       classification="missing_in_gallery",
                       drifted_action_ids=["c"]),
        AppDriftReport(bot_id="atlas", app_id="custom", pkg_id="",
                       classification="skipped_no_pkg_id"),
        AppDriftReport(bot_id="atlas", app_id="sideloaded", pkg_id="p-x",
                       classification="skipped_side_loaded"),
        AppDriftReport(bot_id="atlas", app_id="quiet", pkg_id="p",
                       classification="skipped_no_daemon"),
    ])
    monkeypatch.setattr(
        "reconcile_audit.reconcile_actions" if False else
        "evolve_admin.applications.reconcile_actions.reconcile_actions",
        lambda *a, **kw: fake_result,
    )
    specs = collect(tmp_path)
    by_app = {s["details"]["app_id"] for s in specs}
    assert by_app == {"drift-app", "missing-app", "orphan-app"}


# ── run — end-to-end with real tmp_path signal store ────────────────────────


def _stub_reconcile_actions(monkeypatch, reports):
    """Replace reconcile_actions with a stub returning a controllable
    ReconcileResult shape."""
    from evolve_admin.applications.reconcile_actions import (
        ReconcileResult, AppDriftReport,
    )
    result = ReconcileResult(reports=[
        AppDriftReport(**r) for r in reports
    ])
    monkeypatch.setattr(
        "evolve_admin.applications.reconcile_actions.reconcile_actions",
        lambda *a, **kw: result,
    )


def test_run_writes_signals_to_firing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
        {"bot_id": "admin_bot", "app_id": "morning-briefing", "pkg_id": "p",
         "classification": "missing_in_installed", "drifted_action_ids": ["m"]},
    ])

    kept, n_fired, n_resolved = run(tmp_path)
    assert n_fired == 2
    assert n_resolved == 0
    assert len(kept) == 2

    sigs = list(signals_store.iter_active(tmp_path, producer=PRODUCER))
    assert len(sigs) == 2
    apps = {s.details.get("app_id") for s in sigs}
    assert apps == {"atlas-daily-digest", "morning-briefing"}


def test_run_is_idempotent_across_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same drift on two consecutive runs → exactly one Signal in
    firing/ (observe deduplicates by signature)."""
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
    ])
    run(tmp_path)
    run(tmp_path)
    sigs = list(signals_store.iter_active(tmp_path, producer=PRODUCER))
    assert len(sigs) == 1


def test_run_sweep_resolves_when_drift_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run sees drift → Signal fires. Second run sees no drift
    (operator applied the fix) → sweep_resolve archives the Signal."""
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
    ])
    run(tmp_path)
    assert len(list(signals_store.iter_active(tmp_path, producer=PRODUCER))) == 1

    # Drift cleared.
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "ok"},
    ])
    kept, n_fired, n_resolved = run(tmp_path)
    assert n_fired == 0
    assert n_resolved == 1
    assert kept == set()
    assert len(list(signals_store.iter_active(tmp_path, producer=PRODUCER))) == 0


def test_run_partial_resolve_when_one_app_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two drifted apps; operator fixes one. Next run keeps the other
    Signal active and archives just the fixed one."""
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
        {"bot_id": "atlas", "app_id": "morning-briefing", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["y"]},
    ])
    run(tmp_path)
    assert len(list(signals_store.iter_active(tmp_path, producer=PRODUCER))) == 2

    # Operator fixed morning-briefing.
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "atlas-daily-digest", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
        {"bot_id": "atlas", "app_id": "morning-briefing", "pkg_id": "p",
         "classification": "ok"},
    ])
    kept, n_fired, n_resolved = run(tmp_path)
    assert n_fired == 1
    assert n_resolved == 1
    remaining = list(signals_store.iter_active(tmp_path, producer=PRODUCER))
    assert len(remaining) == 1
    assert remaining[0].details["app_id"] == "atlas-daily-digest"


def test_run_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "x", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
    ])
    kept, n_fired, n_resolved = run(tmp_path, dry_run=True)
    assert n_fired == 1
    assert n_resolved == 0
    # Nothing actually written.
    assert len(list(signals_store.iter_active(tmp_path, producer=PRODUCER))) == 0
    out = capsys.readouterr().out
    assert "would_observe" in out


# ── main — daemon entry point ──────────────────────────────────────────────


def test_main_returns_zero_on_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_reconcile_actions(monkeypatch, [])
    rc = main(["--shared-dir", str(tmp_path), "--once"])
    assert rc == 0


def test_main_returns_zero_even_when_drift_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon writes the Signals and exits 0. Exit code is for the
    daemon's launchd liveness, not the drift count — Signals are the
    drift-reporting channel."""
    _stub_reconcile_actions(monkeypatch, [
        {"bot_id": "atlas", "app_id": "x", "pkg_id": "p",
         "classification": "shape_drift", "drifted_action_ids": ["x"]},
    ])
    rc = main(["--shared-dir", str(tmp_path), "--once"])
    assert rc == 0
