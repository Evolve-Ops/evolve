"""Tests for scanner Pass D — discoverability backfill.

Pass D fills two manifest fields when they're empty:

  * ``usage.model`` — inferred from manifest shape (scheduled_actions /
    crons → "scheduled", event_triggers → "event-driven", else
    "user-initiated").

  * ``usage.trigger_recognition.hint_words`` — materialized from the
    union of explicit hint_words + capability_tags + session_keywords
    when explicit hints are empty AND the union meets the
    discoverability floor.

These tests are pure unit tests over the helpers; the full pipeline
behavior is covered by the existing piped_scan tests in
test_scanner_backfill.py and would re-exercise the patch loop without
adding signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── _infer_usage_model ───────────────────────────────────────────────────────


def test_infer_model_scheduled_actions_marks_scheduled():
    assert _scanner._infer_usage_model({
        "id": "x",
        "scheduled_actions": [{"id": "morning"}],
    }) == "scheduled"


def test_infer_model_crons_marks_scheduled():
    assert _scanner._infer_usage_model({
        "id": "x",
        "crons": [{"schedule": "0 * * * *", "script": "x.py"}],
    }) == "scheduled"


def test_infer_model_event_triggers_marks_event_driven():
    assert _scanner._infer_usage_model({
        "id": "x",
        "event_triggers": [{"event": "incoming_message"}],
    }) == "event-driven"


def test_infer_model_default_is_user_initiated():
    """Empty manifest (or one with no scheduling/event signal) is
    user-initiated. The structural verifier treats empty model AND
    'user-initiated' as the same permissive default — Pass D just makes
    the implicit explicit so the renderer can show 'When to invoke'.
    """
    assert _scanner._infer_usage_model({"id": "x"}) == "user-initiated"


def test_infer_model_meta_installer_is_user_initiated():
    """A meta-installer manifest (deps only, no scheduled work) is
    user-initiated. The discoverability check skips it for the no_cli
    finding via _looks_like_meta_installer, but it still needs a model
    value for the renderer."""
    assert _scanner._infer_usage_model({
        "id": "ea-pack",
        "app_dependencies": ["a", "b", "c"],
    }) == "user-initiated"


def test_infer_model_scheduled_wins_over_event_triggers():
    """Order matters: if a manifest has both scheduled_actions and
    event_triggers, scheduled wins. This matches how the v16+ runner
    tends to materialize hybrid apps."""
    assert _scanner._infer_usage_model({
        "id": "x",
        "scheduled_actions": [{"id": "morning"}],
        "event_triggers": [{"event": "incoming_message"}],
    }) == "scheduled"


def test_infer_model_non_dict_returns_empty():
    assert _scanner._infer_usage_model("not a dict") == ""  # type: ignore[arg-type]
    assert _scanner._infer_usage_model(None) == ""  # type: ignore[arg-type]


# ── _hint_words_union ────────────────────────────────────────────────────────


def test_hint_words_union_combines_all_three_sources():
    out = _scanner._hint_words_union({
        "usage": {"trigger_recognition": {"hint_words": ["alpha"]}},
        "capability_tags": ["beta", "gamma"],
        "session_keywords": ["delta"],
    })
    # Explicit first, then tags, then keywords. Dedup preserves first
    # occurrence.
    assert out == ["alpha", "beta", "gamma", "delta"]


def test_hint_words_union_dedupes_across_sources():
    out = _scanner._hint_words_union({
        "usage": {"trigger_recognition": {"hint_words": ["shared"]}},
        "capability_tags": ["shared", "unique"],
        "session_keywords": ["shared"],
    })
    assert out == ["shared", "unique"]


def test_hint_words_union_strips_and_skips_empty():
    out = _scanner._hint_words_union({
        "capability_tags": ["  trim  ", "", "  ", "keep"],
    })
    assert out == ["trim", "keep"]


def test_hint_words_union_tolerates_missing_sources():
    assert _scanner._hint_words_union({}) == []


def test_hint_words_union_tolerates_non_list_inputs():
    """Defensive against schema drift / hand-edited manifests."""
    out = _scanner._hint_words_union({
        "usage": "not-a-dict",
        "capability_tags": "not-a-list",
        "session_keywords": ["valid"],
    })
    assert out == ["valid"]


# ── _apply_discoverability_backfill ──────────────────────────────────────────


def test_backfill_writes_inferred_model_when_missing():
    data = {"id": "x", "scheduled_actions": [{"id": "morning"}]}
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is True
    assert data["usage"]["model"] == "scheduled"


def test_backfill_does_not_clobber_explicit_model():
    data = {
        "id": "x",
        "scheduled_actions": [{"id": "morning"}],
        "usage": {"model": "user-initiated"},  # operator-set
    }
    # No change because model is already set (even though scheduled would
    # be the inferred value).
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is False
    assert data["usage"]["model"] == "user-initiated"


def test_backfill_treats_blank_model_as_empty():
    """Whitespace-only model is the same as missing — backfill should fill."""
    data = {"id": "x", "usage": {"model": "   "}}
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is True
    assert data["usage"]["model"] == "user-initiated"


def test_backfill_materializes_hint_words_union_when_explicit_empty():
    """Explicit hint_words empty, but the capability_tags +
    session_keywords union has enough — write it back to explicit."""
    data = {
        "id": "x",
        "capability_tags": ["alpha", "beta"],
        "session_keywords": ["gamma"],
    }
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is True
    assert data["usage"]["trigger_recognition"]["hint_words"] == [
        "alpha", "beta", "gamma",
    ]


def test_backfill_skips_hint_words_when_union_below_floor():
    """Union of 2 words is below the 3-word floor — no write, no
    hallucination of additional words."""
    data = {
        "id": "x",
        "capability_tags": ["alpha"],
        "session_keywords": ["beta"],
    }
    _scanner._apply_discoverability_backfill(data)
    tr = (data.get("usage") or {}).get("trigger_recognition") or {}
    assert "hint_words" not in tr


def test_backfill_caps_hint_words_at_renderer_limit():
    """The renderer ceiling is 12; backfill must not write more."""
    data = {
        "id": "x",
        "capability_tags": [f"tag{i}" for i in range(20)],
    }
    _scanner._apply_discoverability_backfill(data)
    hints = data["usage"]["trigger_recognition"]["hint_words"]
    assert len(hints) == 12
    assert hints == [f"tag{i}" for i in range(12)]


def test_backfill_preserves_explicit_hint_words():
    """When explicit hint_words is non-empty, the union pass is a no-op
    — never clobber operator-set values, even if the union has more."""
    data = {
        "id": "x",
        "usage": {"trigger_recognition": {"hint_words": ["only_this"]}},
        "capability_tags": ["a", "b", "c", "d"],
    }
    _scanner._apply_discoverability_backfill(data)
    assert data["usage"]["trigger_recognition"]["hint_words"] == ["only_this"]


def test_backfill_fills_both_in_one_call():
    """Single-call atomicity: both fields filled in one pass."""
    data = {
        "id": "x",
        "scheduled_actions": [{"id": "morning"}],
        "capability_tags": ["alpha", "beta", "gamma"],
    }
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is True
    assert data["usage"]["model"] == "scheduled"
    assert data["usage"]["trigger_recognition"]["hint_words"] == [
        "alpha", "beta", "gamma",
    ]


def test_backfill_noop_on_complete_manifest():
    """Already-populated manifest gets no writes."""
    data = {
        "id": "x",
        "usage": {
            "model": "user-initiated",
            "trigger_recognition": {"hint_words": ["a", "b", "c"]},
        },
    }
    before = {**data, "usage": {**data["usage"]}}
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is False
    assert data == before


def test_backfill_tolerates_non_dict_usage():
    """Schema drift / hand-edited manifest: usage as a string. Don't crash,
    overwrite with a valid block."""
    data = {"id": "x", "usage": "wrong-shape", "capability_tags": ["a", "b", "c"]}
    changed = _scanner._apply_discoverability_backfill(data)
    assert changed is True
    assert isinstance(data["usage"], dict)
    assert data["usage"]["model"] == "user-initiated"
    assert data["usage"]["trigger_recognition"]["hint_words"] == ["a", "b", "c"]


def test_backfill_returns_false_for_non_dict_input():
    """Defensive — never raise on bad input."""
    assert _scanner._apply_discoverability_backfill("not a dict") is False  # type: ignore[arg-type]
    assert _scanner._apply_discoverability_backfill(None) is False  # type: ignore[arg-type]


# ── Constants mirror the structural verifier ────────────────────────────────


def test_hint_words_min_matches_structural_verifier():
    """If app_audit_structural retunes _DISCOVERABILITY_MIN_HINT_WORDS,
    the scanner's mirror constant must move with it — otherwise Pass D
    writes hint sets the verifier still flags (or skips ones it accepts).
    """
    _PACKAGES_DIR = _ADMIN_DIR.parent
    sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))
    import app_audit_structural as _aas  # noqa: E402

    assert (
        _scanner._DISCOVERABILITY_HINT_WORDS_MIN
        == _aas._DISCOVERABILITY_MIN_HINT_WORDS
    )
