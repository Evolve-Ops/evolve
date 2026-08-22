"""Phase definitions for the wizard agenda.

Spec: docs/spec-evo-wizard-2026-05-05.md §4.2.

Each :class:`Phase` declares:

  * ``name`` — canonical phase name (also written into ``state.current_phase``)
  * ``targets`` — fields the extractor should try to fill from the user's
    most recent message
  * ``exit_condition`` — predicate over the merged state.extracted that
    returns True when this phase is "done" and we should advance
  * ``next_phase`` — the phase to transition to when exit fires (or
    ``None`` to mark the wizard completed)

5a MVP ships three primary phases: GREET → ABOUT_YOU → WRAP. WRAP has no
extractor and exits after one turn, at which point the engine commits the
profile and marks the run completed.

Slice 5b will add Goals, Platform tour, and a secondary variant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


# Phase name constants — referenced from prompts, engine, tests.
# Primary chain (admin or recorded primary):
PHASE_CHALLENGE = "challenge"  # 5b2: passphrase entry for unclaimed callers
PHASE_GREET = "greet"
PHASE_ABOUT_YOU = "about_you"
PHASE_GOALS = "goals"  # 5b3: what the user wants to use this bot for
PHASE_PLATFORM_TOUR = "platform_tour"  # 5b3: short Evolve overview
PHASE_GALLERY_RECS = "gallery_recs"  # 5b5: gallery app recommendations
PHASE_WRAP = "wrap"
# Secondary chain (5b4) — runs when a non-primary user opts in to a
# "tour" of the bot they're using rather than claiming primary status.
# Reads from the bot guide (#772) for bot-specific framing.
PHASE_SECONDARY_GREET = "secondary_greet"
PHASE_SECONDARY_ABOUT_YOU = "secondary_about_you"
PHASE_HOW_TO_USE = "how_to_use"
PHASE_SECONDARY_WRAP = "secondary_wrap"
# Guide-draft chain (5b6) — primary-only conversational flow that
# authors / edits the bot guide instead of the file-based CLI from #772.
# Reuses the wizard engine's state + turn route; the user-key shape and
# session ID work identically. Pre-populates from any existing guide so
# the flow doubles as an editor.
PHASE_GUIDE_GATHER = "guide_gather"
PHASE_GUIDE_CONFIRM = "guide_confirm"
# Forge-via-messaging phases (5b7b) — primary-only optional branch from
# GALLERY_RECS when goals exist but the gallery didn't satisfy them.
# The bot proposes building something custom; the user confirms, refines,
# or skips.
PHASE_FORGE_INTRO = "forge_intro"
PHASE_FORGE_DESIGN = "forge_design"
PHASE_FORGE_CONFIRM = "forge_confirm"
# Conversational-approval phase (slice 5b8) — entered when the user types
# bare ``evo`` and the dispatcher fetches a top recommendation. The phase
# pitches the rec, classifies the user's reply via :mod:`evo.wizard.intent`,
# and either records feedback through the Better Engine and chains to the
# next rec, or finalizes when the queue is empty / the user disengages.
PHASE_REC_PENDING = "rec_pending"
# App-promotion offer chain (AL-1.7) — design
# ``design-app-spec-and-discovery-2026-08-15.md`` §7.2: "The primary user is
# asked by their own bot, in their own channel, in the bot's voice — via the
# existing conversational-approval flow ... New phase pair
# PHASE_APP_PROMOTE_OFFER -> PHASE_APP_PROMOTE_CONFIRM following REC_PENDING's
# shape (intent classifier server-side, snooze extraction, session TTL)."
#
# OFFER pitches one eligible discovered draft and classifies the reply.
# CONFIRM is entered only on a rename ("call it X instead") — design §7.2's
# "Rename → applied before conferring" — and echoes the name back before the
# approval is recorded. A plain yes never passes through CONFIRM: adding a
# second prompt to a one-word answer is exactly the friction §10 warns the
# offer must not have.
PHASE_APP_PROMOTE_OFFER = "app_promote_offer"
PHASE_APP_PROMOTE_CONFIRM = "app_promote_confirm"
# App-create chain (Wave 3) — entered when the user types `evo app create`.
# Single-description-then-draft UX: gather one description from the user,
# call `_build_draft` once, render the result for approve/iterate/cancel
# in REVIEW. Less back-and-forth than the FORGE_DESIGN chain since the
# user typically wants to type one paragraph and see a spec, not be
# pumped for fields incrementally.
PHASE_APP_CREATE_DESCRIBE = "app_create_describe"
PHASE_APP_CREATE_REVIEW = "app_create_review"
# Google setup chain — `evo setup-google`. Five phases: intro pitch,
# collect the admin base URL (so we can tell the operator what redirect
# URI to whitelist), render the Google Cloud Console checklist, collect
# the resulting client_id, collect the client_secret + commit to
# network.json. No LLM extraction — every reply is a single value with
# deterministic validation.
PHASE_GOOGLE_SETUP_INTRO = "google_setup_intro"
PHASE_GOOGLE_SETUP_ADMIN_URL = "google_setup_admin_url"
PHASE_GOOGLE_SETUP_CONSOLE = "google_setup_console"
PHASE_GOOGLE_SETUP_CLIENT_ID = "google_setup_client_id"
PHASE_GOOGLE_SETUP_CLIENT_SECRET = "google_setup_client_secret"
# Add-bot chain — `evo add-bot` (admin-only). M1 shipped the planning
# half: why-before-what need finding, audience, purpose capture (the
# effectiveness-layer §4 field), naming after the role is locked, then a
# verbatim plan echo for confirmation. M2 adds the build half: the
# credential walk (borrow / paste / skip), the provision run with stage
# relay + honest failure handling, and the smoke-test wrap. M3 adds the
# defaults between purpose and naming: the starter-app proposal from the
# captured role (delta §5), the morning-briefing default-on/opt-out turn
# (delta §6), and the non-skippable consent + tone phase (delta §7).
# Spec: docs/spec-add-bot-wizard-build-delta-2026-06-10.md §3.2 + §9.
PHASE_AB_NEED = "ab_need"
PHASE_AB_AUDIENCE = "ab_audience"
PHASE_AB_PURPOSE = "ab_purpose"
PHASE_AB_SHAPE = "ab_shape"
PHASE_AB_BRIEFING = "ab_briefing"
PHASE_AB_CHANNEL = "ab_channel"
PHASE_AB_CONSENT_TONE = "ab_consent_tone"
PHASE_AB_NAME = "ab_name"
PHASE_AB_PLAN = "ab_plan"
PHASE_AB_CREDENTIALS = "ab_credentials"
PHASE_AB_PROVISION = "ab_provision"
PHASE_AB_SMOKE_WRAP = "ab_smoke_wrap"


FieldType = Literal["string", "string_list"]

# Bot-purpose archetypes — adopted EXACTLY from the effectiveness-layer
# spec §4 (docs/spec-effectiveness-layer-2026-06-09.md): same enum, no
# parallel vocabulary. The archetype the add-bot wizard captures is the
# same key the starter packs (delta §5) and the Layer-2 playbook
# selector consume — one vocabulary, three consumers, no mapping tables.
AB_ARCHETYPES: tuple[str, ...] = (
    "personal-assistant",
    "project-manager",
    "home-automation",
    "research-analyst",
    "customer-facing",
    "custom",
)


def normalize_archetype(value: Any) -> str | None:
    """Map an extracted archetype string onto the canonical enum, or
    ``None`` when it isn't one. Tolerates case / space / underscore
    variants ("Personal Assistant" → "personal-assistant") but does NOT
    guess from free text — that's the extractor's job; this is the
    schema gate."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace("_", "-").replace(" ", "-")
    return s if s in AB_ARCHETYPES else None


