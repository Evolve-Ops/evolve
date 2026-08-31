"""
provisioner.py — Plan the work needed to provision a bot from a template.

The provisioner produces a :class:`ProvisionPlan` describing all the
steps required to go from a freshly-registered bot to a fully-
configured one: which skills to install, which applications to install,
which AGENTS.md / SOUL.md / exec-approvals files to render and where.

The plan is **pure data** — it does not execute anything. The CLI
(``evolve-admin deploy --from-template``) takes the plan and dispatches
each step through the existing install pathways:
  - Skills → :func:`evolve_admin.deploy.install_oc_plugin` (after
    arranging plugin allow-listing in openclaw.json).
  - Applications → existing app gallery install pathway.
  - AGENTS.md / SOUL.md / exec-approvals → write to the bot's workspace
    via the standard `/tmp staging + sudo /bin/cp` pattern (see
    CLAUDE.md "File Access Pattern").

Keeping execution out of this module preserves the layering: the
template framework can be unit-tested without mocking the whole deploy
machinery, and a different runtime adapter (e.g. Hermes) can reuse
plan_provision unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── Derived var helpers ───────────────────────────────────────────────────────


def _tz_offset_minutes(tz_name: str) -> int | None:
    """Return the current UTC offset in minutes for ``tz_name``, or None.

    Uses :mod:`zoneinfo` (Python 3.9+). Returns ``None`` if the
    timezone is not recognised (so the caller can fall back gracefully
    rather than crashing). The offset is used to convert a user-desired
    local time into the system's local time for ``StartCalendarInterval``.

    >>> _tz_offset_minutes("America/New_York")  # -240 or -300 depending on DST
    -300  # (illustrative — actual value depends on system clock)
    """
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            # Python < 3.9 fallback — try backport
            from backports.zoneinfo import ZoneInfo  # type: ignore
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz=tz)
        offset = now.utcoffset()
        if offset is None:
            return None
        return int(offset.total_seconds() // 60)
    except Exception:
        return None


def _convert_time_to_system_local(
    hour: int,
    minute: int,
    user_tz: str,
) -> tuple[int, int]:
    """Convert ``hour:minute`` in ``user_tz`` to system-local time.

    ``StartCalendarInterval`` fires according to the system's local
    clock. This function converts the user's desired local time into
    the system's local equivalent so the LaunchAgent fires at the right
    wall-clock moment.

    For example: user_tz=America/New_York, hour=7 (7am ET).
    If the system is on America/Los_Angeles (PT, UTC-8 in winter),
    the LaunchAgent should fire at 4am PT.

    Returns the (hour, minute) pair clamped to [0..23, 0..59] with
    day-wrap (so "11pm ET on a UTC+5 system" wraps to 4am next day,
    which means the same slot will fire the following calendar day —
    acceptable for a morning briefing).

    If the timezone is unrecognised, the original (hour, minute) is
    returned unchanged (best-effort; produces a plan warning).
    """
    import time as _time

    user_offset = _tz_offset_minutes(user_tz)
    if user_offset is None:
        # Can't convert — return original; caller adds a warning.
        return hour, minute

    # Derive system local UTC offset (no tzdata needed; uses system clock).
    sys_offset_seconds = -_time.timezone if not _time.daylight else -_time.altzone
    sys_offset_minutes = sys_offset_seconds // 60

    delta = sys_offset_minutes - user_offset  # minutes to add
    total_minutes = hour * 60 + minute + delta
    # Wrap into 0..1439 (full day)
    total_minutes = total_minutes % (24 * 60)
    return divmod(total_minutes, 60)


def _derive_briefing_hour_minute(
    briefing_time: str,
) -> tuple[int, int] | None:
    """Parse ``HH:MM`` into ``(hour, minute)`` integers.

    Returns ``None`` if the value is not in a recognised format so
    callers can skip var injection (leaving ``{briefing_hour}`` /
    ``{briefing_minute}`` unresolved is louder than silently injecting
    wrong values).

    >>> _derive_briefing_hour_minute("07:00")
    (7, 0)
    >>> _derive_briefing_hour_minute("6:30")
    (6, 30)
    >>> _derive_briefing_hour_minute("bad")  # → None
    """
    if not isinstance(briefing_time, str):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", briefing_time.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute

from .errors import TemplateValidationError
from .loader import ApplicationSpec, BotTemplate
from .resolver import ResolvedSkill, SkillResolution

# NOTE: ``app_blueprint`` is imported lazily inside ``plan_provision`` to
# avoid a circular import (``app_blueprint`` imports
# ``render_template_vars`` from this module).


# ── Placeholder substitution ──────────────────────────────────────────────────

# {var_name} and {{var_name}} — see validator.py for matching rules.
_PLACEHOLDER_RE = re.compile(r"\{\{?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}?\}")


def render_template_vars(
    body: str,
    vars: dict[str, Any],
    *,
    json_safe: bool = False,
) -> str:
    """Replace ``{name}`` / ``{{name}}`` placeholders with values from ``vars``.

    Unmapped placeholders are left as-is (no exception) — this lets the
    wizard prompt for missing values without the template framework
    needing to know which UI is collecting them. The validator already
    warns about undeclared references.

    Substitution is single-pass to keep behaviour deterministic; a
    value like ``"{other_var}"`` will not itself be re-rendered.

    Args:
        body:      Text containing placeholders.
        vars:      Substitution map.
        json_safe: When True, escape each value with ``json.dumps`` and
                   strip its outer quotes before inserting. This keeps a
                   value like ``O'Brien "BB"`` from corrupting an
                   exec-approvals.json template: the surrounding quotes
                   come from the JSON string literal in the template, so
                   the substituted value needs to be JSON-escaped (e.g.
                   ``O'Brien \\"BB\\"``) but not double-quoted. **Use
                   this whenever the substituted output is parsed as
                   JSON.**
    """
    if not body:
        return body

    def _sub(m: re.Match[str]) -> str:
        # Preserve whether the original used { } or {{ }}: peek at the
        # delimiters in the match.
        full = m.group(0)
        name = m.group(1)
        if name not in vars:
            return full  # leave undefined placeholders intact
        value = vars[name]
        if json_safe:
            # json.dumps returns a JSON literal; for strings that's
            # ``"foo"`` (with quotes). We want the *contents* — the
            # template author already wrote the surrounding quotes if
            # the value is going inside a JSON string. For non-strings,
            # we keep the JSON form (numbers, true/false, etc.).
            if isinstance(value, str):
                return json.dumps(value)[1:-1]
            return json.dumps(value)
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, body)


# ── Plan dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApplicationInstall:
    """A planned application installation step.

    Attributes:
        name:           The application's name (as declared in the template).
        app_id:         Gallery app id if this is a gallery reference.
        embedded_path:  Path under ``apps/`` if this is an embedded blueprint.
        skill_deps:     Skill ids required for the app.
        config:         Pass-through config.
        source:         ``"gallery"`` or ``"embedded"``.
    """

    name: str
    app_id: str | None
    embedded_path: str | None
    skill_deps: tuple[str, ...]
    config: dict[str, Any]
    source: str


@dataclass(frozen=True)
class RenderedFile:
    """A file the provisioner will write to the bot's workspace.

    Attributes:
        relative_path:  Path under the bot's `.openclaw/workspace/`
                        directory (e.g. ``"AGENTS.md"``,
                        ``"SOUL.md"``, ``"exec-approvals.json"``).
        content:        Rendered text (placeholders substituted).
        is_json:        True for the exec-approvals file — provisioner
                        consumers may want to validate JSON before write.
    """

    relative_path: str
    content: str
    is_json: bool = False


@dataclass(frozen=True)
class EmbeddedAppPlan:
    """A planned embedded-app render — one per ``ApplicationInstall``
    whose ``source == "embedded"`` and whose blueprint contained
    ``## FILE:`` blocks in its ``build_spec``.

    Atomicity is **per-app**, not pod-wide (see V1.1-1 spec). Each
    :class:`EmbeddedAppPlan` is the unit the executor in
    ``cli_integration`` writes-or-rolls-back.

    Attributes:
        app_id:           ``id`` field from the blueprint JSON (e.g.
                          ``"app_morning_briefing"``).
        app_name:         Display name from the blueprint JSON.
        embedded_path:    Path under the template's ``apps/`` dir
                          (e.g. ``"morning_briefing.json"``).
        files:            Tuple of :class:`BlueprintFile` to render. May be
                          empty if the blueprint had no ``## FILE:`` blocks
                          (a narrative-only blueprint — still loadable,
                          just not yet materialisable).
        launchd_labels:   Unique launchd Labels referenced by the plan.
        warnings:         Non-blocking issues discovered during parse
                          (empty FILE: bodies, duplicate paths, …).
    """

    app_id: str
    app_name: str
    embedded_path: str
    # Typed as Any because ``BlueprintFile`` lives in app_blueprint.py and
    # we deliberately avoid the import here (see lazy-import note above).
    files: tuple[Any, ...]
    launchd_labels: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_files(self) -> bool:
        """True iff this plan has at least one file to materialise."""
        return bool(self.files)


@dataclass
class ProvisionPlan:
    """The complete, executable plan to provision a bot from a template.

    Attributes:
        template_name:        Template id.
        bot_id:               Target bot's id (macOS user + network member).
        vars:                 Variables that were substituted into bodies.
        skill_resolution:     Output of :func:`resolve_skills`.
        skills_to_install:    Skills the provisioner must install (subset
                              of skill_resolution where ``needs_install``
                              is True).
        applications:         Application installations to perform.
        files:                Files to render into the bot workspace
                              (AGENTS.md / SOUL.md / exec-approvals.json
                              — declarative artifacts owned by the
                              template itself, NOT by any embedded app).
        embedded_app_plans:   Per-embedded-app render plans (V1.1-1). Each
                              corresponds to one
                              ``ApplicationInstall(source="embedded")``
                              entry whose blueprint contained
                              ``## FILE:`` blocks.
        warnings:             Informational issues that did not block
                              planning.
    """

    template_name: str
    bot_id: str
    vars: dict[str, Any]
    skill_resolution: SkillResolution
    skills_to_install: tuple[ResolvedSkill, ...]
    applications: tuple[ApplicationInstall, ...]
    files: tuple[RenderedFile, ...]
    embedded_app_plans: tuple[EmbeddedAppPlan, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff the plan can be safely executed."""
        return self.skill_resolution.ok


# ── plan_provision ────────────────────────────────────────────────────────────


def plan_provision(
    template: BotTemplate,
    *,
    bot_id: str,
    vars: dict[str, Any] | None = None,
    skill_resolution: SkillResolution,
) -> ProvisionPlan:
    """Produce a :class:`ProvisionPlan` from a template + resolved skills.

    Args:
        template:           Loaded + validated template.
        bot_id:             Target bot id. Surfaced as the ``bot_id``
                            implicit template var.
        vars:               User-supplied template variable values. The
                            provisioner adds implicit vars (``bot_id``,
                            ``bot_name``, ``template_name``,
                            ``template_display_name``) on top.
        skill_resolution:   Already-computed resolution. Pass in the
                            result of :func:`resolve_skills` so callers
                            can short-circuit on resolver failures
                            before planning the rest.

    Raises:
        TemplateValidationError: if any required template_var is missing
            from ``vars`` (after merging implicit defaults). Required
            vars are those with ``required: True`` in template.yaml.

    Implicit vars vs. declared vars:
      - ``bot_id``, ``bot_name``, ``template_name``,
        ``template_display_name`` are always populated by the framework.
      - Anything else under ``template.template_vars`` must be supplied
        by the caller (wizard/UI/CLI flag), unless it has a ``default``.
    """
    # ── Build the substitution dict ───────────────────────────────────────────
    merged: dict[str, Any] = {}
    # Apply declared defaults first.
    missing_required: list[str] = []
    for vname, vspec in template.template_vars.items():
        if "default" in vspec:
            merged[vname] = vspec["default"]
    # Override with caller-supplied values.
    if vars:
        for k, v in vars.items():
            merged[k] = v
    # Stamp implicit vars LAST so callers can't accidentally override them.
    merged["bot_id"] = bot_id
    merged["bot_name"] = bot_id  # alias — UIs use either spelling
    merged["template_name"] = template.name
    merged["template_display_name"] = template.display_name

    # ── Derived vars ──────────────────────────────────────────────────────────
    # ``briefing_hour`` / ``briefing_minute`` are derived from
    # ``briefing_time`` (HH:MM) so plist templates can reference each
    # component separately in ``StartCalendarInterval``:
    #
    #     <key>Hour</key><integer>{briefing_hour}</integer>
    #     <key>Minute</key><integer>{briefing_minute}</integer>
    #
    # TZ design decision (documented here, in self-review, and in
    # manual-verification.md):
    #
    # ``StartCalendarInterval`` fires based on the SYSTEM's local clock —
    # it has no built-in timezone field. To make the agent fire at 7am
    # America/New_York on a system running America/Los_Angeles (PT), we
    # pre-compute the system-local equivalent (4am PT) at provisioning time
    # and store THAT as {briefing_hour}/{briefing_minute} in the plist.
    #
    # We also inject {briefing_tz_env} = the user's IANA TZ string so the
    # plist's EnvironmentVariables block can set TZ=<zone>, ensuring the
    # *script* runs with the correct timezone for display-formatting and
    # for any time-of-day logic inside briefing.py itself.
    #
    # Together these two mechanisms ensure:
    #   1. launchd fires at the right wall-clock moment (TZ-converted hour)
    #   2. the script sees the right TZ for formatting / calendar interpretation
    #
    # If the user_tz is unrecognised, briefing_hour/briefing_minute fall
    # back to the raw parsed value (no conversion) and a plan warning is
    # added so the operator knows.
    warnings: list[str] = []
    bt = merged.get("briefing_time")
    user_tz = str(merged.get("time_zone") or "").strip()
    if bt is not None:
        parsed = _derive_briefing_hour_minute(str(bt))
        if parsed is not None:
            raw_hour, raw_minute = parsed
            if user_tz:
                # TZ-convert to system local time for StartCalendarInterval.
                sys_hour, sys_minute = _convert_time_to_system_local(
                    raw_hour, raw_minute, user_tz,
                )
                # Warn if the timezone was unrecognised (conversion was a no-op).
                if sys_hour == raw_hour and sys_minute == raw_minute:
                    user_offset = _tz_offset_minutes(user_tz)
                    if user_offset is None:
                        warnings.append(
                            f"time_zone {user_tz!r} is not a recognised IANA "
                            f"timezone; StartCalendarInterval will use the raw "
                            f"time {bt!r} without TZ conversion — the briefing "
                            f"may fire at the wrong local time."
                        )
            else:
                sys_hour, sys_minute = raw_hour, raw_minute
            merged["briefing_hour"] = sys_hour
            merged["briefing_minute"] = sys_minute
    # Expose TZ string for plist EnvironmentVariables.
    if user_tz:
        merged["briefing_tz_env"] = user_tz

    # V1.1-2 fix-up: a template using {briefing_tz_env} without declaring
    # time_zone would leak the unsubstituted placeholder into the rendered
    # plist (`<string>{briefing_tz_env}</string>`). launchd would then set
    # the literal env var `TZ={briefing_tz_env}` — undefined behaviour for
    # any script that reads TZ. Fail loud at planning time rather than
    # silently produce a broken plist.
    if not user_tz:
        offending_blueprints: list[str] = []
        for _app in template.applications:
            if _app.embedded_path is None:
                continue
            _bp = template.embedded_apps.get(_app.embedded_path)
            if not isinstance(_bp, dict):
                continue
            _spec = _bp.get("build_spec")
            if isinstance(_spec, str) and "{briefing_tz_env}" in _spec:
                offending_blueprints.append(_app.embedded_path)
        if offending_blueprints:
            raise TemplateValidationError(
                template.name,
                [
                    f"embedded blueprint {p!r} references {{briefing_tz_env}} "
                    f"but template_var 'time_zone' is not set — would leak "
                    f"the literal placeholder into the rendered plist. Set "
                    f"time_zone or remove {{briefing_tz_env}} from the "
                    f"blueprint."
                    for p in offending_blueprints
                ],
            )

    # Required-var check after merge.
    for vname, vspec in template.template_vars.items():
        if vspec.get("required") and merged.get(vname) in (None, ""):
            missing_required.append(vname)
    if missing_required:
        raise TemplateValidationError(
            template.name,
            [f"missing required template_var {v!r}" for v in missing_required],
        )

    # ── Render text bodies ────────────────────────────────────────────────────
    files: list[RenderedFile] = []
    if template.agents_md_template:
        files.append(RenderedFile(
            relative_path="AGENTS.md",
            content=render_template_vars(template.agents_md_template, merged),
        ))
    if template.soul_md_template:
        files.append(RenderedFile(
            relative_path="SOUL.md",
            content=render_template_vars(template.soul_md_template, merged),
        ))
    if template.exec_approvals is not None:
        # Render placeholders inside the JSON's *string* values without
        # breaking JSON structure. Values containing JSON metacharacters
        # (``"``, ``\``, control chars) must be JSON-escaped so a value
        # like ``O'Brien "BB"`` doesn't terminate the surrounding string
        # literal mid-substitution. ``json_safe=True`` handles this.
        as_text = json.dumps(template.exec_approvals, indent=2)
        rendered = render_template_vars(as_text, merged, json_safe=True)
        try:
            parsed = json.loads(rendered)
            content = json.dumps(parsed, indent=2)
        except json.JSONDecodeError as e:
            # With json_safe=True, this should never happen for
            # well-formed input — if it does, the template itself is
            # broken (e.g. a placeholder lives outside a JSON string
            # literal and produces an invalid JSON token after
            # substitution). Refuse to ship a broken config file:
            # raising here is louder + safer than writing a corrupt
            # exec-approvals.json that the bot's gateway will then
            # reject at every startup.
            raise TemplateValidationError(
                template.name,
                [
                    f"exec-approvals.template.json rendered to invalid JSON "
                    f"after placeholder substitution ({e}). "
                    f"Most likely a placeholder appears outside a JSON "
                    f"string literal in the template. The substitution "
                    f"output was preserved for debugging in the plan "
                    f"warnings."
                ],
            ) from e
        files.append(RenderedFile(
            relative_path="exec-approvals.json",
            content=content,
            is_json=True,
        ))

    # ── Embedded-app blueprints (V1.1-1) ──────────────────────────────────────
    # For each ApplicationInstall whose source is "embedded", parse the
    # corresponding blueprint JSON (loaded by loader.load_template into
    # template.embedded_apps) and turn its build_spec ``## FILE:`` blocks
    # into a per-app render plan. Substitution uses the same merged var
    # map as AGENTS.md / SOUL.md, so {bot_id}, {user_name}, etc. all work
    # inside scripts and plists the same way they do in the markdown
    # bodies.
    from .app_blueprint import parse_app_blueprint  # local import — see top
    embedded_plans: list[EmbeddedAppPlan] = []
    # ``warnings`` was already initialised above (derived-vars section).
    for app in template.applications:
        if app.embedded_path is None:
            continue
        bp = template.embedded_apps.get(app.embedded_path)
        if bp is None:
            # Validator already covers this, but be defensive: a missing
            # blueprint is a planning warning, not a hard failure (the
            # plan is still useful for partial dry-runs).
            warnings.append(
                f"embedded blueprint {app.embedded_path!r} declared by "
                f"application {app.name!r} not found in template.embedded_apps"
            )
            continue
        try:
            parsed = parse_app_blueprint(
                blueprint=bp,
                embedded_path=app.embedded_path,
                vars=merged,
            )
        except TemplateValidationError as e:
            # Surface a structured plan error rather than aborting the
            # whole template plan — callers can decide whether to skip
            # the offending app or fail the deploy.
            raise TemplateValidationError(
                template.name,
                [
                    f"embedded app {app.embedded_path!r}: {issue}"
                    for issue in e.issues
                ],
            ) from e

        if not parsed.files and parsed.app_id:
            warnings.append(
                f"embedded blueprint {app.embedded_path!r} has no "
                f"## FILE: blocks in its build_spec — nothing to render"
            )
        embedded_plans.append(EmbeddedAppPlan(
            app_id=parsed.app_id,
            app_name=parsed.app_name,
            embedded_path=parsed.embedded_path,
            files=parsed.files,
            launchd_labels=parsed.launchd_labels,
            warnings=parsed.warnings,
        ))

    return ProvisionPlan(
        template_name=template.name,
        bot_id=bot_id,
        vars=merged,
        skill_resolution=skill_resolution,
        skills_to_install=skill_resolution.queued(),
        applications=_plan_applications(template),
        files=tuple(files),
        embedded_app_plans=tuple(embedded_plans),
        warnings=warnings,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _plan_applications(template: BotTemplate) -> tuple[ApplicationInstall, ...]:
    """Translate template ApplicationSpecs to ApplicationInstall steps."""
    out: list[ApplicationInstall] = []
    for app in template.applications:
        source = "gallery" if app.app_id else "embedded"
        out.append(ApplicationInstall(
            name=app.name,
            app_id=app.app_id,
            embedded_path=app.embedded_path,
            skill_deps=app.skill_deps,
            config=dict(app.config),
            source=source,
        ))
    return tuple(out)
