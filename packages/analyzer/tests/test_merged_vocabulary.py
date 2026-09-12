"""tests/test_merged_vocabulary.py — pin the static + dynamic
vocabulary merge layer.

Spec: Phase 2 / Layer 2 design discussion (2026-06-05).

The merge layer enables runtime vocabulary expansion without a code
change: pattern monitors read ``static ∪ dynamic`` via
``effective_keywords(shared_dir)``. Dynamic entries can be added
manually (via ``tools.vocab_add``) or by the LLM-backed monitor
that ships in PR γ.

Backward compat: empty dynamic.json → behavior identical to v1.5
(static-only). This is the contract that lets the plumbing ship
without affecting any existing monitor.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import _merged_vocabulary as mv  # noqa: E402


NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Static-only behavior (empty dynamic)
# ─────────────────────────────────────────────────────────────────────────────


def test_effective_keywords_returns_static_only_when_dynamic_missing(tmp_path):
    """Fresh tempdir, no dynamic.json → effective keywords match
    static keywords exactly."""
    effective = mv.effective_keywords(tmp_path, now=NOW)
    static = mv.static_keywords()
    assert effective == static


def test_effective_keywords_returns_static_only_when_shared_dir_none():
    """None shared_dir is the test-fixture path: just static."""
    effective = mv.effective_keywords(None)
    static = mv.static_keywords()
    assert effective == static


def test_load_dynamic_state_returns_empty_for_missing_file(tmp_path):
    state = mv.load_dynamic_state(tmp_path)
    assert state["schema_version"] == mv.SCHEMA_VERSION
    assert state["keywords"] == {}
    assert state["catalog_seeds"] == []


def test_load_dynamic_state_returns_empty_for_malformed_file(tmp_path):
    """A garbled dynamic.json must NOT crash the substrate. Fail
    closed to the empty state so monitors continue working."""
    p = mv.dynamic_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{this is not valid json")
    state = mv.load_dynamic_state(tmp_path)
    assert state["keywords"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# Add + retrieve
# ─────────────────────────────────────────────────────────────────────────────


def test_add_keyword_persists_to_disk(tmp_path):
    mv.add_keyword(
        tmp_path, "sourdough", "domain:food",
        added_by="manual",
        rationale="operator test",
    )
    state = mv.load_dynamic_state(tmp_path)
    assert "sourdough" in state["keywords"]
    entry = state["keywords"]["sourdough"]
    assert entry["domain"] == "domain:food"
    assert entry["added_by"] == "manual"
    assert entry["rationale"] == "operator test"


def test_added_keyword_appears_in_effective_vocabulary(tmp_path):
    """End-to-end: add via mv.add_keyword → effective_keywords sees it."""
    mv.add_keyword(tmp_path, "sourdough", "domain:food", added_by="manual")
    effective = mv.effective_keywords(tmp_path, now=NOW)
    assert effective.get("sourdough") == "domain:food"


def test_add_keyword_rejects_bad_domain():
    """Domain tag must start with 'domain:'."""
    with pytest.raises(ValueError):
        mv.add_keyword(
            Path("/tmp/nope"), "x", "fitness", added_by="manual",
        )


def test_add_keyword_normalizes_to_lowercase(tmp_path):
    mv.add_keyword(tmp_path, "SourDough", "domain:food", added_by="manual")
    state = mv.load_dynamic_state(tmp_path)
    assert "sourdough" in state["keywords"]
    assert "SourDough" not in state["keywords"]


# ─────────────────────────────────────────────────────────────────────────────
# Override + collision
# ─────────────────────────────────────────────────────────────────────────────


def test_dynamic_overrides_static_on_collision(tmp_path):
    """When a static keyword and a dynamic keyword spell the same
    string, dynamic wins. Operators (or approved LLM proposals) can
    correct a wrong static mapping by adding a dynamic override."""
    # 'fitness' is in the static map → domain:fitness.
    static = mv.static_keywords()
    assert static.get("fitness") == "domain:fitness"
    # Override it.
    mv.add_keyword(
        tmp_path, "fitness", "domain:health", added_by="manual"
    )
    effective = mv.effective_keywords(tmp_path, now=NOW)
    assert effective["fitness"] == "domain:health"


def test_unrelated_static_keywords_unchanged_after_dynamic_add(tmp_path):
    mv.add_keyword(tmp_path, "sourdough", "domain:food", added_by="manual")
    effective = mv.effective_keywords(tmp_path, now=NOW)
    # Static 'fitness' → domain:fitness untouched.
    assert effective.get("fitness") == "domain:fitness"


# ─────────────────────────────────────────────────────────────────────────────
# TTL
# ─────────────────────────────────────────────────────────────────────────────


def test_expired_dynamic_entry_dropped_from_effective_keywords(tmp_path):
    """A dynamic entry past its ttl_days must not appear in
    effective_keywords. Stops one-time conversational fads from
    staying in the vocabulary forever."""
    # Pin added_at to NOW so the ttl window is deterministic — without
    # this, add_keyword stamps the real wall clock and the test
    # time-bombs once the real date crosses NOW + ttl_days.
    mv.add_keyword(
        tmp_path, "sourdough", "domain:food",
        added_by="manual", ttl_days=10, added_at=NOW,
    )
    # Run effective_keywords 30 days later — past the 10-day ttl.
    future = NOW + timedelta(days=30)
    effective = mv.effective_keywords(tmp_path, now=future)
    assert "sourdough" not in effective


def test_live_dynamic_entry_kept_in_effective_keywords(tmp_path):
    # Pin added_at to NOW so the live-vs-expired comparison is purely
    # NOW-relative and never depends on the real wall clock.
    mv.add_keyword(
        tmp_path, "sourdough", "domain:food",
        added_by="manual", ttl_days=90, added_at=NOW,
    )
    # 30 days later — still well within the 90-day ttl.
    future = NOW + timedelta(days=30)
    effective = mv.effective_keywords(tmp_path, now=future)
    assert effective.get("sourdough") == "domain:food"


def test_malformed_ttl_treated_as_live(tmp_path):
    """A dynamic entry with a missing/malformed ttl is conservatively
    kept (treated as live). Operator-edited file shouldn't silently
    lose entries just because the TTL field got typo'd."""
    state = mv.load_dynamic_state(tmp_path)
    state["keywords"]["sourdough"] = {
        "domain": "domain:food",
        "added_at": "2026-01-01T00:00:00Z",
        "added_by": "manual",
        "ttl_days": "not-a-number",  # malformed
    }
    p = mv.dynamic_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))
    effective = mv.effective_keywords(tmp_path, now=NOW)
    assert "sourdough" in effective


