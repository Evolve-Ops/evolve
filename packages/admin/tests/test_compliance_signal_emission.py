"""tests/test_compliance_signal_emission.py — Compliance scan → Signal store.

Verifies that ``scan_compliance``'s ``_emit_compliance_signals`` helper
rolls up issues by issue_type and emits ONE Signal per (bot, issue_type)
carrying ``details.items=[...]`` — not one Signal per issue. A bot with
41 ``missing_required_field`` findings produces 1 Signal, not 41.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).resolve().parents[1]
_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest  # noqa: E402

from evolve_admin.applications.scanner import (  # noqa: E402
    _emit_compliance_signals,
    _compliance_signal_severity,
    _COMPLIANCE_PRODUCER,
)
from signals import store as signals_store  # noqa: E402


# ── Severity mapping ──────────────────────────────────────────────────────────


def test_severity_error_maps_to_alert():
    assert _compliance_signal_severity({"severity": "error"}) == "alert"


def test_severity_warning_maps_to_warn():
    assert _compliance_signal_severity({"severity": "warning"}) == "warn"


def test_severity_missing_defaults_to_info():
    """Pre-2026-06-04 this collapsed to ``warn``. The quality-control
    pass added an explicit ``info`` tier so hygiene findings (missing
    description, etc.) emit at the info level rather than at warn —
    info-tier signals don't crowd the Alerts page's default view."""
    assert _compliance_signal_severity({}) == "info"


def test_severity_info_maps_to_info():
    assert _compliance_signal_severity({"severity": "info"}) == "info"


# ── _emit_compliance_signals: rollup behavior ────────────────────────────────


def test_emit_rolls_up_same_type_into_one_signal(tmp_path):
    """A bot with 41 missing_required_field issues produces ONE signal."""
    issues = [
        {"app_id": f"app-{i}", "issue_type": "missing_required_field",
         "severity": "error",
         "message": f"Required field 'description' is missing or empty"}
        for i in range(41)
    ]
    kept = _emit_compliance_signals(tmp_path, "admin_bot", issues)
    assert len(kept) == 1  # one (bot, issue_type) signature

    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert len(fired) == 1
    s = fired[0]
    assert s.type == "missing_required_field"
    # All 41 items live in details.items
    assert len(s.details["items"]) == 41
    assert s.details["item_count"] == 41
    assert s.details["bot_id"] == "admin_bot"
    assert s.details["issue_type"] == "missing_required_field"
    # Each item carries app_id + message (rolled-up from per-issue fields)
    for item in s.details["items"]:
        assert "app_id" in item
        assert "message" in item


def test_emit_separate_signal_per_issue_type(tmp_path):
    issues = [
        {"app_id": "app-a", "issue_type": "stale", "severity": "warning",
         "message": "Last reviewed 100 days ago"},
        {"app_id": "app-b", "issue_type": "test_failing", "severity": "error",
         "message": "Last test exited 1"},
        {"app_id": None, "issue_type": "unregistered_script", "severity": "warning",
         "message": "Unregistered: ops/foo.py", "path": "ops/foo.py"},
    ]
    kept = _emit_compliance_signals(tmp_path, "admin_bot", issues)
    assert len(kept) == 3

    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    by_type = {s.type: s for s in fired}
    assert set(by_type.keys()) == {"stale", "test_failing", "unregistered_script"}
    for s in fired:
        assert s.bot_id == "admin_bot"
        assert s.scope == "bot"
        assert len(s.details["items"]) == 1  # each type has one item here


def test_emit_severity_escalates_to_alert_when_any_item_is_error(tmp_path):
    """A rollup with at least one ``error`` item lands as ``alert`` severity."""
    issues = [
        {"app_id": "app-a", "issue_type": "missing_required_field",
         "severity": "warning", "message": "warn msg"},
        {"app_id": "app-b", "issue_type": "missing_required_field",
         "severity": "error", "message": "err msg"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert len(fired) == 1
    assert fired[0].severity == "alert"


def test_emit_severity_warn_when_no_errors(tmp_path):
    issues = [
        {"app_id": "x", "issue_type": "stale", "severity": "warning",
         "message": "stale"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert fired[0].severity == "warn"


def test_emit_severity_info_when_only_info_items(tmp_path):
    """Missing-description findings now emit as ``info`` severity (not
    alert/warn) so they don't crowd the Alerts page. The 2026-06-03
    quality-control review caught these firing as red alerts across
    long-tail discovered apps — hygiene noise, not security risk. See
    scanner.py::_HYGIENE_FIELDS."""
    issues = [
        {"app_id": "app-a", "issue_type": "missing_required_field",
         "severity": "info",
         "message": "Required field 'description' is missing or empty"},
        {"app_id": "app-b", "issue_type": "missing_required_field",
         "severity": "info",
         "message": "Required field 'description' is missing or empty"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert fired[0].severity == "info"
    assert fired[0].details["item_count"] == 2


def test_emit_severity_error_still_dominates_mixed_with_info(tmp_path):
    """One ``error`` item still escalates the rollup to alert, even if
    every other item is info. The hygiene demotion is per-item — a real
    schema gap mixed in with cosmetic ones doesn't get silently
    swallowed."""
    issues = [
        {"app_id": "app-a", "issue_type": "missing_required_field",
         "severity": "info",
         "message": "Required field 'description' is missing or empty"},
        {"app_id": "app-b", "issue_type": "missing_required_field",
         "severity": "error",
         "message": "Required field 'id' is missing or empty"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert fired[0].severity == "alert"


def test_emit_multi_item_title_summarizes_count(tmp_path):
    issues = [
        {"app_id": f"app-{i}", "issue_type": "stale", "severity": "warning",
         "message": f"app-{i} stale"}
        for i in range(7)
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert "7" in fired[0].title and "stale" in fired[0].title


def test_emit_single_item_title_uses_message(tmp_path):
    issues = [
        {"app_id": "app", "issue_type": "stale", "severity": "warning",
         "message": "the actual message"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert fired[0].title == "the actual message"


def test_emit_item_includes_path_only_when_set(tmp_path):
    """Item dict drops ``"path": None`` so rolled-up app-scoped issues
    don't carry useless null fields."""
    issues = [
        {"app_id": "app-a", "issue_type": "stale", "severity": "warning",
         "message": "stale"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", issues)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    item = fired[0].details["items"][0]
    assert "path" not in item
    assert "cron" not in item
    assert item["app_id"] == "app-a"


def test_emit_signatures_are_per_bot(tmp_path):
    """Two bots with the same issue type produce distinct signatures."""
    issue = {"app_id": "shared-app", "issue_type": "stale", "severity": "warning",
             "message": "stale"}
    kept_a = _emit_compliance_signals(tmp_path, "admin_bot", [issue])
    kept_b = _emit_compliance_signals(tmp_path, "team_bot_c", [issue])
    assert kept_a.isdisjoint(kept_b)


def test_emit_signatures_are_stable_across_runs(tmp_path):
    """Same bot, same issue_type → same signature → one signal, not two."""
    issue = {"app_id": "app", "issue_type": "stale", "severity": "warning",
             "message": "stale"}
    kept_first = _emit_compliance_signals(tmp_path, "admin_bot", [issue])
    kept_second = _emit_compliance_signals(tmp_path, "admin_bot", [issue])
    assert kept_first == kept_second
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert len(fired) == 1


def test_emit_rerun_with_different_items_replaces_items_list(tmp_path):
    """Re-observing with new items overwrites the prior items[] (not append).

    The Signal store's ``observe()`` merges new ``details`` into the existing
    payload via ``dict.update``, so passing the new items list under the same
    key replaces it wholesale — which is what we want, since each scan
    reports the full current set."""
    first_run = [
        {"app_id": "a", "issue_type": "stale", "severity": "warning", "message": "a"},
        {"app_id": "b", "issue_type": "stale", "severity": "warning", "message": "b"},
        {"app_id": "c", "issue_type": "stale", "severity": "warning", "message": "c"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", first_run)
    # Second run: only one of the three is still stale
    second_run = [
        {"app_id": "b", "issue_type": "stale", "severity": "warning", "message": "b"},
    ]
    _emit_compliance_signals(tmp_path, "admin_bot", second_run)
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot", state="firing"
    ))
    assert len(fired) == 1
    assert len(fired[0].details["items"]) == 1
    assert fired[0].details["items"][0]["app_id"] == "b"
    assert fired[0].details["item_count"] == 1


def test_emit_empty_issues_returns_empty_set(tmp_path):
    kept = _emit_compliance_signals(tmp_path, "admin_bot", [])
    assert kept == set()
    fired = list(signals_store.iter_active(
        tmp_path, producer=_COMPLIANCE_PRODUCER, bot_id="admin_bot"
    ))
    assert fired == []
