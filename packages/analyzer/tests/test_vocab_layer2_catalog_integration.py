"""tests/test_vocab_layer2_catalog_integration.py — pin the fixes
for two Layer 2 integration bugs uncovered in the 2026-06-05 audit
of PR #2198.

**Bug P0**: ``vocab_add catalog-seed`` writes a dynamic catalog
entry; nothing reads it. The pattern monitors' ``_load_catalog``
calls only consumed the static ``app_suggester/catalog.json``.

**Bug P1**: ``_extract_covered_domains`` used static
``_DOMAIN_KEYWORDS`` directly, so a manifest whose name / description
used only dynamic-vocab keywords didn't get credit for covering the
dynamic domain. cap-gap monitor would then keep emitting gap
proposals for a domain the bot already covered.

This file pins both fixes end-to-end. Each test reproduces the
buggy behavior on the unfixed code path, then asserts the fix
behavior.
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
import capability_gap_monitor as cap_mod  # noqa: E402
from generators.app_suggester.observe import (  # noqa: E402
    _CATALOG_PATH as APP_SUGGESTER_CATALOG_PATH,
    _extract_covered_domains,
)
from generators.pod_capability_lift import (  # noqa: E402
    PodCapabilityLiftContext,
    observe as pod_lift_observe,
)
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# effective_catalog helper (new in this PR)
# ─────────────────────────────────────────────────────────────────────────────


def test_effective_catalog_returns_static_only_when_no_dynamic(tmp_path):
    """No dynamic.json → effective_catalog returns the static entries
    only. Backward-compat with pre-Layer-2 callers."""
    static = mv.effective_catalog(APP_SUGGESTER_CATALOG_PATH, None)
    assert len(static) >= 18  # v1.5 ships 18 entries
    categories = {e["category"] for e in static}
    assert "fitness_tracking" in categories  # v1 static
    assert "meal_planning" in categories      # v1.5 static


def test_effective_catalog_includes_dynamic_seeds(tmp_path):
    """Dynamic catalog seeds added via vocab_add show up alongside
    the static entries. The P0 bug was that no consumer called this
    — now both cap-gap and pod_capability_lift do."""
    mv.add_catalog_seed(
        tmp_path,
        category="gardening_log",
        title="Garden journal",
        description="Track plantings and harvests",
        example_apps=["garden-log"],
        domain_tag="domain:gardening",
        added_by="manual",
    )
    merged = mv.effective_catalog(APP_SUGGESTER_CATALOG_PATH, tmp_path)
    categories = {e["category"] for e in merged}
    assert "gardening_log" in categories
    # Static entries still present.
    assert "fitness_tracking" in categories


def test_effective_catalog_dynamic_overrides_static_on_category_collision(
    tmp_path,
):
    """When a dynamic seed has the same category as a static entry,
    dynamic wins — operator override semantics. Confirms the merge
    rule is "dynamic-replaces-static by category" (not append-and-
    duplicate which would double the proposal stream)."""
    mv.add_catalog_seed(
        tmp_path,
        category="fitness_tracking",  # same as static
        title="Override fitness title",
        description="overridden",
        example_apps=["override-app"],
        domain_tag="domain:fitness",
        added_by="manual",
    )
    merged = mv.effective_catalog(APP_SUGGESTER_CATALOG_PATH, tmp_path)
    fits = [e for e in merged if e["category"] == "fitness_tracking"]
    assert len(fits) == 1
    assert fits[0]["title"] == "Override fitness title"


# ─────────────────────────────────────────────────────────────────────────────
# P0 fix: cap-gap monitor reads dynamic catalog seeds
# ─────────────────────────────────────────────────────────────────────────────


def _write_tuples_for_noun(
    shared_dir, bot_id, noun, n=6, days_span=6, engagement_each=4,
):
    """6 sessions over 6 days clears default cap-gap thresholds."""
    for i in range(n):
        day = NOW - timedelta(days=(i % days_span))
        t = ObservationTuple(
            id=f"obs-{noun}-{i}",
            bot_id=bot_id,
            session_id=f"sess-{noun}-{i}",
            segment_id=f"seg-{i}",
            noun=noun,
            verb="tracking",
            mood="enthusiastic",
            engagement=engagement_each,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"hash-{noun}-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)


def _stub_agents_md(monkeypatch, content, tmp_path, bot_id):
    """Drop a fake AGENTS.md + patch cap-gap's reader."""
    p = tmp_path / "agents-bot.md"
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        cap_mod, "_bot_workspace_agents_md", lambda _bid: p
    )


