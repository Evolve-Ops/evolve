"""tests/test_arbiter_dismissals.py — Phase A.5 dismissal suppression store.

Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md §"Decline buttons".

The store at ``{shared_dir}/proposals/dismissed_signatures.jsonl`` is
append-only. Each dismiss writes a new entry; each lift writes a new
entry; ``is_suppressed`` resolves the latest non-lifted, non-expired
entry per (signature, bot_id) tuple.

These tests pin:
  1. Round-trip — record, is_suppressed, lift, no longer suppressed
  2. Per-bot scoping — dismiss on one bot doesn't suppress on another
  3. TTL expiry — expired entries no longer suppress
  4. Permanent (no TTL) entries stay active indefinitely
  5. Instance-scope dismissals key off proposal_id, not signature
  6. iter_active surfaces only currently-active entries (one per key)
  7. Malformed JSONL lines are skipped without crashing
  8. signature_for_proposal helper picks the right (key, scope) tuple
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_ANALYZER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER))

from arbiter import dismissals  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ts(year=2026, month=6, day=4, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# record + is_suppressed round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_record_then_is_suppressed_returns_true(tmp_path):
    """Happy path: record a dismissal, the signature is suppressed."""
    sig = "cache_ttl_tuner:bot-a:cacheRetention_too_short"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", scope="kind",
    )
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-a")


def test_is_suppressed_false_for_unrecorded_signature(tmp_path):
    """A signature that's never been dismissed isn't suppressed."""
    assert not dismissals.is_suppressed(
        tmp_path, signature="not_dismissed", bot_id="bot-a",
    )


def test_is_suppressed_false_for_empty_signature(tmp_path):
    """An empty signature can't be suppressed — defensive default
    so a generator-side bug emitting None doesn't accidentally
    silence the whole queue."""
    assert not dismissals.is_suppressed(
        tmp_path, signature="", bot_id="bot-a",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot scoping
# ─────────────────────────────────────────────────────────────────────────────


def test_dismissal_is_per_bot_by_default(tmp_path):
    """Dismissing on bot-a doesn't suppress for bot-b — operators
    might decide one bot's cache TTL is fine but another's isn't."""
    sig = "cache_ttl_tuner:per_bot_test"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", scope="kind",
    )
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-a")
    assert not dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-b",
    )


def test_pod_wide_dismissal_suppresses_all_bots(tmp_path):
    """A dismissal recorded with bot_id=None is pod-wide — suppresses
    for every bot. Used when the operator picks 'Dismiss for all bots'."""
    sig = "platform_finding:pod_wide"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id=None, scope="kind",
    )
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-a")
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-b")
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id=None)


# ─────────────────────────────────────────────────────────────────────────────
# TTL behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_dismissal_expires_after_ttl(tmp_path):
    """After the 90-day TTL passes, the suppression lifts. The spec
    rationale: 'you said no in June; if the same thing is true in
    September, ask again.'"""
    sig = "ttl_test_signature"
    # Record a dismissal at a fixed point in time.
    base = _ts(2026, 6, 4)
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", ttl_days=90,
    )

    # Verify the JSONL entry has the expected expires_at — we need
    # to read it back to confirm record_dismissal computed it correctly.
    store = tmp_path / "proposals" / "dismissed_signatures.jsonl"
    entry = json.loads(store.read_text().strip().split("\n")[-1])
    assert entry["ttl_days"] == 90
    assert entry["expires_at"] is not None

    # At now: active. At 91 days from the recorded dismissed_at: expired.
    dismissed_at = datetime.fromisoformat(entry["dismissed_at"])
    assert dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-a", at=dismissed_at,
    )
    assert not dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-a",
        at=dismissed_at + timedelta(days=91),
    )


def test_dismissal_permanent_never_expires(tmp_path):
    """ttl_days=None means permanent — no expires_at, stays active
    indefinitely."""
    sig = "permanent_suppression"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", ttl_days=None,
    )
    store = tmp_path / "proposals" / "dismissed_signatures.jsonl"
    entry = json.loads(store.read_text().strip().split("\n")[-1])
    assert entry["expires_at"] is None
    assert entry["ttl_days"] is None
    # Still suppressed years later.
    assert dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-a",
        at=_ts(2099, 1, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lift
# ─────────────────────────────────────────────────────────────────────────────


def test_lift_cancels_active_suppression(tmp_path):
    sig = "lift_test"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", scope="kind",
    )
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-a")
    result = dismissals.lift_dismissal(tmp_path, key=sig, bot_id="bot-a")
    assert result is not None, "lift should return the lift entry"
    assert not dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-a",
    )


