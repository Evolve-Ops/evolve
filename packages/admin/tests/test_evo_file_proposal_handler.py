"""Tests for the `evo file-proposal` handler — Channel B routing.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

The handler lands a Proposal in the operator's queue when a non-evo
bot's in-situ repair conversation produces a structured outcome.
Mirrors the proposal-write contract from
``app_changes._handle_flag`` (PR #2332) but carries a structured
``--content`` payload rather than free-form user text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
# schema + arbiter live under packages/analyzer/ — the handler imports
# them lazily; tests need them on the path so the lazy import resolves.
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))


def _handler_module():
    from evolve_admin.evo.handlers import file_proposal
    return file_proposal


def _network_for(shared_dir: Path) -> dict:
    return {"pod": {"shared_dir": str(shared_dir)}}


# ── Grammar errors surface to the user ──────────────────────────────────────


def test_missing_flags_returns_usage_error(tmp_path):
    handler = _handler_module()
    result = handler.render(
        role="primary", bot_id="evolve",
        args="--on-behalf-of team-bot-c",
        network=_network_for(tmp_path),
    )
    body = result.direct_send_message
    assert "Usage" in body
    assert "--app" in body or "missing" in body


def test_unknown_action_returns_usage_error(tmp_path):
    handler = _handler_module()
    result = handler.render(
        role="primary", bot_id="evolve",
        args='--on-behalf-of team-bot-c --app j --action delete_everything --content {}',
        network=_network_for(tmp_path),
    )
    body = result.direct_send_message
    assert "delete_everything" in body or "Usage" in body


def test_malformed_content_returns_usage_error(tmp_path):
    handler = _handler_module()
    result = handler.render(
        role="primary", bot_id="evolve",
        args=("--on-behalf-of team-bot-c --app j --action mark_resolved "
              "--content not-json"),
        network=_network_for(tmp_path),
    )
    body = result.direct_send_message
    assert "content" in body.lower() or "JSON" in body


# ── Happy path: Proposal lands in the arbiter store ─────────────────────────


def test_proposal_lands_in_pending_dir(tmp_path):
    """Hand-rolled positional args with quoted JSON. The handler must
    write a single pending Proposal under {shared_dir}/proposals/pending/."""
    handler = _handler_module()
    content = {"signature": "abc123def4567890", "rationale": "intentional"}
    args = (
        f'--on-behalf-of team-bot-c --app journal '
        f'--action mark_resolved --content \'{json.dumps(content)}\''
    )
    handler.render(
        role="primary", bot_id="evolve",
        args=args, network=_network_for(tmp_path),
    )
    pending = tmp_path / "proposals" / "pending"
    files = list(pending.glob("*.json")) if pending.exists() else []
    assert len(files) == 1, (
        f"expected 1 pending proposal, found {len(files)} — "
        f"shared_dir contents: {list(tmp_path.rglob('*.json'))}"
    )
    proposal = json.loads(files[0].read_text())
    # The proposal's bot_id is the on-behalf-of target — the bot the
    # repair conversation is about, not whoever's gateway hosted the
    # cross-bot evo call.
    assert proposal["bot_id"] == "team-bot-c"
    assert proposal["generator_id"] == "evo_file_proposal"
    assert proposal["status"] == "pending"
    # Audience is locked to pod_operator regardless of caller role.
    assert proposal["approval_audience"] == "pod_operator"
    # Investigation action — the operator reads the context, the
    # repair-apply path doesn't auto-mutate.
    assert proposal["action"]["kind"] == "Investigation"
    # Provenance carries on_behalf_of, sender_bot_id, app, action so
    # the operator queue can group and route.
    signals = proposal["provenance"]["signals"]
    assert signals["on_behalf_of"] == "team-bot-c"
    assert signals["sender_bot_id"] == "evolve"
    assert signals["app_id"] == "journal"
    assert signals["action"] == "mark_resolved"


def test_proposal_id_surfaces_in_reply(tmp_path):
    """The acknowledgement message must include the Proposal id so the
    user sees confirmation and the operator can correlate."""
    handler = _handler_module()
    content = {"signature": "0123456789abcdef"}
    args = (
        f'--on-behalf-of team-bot-c --app j --action mark_resolved '
        f'--content \'{json.dumps(content)}\''
    )
    result = handler.render(
        role="primary", bot_id="evolve",
        args=args, network=_network_for(tmp_path),
    )
    body = result.direct_send_message
    files = list((tmp_path / "proposals" / "pending").glob("*.json"))
    assert len(files) == 1
    proposal_id = json.loads(files[0].read_text())["id"]
    assert proposal_id in body
    assert "Proposal" in body


def test_payload_content_appears_in_investigation_context(tmp_path):
    """Operator needs to read the structured payload — it lands in the
    Investigation context block as pretty-printed JSON."""
    handler = _handler_module()
    content = {
        "field": "success_criteria.observable_outcomes",
        "after": ["new_outcome"],
        "rationale": "user agreed to this",
    }
    args = (
        f'--on-behalf-of team-bot-c --app j --action propose_field_edit '
        f'--content \'{json.dumps(content)}\''
    )
    handler.render(
        role="primary", bot_id="evolve",
        args=args, network=_network_for(tmp_path),
    )
    files = list((tmp_path / "proposals" / "pending").glob("*.json"))
    proposal = json.loads(files[0].read_text())
    context = proposal["action"]["context"]
    assert "propose_field_edit" in context
    assert "success_criteria.observable_outcomes" in context
    assert "user agreed to this" in context


# ── Cross-bot path: a team / personal bot (not evolve) routes through evo ─


def test_non_evo_bot_can_file_proposal(tmp_path):
    """Per the registry, file-proposal is `available_to=ALL` — any bot
    (team-bot-a / team-bot-c / personal-bot / etc.) can invoke it via
    the cross-bot `evo` mechanism. The proposal is owned by the
    on-behalf-of bot regardless of which gateway hosted the call."""
    handler = _handler_module()
    content = {"reason": "no test makes sense for this app"}
    args = (
        f'--on-behalf-of team-bot-c --app journal '
        f'--action propose_test_exemption '
        f'--content \'{json.dumps(content)}\''
    )
    # secondary role on team-bot-c's own gateway — file-proposal is open
    # to all roles, so this must succeed.
    handler.render(
        role="secondary", bot_id="team-bot-c",
        args=args, network=_network_for(tmp_path),
    )
    files = list((tmp_path / "proposals" / "pending").glob("*.json"))
    assert len(files) == 1
    proposal = json.loads(files[0].read_text())
    assert proposal["bot_id"] == "team-bot-c"
    assert proposal["provenance"]["signals"]["sender_bot_id"] == "team-bot-c"
    # Audience stays pod_operator even when the caller is secondary —
    # the handler doesn't let secondaries promote audience.
    assert proposal["approval_audience"] == "pod_operator"


# ── Subcommand registry wiring ──────────────────────────────────────────────


def test_file_proposal_is_registered_in_evo_grammar():
    """The handler is only useful if the subcommand registry routes
    `evo file-proposal …` to it. Regression-guards a future rename or
    accidental removal."""
    from evolve_admin.evo import subcommands
    names = {sc.name: sc for sc in subcommands._REGISTRY}
    assert "file-proposal" in names
    sc = names["file-proposal"]
    assert sc.handler == (
        "evolve_admin.evo.handlers.file_proposal:render"
    )
    # Audience: ALL (anyone may FILE; operator approves).
    assert "admin" in sc.available_to
    assert "primary" in sc.available_to
    assert "secondary" in sc.available_to