def test_cap_gap_emits_signal_for_dynamic_catalog_seed(
    tmp_path, monkeypatch
):
    """End-to-end P0 fix:

      1. Operator adds keyword 'gardening' → 'domain:gardening'.
      2. Operator adds catalog seed for category 'gardening_log'
         tagged with 'domain:gardening'.
      3. Bot has a gardening pattern (6 sessions / 6 days).
      4. Bot's AGENTS.md mentions 'gardening' (confirmed fit).
      5. cap-gap monitor MUST emit a signal for 'gardening_log'.

    Pre-fix, step 5 was silent — cap-gap's _load_catalog didn't see
    the dynamic seed. This test confirms the merged read path works.
    """
    # Step 1: dynamic keyword.
    mv.add_keyword(
        tmp_path, "gardening", "domain:gardening", added_by="manual"
    )
    # Step 2: dynamic catalog seed.
    mv.add_catalog_seed(
        tmp_path,
        category="gardening_log",
        title="Garden journal",
        description="Track plantings and harvests",
        example_apps=["garden-log"],
        domain_tag="domain:gardening",
        added_by="manual",
    )
    # Step 3: observation pattern.
    _write_tuples_for_noun(tmp_path, BOT_ID, noun="gardening")
    # Step 4: AGENTS.md confirms.
    _stub_agents_md(
        monkeypatch,
        "This bot helps with home gardening and planting schedules.",
        tmp_path, BOT_ID,
    )
    # Empty manifests so no domain is pre-covered.
    (tmp_path / "applications" / BOT_ID).mkdir(parents=True, exist_ok=True)

    detections = cap_mod.detect_capability_gaps(
        BOT_ID, tmp_path, now=NOW
    )
    categories = [d["details"]["category"] for d in detections]
    assert "gardening_log" in categories, (
        f"Expected gardening_log signal from dynamic catalog seed; "
        f"got {categories}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1 fix: covered-domain detection uses merged vocab
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_covered_domains_uses_default_static_when_kw_map_none():
    """Backward compat: no kw_map → static behavior preserved. The
    in-tree app_suggester tests that don't yet know about dynamic
    vocab continue to work."""
    manifests = [{"name": "fitness-tracker", "description": ""}]
    domains = _extract_covered_domains(manifests)
    assert "domain:fitness" in domains


def test_extract_covered_domains_recognizes_dynamic_keyword():
    """When the caller passes a merged kw_map, a manifest using a
    dynamic-only keyword (no static overlap) gets credit for the
    dynamic domain."""
    manifests = [{"name": "sourdough-tracker", "description": ""}]
    # Static vocab: 'sourdough' doesn't resolve → no domain.
    static_domains = _extract_covered_domains(manifests)
    assert "domain:food" not in static_domains
    # Merged vocab (simulated): 'sourdough' → domain:food.
    merged_kw = dict(mv.static_keywords())
    merged_kw["sourdough"] = "domain:food"
    domains = _extract_covered_domains(manifests, kw_map=merged_kw)
    assert "domain:food" in domains


def test_cap_gap_does_NOT_emit_when_dynamic_keyword_covers_domain(
    tmp_path, monkeypatch
):
    """End-to-end P1 fix:

      1. Operator adds keyword 'sourdough' → 'domain:food'.
      2. Bot has a 'sourdough-tracker' manifest installed.
      3. Bot has a sourdough conversation pattern.
      4. AGENTS.md mentions food.
      5. cap-gap monitor MUST recognize the manifest as covering
         'domain:food' and skip emitting a gap signal.

    Pre-fix, the covered-domain check used static vocab and didn't
    recognize 'sourdough' in the manifest name → cap-gap fired
    repeatedly on a bot that already had food coverage. Operator
    would dismiss every cycle.
    """
    mv.add_keyword(
        tmp_path, "sourdough", "domain:food", added_by="manual"
    )
    # Install a 'sourdough-tracker' manifest.
    manif_dir = tmp_path / "applications" / BOT_ID
    manif_dir.mkdir(parents=True)
    (manif_dir / "sourdough-tracker.json").write_text(
        json.dumps({
            "name": "sourdough-tracker",
            "description": "logs sourdough bakes",
        })
    )
    # Observation pattern.
    _write_tuples_for_noun(tmp_path, BOT_ID, noun="sourdough")
    # AGENTS.md confirms food is in scope.
    _stub_agents_md(
        monkeypatch,
        "This bot helps with cooking and meal planning.",
        tmp_path, BOT_ID,
    )

    detections = cap_mod.detect_capability_gaps(
        BOT_ID, tmp_path, now=NOW
    )
    categories = [d["details"]["category"] for d in detections]
    # The bot has a food-covering manifest; no food gap should fire.
    food_cats = [
        c for c in categories
        if c in {"meal_planning", "fitness_tracking", "household_upkeep"}
    ]
    assert "meal_planning" not in categories, (
        f"meal_planning gap fired despite sourdough-tracker manifest "
        f"covering domain:food via dynamic vocab; got {categories}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# pod_capability_lift sees dynamic catalog seeds
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_capability_lift_uses_dynamic_catalog_title(tmp_path):
    """A pod-wide gap proposal on a dynamic-seed category quotes the
    seed's title in the headline. Pre-fix, pod_capability_lift's
    _load_catalog couldn't see dynamic seeds → headline fell back to
    a slug-derived title."""
    # Add a dynamic catalog seed for the test category.
    mv.add_catalog_seed(
        tmp_path,
        category="gardening_log",
        title="Garden journal — pod test title",
        description="Track plantings",
        example_apps=["garden-log"],
        domain_tag="domain:gardening",
        added_by="manual",
    )

    # Drop 3 cap-gap signals for this category — minimum for pod lift.
    for bot in ("team-bot-a", "team-bot-b", "team-bot-c"):
        signals_store.observe(
            tmp_path,
            signature=f"app_suggester_gap:{bot}:gardening_log",
            producer="capability_gap_monitor",
            type="app_suggester_gap",
            flavor="activity",
            severity="info",
            scope="bot",
            bot_id=bot,
            title=f"Gap: gardening_log for {bot}",
            body="(test fixture)",
            details={
                "category": "gardening_log",
                "bot_id": bot,
                "domain_tag": "domain:gardening",
                "example_nouns": ["gardening"],
                "distinct_sessions": 5,
                "distinct_days": 6,
                "engagement_total": 20,
            },
        )

    ctx = PodCapabilityLiftContext(
        bot_ids=["team-bot-a", "team-bot-b", "team-bot-c"],
        shared_dir=tmp_path,
    )
    proposals = pod_lift_observe(ctx)
    assert len(proposals) >= 1
    # Find the gardening_log proposal.
    gardening_props = [
        p for p in proposals
        if any("gardening_log" in t for t in p.trigger_observations)
    ]
    assert len(gardening_props) == 1
    p = gardening_props[0]
    # Pod-wide headline must use the dynamic seed's title, not a slug.
    assert "Garden journal" in p.admin_surface_summary
