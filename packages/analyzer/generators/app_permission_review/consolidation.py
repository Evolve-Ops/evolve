"""generators.app_permission_review.consolidation — pod-aware second pass.

Cross-references each first-pass candidate finding against the bot's
full manifest set so removal proposals don't ignore the fact that a
permission an app no longer needs may still be needed by a sibling app.

Three outcomes per candidate (parent spec §5):

- **No sibling references it** — emit AS-IS.
- **A sibling declares it** — emit AS-IS but annotated *"still in
  effect via sibling app B."*
- **A sibling uses it but doesn't declare it** — convert to a MOVE
  proposal ("remove from app A; add to app B").

For ``*_missing_declaration`` findings (asymmetric: "app A should
declare X"), only outcomes 1 and 2 apply — outcome 2 becomes
"sibling app B already declares this; making it explicit on app A
makes the dependency explicit."

For ``*_overkill_wildcard`` findings, no consolidation — wildcards
are per-app concerns. Pass through unchanged.

See sub-spec docs/spec-app-permission-review-2026-05-26.md §"`app_permission_review` — second pass (pod-aware consolidation)".
"""

# identity: this module was SWEPT onto applications.app_identity.resolve_app_id in AL-1.4b (area 4c): ``_app_id``
# carried an ``id or instance_id`` chain and now calls the resolver. The two
# remaining mentions are its docstring naming what was removed.
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
    resolve_app_id,
)
from generators.app_permission_review.review import (
    ALL_MISSING_KINDS,
    ALL_OVERKILL_KINDS,
    ALL_UNUSED_KINDS,
    Finding,
    MAX_SCRIPT_BYTES,
    MAX_SCRIPTS_PER_APP,
    SCRIPT_EXTENSIONS,
    _file_paths,
    _has_wildcard,
    _read_script_bodies,
    _scripts_in_manifest,
    _wildcard_to_regex,
    _HOST_REGEX,
    _API_HOST_REGEX,
)


# Outcome kinds — annotation strings carried through the consolidation
# result so the proposal builder can choose the right framing.
OUTCOME_AS_IS = "as_is"
OUTCOME_SIBLING_DECLARES = "sibling_declares"
OUTCOME_MOVE_TO_SIBLING = "move_to_sibling"
OUTCOME_SIBLING_ALREADY_HAS = "sibling_already_declares"  # for *_missing_declaration


@dataclass
class ConsolidatedFinding:
    """Wraps a Finding with consolidation context — the proposal builder
    reads this to choose its framing without re-running the cross-reference."""
    finding: Finding
    outcome: str  # one of OUTCOME_*
    # For OUTCOME_SIBLING_DECLARES / OUTCOME_SIBLING_ALREADY_HAS:
    #   list of sibling app_ids that declare this resource.
    # For OUTCOME_MOVE_TO_SIBLING:
    #   exactly one sibling app_id (the destination).
    sibling_apps: list[str] = field(default_factory=list)


# ── Sibling index ────────────────────────────────────────────────────────────


@dataclass
class SiblingIndex:
    """Cross-app index for consolidation lookups.

    Two layers:
      - declared: (entry_kind, entry_value) → list[app_id] declaring it
      - used:     (entry_kind, entry_value) → list[app_id] grep-using it
                  (whether declared or not)
    """
    declared: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    used: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))

    def siblings_declaring(self, entry_kind: str, entry_value: str, exclude_app_id: str) -> list[str]:
        return [a for a in self.declared.get((entry_kind, entry_value), [])
                if a != exclude_app_id]

    def siblings_using_undeclared(
        self,
        entry_kind: str,
        entry_value: str,
        exclude_app_id: str,
    ) -> list[str]:
        """Sibling apps that USE the resource but don't DECLARE it."""
        declarers = set(self.declared.get((entry_kind, entry_value), []))
        users = self.used.get((entry_kind, entry_value), [])
        return [a for a in users if a != exclude_app_id and a not in declarers]


