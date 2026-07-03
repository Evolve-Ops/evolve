"""First-audit role routing in the Tier-3 audit runner (decision C).

A just-built app's FIRST Tier-3 audit is provisioning cost — a structural
audit of fresh, to-spec forge output that doesn't need the ``power`` rung.
The runner detects the first audit off the manifest and resolves the pod's
``standard`` role for it; every later (steady-state) audit keeps the bot's
default model. Stated as a ROLE change — no provider/model name appears in
runner logic.

docs/finding-new-bot-activation-cost-2026-06-12.md
docs/roadmap-user-value-2026-06-10.md  (Cost-defaults decision)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_runner  # noqa: E402


# ── _is_first_audit ──────────────────────────────────────────────────────────


def test_is_first_audit_true_when_never_audited() -> None:
    """No last_audit, an empty last_audit, or a blank verified_at all mean
    'never audited' — the same condition _is_tier3_due reports."""
    assert app_audit_runner._is_first_audit({"id": "a"}) is True
    assert app_audit_runner._is_first_audit({"id": "a", "last_audit": {}}) is True
    assert app_audit_runner._is_first_audit(
        {"id": "a", "last_audit": {"verified_at": "   "}}
    ) is True
    assert app_audit_runner._is_first_audit(
        {"id": "a", "last_audit": {"verified_at": None}}
    ) is True


def test_is_first_audit_false_after_verified_at() -> None:
    """Once the first audit stamps last_audit.verified_at, every later audit
    is no longer 'first' and inherits the bot default — steady-state audits
    are never downgraded by this routing."""
    assert app_audit_runner._is_first_audit(
        {"id": "a", "last_audit": {"verified_at": "2026-06-12T00:00:00Z"}}
    ) is False


# ── _resolve_first_audit_model ───────────────────────────────────────────────


def test_resolve_first_audit_model_uses_standard_role(monkeypatch) -> None:
    """The first-audit model resolves the ``standard`` ROLE, not the bot's
    ``power`` default. Captures the tier argument to prove it's the role
    string ``"standard"`` (the abstraction), not a provider/model literal."""
    import evolve_config  # noqa: F401 — ensure module exists for string patch
    import models  # noqa: F401

    seen: dict = {}

    def fake_resolve(tier, config, bot_id=None):
        seen["tier"] = tier
        seen["bot_id"] = bot_id
        return {
            "standard": "anthropic/claude-sonnet-4-6",
            "power": "anthropic/claude-opus-4-6",
        }[tier]

    monkeypatch.setattr("models.resolve_tier", fake_resolve)
    monkeypatch.setattr("evolve_config.load_config", lambda *a, **k: {})

    got = app_audit_runner._resolve_first_audit_model("ledger")
    assert seen["tier"] == "standard"     # resolved via the role abstraction
    assert seen["bot_id"] == "ledger"     # per-bot standard override respected
    assert got == "anthropic/claude-sonnet-4-6"


def test_resolve_first_audit_model_none_on_broken_config(monkeypatch) -> None:
    """Resolution failure ⇒ None, so the dispatch falls back to the bot's
    agent default (today's behavior) — never a hard failure, never a
    hardcoded provider literal."""
    monkeypatch.setitem(sys.modules, "models", None)
    monkeypatch.setitem(sys.modules, "evolve_config", None)
    assert app_audit_runner._resolve_first_audit_model("ledger") is None
