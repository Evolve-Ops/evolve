"""tests/test_still_motivated_and_sweep_proposals — stale-proposal invalidation.

Two halves of the stale-proposal fix (PR for the Codex report symptom):

  1. ``arbiter.store.sweep_resolve_proposals`` — the proposal analogue of
     ``signals.store.sweep_resolve``. Producer-side: at the end of each
     generator cycle, archive pending/snoozed proposals owned by this
     generator whose fingerprint wasn't re-emitted. Hoisted out of the
     inline loop in ``generator_runner`` so the mechanism is reusable
     and discoverable.

  2. ``arbiter.still_motivated.is_still_motivated`` — defense-in-depth.
     The home briefing path (and other surfaces that read proposals
     straight off disk) run this on each proposal before exposing it.
     If the condition has demonstrably cleared, the proposal is skipped
     and archived in place so the next read agrees.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.still_motivated import (  # noqa: E402
    archive_stale,
    is_still_motivated,
)
from arbiter.store import (  # noqa: E402
    find_proposal,
    sweep_resolve_proposals,
    write_proposal,
)
from schema.signal import Signal, new_signal_id  # noqa: E402
from signals import store as signals_store  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _seed_pending(
    shared_dir: Path,
    *,
    bot_id: str = "team_bot_a",
    generator_id: str = "test_sensor",
    target_path: str | None = None,
):
    """Write a pending ConfigPatch proposal for (gen, bot)."""
    p = make_config_patch_proposal(
        target_path=target_path or f"/tmp/{bot_id}/test.json::flag",
        bot_id=bot_id,
        generator_id=generator_id,
        claim_metric=f"test.{generator_id}.metric",
    )
    p.trigger_observations = [f"test:{bot_id}"]
    transition(p, "pending", actor="arbiter")
    write_proposal(p, shared_dir)
    return p


def _make_signal(*, state: str, signature: str, sid: str | None = None) -> Signal:
    sig = Signal(
        id=sid or new_signal_id(),
        signature=signature,
        producer="test",
        type="test_signal",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="team_bot_a",
        title="t",
        body="b",
    )
    sig.state = state  # type: ignore[assignment]
    return sig


def _write_signal(shared_dir: Path, sig: Signal) -> Signal:
    subdir = {
        "firing": "firing",
        "snoozed": "snoozed",
        "resolved": "archived",
        "dismissed": "archived",
    }[sig.state]
    signals_store.write_signal(sig, shared_dir, subdir=subdir)  # type: ignore[arg-type]
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# sweep_resolve_proposals
# ─────────────────────────────────────────────────────────────────────────────


def test_sweep_archives_silent_per_bot_proposal_as_resolved_externally(tmp_path):
    """Per-bot generator goes silent for a bot → its proposal archives."""
    seeded = _seed_pending(tmp_path)

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="test_sensor",
        emissions_by_bot={"team_bot_a": set()},
        visited_bots={"team_bot_a"},
        valid_bot_ids={"team_bot_a"},
        per_bot=True,
    )

    assert archived == 1
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    proposal, _, subdir = located
    assert subdir == "archived"
    assert proposal.status == "resolved_externally"
    last = proposal.history[-1]
    assert last.to_status == "resolved_externally"


def test_sweep_preserves_proposal_when_fingerprint_re_emitted(tmp_path):
    """Same fingerprint re-emitted this cycle → proposal stays pending."""
    seeded = _seed_pending(tmp_path)
    fp = compute_fingerprint(seeded)

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="test_sensor",
        emissions_by_bot={"team_bot_a": {fp}},
        visited_bots={"team_bot_a"},
        valid_bot_ids={"team_bot_a"},
        per_bot=True,
    )

    assert archived == 0
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    assert located[2] == "pending"


def test_sweep_skips_proposal_for_bot_not_visited_this_cycle(tmp_path):
    """Bot is in the pod but the runner couldn't build a context for it
    this cycle (factory failed). Sweep must leave the proposal alone —
    we have no observation to act on."""
    seeded = _seed_pending(tmp_path)

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="test_sensor",
        emissions_by_bot={},
        visited_bots=set(),  # didn't visit team_bot_a
        valid_bot_ids={"team_bot_a"},
        per_bot=True,
    )

    assert archived == 0
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    assert located[2] == "pending"


def test_sweep_supersedes_proposal_for_bot_no_longer_in_pod(tmp_path):
    """Proposal's bot_id has left pod membership → archive as superseded."""
    seeded = _seed_pending(tmp_path, bot_id="ex_bot")

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="test_sensor",
        emissions_by_bot={},
        visited_bots={"team_bot_a"},
        valid_bot_ids={"team_bot_a"},
        per_bot=True,
    )

    assert archived == 1
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    proposal, _, subdir = located
    assert subdir == "archived"
    assert proposal.status == "superseded"


