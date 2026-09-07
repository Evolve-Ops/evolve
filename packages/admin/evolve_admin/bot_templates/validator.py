"""
validator.py — Invariant checks on a loaded :class:`BotTemplate`.

Separated from the loader so callers can either:
  - Load and validate eagerly (``validate_template(load_template(name))``)
  - Inspect partial/broken templates without raising

The validator enforces structural invariants only — *can this template
be provisioned?* not *is this template the one you want?*. Configuration
correctness (e.g. "is the OAuth scope list reasonable") belongs to the
relevant skill installer, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import channel_registry as _channel_registry
from .errors import TemplateValidationError
from .loader import BotTemplate
from .resolver import known_skill_ids


# ── Constants ─────────────────────────────────────────────────────────────────

# Allowed shape for skill id / template name / application id (lowercase
# kebab/snake, can contain digits). Mirrors the conventions used by
# ClawHub and the existing app gallery's pkg id space.
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Recognised skill sources. Today only ``openclaw-plugin`` is wired up
# in the resolver. Others are reserved for future adapters; declaring
# them is allowed (validator warns), provisioning them will fail loudly.
_KNOWN_SKILL_SOURCES = frozenset({
    "openclaw-plugin",  # current substrate
    "clawhub",          # agentskills.io-aligned registry (future)
    "local",            # bundled with the template (future)
})

# Short hint string for the unknown-skill warning. Built lazily because
# the resolver's registries are populated at import time.
_SKILL_NEIGHBORHOOD_HINT = (
    "see resolver._SKILL_TO_OC_PLUGIN, _SKILL_ADAPTERS, _MANUAL_SKILLS"
)


# Sentinels that are not channels: "target whatever messaging the bot has"
# and "this template needs no channel". They have no registry row because
# they name a policy, not a platform.
_CHANNEL_PATTERN_SENTINELS = frozenset({"any-messaging", "none"})

# Recognised channel patterns = the sentinels plus every registry channel.
# Advisory list — anything not on it produces a warning, not an error, so
# templates can target new channels without a code change.
#
# WIDENING (deliberate, M1-B1): the hand-maintained set covered 5 of the 9
# registry channels, so a template targeting WhatsApp / iMessage / SMS /
# webhook drew a spurious "unknown channel" warning despite those being
# genuinely supported. Warn-only surface, so the widening removes false
# positives and blocks nothing.
_KNOWN_CHANNEL_PATTERNS = _CHANNEL_PATTERN_SENTINELS | _channel_registry.known_ids()


# ── Report ────────────────────────────────────────────────────────────────────


@dataclass
class ValidationReport:
    """Collected errors + warnings from validating a template.

    ``errors`` block provisioning; ``warnings`` are informational.
    """

    template_name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        """Raise :class:`TemplateValidationError` if any errors exist."""
        if self.errors:
            raise TemplateValidationError(self.template_name, self.errors)


# ── Validator ─────────────────────────────────────────────────────────────────


def validate_template(template: BotTemplate) -> ValidationReport:
    """Walk a loaded template, accumulating structural problems.

    Checks performed:
      1. Template name is a valid identifier (kebab/snake/digits, leading letter).
      2. ``description`` is non-empty (warning if missing — useful for UI).
      3. Skill ids are unique + valid identifiers.
      4. Skill sources, if specified, are known (warning for unknown).
      5. Application names are unique.
      6. Each application has exactly one of ``app_id`` / ``embedded_path``.
      7. Application skill_deps are all declared in the template's skills list.
      8. Embedded app paths actually exist in ``embedded_apps``.
      9. Template var entries are well-formed (description present).
     10. AGENTS.md / SOUL.md placeholders only reference declared vars
         (warning if they reference undeclared vars — could be intentional
         for vars resolved at runtime by the wizard, but worth flagging).
    """
    r = ValidationReport(template_name=template.name)

    # 1. Template name shape
    if not _ID_RE.match(template.name):
        r.errors.append(
            f"template name {template.name!r} must match "
            f"{_ID_RE.pattern!r} (lowercase, leading letter, "
            f"digits/dash/underscore only)"
        )

    # 2. Description
    if not template.description:
        r.warnings.append(
            "template has no 'description' — UI/CLI cannot present a useful summary"
        )

    # 3. Skill ids unique + well-formed + known
    seen_skill_ids: set[str] = set()
    declared_skills: set[str] = set()
    known_ids = known_skill_ids()
    for i, s in enumerate(template.skills):
        declared_skills.add(s.id)
        if s.id in seen_skill_ids:
            r.errors.append(f"skill {s.id!r} declared more than once")
        seen_skill_ids.add(s.id)
        if not _ID_RE.match(s.id):
            r.errors.append(
                f"skills[{i}].id {s.id!r} must match {_ID_RE.pattern!r}"
            )
        # 3a. Unknown-skill-id warning. Fall-through behaviour (treat the id
        # as the plugin name verbatim) means typos like ``g0g`` for ``gog``
        # are silently accepted and the resolver later marks the phantom
        # plugin "unsupported" — which is correct but the validation-time
        # warning saves a round-trip for template authors.
        if s.id not in known_ids:
            r.warnings.append(
                f"skill {s.id!r} is not in the known-skill map "
                f"({_SKILL_NEIGHBORHOOD_HINT}). Deploy will treat it as "
                f"a literal plugin name; if it's meant to be a community "
                f"skill or app-provided integration, this may fail at "
                f"install time. Add it to "
                f"`bot_templates.resolver._SKILL_TO_OC_PLUGIN`, "
                f"`_SKILL_ADAPTERS`, or `_MANUAL_SKILLS` if it should be "
                f"recognised."
            )
        # 4. Known source warning
        if s.source is not None and s.source not in _KNOWN_SKILL_SOURCES:
            r.warnings.append(
                f"skill {s.id!r}: source {s.source!r} is not a recognised adapter "
                f"(known: {sorted(_KNOWN_SKILL_SOURCES)}) — provisioning will fail "
                f"unless an adapter is registered for it"
            )

    # 5/6/7/8. Application invariants
    seen_app_names: set[str] = set()
    for i, app in enumerate(template.applications):
        if app.name in seen_app_names:
            r.errors.append(f"application {app.name!r} declared more than once")
        seen_app_names.add(app.name)

        # 6. Exactly one of app_id / embedded_path
        has_id = bool(app.app_id)
        has_embedded = bool(app.embedded_path)
        if has_id == has_embedded:
            r.errors.append(
                f"application {app.name!r}: must declare exactly one of "
                f"'app_id' (gallery reference) or 'embedded_path' "
                f"(under apps/), got "
                f"{'both' if has_id and has_embedded else 'neither'}"
            )

        # 7. skill_deps must be declared on the template
        for dep in app.skill_deps:
            if dep not in declared_skills:
                r.errors.append(
                    f"application {app.name!r}: skill_dep {dep!r} is not "
                    f"declared in the template's 'skills' list "
                    f"(declared: {sorted(declared_skills) or 'none'})"
                )

        # 8. Embedded path must exist
        if has_embedded:
            ep = app.embedded_path or ""
            if ep not in template.embedded_apps:
                r.errors.append(
                    f"application {app.name!r}: embedded_path {ep!r} not "
                    f"found in apps/ "
                    f"(available: {sorted(template.embedded_apps) or 'none'})"
                )

    # 9. Template var entries
    for vname, vspec in template.template_vars.items():
        if not _ID_RE.match(vname) and not vname.isidentifier():
            r.warnings.append(
                f"template_var {vname!r} is not a typical identifier — "
                f"placeholder substitution uses {{var_name}} syntax"
            )
        if "description" not in vspec:
            r.warnings.append(
                f"template_var {vname!r} has no 'description' — wizard cannot "
                f"explain it to the user"
            )

    # 10. Placeholder cross-check
    declared_vars = set(template.template_vars.keys())
    for body_name, body in [
        ("AGENTS.md.template", template.agents_md_template),
        ("SOUL.md.template", template.soul_md_template),
    ]:
        if not body:
            continue
        referenced = _placeholders_in(body)
        undeclared = referenced - declared_vars - _IMPLICIT_VARS
        if undeclared:
            r.warnings.append(
                f"{body_name} references undeclared template vars "
                f"{sorted(undeclared)} — wizard must provide them or they "
                f"will render literally"
            )

    # 11. Channel pattern warning
    if template.channel_pattern is not None and (
        template.channel_pattern not in _KNOWN_CHANNEL_PATTERNS
    ):
        r.warnings.append(
            f"channel_pattern {template.channel_pattern!r} not in known list "
            f"(known: {sorted(_KNOWN_CHANNEL_PATTERNS)}) — taken as advisory"
        )

    return r


# ── Helpers ───────────────────────────────────────────────────────────────────

# Variables the framework substitutes automatically; templates may
# reference these without declaring them in template_vars.
_IMPLICIT_VARS = frozenset({
    "bot_id",
    "bot_name",
    "template_name",
    "template_display_name",
})

# Match {name} or {{name}} placeholders. We support both because some
# template bodies (especially YAML/JSON snippets inside Markdown) need
# {{ }} to avoid format-string collisions.
_PLACEHOLDER_RE = re.compile(r"\{\{?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}?\}")


def _placeholders_in(text: str) -> set[str]:
    """Return the set of placeholder names referenced in ``text``."""
    return set(_PLACEHOLDER_RE.findall(text))