def _app_id(manifest: dict) -> str:
    """AL-1.4b: "which app is this?" via the ONE resolver.

    Was ``manifest.get("id") or manifest.get("instance_id")`` — a chain that
    skipped ``pkg_id`` entirely, so a gallery-installed app keyed the
    ``declared`` / ``used`` sibling maps under its slug while every already-
    swept reader resolved it to its package id.
    """
    return resolve_app_id(manifest) or "?"


def _collect_declared_entries(manifest: dict) -> list[tuple[str, str]]:
    """Every (kind, value) declared in this manifest's permissions block."""
    out: list[tuple[str, str]] = []
    perms = manifest.get("permissions") or {}
    if not isinstance(perms, dict):
        return out
    for kind in ("exec", "fs_read", "fs_write", "network_egress", "env"):
        for raw in (perms.get(kind) or []):
            if isinstance(raw, str) and raw.strip():
                out.append((kind, raw.strip()))
    return out


def _collect_used_entries(
    manifest: dict, workspace: Path,
) -> list[tuple[str, str]]:
    """Every (kind, value) the app's scripts grep-match for.

    For exec: paths declared in files[]/realized_files[] (those are
    "used by the app" by virtue of being part of it).

    For network_egress: hosts grep'd from script bodies.

    For fs_read / fs_write / env: paths grep'd from script bodies.
    These are weaker signals (substring grep) but match the necessity
    check's logic.
    """
    out: list[tuple[str, str]] = []

    # Scripts declared in files/realized_files = exec entries the app uses.
    for path in _scripts_in_manifest(manifest):
        out.append(("exec", path))

    scripts = _scripts_in_manifest(manifest)
    bodies = _read_script_bodies(workspace, scripts)

    # Hosts grep'd from bodies.
    for body in bodies.values():
        for m in _HOST_REGEX.finditer(body):
            out.append(("network_egress", m.group(1).lower()))
        for m in _API_HOST_REGEX.finditer(body):
            out.append(("network_egress", m.group(1).lower()))

    # fs_read / fs_write / env: we don't have an easy regex for "this
    # script reads/writes this path." Grep against declared sibling
    # paths is handled in the consolidation phase, which is when we
    # have the full set of (kind, value) tuples to check.
    # For the index build, we capture the script bodies themselves so
    # the consolidation phase can grep against them.
    return out


def build_sibling_index(
    all_manifests: Iterable[dict], workspace: Path,
) -> SiblingIndex:
    """Build the cross-app declared/used index for all manifests on a bot."""
    idx = SiblingIndex()

    # Pre-read every app's bodies once — the per-app body read in
    # _collect_used_entries plus a second pass to grep-match against
    # declared resources requires we have all bodies in hand.
    bodies_by_app: dict[str, dict[str, str]] = {}
    for m in all_manifests:
        app_id = _app_id(m)
        scripts = _scripts_in_manifest(m)
        bodies_by_app[app_id] = _read_script_bodies(workspace, scripts)

    # First pass: declared
    for m in all_manifests:
        app_id = _app_id(m)
        for kind, value in _collect_declared_entries(m):
            idx.declared[(kind, value)].append(app_id)

    # Second pass: used
    for m in all_manifests:
        app_id = _app_id(m)
        bodies = bodies_by_app.get(app_id, {})

        # exec from files/realized_files
        for script_path in _scripts_in_manifest(m):
            idx.used[("exec", script_path)].append(app_id)

        # network_egress from grep'd hosts
        for body in bodies.values():
            for m_re in _HOST_REGEX.finditer(body):
                idx.used[("network_egress", m_re.group(1).lower())].append(app_id)
            for m_re in _API_HOST_REGEX.finditer(body):
                idx.used[("network_egress", m_re.group(1).lower())].append(app_id)

    # For fs_read / fs_write / env: cross-app "uses without declaring"
    # is detected by grepping against the universe of declared (kind, value)
    # pairs across all manifests, since those are the resources anyone
    # might depend on. We populate "used" by scanning each app's bodies
    # for each declared resource string.
    for (kind, value), _declarers in list(idx.declared.items()):
        if kind not in ("fs_read", "fs_write", "env"):
            continue
        # Strip wildcards for grep — use the longest literal substring.
        needle = value
        if "*" in needle:
            literals = [s for s in re.split(r"[*?]", needle) if len(s) >= 3]
            if not literals:
                continue
            needle = max(literals, key=len)
        for app_id, bodies in bodies_by_app.items():
            for body in bodies.values():
                if needle in body:
                    idx.used[(kind, value)].append(app_id)
                    break  # one match per app is enough

    # Dedupe app lists (a single app may match a resource multiple times).
    for key, app_list in idx.declared.items():
        idx.declared[key] = sorted(set(app_list))
    for key, app_list in idx.used.items():
        idx.used[key] = sorted(set(app_list))

    return idx


