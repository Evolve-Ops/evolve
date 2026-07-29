"""Local skill retirement detector — Phase 1.5b slice of spec §14.2.

When upstream Evolve ships a tool that obviates a pod-local skill,
the operator wants to know — but the system MUST NOT auto-delete
the skill (operator may have customized the recipe beyond what the
tool offers, OR want the skill as documentation alongside the tool).

This module scans for SKILL.md files under:

  - ``agents.defaults.skills.load.extraDirs[]``  (Evolve-shipped)
  - ``{workspaceDir}/skills/local/``              (pod-local)

…parses each skill's frontmatter, reads ``metadata.evolve.obviated_by``,
and cross-checks against the current tool registry. Matches surface
as RetirementCandidate records the deploy step logs for the operator
to act on (or ignore).

obviated_by semantics (spec §14.2):

  - Exact match — ``action.bot.set_model`` against the tool name.
  - Prefix match — ``action.bot.`` matches any tool starting with that.
    Operator can declare "any tool in this dotted prefix obviates me"
    without enumerating each.

Skills authored without ``obviated_by`` are silently skipped (the
field is recommended for evo-authored skills, optional for operator-
authored).

The detector returns candidates; it doesn't delete or rewrite
anything. Per ``feedback_prelaunch_architect_properly`` and spec
§14.2's "Never automatic deletion. Operator decides" mandate, the
delete path is an explicit operator action (future
``evolve-admin retire-local-skill`` CLI).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetirementCandidate:
    """One skill whose ``obviated_by`` field matches the current tool registry.

    ``source`` is a short tag for the deploy log — "evolve-shipped"
    when the skill came from an extraDirs source, "pod-local" when
    it's under ``{workspaceDir}/skills/local/``.
    """

    skill_name: str
    skill_path: str
    obviated_by: str            # the declared marker on the skill
    matching_tool: str           # the actual tool that matched (= obviated_by
                                 # for exact match, or the resolved name for
                                 # prefix match)
    source: str                  # "evolve-shipped" | "pod-local"
    match_mode: str              # "exact" | "prefix"


@dataclass(frozen=True)
class _ScannedSkill:
    """Internal: one parsed SKILL.md ready for retirement checking."""

    name: str
    path: Path
    obviated_by: str | None
    source: str


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a SKILL.md.

    Same convention as ``evolve_admin.evo.guide._split_frontmatter``
    — kept independent so this module doesn't import the unrelated
    Guide schema. Returns ({}, body) when no frontmatter is present.
    Returns None when YAML parsing fails (caller skips such skills).
    """
    text = raw.lstrip("﻿")  # strip BOM if present
    if not text.startswith("---"):
        return {}, text

    rest = text[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    fence_idx = rest.find("\n---")
    if fence_idx == -1:
        return {}, text  # malformed — treat as no frontmatter
    fm_text = rest[:fence_idx]

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        log.debug("PyYAML unavailable; skipping frontmatter parse")
        return {}, ""

    try:
        loaded = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except Exception:  # noqa: BLE001  -- yaml errors of any shape
        return {}, ""
    if not isinstance(loaded, dict):
        return {}, ""
    return loaded, ""


def _obviated_by_from_frontmatter(fm: dict[str, Any]) -> str | None:
    """Return ``metadata.evolve.obviated_by`` or None.

    Defensive walk — operator-authored skills may have any frontmatter
    shape; we only extract what we recognize and silently ignore the
    rest.
    """
    md = fm.get("metadata") if isinstance(fm, dict) else None
    if not isinstance(md, dict):
        return None
    ev = md.get("evolve")
    if not isinstance(ev, dict):
        return None
    raw = ev.get("obviated_by")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _name_from_frontmatter(fm: dict[str, Any], fallback: str) -> str:
    """Extract the OC-loader skill name from frontmatter.

    Falls back to the parent directory name (which is what OC's loader
    uses as a default when the frontmatter omits ``name``).
    """
    raw = fm.get("name") if isinstance(fm, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback


def _scan_dir_for_skills(root: Path, source: str) -> list[_ScannedSkill]:
    """Walk ``root`` looking for SKILL.md files and parse each one's
    frontmatter into a ``_ScannedSkill``. Skips broken files silently.

    Recursive: OC's loader scans the whole subtree under each source,
    so we mirror that.
    """
    out: list[_ScannedSkill] = []
    if not root.is_dir():
        return out
    for path in root.rglob("SKILL.md"):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = _split_frontmatter(raw)
        name = _name_from_frontmatter(fm, fallback=path.parent.name)
        obviated_by = _obviated_by_from_frontmatter(fm)
        out.append(_ScannedSkill(
            name=name, path=path,
            obviated_by=obviated_by, source=source,
        ))
    return out


def _resolve_match(
    obviated_by: str, tool_names: Iterable[str],
) -> tuple[str, str] | None:
    """Return ``(matching_tool, match_mode)`` if obviated_by maps to a
    real tool; else None.

    Exact-match check first (most specific). Then prefix-match for
    declarations ending in ``.`` — e.g. ``action.bot.`` matches any
    tool with that prefix. Trailing dot is required for prefix mode
    so a typo'd ``action.bot`` (no dot) doesn't silently degrade to a
    prefix match and over-claim coverage.
    """
    tool_set = list(tool_names)
    # Exact match
    if obviated_by in tool_set:
        return obviated_by, "exact"
    # Prefix match — only when the declaration explicitly ends in '.'.
    if obviated_by.endswith("."):
        for t in tool_set:
            if t.startswith(obviated_by):
                return t, "prefix"
    return None


def detect_retirement_candidates(
    *,
    extra_dirs: Iterable[Path],
    workspace_dir: Path | None,
    tool_names: Iterable[str],
) -> list[RetirementCandidate]:
    """Return retirement candidates given the current tool registry.

    ``extra_dirs`` — Evolve-shipped + operator-extra skill source
    dirs (typically the contents of
    ``agents.defaults.skills.load.extraDirs``).

    ``workspace_dir`` — bot's OC workspace root. If set, we also scan
    ``{workspace_dir}/skills/local/``. ``None`` skips that source.

    ``tool_names`` — flat iterable of the currently-registered tool
    names from ``evolve_admin.evo.tools.all_tools()``.

    Each candidate has all the data the deploy log line / CLI listing
    needs — skill name, path on disk, the matched tool, and a source
    tag ("evolve-shipped" vs "pod-local") so the operator can prioritize.
    """
    scanned: list[_ScannedSkill] = []
    for d in extra_dirs:
        scanned.extend(_scan_dir_for_skills(Path(d), source="evolve-shipped"))
    if workspace_dir is not None:
        local_root = Path(workspace_dir) / "skills" / "local"
        scanned.extend(_scan_dir_for_skills(local_root, source="pod-local"))

    candidates: list[RetirementCandidate] = []
    tool_list = list(tool_names)
    for skill in scanned:
        if not skill.obviated_by:
            continue
        match = _resolve_match(skill.obviated_by, tool_list)
        if match is None:
            continue
        matching_tool, mode = match
        candidates.append(RetirementCandidate(
            skill_name=skill.name,
            skill_path=str(skill.path),
            obviated_by=skill.obviated_by,
            matching_tool=matching_tool,
            source=skill.source,
            match_mode=mode,
        ))
    return candidates


def resolve_skill_sources_for_bot(
    bot_id: str, *, bot_user: str,
) -> tuple[list[Path], Path | None]:
    """Resolve a bot's current skill source dirs by reading openclaw.json.

    Returns ``(extra_dirs, workspace_dir)`` for use as
    ``detect_retirement_candidates`` args. Both halves are
    defensively-typed:

    * ``extra_dirs`` — the strings in
      ``agents.defaults.skills.load.extraDirs`` parsed into Paths;
      empty list when absent or malformed.

    * ``workspace_dir`` — the bot's OC workspace as Path, or None if
      it can't be resolved.

    Used by the deploy hook (Phase 1.5b §14.2 retirement detector).
    Standalone helper so tests can stub it out without going through
    the deploy module's heavier import graph.
    """
    extra_dirs: list[Path] = []
    workspace_dir: Path | None = None

    # Workspace resolution mirrors evolve_admin.config.get_bot_workspace,
    # imported lazily so this module loads cleanly during isolated tests
    # that don't have the full admin import graph wired in.
    try:
        from evolve_admin.config import get_bot_workspace
        workspace_dir = get_bot_workspace(bot_id, user=bot_user)
    except Exception:  # noqa: BLE001
        workspace_dir = None

    # extraDirs lives in the bot's openclaw.json. The deploy_integration
    # module has a reader with the right fallback semantics (direct read
    # → sudo /bin/cat). Reuse it rather than re-implementing.
    try:
        from evolve_admin.evo.tools.deploy_integration import _read_bot_oc_config
        oc, _err = _read_bot_oc_config(bot_id, bot_user)
        if isinstance(oc, dict):
            cur: Any = oc
            for seg in ("agents", "defaults", "skills", "load", "extraDirs"):
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(seg)
            if isinstance(cur, list):
                extra_dirs = [Path(p) for p in cur if isinstance(p, str) and p]
    except Exception:  # noqa: BLE001
        pass

    return extra_dirs, workspace_dir


def format_deploy_notice(candidate: RetirementCandidate) -> str:
    """Render a candidate as a one-line operator-facing deploy notice.

    Format matches the spec's example: "local skill ``set-bot-model``
    is obviated by the now-available ``action.bot.set_model`` tool…"
    The CLI command name is forward-looking; the CLI itself is a
    later 1.5b slice.
    """
    return (
        f"{candidate.source} skill `{candidate.skill_name}` is obviated "
        f"by the now-available `{candidate.matching_tool}` tool "
        f"(match={candidate.match_mode}). Run "
        f"`evolve-admin retire-local-skill {candidate.skill_name}` to "
        f"delete it, or leave it as belt-and-suspenders."
    )
