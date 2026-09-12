"""tests/test_ai_optimization_reconcile_all.py — "Reconcile all" bulk button
on the catalog-drift card.

Source-level assertions on ai-optimization.js (the admin SPA has no JS test
harness; the established pattern across packages/admin/tests is to scan the
web JS source — see test_ai_optimization_rank_presentation.py).

Pins:
  1. The drift card renders a "Reconcile all (N bots)" button when more than
     one bot has drift — mirroring the freshness "Apply all" affordance so the
     operator isn't clicking N per-bot Reconcile buttons one at a time.
  2. The bulk handler (_aiReconcileAllCatalog) drives the per-bot reconcile
     (_aiReconcileCatalog) in quiet mode — i.e. it reuses the existing per-bot
     endpoint sequentially as the fallback, rather than depending on a new
     bulk server route.
  3. A confirm dialog summarizing per-bot drift counts gates the bulk action.
  4. Per-bot success/failure is tallied and surfaced (no silent partial fail).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


def test_reconcile_all_button_present_in_drift_card(js: str):
    """The drift render emits a Reconcile-all button wired to the bulk handler."""
    assert "_aiReconcileAllCatalog()" in js, (
        "drift card must expose a bulk reconcile button"
    )
    assert "Reconcile all" in js, "button label missing"
    # Button label names the bot count (mirrors 'Apply All (N)').
    assert re.search(r"Reconcile all \(\$\{botCount\}", js), (
        "Reconcile-all button should show the bot count"
    )


def test_reconcile_all_button_is_gated_to_multi_bot(js: str):
    """Single-bot drift keeps the per-bot button only; the bulk button shows
    when more than one bot has drift (botCount > 1)."""
    m = re.search(r"const reconcileAll = botCount > 1", js)
    assert m, "Reconcile-all button must be gated on botCount > 1"


def test_bulk_handler_exists_and_loops_per_bot_endpoint(js: str):
    """_aiReconcileAllCatalog must reuse the per-bot reconcile sequentially in
    quiet mode — the per-bot-fallback approach, not a new bulk route."""
    m = re.search(
        r"async function _aiReconcileAllCatalog\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiReconcileAllCatalog not found"
    body = m.group(1)
    # Loops the per-bot reconcile call in quiet mode.
    assert "_aiReconcileCatalog(" in body, "must call the per-bot reconcile"
    assert "quiet: true" in body, "per-bot calls in the loop must be quiet"
    # No new bulk server route — it drives the existing per-bot tiers endpoint.
    assert "update-tier-bulk" not in body, (
        "reconcile-all should not piggyback the freshness bulk route"
    )


def test_bulk_handler_confirms_with_per_bot_summary(js: str):
    """A confirm() gates the action and summarizes per-bot drift counts."""
    m = re.search(
        r"async function _aiReconcileAllCatalog\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    body = m.group(1)
    assert "confirmModal(" in body, "bulk reconcile must confirm before acting"
    assert "botSummary" in body, "confirm must summarize per-bot drift counts"


def test_bulk_handler_reports_per_bot_failures(js: str):
    """Partial failures are tallied and surfaced, not swallowed."""
    m = re.search(
        r"async function _aiReconcileAllCatalog\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    body = m.group(1)
    assert "failures" in body and "okCount" in body, (
        "must tally success/failure per bot"
    )
    assert "Failed:" in body, "must surface which bots failed"


def test_per_bot_reconcile_supports_quiet_mode(js: str):
    """_aiReconcileCatalog must accept a quiet option and return a result the
    bulk loop can tally (ok/error), so the loop can suppress per-bot toasts."""
    m = re.search(
        r"async function _aiReconcileCatalog\(([^)]*)\)", js,
    )
    assert m, "_aiReconcileCatalog signature not found"
    assert "opts" in m.group(1), "per-bot reconcile must take an opts arg"
    body_m = re.search(
        r"async function _aiReconcileCatalog\([^)]*\)\s*\{(.*?)\n\}\n",
        js, re.DOTALL,
    )
    body = body_m.group(1)
    assert "opts.quiet" in body, "quiet flag must be read"
    assert "return { ok: true" in body, "must return a tally-able success result"
