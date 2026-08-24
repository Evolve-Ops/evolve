"""First-audit model plumbing in app_audit_tier3 (decision C).

A just-built app's FIRST Tier-3 audit runs on the standard role rather than
inheriting the bot's power default. The runner resolves the model and passes
it to run_tier3_for_app, which threads it to BOTH stage dispatches (3a
discovery + 3b triage) via the per-dispatch ``--model`` flag.

Kept in its own file so the surgical bite-C change doesn't have to disturb
(and re-lint) the legacy test_app_audit_tier3.py.

internal/finding-new-bot-activation-cost-2026-06-12.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_tier3 import (  # noqa: E402
    _build_audit_agent_cmd,
    run_tier3_for_app,
)


# ── _build_audit_agent_cmd — pure argv construction ─────────────────────────


def test_build_audit_agent_cmd_omits_model_when_none() -> None:
    """No model ⇒ no --model flag — openclaw uses the bot's agent default.
    This is every recurring (non-first) audit's path."""
    cmd = _build_audit_agent_cmd(
        binary="/opt/homebrew/bin/openclaw", body="hi",
        timeout_s=30, session_id="sess-1", model=None,
    )
    assert "--model" not in cmd


def test_build_audit_agent_cmd_emits_model_when_set() -> None:
    """First-audit dispatch pins --model to the resolved standard-role id,
    without disturbing the other required flags."""
    cmd = _build_audit_agent_cmd(
        binary="/opt/homebrew/bin/openclaw", body="hi",
        timeout_s=30, session_id="sess-1", model="anthropic/claude-sonnet-4-6",
    )
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "anthropic/claude-sonnet-4-6"
    for required in ("--local", "--agent", "--json", "--timeout", "--message", "--session-id"):
        assert required in cmd, f"missing required flag: {required}"


# ── run_tier3_for_app threads model to both stages ──────────────────────────


def test_run_tier3_for_app_threads_model_to_both_stages(tmp_path: Path) -> None:
    """run_tier3_for_app(model=…) must reach BOTH stage 3a discovery and
    stage 3b triage — the standard-role pin can't be dropped between stages."""
    seen_models: list = []
    stage_texts = iter([
        json.dumps([{
            "obs_id": "o1", "category": "drift", "severity": "major",
            "description": "d", "evidence": ["scripts/x.py"],
        }]),
        json.dumps([{"obs_id": "o1", "outcome": "propose", "rationale": "r"}]),
    ])

    def _fake(system, user, *, timeout_s, openclaw_bin=None, model=None, **_extra):
        seen_models.append(model)
        try:
            return next(stage_texts), 10, ""
        except StopIteration:
            return "", 0, "exhausted"

    with patch("app_audit_tier3._dispatch_via_oc", side_effect=_fake):
        run_tier3_for_app(
            manifest={"id": "journal"}, workspace=tmp_path, bot_id="ledger",
            audit_run_id="r1", full_audit=False,
            model="anthropic/claude-sonnet-4-6",
        )

    assert seen_models == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
    ]


def test_run_tier3_for_app_default_model_is_none(tmp_path: Path) -> None:
    """Backwards-compat: callers that don't pass model (every recurring
    audit) still dispatch with model=None — inherit the bot default."""
    seen_models: list = []

    def _fake(system, user, *, timeout_s, openclaw_bin=None, model=None, **_extra):
        seen_models.append(model)
        return "[]", 0, ""  # empty observations → triage skipped

    with patch("app_audit_tier3._dispatch_via_oc", side_effect=_fake):
        run_tier3_for_app(
            manifest={"id": "journal"}, workspace=tmp_path, bot_id="ledger",
            audit_run_id="r1", full_audit=False,
        )

    assert seen_models == [None]