def account_name_for(bot_name: Any) -> str:
    """Derive the lowercase account name from a bot's display name —
    "Atlas" → "atlas", "Field Notes" → "field_notes". Returns "" when
    no valid account name can be derived (the AB_NAME exit gates on
    this, so the wizard keeps the naming conversation going instead of
    carrying an unusable name into the plan).

    Underscores, never hyphens: the account name becomes the bot_id and
    the macOS user, and bot_ids are ``[a-z][a-z0-9_]{1,31}`` everywhere
    (hyphens clash with launchd label namespacing — see ``_BOT_ID_RE``
    in web/wizard_routes.py). M1 emitted hyphens here; M2 provisions the
    account, so the slug now matches the provisioning contract."""
    if not isinstance(bot_name, str):
        return ""
    import re as _re
    s = bot_name.strip().lower()
    s = _re.sub(r"[\s-]+", "_", s)
    s = _re.sub(r"[^a-z0-9_]", "", s)
    s = _re.sub(r"_{2,}", "_", s).strip("_")
    if not _re.fullmatch(r"[a-z][a-z0-9_]{1,31}", s or ""):
        return ""
    return s

# Render mode declares whether a phase's prompt is for the LLM (agenda) or
# for the user directly (verbatim). See docs/spec-wizard-render-modes-2026-05-15.md
# (TBD) and the discussion in PR for the verbatim wizard work for the rationale.
#
# - ``verbatim`` (default): the body is exactly what the user should see.
#   The plugin direct-sends it via channel transport (Telegram Bot API,
#   slack chat.postMessage, etc.) and instructs the LLM to stay silent.
#   Zero LLM cost, zero hallucination risk. Use for: one-shot info dumps,
#   step-by-step checklists, terminal/error renders, every phase that has
#   a deterministic-classifier reply (cancel/save/edit/etc.).
#
# - ``agenda``: the body is guidance for the LLM to interpret and respond
#   to in its own voice. The plugin attaches it as systemAppend; the LLM
#   engages naturally. Use only where conversational LLM extraction adds
#   genuine value — open-ended fact gathering, voice-flavored chat.
#
# Default is ``agenda`` because that's how the wizard originally shipped
# and where most existing prompts live. Phases with a canonical
# user-facing body (URLs / step lists / rendered specs / preview blocks
# the user must see exactly) opt INTO verbatim — explicit, audited.
#
# Background: the wizard's all-agenda design was vulnerable when the
# canonical body included technical terms (e.g. "evo setup-google").
# admin_bot's LLM saw the keyword in the user message and the prompt,
# decided it needed to figure out what setup-google does, and burned 52
# tool calls investigating instead of rendering the body. The fix
# (verbatim mode) bypasses the LLM entirely for canonical-body phases.
# See PR for the verbatim wizard work.
RenderMode = Literal["verbatim", "agenda"]


@dataclass(frozen=True)
class FieldSpec:
    """One target field for the extractor — name + description + type."""

    name: str
    description: str
    type: FieldType = "string"


@dataclass(frozen=True)
class Phase:
    name: str
    description: str  # one-liner used in prompts ("phase: about_you / gathering role + environment")
    targets: tuple[FieldSpec, ...]
    exit_condition: Callable[[dict[str, Any]], bool]
    next_phase: str | None  # None on terminal phase
    # Render mode — see ``RenderMode`` docstring above. Defaults to
    # ``agenda`` (matches the wizard's original LLM-driven design).
    # Canonical-body phases (URLs / rendered specs / preview blocks)
    # opt INTO verbatim for direct-send + zero hallucination risk.
    render_mode: RenderMode = "agenda"

    @property
    def has_extractor(self) -> bool:
        return bool(self.targets)


# ─────────────────────────────────────────────────────────────────────────────
# Exit-condition helpers
# ─────────────────────────────────────────────────────────────────────────────


def _has_value(state: dict[str, Any], key: str) -> bool:
    """True iff ``state[key]`` is a non-empty string or non-empty list."""
    v = state.get(key)
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return bool(v)
    return False


def _always_exit(_state: dict[str, Any]) -> bool:
    return True


def _about_you_exit(state: dict[str, Any]) -> bool:
    """Per spec §4.2: exit ABOUT_YOU when we have ``name`` plus at least
    one of ``role`` or ``environment``. Keeps the back-and-forth short for
    cooperative users while not letting it run forever for evasive ones —
    we accept a short-but-grounded profile rather than holding out for
    everything."""
    if not _has_value(state, "name"):
        return False
    return _has_value(state, "role") or _has_value(state, "environment")


def _goals_exit(state: dict[str, Any]) -> bool:
    """Per spec §4.2: exit GOALS when at least one goal is articulated.
    Pain points and workflow notes are optional bonus structure — the
    wizard should not refuse to advance because the user articulated a
    goal but didn't volunteer their gripes about the status quo."""
    return _has_value(state, "top_goals")


def _secondary_about_you_exit(state: dict[str, Any]) -> bool:
    """Secondary's about_you exits as soon as we have a name. Lighter
    than primary's because we're already orienting around the bot, not
    the user — team_role is a nice-to-have, not a gate."""
    return _has_value(state, "name")


def _forge_design_exit(state: dict[str, Any]) -> bool:
    """Exit FORGE_DESIGN once we have enough to draft a build_spec —
    name + description AT MINIMUM. Capabilities and examples are nice
    bonuses; the conversational_summary the bot's been maintaining is
    the load-bearing artifact regardless."""
    return _has_value(state, "forge_app_name") and _has_value(state, "forge_description")


