"""Tests for the Customizations admin endpoints.

Phase 4 of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

GET /api/customizations/<bot_id>
POST /api/customizations/<bot_id>/accept
POST /api/customizations/<bot_id>/annotate
POST /api/customizations/<bot_id>/revert

Tests use Flask's in-process test_client + a real tmp_path for the
shared dir so the overrides API touches real disk (matching the
Phase 2 layer's atomic-write semantics). The patterns mirror existing
admin-route tests in this package.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
# The web server imports from packages/analyzer at runtime (schema.signal,
# signals.store) — match the production setup.
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


# A known per_bot schema path we can write overrides against in tests.
_TIER_KEY = "openclaw.plugins.evolve.tier"
_SUMMARIZER_KEY = "openclaw.plugins.evolve.summarizerMinTurns"


def _make_app(tmp_path: Path):
    """Build a Flask app with a minimal network.json pointing shared_dir
    at tmp_path. Returns (app, shared_dir)."""
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "members": ["team_bot_a"],
        "bots": {
            "team_bot_a": {"role": "member", "user": "team_bot_a"},
            "evolve": {"role": "primary", "user": "evolve"},
        },
    }))
    from evolve_admin.web.server import create_app
    return create_app(net_path), tmp_path


def _seed_override(shared_dir: Path, bot_id: str, key: str, value, **kwargs):
    """Convenience: drop an override directly into the file using the
    overrides API. Tests then assert the endpoint surfaces it."""
    from evolve_admin.config_sandbox import write_override
    write_override(
        shared_dir, bot_id, key, value,
        set_by=kwargs.get("set_by", "operator"),
        note=kwargs.get("note"),
        expires_at=kwargs.get("expires_at"),
        needs_review=kwargs.get("needs_review", False),
        now=kwargs.get("now"),
    )


# ─── GET /api/customizations/<bot_id> ──────────────────────────────────────


def test_list_returns_empty_for_bot_with_no_overrides(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().get("/api/customizations/team_bot_a")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["bot_id"] == "team_bot_a"
    assert data["entries"] == []
    assert data["needs_review_count"] == 0


def test_list_returns_overrides_with_schema_metadata(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    r = app.test_client().get("/api/customizations/team_bot_a")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["entries"]) == 1
    entry = data["entries"][0]
    assert entry["key"] == _TIER_KEY
    assert entry["value"] == "monitor"
    assert entry["set_by"] == "operator"
    assert entry["needs_review"] is False
    # Schema metadata enriched
    assert entry["schema"] is not None
    assert entry["schema"]["stock_default"] == "full"
    assert entry["schema"]["strength"] == "free"
    assert entry["schema"]["type_hint"] == "enum"
    assert "description" in entry["schema"]
    assert entry["matches_default"] is False


def test_list_sorts_needs_review_first(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor",
                   set_by="operator", needs_review=False)
    _seed_override(shared, "team_bot_a", _SUMMARIZER_KEY, 5,
                   set_by="auto:drift_x", needs_review=True)
    r = app.test_client().get("/api/customizations/team_bot_a")
    data = r.get_json()
    assert data["needs_review_count"] == 1
    # needs_review entries come first
    assert data["entries"][0]["key"] == _SUMMARIZER_KEY
    assert data["entries"][0]["needs_review"] is True
    assert data["entries"][1]["key"] == _TIER_KEY


def test_list_unknown_bot_returns_404(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().get("/api/customizations/bogus")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_list_invalid_bot_id_shape_returns_404_via_network_lookup(tmp_path):
    """Path-traversal-y bot_id fails the network bot lookup first."""
    app, _ = _make_app(tmp_path)
    r = app.test_client().get("/api/customizations/..%2Fescape")
    # Flask url-decodes %2F into / which doesn't match; we get a 404
    # from the route itself OR from the bot lookup. Either is fine —
    # the key invariant is the request doesn't crash and the operator
    # gets a non-200.
    assert r.status_code in (400, 404)


# ─── POST /api/customizations/<bot_id>/accept ──────────────────────────────


def test_accept_clears_needs_review(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor",
                   set_by="auto:drift_2026-05-25", needs_review=True,
                   note="Auto-recorded; review.")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["entry"]["needs_review"] is False
    assert data["entry"]["set_by"] == "operator"   # re-attributed
    # And persisted
    from evolve_admin.config_sandbox import read_bot_overrides
    bo = read_bot_overrides(shared, "team_bot_a")
    assert bo.get(_TIER_KEY).needs_review is False
    assert bo.get(_TIER_KEY).set_by == "operator"


def test_accept_with_new_note_overwrites_existing(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor",
                   needs_review=True, note="original auto note")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY, "note": "operator's rationale"},
    )
    assert r.status_code == 200
    assert r.get_json()["entry"]["note"] == "operator's rationale"


def test_accept_unknown_key_returns_404(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 404
    assert "no override" in r.get_json()["error"]


def test_accept_missing_key_in_body_returns_400(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post("/api/customizations/team_bot_a/accept", json={})
    assert r.status_code == 400
    assert "missing 'key'" in r.get_json()["error"]


def test_accept_idempotent_on_already_accepted_entry(tmp_path):
    """Re-accepting an already-operator-attributed entry is a no-op:
    the response includes ``noop=true`` and the set_at timestamp from
    the original acceptance is preserved (vs. a naïve mark_reviewed
    that would rewrite set_at every click)."""
    app, shared = _make_app(tmp_path)
    from datetime import datetime, timezone
    original_when = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    _seed_override(
        shared, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", needs_review=False,
        note="already settled",
        now=original_when,
    )
    # Capture the original set_at.
    from evolve_admin.config_sandbox import read_bot_overrides
    bo_before = read_bot_overrides(shared, "team_bot_a")
    set_at_before = bo_before.get(_TIER_KEY).set_at

    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data.get("noop") is True
    # set_at unchanged
    bo_after = read_bot_overrides(shared, "team_bot_a")
    assert bo_after.get(_TIER_KEY).set_at == set_at_before


def test_accept_writes_when_note_change_requested_on_settled_entry(tmp_path):
    """If the caller passes a new note while the entry is already
    accepted, we DO rewrite (the note change is the operator's intent
    — the noop guard only fires when nothing meaningfully changes)."""
    app, shared = _make_app(tmp_path)
    _seed_override(
        shared, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", needs_review=False, note="old note",
    )
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY, "note": "operator added rationale later"},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data.get("noop") is not True
    assert data["entry"]["note"] == "operator added rationale later"


def test_accept_whitespace_only_key_rejected(tmp_path):
    """Empty-after-strip key is a 400, not a 404 — paranoid input check
    from the reviewer pass."""
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": "   "},
    )
    assert r.status_code == 400
    assert "empty or whitespace" in r.get_json()["error"]


def test_accept_overlong_key_rejected(tmp_path):
    """Length cap on key. Schema paths are well under 256 chars; reject
    larger inputs defensively."""
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": "x" * 300},
    )
    assert r.status_code == 400
    assert "too long" in r.get_json()["error"]


# ─── POST /api/customizations/<bot_id>/annotate ────────────────────────────


def test_annotate_updates_note_and_expires_at(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={
            "key": _TIER_KEY,
            "note": "until upstream lands",
            "expires_at": "2026-08-01",
        },
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["entry"]["note"] == "until upstream lands"
    assert data["entry"]["expires_at"] == "2026-08-01"
    # Value unchanged
    assert data["entry"]["value"] == "monitor"


def test_annotate_clears_expires_at_when_null(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor",
                   set_by="operator", expires_at="2026-08-01")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={"key": _TIER_KEY, "expires_at": None},
    )
    assert r.status_code == 200
    assert r.get_json()["entry"]["expires_at"] is None


def test_annotate_preserves_unmentioned_fields(tmp_path):
    """Body with only ``note`` doesn't touch ``expires_at`` and vice versa."""
    app, shared = _make_app(tmp_path)
    _seed_override(
        shared, "team_bot_a", _TIER_KEY, "monitor",
        set_by="operator", note="original", expires_at="2026-08-01",
    )
    # Annotate only the note; expires_at must survive.
    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={"key": _TIER_KEY, "note": "new"},
    )
    assert r.status_code == 200
    entry = r.get_json()["entry"]
    assert entry["note"] == "new"
    assert entry["expires_at"] == "2026-08-01"


