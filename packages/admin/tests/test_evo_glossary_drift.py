"""tests/test_evo_glossary_drift.py — keep glossary.yaml in lock-step with source.

The pod glossary in evo's AGENTS.md is generated from
``packages/analyzer/evolve_bot/glossary.yaml``. Hand-curated glossary
content has a brittleness profile that's worst-case unbounded: a new
chip / producer / generator silently makes evo's taught semantics
stale, and the model gives confident-but-wrong advice.

These tests close that gap by enforcing: every chip in
``tile_metrics.py``, every signal producer call site, and every
``analyzer/generators/<id>/`` subdir has a YAML entry — and the YAML
can't include entries that aren't real.

Failures here mean: a new entity was added in source without updating
glossary.yaml. The fix is to add the entry (with evo_advice + urgency
defaults) to the yaml — NOT to weaken these tests.

The single legitimate weakening: add a known-fake producer id to
``glossary._FAKE_PRODUCER_IDS`` (eg new test-fixture id).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

# yaml is the only hard dependency; skip cleanly when missing rather
# than failing in a confusing way.
pytest.importorskip("yaml")

from evolve_admin.evo import glossary as G  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def loaded_glossary() -> G.Glossary:
    return G.load()


# ─────────────────────────────────────────────────────────────────────────────
# Structure
# ─────────────────────────────────────────────────────────────────────────────


def test_glossary_yaml_loads(loaded_glossary):
    """The canonical YAML parses and has each section populated. A
    completely-empty section is suspect — the codebase has chips,
    producers, AND generators today; an empty section means the YAML
    was corrupted or someone wiped it by accident."""
    assert loaded_glossary.version >= 1
    assert loaded_glossary.chips, "glossary.yaml has no chips"
    assert loaded_glossary.producers, "glossary.yaml has no producers"
    assert loaded_glossary.generators, "glossary.yaml has no generators"


def test_every_chip_entry_has_required_fields(loaded_glossary):
    """Schema sanity — every chip carries the fields the renderer
    expects. Catches malformed entries before they produce empty or
    half-rendered glossary sections."""
    required = {"id", "severity", "label", "scope", "fires_when",
                "evo_urgency_default", "evo_advice"}
    for c in loaded_glossary.chips:
        missing = required - set(c.keys())
        assert not missing, f"chip {c.get('id')!r} missing fields: {missing}"
        assert c["severity"] in {"critical", "warn", "info", "depends"}, \
            f"chip {c['id']}: unknown severity {c['severity']!r}"
        assert c["scope"] in {"member_bot", "primary_bot", "any"}, \
            f"chip {c['id']}: unknown scope {c['scope']!r}"
        assert c["evo_urgency_default"] in {"act", "defer", "depends"}, \
            f"chip {c['id']}: unknown urgency {c['evo_urgency_default']!r}"


def test_every_producer_entry_has_required_fields(loaded_glossary):
    """Schema sanity for producers."""
    required = {"id", "source_file", "monitors", "signal_types",
                "sweep_resolves", "evo_urgency_default", "evo_advice"}
    for p in loaded_glossary.producers:
        missing = required - set(p.keys())
        assert not missing, f"producer {p.get('id')!r} missing fields: {missing}"
        assert isinstance(p["sweep_resolves"], bool), \
            f"producer {p['id']}: sweep_resolves must be bool"
        assert p["evo_urgency_default"] in {"act", "defer", "depends"}, \
            f"producer {p['id']}: unknown urgency"


def test_every_generator_entry_has_required_fields(loaded_glossary):
    """Schema sanity for generators."""
    required = {"id", "source_file", "cadence", "audience", "watches",
                "proposes", "evo_urgency_default", "evo_advice"}
    for g in loaded_glossary.generators:
        missing = required - set(g.keys())
        assert not missing, f"generator {g.get('id')!r} missing fields: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# Drift detection — the core invariant
# ─────────────────────────────────────────────────────────────────────────────


def test_glossary_matches_source_chips(loaded_glossary):
    """Every chip emitted by tile_metrics.py must have a YAML entry,
    and vice versa. Drift here means evo's taught semantics fall
    silently out of date — exactly the failure mode this whole
    mitigation framework targets."""
    diff = G._diff_ids(
        G.discover_chip_ids_in_source(_REPO_ROOT),
        loaded_glossary.chip_ids(),
    )
    assert not diff["missing_from_glossary"], (
        f"tile_metrics.py emits these chips but glossary.yaml doesn't "
        f"document them: {diff['missing_from_glossary']}. "
        f"Add entries (with evo_urgency_default + evo_advice) to "
        f"packages/analyzer/evolve_bot/glossary.yaml."
    )
    assert not diff["extra_in_glossary"], (
        f"glossary.yaml has chip entries that don't exist in "
        f"tile_metrics.py: {diff['extra_in_glossary']}. "
        f"Either restore the chip or remove the yaml entry."
    )


def test_glossary_matches_source_producers(loaded_glossary):
    """Every producer the analyzer emits signals under must have a YAML
    entry. New producers without yaml metadata leave evo with no
    taught semantics for the signals they emit."""
    diff = G._diff_ids(
        G.discover_producer_ids_in_source(_REPO_ROOT),
        loaded_glossary.producer_ids(),
    )
    assert not diff["missing_from_glossary"], (
        f"These producers emit signals in source but aren't in "
        f"glossary.yaml: {diff['missing_from_glossary']}. Add entries "
        f"(with evo_advice + evo_urgency_default) to the yaml. If a "
        f"producer is a test fixture rather than a real producer, "
        f"add it to glossary._FAKE_PRODUCER_IDS."
    )
    assert not diff["extra_in_glossary"], (
        f"glossary.yaml has producer entries that don't appear in "
        f"source: {diff['extra_in_glossary']}. "
        f"Producer was removed but yaml wasn't updated, OR the entry "
        f"is misspelled, OR the producer uses a non-standard call "
        f"pattern (extend discover_producer_ids_in_source if so)."
    )


def test_glossary_matches_source_generators(loaded_glossary):
    """Every analyzer/generators/<id>/ subdir must have a YAML entry."""
    diff = G._diff_ids(
        G.discover_generator_ids_in_source(_REPO_ROOT),
        loaded_glossary.generator_ids(),
    )
    assert not diff["missing_from_glossary"], (
        f"These proposal generators exist in source but aren't in "
        f"glossary.yaml: {diff['missing_from_glossary']}. "
        f"Add entries to the yaml."
    )
    assert not diff["extra_in_glossary"], (
        f"glossary.yaml has generator entries that don't have a "
        f"directory under analyzer/generators/: "
        f"{diff['extra_in_glossary']}. Either the generator was "
        f"removed or the entry is misspelled."
    )


def test_compute_drift_aggregates_all_sections(loaded_glossary):
    """The compute_drift helper returns a structured diff used by both
    the test layer (this file) and the CLI's --check flag. Sanity-check
    its shape so future callers don't break."""
    diff = G.compute_drift(loaded_glossary, _REPO_ROOT)
    assert set(diff.keys()) == {"chips", "producers", "generators"}
    for section, body in diff.items():
        assert set(body.keys()) == {"missing_from_glossary", "extra_in_glossary"}