def test_lift_returns_none_when_nothing_suppressed(tmp_path):
    """Lifting an empty suppression list is a no-op that returns
    None — the caller can show 'nothing was suppressed' rather than
    silently appearing to have done something."""
    result = dismissals.lift_dismissal(
        tmp_path, key="never_dismissed", bot_id="bot-a",
    )
    assert result is None


def test_relift_after_re_dismiss(tmp_path):
    """After a lift, if the operator re-dismisses, the suppression
    is active again. After another lift, inactive again. Order
    matters; latest wins."""
    sig = "relift_test"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a",
    )
    dismissals.lift_dismissal(tmp_path, key=sig, bot_id="bot-a")
    assert not dismissals.is_suppressed(
        tmp_path, signature=sig, bot_id="bot-a",
    )
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a",
    )
    assert dismissals.is_suppressed(tmp_path, signature=sig, bot_id="bot-a")


# ─────────────────────────────────────────────────────────────────────────────
# Instance-scope
# ─────────────────────────────────────────────────────────────────────────────


def test_instance_scope_keys_off_proposal_id(tmp_path):
    """instance-scope dismissals suppress a specific proposal id,
    not future findings with the same signature. is_suppressed
    (which checks signature) won't see them — they only match via
    the proposal:<id> key."""
    pid = "abc-123"
    dismissals.record_dismissal(
        tmp_path,
        signature="ignored",
        bot_id="bot-a",
        scope="instance",
        proposal_id=pid,
    )
    # Reading the entry back confirms the key was rewritten.
    entries = list(dismissals._read_jsonl(
        tmp_path / "proposals" / "dismissed_signatures.jsonl",
    ))
    assert entries[0]["key"] == f"proposal:{pid}"
    assert entries[0]["scope"] == "instance"

    # is_suppressed checking the original signature returns False —
    # instance scope doesn't suppress future findings of that kind.
    assert not dismissals.is_suppressed(
        tmp_path, signature="ignored", bot_id="bot-a",
    )


def test_instance_scope_requires_proposal_id(tmp_path):
    """Passing scope='instance' without proposal_id is a programming
    error — raise ValueError, don't write a useless entry."""
    with pytest.raises(ValueError):
        dismissals.record_dismissal(
            tmp_path, signature="x", bot_id="bot-a", scope="instance",
        )


def test_kind_scope_requires_signature(tmp_path):
    """scope='kind' with empty signature is a programming error —
    a kind-scoped dismissal needs a real signature to key off."""
    with pytest.raises(ValueError):
        dismissals.record_dismissal(
            tmp_path, signature="", bot_id="bot-a", scope="kind",
        )


# ─────────────────────────────────────────────────────────────────────────────
# iter_active for the UI
# ─────────────────────────────────────────────────────────────────────────────


def test_iter_active_returns_latest_per_key(tmp_path):
    """Multiple writes to the same (signature, bot_id) — iter_active
    surfaces only the latest entry. Old re-dismissals aren't
    re-counted as separate suppressions."""
    sig = "iter_test"
    # Two entries for the same (key, bot) — second wins.
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", rationale="first",
    )
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", rationale="second",
    )
    active = list(dismissals.iter_active(tmp_path))
    assert len(active) == 1
    assert active[0]["rationale"] == "second"


def test_iter_active_skips_lifted_entries(tmp_path):
    """A lift cancels the suppression — iter_active should not
    surface lifted entries (the UI shouldn't show them as active)."""
    sig = "iter_lifted"
    dismissals.record_dismissal(tmp_path, signature=sig, bot_id="bot-a")
    dismissals.lift_dismissal(tmp_path, key=sig, bot_id="bot-a")
    active = list(dismissals.iter_active(tmp_path))
    assert active == []


def test_iter_active_skips_expired_entries(tmp_path):
    """An expired entry isn't active — iter_active should skip it
    at the given timestamp."""
    sig = "iter_expired"
    dismissals.record_dismissal(
        tmp_path, signature=sig, bot_id="bot-a", ttl_days=1,
    )
    # Now: active. 2 days from now: expired (default TTL was 1 day).
    active_now = list(dismissals.iter_active(tmp_path))
    assert len(active_now) == 1
    active_later = list(dismissals.iter_active(
        tmp_path, at=_utc_now_plus(days=2),
    ))
    assert active_later == []