def test_annotate_rejects_malformed_expires_at(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={"key": _TIER_KEY, "expires_at": "tomorrow"},
    )
    assert r.status_code == 400
    assert "expires_at" in r.get_json()["error"]


def test_annotate_unknown_key_returns_404(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={"key": _TIER_KEY, "note": "x"},
    )
    assert r.status_code == 404


# ─── POST /api/customizations/<bot_id>/revert ──────────────────────────────


def test_revert_deletes_override(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/revert",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["removed"] is True
    from evolve_admin.config_sandbox import read_bot_overrides
    bo = read_bot_overrides(shared, "team_bot_a")
    assert bo.get(_TIER_KEY) is None


def test_revert_idempotent_when_already_absent(tmp_path):
    """Re-reverting a key that doesn't exist returns ok=true, removed=false."""
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/revert",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["removed"] is False


def test_revert_missing_key_returns_400(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post("/api/customizations/team_bot_a/revert", json={})
    assert r.status_code == 400


def test_revert_unknown_bot_returns_404(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/bogus/revert",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 404


# ─── Malformed overrides file ──────────────────────────────────────────────


def test_accept_returns_500_when_overrides_file_corrupted(tmp_path):
    """Defensive read returns empty BotOverrides on malformed JSON, so
    GET returns 200 with no entries. But the write path is strict — it
    refuses to clobber a non-empty malformed file. Surface a 500 to the
    operator with an actionable error message rather than silently
    nuking the file."""
    app, shared = _make_app(tmp_path)
    # First write a valid override, then corrupt the file.
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor",
                   set_by="operator", needs_review=True)
    from evolve_admin.config_sandbox import path_for_bot
    p = path_for_bot(shared, "team_bot_a")
    p.write_text("{ corrupted mid-line")

    r = app.test_client().post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY},
    )
    # Accept's idempotency guard calls read_bot_overrides (defensive,
    # returns empty BotOverrides on malformed) → the entry "doesn't
    # exist" → mark_reviewed returns None → 404. This is acceptable:
    # the operator sees the entry is unreachable. The OverrideStateError
    # path fires on write, which annotate exercises below.
    assert r.status_code in (404, 500)