def test_drift_is_clean_on_main(loaded_glossary):
    """The yaml-as-shipped is fully in sync with source. Any drift
    here means a PR slipped in changes to source without updating the
    yaml — the whole point of these tests."""
    diff = G.compute_drift(loaded_glossary, _REPO_ROOT)
    assert G.drift_is_clean(diff), (
        f"glossary.yaml is out of sync with source — see other test "
        f"failures for specifics. Drift: {diff}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Override application — operator personalization
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_overrides_no_op_when_overrides_none(loaded_glossary):
    """None / empty overrides return the glossary unchanged."""
    assert G.apply_overrides(loaded_glossary, None) is loaded_glossary
    assert G.apply_overrides(loaded_glossary, {}).chips == loaded_glossary.chips


def test_apply_overrides_replaces_advice():
    """A pod-specific override for `evo_advice` wins over the
    canonical YAML's default. Operator with a tight budget can flip
    cost_spike from 'depends' to 'act' for their pod without forking
    the yaml."""
    g = G.Glossary(
        version=1,
        chips=[{
            "id": "cost_spike", "severity": "warn",
            "label": "cost spike", "scope": "member_bot",
            "source_file": "x", "fires_when": "x",
            "evo_urgency_default": "depends",
            "evo_advice": "default advice",
        }],
    )
    merged = G.apply_overrides(g, {
        "chips": {
            "cost_spike": {
                "evo_urgency_default": "act",
                "evo_advice": "Our budget is tight; act now.",
            },
        },
    })
    chip = merged.chips[0]
    assert chip["evo_urgency_default"] == "act"
    assert "tight" in chip["evo_advice"]
    # Other fields untouched
    assert chip["label"] == "cost spike"
    assert chip["severity"] == "warn"


def test_apply_overrides_unknown_id_drops_silently():
    """Override for an entity that's not in the glossary is logged +
    dropped — defensive against operator typos / stale config."""
    g = G.Glossary(
        version=1,
        chips=[{
            "id": "real_chip", "severity": "warn", "label": "x",
            "scope": "member_bot", "source_file": "x",
            "fires_when": "x", "evo_urgency_default": "defer",
            "evo_advice": "x",
        }],
    )
    merged = G.apply_overrides(g, {
        "chips": {"nonexistent": {"evo_advice": "ignored"}},
    })
    # Real chip is untouched, no extra entries added
    assert len(merged.chips) == 1
    assert merged.chips[0]["id"] == "real_chip"


def test_apply_overrides_propagates_to_producers_and_generators():
    """Overrides work for all three sections, not just chips."""
    g = G.Glossary(
        version=1,
        producers=[{
            "id": "p1", "source_file": "x", "monitors": "x",
            "signal_types": [], "sweep_resolves": True,
            "evo_urgency_default": "defer", "evo_advice": "x",
        }],
        generators=[{
            "id": "g1", "source_file": "x", "cadence": "daily",
            "audience": "pod_operator", "watches": "x",
            "proposes": "x", "evo_urgency_default": "defer",
            "evo_advice": "x",
        }],
    )
    merged = G.apply_overrides(g, {
        "producers": {"p1": {"evo_advice": "p1 override"}},
        "generators": {"g1": {"evo_advice": "g1 override"}},
    })
    assert merged.producers[0]["evo_advice"] == "p1 override"
    assert merged.generators[0]["evo_advice"] == "g1 override"


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────


def test_render_markdown_contains_required_sections(loaded_glossary):
    """The rendered markdown has the three top sections + the cheat
    sheet. Operator-facing structure stability — the model's reading
    of the document depends on the section headers being predictable."""
    md = G.render_markdown(loaded_glossary)
    assert "## Pod glossary" in md
    assert "### Tile chips" in md
    assert "### Signal producers" in md
    assert "### Proposal generators" in md
    assert "### Severity → urgency cheat sheet" in md


def test_render_markdown_includes_every_entity(loaded_glossary):
    """Sanity: every entity in the yaml ends up in the rendered output."""
    md = G.render_markdown(loaded_glossary)
    for c in loaded_glossary.chips:
        assert f"`{c['id']}`" in md, f"chip {c['id']} missing from rendered glossary"
    for p in loaded_glossary.producers:
        assert f"`{p['id']}`" in md, f"producer {p['id']} missing"
    for g in loaded_glossary.generators:
        assert f"`{g['id']}`" in md, f"generator {g['id']} missing"


def test_render_markdown_is_deterministic(loaded_glossary):
    """Same input → byte-identical output. Lets the deploy pipeline
    detect "no change" cheaply and lets CI diff committed
    GLOSSARY.md against re-rendered content."""
    a = G.render_markdown(loaded_glossary)
    b = G.render_markdown(loaded_glossary)
    assert a == b


def test_committed_glossary_md_matches_yaml(loaded_glossary):
    """The committed GLOSSARY.md must equal what the renderer produces
    from the yaml. Catches `glossary.yaml was updated but
    GLOSSARY.md wasn't regenerated` PRs at CI time — same drift
    surface ``evolve-admin gen-evo-glossary --check`` covers."""
    expected = G.render_markdown(loaded_glossary)
    on_disk = G.DEFAULT_GLOSSARY_OUTPUT.read_text()
    if expected != on_disk:
        pytest.fail(
            "Committed GLOSSARY.md is out of sync with glossary.yaml. "
            "Regenerate via:\n"
            "    evolve-admin gen-evo-glossary\n"
            "Then commit the result. The file lives at:\n"
            f"    {G.DEFAULT_GLOSSARY_OUTPUT}"
        )