# ─────────────────────────────────────────────────────────────────────────────
# Catalog seeds
# ─────────────────────────────────────────────────────────────────────────────


def test_add_catalog_seed_persists(tmp_path):
    mv.add_catalog_seed(
        tmp_path,
        category="gardening_log",
        title="Garden journal",
        description="Track plantings and harvests",
        example_apps=["garden-log"],
        domain_tag="domain:gardening",
        added_by="manual",
    )
    seeds = mv.effective_catalog_seeds(tmp_path)
    assert len(seeds) == 1
    assert seeds[0]["category"] == "gardening_log"
    assert "domain:gardening" in seeds[0]["tags"]


def test_add_catalog_seed_dedups_on_category(tmp_path):
    """Re-adding the same category replaces in place — no duplicate
    entries when the operator iterates."""
    for desc in ("v1 description", "v2 description"):
        mv.add_catalog_seed(
            tmp_path,
            category="gardening_log",
            title="Garden journal",
            description=desc,
            example_apps=["garden-log"],
            domain_tag="domain:gardening",
            added_by="manual",
        )
    seeds = mv.effective_catalog_seeds(tmp_path)
    assert len(seeds) == 1
    assert seeds[0]["description"] == "v2 description"


def test_add_catalog_seed_rejects_bad_domain():
    with pytest.raises(ValueError):
        mv.add_catalog_seed(
            Path("/tmp/nope"),
            category="x", title="x", description="x",
            example_apps=[], domain_tag="not-a-domain", added_by="manual",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Remove
# ─────────────────────────────────────────────────────────────────────────────


def test_remove_keyword_returns_true_when_present(tmp_path):
    mv.add_keyword(tmp_path, "sourdough", "domain:food", added_by="manual")
    assert mv.remove_keyword(tmp_path, "sourdough") is True


def test_remove_keyword_returns_false_when_absent(tmp_path):
    assert mv.remove_keyword(tmp_path, "nonexistent") is False


def test_removed_keyword_falls_back_to_static(tmp_path):
    """Override + remove: effective vocab returns to static."""
    mv.add_keyword(
        tmp_path, "fitness", "domain:health", added_by="manual"
    )
    assert mv.effective_keywords(tmp_path, now=NOW)["fitness"] == "domain:health"
    mv.remove_keyword(tmp_path, "fitness")
    assert (
        mv.effective_keywords(tmp_path, now=NOW)["fitness"]
        == "domain:fitness"  # back to static
    )
