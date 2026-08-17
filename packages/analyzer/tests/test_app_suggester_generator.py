"""tests/test_app_suggester_generator.py — app_suggester observe + helpers.

Pod-wide generator: one pitch fans to at most one Proposal per run, and
nothing fires without grounding from the Signal store. See triage
2026-05-25 for the fan-out incident this guards against.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.app_suggester.observe import (  # noqa: E402
    _CATALOG_MATCH_CONFIDENCE,
    _MIN_UNGROUNDED_CONFIDENCE,
    AppSuggesterContext,
    _entry_domain_tags,
    _extract_covered_domains,
    _load_bot_manifests,
    _load_catalog,
    _make_proposal,
    observe,
)
from signals import store as signals_store  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_manifest(shared_dir: Path, bot_id: str, *, name: str, description: str,
                    app_id: str | None = None) -> None:
    d = shared_dir / "applications" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "description": description}
    if app_id:
        payload["id"] = app_id
    (d / f"{app_id or name}.json").write_text(json.dumps(payload))


def _write_catalog(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def _emit_grounding_signal(
    shared_dir: Path, bot_id: str, category: str
) -> str:
    """Drop an ``app_suggester_gap`` Signal that grounds (bot, category)."""
    sig = signals_store.observe(
        shared_dir,
        signature=f"app_suggester_gap:{bot_id}:{category}",
        producer="test_fixture",
        type="app_suggester_gap",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=f"Gap: {category} for {bot_id}",
        body="test fixture",
        details={"category": category, "bot_id": bot_id},
    )
    return sig.id


# ── Catalog reader ──────────────────────────────────────────────────────────


def test_entry_domain_tags_filters_to_domain_prefix():
    tags = _entry_domain_tags({"tags": ["domain:health", "intent:explore", "x"]})
    assert tags == {"domain:health"}


def test_entry_domain_tags_empty_list():
    assert _entry_domain_tags({}) == set()
    assert _entry_domain_tags({"tags": []}) == set()


def test_load_catalog_missing_file_returns_empty(tmp_path):
    assert _load_catalog(tmp_path / "nope.json") == []


def test_load_catalog_malformed_file_returns_empty(tmp_path):
    p = tmp_path / "catalog.json"
    p.write_text("{not valid json")
    assert _load_catalog(p) == []


def test_load_catalog_non_list_returns_empty(tmp_path):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"category": "x"}))
    assert _load_catalog(p) == []


def test_load_catalog_filters_non_dict_entries(tmp_path):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps([{"category": "a"}, "string-not-dict", {"category": "b"}]))
    entries = _load_catalog(p)
    assert [e.get("category") for e in entries] == ["a", "b"]


# ── Manifest reader + domain extraction ─────────────────────────────────────


def test_extract_covered_domains_matches_keyword_in_name():
    manifests = [{"name": "daily-health-log", "description": ""}]
    assert _extract_covered_domains(manifests) == {"domain:health"}


def test_extract_covered_domains_matches_keyword_in_description():
    manifests = [{"name": "x", "description": "tracks workouts and runs"}]
    assert _extract_covered_domains(manifests) == {"domain:fitness"}


def test_extract_covered_domains_collects_multiple_domains():
    manifests = [
        {"name": "health-log", "description": ""},
        {"name": "budget-tracker", "description": "expense tracking"},
    ]
    assert _extract_covered_domains(manifests) == {"domain:health", "domain:finance"}


def test_extract_covered_domains_no_keywords_returns_empty():
    assert _extract_covered_domains([{"name": "x", "description": "y"}]) == set()


def test_load_bot_manifests_skips_dot_and_underscore_files(tmp_path):
    d = tmp_path / "applications" / "admin_bot"
    d.mkdir(parents=True)
    (d / "real.json").write_text(json.dumps({"name": "real"}))
    (d / "_internal.json").write_text(json.dumps({"name": "internal"}))
    (d / ".hidden.json").write_text(json.dumps({"name": "hidden"}))
    out = _load_bot_manifests(tmp_path, "admin_bot")
    assert len(out) == 1
    assert out[0]["name"] == "real"


def test_load_bot_manifests_skips_malformed(tmp_path):
    d = tmp_path / "applications" / "admin_bot"
    d.mkdir(parents=True)
    (d / "good.json").write_text(json.dumps({"name": "good"}))
    (d / "bad.json").write_text("{not json")
    out = _load_bot_manifests(tmp_path, "admin_bot")
    names = [m["name"] for m in out]
    assert names == ["good"]


def test_load_bot_manifests_missing_dir_returns_empty(tmp_path):
    assert _load_bot_manifests(tmp_path, "nonexistent") == []


# ── Proposal shape + grounding floor ────────────────────────────────────────


def test_make_proposal_returns_none_without_grounding():
    """catalog_match (0.6 conf) + no signals = no proposal."""
    entry = {"category": "weekly_reflection", "title": "Weekly reflection",
             "description": "x", "tags": ["domain:productivity"]}
    p = _make_proposal(
        "admin_bot", entry, covered_domains=set(), motivating_signals=[]
    )
    assert p is None


def test_make_proposal_emits_when_grounded():
    entry = {"category": "weekly_reflection", "title": "Weekly reflection",
             "description": "x", "tags": ["domain:productivity"]}
    p = _make_proposal(
        "admin_bot", entry, covered_domains=set(),
        motivating_signals=["sig-1"],
    )
    assert p is not None
    assert p.motivating_signals == ["sig-1"]
    assert p.generator_id == "app_suggester"
    assert p.action.kind == "Investigation"
    assert p.urgency == "improvement"
    assert p.approval_audience == "pod_operator"
    assert p.bot_id == "admin_bot"
    assert "weekly_reflection" in p.trigger_observations[0]
    # Title-shape change 2026-06-05: was "Explore: {title} for {bot}",
    # now "Consider {title} on {bot}" — more decisive after the
    # evidence-grounded migration where the proposal carries real
    # observation evidence rather than catalog-only speculation.
    assert p.admin_surface_summary.startswith("Consider ")
    assert len(p.admin_surface_summary) <= 120


def test_make_proposal_emits_when_high_confidence_without_signals():
    """Ungrounded path is still allowed when confidence >= floor."""
    entry = {"category": "x", "title": "X", "description": "x",
             "tags": ["domain:health"]}
    p = _make_proposal(
        "admin_bot", entry, covered_domains=set(),
        motivating_signals=[],
        confidence=_MIN_UNGROUNDED_CONFIDENCE,
    )
    assert p is not None
    assert p.provenance.confidence == _MIN_UNGROUNDED_CONFIDENCE


def test_make_proposal_records_covered_domains_in_provenance():
    entry = {"category": "fitness_tracking", "title": "Workouts",
             "description": "x", "tags": ["domain:fitness"]}
    p = _make_proposal(
        "admin_bot", entry, covered_domains={"domain:health"},
        motivating_signals=["sig-1"],
    )
    assert p is not None
    assert p.provenance.signals["covered_domains"] == ["domain:health"]
    assert p.provenance.signals["category"] == "fitness_tracking"
    assert p.provenance.signals["grounding_signal_ids"] == ["sig-1"]


# ── observe() end-to-end ────────────────────────────────────────────────────


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "catalog.json"
    _write_catalog(p, [
        {"category": "health", "title": "Health log",
         "description": "Health tracking", "example_apps": ["h1"],
         "tags": ["domain:health"]},
        {"category": "finance", "title": "Finance log",
         "description": "Finance tracking", "example_apps": ["f1"],
         "tags": ["domain:finance"]},
        {"category": "learning", "title": "Learning log",
         "description": "Learning", "example_apps": ["l1"],
         "tags": ["domain:learning"]},
    ])
    return p


def test_observe_emits_nothing_without_grounding(tmp_path, catalog_path):
    """The fan-out bug regression test: 7 bots, no signals → 0 proposals."""
    bot_ids = ["evolve", "security_bot", "admin_bot", "team_bot_a", "team_bot_c", "team_bot_b", "personal_bot"]
    ctx = AppSuggesterContext(
        bot_ids=bot_ids, shared_dir=tmp_path, catalog_path=catalog_path,
        max_per_run=10,
    )
    assert observe(ctx) == []


def test_observe_emits_one_proposal_per_grounded_pitch(tmp_path, catalog_path):
    """A grounding Signal on one bot for one category → one Proposal."""
    bot_ids = ["admin_bot", "team_bot_c"]
    sig_id = _emit_grounding_signal(tmp_path, "admin_bot", "finance")
    ctx = AppSuggesterContext(
        bot_ids=bot_ids, shared_dir=tmp_path, catalog_path=catalog_path,
        max_per_run=5,
    )
    proposals = observe(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "admin_bot"
    assert p.motivating_signals == [sig_id]
    assert "finance" in p.trigger_observations[0]


def test_observe_dedupes_one_pitch_across_bots(tmp_path, catalog_path):
    """Same category grounded for two bots → still only one Proposal."""
    bot_ids = ["admin_bot", "team_bot_c"]
    _emit_grounding_signal(tmp_path, "admin_bot", "finance")
    _emit_grounding_signal(tmp_path, "team_bot_c", "finance")
    ctx = AppSuggesterContext(
        bot_ids=bot_ids, shared_dir=tmp_path, catalog_path=catalog_path,
        max_per_run=5,
    )
    proposals = observe(ctx)
    # Both bots are tied at one signal each; "admin_bot" wins by iteration order.
    assert len(proposals) == 1
    assert proposals[0].bot_id == "admin_bot"
    assert "finance" in proposals[0].trigger_observations[0]


def test_observe_picks_most_grounded_bot_for_a_pitch(tmp_path, catalog_path):
    """Bot with more grounding signals wins the pitch."""
    bot_ids = ["admin_bot", "team_bot_c"]
    _emit_grounding_signal(tmp_path, "admin_bot", "finance")
    # team_bot_c gets a second signal for the same category via a different
    # producer + signature, so observation_count rises to 2 distinct sigs.
    signals_store.observe(
        tmp_path,
        signature="alt_producer:finance:team_bot_c",
        producer="alt_producer",
        type="app_suggester_gap",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id="team_bot_c",
        title="Gap (alt)",
        body="",
        details={"category": "finance"},
    )
    signals_store.observe(
        tmp_path,
        signature="alt_producer:finance:team_bot_c:second",
        producer="alt_producer",
        type="app_suggester_gap",
        flavor="activity",
        severity="info",
        scope="bot",
        bot_id="team_bot_c",
        title="Gap (alt2)",
        body="",
        details={"category": "finance"},
    )
    ctx = AppSuggesterContext(
        bot_ids=bot_ids, shared_dir=tmp_path, catalog_path=catalog_path,
        max_per_run=5,
    )
    proposals = observe(ctx)
    assert len(proposals) == 1
    assert proposals[0].bot_id == "team_bot_c"


def test_observe_skips_pitch_when_all_bots_cover_it(tmp_path, catalog_path):
    """Even with grounding, a covered pitch isn't suggested."""
    _write_manifest(tmp_path, "admin_bot", name="my-finance",
                    description="expense tracking")
    _emit_grounding_signal(tmp_path, "admin_bot", "finance")
    ctx = AppSuggesterContext(
        bot_ids=["admin_bot"], shared_dir=tmp_path, catalog_path=catalog_path,
        max_per_run=5,
    )
    proposals = observe(ctx)
    # admin_bot already covers domain:finance — finance entry is filtered out.
    assert all("finance" not in p.trigger_observations[0] for p in proposals)