def _guide_gather_exit(state: dict[str, Any]) -> bool:
    """Exit GUIDE_GATHER once we have *both* an audience and a tone OR a
    body outline — those are the load-bearing fields for the guide.
    do_say / dont_say are bonus structure that the CONFIRM step still
    surfaces if present."""
    has_audience = _has_value(state, "audience")
    has_tone = _has_value(state, "tone")
    has_body = _has_value(state, "body_outline") or _has_value(state, "body")
    return has_audience and (has_tone or has_body)


def _ab_need_exit(state: dict[str, Any]) -> bool:
    """Exit AB_NEED once the underlying need is articulated. The
    research-assistant vs co-pilot framing and the mission line are
    bonus structure — later phases re-extract them, so we don't hold
    the conversation hostage here."""
    return _has_value(state, "ab_need")


def _ab_audience_exit(state: dict[str, Any]) -> bool:
    """Exit AB_AUDIENCE once we know who interacts with the bot. The
    surface and shared-vs-private-feed answers are bonus structure."""
    return _has_value(state, "ab_audiences")


def _ab_purpose_exit(state: dict[str, Any]) -> bool:
    """Exit AB_PURPOSE only with a schema-valid purpose: a mission line
    plus an archetype that normalizes onto the effectiveness-layer enum.
    This is the field the wizard exists to capture — no fuzzy exits."""
    if not _has_value(state, "ab_mission"):
        return False
    return normalize_archetype(state.get("ab_archetype")) is not None


def _ab_name_exit(state: dict[str, Any]) -> bool:
    """Exit AB_NAME once the chosen name yields a usable account name
    ("Atlas" → "atlas"). A name that slugs to nothing keeps the naming
    conversation open rather than carrying junk into the plan."""
    return bool(account_name_for(state.get("ab_bot_name")))


def ab_group_audience(state: dict[str, Any]) -> bool:
    """True when the bot serves more than just the admin — a group,
    team, household, or any second audience. Drives the consent phase's
    posture (pinned notice + opt-out gesture vs. personal DNT) and the
    audience-scoping seed on installed apps (delta §7)."""
    audiences = state.get("ab_audiences")
    if isinstance(audiences, list):
        if len(audiences) > 1:
            return True
        joined = " ".join(str(a).lower() for a in audiences)
    else:
        joined = ""
    surface = str(state.get("ab_surface") or "").lower()
    groupish = ("group", "team", "channel", "household", "family",
                "community", "members", "customers", "clients")
    return any(g in joined or g in surface for g in groupish)


def ab_telegram_group_surface(state: dict[str, Any]) -> bool:
    """True when the bot lives on a Telegram group — the one surface
    where the privacy-mode question (see every message vs. mentions
    only) applies (delta §7, 05-28 friction #6)."""
    surface = str(state.get("ab_surface") or "").lower()
    return "telegram" in surface and ab_group_audience(state)


def _ab_consent_tone_exit(state: dict[str, Any]) -> bool:
    """Non-skippable (delta §7): a bot with an audience never finishes
    without an explicit consent answer and a tone. Group-audience bots
    additionally need the opt-out gesture pinned down, and Telegram
    group bots the privacy-mode answer — those are the questions the
    reference design says must never be skipped."""
    if not (_has_value(state, "ab_consent_notice") and _has_value(state, "ab_tone")):
        return False
    if ab_group_audience(state) and not _has_value(state, "ab_opt_out"):
        return False
    if ab_telegram_group_surface(state) and not _has_value(state, "ab_tg_privacy"):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Primary phase definitions
# ─────────────────────────────────────────────────────────────────────────────


CHALLENGE_PHASE = Phase(
    name=PHASE_CHALLENGE,
    description="passphrase challenge — verify caller is admin or primary",
    # No LLM-extractor targets. The engine special-cases this phase: it
    # runs a deterministic passphrase match against ``network.pod`` and
    # applies admin/primary claims directly via :mod:`evo.identity`.
    targets=(),
    # Always exits after one user reply, whether or not the passphrase
    # matched. The engine decides what to do next based on whether any
    # claim was applied.
    exit_condition=lambda _state: True,
    next_phase=PHASE_GREET,  # advance to greet on successful claim;
                             # on refusal the engine terminates instead
)


GREET_PHASE = Phase(
    name=PHASE_GREET,
    description="kickoff — greet, explain briefly, ask first name",
    targets=(
        FieldSpec(
            "name",
            "the user's preferred first name (or whatever they want to be called)",
            "string",
        ),
    ),
    # GREET's job is to ask the name. Always advance after one user reply
    # whether or not we successfully extracted — ABOUT_YOU can re-ask.
    exit_condition=_always_exit,
    next_phase=PHASE_ABOUT_YOU,
    # render_mode defaults to "agenda" — bot greets in its own voice.
)


ABOUT_YOU_PHASE = Phase(
    name=PHASE_ABOUT_YOU,
    description="getting basic context: role, environment, current tooling",
    targets=(
        FieldSpec("name", "the user's preferred first name", "string"),
        FieldSpec(
            "role",
            "what the user does (job title, role on a team, "
            "or freeform self-description)",
            "string",
        ),
        FieldSpec(
            "environment",
            "where the user works / what kind of project or organization "
            "they're in (company name, family/household context, hobby, etc.)",
            "string",
        ),
        FieldSpec(
            "current_tooling",
            "tools / systems the user is currently using and might want this "
            "bot to interact with (e.g. Slack, GitHub, Calendar, Notion). "
            "List of strings.",
            "string_list",
        ),
    ),
    exit_condition=_about_you_exit,
    next_phase=PHASE_GOALS,
    # render_mode defaults to "agenda" — open-ended fact gathering
    # benefits from bot-voice conversation + LLM extractor.
)


GOALS_PHASE = Phase(
    name=PHASE_GOALS,
    description="what the user wants to use this bot to accomplish",
    targets=(
        FieldSpec(
            "top_goals",
            "the user's main goals for using this bot — concrete things they "
            "want it to help with. Short phrases, not paragraphs. List of "
            "strings.",
            "string_list",
        ),
        FieldSpec(
            "pain_points",
            "specific frustrations the user mentions — places where their "
            "current setup falls short or causes friction. List of short "
            "strings.",
            "string_list",
        ),
        FieldSpec(
            "current_workflow_notes",
            "free-form notes about how the user currently handles the work "
            "this bot might touch — the 'before-state' so future changes "
            "have something to be measured against.",
            "string",
        ),
    ),
    exit_condition=_goals_exit,
    next_phase=PHASE_PLATFORM_TOUR,
    # render_mode defaults to "agenda" — open-ended elicitation.
)


