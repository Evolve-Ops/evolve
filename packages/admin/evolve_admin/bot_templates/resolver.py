"""
resolver.py — Resolve template skill dependencies against a bot's current state.

The resolver answers: *given this template's declared skills, and this
bot's currently-installed plugins, which skills are already present,
which need to be installed, which require a per-skill adapter (e.g.
GOG OAuth), and which cannot be installed automatically at all?*

Resolution model
────────────────
Every skill resolves to one of these statuses:

* ``installed`` — the OpenClaw plugin backing this skill is already in
  the bot's ``openclaw.json``. Nothing to do.
* ``queued`` — not installed yet, but the standard deploy
  (``openclaw plugins install -l <PLUGIN_INSTALL_DIR>``) will pick it
  up because the plugin is part of the OpenClaw built-in set. **Note:**
  see the warning below — today this set is empty.
* ``adapter-required`` — there is a dedicated install pathway for this
  skill (e.g. GOG OAuth via :mod:`evolve_admin.skills.gog_install`).
  The provisioner does not run the install itself; it surfaces the
  next-step command/URL the operator must drive.
* ``manual`` — the skill is recognised but has no auto-install path
  on this substrate (e.g. ``weather`` / ``news`` aren't OpenClaw
  plugins). The deploy emits a clear "you must configure this before
  first use" message; provisioning continues.
* ``unsupported`` — the resolver knows the skill maps to plugin X but
  the substrate's available-plugin set explicitly excludes X. Blocks
  unless ``optional=True``.
* ``unknown-adapter`` — the skill declared an unrecognised ``source``
  (e.g. ``clawhub`` before that adapter exists). Blocks unless
  ``optional=True``.

CRITICAL FACT (verified 2026-05-12 against deploy.py:89, deploy.py:560,
health.py:367): the standard deploy's ``install_oc_plugin`` runs
``openclaw plugins install -l /Users/Shared/evolve-plugin``. The
``/Users/Shared/evolve-plugin`` directory contains **only the Evolve
plugin's dist + node_modules** — synced there by ``build_plugin()`` at
deploy time. No other OpenClaw plugins (brave/gog/obsidian/...) live
there. Templates that declare those as skills must either:

  1. Have them already installed on the bot (status=installed), or
  2. Use an adapter like ``gog_install`` (status=adapter-required), or
  3. Be marked as manual / user-supplied (status=manual), or
  4. Set ``optional=True`` on the SkillSpec.

If none of the above applies, the resolver returns ``unsupported``
with a blocking_issue. The previous behaviour — happily marking
``brave`` as "queued" and trusting the standard deploy to install it
— was a silent half-deployment.

The resolver is **pure**: it reads a bot's installed-plugin state from
an ``installed_plugins`` mapping passed in (so tests don't need to
touch openclaw.json). The CLI integration is responsible for sourcing
that mapping via the standard ``ensure_plugin_config`` read pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .loader import BotTemplate, SkillSpec


# ── Skill ↔ plugin mapping ───────────────────────────────────────────────────

# Today's translation table between substrate-neutral skill ids
# (agentskills.io vocabulary) and OpenClaw plugin names. When ClawHub
# starts shipping skills in the substrate-neutral format, this table
# becomes a fallback path for legacy openclaw-specific plugin names
# only.
#
# Templates should declare the substrate-neutral skill id; this table
# is consulted only when the resolver needs to check "is this skill
# already installed in OpenClaw?"
#
# Adding a new skill: pick the substrate-neutral id (lowercase,
# kebab/snake), add it here pointing at the OpenClaw plugin name, and
# the resolver+install pathway picks it up automatically.
#
# Note on ``gog`` → ``google``: per A3 (gog_install.py:62), the
# user-facing skill id is ``gog`` (matching ClawHub conventions) but
# the OpenClaw plugin that backs it is named ``google``. Keeping the
# rename here means the rest of the framework can stay substrate-neutral.
_SKILL_TO_OC_PLUGIN: dict[str, str] = {
    # Messaging
    "slack": "slack",
    "telegram": "telegram",
    "discord": "discord",
    "signal": "signal",
    # Web / browsing
    "brave": "brave",
    "brave-search": "brave",
    # Email + Calendar (Google Workspace) — backed by the 'google' plugin
    "gog": "google",
    "gmail": "google",
    "calendar": "google",
    "google-workspace": "google",
    # Code
    "github": "github",
    # Memory / notes
    "obsidian": "obsidian",
    # Realtime / push
    "gmail-watcher": "gmail-watcher",
    # Game / simulator
    "unity": "unity",
}


# ── Plugins the standard deploy can actually install ─────────────────────────
#
# As of 2026-05-12, the standard deploy's plugin install step
# (``deploy.install_oc_plugin``) does **only** the Evolve plugin —
# ``openclaw plugins install -l /Users/Shared/evolve-plugin``. That
# directory contains the Evolve plugin's dist/ + node_modules and
# nothing else.
#
# So the set of plugin names that are auto-installable by the standard
# deploy is, today, empty for the template framework's purposes (the
# Evolve plugin is installed unconditionally on every deploy and isn't
# a skill anyone declares).
#
# Keeping this as a configurable set means a future change — e.g.
# building a multi-plugin install dir, or wiring up a plugin registry —
# can flip behaviour without changing the resolver's contract.
_STANDARD_DEPLOY_AUTO_INSTALLABLE: frozenset[str] = frozenset()


# ── Skills that route through a dedicated adapter ─────────────────────────────
#
# An adapter is anything OTHER than "openclaw plugins install" that can
# bring a skill online — e.g. the GOG OAuth flow (Spec 11), wired up by
# :mod:`evolve_admin.skills.gog_install`. The resolver doesn't import
# the adapter module (avoids a circular dep + lets the template
# framework run before A3 lands); it just records that the skill *has*
# one. The CLI/UI is responsible for dispatching to the right place.
#
# Map: skill id → human-readable "what to do next" string. Used by the
# plan summary so operators see a concrete next step.
_SKILL_ADAPTERS: dict[str, str] = {
    # GOG = Google Workspace (Gmail + Calendar). Backed by the per-bot
    # OAuth flow in evolve_admin.skills.gog_install (Spec 11).
    "gog": (
        "Use the Gmail/Calendar install flow: "
        "`evolve-admin skills install gog --bot <bot_id>` "
        "(or the in-UI 'Add Gmail/Calendar' button on the bot's skills page)."
    ),
    "gmail": (
        "Use the Gmail/Calendar install flow: "
        "`evolve-admin skills install gog --bot <bot_id>`."
    ),
    "calendar": (
        "Use the Gmail/Calendar install flow: "
        "`evolve-admin skills install gog --bot <bot_id>`."
    ),
    "google-workspace": (
        "Use the Gmail/Calendar install flow: "
        "`evolve-admin skills install gog --bot <bot_id>`."
    ),
}


# ── Skills that are recognised but not auto-installable on any substrate ─────
#
# Examples: ``weather`` and ``news`` aren't OpenClaw plugins — they're
# capability-shaped names that a template can declare so its prompts and
# applications can reason about them, but configuring them is a manual
# operator step (set an API key, pick RSS feeds, etc.).
#
# Map: skill id → human-readable "manual setup" guidance.
_MANUAL_SKILLS: dict[str, str] = {
    "weather": (
        "Weather is not an OpenClaw plugin. For the morning-briefing "
        "template, weather is fetched directly from wttr.in (no API key "
        "required) — configure the bot's 'location' template variable "
        "to enable it. For other templates, configure a weather source "
        "(OpenWeather API key, weather.gov RSS, etc.) in the app config."
    ),
    "news": (
        "News is not an OpenClaw plugin. For the morning-briefing "
        "template, news is fetched directly from RSS feeds — configure "
        "the bot's 'news_sources' template variable (comma-separated RSS "
        "URLs or topic keywords like 'hacker news'). For other templates, "
        "configure news sources in the app config before first use."
    ),
    # ``messaging`` is configured at the channel/integration layer, not
    # via plugin install. Templates can declare it so application
    # skill_deps validate, but the actual configuration happens through
    # the per-bot integration setup (Slack/Telegram/Discord/...).
    "messaging": (
        "Messaging is configured via the bot's channel/integration setup, "
        "not via skill install. Connect the bot's primary integration "
        "(Slack/Telegram/Discord/Signal/email) in the admin UI."
    ),
}


def _oc_plugin_for(skill_id: str) -> str:
    """Map a substrate-neutral skill id to its OpenClaw plugin name.

    Falls through to the id itself if no mapping is registered. The
    fall-through is **not** a guarantee that the plugin exists — see
    :func:`is_known_skill` for the explicit "do we recognise this id?"
    check used by the validator.
    """
    return _SKILL_TO_OC_PLUGIN.get(skill_id, skill_id)


def is_known_skill(skill_id: str) -> bool:
    """True if the resolver has explicit knowledge of this skill id.

    "Explicit knowledge" means: the id appears in the
    skill→plugin table, has a dedicated adapter, or is recognised as a
    manual skill. Anything else is fall-through territory — the resolver
    will still try to handle it (using id-as-plugin-name), but the
    validator should warn so template authors catch typos.
    """
    return (
        skill_id in _SKILL_TO_OC_PLUGIN
        or skill_id in _SKILL_ADAPTERS
        or skill_id in _MANUAL_SKILLS
    )


def known_skill_ids() -> frozenset[str]:
    """Return the union of all skill ids the resolver explicitly recognises.

    Used by the validator to warn on unknown ids — see
    :func:`evolve_admin.bot_templates.validator.validate_template`.
    """
    return frozenset(_SKILL_TO_OC_PLUGIN) | frozenset(_SKILL_ADAPTERS) | frozenset(_MANUAL_SKILLS)


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedSkill:
    """The resolver's verdict on a single declared skill.

    Attributes:
        spec:            The :class:`SkillSpec` from the template.
        status:          One of:

                         * ``"installed"`` — plugin already in openclaw.json
                         * ``"queued"`` — standard deploy will install it
                         * ``"adapter-required"`` — needs a dedicated flow
                           (e.g. GOG OAuth); see ``adapter_hint``
                         * ``"manual"`` — recognised but no auto-install
                           (weather/news/messaging); see ``adapter_hint``
                         * ``"unsupported"`` — substrate cannot install
                         * ``"unknown-adapter"`` — declared ``source`` is
                           unimplemented
        plugin_name:     OpenClaw plugin name this skill maps to (when
                         ``status`` is not ``unsupported`` or ``manual``).
        reason:          Human-readable explanation for the chosen status.
        adapter_hint:    For ``adapter-required`` and ``manual`` statuses,
                         the operator-facing next-step instructions.
    """

    spec: SkillSpec
    status: str
    plugin_name: str | None
    reason: str
    adapter_hint: str | None = None

    @property
    def needs_install(self) -> bool:
        """True if provisioning must run a standard plugin install step
        for this skill (status=queued)."""
        return self.status == "queued"

    @property
    def needs_adapter(self) -> bool:
        """True if this skill needs a dedicated install adapter
        (e.g. GOG OAuth flow)."""
        return self.status == "adapter-required"

    @property
    def needs_manual_setup(self) -> bool:
        """True if this skill is recognised but needs manual operator
        configuration after deploy (no auto-install path)."""
        return self.status == "manual"


@dataclass(frozen=True)
class SkillResolution:
    """The full resolution result for a template.

    Attributes:
        resolved:        Per-skill verdicts, in declaration order.
        ok:              True if every skill is in a non-blocking
                         status (installed / queued / adapter-required /
                         manual). False if any skill is unsupported or
                         unknown-adapter without ``optional=True``.
        blocking_issues: Human-readable problems that block provisioning.
                         Empty iff ``ok``.
    """

    resolved: tuple[ResolvedSkill, ...]
    ok: bool
    blocking_issues: tuple[str, ...]

    def queued(self) -> tuple[ResolvedSkill, ...]:
        """Skills the standard deploy will install (status=queued)."""
        return tuple(r for r in self.resolved if r.needs_install)

    def installed(self) -> tuple[ResolvedSkill, ...]:
        """Skills already present on the bot."""
        return tuple(r for r in self.resolved if r.status == "installed")

    def adapter_required(self) -> tuple[ResolvedSkill, ...]:
        """Skills that need a dedicated install adapter (e.g. GOG)."""
        return tuple(r for r in self.resolved if r.needs_adapter)

    def manual(self) -> tuple[ResolvedSkill, ...]:
        """Skills that need post-deploy manual setup (weather/news/messaging)."""
        return tuple(r for r in self.resolved if r.needs_manual_setup)


# ── Resolution ────────────────────────────────────────────────────────────────


def resolve_skills(
    template: BotTemplate,
    *,
    installed_plugins: Iterable[str] | None = None,
    available_plugins: Iterable[str] | None = None,
) -> SkillResolution:
    """Decide, for every skill in ``template``, what provisioning must do.

    Args:
        template:          The loaded :class:`BotTemplate`.
        installed_plugins: Names of OpenClaw plugins currently installed
                           on the target bot. Reads from openclaw.json's
                           ``plugins.entries`` keys in production. None
                           is treated as "no plugins installed".
        available_plugins: Optional whitelist of plugins the substrate
                           can install via the standard ``openclaw
                           plugins install`` pathway. If None, defaults
                           to :data:`_STANDARD_DEPLOY_AUTO_INSTALLABLE`
                           (empty today — see module docstring). Tests
                           pass an explicit set/empty-set to assert
                           specific branches.

    Returns:
        :class:`SkillResolution`. Inspect ``ok`` before provisioning.

    The "queued" status is reserved for skills whose plugin actually IS
    in the auto-installable set. If a skill's plugin can't be installed
    by the standard deploy AND isn't covered by a dedicated adapter or
    declared as manual, the skill resolves to ``unsupported`` and
    blocks provisioning. This prevents the silent half-deployment mode
    where the deploy reports success but the bot is missing declared
    plugins — see module docstring for the verified evidence.

    Application skill_deps validation is performed at template-validation
    time (see :func:`validate_template`), not here — by the time we're
    resolving, every dep is guaranteed to be in the skills list.
    """
    installed = frozenset(installed_plugins or ())
    auto_installable = (
        frozenset(available_plugins)
        if available_plugins is not None
        else _STANDARD_DEPLOY_AUTO_INSTALLABLE
    )

    resolved: list[ResolvedSkill] = []
    blocking: list[str] = []

    for spec in template.skills:
        # ── Choose an adapter ─────────────────────────────────────────────────
        # Default source is "openclaw-plugin" (the current substrate). An
        # explicit source like "clawhub" or "local" is recognised by the
        # validator but has no implementation yet — surface as
        # "unknown-adapter" so provisioning fails loudly rather than
        # silently skipping the install.
        source = spec.source or "openclaw-plugin"
        if source != "openclaw-plugin":
            reason = (
                f"source {source!r} has no adapter yet — only "
                f"'openclaw-plugin' is implemented in this version"
            )
            resolved.append(ResolvedSkill(
                spec=spec,
                status="unknown-adapter",
                plugin_name=None,
                reason=reason,
            ))
            if not spec.optional:
                blocking.append(f"skill {spec.id!r}: {reason}")
            continue

        plugin_name = _oc_plugin_for(spec.id)

        # ── 1. Already installed? ────────────────────────────────────────────
        if plugin_name in installed:
            resolved.append(ResolvedSkill(
                spec=spec,
                status="installed",
                plugin_name=plugin_name,
                reason=f"plugin {plugin_name!r} already in openclaw.json",
            ))
            continue

        # ── 2. Dedicated adapter for this skill (e.g. GOG OAuth)? ───────────
        if spec.id in _SKILL_ADAPTERS:
            hint = _SKILL_ADAPTERS[spec.id]
            resolved.append(ResolvedSkill(
                spec=spec,
                status="adapter-required",
                plugin_name=plugin_name,
                reason=(
                    f"skill {spec.id!r} requires a dedicated install "
                    f"flow (not the standard plugins-install)"
                ),
                adapter_hint=hint,
            ))
            continue

        # ── 3. Manual skill (no auto-install on any substrate)? ─────────────
        if spec.id in _MANUAL_SKILLS:
            hint = _MANUAL_SKILLS[spec.id]
            resolved.append(ResolvedSkill(
                spec=spec,
                status="manual",
                plugin_name=None,
                reason=(
                    f"skill {spec.id!r} is not provided by any plugin — "
                    f"manual configuration required"
                ),
                adapter_hint=hint,
            ))
            continue

        # ── 4. Standard auto-install? ────────────────────────────────────────
        if plugin_name in auto_installable:
            resolved.append(ResolvedSkill(
                spec=spec,
                status="queued",
                plugin_name=plugin_name,
                reason=(
                    f"will be installed via openclaw plugin install ({plugin_name})"
                ),
            ))
            continue

        # ── 5. Nothing can install this skill ───────────────────────────────
        # This is the silent-half-deployment guard. Previously the
        # resolver would happily mark this "queued" and let the deploy
        # report "succeeded" while the plugin was never installed.
        reason = (
            f"plugin {plugin_name!r} cannot be installed by the standard "
            f"deploy (which only installs Evolve's own plugin) and no "
            f"dedicated adapter is registered for skill {spec.id!r}. "
            f"Install it manually before re-running the template "
            f"(e.g. via `evolve-admin menu plugins <bot>` to add the entry, "
            f"then `openclaw plugins install -l <plugin-source-dir>` as the "
            f"bot user) or mark the skill `optional: true` in the template."
        )
        resolved.append(ResolvedSkill(
            spec=spec,
            status="unsupported",
            plugin_name=plugin_name,
            reason=reason,
        ))
        if not spec.optional:
            blocking.append(f"skill {spec.id!r}: {reason}")

    return SkillResolution(
        resolved=tuple(resolved),
        ok=not blocking,
        blocking_issues=tuple(blocking),
    )


# ── Helpers for the CLI / deploy integration ──────────────────────────────────


def installed_plugins_from_openclaw_json(cfg: dict[str, Any]) -> list[str]:
    """Extract installed plugin names from a parsed openclaw.json dict.

    Mirrors the read pattern in :func:`ensure_plugin_config` — but
    without the file-system side. Returns plugin names whose ``enabled``
    flag is True (or absent — older configs omit it).

    Returns an empty list if the structure is unexpected, which is the
    same behaviour as "no plugins installed" for the resolver.
    """
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    if not isinstance(plugins, dict):
        return []
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        return []
    out: list[str] = []
    for name, data in entries.items():
        if isinstance(data, dict):
            # Treat missing 'enabled' as enabled, mirroring OC's default.
            if data.get("enabled", True) is not False:
                out.append(str(name))
        else:
            # Older configs sometimes store entries as bare values.
            out.append(str(name))
    return out
