"""test_review_ast_fail_closed.py — the AST security layer must fail CLOSED.

The proposal reviewer runs a static-rule pass (security_rules.json) AND an
AST-analysis pass (review_ast). The AST pass catches the type-spoofed /
obfuscated code the static patterns miss (see test_review_redteam.py). The
AST module is imported lazily; if it cannot be imported the reviewer must NOT
silently degrade to a weaker static-only review and pass proposals — that is a
fail-OPEN hole where a packaging/deploy fault quietly weakens the gate.

These tests pin the fail-CLOSED contract:

  1. A proposal that would otherwise be approved comes back non-approving
     (flagged), carrying the ``ast_analyzer_unavailable`` marker.
  2. A proposal the static rules already reject STILL comes back rejected —
     failing closed must never downgrade a genuine rejection.
  3. When the AST layer IS available (normal case) an ordinarily-approved
     proposal is still approved — the fail-closed branch must not over-broadly
     deny the happy path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_rules():
    import json
    rules = json.loads((_ANALYZER_DIR / "security_rules.json").read_text())
    return (
        rules.get("auto_reject", []),
        rules.get("auto_flag", []),
        rules.get("risk_scoring", {}),
    )


AUTO_REJECT_RULES, AUTO_FLAG_RULES, RISK_SCORING = _load_rules()


def _evaluate(proposal: dict) -> review.SecurityReview:
    return review.evaluate_proposal(
        proposal,
        AUTO_REJECT_RULES,
        AUTO_FLAG_RULES,
        RISK_SCORING,
        reviewer_mode="primary",
    )


def _benign_approvable() -> dict:
    """A proposal no static rule flags/rejects and the AST layer clears —
    it evaluates to ``approved`` when the AST layer is available."""
    return {
        "id": "test-fc-approvable",
        "type": "workflow_change",
        "target_bot": "team-bot-a",
        "confidence": 0.9,
        "proposed_change": {
            "description": "Move the morning briefing to 8am on weekdays",
            "schedule": "0 8 * * 1-5",
        },
    }


def _statically_rejected() -> dict:
    """A proposal the STATIC auto_reject rules reject (no_self_modification —
    a script targeting the evolve/ tree). Static-only, so it rejects even
    with the AST layer offline."""
    return {
        "id": "test-fc-rejected",
        "type": "script_change",
        "target_bot": "team-bot-a",
        "confidence": 0.9,
        "proposed_change": {
            "target_file": "/Users/team-bot-a/.openclaw/workspace/evolve/patch.py",
            "content": "x = 1\n",
        },
    }


@pytest.fixture
def ast_unavailable(monkeypatch):
    """Simulate a review_ast import failure — the exact state the top-of-module
    ``try/except ImportError`` leaves the module in when the file is missing."""
    monkeypatch.setattr(review, "_AST_AVAILABLE", False)
    monkeypatch.setattr(review, "_ast_analyze_proposal", None)


@pytest.fixture
def ast_available():
    """Guard: only run the happy-path assertion when the AST layer really
    imported. If review_ast is genuinely missing from the env, skip rather
    than assert a false negative."""
    if not (review._AST_AVAILABLE and review._ast_analyze_proposal is not None):
        pytest.skip("review_ast not importable in this environment")


# ── 1. Otherwise-approvable proposal is force-flagged when AST is unavailable ──

def test_approvable_proposal_is_flagged_when_ast_unavailable(ast_unavailable):
    review_result = _evaluate(_benign_approvable())

    assert review_result.result != "approved", (
        "AST unavailable must NOT silently approve — that is the fail-OPEN hole"
    )
    assert review_result.result in ("flagged", "rejected"), (
        f"fail-closed outcome must be non-approving; got {review_result.result}"
    )
    # Descriptive marker present on both the flag list and the triggered rules.
    assert any("ast_analyzer_unavailable" in f for f in review_result.flags), (
        f"expected ast_analyzer_unavailable flag; got {review_result.flags}"
    )
    assert "ast_analyzer_unavailable" in {
        r.rule_id for r in review_result.triggered_rules
    }, "expected an ast_analyzer_unavailable triggered rule"


# ── 2. A genuine static rejection is NOT downgraded by the fail-closed branch ──

def test_static_rejection_survives_when_ast_unavailable(ast_unavailable):
    review_result = _evaluate(_statically_rejected())

    assert review_result.result == "rejected", (
        "fail-closed must not downgrade a real static rejection to flagged; "
        f"got {review_result.result} (reason: {review_result.rejection_reason})"
    )


# ── 3. Happy path unchanged — AST available approves the approvable proposal ───

def test_approvable_proposal_still_approved_when_ast_available(ast_available):
    review_result = _evaluate(_benign_approvable())

    assert review_result.result == "approved", (
        "with the AST layer available, an ordinarily-approved proposal must "
        f"still approve; got {review_result.result} "
        f"(flags: {review_result.flags})"
    )
    assert "ast_analyzer_unavailable" not in {
        r.rule_id for r in review_result.triggered_rules
    }, "the fail-closed marker must NOT appear on the happy path"