def test_sweep_ignores_other_generators_proposals(tmp_path):
    """Sweep is scoped by generator_id."""
    other = _seed_pending(tmp_path, generator_id="other_sensor")

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="test_sensor",
        emissions_by_bot={},
        visited_bots={"team_bot_a"},
        valid_bot_ids={"team_bot_a"},
        per_bot=True,
    )

    assert archived == 0
    located = find_proposal(tmp_path, other.id)
    assert located is not None
    assert located[2] == "pending"


def test_sweep_pod_wide_generator_archives_silent_proposal(tmp_path):
    """``per_bot=False`` skips the membership-supersede branch — silent
    fingerprints still archive."""
    p = make_investigation_proposal(
        bot_id="<pod>",
        generator_id="pod_sensor",
    )
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="pod_sensor",
        emissions_by_bot={"<pod>": set()},
        visited_bots={"<pod>"},
        valid_bot_ids=None,
        per_bot=False,
    )

    assert archived == 1
    located = find_proposal(tmp_path, p.id)
    assert located is not None
    assert located[2] == "archived"


# ─────────────────────────────────────────────────────────────────────────────
# is_still_motivated — layer 1: motivating signals
# ─────────────────────────────────────────────────────────────────────────────


def test_still_motivated_false_when_all_motivating_signals_resolved(tmp_path):
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:1"))
    s2 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:2"))
    p = _seed_pending(tmp_path)
    p.motivating_signals = [s1.id, s2.id]
    write_proposal(p, tmp_path)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is False


def test_still_motivated_true_when_any_motivating_signal_firing(tmp_path):
    firing = _write_signal(tmp_path, _make_signal(state="firing", signature="s:1"))
    resolved = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:2"))
    p = _seed_pending(tmp_path)
    p.motivating_signals = [firing.id, resolved.id]
    write_proposal(p, tmp_path)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is True


def test_still_motivated_missing_signal_treated_as_inactive(tmp_path):
    """Retention may prune the archived signal file. A motivating link
    that no longer exists on disk is definitionally stale."""
    p = _seed_pending(tmp_path)
    p.motivating_signals = ["sig-that-was-pruned"]
    write_proposal(p, tmp_path)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is False


# ─────────────────────────────────────────────────────────────────────────────
# is_still_motivated — layer 2: claim re-probe
# ─────────────────────────────────────────────────────────────────────────────


def _register_metric(name: str, value: float):
    """Register a single-shot metric resolver returning ``value``."""
    from metrics.registry import (
        MetricSpec,
        MetricValue,
        clear_for_test,
        register,
        unregister,
    )

    # Avoid stomping other registered metrics — but we do want a fresh
    # binding for this name.
    unregister(name)
    spec = MetricSpec(
        name=name,
        description="t",
        unit="",
        source="test",
    )

    def _resolver(_bot_id: str, _as_of: datetime) -> MetricValue:
        return MetricValue(value=value, confidence=1.0, source_note="test")

    register(spec, _resolver)


def test_still_motivated_false_when_claim_target_already_met_upward(tmp_path):
    """direction=up, baseline=0, magnitude=1 → target ≥ 1. Live=1.0 clears."""
    p = _seed_pending(tmp_path)
    metric_name = p.claim.metric  # populated by make_config_patch_proposal
    _register_metric(metric_name, value=1.0)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is False


def test_still_motivated_true_when_claim_target_not_met(tmp_path):
    p = _seed_pending(tmp_path)
    _register_metric(p.claim.metric, value=0.0)  # below the up-target

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is True


def test_still_motivated_none_when_metric_unregistered(tmp_path):
    """Unknown metric → can't decide → caller falls back to surfacing."""
    p = _seed_pending(tmp_path)
    from metrics.registry import unregister
    unregister(p.claim.metric)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is None


# ─────────────────────────────────────────────────────────────────────────────
# is_still_motivated — layer 3: no opinion
# ─────────────────────────────────────────────────────────────────────────────


def test_still_motivated_none_for_investigation_with_no_signals_or_claim(tmp_path):
    """Investigation proposals have no claim and (typically) no
    motivating_signals. The layer returns None — caller surfaces."""
    p = make_investigation_proposal(generator_id="evo_lookup")
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)

    verdict = is_still_motivated(p, tmp_path)
    assert verdict is None


# ─────────────────────────────────────────────────────────────────────────────
# archive_stale
# ─────────────────────────────────────────────────────────────────────────────


def test_archive_stale_moves_pending_to_archived_resolved_externally(tmp_path):
    p = _seed_pending(tmp_path)

    ok = archive_stale(p, tmp_path, reason="test", actor="unit_test")
    assert ok is True

    located = find_proposal(tmp_path, p.id)
    assert located is not None
    found, _, subdir = located
    assert subdir == "archived"
    assert found.status == "resolved_externally"
    assert found.history[-1].actor == "unit_test"


def test_archive_stale_returns_false_when_proposal_missing(tmp_path):
    """Already-archived proposal is a no-op rather than an exception."""
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    # NOTE: never written to disk.

    ok = archive_stale(p, tmp_path, reason="test")
    assert ok is False
