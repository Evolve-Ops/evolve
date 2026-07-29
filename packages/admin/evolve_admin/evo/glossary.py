"""evo glossary — load, override, render.

Spec §3.7 brittleness mitigation: the pod glossary in evo's AGENTS.md
is generated from a single source of truth at
``packages/analyzer/evolve_bot/glossary.yaml``. This module:

  * loads the YAML (the repo's canonical defaults)
  * applies a pod's ``network.json::evo_glossary_overrides`` overlay
  * renders the merged structure as markdown the deploy step
    concatenates onto evo's AGENTS.md

Why single-source: hand-curated content embedded in AGENTS.md drifts
silently when chips / producers / generators change in code. The
drift tests under ``tests/test_evo_glossary_drift.py`` enforce the
yaml is in lock-step with source — every chip in
``tile_metrics.py``, every signal producer call site, every
``analyzer/generators/<id>/`` subdir must have a yaml entry, and the
yaml can't include entries that aren't real.

Operator overrides (network.json::evo_glossary_overrides):

    evo_glossary_overrides:
      chips:
        cost_spike:
          evo_urgency_default: act
          evo_advice: "Our budget is tight; treat every spike as urgent."
      producers:
        bot_log_monitor:
          evo_advice: "We rotated to Sonnet — max_auth_failure is expected."

The repo's default glossary stays generic; pods customize without
forking the yaml. Overrides are merged per-entity at gen time; an
operator's `evo_urgency_default = act` wins over the repo's `defer`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Default location of the source YAML (relative to repo root).
DEFAULT_GLOSSARY_YAML = (
    Path(__file__).resolve().parents[3]
    / "analyzer" / "evolve_bot" / "glossary.yaml"
)

# Where the deploy step writes the rendered, override-applied markdown
# into evo's workspace. Read by evo at session_start as part of
# AGENTS.md (concatenated by install_bot_docs).
DEFAULT_GLOSSARY_OUTPUT = (
    Path(__file__).resolve().parents[3]
    / "analyzer" / "evolve_bot" / "GLOSSARY.md"
)


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Glossary:
    """In-memory representation of the parsed YAML, post-override.

    Each list contains dict entries (not strict dataclasses) so the
    schema can evolve without API churn. The dict keys are the same
    keys defined in glossary.yaml; the drift tests cross-check.
    """

    version: int
    chips: list[dict[str, Any]] = field(default_factory=list)
    producers: list[dict[str, Any]] = field(default_factory=list)
    generators: list[dict[str, Any]] = field(default_factory=list)

    def chip_ids(self) -> set[str]:
        return {c["id"] for c in self.chips}

    def producer_ids(self) -> set[str]:
        return {p["id"] for p in self.producers}

    def generator_ids(self) -> set[str]:
        return {g["id"] for g in self.generators}


# ── Loader ───────────────────────────────────────────────────────────────────


def load(yaml_path: Path | None = None) -> Glossary:
    """Parse the canonical YAML into a Glossary. No overrides applied
    yet — see ``apply_overrides``. Raises FileNotFoundError when the
    file is missing (a deploy-time invariant; CI tests cover it).
    """
    import yaml  # type: ignore[import]
    path = yaml_path or DEFAULT_GLOSSARY_YAML
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return Glossary(
        version=int(data.get("version", 0)),
        chips=list(data.get("chips") or []),
        producers=list(data.get("producers") or []),
        generators=list(data.get("generators") or []),
    )


# ── Override application ─────────────────────────────────────────────────────


def apply_overrides(
    glossary: Glossary,
    overrides: dict[str, Any] | None,
) -> Glossary:
    """Merge pod-specific overrides on top of the canonical glossary.

    Overrides shape (mirrors network.json::evo_glossary_overrides):

        {
          "chips": {
            "<chip_id>": { "evo_urgency_default": "...",
                           "evo_advice": "...",
                           "label": "..." (etc) }
          },
          "producers": { "<producer_id>": { ... } },
          "generators": { "<generator_id>": { ... } }
        }

    Unknown ids in the override are logged + dropped; never raises.
    The override values shallow-merge on top of the entry — a field
    not in the override is left as-is.
    """
    if not overrides:
        return glossary

    def _merge(entries: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]):
        if not isinstance(by_id, dict):
            return entries
        existing = {e["id"]: e for e in entries}
        for entity_id, fields in by_id.items():
            if entity_id not in existing:
                log.warning(
                    "evo_glossary_overrides: unknown id %r in %s — dropping",
                    entity_id, "section",
                )
                continue
            if not isinstance(fields, dict):
                continue
            existing[entity_id] = {**existing[entity_id], **fields}
        return list(existing.values())

    return Glossary(
        version=glossary.version,
        chips=_merge(glossary.chips, overrides.get("chips") or {}),
        producers=_merge(glossary.producers, overrides.get("producers") or {}),
        generators=_merge(glossary.generators, overrides.get("generators") or {}),
    )


# ── Markdown renderer ────────────────────────────────────────────────────────


def render_markdown(glossary: Glossary) -> str:
    """Render the merged glossary as a single markdown block suitable
    for concatenation onto AGENTS.md. Deterministic — same input
    produces byte-identical output (sort order is the YAML's
    declared order; we don't re-sort)."""
    lines: list[str] = []
    lines.append("## Pod glossary — what evo's terms actually mean")
    lines.append("")
    lines.append(
        "*Generated from `packages/analyzer/evolve_bot/glossary.yaml`. "
        "Do not hand-edit this section — regenerate via "
        "`evolve-admin gen-evo-glossary` and edit the yaml instead. "
        "Pod-specific overrides live in `network.json::"
        "evo_glossary_overrides`.*"
    )
    lines.append("")
    lines.append(
        "This section is the taught reference for evo's domain. When the "
        "operator asks about a *chip*, a *signal*, or a *proposal*, your "
        "answer should match how it actually works, not a plausible "
        "sound-alike interpretation."
    )
    lines.append("")

    # ── Chips ──────────────────────────────────────────────────────────────
    lines.append("### Tile chips (Dashboard pills)")
    lines.append("")
    lines.append(
        "Source: `analyzer/tile_metrics.py`. Tool: "
        "`pod_state.bots` returns them per bot in the `tile_chips` "
        "field."
    )
    lines.append("")

    member_chips = [c for c in glossary.chips if c.get("scope") == "member_bot"]
    primary_chips = [c for c in glossary.chips if c.get("scope") == "primary_bot"]
    other_chips = [
        c for c in glossary.chips
        if c.get("scope") not in ("member_bot", "primary_bot")
    ]

    if member_chips:
        lines.append("**Member-bot chips:**")
        lines.append("")
        for c in member_chips:
            lines.extend(_render_chip(c))
        lines.append("")

    if primary_chips:
        lines.append("**Primary-bot chips (appended when role=primary):**")
        lines.append("")
        for c in primary_chips:
            lines.extend(_render_chip(c))
        lines.append("")

    if other_chips:
        lines.append("**Other chips:**")
        lines.append("")
        for c in other_chips:
            lines.extend(_render_chip(c))
        lines.append("")

    # ── Producers ──────────────────────────────────────────────────────────
    lines.append("### Signal producers (the Alerts page sources)")
    lines.append("")
    lines.append(
        "Source: `signals.store.observe(producer=...)`. The Alerts page "
        "filters by producer. Tool: "
        "`pod_state.signals.firing(producer=...)`."
    )
    lines.append("")
    for p in glossary.producers:
        lines.extend(_render_producer(p))
    lines.append("")

    # ── Generators ─────────────────────────────────────────────────────────
    lines.append("### Proposal generators (the Recommendations page sources)")
    lines.append("")
    lines.append(
        "Source: `analyzer/generators/<id>/charter.yaml`. The "
        "Recommendations page filters by generator. Tool: "
        "`pod_state.proposals.pending`."
    )
    lines.append("")
    for g in glossary.generators:
        lines.extend(_render_generator(g))
    lines.append("")

    # ── Severity cheat sheet ───────────────────────────────────────────────
    lines.append("### Severity → urgency cheat sheet")
    lines.append("")
    lines.append("When evaluating any signal, audit finding, or proposal:")
    lines.append("")
    lines.append("| Severity | Default treatment |")
    lines.append("|----------|-------------------|")
    lines.append(
        "| critical | **Act now or stage a confirmable action "
        "immediately.** Don't defer; if you can't fix, escalate to "
        "operator with a concrete next step. |"
    )
    lines.append(
        "| alert / warn | **Surface and propose action; ok to defer "
        "with a reason.** If the operator says \"snooze it\", say a "
        "duration. |"
    )
    lines.append(
        "| info | **Read-only; only mention if directly asked.** "
        "Don't include in summaries unless the operator wants them. |"
    )
    lines.append("")
    lines.append(
        "The Alerts page has a \"show info-tier signals\" toggle — info "
        "signals are deliberately hidden by default."
    )
    lines.append("")
    return "\n".join(lines)


def _render_chip(c: dict[str, Any]) -> list[str]:
    sev = c.get("severity") or "info"
    label = c.get("label") or c.get("id")
    advice = (c.get("evo_advice") or "").strip()
    fires = (c.get("fires_when") or "").strip()
    urgency = c.get("evo_urgency_default") or "depends"
    out = [
        f"- **`{c['id']}`** ({sev}) — \"{label}\"",
    ]
    if fires:
        out.append(f"  Fires when: {fires}")
    if advice:
        out.append(f"  **Urgency: {urgency}.** {advice}")
    out.append("")
    return out


def _render_producer(p: dict[str, Any]) -> list[str]:
    monitors = (p.get("monitors") or "").strip()
    types = p.get("signal_types") or []
    sweep = p.get("sweep_resolves")
    advice = (p.get("evo_advice") or "").strip()
    out = [f"- **`{p['id']}`** — {monitors}"]
    if types:
        types_str = ", ".join(f"`{t}`" for t in types[:8])
        if len(types) > 8:
            types_str += f", … ({len(types) - 8} more)"
        out.append(f"  Signal types: {types_str}")
    if sweep is not None:
        out.append(f"  Sweep-resolves: {'yes' if sweep else 'no'}")
    if advice:
        out.append(f"  {advice}")
    out.append("")
    return out


def _render_generator(g: dict[str, Any]) -> list[str]:
    cadence = g.get("cadence") or "?"
    audience = g.get("audience") or "?"
    watches = (g.get("watches") or "").strip()
    proposes = (g.get("proposes") or "").strip()
    advice = (g.get("evo_advice") or "").strip()
    out = [f"- **`{g['id']}`** ({cadence}, audience={audience}) — {watches}"]
    if proposes:
        out.append(f"  Proposes: {proposes}")
    if advice:
        out.append(f"  {advice}")
    out.append("")
    return out


# ── Drift-detection helpers (used by the test suite + the CLI) ──────────────


def discover_chip_ids_in_source(source_root: Path) -> set[str]:
    """Find all chip ids declared in tile_metrics.py.

    The chip-emit pattern is ``chips.append({"id": "<id>", ...})``.
    Parsed via regex — a proper AST walk is overkill given the
    consistent pattern and would add complexity here. The drift test
    cross-checks this set against ``glossary.chip_ids()``.
    """
    import re
    path = source_root / "packages" / "analyzer" / "tile_metrics.py"
    text = path.read_text()
    return set(re.findall(r'"id"\s*:\s*"([a-z_]+)"', text))


def discover_producer_ids_in_source(source_root: Path) -> set[str]:
    """Find all producer ids the analyzer + admin emit signals under.

    Three patterns we recognize:
      * ``producer="X"``  — inline keyword in observe()
      * ``PRODUCER = "X"``  — module-level constant referenced as
        ``producer=PRODUCER`` (the permissions/plugins/hooks monitors)
      * ``producer=PRODUCER``  — sanity-cross-check the constant is used

    Walks both ``packages/analyzer/`` and ``packages/admin/evolve_admin/``
    — admin-side producers (anthropic_admin_ingest, openclaw_posture,
    slack_config_probe, infra_audit, app_structural_verifier,
    compliance_scan) emit Signals under their own names too and need
    glossary coverage just as much as analyzer-side producers.

    Test-fixture and operator-test ids are filtered out via the
    ``_FAKE_PRODUCER_IDS`` set.
    """
    import re
    roots = [
        source_root / "packages" / "analyzer",
        source_root / "packages" / "admin" / "evolve_admin",
    ]
    found: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            text = py.read_text(errors="ignore")
            # Inline producer= literals
            for m in re.finditer(r'producer\s*=\s*[\'"]([a-z_]+)[\'"]', text):
                found.add(m.group(1))
            # Module-level constant
            for m in re.finditer(
                r'^PRODUCER\s*=\s*[\'"]([a-z_]+)[\'"]',
                text, re.MULTILINE,
            ):
                found.add(m.group(1))
    return found - _FAKE_PRODUCER_IDS


# Producer ids that appear only in tests / examples and shouldn't be
# expected in the glossary. Update this set when adding new test
# fixtures that use literal producer names.
_FAKE_PRODUCER_IDS = frozenset({
    "brand_new", "default_producer", "explicit_override",
    "some_other_producer", "test", "x", "fake_producer",
    # ``content_scan`` is currently used by tests as a placeholder
    # producer for synthesized signals; it's not a real long-lived
    # producer in the analyzer. Remove from this set if/when
    # content_scan ships as a documented producer.
    "content_scan",
})


def discover_generator_ids_in_source(source_root: Path) -> set[str]:
    """Find all proposal-generator ids — subdirs under
    ``packages/analyzer/generators/``."""
    gen_root = source_root / "packages" / "analyzer" / "generators"
    if not gen_root.is_dir():
        return set()
    out: set[str] = set()
    for child in gen_root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        out.add(child.name)
    return out


def compute_drift(
    glossary: Glossary, source_root: Path
) -> dict[str, dict[str, list[str]]]:
    """Compare glossary.yaml against source. Returns a structured diff
    suitable for both the CLI's `--check` flag and the pytest assertion.

    Shape:

        {
          "chips": {"missing_from_glossary": [...], "extra_in_glossary": [...]},
          "producers": {...},
          "generators": {...},
        }

    Each list is empty when the side is in sync.
    """
    src_chips = discover_chip_ids_in_source(source_root)
    src_producers = discover_producer_ids_in_source(source_root)
    src_generators = discover_generator_ids_in_source(source_root)

    return {
        "chips": _diff_ids(src_chips, glossary.chip_ids()),
        "producers": _diff_ids(src_producers, glossary.producer_ids()),
        "generators": _diff_ids(src_generators, glossary.generator_ids()),
    }


def _diff_ids(in_source: set[str], in_yaml: set[str]) -> dict[str, list[str]]:
    return {
        "missing_from_glossary": sorted(in_source - in_yaml),
        "extra_in_glossary": sorted(in_yaml - in_source),
    }


def drift_is_clean(diff: dict[str, dict[str, list[str]]]) -> bool:
    """True when every section has empty lists in both directions."""
    for section in diff.values():
        if section["missing_from_glossary"] or section["extra_in_glossary"]:
            return False
    return True