PLATFORM_TOUR_PHASE = Phase(
    name=PHASE_PLATFORM_TOUR,
    description="brief overview of what Evolve can do",
    # No extraction targets — the platform tour is a one-turn explainer.
    # The bot reads the prompt, delivers the overview in its own voice,
    # and the wizard advances to gallery recs on the user's next reply.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_GALLERY_RECS,
)


GALLERY_RECS_PHASE = Phase(
    name=PHASE_GALLERY_RECS,
    description="surface 1-3 gallery apps that match the user's profile",
    # No LLM-extractor targets — the engine handles this phase deterministically:
    # loads candidates from the gallery, renders them in the prompt, and
    # classifies the user's reply on the next turn (accept / dismiss / enough).
    # apps_accepted and apps_dismissed land in state.extracted via the
    # engine, so they round-trip into the user profile on Wrap.
    targets=(),
    exit_condition=_always_exit,  # engine controls advance based on classification
    next_phase=PHASE_WRAP,
)


# ─────────────────────────────────────────────────────────────────────────────
# Secondary chain — for non-primary users who opted in to a bot tour
# ─────────────────────────────────────────────────────────────────────────────


SECONDARY_GREET_PHASE = Phase(
    name=PHASE_SECONDARY_GREET,
    description="bot-flavored intro for a secondary user, reading from the bot guide",
    targets=(
        FieldSpec(
            "name",
            "the user's preferred first name (or whatever they want to be called)",
            "string",
        ),
    ),
    # Always advance after one user reply. The bot reads its guide,
    # introduces itself in the primary's voice, and asks the visitor's
    # name. SECONDARY_ABOUT_YOU re-asks if extraction missed.
    exit_condition=_always_exit,
    next_phase=PHASE_SECONDARY_ABOUT_YOU,
    # render_mode defaults to "agenda" — bot voice depends on guide.
)


SECONDARY_ABOUT_YOU_PHASE = Phase(
    name=PHASE_SECONDARY_ABOUT_YOU,
    description="light context: the visitor's role on the team / household",
    targets=(
        FieldSpec("name", "the user's preferred first name", "string"),
        FieldSpec(
            "team_role",
            "the visitor's role relative to the bot's primary or team — "
            "e.g. 'engineer on the same team', 'partner', 'household member'. "
            "Short phrase.",
            "string",
        ),
    ),
    exit_condition=_secondary_about_you_exit,
    next_phase=PHASE_HOW_TO_USE,
    # render_mode defaults to "agenda" — mirrors primary ABOUT_YOU.
)


HOW_TO_USE_PHASE = Phase(
    name=PHASE_HOW_TO_USE,
    description="reads the bot guide's do_say / dont_say / tone, ends with an invitation",
    # No extraction — this is purely a one-turn rendering of the
    # primary's authored guidance.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_SECONDARY_WRAP,
)


SECONDARY_WRAP_PHASE = Phase(
    name=PHASE_SECONDARY_WRAP,
    description="close out the secondary tour and invite real interaction",
    targets=(),
    exit_condition=_always_exit,
    next_phase=None,  # terminal — finalize on reach
)


# ─────────────────────────────────────────────────────────────────────────────
# Guide-draft chain (5b6) — primary authors the bot_guide conversationally
# ─────────────────────────────────────────────────────────────────────────────


GUIDE_GATHER_PHASE = Phase(
    name=PHASE_GUIDE_GATHER,
    description="gather guide fields: audience, tone, do_say, dont_say, body",
    targets=(
        FieldSpec(
            "audience",
            "who this bot is for — short description of the team / household / "
            "people who'll be using it (e.g. 'engineering team, ~6 people').",
            "string",
        ),
        FieldSpec(
            "tone",
            "the voice / tone the bot should use with this audience "
            "(e.g. 'direct, no emoji', 'warm and chatty', 'concise').",
            "string",
        ),
        FieldSpec(
            "do_say",
            "things the user wants the bot to lean toward — what it should "
            "help with, when to engage. List of short phrases.",
            "string_list",
        ),
        FieldSpec(
            "dont_say",
            "topics or behaviors the bot should avoid (e.g. 'no HR or "
            "payroll', 'don't joke about deadlines'). List of short phrases.",
            "string_list",
        ),
        FieldSpec(
            "body_outline",
            "free-form prose describing what this bot is for in a sentence "
            "or two — becomes the body of the saved guide. The user might "
            "say 'team_bot_a handles deploys and CI'.",
            "string",
        ),
    ),
    exit_condition=_guide_gather_exit,
    next_phase=PHASE_GUIDE_CONFIRM,
    # render_mode defaults to "agenda" — open-ended elicitation.
)


GUIDE_CONFIRM_PHASE = Phase(
    name=PHASE_GUIDE_CONFIRM,
    description="render the proposed guide and gate save on user confirmation",
    # The engine special-cases this phase: it doesn't run the LLM
    # extractor, it pattern-matches the user's reply for save/cancel/edit
    # and either writes the guide and finalizes, or kicks back to GATHER,
    # or terminates without saving.
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing on this one
    next_phase=None,  # terminal — engine finalizes on save / cancel
    # Verbatim — the rendered guide preview is canonical text the user
    # must see exactly to make the save/cancel/edit call.
    render_mode="verbatim",
)


# ─────────────────────────────────────────────────────────────────────────────
# Forge-via-messaging chain (5b7b) — conditional branch from GALLERY_RECS
# ─────────────────────────────────────────────────────────────────────────────


FORGE_INTRO_PHASE = Phase(
    name=PHASE_FORGE_INTRO,
    description="ask whether the user wants a custom app built for unmet goals",
    # Engine special-cases this phase: classifies user reply as opt-in /
    # opt-out and routes accordingly. No LLM extractor.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_FORGE_DESIGN,
)


FORGE_DESIGN_PHASE = Phase(
    name=PHASE_FORGE_DESIGN,
    description="conversational design loop — gather what the app should be",
    targets=(
        FieldSpec(
            "forge_app_name",
            "a short, memorable name for the proposed app (e.g. "
            "'pr-watcher', 'standup-helper'). Lowercase, hyphens. "
            "If the user describes the app without naming it, you can "
            "propose a name and let them push back.",
            "string",
        ),
        FieldSpec(
            "forge_description",
            "one or two sentences capturing what the app does — what "
            "user-facing behavior the bot will pick up once this app is "
            "installed. The user's voice; the bot's framing OK either way.",
            "string",
        ),
        FieldSpec(
            "forge_capabilities",
            "the integrations / tools / data sources the app will rely "
            "on (e.g. 'github', 'google calendar', 'slack', 'shell scripts'). "
            "Short list of strings.",
            "string_list",
        ),
        FieldSpec(
            "forge_example_behaviors",
            "concrete behaviors the user wants — e.g. 'every weekday at "
            "9am summarize PRs awaiting review', 'when CI fails on main "
            "DM the on-call'. List of short phrases.",
            "string_list",
        ),
        FieldSpec(
            "forge_conversational_summary",
            "a running paragraph capturing what the bot and user have "
            "agreed the app does. Updates each turn as the conversation "
            "progresses. This becomes the conversational_summary field "
            "on the saved manifest.",
            "string",
        ),
    ),
    exit_condition=_forge_design_exit,
    next_phase=PHASE_FORGE_CONFIRM,
    # render_mode defaults to "agenda" — conversational design loop.
)


