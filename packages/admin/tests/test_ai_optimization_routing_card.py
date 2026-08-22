"""tests/test_ai_optimization_routing_card.py — routing-rules card rework.

The card is organized by WHO STARTED THE SESSION (the objective trigger
axis), not by the content classifier's labels:

  Conversations  — humans messaging the bot; picker = the operator default
                   role (userTierOverride.defaultTier, spec-user-tier-control
                   Phase A) that replaces the old misleading "Standard fixed"
                   badge (the router ABSTAINS for these sessions; the bot
                   default model is the Standard rung head by deploy
                   convention, not a router decision).
  Scheduled      — cron + heartbeats (trigger-anchored `background`).
  Internal work  — Evolve's own scaffolding subagents (trigger-anchored
                   `maintenance`).

Content-based maintenance downgrades of human chats are a separate opt-in
(routing.classifierDowngrade, default off — enforced router-side in
ModelRouter, covered by packages/plugin/tests/modelRouter.classifierDowngrade
.test.mjs). The old Ambiguous row (a router no-op) is gone; its stored value
is passed through on save so the wholesale routing write can't clobber it.

The admin SPA has no JS test harness; the established pattern is to pin UI
behaviour by asserting on ai-optimization.js *source strings*.
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


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def _card_region(js: str) -> str:
    code = _strip_comments(js)
    start = code.find("function _aiRenderRouting(")
    end = code.find("function _aiComputeFallbackList(")
    assert start != -1 and end != -1
    return code[start:end]


# ── 1. Trigger-axis rows; classifier-label rows retired ───────────────────────

def test_rows_are_trigger_shaped(js: str):
    region = _card_region(js)
    assert "Conversations" in region
    assert "Scheduled" in region
    assert "Internal work" in region
    # The old content-label rows are gone.
    assert "Ambiguous" not in region
    assert "ai-route-ambiguousTier" not in region
    # No "fixed" lock badge — Conversations is a real picker now.
    assert ">fixed<" not in region


def test_conversations_picker_is_role_shaped_with_auto(js: str):
    region = _card_region(js)
    assert "ai-route-defaultRole" in region
    # "auto" = router abstains (bot default); the three classifier roles
    # are the only other options (max is pull-only and must not appear).
    assert "Auto — bot default" in region
    assert '"max"' not in region and "'max'" not in region


# ── 2. Save paths ─────────────────────────────────────────────────────────────

def test_save_writes_routing_and_conversations_default(js: str):
    region = _card_region(js)
    # Routing block keeps its endpoint...
    assert "/api/admin/config/' + _aiBot + '/routing" in region
    # ...and the Conversations default rides the Phase-A endpoint,
    # role value in `defaultTier`.
    assert "/api/admin/config/' + _aiBot + '/user-tier-override" in region
    assert "defaultTier: chosen" in region


def test_conversations_default_written_only_on_change(js: str):
    region = _card_region(js)
    # A routine routing save must not churn evolve-tiers.json / audit log
    # with a no-op userTierOverride write.
    assert "chosen !== prior" in region


def test_classifier_downgrade_is_saved_as_real_bool(js: str):
    region = _card_region(js)
    assert "ai-route-classifier-downgrade" in region
    # Saved into the routing block as a genuine bool (matches the router's
    # bool-only parse; a truthy string must never enable it).
    assert "classifierDowngrade: document.getElementById('ai-route-classifier-downgrade')?.checked === true" in region


def test_stored_ambiguous_tier_passes_through_untouched(js: str):
    region = _card_region(js)
    # The row is gone but the wholesale routing PUT would clobber a stored
    # value — pin the passthrough.
    assert "ambiguousTier: (_aiBotConfig.routing || {}).ambiguousTier ?? null" in region


# ── 3. Model-column honesty ───────────────────────────────────────────────────

def test_conversations_auto_shows_standard_rung_model(js: str):
    region = _card_region(js)
    # "auto" renders the Standard rung head (the bot default by deploy
    # convention) rather than a dash — the operator sees what actually runs.
    assert "_AI_ROUTE_ROLE_TO_TIER[convRole] || 'tier2'" in region
