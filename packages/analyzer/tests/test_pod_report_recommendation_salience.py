r"""tests/test_pod_report_recommendation_salience.py — the daily Pod Report's
"New recommendations / New findings" selection (RSI Recommendations revival).

Pins the FIX-A salience + FIX-B honest-split behavior of
``pod_report.collect_new_recommendations`` / ``list_new_proposals``:

  1. Selection ranks by the ``urgency`` ladder, not recency — a high-urgency
     item leads even when a lower-urgency item is newer; the top-3 are the 3
     most SALIENT, not the 3 newest.
  2. ``surface == "drift"`` hygiene nitpicks are excluded from BOTH lines
     (the motivating bug: manifest_quality drift led the daily digest). This
     covers the charter-resolution path — manifest_quality leaves the
     per-proposal ``surface`` override None and inherits ``charter.surface =
     drift``, so the report must consult the charter, not just ``p.surface``.
  3. Proposals missing ranking fields default to lowest priority and never
     crash the report (back-compat with pre-urgency proposals).
  4. Items split into "New recommendations" (actionable → /admin/improvements)
     and "New findings" (awareness → Reports → Findings) by the operator-facing
     taxonomy.

All timestamps are fixed ISO strings — the selection is pure string-compare,
so there is no wall-clock coupling (no real ``now`` is ever read).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_report  # noqa: E402
from arbiter import store as arbiter_store  # noqa: E402

# A fixed cutoff; every fake proposal below is created after it so all pass
# the "created since the last run" filter. No real clock is consulted.
_SINCE = "2026-06-19T00:00:00Z"
_ANY_SHARED_DIR = Path("/nonexistent/shared")  # iter_proposals is stubbed


def _fake(**kw) -> SimpleNamespace:
    """A minimal duck-typed stand-in for a Proposal — ``list_new_proposals``
    reads every field via ``getattr``, so a namespace suffices and lets each
    test set exactly the ranking fields it cares about."""
    base = dict(
        id="p",
        bot_id="bot",
        summary="summary",
        created_at="2026-06-20T00:00:00Z",
        urgency="improvement",
        surface="improvement",          # per-proposal override (may be None)
        generator_id="gen",
        action=SimpleNamespace(kind="ConfigPatch"),  # actionable, not FYI
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def collect(monkeypatch):
    """Return a helper that installs a controlled proposal set + charter
    surface map, then runs ``collect_new_recommendations`` and returns its
    ReportLines."""

    def _run(proposals, *, surface_map=None):
        monkeypatch.setattr(
            arbiter_store, "iter_proposals",
            lambda *a, **k: list(proposals),
        )
        monkeypatch.setattr(
            pod_report, "_charter_surface_map",
            lambda: dict(surface_map or {}),
        )
        return pod_report.collect_new_recommendations(_ANY_SHARED_DIR, _SINCE)

    return _run


def _blob(lines) -> str:
    return " || ".join(line.text for line in lines)


# ── FIX A: salience ranking ────────────────────────────────────────────────


def test_high_urgency_outranks_newer_low_urgency(collect):
    """An older operational_urgent item must lead a NEWER hygiene item — the
    selection ranks by urgency, not by recency."""
    urgent = _fake(
        id="urgent", bot_id="admin_bot", summary="restart wedged daemon",
        urgency="operational_urgent", created_at="2026-06-20T08:00:00Z",
    )
    newer_hygiene = _fake(
        id="hyg", bot_id="team_bot_a", summary="tidy AGENTS heading",
        urgency="hygiene", created_at="2026-06-22T09:00:00Z",
    )
    # Feed newest-first to prove the sort, not the input order, decides rank.
    lines = collect([newer_hygiene, urgent])

    assert len(lines) == 1
    text = lines[0].text
    assert text.startswith("New recommendations:")
    assert text.index("restart wedged daemon") < text.index("tidy AGENTS heading")


def test_top3_are_most_salient_not_newest(collect):
    """With >3 actionable items, the top-3 are the 3 most salient. A lone
    high-urgency item that is the OLDEST still makes (and leads) the head;
    the surplus collapses into '+N more'."""
    salient = _fake(
        id="u", bot_id="b_urgent", summary="cost breaker risk",
        urgency="cost_alert", created_at="2026-06-20T00:00:00Z",  # oldest
    )
    newer_hygiene = [
        _fake(id=f"f{i}", bot_id=f"b{i}", summary=f"hyg{i}",
              urgency="hygiene", created_at=f"2026-06-2{i}T00:00:00Z")
        for i in (1, 2, 3)
    ]
    lines = collect(newer_hygiene + [salient])

    assert len(lines) == 1
    text = lines[0].text
    # The salient (oldest) item leads the head despite three newer items.
    assert text.split("New recommendations: ", 1)[1].startswith(
        "b_urgent: cost breaker risk")
    # 4 actionable items → 3 shown + "+1 more".
    assert text.endswith("+1 more")
    # The oldest, most-salient item is NOT pushed out of the head.
    assert "cost breaker risk" in text


# ── FIX A: drift exclusion (charter-resolved) ──────────────────────────────


def test_drift_surface_excluded_from_both_lines(collect):
    """A manifest_quality-shape proposal (per-proposal surface override None,
    charter.surface = drift) is dropped from the daily report entirely — it is
    neither a recommendation nor a finding. Proves the report resolves the
    EFFECTIVE surface from the charter, not just ``p.surface``."""
    rec = _fake(
        id="rec", bot_id="admin_bot", summary="adopt mcp plugin",
        urgency="operational_urgent", surface="improvement",
        created_at="2026-06-20T00:00:00Z",
    )
    drift = _fake(
        id="drift", bot_id="atlas", summary="manifest description is stale",
        urgency="hygiene", surface=None, generator_id="manifest_quality",
        created_at="2026-06-22T00:00:00Z",  # newest — would have led pre-fix
    )
    lines = collect([drift, rec], surface_map={"manifest_quality": "drift"})

    blob = _blob(lines)
    assert "adopt mcp plugin" in blob
    # The drift nitpick appears in NO line (headline or findings).
    assert "manifest" not in blob
    assert all("manifest" not in line.text for line in lines)


# ── FIX A: back-compat (missing ranking fields) ────────────────────────────


def test_missing_urgency_sorts_last(collect):
    """A proposal with ``urgency=None`` ranks below one with a real urgency,
    even when the urgency-less item is newer — and nothing raises."""
    has = _fake(
        id="has", bot_id="admin_bot", summary="cost fix",
        urgency="cost_alert", created_at="2026-06-20T00:00:00Z",
    )
    missing = _fake(
        id="miss", bot_id="team_bot_a", summary="no urgency field",
        urgency=None, created_at="2026-06-22T00:00:00Z",  # newer
    )
    lines = collect([missing, has])

    text = lines[0].text
    assert text.index("cost fix") < text.index("no urgency field")


def test_proposal_missing_all_ranking_fields_does_not_crash(collect):
    """A bare legacy proposal lacking ``urgency`` / ``surface`` / ``action``
    attrs entirely is handled by getattr defaults: effective surface None →
    awareness bucket, lowest salience, no exception."""
    bare = SimpleNamespace(
        id="bare", bot_id="legacy_bot", summary="legacy husk",
        created_at="2026-06-21T00:00:00Z", generator_id="g",
    )  # deliberately no urgency / surface / action attributes
    rec = _fake(
        id="rec", bot_id="admin_bot", summary="modern action",
        surface="improvement", urgency="improvement",
        created_at="2026-06-20T00:00:00Z",
    )
    lines = collect([bare, rec])

    types = {line.signal_type for line in lines}
    assert "new_recommendations" in types  # the modern actionable item
    # The bare item (surface None) lands in awareness, not the headline.
    blob = _blob(lines)
    assert "legacy husk" in blob


# ── FIX B: honest split + deeplinks ────────────────────────────────────────


def test_split_into_recommendations_and_findings(collect):
    """Actionable improvement-surface items render as 'New recommendations'
    (→ /admin/improvements); awareness items — informational observations and
    firing/cleanup-surface anomalies — render as 'New findings' (→ Reports →
    Findings)."""
    rec = _fake(
        id="rec", bot_id="admin_bot", summary="actionable change",
        surface="improvement", urgency="operational_urgent",
        created_at="2026-06-20T00:00:00Z",
        action=SimpleNamespace(kind="ConfigPatch"),
    )
    investigation = _fake(  # improvement-surface but FYI → awareness
        id="obs", bot_id="team_bot_a", summary="look into maintenance ratio",
        surface="improvement", urgency="improvement",
        created_at="2026-06-21T00:00:00Z",
        action=SimpleNamespace(kind="Investigation"),
    )
    anomaly = _fake(  # firing-surface → awareness
        id="fire", bot_id="team_bot_b", summary="session token outlier",
        surface="firing", urgency="cost_alert",
        created_at="2026-06-22T00:00:00Z",
    )
    lines = collect([rec, investigation, anomaly])

    by_type = {line.signal_type: line for line in lines}
    assert set(by_type) == {"new_recommendations", "new_findings"}

    rec_line = by_type["new_recommendations"]
    find_line = by_type["new_findings"]
    assert rec_line.deeplink == "/admin/improvements"
    assert find_line.deeplink == "/admin/reports?subtab=alerts"

    assert "actionable change" in rec_line.text
    assert "actionable change" not in find_line.text
    # Both awareness items land in findings.
    assert "look into maintenance ratio" in find_line.text
    assert "session token outlier" in find_line.text


def test_no_new_proposals_returns_no_lines(collect):
    """Silence means nothing changed — an empty proposal set yields no
    ReportLines at all."""
    assert collect([]) == []


def test_only_drift_yields_no_lines(collect):
    """If the only new proposals are drift hygiene, the daily report stays
    silent rather than emitting an empty findings line."""
    drift = _fake(
        id="d", bot_id="atlas", summary="files array empty",
        surface=None, generator_id="manifest_quality", urgency="hygiene",
        created_at="2026-06-21T00:00:00Z",
    )
    assert collect([drift], surface_map={"manifest_quality": "drift"}) == []