FORGE_CONFIRM_PHASE = Phase(
    name=PHASE_FORGE_CONFIRM,
    description="render proposed build summary and gate kickoff on user confirmation",
    # Engine special-cases this phase: pattern-matches build/edit/cancel
    # on the user reply and either kicks off the forge job + finalizes,
    # or kicks back to FORGE_DESIGN, or terminates without building.
    targets=(),
    exit_condition=_always_exit,
    next_phase=None,  # terminal — engine finalizes on build / cancel
    # Verbatim — the rendered build summary is canonical text.
    render_mode="verbatim",
)


# ─────────────────────────────────────────────────────────────────────────────
# Conversational approval (5b8) — single-phase mini-wizard entered when
# the user types bare ``evo``. Coexists with the forge chain above.
# ─────────────────────────────────────────────────────────────────────────────


REC_PENDING_PHASE = Phase(
    name=PHASE_REC_PENDING,
    description="pitch a Better Engine recommendation and route the user's reply",
    # Engine special-cases this phase identically to GUIDE_CONFIRM and
    # GALLERY_RECS: no extractor, deterministic stage-1 classifier with
    # an LLM stage-2 fallback (see :mod:`evo.wizard.intent`), and the
    # handler drives state transitions itself rather than relying on
    # exit_condition. The pending rec lives in state.extracted under an
    # underscore-prefixed key so :func:`profile.commit` skips it.
    targets=(),
    exit_condition=_always_exit,  # engine controls routing
    next_phase=None,  # terminal — engine finalizes when the queue empties
                      # or the user disengages
    # Verbatim: the renderer produces user-facing text (template-based,
    # not an LLM directive). Tried the agenda model first — the LLM was
    # supposed to pitch the rec in its own voice — but compliance
    # cratered under the suppress-LLM regime that every other `evo`
    # subcommand uses: prior turns' "produce exactly a period" pattern
    # poisoned the conversation context, so by the time `evo better`
    # fired the LLM was emitting `.` instead of the pitch. Going
    # deterministic gives up some persona warmth but matches the
    # reliability of every other `evo X` subcommand.
    render_mode="verbatim",
)


# ─────────────────────────────────────────────────────────────────────────────
# App-promotion offer (AL-1.7) — design §7.2's phase pair
# ─────────────────────────────────────────────────────────────────────────────


APP_PROMOTE_OFFER_PHASE = Phase(
    name=PHASE_APP_PROMOTE_OFFER,
    description="offer to promote a discovered draft to an app; route the reply",
    # Engine-special-cased exactly like REC_PENDING: no LLM extractor, the
    # deterministic stage-1 classifier in :mod:`evo.wizard.intent` with the
    # stage-2 LLM fallback, and the handler drives transitions itself. The
    # pending offer lives in state.extracted under an underscore-prefixed key
    # so :func:`profile.commit` skips it.
    targets=(),
    exit_condition=_always_exit,  # engine controls routing
    next_phase=None,  # terminal — engine finalizes on yes / no / never
    # Verbatim, for REC_PENDING's recorded reason (see REC_PENDING_PHASE): LLM
    # pitch compliance cratered under the suppress-LLM regime every ``evo``
    # subcommand runs under. The offer text is a deterministic template built by
    # ``applications.app_promotion.offer_text``.
    render_mode="verbatim",
)


APP_PROMOTE_CONFIRM_PHASE = Phase(
    name=PHASE_APP_PROMOTE_CONFIRM,
    description="echo a renamed app back to the user before conferring identity",
    # Entered ONLY from a rename. Design §7.2: "Rename → applied before
    # conferring" — the id is slugged from the name, so a mis-heard name would
    # become a durable identity that §7.3a's ADOPT rule then protects forever.
    # That asymmetry is why a rename gets a confirmation and a plain yes does
    # not.
    targets=(),
    exit_condition=_always_exit,
    next_phase=None,  # terminal — engine finalizes on confirm / cancel
    render_mode="verbatim",
)

# ─────────────────────────────────────────────────────────────────────────────
# App-create chain (Wave 3) — `evo app create`
# ─────────────────────────────────────────────────────────────────────────────


APP_CREATE_DESCRIBE_PHASE = Phase(
    name=PHASE_APP_CREATE_DESCRIBE,
    description="ask the user to describe the app in one paragraph",
    # Engine special-cases this phase: the user's reply IS the
    # description — no LLM extractor. The handler stores it in
    # state.extracted['_app_description'] (underscore-prefixed so the
    # state file's optional profile commit, were it ever to run for
    # this audience, would skip it) and calls `_build_draft` to advance.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_APP_CREATE_REVIEW,
    # Verbatim — short canonical "describe the app" prompt + examples.
    render_mode="verbatim",
)


APP_CREATE_REVIEW_PHASE = Phase(
    name=PHASE_APP_CREATE_REVIEW,
    description="present the drafted spec and route approve/iterate/cancel",
    # Engine special-cases this phase: no extractor, deterministic
    # classifier for the user's reply (`approve` / `iterate <feedback>` /
    # `cancel`). The current draft lives in
    # state.extracted['_current_draft'].
    targets=(),
    exit_condition=_always_exit,
    next_phase=None,  # terminal — engine finalizes on approve / cancel
    # Verbatim — drafted spec is canonical text the user must see exactly
    # to make the approve/iterate/cancel call.
    render_mode="verbatim",
)


# ─────────────────────────────────────────────────────────────────────────────
# Google setup chain — `evo setup-google`
# ─────────────────────────────────────────────────────────────────────────────

# Each phase is engine-special-cased — no LLM extractor. The user's
# reply is a single value (URL / consent token / client_id / secret)
# validated deterministically, with `cancel` accepted at every step.
# All five share `next_phase=None` because the engine drives the chain
# explicitly rather than via exit_condition. All five are verbatim mode:
# every render is canonical text (URLs, exact CLI commands, exact
# Console steps), and the LLM has zero useful contribution.