def _utc_now_plus(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


# ─────────────────────────────────────────────────────────────────────────────
# Malformed lines
# ─────────────────────────────────────────────────────────────────────────────


def test_read_skips_malformed_lines(tmp_path):
    """A partial-write or hand-edit shouldn't break reads."""
    store = tmp_path / "proposals" / "dismissed_signatures.jsonl"
    store.parent.mkdir(parents=True)
    store.write_text(
        json.dumps({
            "key": "good", "signature": "good", "proposal_id": None,
            "bot_id": "bot-a", "scope": "kind",
            "dismissed_at": "2026-06-04T12:00:00+00:00",
            "expires_at": None, "ttl_days": None,
            "rationale": "", "lifted_at": None,
        }) + "\n"
        "{this is not json}\n"
        "\n"  # blank line ok
    )
    # Reading must yield only the good entry.
    out = list(dismissals._read_jsonl(store))
    assert len(out) == 1
    assert out[0]["key"] == "good"
    # is_suppressed still works.
    assert dismissals.is_suppressed(
        tmp_path, signature="good", bot_id="bot-a",
    )


# ─────────────────────────────────────────────────────────────────────────────
# signature_for_proposal helper
# ─────────────────────────────────────────────────────────────────────────────


def test_signature_for_proposal_uses_dismiss_signature_when_set():
    """The helper picks the explicit dismiss_signature when set."""
    p = SimpleNamespace(
        dismiss_signature="cache_ttl_tuner:bot-a:foo",
        dismiss_scope="kind",
    )
    sig, scope = dismissals.signature_for_proposal(p)
    assert sig == "cache_ttl_tuner:bot-a:foo"
    assert scope == "kind"


def test_signature_for_proposal_returns_instance_when_no_signature():
    """No dismiss_signature → instance scope (the caller passes
    proposal_id to record_dismissal)."""
    p = SimpleNamespace(dismiss_signature=None, dismiss_scope="kind")
    sig, scope = dismissals.signature_for_proposal(p)
    assert sig == ""
    assert scope == "instance"


def test_signature_for_proposal_honors_explicit_instance_scope():
    """Even with a signature set, dismiss_scope='instance' forces
    instance scope. Generators that emit per-finding signatures but
    want each one to be individually dismissed (rare) opt in this way."""
    p = SimpleNamespace(
        dismiss_signature="explicit_sig",
        dismiss_scope="instance",
    )
    sig, scope = dismissals.signature_for_proposal(p)
    assert sig == ""
    assert scope == "instance"


def test_signature_for_proposal_defensive_for_pre_phase_a_objects():
    """Proposals from before Phase A have no dismiss_* attrs — the
    helper must not crash, must return instance scope."""
    p = SimpleNamespace()  # nothing set
    sig, scope = dismissals.signature_for_proposal(p)
    assert sig == ""
    assert scope == "instance"


# ─────────────────────────────────────────────────────────────────────────────
# preload_suppressed_signatures
# ─────────────────────────────────────────────────────────────────────────────


def test_preload_suppressed_returns_empty_when_store_missing(tmp_path):
    """No dismissals file → empty set, not an error."""
    assert dismissals.preload_suppressed_signatures(tmp_path, "team_bot_a") == set()


def test_preload_suppressed_returns_empty_when_shared_dir_none():
    """``shared_dir=None`` — the test-hook callers pass this when the
    runner hasn't wired a shared dir yet. Must short-circuit to empty
    without raising."""
    assert dismissals.preload_suppressed_signatures(None, "team_bot_a") == set()


def test_preload_suppressed_includes_per_bot_entries(tmp_path):
    """A per-bot dismiss for the queried bot appears in the result."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="team_bot_a",
        scope="kind",
    )
    out = dismissals.preload_suppressed_signatures(tmp_path, "team_bot_a")
    assert out == {"generator_x:finding_a"}


def test_preload_suppressed_excludes_other_bots_entries(tmp_path):
    """A per-bot dismiss for a different bot does NOT appear when we
    query the queried bot — the bot-scoping contract every generator
    used to enforce by hand."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="ellie",
        scope="kind",
    )
    out = dismissals.preload_suppressed_signatures(tmp_path, "team_bot_a")
    assert out == set()