def test_observe_respects_max_per_run(tmp_path, catalog_path):
    bot_ids = ["admin_bot"]
    _emit_grounding_signal(tmp_path, "admin_bot", "health")
    _emit_grounding_signal(tmp_path, "admin_bot", "finance")
    _emit_grounding_signal(tmp_path, "admin_bot", "learning")
    ctx = AppSuggesterContext(
        bot_ids=bot_ids, shared_dir=tmp_path,
        catalog_path=catalog_path, max_per_run=2,
    )
    proposals = observe(ctx)
    assert len(proposals) == 2


def test_observe_returns_empty_on_empty_catalog(tmp_path):
    empty = tmp_path / "empty.json"
    _write_catalog(empty, [])
    ctx = AppSuggesterContext(
        bot_ids=["admin_bot"], shared_dir=tmp_path, catalog_path=empty
    )
    assert observe(ctx) == []


def test_observe_returns_empty_on_no_bots(tmp_path, catalog_path):
    ctx = AppSuggesterContext(
        bot_ids=[], shared_dir=tmp_path, catalog_path=catalog_path
    )
    assert observe(ctx) == []


def test_observe_catalog_match_confidence_is_below_floor():
    """Guardrail: the deterministic technique must stay below the
    ungrounded floor so it requires Signal grounding to emit."""
    assert _CATALOG_MATCH_CONFIDENCE < _MIN_UNGROUNDED_CONFIDENCE