GOOGLE_SETUP_INTRO_PHASE = Phase(
    name=PHASE_GOOGLE_SETUP_INTRO,
    description="pitch the setup, confirm operator wants to proceed",
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_GOOGLE_SETUP_ADMIN_URL,
    render_mode="verbatim",
)


GOOGLE_SETUP_ADMIN_URL_PHASE = Phase(
    name=PHASE_GOOGLE_SETUP_ADMIN_URL,
    description="collect the admin server's externally-reachable base URL",
    # Stored in state.extracted['_admin_base_url']. Needed before the
    # console phase because we tell the operator the exact redirect URI
    # to whitelist in Google Cloud Console.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_GOOGLE_SETUP_CONSOLE,
    render_mode="verbatim",
)


GOOGLE_SETUP_CONSOLE_PHASE = Phase(
    name=PHASE_GOOGLE_SETUP_CONSOLE,
    description="render the Google Cloud Console checklist, wait for 'done'",
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_GOOGLE_SETUP_CLIENT_ID,
    render_mode="verbatim",
)


GOOGLE_SETUP_CLIENT_ID_PHASE = Phase(
    name=PHASE_GOOGLE_SETUP_CLIENT_ID,
    description="collect the OAuth client_id (validated)",
    # Stored in state.extracted['_client_id']. Validation: ends with
    # `.apps.googleusercontent.com`.
    targets=(),
    exit_condition=_always_exit,
    next_phase=PHASE_GOOGLE_SETUP_CLIENT_SECRET,
    render_mode="verbatim",
)


GOOGLE_SETUP_CLIENT_SECRET_PHASE = Phase(
    name=PHASE_GOOGLE_SETUP_CLIENT_SECRET,
    description="collect the client_secret, then commit + finalize",
    # Stored transiently in state.extracted['_client_secret']. On the
    # commit step the secret moves to auth-profiles.json (via
    # `_save_google_oauth_client`) and is dropped from the wizard state
    # before the file is rewritten.
    targets=(),
    exit_condition=_always_exit,
    next_phase=None,  # terminal — engine commits + finalizes
    render_mode="verbatim",
)


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot chain — `evo add-bot` (admin-only, delta spec §3.2, M1 slice)
# ─────────────────────────────────────────────────────────────────────────────

# Shared field specs: the mission and archetype are listed as targets on
# every open phase, not just AB_PURPOSE. The extractor only ever sees the
# user's words, and the words that pin down a bot's purpose mostly arrive
# while the user is describing the need and the audience — by the time
# AB_PURPOSE renders, its job is usually to *confirm* an already-extracted
# purpose in plain language, exactly as the delta prescribes.

_AB_MISSION_FIELD = FieldSpec(
    "ab_mission",
    "a one-line mission for the new bot, in the user's own framing — what "
    "the bot's job is and for whom (e.g. 'Watch the ecosystem and post a "
    "daily digest to the group.'). Compose/update it from what the user "
    "has said so far; refine as the conversation sharpens.",
    "string",
)

_AB_ARCHETYPE_FIELD = FieldSpec(
    "ab_archetype",
    "the closest role for the new bot, EXACTLY one of: "
    "personal-assistant (keeps one person's life/schedule on track), "
    "project-manager (tracks work, tasks, commitments for a project or "
    "team), home-automation (runs/automates things around a household), "
    "research-analyst (watches a topic, gathers and summarizes findings), "
    "customer-facing (talks to a business's customers or clients), "
    "custom (none of the above fit). Only fill this when the "
    "conversation so far clearly supports one value.",
    "string",
)


AB_NEED_PHASE = Phase(
    name=PHASE_AB_NEED,
    description="why before what — the underlying need and who it serves",
    targets=(
        FieldSpec(
            "ab_need",
            "the underlying problem or job the user wants a new bot to "
            "take on — the need, not a feature list. Short prose.",
            "string",
        ),
        FieldSpec(
            "ab_bot_kind",
            "whether the user wants a research-assistant style bot "
            "(surfaces information, the human decides) or a co-pilot "
            "style bot (acts and produces work on their behalf). Short "
            "phrase like 'research assistant' or 'co-pilot'.",
            "string",
        ),
        _AB_MISSION_FIELD,
        _AB_ARCHETYPE_FIELD,
    ),
    exit_condition=_ab_need_exit,
    next_phase=PHASE_AB_AUDIENCE,
    # render_mode defaults to "agenda" — open-ended need finding.
)


AB_AUDIENCE_PHASE = Phase(
    name=PHASE_AB_AUDIENCE,
    description="who interacts: one audience or several, and on what surface",
    targets=(
        FieldSpec(
            "ab_audiences",
            "who interacts with the new bot — e.g. 'just me', 'a Telegram "
            "group of enthusiasts', 'my household', 'the support team'. "
            "List of short phrases, one per audience.",
            "string_list",
        ),
        FieldSpec(
            "ab_surface",
            "where the bot lives / where people talk to it — e.g. 'a "
            "Telegram group', 'Slack', 'direct messages'. Short phrase.",
            "string",
        ),
        FieldSpec(
            "ab_shared_feed",
            "when there's more than one audience: whether everyone sees "
            "the same content or the user wants a private feed for things "
            "only they see. Short phrase like 'one shared feed' or "
            "'private feed for me'.",
            "string",
        ),
        _AB_MISSION_FIELD,
        _AB_ARCHETYPE_FIELD,
    ),
    exit_condition=_ab_audience_exit,
    next_phase=PHASE_AB_PURPOSE,
    # render_mode defaults to "agenda".
)


AB_PURPOSE_PHASE = Phase(
    name=PHASE_AB_PURPOSE,
    description="confirm the bot's purpose (mission + role) in plain words",
    targets=(
        _AB_MISSION_FIELD,
        _AB_ARCHETYPE_FIELD,
    ),
    exit_condition=_ab_purpose_exit,
    next_phase=PHASE_AB_SHAPE,
    # render_mode defaults to "agenda" — the bot mirrors the purpose back
    # and the user confirms or corrects; the extractor refines the fields.
)


AB_SHAPE_PHASE = Phase(
    name=PHASE_AB_SHAPE,
    description="propose the starter apps for the role; adjust in conversation",
    # Engine special-cases this phase (delta §5): on entry it loads the
    # role's starter pack from the gallery (seam-injected in tests) and
    # stores the proposal; the reply is classified deterministically
    # (accept / none / drop-or-add adjustments by app name / cancel).
    # No LLM extractor — the proposal universe is a fixed list, so
    # name-matching beats extraction here.
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    next_phase=PHASE_AB_BRIEFING,
    # render_mode stays "agenda" — the bot voices the proposal and the
    # cost/time preview conversationally; only reply handling is
    # deterministic (same split as AB_CREDENTIALS).
)