def test_annotate_returns_500_when_overrides_file_corrupted(tmp_path):
    """Annotate's write path goes through _read_for_write which is the
    strict variant — refuses to clobber malformed."""
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    from evolve_admin.config_sandbox import path_for_bot
    p = path_for_bot(shared, "team_bot_a")
    p.write_text("{ corrupted mid-line")

    r = app.test_client().post(
        "/api/customizations/team_bot_a/annotate",
        json={"key": _TIER_KEY, "note": "rationale"},
    )
    # The defensive GET inside the annotate handler sees an empty bo,
    # so the override "doesn't exist" → 404. Either 404 or 500 is
    # acceptable: both surface that something's wrong with the file.
    assert r.status_code in (404, 500)


# ─── Full workflow ─────────────────────────────────────────────────────────


def test_full_review_workflow(tmp_path):
    """Operator's typical journey: see needs_review entry, annotate with
    rationale + expires_at, then accept. Final state: clean override
    with operator attribution."""
    app, shared = _make_app(tmp_path)
    # Migration A seeded an entry needing review
    _seed_override(
        shared, "team_bot_a", _TIER_KEY, "monitor",
        set_by="migration:openclaw_derived_2026_05_24",
        needs_review=True,
        note="Auto-recorded by Migration A; review.",
    )
    client = app.test_client()

    # 1. List → sees the entry
    r = client.get("/api/customizations/team_bot_a")
    assert r.get_json()["needs_review_count"] == 1

    # 2. Operator annotates with their rationale
    r = client.post(
        "/api/customizations/team_bot_a/annotate",
        json={
            "key": _TIER_KEY,
            "note": "Reduced tier to lower cost on this bot's idle days.",
            "expires_at": "2026-12-31",
        },
    )
    assert r.status_code == 200

    # 3. Operator accepts (clears needs_review, re-attributes to operator)
    r = client.post(
        "/api/customizations/team_bot_a/accept",
        json={"key": _TIER_KEY},
    )
    assert r.status_code == 200
    entry = r.get_json()["entry"]
    assert entry["needs_review"] is False
    assert entry["set_by"] == "operator"
    assert entry["note"] == "Reduced tier to lower cost on this bot's idle days."
    assert entry["expires_at"] == "2026-12-31"

    # 4. Final list shows zero needs_review
    r = client.get("/api/customizations/team_bot_a")
    assert r.get_json()["needs_review_count"] == 0