def test_preload_suppressed_includes_pod_wide_entries(tmp_path):
    """A pod-wide dismiss (bot_id=None recorded) matches every bot."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id=None,
        scope="kind",
    )
    for bot in ("team_bot_a", "ellie", "team_bot_c"):
        out = dismissals.preload_suppressed_signatures(tmp_path, bot)
        assert "generator_x:finding_a" in out


def test_preload_suppressed_pod_wide_caller_gets_everything(tmp_path):
    """When the caller passes ``bot_id=None`` (a pod-wide generator
    like evolve_watchdog), the result includes every kind-scoped
    entry regardless of which bot it was recorded for. This matches
    the prior pod-wide ``_filter_dismissed`` behavior in those
    generators."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="team_bot_a",
        scope="kind",
    )
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_b",
        bot_id="ellie",
        scope="kind",
    )
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_c",
        bot_id=None,
        scope="kind",
    )
    out = dismissals.preload_suppressed_signatures(tmp_path, None)
    assert out == {
        "generator_x:finding_a",
        "generator_x:finding_b",
        "generator_x:finding_c",
    }


def test_preload_suppressed_skips_instance_scope_entries(tmp_path):
    """Instance-scope dismissals can't suppress kind-wide. The store
    records them with an empty signature; the preload helper must
    skip those so they don't pollute the kind-gate set."""
    dismissals.record_dismissal(
        tmp_path,
        signature="",  # instance scope — empty signature
        bot_id="team_bot_a",
        scope="instance",
        proposal_id="prop-1",
    )
    out = dismissals.preload_suppressed_signatures(tmp_path, "team_bot_a")
    assert out == set()


def test_preload_suppressed_excludes_lifted_entries(tmp_path):
    """A dismissal that was later lifted no longer appears."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="team_bot_a",
        scope="kind",
    )
    dismissals.lift_dismissal(
        tmp_path,
        key="generator_x:finding_a",
        bot_id="team_bot_a",
    )
    out = dismissals.preload_suppressed_signatures(tmp_path, "team_bot_a")
    assert out == set()


def test_filter_dismissed_drops_suppressed_proposals(tmp_path):
    """The filter wrapper drops proposals whose dismiss_signature is
    in the active set, keeps the rest."""
    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="team_bot_a",
        scope="kind",
    )
    proposals = [
        SimpleNamespace(dismiss_signature="generator_x:finding_a"),
        SimpleNamespace(dismiss_signature="generator_x:finding_b"),
    ]
    out = dismissals.filter_dismissed(proposals, tmp_path, "team_bot_a")
    assert [p.dismiss_signature for p in out] == ["generator_x:finding_b"]


def test_filter_dismissed_empty_input_returns_empty_without_store_read(tmp_path):
    """The convenience wrapper short-circuits on an empty proposal
    list — avoids a wasted JSONL read."""
    assert dismissals.filter_dismissed([], tmp_path, "team_bot_a") == []


def test_filter_dismissed_no_suppression_returns_copy(tmp_path):
    """Empty suppression set returns the input as-is (shallow copy
    so caller mutations don't reach into the source list)."""
    proposals = [
        SimpleNamespace(dismiss_signature="x:1"),
        SimpleNamespace(dismiss_signature="x:2"),
    ]
    out = dismissals.filter_dismissed(proposals, tmp_path, "team_bot_a")
    assert out == proposals
    assert out is not proposals  # shallow copy, not the same list


def test_preload_suppressed_excludes_expired_entries(tmp_path, monkeypatch):
    """An expired TTL drops the entry from the active set. ``at`` is
    a test hook for time-pinning — we mock the active set's clock so
    the recorded expiry (real-time ``now + ttl_days``) is past."""
    from datetime import timedelta

    dismissals.record_dismissal(
        tmp_path,
        signature="generator_x:finding_a",
        bot_id="team_bot_a",
        scope="kind",
        ttl_days=7,
    )
    # Look 30 days past *real* now — well past the 7-day TTL.
    future = dismissals._utc_now() + timedelta(days=30)
    out = dismissals.preload_suppressed_signatures(
        tmp_path, "team_bot_a", at=future,
    )
    assert "generator_x:finding_a" not in out