AB_BRIEFING_PHASE = Phase(
    name=PHASE_AB_BRIEFING,
    description="morning briefing default-on; explicit opt-out turn",
    # Engine special-cases this phase (delta §6): the offer is the
    # decided default (on, ~7am, changeable later); the reply is a
    # deterministic yes / no classification. Only offered when the
    # conversation captured a place for the bot to live — without a
    # surface the engine skips straight to consent, recording "off".
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    # The engine routes to AB_CHANNEL when the captured surface supports
    # an in-conversation connect (Telegram today), straight to consent
    # otherwise — see _ab_advance_past_briefing.
    next_phase=PHASE_AB_CHANNEL,
    # render_mode stays "agenda" — the offer is voiced; the §6 copy
    # rides in the agenda block.
)


AB_CHANNEL_PHASE = Phase(
    name=PHASE_AB_CHANNEL,
    description="optional channel connect: paste a token now, or later",
    # Engine special-cases this phase (offer-now / auto-activate-later,
    # 2026-06-11 design sync): only entered when the captured surface
    # supports an in-conversation token connect (Telegram today). The
    # reply is handled deterministically — a pasted channel token must
    # never round-trip through LLM extraction, exactly like the API key
    # at AB_CREDENTIALS — and a validated token is held in memory only
    # (channel_driver), applied after the bot is built. Skipping is a
    # first-class answer: the recorded briefing decision auto-activates
    # when a channel is connected later.
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    next_phase=PHASE_AB_CONSENT_TONE,
    # render_mode stays "agenda" — the offer is voiced; reply handling
    # is deterministic (same split as AB_CREDENTIALS).
)


AB_CONSENT_TONE_PHASE = Phase(
    name=PHASE_AB_CONSENT_TONE,
    description="consent posture + tone — non-skippable for any audience",
    targets=(
        FieldSpec(
            "ab_consent_notice",
            "the plain-language notice for the people this bot serves — "
            "what it collects or does with what it sees, and how to opt "
            "out. Compose it from the conversation once the admin has "
            "agreed to a posture; refine as they adjust the wording.",
            "string",
        ),
        FieldSpec(
            "ab_opt_out",
            "the agreed opt-out: a reaction (e.g. 'react 🤐 to exclude a "
            "message'), a command, or 'ask the admin'. Short phrase.",
            "string",
        ),
        FieldSpec(
            "ab_tone",
            "the message style the admin wants for this bot (e.g. 'short "
            "and direct, no emoji', 'warm and chatty'). Short phrase.",
            "string",
        ),
        FieldSpec(
            "ab_tg_privacy",
            "for Telegram group bots only: whether the bot should see "
            "every message in the group or only messages that mention "
            "it. Short phrase like 'every message' or 'mentions only'.",
            "string",
        ),
    ),
    exit_condition=_ab_consent_tone_exit,
    next_phase=PHASE_AB_NAME,
    # render_mode defaults to "agenda" — open-ended consent/tone
    # conversation; the extractor fills the targets. Non-skippable via
    # the exit condition: no answers, no advance.
)


AB_NAME_PHASE = Phase(
    name=PHASE_AB_NAME,
    description="propose names with rationale, after the role is locked",
    targets=(
        FieldSpec(
            "ab_bot_name",
            "the name the user settled on for the new bot — a short word "
            "like 'Atlas'. Only fill this once the user has picked or "
            "clearly accepted a name (not while options are still being "
            "weighed).",
            "string",
        ),
    ),
    exit_condition=_ab_name_exit,
    next_phase=PHASE_AB_PLAN,
    # render_mode defaults to "agenda" — names are proposed with rationale.
)


AB_PLAN_PHASE = Phase(
    name=PHASE_AB_PLAN,
    description="echo the full plan for confirmation",
    # Engine special-cases this phase (like GUIDE_CONFIRM): no extractor
    # on the standard path — the handler classifies confirm / cancel and
    # treats anything else as change-feedback (re-extracting across the
    # whole chain's targets so "actually, make it weekly" just works).
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    # M2: confirm advances to the credential walk (the handler drives the
    # transition; next_phase documents the chain for tests/readers).
    next_phase=PHASE_AB_CREDENTIALS,
    # Verbatim — the plan echo is canonical text the user must see
    # exactly to make the confirm call.
    render_mode="verbatim",
)


AB_CREDENTIALS_PHASE = Phase(
    name=PHASE_AB_CREDENTIALS,
    description="walk the needed credential: borrow / paste / skip",
    # Engine special-cases this phase: the user's reply is classified
    # deterministically (borrow-from-<bot> / pasted key / skip / cancel)
    # and validated before the provision run starts. No LLM extractor —
    # a pasted API key must never round-trip through LLM extraction.
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    next_phase=PHASE_AB_PROVISION,
    # render_mode stays "agenda" (delta §3.2) — the bot voices the
    # credential ask conversationally; only the reply handling is
    # deterministic.
)


AB_PROVISION_PHASE = Phase(
    name=PHASE_AB_PROVISION,
    description="build the bot; relay progress in outcome language",
    # Engine special-cases this phase: the credential handler starts the
    # build in a background thread (see provision_driver); each user turn
    # here polls it and renders progress / success / honest failure.
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    next_phase=PHASE_AB_SMOKE_WRAP,
    # Verbatim — progress and failure reports are canonical text
    # (outcome lines, never internals).
    render_mode="verbatim",
)


AB_SMOKE_WRAP_PHASE = Phase(
    name=PHASE_AB_SMOKE_WRAP,
    description="smoke-test result; never leave a broken bot",
    # Engine special-cases this phase. Normally non-interactive (the
    # provision poll lands here with a passing smoke check and finalizes
    # in the same turn); on a failing smoke check it gates on the
    # admin's call — try again, or switch the bot off (quarantine).
    targets=(),
    exit_condition=_always_exit,  # engine controls the routing
    next_phase=None,  # terminal — engine finalizes
    render_mode="verbatim",
)


WRAP_PHASE = Phase(
    name=PHASE_WRAP,
    description="summarize what we learned and end the wizard",
    targets=(),  # no extraction; this is a closing turn
    exit_condition=_always_exit,
    next_phase=None,  # terminal — engine marks completed
)