# ── Consolidation per finding ────────────────────────────────────────────────


def _consolidate_unused(finding: Finding, idx: SiblingIndex) -> ConsolidatedFinding:
    """Spec §5 outcomes for a "narrow this declaration" candidate."""
    declaring_siblings = idx.siblings_declaring(
        finding.entry_kind, finding.entry_value, exclude_app_id=finding.app_id,
    )
    if declaring_siblings:
        return ConsolidatedFinding(
            finding=finding,
            outcome=OUTCOME_SIBLING_DECLARES,
            sibling_apps=declaring_siblings,
        )
    using_undeclared_siblings = idx.siblings_using_undeclared(
        finding.entry_kind, finding.entry_value, exclude_app_id=finding.app_id,
    )
    if using_undeclared_siblings:
        # Pick the first using sibling as the move destination. If there
        # are multiple, the operator can decide; the proposal mentions all.
        return ConsolidatedFinding(
            finding=finding,
            outcome=OUTCOME_MOVE_TO_SIBLING,
            sibling_apps=using_undeclared_siblings,
        )
    return ConsolidatedFinding(
        finding=finding,
        outcome=OUTCOME_AS_IS,
        sibling_apps=[],
    )


def _consolidate_missing(finding: Finding, idx: SiblingIndex) -> ConsolidatedFinding:
    """Spec §5 outcomes for a "this app should declare X" candidate."""
    declaring_siblings = idx.siblings_declaring(
        finding.entry_kind, finding.entry_value, exclude_app_id=finding.app_id,
    )
    if declaring_siblings:
        return ConsolidatedFinding(
            finding=finding,
            outcome=OUTCOME_SIBLING_ALREADY_HAS,
            sibling_apps=declaring_siblings,
        )
    return ConsolidatedFinding(
        finding=finding,
        outcome=OUTCOME_AS_IS,
        sibling_apps=[],
    )


def _consolidate_overkill(finding: Finding) -> ConsolidatedFinding:
    """Overkill findings are per-app concerns; pass through unchanged."""
    return ConsolidatedFinding(
        finding=finding,
        outcome=OUTCOME_AS_IS,
        sibling_apps=[],
    )


def consolidate(
    candidates: Iterable[Finding],
    all_manifests: Iterable[dict],
    workspace: Path,
) -> list[ConsolidatedFinding]:
    """Run the pod-aware second pass on a list of first-pass candidates.

    Empty candidate list → empty result. Empty manifest set → every
    candidate passes through as ``OUTCOME_AS_IS`` (no siblings to cross-
    reference against).
    """
    candidates = list(candidates)
    if not candidates:
        return []

    all_manifests = list(all_manifests)
    idx = build_sibling_index(all_manifests, workspace)

    out: list[ConsolidatedFinding] = []
    for c in candidates:
        if c.kind in ALL_UNUSED_KINDS:
            out.append(_consolidate_unused(c, idx))
        elif c.kind in ALL_MISSING_KINDS:
            out.append(_consolidate_missing(c, idx))
        elif c.kind in ALL_OVERKILL_KINDS:
            out.append(_consolidate_overkill(c))
        else:
            # Unknown finding kind — pass through as-is so we don't drop it.
            out.append(ConsolidatedFinding(finding=c, outcome=OUTCOME_AS_IS))
    return out
