"""
loader.py — Read a bot template from disk into structured objects.

A template directory looks like::

    gallery/bot-templates/<name>/
        template.yaml             ← required manifest (skills + applications + meta)
        AGENTS.md.template        ← optional instructions baseline
        SOUL.md.template          ← optional personality baseline
        exec-approvals.template.json  ← optional default exec-approval baseline
        apps/                     ← optional embedded app blueprints (.json package files)

The loader is intentionally permissive about *which* optional files exist —
the validator is the gate. The loader's job is to surface the on-disk
state as Python objects without interpreting absences as errors.

Template-format design notes
────────────────────────────
This is substrate-neutral by design (per
project_evolve_substrate_strategy memory). Template fields like ``skill``
identifiers, version pins, and ``provides`` capabilities do not encode
OpenClaw-specific concepts; the resolver/provisioner adapt to OpenClaw at
install time. Tomorrow a different adapter could target Hermes or
ClawHub-style skill registries by swapping the resolver implementation
without changing template files.

agentskills.io vocabulary alignment:
  - ``skill.id`` is a stable identifier (the same shape ClawHub uses for
    skill packages, e.g. ``"gog"``, ``"weather"``, ``"slack"``).
  - ``skill.version`` is a version range or exact version (``">=1.0"``,
    ``"1.2.3"``); the resolver treats it as advisory until standards
    settle.
  - ``skill.source`` is optional and names where to fetch the skill from
    (``"openclaw-plugin"``, ``"clawhub"``, ``"local"``). Today only
    ``openclaw-plugin`` is implemented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import TemplateError, TemplateNotFoundError


# ── Path resolution ───────────────────────────────────────────────────────────

# This file lives at:
#   packages/admin/evolve_admin/bot_templates/loader.py
# Repo root is 4 parents up:
#   bot_templates → evolve_admin → admin → packages → repo root
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]


def builtin_templates_dir() -> Path:
    """Return the directory holding builtin bot templates.

    Lives at ``{repo_root}/gallery/bot-templates/`` so it ships with the
    evolve repo alongside the app gallery.
    """
    return _REPO_ROOT / "gallery" / "bot-templates"


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillSpec:
    """A skill the template declares as a dependency.

    Fields mirror agentskills.io vocabulary so the same template can target
    multiple substrates over time.

    Attributes:
        id:       Stable skill identifier (e.g. ``"gog"``, ``"weather"``).
        version:  Version pin or range. ``None`` means "any installed".
        source:   Where the skill should be installed from. ``None`` means
                  "use the default adapter for this substrate". Today only
                  ``"openclaw-plugin"`` is recognised by the resolver.
        config:   Substrate-neutral config dict the resolver passes through
                  to the install step (e.g. OAuth scopes for ``gog``).
        optional: If True, missing-and-uninstallable does not block
                  provisioning — it surfaces as a warning instead.
    """

    id: str
    version: str | None = None
    source: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    optional: bool = False


@dataclass(frozen=True)
class ApplicationSpec:
    """An application the template declares should be installed on bot creation.

    Two flavours:
      1. **Gallery reference** — ``app_id`` points at an app in
         ``gallery/<name>/<pkg_id>.json`` (the existing builtin gallery).
      2. **Embedded blueprint** — ``embedded_path`` is a path relative to
         the template's ``apps/`` directory.

    Exactly one of ``app_id`` or ``embedded_path`` must be set; the
    validator enforces this.

    Attributes:
        name:           Human-readable label for the application.
        app_id:         Gallery app id (e.g. ``"app_task_manager"``).
                        identity: see resolve_app_id — this is a template's
                        DECLARATION of an app, read straight off the template
                        manifest, not an identity resolved out of an installed
                        app manifest, so AL-1.4b leaves it alone. Note the
                        namespace it lives in is the gallery slug, which is NOT
                        what ``resolve_app_id`` returns for a stamped manifest
                        (that is the ``p-`` pkg_id) — see the matching note in
                        ``pod_templates/extractor.bot_evidence_from_files``.
        embedded_path:  Relative path under the template's ``apps/`` dir.
        skill_deps:     Skill ids the application needs to run. Must be a
                        subset of the template's ``skills`` list (enforced
                        by the validator).
        config:         Pass-through config the installer hands to the app.
    """

    name: str
    app_id: str | None = None
    embedded_path: str | None = None
    skill_deps: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BotTemplate:
    """A fully-loaded bot template, ready for validation / resolution.

    Attributes:
        name:               Stable id (matches the directory name).
        path:               Absolute path to the template directory.
        display_name:       Human-readable name (defaults to ``name``).
        description:        One-paragraph description.
        voice_preset:       Optional voice id (a key the personality layer
                            understands). ``None`` means "use SOUL.md.template
                            verbatim".
        channel_pattern:    Hint about the primary integration shape, e.g.
                            ``"any-messaging"`` or ``"slack"`` or
                            ``"telegram"``. Advisory — the wizard/UI may
                            override.
        skills:             Skill dependencies.
        applications:       Applications to install.
        template_vars:      Documented variables the template body uses
                            (``bot_id``, ``user_name``, ``time_zone``, …).
                            Each entry maps name → {description, required,
                            default}. Used by the wizard/UI to prompt for
                            missing values.
        agents_md_template: Raw text of AGENTS.md.template, or None.
        soul_md_template:   Raw text of SOUL.md.template, or None.
        exec_approvals:     Parsed exec-approvals baseline, or None.
        embedded_apps:      Map of relative path → parsed app manifest for
                            each .json file under apps/.
        raw:                The parsed template.yaml dict (for forward-
                            compat / debugging).
    """

    name: str
    path: Path
    display_name: str
    description: str
    voice_preset: str | None
    channel_pattern: str | None
    skills: tuple[SkillSpec, ...]
    applications: tuple[ApplicationSpec, ...]
    template_vars: dict[str, dict[str, Any]]
    agents_md_template: str | None
    soul_md_template: str | None
    exec_approvals: dict[str, Any] | None
    embedded_apps: dict[str, dict[str, Any]]
    raw: dict[str, Any]


# ── Loader ────────────────────────────────────────────────────────────────────


def load_template(
    name: str,
    *,
    templates_dir: Path | None = None,
) -> BotTemplate:
    """Read a template from disk into a :class:`BotTemplate`.

    Args:
        name:           Template directory name (e.g. ``"morning-briefing"``).
        templates_dir:  Override the search root. Defaults to
                        :func:`builtin_templates_dir`.

    Raises:
        TemplateNotFoundError: if no directory at ``<templates_dir>/<name>``
            or no ``template.yaml`` inside it.
        TemplateError: if ``template.yaml`` is unreadable or malformed.

    Note: this returns the on-disk representation *as-is*. Run
    :func:`validate_template` separately before using a template — the
    loader does not enforce invariants (intentional: simpler error
    messages, lazy validation, support for partial templates during
    authoring).
    """
    root = templates_dir or builtin_templates_dir()
    tdir = root / name
    if not tdir.is_dir():
        raise TemplateNotFoundError(
            f"template {name!r} not found at {tdir} "
            f"(searched root: {root})"
        )

    manifest_path = tdir / "template.yaml"
    if not manifest_path.is_file():
        raise TemplateNotFoundError(
            f"template {name!r} has no template.yaml "
            f"(expected at {manifest_path})"
        )

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise TemplateError(f"cannot read {manifest_path}: {e}") from e

    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise TemplateError(f"{manifest_path} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise TemplateError(
            f"{manifest_path} top-level must be a mapping, got {type(raw).__name__}"
        )

    # ── Required meta fields ──────────────────────────────────────────────────
    display_name = str(raw.get("display_name") or raw.get("name") or name)
    description = str(raw.get("description") or "").strip()
    voice_preset = raw.get("voice_preset") or None
    channel_pattern = raw.get("channel_pattern") or None
    voice_preset = str(voice_preset) if voice_preset is not None else None
    channel_pattern = str(channel_pattern) if channel_pattern is not None else None

    # ── Skills ────────────────────────────────────────────────────────────────
    skills_raw = raw.get("skills") or []
    if not isinstance(skills_raw, list):
        raise TemplateError(
            f"{manifest_path}: 'skills' must be a list, got {type(skills_raw).__name__}"
        )
    skills: list[SkillSpec] = []
    for i, entry in enumerate(skills_raw):
        if not isinstance(entry, dict):
            raise TemplateError(
                f"{manifest_path}: skills[{i}] must be a mapping, "
                f"got {type(entry).__name__}"
            )
        sid = entry.get("id")
        if not sid or not isinstance(sid, str):
            raise TemplateError(
                f"{manifest_path}: skills[{i}].id is required (string)"
            )
        skills.append(SkillSpec(
            id=sid,
            version=(str(entry["version"]) if entry.get("version") is not None else None),
            source=(str(entry["source"]) if entry.get("source") is not None else None),
            config=dict(entry.get("config") or {}),
            optional=bool(entry.get("optional", False)),
        ))

    # ── Applications ──────────────────────────────────────────────────────────
    apps_raw = raw.get("applications") or []
    if not isinstance(apps_raw, list):
        raise TemplateError(
            f"{manifest_path}: 'applications' must be a list, "
            f"got {type(apps_raw).__name__}"
        )
    applications: list[ApplicationSpec] = []
    for i, entry in enumerate(apps_raw):
        if not isinstance(entry, dict):
            raise TemplateError(
                f"{manifest_path}: applications[{i}] must be a mapping, "
                f"got {type(entry).__name__}"
            )
        app_name = entry.get("name")
        if not app_name or not isinstance(app_name, str):
            raise TemplateError(
                f"{manifest_path}: applications[{i}].name is required (string)"
            )
        skill_deps_raw = entry.get("skill_deps") or []
        if not isinstance(skill_deps_raw, list):
            raise TemplateError(
                f"{manifest_path}: applications[{i}].skill_deps must be a list"
            )
        applications.append(ApplicationSpec(
            name=app_name,
            app_id=(str(entry["app_id"]) if entry.get("app_id") else None),
            embedded_path=(
                str(entry["embedded_path"]) if entry.get("embedded_path") else None
            ),
            skill_deps=tuple(str(s) for s in skill_deps_raw),
            config=dict(entry.get("config") or {}),
        ))

    # ── Template vars ─────────────────────────────────────────────────────────
    vars_raw = raw.get("template_vars") or {}
    if not isinstance(vars_raw, dict):
        raise TemplateError(
            f"{manifest_path}: 'template_vars' must be a mapping, "
            f"got {type(vars_raw).__name__}"
        )
    template_vars: dict[str, dict[str, Any]] = {}
    for vname, vspec in vars_raw.items():
        if not isinstance(vspec, dict):
            # Accept short form: name: "description"
            template_vars[str(vname)] = {"description": str(vspec)}
        else:
            template_vars[str(vname)] = dict(vspec)

    # ── Optional companion files ──────────────────────────────────────────────
    agents_md = _read_optional_text(tdir / "AGENTS.md.template")
    soul_md = _read_optional_text(tdir / "SOUL.md.template")

    exec_approvals: dict[str, Any] | None = None
    exec_path = tdir / "exec-approvals.template.json"
    if exec_path.is_file():
        try:
            exec_approvals = json.loads(exec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise TemplateError(
                f"{exec_path} is not valid JSON: {e}"
            ) from e

    # ── Embedded apps ─────────────────────────────────────────────────────────
    embedded_apps: dict[str, dict[str, Any]] = {}
    apps_dir = tdir / "apps"
    if apps_dir.is_dir():
        for app_file in sorted(apps_dir.glob("*.json")):
            try:
                embedded_apps[app_file.name] = json.loads(
                    app_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as e:
                raise TemplateError(
                    f"embedded app {app_file} is not valid JSON: {e}"
                ) from e

    return BotTemplate(
        name=name,
        path=tdir,
        display_name=display_name,
        description=description,
        voice_preset=voice_preset,
        channel_pattern=channel_pattern,
        skills=tuple(skills),
        applications=tuple(applications),
        template_vars=template_vars,
        agents_md_template=agents_md,
        soul_md_template=soul_md,
        exec_approvals=exec_approvals,
        embedded_apps=embedded_apps,
        raw=raw,
    )


def list_templates(
    *, templates_dir: Path | None = None,
) -> list[str]:
    """Return the names of all template directories under ``templates_dir``.

    A directory is considered a template iff it contains a
    ``template.yaml`` file. Silently skips entries that don't qualify.
    """
    root = templates_dir or builtin_templates_dir()
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "template.yaml").is_file():
            out.append(entry.name)
    return out


# ── Internal helpers ──────────────────────────────────────────────────────────


def _read_optional_text(path: Path) -> str | None:
    """Read a text file if it exists. Return None if absent.

    Raises TemplateError on read failure (existence + permission failure
    are distinct from missing-optional-file).
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise TemplateError(f"cannot read {path}: {e}") from e