_PRIMARY_PHASES: dict[str, Phase] = {
    PHASE_CHALLENGE: CHALLENGE_PHASE,
    PHASE_GREET: GREET_PHASE,
    PHASE_ABOUT_YOU: ABOUT_YOU_PHASE,
    PHASE_GOALS: GOALS_PHASE,
    PHASE_PLATFORM_TOUR: PLATFORM_TOUR_PHASE,
    PHASE_GALLERY_RECS: GALLERY_RECS_PHASE,
    PHASE_WRAP: WRAP_PHASE,
    # Secondary chain shares the same lookup table — phase names are
    # globally unique, so the engine can resolve any phase regardless of
    # which audience is running.
    PHASE_SECONDARY_GREET: SECONDARY_GREET_PHASE,
    PHASE_SECONDARY_ABOUT_YOU: SECONDARY_ABOUT_YOU_PHASE,
    PHASE_HOW_TO_USE: HOW_TO_USE_PHASE,
    PHASE_SECONDARY_WRAP: SECONDARY_WRAP_PHASE,
    # Guide-draft chain (5b6) — also in the same registry.
    PHASE_GUIDE_GATHER: GUIDE_GATHER_PHASE,
    PHASE_GUIDE_CONFIRM: GUIDE_CONFIRM_PHASE,
    # Forge-via-messaging chain (5b7b) — also in the same registry.
    PHASE_FORGE_INTRO: FORGE_INTRO_PHASE,
    PHASE_FORGE_DESIGN: FORGE_DESIGN_PHASE,
    PHASE_FORGE_CONFIRM: FORGE_CONFIRM_PHASE,
    # Conversational approval (5b8) — single-phase mini-wizard.
    PHASE_REC_PENDING: REC_PENDING_PHASE,
    # App-promotion offer chain (AL-1.7) — design §7.2.
    PHASE_APP_PROMOTE_OFFER: APP_PROMOTE_OFFER_PHASE,
    PHASE_APP_PROMOTE_CONFIRM: APP_PROMOTE_CONFIRM_PHASE,
    # App-create chain (Wave 3) — `evo app create`.
    PHASE_APP_CREATE_DESCRIBE: APP_CREATE_DESCRIBE_PHASE,
    PHASE_APP_CREATE_REVIEW: APP_CREATE_REVIEW_PHASE,
    # Google setup chain — `evo setup-google`.
    PHASE_GOOGLE_SETUP_INTRO: GOOGLE_SETUP_INTRO_PHASE,
    PHASE_GOOGLE_SETUP_ADMIN_URL: GOOGLE_SETUP_ADMIN_URL_PHASE,
    PHASE_GOOGLE_SETUP_CONSOLE: GOOGLE_SETUP_CONSOLE_PHASE,
    PHASE_GOOGLE_SETUP_CLIENT_ID: GOOGLE_SETUP_CLIENT_ID_PHASE,
    PHASE_GOOGLE_SETUP_CLIENT_SECRET: GOOGLE_SETUP_CLIENT_SECRET_PHASE,
    # Add-bot chain — `evo add-bot` (admin-only, M1 + M2 + M3).
    PHASE_AB_NEED: AB_NEED_PHASE,
    PHASE_AB_AUDIENCE: AB_AUDIENCE_PHASE,
    PHASE_AB_PURPOSE: AB_PURPOSE_PHASE,
    PHASE_AB_SHAPE: AB_SHAPE_PHASE,
    PHASE_AB_BRIEFING: AB_BRIEFING_PHASE,
    PHASE_AB_CHANNEL: AB_CHANNEL_PHASE,
    PHASE_AB_CONSENT_TONE: AB_CONSENT_TONE_PHASE,
    PHASE_AB_NAME: AB_NAME_PHASE,
    PHASE_AB_PLAN: AB_PLAN_PHASE,
    PHASE_AB_CREDENTIALS: AB_CREDENTIALS_PHASE,
    PHASE_AB_PROVISION: AB_PROVISION_PHASE,
    PHASE_AB_SMOKE_WRAP: AB_SMOKE_WRAP_PHASE,
}


def initial_phase(audience: str = "primary", role: str = "primary") -> str:
    """Return the entry phase for the given (audience, role).

    Recognized callers (admin / primary) start at GREET. Unrecognized
    callers (secondary, on a multi-user bot or one that already has a
    different primary recorded) start at CHALLENGE so they can identify
    themselves via passphrase before any onboarding happens.

    ``add_bot`` is audience-routed: the chain has its own entry phase and
    never passes through CHALLENGE (the dispatcher already gated the
    subcommand to admins before any session exists). Handled here so the
    engine's recover-from-unknown-phase path restarts the right chain.
    """
    if audience == "add_bot":
        return PHASE_AB_NEED
    if role == "secondary":
        return PHASE_CHALLENGE
    return PHASE_GREET


def get_phase(name: str) -> Phase | None:
    """Look up a phase definition by canonical name."""
    return _PRIMARY_PHASES.get(name)


def all_primary_phases() -> tuple[Phase, ...]:
    """Ordered tuple of primary phases. Excludes CHALLENGE because it's
    only entered for unrecognized callers — not part of the canonical
    primary onboarding chain.

    Order: Greet → About you → Goals → Platform tour → Gallery recs → Wrap."""
    return (
        GREET_PHASE,
        ABOUT_YOU_PHASE,
        GOALS_PHASE,
        PLATFORM_TOUR_PHASE,
        GALLERY_RECS_PHASE,
        WRAP_PHASE,
    )


def all_secondary_phases() -> tuple[Phase, ...]:
    """Ordered tuple of secondary phases. Order:
    Greet → About you → How to use → Wrap."""
    return (
        SECONDARY_GREET_PHASE,
        SECONDARY_ABOUT_YOU_PHASE,
        HOW_TO_USE_PHASE,
        SECONDARY_WRAP_PHASE,
    )


def all_guide_draft_phases() -> tuple[Phase, ...]:
    """Ordered tuple of guide-draft phases. Order: Gather → Confirm."""
    return (GUIDE_GATHER_PHASE, GUIDE_CONFIRM_PHASE)


def all_forge_phases() -> tuple[Phase, ...]:
    """Ordered tuple of forge-via-messaging phases. Order:
    Intro → Design → Confirm."""
    return (FORGE_INTRO_PHASE, FORGE_DESIGN_PHASE, FORGE_CONFIRM_PHASE)


def all_add_bot_phases() -> tuple[Phase, ...]:
    """Ordered tuple of add-bot phases. Order:
    Need → Audience → Purpose → Shape → Briefing → Channel →
    Consent+tone → Name → Plan → Credentials → Provision → Smoke-wrap."""
    return (
        AB_NEED_PHASE,
        AB_AUDIENCE_PHASE,
        AB_PURPOSE_PHASE,
        AB_SHAPE_PHASE,
        AB_BRIEFING_PHASE,
        AB_CHANNEL_PHASE,
        AB_CONSENT_TONE_PHASE,
        AB_NAME_PHASE,
        AB_PLAN_PHASE,
        AB_CREDENTIALS_PHASE,
        AB_PROVISION_PHASE,
        AB_SMOKE_WRAP_PHASE,
    )