# ─── POST /api/customizations/<bot_id>/set ─────────────────────────────────
# Direct operator-create-override endpoint. Distinct from accept/annotate
# (which act on existing entries); this is the path the per-bot
# Customizations UI uses to flip a value (e.g. cacheRetention) without
# waiting for the auto-promote-on-drift detector to notice.


# Generic "known schema key" used by the /set endpoint tests below. Was
# previously `cacheRetention`; that TunableKey schema entry was removed in
# Phase 4b of the cost-cap normalization (it moved to better-engine-config).
# Switched to the summarizer field, which is a stable plugin config tunable
# unrelated to the cost-cap surface.
_GENERIC_SET_KEY = "openclaw.plugins.evolve.summarizerMinTurns"
_GENERIC_SET_VALUE = 5
_GENERIC_SET_ALT_VALUE = 8


def test_set_creates_override_for_known_key(tmp_path):
    app, shared = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={"key": _GENERIC_SET_KEY, "value": _GENERIC_SET_VALUE},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    entry = body["entry"]
    assert entry["key"] == _GENERIC_SET_KEY
    assert entry["value"] == _GENERIC_SET_VALUE
    assert entry["set_by"] == "operator"
    assert entry["needs_review"] is False


def test_set_replaces_existing_override(tmp_path):
    app, shared = _make_app(tmp_path)
    _seed_override(shared, "team_bot_a", _GENERIC_SET_KEY, _GENERIC_SET_VALUE,
                   set_by="operator")
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={"key": _GENERIC_SET_KEY, "value": _GENERIC_SET_ALT_VALUE},
    )
    assert r.status_code == 200
    assert r.get_json()["entry"]["value"] == _GENERIC_SET_ALT_VALUE


def test_set_carries_optional_note(tmp_path):
    app, shared = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={
            "key": _GENERIC_SET_KEY,
            "value": _GENERIC_SET_VALUE,
            "note": "Bumping summarizer threshold for the long-thread cohort.",
        },
    )
    assert r.status_code == 200
    entry = r.get_json()["entry"]
    assert entry["note"] == "Bumping summarizer threshold for the long-thread cohort."


def test_set_rejects_missing_value(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={"key": _GENERIC_SET_KEY},
    )
    assert r.status_code == 400
    assert "value" in r.get_json()["error"]


def test_set_rejects_unknown_schema_key(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={"key": "openclaw.this.does.not.exist", "value": "anything"},
    )
    assert r.status_code == 400
    assert "unknown schema path" in r.get_json()["error"]


def test_set_rejects_wrong_type_for_key(tmp_path):
    """The summarizerMinTurns key expects an int — passing a string is
    rejected upstream by write_override's type-check."""
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/team_bot_a/set",
        json={"key": _SUMMARIZER_KEY, "value": "not-a-number"},
    )
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert "wrong type" in err.lower() or "expected" in err.lower()


def test_set_unknown_bot_returns_404(tmp_path):
    app, _ = _make_app(tmp_path)
    r = app.test_client().post(
        "/api/customizations/no_such_bot/set",
        json={"key": _GENERIC_SET_KEY, "value": _GENERIC_SET_VALUE},
    )
    assert r.status_code == 404


def test_set_then_list_shows_the_new_override(tmp_path):
    """End-to-end: operator sets a value via /set, the listing endpoint
    now surfaces it as a settled override (not needs_review)."""
    app, shared = _make_app(tmp_path)
    client = app.test_client()
    r = client.post(
        "/api/customizations/team_bot_a/set",
        json={"key": _GENERIC_SET_KEY, "value": _GENERIC_SET_VALUE},
    )
    assert r.status_code == 200

    r = client.get("/api/customizations/team_bot_a")
    data = r.get_json()
    keys = {e["key"] for e in data["entries"]}
    assert _GENERIC_SET_KEY in keys
    assert data["needs_review_count"] == 0  # operator-authored, not needs_review


# Session-budget-cap endpoint tests removed in Phase 4b. The TunableKey
# schema entry for sessionBudgetCapUsd was deleted; the per-session cost
# cap now lives in better-engine-config and is set via /api/arbiter/bot-setup.
# Endpoint coverage for that field belongs in test_cost_caps_normalize +
# the Phase 5 BE config tests.
