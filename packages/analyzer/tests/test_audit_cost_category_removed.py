"""Phase E1 — audit no longer emits cost-category findings.

Regression guard against reintroducing audit_cost(). The audit emitter
used to bundle spend overages under "🔴 *Evolve Security Audit —
CRITICAL Findings*", which mislabeled a usage notice as a security
issue. spend_alert.py is the canonical operator-facing path for
cost.daily_threshold; this test prevents the audit-side duplicate
from coming back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def test_audit_cost_function_is_gone():
    """The audit_cost function is removed in Phase E1. Re-introducing
    it would re-create the audit/spend_alert duplicate that produced
    two operator-facing chat messages per spend overage (third copy
    was cost.py — retired in Phase C, #947)."""
    assert not hasattr(audit, "audit_cost"), (
        "audit.audit_cost was retired in Phase E1 — spend_alert.py owns "
        "cost.daily_threshold notifications"
    )


def test_audit_category_choices_exclude_cost():
    """The --category CLI choice list must not include 'cost' anymore.
    A non-trivial check because the argparse choices list is the
    operator-facing surface for invoking audit_cost manually."""
    import argparse
    import sys as _sys

    captured: dict = {}

    def fake_parse(self, *a, **kw):
        # Inspect the parser's actions to extract the --category choices.
        for action in self._actions:
            if action.dest == "category":
                captured["choices"] = list(action.choices or [])
        # Return a minimal namespace; main() will short-circuit on
        # reset_baselines / network None / etc.
        ns = argparse.Namespace(
            network=None, dry_run=True, bot=None, category=None,
            reset_baselines=False,
        )
        return ns

    # Patch ArgumentParser.parse_args, then trigger audit.main() just
    # long enough to capture the choices. Use a fake sys.exit to escape
    # after parsing.
    orig_parse = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = fake_parse  # type: ignore[assignment]
    try:
        # Force main() to exit before doing real work.
        def fake_load_config(*_a, **_kw):
            raise SystemExit(0)
        orig_load = audit.load_config
        audit.load_config = fake_load_config  # type: ignore[assignment]
        try:
            try:
                audit.main()
            except SystemExit:
                pass
        finally:
            audit.load_config = orig_load
    finally:
        argparse.ArgumentParser.parse_args = orig_parse  # type: ignore[assignment]

    assert "choices" in captured, "could not capture --category choices"
    assert "cost" not in captured["choices"], (
        f"Phase E1: --category 'cost' must be removed from the choices "
        f"list. got: {captured['choices']}"
    )


def test_dispatch_findings_with_no_cost_findings_is_clean(tmp_path, monkeypatch):
    """Sanity check: when the audit pipeline produces zero findings
    (the natural state once audit_cost is gone), the dispatch path
    short-circuits with the 'All clear' branch rather than rendering
    an empty CRITICAL bullet list."""
    # Monkeypatch _send_via_dispatcher so we observe what dispatch_findings
    # decides to send (or not send).
    sent: list = []

    def fake_send(msg, shared_dir, config):
        sent.append(msg)

    monkeypatch.setattr(audit, "_send_via_dispatcher", fake_send)
    # _send_telegram_direct should NOT be called for an all-clear path.
    def fail_direct(*a, **kw):
        raise AssertionError(
            f"_send_telegram_direct must not fire on no-findings path: {a}"
        )
    monkeypatch.setattr(audit, "_send_telegram_direct", fail_direct)

    # No findings → dispatch_findings should print a benign log and not
    # produce a CRITICAL message.
    audit.dispatch_findings(
        findings=[], shared_dir=tmp_path, config={"alerts": {}},
        dry_run=False,
    )
    assert sent == [], (
        f"no findings should not produce any chat dispatch; got {sent}"
    )
