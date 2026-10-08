"""tests/test_rsi_store_and_bridge.py — Phase 1 bridge + store tests.

Covers:
  - arbiter.store: write / read / iter / find / move / delete
  - better_engine.proposal_reader: Proposal → Recommendation mapping

Bridge tests import the admin package via a sys.path tweak so they can
exercise the adapter directly without spinning up the web server.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import (  # noqa: E402
    delete_proposal,
    find_proposal,
    iter_proposals,
    load_proposal_file,
    move_proposal,
    proposal_path,
    subdir_for_status,
    write_proposal,
)
from schema.proposal import GuardianAnnotation  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# arbiter.store
# ─────────────────────────────────────────────────────────────────────────────


def test_store_write_and_load_roundtrip(tmp_path):
    p = make_investigation_proposal(bot_id="team_bot_a", problem="gateway down")
    transition(p, "pending", actor="test", reason="seed")
    path = write_proposal(p, tmp_path)
    assert path.exists()
    assert path == proposal_path(tmp_path, p.id, subdir="pending")
    loaded = load_proposal_file(path)
    assert loaded is not None
    assert loaded.id == p.id
    assert loaded.status == "pending"


def test_store_iter_only_requested_subdirs(tmp_path):
    p1 = make_investigation_proposal(bot_id="team_bot_a", problem="first")
    p2 = make_investigation_proposal(bot_id="team_bot_a", problem="second")
    transition(p1, "pending", actor="test")
    transition(p2, "pending", actor="test")
    transition(p2, "snoozed", actor="test", reason="defer")
    p2.snoozed_until = "2026-12-01T00:00:00+00:00"

    write_proposal(p1, tmp_path)
    write_proposal(p2, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert [p.id for p in pending] == [p1.id]
    both = list(iter_proposals(tmp_path, subdirs=("pending", "snoozed")))
    assert {p.id for p in both} == {p1.id, p2.id}


def test_store_find_proposal_across_subdirs(tmp_path):
    p = make_investigation_proposal(bot_id="team_bot_a")
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)
    found = find_proposal(tmp_path, p.id)
    assert found is not None
    prop, _, subdir = found
    assert prop.id == p.id
    assert subdir == "pending"

    # Not found
    assert find_proposal(tmp_path, "no-such-id") is None


def test_store_move_proposal_cleans_source(tmp_path):
    p = make_investigation_proposal(bot_id="team_bot_a")
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()

    transition(p, "dismissed", actor="user")
    move_proposal(p, tmp_path, from_subdir="pending")

    assert not proposal_path(tmp_path, p.id, subdir="pending").exists()
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_store_move_proposal_no_half_state_when_dest_unwritable(tmp_path, monkeypatch):
    """Regression: previously the move wrote to dest first, then unlinked
    src; if the unlink failed (e.g. EACCES from foreign-owned source under
    a sticky-bit dir), the proposal ended up in BOTH subdirs. Now the move
    writes the updated content into src, then ``os.replace(src, dest)``;
    if the replace fails, dest is empty and src holds the new content —
    never both populated.
    """
    import os
    import errno

    p = make_investigation_proposal(bot_id="team_bot_b", problem="x")
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)
    src_path = proposal_path(tmp_path, p.id, subdir="pending")
    dest_path = proposal_path(tmp_path, p.id, subdir="archived")
    assert src_path.exists()
    assert not dest_path.exists()

    # Simulate the replace failing — exactly the dismiss-EACCES situation.
    real_replace = os.replace

    def boom(a, b, *args, **kwargs):
        if str(b) == str(dest_path):
            raise PermissionError(errno.EACCES, "simulated EACCES on rename")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "replace", boom)

    transition(p, "dismissed", actor="user")
    import pytest

    with pytest.raises(PermissionError):
        move_proposal(p, tmp_path, from_subdir="pending")

    # Critical invariant: dest must NOT exist (no half-state).
    assert not dest_path.exists(), (
        "move_proposal must not leave the proposal in two subdirs when the "
        "transition fails"
    )
    # src still exists with the updated (dismissed) content — caller can
    # retry once the underlying permission is fixed.
    assert src_path.exists()


def test_store_write_produces_world_readable_files(tmp_path):
    """Regression: ``_atomic_write_json`` used to leave files at mode
    0o600 (tempfile.mkstemp default). When a proposal written by one
    user (e.g. security_bot applier) needs to be read by another (e.g. evolve
    auto-resolve sweep, admin UI), the read fails with EACCES. Every
    file written through the arbiter store must end up world-readable
    so cross-daemon coordination works regardless of which user wrote
    the source proposal.
    """
    import stat as _stat

    p = make_investigation_proposal(bot_id="any_bot")
    transition(p, "pending", actor="test")
    written_path = write_proposal(p, tmp_path)

    mode = _stat.S_IMODE(written_path.stat().st_mode)
    # Owner rw, group r, other r — 0o644
    assert mode & 0o077 == 0o044, (
        f"proposal file mode {oct(mode)} blocks cross-user reads; "
        "auto-resolve / admin UI / verify won't be able to load "
        "proposals written by other daemons"
    )


def test_store_move_proposal_in_place_when_status_unchanged(tmp_path):
    """If from_subdir == status-derived dest, the call rewrites in place
    (no move). Used by callers that want to refresh content without
    transitioning."""
    p = make_investigation_proposal(bot_id="team_bot_c", problem="initial")
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)

    # Update content without changing status.
    p.problem = "updated"
    move_proposal(p, tmp_path, from_subdir="pending", to_subdir="pending")

    src_path = proposal_path(tmp_path, p.id, subdir="pending")
    assert src_path.exists()
    reloaded = load_proposal_file(src_path)
    assert reloaded.problem == "updated"


def test_store_delete_proposal(tmp_path):
    p = make_investigation_proposal()
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)
    assert delete_proposal(tmp_path, p.id, subdir="pending") is True
    assert delete_proposal(tmp_path, p.id, subdir="pending") is False


def test_subdir_for_status_covers_lifecycle():
    assert subdir_for_status("pending") == "pending"
    assert subdir_for_status("approved_human") == "pending"
    assert subdir_for_status("applied") == "applied"
    assert subdir_for_status("succeeded") == "archived"
    assert subdir_for_status("snoozed") == "snoozed"


def test_store_iter_skips_corrupt_files(tmp_path):
    # Seed a valid one + a garbage one
    p = make_investigation_proposal()
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)

    bad = proposal_path(tmp_path, "corrupt", subdir="pending")
    bad.write_text("not json at all")

    results = list(iter_proposals(tmp_path))
    assert {r.id for r in results} == {p.id}


# ─────────────────────────────────────────────────────────────────────────────
# write_proposal: maintain motivated_proposals backref on linked Signals
# ─────────────────────────────────────────────────────────────────────────────


def _seed_signal(tmp_path, signature: str = "audit:config:bot:sig1"):
    """Seed a firing Signal so write_proposal has a backref target."""
    from signals import store as signals_store
    return signals_store.observe(
        tmp_path,
        signature=signature,
        producer="audit",
        type="config",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="team_bot_a",
        title="seed for backref test",
    )


def test_write_proposal_attaches_backref_on_linked_signal(tmp_path):
    """Per the 2026-06-04 backref-maintenance pass: a Proposal with
    motivating_signals=[sig.id] must result in sig.motivated_proposals
    containing the proposal.id after write_proposal returns. This is
    what lets the UI render observation+action as one row instead of two."""
    from signals import store as signals_store
    sig = _seed_signal(tmp_path)

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.motivating_signals = [sig.id]
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)

    # Reload signal from store — the backref must be on disk, not just memory
    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    reloaded, _path, _subdir = located
    assert p.id in reloaded.motivated_proposals, (
        f"expected proposal {p.id} in signal {sig.id}.motivated_proposals; "
        f"got {reloaded.motivated_proposals}"
    )


def test_write_proposal_backref_is_idempotent(tmp_path):
    """Status transitions re-write a proposal multiple times. The backref
    list must NOT grow with each write — attach_proposal is idempotent."""
    from signals import store as signals_store
    sig = _seed_signal(tmp_path)

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.motivating_signals = [sig.id]
    transition(p, "pending", actor="test")

    # Three writes in a row (e.g. ingest → approve → apply)
    write_proposal(p, tmp_path)
    write_proposal(p, tmp_path)
    write_proposal(p, tmp_path)

    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    reloaded, _path, _subdir = located
    assert reloaded.motivated_proposals.count(p.id) == 1, (
        "backref list must be idempotent — duplicates would bloat the "
        "Signal payload across the proposal's lifecycle"
    )


def test_write_proposal_handles_missing_motivating_signal(tmp_path):
    """A proposal's motivating signal may have been resolved/archived
    between the generator's read and our write. Missing signal must NOT
    raise — the proposal write is authoritative; the backref is best-effort."""
    p = make_investigation_proposal(bot_id="team_bot_a")
    p.motivating_signals = ["nonexistent-signal-id"]
    transition(p, "pending", actor="test")

    # Should not raise
    path = write_proposal(p, tmp_path)
    assert path.exists()


def test_write_proposal_attaches_to_multiple_motivating_signals(tmp_path):
    """A correlator-style generator can list multiple motivating signals
    (e.g. cost_root_cause_correlator names an acute + chronic pair).
    Every linked signal must get the backref."""
    from signals import store as signals_store
    sig_a = _seed_signal(tmp_path, signature="cost_watchdog:spike:bot:1")
    sig_b = _seed_signal(tmp_path, signature="session_economics:cache:bot:1")

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.motivating_signals = [sig_a.id, sig_b.id]
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path)

    for sig_id in (sig_a.id, sig_b.id):
        located = signals_store.find_signal(tmp_path, sig_id)
        assert located is not None
        reloaded, _path, _subdir = located
        assert p.id in reloaded.motivated_proposals


def test_write_proposal_skip_backref_flag_works(tmp_path):
    """Callers doing bulk migrations can pass maintain_signal_backrefs=False
    and the backref isn't touched. Used to avoid re-attaching when the
    backref list will be batch-rebuilt at end of a migration."""
    from signals import store as signals_store
    sig = _seed_signal(tmp_path)

    p = make_investigation_proposal(bot_id="team_bot_a")
    p.motivating_signals = [sig.id]
    transition(p, "pending", actor="test")
    write_proposal(p, tmp_path, maintain_signal_backrefs=False)

    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    reloaded, _path, _subdir = located
    assert reloaded.motivated_proposals == [], (
        "maintain_signal_backrefs=False must skip the backref attach"
    )


# ─────────────────────────────────────────────────────────────────────────────
# better_engine.proposal_reader
# ─────────────────────────────────────────────────────────────────────────────


def _import_reader():
    from evolve_admin.better_engine import proposal_reader  # noqa: WPS433
    return proposal_reader


def test_bridge_skips_audience_none(tmp_path):
    pr = _import_reader()
    p = make_config_patch_proposal(target_path="/tmp/x::a.b", value="v")
    # Default audience = "none" on make_config_patch_proposal
    assert p.approval_audience == "none"
    assert pr.proposal_to_recommendation(p) is None


def test_bridge_maps_core_fields(tmp_path):
    pr = _import_reader()
    p = make_investigation_proposal(
        bot_id="team_bot_a",
        generator_id="sysadmin_watchdog",
        dimension="substrate_health",
        urgency="operational_urgent",
        problem="gateway unreachable",
        audience="pod_operator",
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert rec.id == f"rec_prop_{p.id}"
    assert rec.dedup_key.startswith("arbiter::sysadmin_watchdog::")
    assert rec.source == "generator:sysadmin_watchdog"
    assert rec.scope == "admin"
    assert rec.scope_id == "admin"
    assert rec.dimension == "substrate_health"
    assert rec.urgency == "operational_urgent"
    assert rec.type == "operational"
    assert rec.generator_id == "sysadmin_watchdog"
    assert rec.action_kind == "Investigation"
    assert rec.approval_audience == "pod_operator"
    assert rec.action == "arbiter_act"
    assert rec.action_args == {"proposal_id": p.id}
    # Pre-dimension-weights priority ladder
    assert rec.priority_score >= pr.URGENCY_SCORE["operational_urgent"]


def test_bridge_routes_bot_audience_to_bot_scope():
    pr = _import_reader()
    p = make_investigation_proposal(
        bot_id="ellie",
        audience="bot_primary_user",
        problem="hi there",
    )
    p.conversational_pitch = "Hey! I noticed something. Can I fix it?"
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert rec.scope == "bot"
    assert rec.scope_id == "ellie"
    assert rec.bot_executable is True
    assert rec.member_bot_detail == p.conversational_pitch
    assert rec.member_bot_title  # some short preface


def test_bridge_serializes_guardian_annotations():
    pr = _import_reader()
    p = make_investigation_proposal(audience="pod_operator")
    p.guardian_annotations.append(
        GuardianAnnotation(
            guardian_id="security_warden",
            severity="high",
            reason="touches auth scopes",
        )
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert len(rec.guardian_annotations) == 1
    ann = rec.guardian_annotations[0]
    assert ann["guardian_id"] == "security_warden"
    assert ann["severity"] == "high"
    # Tags should include the warn marker
    assert any(t.startswith("warn:security_warden:") for t in rec.tags)
    # High severity adds a score boost
    base = pr.URGENCY_SCORE[p.urgency]
    assert rec.priority_score > base


def test_bridge_annotation_boost_scales_with_severity():
    pr = _import_reader()

    def mk(sev):
        p = make_investigation_proposal(audience="pod_operator")
        p.guardian_annotations.append(
            GuardianAnnotation(guardian_id="g", severity=sev, reason="r")
        )
        return pr.proposal_to_recommendation(p)

    low = mk("low").priority_score
    medium = mk("medium").priority_score
    high = mk("high").priority_score
    critical = mk("critical").priority_score
    assert low <= medium <= high <= critical


def test_bridge_dimension_to_type_mapping():
    pr = _import_reader()
    for dim, expected in pr.DIMENSION_TO_TYPE.items():
        p = make_investigation_proposal(
            dimension=dim, audience="pod_operator"
        )
        rec = pr.proposal_to_recommendation(p)
        assert rec is not None, f"dimension {dim} produced no rec"
        assert rec.type == expected


def test_bridge_verify_status_derives_from_proposal_status():
    pr = _import_reader()
    # Applied + claim → pending
    p_applied = make_investigation_proposal(audience="pod_operator")
    p_applied.status = "applied"
    rec = pr.proposal_to_recommendation(p_applied)
    assert rec is not None
    assert rec.verify_status == "pending"

    p_ok = make_investigation_proposal(audience="pod_operator")
    p_ok.status = "succeeded"
    rec = pr.proposal_to_recommendation(p_ok)
    assert rec is not None
    assert rec.verify_status == "confirmed"

    p_bad = make_investigation_proposal(audience="pod_operator")
    p_bad.status = "failed_flagged"
    rec = pr.proposal_to_recommendation(p_bad)
    assert rec is not None
    assert rec.verify_status == "refuted"


def test_bridge_adapter_reads_from_disk(tmp_path):
    pr = _import_reader()
    p1 = make_investigation_proposal(audience="pod_operator")
    p2 = make_investigation_proposal(audience="none")  # should be skipped
    transition(p1, "pending", actor="test")
    transition(p2, "pending", actor="test")
    write_proposal(p1, tmp_path)
    write_proposal(p2, tmp_path)

    adapter = pr.ProposalReaderAdapter()
    recs = adapter.generate(tmp_path, {"members": ["team_bot_a"]})
    assert len(recs) == 1
    assert recs[0].generator_id == p1.generator_id


def test_bridge_adapter_returns_empty_when_no_proposals(tmp_path):
    pr = _import_reader()
    adapter = pr.ProposalReaderAdapter()
    recs = adapter.generate(tmp_path, {"members": []})
    assert recs == []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: evo keyword surface polish
# ─────────────────────────────────────────────────────────────────────────────


def test_member_bot_title_prefixes_dimension_word_when_plain():
    pr = _import_reader()
    p = make_investigation_proposal(
        dimension="cost",
        audience="bot_primary_user",
        problem="spending up",
    )
    p.conversational_pitch = "I'm noticing my token bill is climbing — want me to tier down?"
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert rec.member_bot_title.startswith("cost:")


def test_member_bot_title_skips_prefix_for_jargon_dimensions():
    """substrate_health / capability_growth are too jargon-y to prefix."""
    pr = _import_reader()
    p = make_investigation_proposal(
        dimension="substrate_health",
        audience="bot_primary_user",
    )
    p.conversational_pitch = "Something's off with the gateway."
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert ":" not in rec.member_bot_title.split(" ")[0]  # no forced prefix


def test_member_bot_title_doesnt_double_tag_when_already_mentioned():
    pr = _import_reader()
    p = make_investigation_proposal(
        dimension="cost",
        audience="bot_primary_user",
    )
    p.conversational_pitch = "Cost is running higher than usual — want a cheaper tier?"
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    # Should NOT start with "cost: Cost is..." — generator already said it
    assert not rec.member_bot_title.lower().startswith("cost: cost")


def test_member_bot_detail_frames_adjacency():
    pr = _import_reader()
    p = make_investigation_proposal(
        audience="bot_primary_user",
        dimension="capability_growth",
    )
    p.conversational_pitch = "Can I also help with your calendar?"
    p.adjacency_type = "extend_same_cell"
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert "small extension" in rec.member_bot_detail.lower()
    # Pitch itself is preserved
    assert "Can I also help with your calendar?" in rec.member_bot_detail


def test_member_bot_detail_doesnt_repeat_framing_if_generator_wrote_it():
    pr = _import_reader()
    p = make_investigation_proposal(audience="bot_primary_user")
    p.adjacency_type = "extend_same_cell"
    p.conversational_pitch = "This is a small extension of what I already do. Want to try?"
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    # Count only one occurrence
    assert rec.member_bot_detail.lower().count("small extension") == 1


def test_member_bot_detail_inlines_guardian_concern():
    from schema.proposal import GuardianAnnotation  # noqa

    pr = _import_reader()
    p = make_investigation_proposal(audience="bot_primary_user")
    p.conversational_pitch = "Want me to check your Slack for pending asks?"
    p.guardian_annotations.append(
        GuardianAnnotation(
            guardian_id="security_warden",
            severity="high",
            reason="this would widen what I can read in Slack",
        )
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    # Concern is appended after the pitch, in natural phrasing
    assert "concern" in rec.member_bot_detail.lower()
    assert "widen what i can read" in rec.member_bot_detail.lower()
    # No admin jargon leaked through
    assert "severity" not in rec.member_bot_detail.lower()
    assert "security_warden" not in rec.member_bot_detail.lower()


def test_member_bot_detail_ignores_low_severity_annotations():
    from schema.proposal import GuardianAnnotation  # noqa

    pr = _import_reader()
    p = make_investigation_proposal(audience="bot_primary_user")
    p.conversational_pitch = "Want to try a new thing?"
    p.guardian_annotations.append(
        GuardianAnnotation(guardian_id="g", severity="low", reason="minor note")
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert "concern" not in rec.member_bot_detail.lower()
    assert "heads up" not in rec.member_bot_detail.lower()


def test_member_bot_detail_picks_strongest_annotation():
    from schema.proposal import GuardianAnnotation  # noqa

    pr = _import_reader()
    p = make_investigation_proposal(audience="bot_primary_user")
    p.conversational_pitch = "Ok?"
    p.guardian_annotations.append(
        GuardianAnnotation(guardian_id="g1", severity="medium", reason="mild concern")
    )
    p.guardian_annotations.append(
        GuardianAnnotation(guardian_id="g2", severity="critical", reason="big problem")
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None
    assert "big problem" in rec.member_bot_detail.lower()
    assert "mild concern" not in rec.member_bot_detail.lower()


def test_recommendation_roundtrip_preserves_enrichment():
    """Serializing a Recommendation with L1-L6 fields and loading it back
    should preserve every field the bridge populates."""
    pr = _import_reader()
    from evolve_admin.better_engine.model import Recommendation

    p = make_investigation_proposal(audience="pod_operator")
    p.adjacency_type = "adjacent_noun"
    p.guardian_annotations.append(
        GuardianAnnotation(guardian_id="g", severity="medium", reason="r")
    )
    rec = pr.proposal_to_recommendation(p)
    assert rec is not None

    serialized = rec.to_dict()
    rec2 = Recommendation.from_dict(serialized)
    assert rec2.dimension == rec.dimension
    assert rec2.urgency == rec.urgency
    assert rec2.adjacency_type == "adjacent_noun"
    assert rec2.generator_id == rec.generator_id
    assert rec2.action_kind == rec.action_kind
    assert rec2.approval_audience == rec.approval_audience
    assert rec2.guardian_annotations == rec.guardian_annotations
    assert rec2.verify_status == rec.verify_status
