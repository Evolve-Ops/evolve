"""
evolve_admin.bot_templates — Bot template framework.

Substrate that lets templates declare both **skill dependencies** and
**application installations** for new bots.

This is the framework layer behind ``evolve-admin deploy --from-template
<template-name> <bot-name>``. It is intentionally substrate-neutral on
the template format: templates declare *what* the bot needs (skills,
applications, voice, channel pattern). The loader's adapter translates
to OpenClaw-specific install steps today; tomorrow a different adapter
could target Hermes or another substrate without changing the template
files.

Module layout
─────────────
    loader.py      — read template.yaml + AGENTS.md.template + SOUL.md.template
    validator.py   — schema + invariant checks on a loaded template
    resolver.py    — skill dependency resolution against a bot's installed plugins
    provisioner.py — render templates with substituted vars + queue installs
    errors.py      — exception types

Templates live in two places:
    Builtin:  {repo_root}/gallery/bot-templates/<template-name>/
    Imported: {shared_dir}/bot-templates/imported/<template-name>/  (future)

See: spec 6 of evolve-mvp-sprint.md (C1.a Bot template framework).
"""

from __future__ import annotations

from .errors import (
    TemplateError,
    TemplateNotFoundError,
    TemplateValidationError,
    SkillResolutionError,
)
from .loader import (
    BotTemplate,
    SkillSpec,
    ApplicationSpec,
    load_template,
    list_templates,
    builtin_templates_dir,
)
from .resolver import (
    SkillResolution,
    ResolvedSkill,
    resolve_skills,
)
from .validator import (
    ValidationReport,
    validate_template,
)
from .provisioner import (
    ApplicationInstall,
    EmbeddedAppPlan,
    ProvisionPlan,
    RenderedFile,
    plan_provision,
    render_template_vars,
    _derive_briefing_hour_minute,
    _tz_offset_minutes,
    _convert_time_to_system_local,
)
from .app_blueprint import (
    BlueprintFile,
    ParsedBlueprint,
    classify_path,
    parse_app_blueprint,
)
from .cli_integration import (
    BuildPlanResult,
    EmbeddedAppApplyResult,
    LaunchdAdapter,
    apply_embedded_app,
    build_plan,
    summarize_plan,
    write_file_to_bot_workspace,
)

__all__ = [
    # errors
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateValidationError",
    "SkillResolutionError",
    # loader
    "BotTemplate",
    "SkillSpec",
    "ApplicationSpec",
    "load_template",
    "list_templates",
    "builtin_templates_dir",
    # resolver
    "SkillResolution",
    "ResolvedSkill",
    "resolve_skills",
    # validator
    "ValidationReport",
    "validate_template",
    # provisioner
    "ApplicationInstall",
    "EmbeddedAppPlan",
    "ProvisionPlan",
    "RenderedFile",
    "plan_provision",
    "render_template_vars",
    # provisioner derived-var helpers (V1.1-2)
    "_derive_briefing_hour_minute",
    "_tz_offset_minutes",
    "_convert_time_to_system_local",
    # app blueprint (V1.1-1)
    "BlueprintFile",
    "ParsedBlueprint",
    "classify_path",
    "parse_app_blueprint",
    # cli integration
    "BuildPlanResult",
    "EmbeddedAppApplyResult",
    "LaunchdAdapter",
    "apply_embedded_app",
    "build_plan",
    "summarize_plan",
    "write_file_to_bot_workspace",
]
