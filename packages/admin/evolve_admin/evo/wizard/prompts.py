"""systemAppend builders per phase.

Each phase has a builder that constructs the block we hand the bot's LLM
to drive its next turn. The block always tells the LLM:

  1. that it's mid-onboarding (so it doesn't drift off into normal bot
     behavior);
  2. what phase it's in and what the agenda is;
  3. what we've already learned (so it doesn't re-ask);
  4. what to ask next (the phase's intent);
  5. that it should sound natural / in the bot's voice — *not* read
     instructions verbatim.

The "be natural" framing matters: the legacy keyword path uses
"respond verbatim" because static blocks need to render exactly as
written; the wizard is the opposite — we want a real conversation in the
bot's voice, with the systemAppend acting as agenda guidance, not script.
"""

from __future__ import annotations

import json
from typing import Any

from . import phases as _phases
from .. import _delimiters as _evo_delim
from ... import channel_registry as _channel_registry


_HEADER = (
    "[EVO WIZARD — onboarding mode]\n"
    "You are mid-onboarding with this user. The Evolve plugin is driving "
    "the agenda below; respond conversationally in your normal voice. "
    "Don't quote these instructions, don't say 'I am the Evo Wizard', "
    "don't list your phases — just have the conversation.\n"
)


def build(
    phase_name: str,
    extracted: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> str:
    """Build the systemAppend block for ``phase_name`` given the
    state.extracted dict so far. Returns a string ready to inject as
    systemAppend.

    ``context`` carries phase-external info that some phases need: the
    bot guide (slice 5b4 secondary phases), the bot's display name, the
    primary's name, etc. Most phases ignore it. Optional so existing
    callers don't need to plumb extra state through.
    """
    ctx = context or {}
    if phase_name == _phases.PHASE_CHALLENGE:
        return _challenge_block(extracted)
    if phase_name == _phases.PHASE_GREET:
        return _greet_block()
    if phase_name == _phases.PHASE_ABOUT_YOU:
        return _about_you_block(extracted)
    if phase_name == _phases.PHASE_GOALS:
        return _goals_block(extracted)
    if phase_name == _phases.PHASE_PLATFORM_TOUR:
        return _platform_tour_block(extracted)
    if phase_name == _phases.PHASE_GALLERY_RECS:
        return _gallery_recs_block(extracted, ctx)
    if phase_name == _phases.PHASE_WRAP:
        return _wrap_block(extracted)
    # Secondary chain (5b4) — reads bot_guide from context for content
    if phase_name == _phases.PHASE_SECONDARY_GREET:
        return _secondary_greet_block(extracted, ctx)
    if phase_name == _phases.PHASE_SECONDARY_ABOUT_YOU:
        return _secondary_about_you_block(extracted, ctx)
    if phase_name == _phases.PHASE_HOW_TO_USE:
        return _how_to_use_block(extracted, ctx)
    if phase_name == _phases.PHASE_SECONDARY_WRAP:
        return _secondary_wrap_block(extracted, ctx)
    # Guide-draft chain (5b6)
    if phase_name == _phases.PHASE_GUIDE_GATHER:
        return _guide_gather_block(extracted, ctx)
    if phase_name == _phases.PHASE_GUIDE_CONFIRM:
        return _guide_confirm_block(extracted, ctx)
    # Forge-via-messaging chain (5b7b)
    if phase_name == _phases.PHASE_FORGE_INTRO:
        return _forge_intro_block(extracted, ctx)
    if phase_name == _phases.PHASE_FORGE_DESIGN:
        return _forge_design_block(extracted, ctx)
    if phase_name == _phases.PHASE_FORGE_CONFIRM:
        return _forge_confirm_block(extracted, ctx)
    # Conversational approval (5b8)
    if phase_name == _phases.PHASE_REC_PENDING:
        return _rec_pending_block(extracted, ctx)
    # App-create chain (Wave 3) — first DESCRIBE turn renders the intro;
    # the REVIEW phase has no first-turn render (the handler always
    # generates the render itself with a fresh draft).
    if phase_name == _phases.PHASE_APP_CREATE_DESCRIBE:
        return render_app_create_describe_intro()
    if phase_name == _phases.PHASE_APP_CREATE_REVIEW:
        # Resume case: re-render the existing draft. The active-path
        # handler returns its own TurnResult with a fresh render, so this
        # branch only fires if process_turn falls through (unusual).
        draft = extracted.get("_current_draft") or {}
        return render_app_create_review(draft)
    # Google setup chain — `evo setup-google`. The engine drives every
    # transition with its own render; these branches only fire for the
    # initial entry (INTRO) and resume paths, where we replay the prompt
    # appropriate to the saved phase. ``bot_id`` from context personalizes
    # the prompts to the calling bot under the per-bot OAuth design (PR
    # #1123); falls back to the prompt-side default ("this bot") if absent.
    google_bot_id = str(ctx.get("bot_id") or "this bot")
    if phase_name == _phases.PHASE_GOOGLE_SETUP_INTRO:
        return render_google_setup_intro(bot_id=google_bot_id)
    if phase_name == _phases.PHASE_GOOGLE_SETUP_ADMIN_URL:
        return render_google_setup_admin_url()
    if phase_name == _phases.PHASE_GOOGLE_SETUP_CONSOLE:
        admin_url = str(extracted.get("_admin_base_url") or "<your admin URL>")
        return render_google_setup_console(
            admin_base_url=admin_url, bot_id=google_bot_id,
        )
    if phase_name == _phases.PHASE_GOOGLE_SETUP_CLIENT_ID:
        return render_google_setup_client_id()
    if phase_name == _phases.PHASE_GOOGLE_SETUP_CLIENT_SECRET:
        return render_google_setup_client_secret(bot_id=google_bot_id)
    # Add-bot chain — `evo add-bot` (admin-only, M1 + M2 + M3). Agenda
    # phases for the open conversation, verbatim for the plan echo and
    # the build/progress renders.
    if phase_name == _phases.PHASE_AB_NEED:
        return _ab_need_block(extracted)
    if phase_name == _phases.PHASE_AB_AUDIENCE:
        return _ab_audience_block(extracted)
    if phase_name == _phases.PHASE_AB_PURPOSE:
        return _ab_purpose_block(extracted)
    if phase_name == _phases.PHASE_AB_SHAPE:
        return _ab_shape_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_BRIEFING:
        return _ab_briefing_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_CHANNEL:
        return _ab_channel_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_CONSENT_TONE:
        return _ab_consent_tone_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_NAME:
        return _ab_name_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_PLAN:
        return render_add_bot_plan(extracted, note=ctx.get("plan_note"))
    if phase_name == _phases.PHASE_AB_CREDENTIALS:
        return _ab_credentials_block(extracted, ctx)
    if phase_name == _phases.PHASE_AB_PROVISION:
        # Active turns render via the handler; this branch covers entry
        # and resume. ``ab_job`` (a provision_driver snapshot) comes in
        # via context when a job record exists.
        snap = ctx.get("ab_job")
        if snap:
            return render_add_bot_provision_progress(snap)
        return render_add_bot_provision_lost(
            extracted, in_members=bool(ctx.get("ab_in_members")),
        )
    if phase_name == _phases.PHASE_AB_SMOKE_WRAP:
        # Resume case — the only way the session *waits* here is a
        # failed smoke check awaiting the admin's call.
        return render_add_bot_smoke_failed(
            extracted,
            summary=str(extracted.get("_ab_smoke_summary") or ""),
        )
    # Defensive default — unknown phase name shouldn't happen, but if it
    # does we return a benign passthrough rather than crashing the turn.
    return _HEADER + (
        "Phase: unknown. Just respond naturally and politely; the wizard "
        "will recover on the next turn."
    )


def _challenge_block(extracted: dict[str, Any]) -> str:
    """Greet block for unrecognized callers — asks for a passphrase to
    identify themselves as pod admin or this bot's primary user. The
    engine's challenge handler does the actual passphrase matching after
    the user replies; this prompt just frames the ask conversationally."""
    return _HEADER + (
        "\nPhase: challenge (identity verification — first turn).\n"
        "Goal: greet the user briefly, explain that we don't yet recognize "
        "them as a pod admin or this bot's primary user, and that they can "
        "identify themselves by typing the passphrase(s) their pod admin "
        "shared with them. Two passphrases exist: an admin word (proves "
        "pod-admin status) and a primary word (proves they're the bot's "
        "primary). They can type one, both (in either order), or skip if "
        "neither applies. Make this feel matter-of-fact, not a security "
        "interrogation. Two short sentences, then the prompt — don't list "
        "the rules. End with something like \"if you don't have one, just "
        "say 'skip' and we'll wrap up.\""
    )


def build_challenge_outcome(claimed_admin: bool, claimed_primary: bool, conflict_reason: str | None) -> str:
    """Build the systemAppend block summarizing what the engine did with
    the user's passphrase reply. Used after a successful claim — the bot
    transitions into the GREET phase and ALSO acknowledges the claim.

    Conflict case: the user typed the primary passphrase but a different
    user is already recorded as primary. We don't claim, and the message
    explains why."""
    lines = []
    if claimed_admin:
        lines.append("Claim recorded: this user is now a pod admin (full access).")
    if claimed_primary:
        lines.append(
            "Claim recorded: this user is now the bot's primary user."
        )
    if conflict_reason:
        lines.append(f"Note: {conflict_reason}")

    if not lines:
        lines.append("(No claim was applied this turn — falling through to greet.)")

    return _HEADER + (
        "\nPhase: greet (kickoff after a passphrase claim).\n"
        "What just happened (server-side, before this prompt was built):\n"
        + "\n".join(f"  - {l}" for l in lines)
        + "\n\nGoal: acknowledge the claim briefly and warmly (one sentence — "
        "don't dwell on it), then ask for the user's first name to get the "
        "wizard going. Treat the claim as confirmation that they belong "
        "here; don't recap the security implications."
    )


def build_challenge_dismissal(*, gave_passphrase_attempt: bool) -> str:
    """systemAppend for the case where no passphrase matched — wizard ends
    politely. ``gave_passphrase_attempt`` distinguishes "user typed
    something we didn't recognize" from "user said skip/no" so the
    bot can phrase the close-out appropriately."""
    if gave_passphrase_attempt:
        framing = (
            "User typed something but it wasn't one of the configured "
            "passphrases. Tell them you didn't recognize what they typed "
            "as a passphrase and that they can run `evo wizard` again "
            "any time. Keep it warm and brief — two sentences."
        )
    else:
        framing = (
            "User declined / asked to skip. Acknowledge cheerfully, tell "
            "them they can run `evo wizard` again any time if they want "
            "to come back. One short sentence."
        )
    return _HEADER + (
        "\nPhase: challenge (closing — no claim made).\n"
        f"{framing}"
    )


def _greet_block() -> str:
    return _HEADER + (
        "\nPhase: greet (kickoff).\n"
        "What we know: nothing yet.\n"
        "Goal: greet the user warmly. Briefly explain that the next ~3 "
        "minutes will be a quick conversation to learn who they are and "
        "what they want to use this bot for, so the bot can be useful to "
        "them. Then ask for their first name (or whatever they'd like to "
        "be called). Keep it light — one or two sentences, then the "
        "question. Don't list everything we'll cover."
    )


def _about_you_block(extracted: dict[str, Any]) -> str:
    known_lines = []
    for k in ("name", "role", "environment", "current_tooling"):
        v = extracted.get(k)
        if v:
            if isinstance(v, list):
                known_lines.append(f"  - {k}: {', '.join(str(x) for x in v)}")
            else:
                known_lines.append(f"  - {k}: {v}")
    known_block = "\n".join(known_lines) if known_lines else "  (nothing yet)"

    # Identify what's still missing so the LLM can prioritize.
    missing = []
    if not extracted.get("name"):
        missing.append("first name (or what they'd like to be called)")
    if not extracted.get("role"):
        missing.append("their role / what they do")
    if not extracted.get("environment"):
        missing.append("where they work / household / project context")
    if not extracted.get("current_tooling"):
        missing.append("tools they use day-to-day that might come up")
    missing_block = (
        "\n".join(f"  - {m}" for m in missing)
        if missing else "  (we have enough — but feel free to confirm naturally)"
    )

    return _HEADER + (
        "\nPhase: about_you (light context-gathering).\n"
        "What we know:\n"
        f"{known_block}\n"
        "What we still want (don't grill — at most one or two questions "
        "per turn):\n"
        f"{missing_block}\n"
        "Goal: have a natural exchange. Ask about ONE topic per turn, "
        "weave it into a real conversation, acknowledge what they say, "
        "then move on. We exit this phase as soon as we have a name and "
        "either a role or environment — the rest can come up later. "
        "If the user asks to skip or move on, accept it and keep going."
    )


def _goals_block(extracted: dict[str, Any]) -> str:
    name = extracted.get("name") or "the user"
    role = extracted.get("role")
    environment = extracted.get("environment")

    context_lines = []
    if name and name != "the user":
        context_lines.append(f"  - name: {name}")
    if role:
        context_lines.append(f"  - role: {role}")
    if environment:
        context_lines.append(f"  - environment: {environment}")
    context_block = (
        "\n".join(context_lines) if context_lines
        else "  (we have basics from earlier; build on them naturally)"
    )

    # Surface what we already extracted so the LLM doesn't re-ask.
    have_lines = []
    for k in ("top_goals", "pain_points", "current_workflow_notes"):
        v = extracted.get(k)
        if v:
            if isinstance(v, list):
                have_lines.append(f"  - {k}: {', '.join(str(x) for x in v)}")
            else:
                have_lines.append(f"  - {k}: {v}")
    have_block = "\n".join(have_lines) if have_lines else "  (nothing yet)"

    return _HEADER + (
        "\nPhase: goals (what the user wants to use this bot for).\n"
        "Context from earlier:\n"
        f"{context_block}\n"
        "What we've gathered in this phase:\n"
        f"{have_block}\n"
        "Goal: ask what they actually want this bot to help with — "
        "concrete things, not abstract aspirations. One topic per turn. "
        "If they answer with goals, follow up briefly on what's currently "
        "frustrating (pain points) or how they handle this work today "
        "(current workflow). Don't grill — two questions max per turn.\n"
        "We exit this phase as soon as we have at least one concrete goal. "
        "Pain points and workflow notes are bonus structure; don't hold up "
        "the wizard to gather them if the user is moving on."
    )


def _platform_tour_block(extracted: dict[str, Any]) -> str:
    name = extracted.get("name") or "the user"
    has_goals = bool(extracted.get("top_goals"))
    goal_hint = (
        "Briefly tie one of their stated goals to a relevant capability "
        "if it fits naturally — but don't force it."
        if has_goals else
        "Keep it short — one paragraph, not a brochure."
    )

    return _HEADER + (
        "\nPhase: platform_tour (one-turn overview of Evolve).\n"
        "What Evolve actually offers, framed for someone new:\n"
        "  - **Better Engine**: surfaces a single, ranked recommendation at "
        "any given moment — fixing operational drift, suggesting an app "
        "to install, lining up next steps. They invoke it by typing `evo`.\n"
        "  - **Continuity Engine**: lets the bot deliver on deferred "
        "commitments (\"I'll check again in 20 minutes\", \"remind me "
        "Tuesday\") — it schedules the follow-up itself and a background "
        "runner fires it in the original conversation at the right time.\n"
        "  - **Gallery of apps**: pre-built capability bundles (calendar "
        "sync, contacts, task tracking, etc.) the user can install on "
        "their bot. The Better Engine recommends gallery installs when "
        "they fit the user's pattern of work.\n"
        f"\nGoal: give {name} a quick conversational overview of those "
        "three things. Don't lecture. Don't list bullet points verbatim — "
        "weave them into a couple of natural sentences. "
        f"{goal_hint} "
        "End by saying you'll wrap up shortly — they'll see a summary and "
        "be all set."
    )


def _wrap_block(extracted: dict[str, Any]) -> str:
    # Drop scratch fields (underscore-prefixed) and the gallery-recs
    # candidate list that's only meaningful mid-flow.
    summary_payload = {
        k: v for k, v in extracted.items()
        if v not in (None, "", []) and not k.startswith("_")
    }
    summary_json = json.dumps(summary_payload, indent=2, ensure_ascii=False)

    accepted_apps = extracted.get("apps_accepted") or []
    apps_clause = ""
    if isinstance(accepted_apps, list) and accepted_apps:
        # accepted_apps holds display names (engine resolves pkg_ids → names
        # before the wrap turn so the LLM can speak about them naturally)
        names = [str(a) for a in accepted_apps if a]
        if names:
            apps_clause = (
                f"\nThe user accepted these gallery apps during the wizard: "
                f"{', '.join(names)}. Mention that you'll line up their "
                f"installs and they'll see them in the admin dashboard's "
                f"Apps tab next, OR tell them to run `evo apps` to confirm."
            )

    return _HEADER + (
        "\nPhase: wrap (closing).\n"
        "What we have:\n"
        f"{summary_json}\n"
        f"{apps_clause}\n"
        "Goal: thank the user briefly, summarize what you learned (don't "
        "just dump JSON — say it back conversationally), mention they can "
        "review or refine their profile any time with `evo profile`, and "
        "note that from now on `evo` will route to recommendations from "
        "the Better Engine. Keep it short — three or four sentences. "
        "After this turn the wizard ends; subsequent messages from this "
        "user route normally."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gallery recommendations (slice 5b5)
# ─────────────────────────────────────────────────────────────────────────────


def _gallery_recs_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = extracted.get("name") or "the user"
    candidates = ctx.get("gallery_candidates") or []

    if not candidates:
        # No candidates — gallery empty / everything already installed.
        return _HEADER + (
            "\nPhase: gallery_recs (no recommendations).\n"
            f"Goal: tell {name} that there aren't gallery apps to recommend "
            "right now (either everything we have is already installed, or "
            "the gallery is sparse). Keep it brief — one sentence. The "
            "wizard will move to wrap on the user's next reply."
        )

    cand_lines = []
    for c in candidates:
        name_part = c.get("display_name") or c.get("pkg_id") or "(unnamed)"
        desc = c.get("description") or ""
        if len(desc) > 180:
            desc = desc[:180].rstrip() + "…"
        cand_lines.append(f"  - **{name_part}** — {desc}")
    cand_block = "\n".join(cand_lines)

    profile_lines = []
    for k in ("top_goals", "current_tooling", "pain_points"):
        v = extracted.get(k)
        if v:
            if isinstance(v, list):
                profile_lines.append(f"  - {k}: {', '.join(str(x) for x in v)}")
            else:
                profile_lines.append(f"  - {k}: {v}")
    profile_block = (
        "\n".join(profile_lines) if profile_lines
        else "  (we don't have specific profile signals to anchor on; "
             "present the candidates straight)"
    )

    return _HEADER + (
        "\nPhase: gallery_recs (presenting tailored gallery apps).\n"
        "Profile signals informing these picks:\n"
        f"{profile_block}\n"
        "Candidates (top by overlap with the user's profile):\n"
        f"{cand_block}\n"
        f"Goal: present these to {name} conversationally. Tie one or two "
        "back to what they said earlier where it fits naturally — don't "
        "force the connection. Ask which (if any) they'd like to install. "
        "They can name one (\"the calendar one\"), name several (\"calendar "
        "and CI\"), say \"all of them\", or skip with \"none\" / \"skip\" / "
        "\"maybe later\". Keep it to two short sentences plus the question. "
        "Don't list bullet points verbatim — weave the candidates into prose."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Secondary chain (slice 5b4)
# ─────────────────────────────────────────────────────────────────────────────


def _guide_summary(ctx: dict[str, Any]) -> tuple[str | None, str | None, list[str], list[str], str | None]:
    """Pull (purpose, tone, do_say, dont_say, authored_by) out of the
    bot guide carried in context. Each is None / empty when missing.

    The guide is passed in as ``ctx['bot_guide']`` — a Guide dataclass or
    None. We read the frontmatter dict off it. ``purpose`` isn't a
    canonical guide field; we synthesize it from the body's first
    paragraph if available, otherwise from the audience field, otherwise
    return None and let the prompt builder fall back."""
    guide = ctx.get("bot_guide")
    if guide is None:
        return None, None, [], [], None
    fm = getattr(guide, "frontmatter", None) or {}
    body = (getattr(guide, "body", "") or "").strip()

    audience = fm.get("audience")
    tone = fm.get("tone") if isinstance(fm.get("tone"), str) else None
    do_say_raw = fm.get("do_say") or []
    dont_say_raw = fm.get("dont_say") or []
    do_say = [str(x) for x in do_say_raw if x] if isinstance(do_say_raw, list) else []
    dont_say = [str(x) for x in dont_say_raw if x] if isinstance(dont_say_raw, list) else []
    authored_by = fm.get("authored_by") if isinstance(fm.get("authored_by"), str) else None

    # First non-heading non-empty line of the body, capped, makes a
    # decent purpose. Skip markdown headings (lines starting with '#')
    # so a stylized "# Team_bot_a" title doesn't mask the prose mission below.
    purpose: str | None = None
    if body:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            purpose = stripped[:240]
            break
    if not purpose and isinstance(audience, str) and audience:
        purpose = f"used by {audience}"

    return purpose, tone, do_say, dont_say, authored_by


def _secondary_greet_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    bot_id = ctx.get("bot_id") or "this bot"
    purpose, _tone, _do, _dont, authored_by = _guide_summary(ctx)
    primary_hint = f"set up by {authored_by}" if authored_by else "set up by the team's primary user"
    purpose_clause = (
        f"This bot is {purpose}." if purpose
        else "There's no team guide written for this bot yet — keep the intro generic."
    )

    return _HEADER + (
        f"\nPhase: secondary_greet — first turn for a non-primary user "
        f"who's opted in to a bot tour.\n"
        f"Bot identity: {bot_id} ({primary_hint}).\n"
        f"Bot framing from the team guide: {purpose_clause}\n"
        f"Goal: greet the visitor warmly, briefly say what this bot is for "
        f"(weave in the framing above naturally — don't quote it), and ask "
        f"their first name. Two sentences max, then the question. "
        f"Don't list rules, don't recap that they're not the primary."
    )


def _secondary_about_you_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = extracted.get("name")
    purpose, _tone, _do, _dont, _authored_by = _guide_summary(ctx)

    name_line = f"  - name: {name}" if name else "  (no name yet — re-ask if needed)"
    purpose_clause = (
        f" The bot is {purpose}." if purpose else ""
    )

    return _HEADER + (
        "\nPhase: secondary_about_you — light context-gathering.\n"
        "What we know:\n"
        f"{name_line}\n"
        "What we want next: a quick sense of the visitor's role relative to "
        f"the bot's primary user / team.{purpose_clause}\n"
        "Goal: if we don't have a name yet, ask for it once. Otherwise ask "
        "in one short sentence what their role is on the team / household / "
        "project — nothing exhaustive, just enough to be useful when "
        "deciding how to talk to them later. Don't grill."
    )


def _how_to_use_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = extracted.get("name") or "the visitor"
    purpose, tone, do_say, dont_say, _authored_by = _guide_summary(ctx)

    do_block = (
        "\n".join(f"  - {d}" for d in do_say) if do_say
        else "  (no specific 'do' guidance from the bot guide)"
    )
    dont_block = (
        "\n".join(f"  - {d}" for d in dont_say) if dont_say
        else "  (no specific 'don't' guidance)"
    )
    tone_line = f"Preferred tone: {tone}\n" if tone else ""
    purpose_line = f"Bot mission (from guide): {purpose}\n" if purpose else ""

    if not do_say and not dont_say and not purpose:
        # No guide at all — render a generic "how to use this bot" message.
        return _HEADER + (
            "\nPhase: how_to_use — one-turn rendering.\n"
            f"There's no team-authored guide for this bot yet. Tell {name} "
            "in one or two friendly sentences that they can just chat with "
            "the bot naturally — ask questions, request help, send tasks — "
            "and that any specifics about how this team uses the bot will "
            "show up here once the primary writes a guide."
        )

    return _HEADER + (
        "\nPhase: how_to_use — one-turn rendering of the team's authored "
        "guidance.\n"
        f"{purpose_line}"
        f"{tone_line}"
        "Things to lean toward (from the guide):\n"
        f"{do_block}\n"
        "Things to steer clear of (from the guide):\n"
        f"{dont_block}\n"
        f"Goal: tell {name} how this bot is meant to be used. Weave the "
        "guide's bullets into 2–3 conversational sentences (don't dump "
        "them as bullets). Match the preferred tone if one was set. End "
        "with a friendly invitation to ask the bot something real."
    )


def _secondary_wrap_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = extracted.get("name") or "you"
    return _HEADER + (
        "\nPhase: secondary_wrap (closing).\n"
        f"Goal: tell {name} they're all set — the wizard is wrapping up. "
        "Keep it warm and short (one or two sentences). Invite them to "
        "send their next message naturally; the bot will respond as "
        "itself from here on. After this turn the wizard ends and "
        "subsequent messages route normally."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Guide-draft chain (slice 5b6)
# ─────────────────────────────────────────────────────────────────────────────


def _guide_gather_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    bot_id = ctx.get("bot_id") or "this bot"
    is_editing = bool(ctx.get("editing_existing"))

    have_lines = []
    for k in ("audience", "tone", "do_say", "dont_say", "body_outline"):
        v = extracted.get(k)
        if v:
            if isinstance(v, list):
                have_lines.append(f"  - {k}: {', '.join(str(x) for x in v)}")
            else:
                # Truncate long body for the prompt
                disp = str(v)
                if len(disp) > 200:
                    disp = disp[:200] + "…"
                have_lines.append(f"  - {k}: {disp}")
    have_block = "\n".join(have_lines) if have_lines else "  (nothing yet)"

    missing = []
    if not extracted.get("audience"):
        missing.append("who'll use this bot (audience)")
    if not extracted.get("tone"):
        missing.append("what tone the bot should use with them")
    if not extracted.get("do_say"):
        missing.append("things the bot should lean toward (do)")
    if not extracted.get("dont_say"):
        missing.append("things to avoid (don't)")
    if not extracted.get("body_outline"):
        missing.append("a one-sentence summary of what the bot is for")
    missing_block = (
        "\n".join(f"  - {m}" for m in missing)
        if missing else "  (we have enough; the engine should advance to "
                        "confirmation soon — keep the conversation natural)"
    )

    intro = (
        f"You're helping the user EDIT the existing bot guide for {bot_id}. "
        f"Their previous answers are pre-loaded below; ask if they want "
        f"to update any of them, or add anything missing."
        if is_editing else
        f"You're helping the user AUTHOR a bot guide for {bot_id}. The "
        f"goal is a short, primary-authored note that says what this bot "
        f"is for and how the team should use it. The note will be read "
        f"by the bot itself at every session start AND shown to any "
        f"secondary users running their wizard."
    )

    return _HEADER + (
        "\nPhase: guide_gather (authoring the bot guide).\n"
        f"{intro}\n"
        "What we have so far:\n"
        f"{have_block}\n"
        "What we still want:\n"
        f"{missing_block}\n"
        "Goal: ask about ONE missing topic per turn, conversationally. "
        "Don't grill. Acknowledge what they say, then move on. We exit "
        "this phase as soon as we have the audience plus either a tone "
        "or a one-sentence summary — the rest is bonus structure. The "
        "user can also say 'show me' or 'preview' if they want to see "
        "the draft so far; pass that through and the engine will route "
        "to the confirmation step on the next turn."
    )


def _guide_confirm_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Verbatim — render the proposed guide preview, gate on user reply.
    Engine pattern-matches save / edit / cancel deterministically."""
    bot_id = ctx.get("bot_id") or "this bot"
    rendered = _render_guide_preview(bot_id, extracted)
    return (
        f"**Proposed bot guide for `{bot_id}`** — review below, then say "
        "`save`, `edit`, or `cancel`.\n\n"
        "```markdown\n"
        f"{rendered}\n"
        "```\n\n"
        "Reply `save` (or `yes`) to write this to disk.\n"
        "Reply `edit` to keep refining.\n"
        "Reply `cancel` to drop without saving."
    )


def _render_guide_preview(bot_id: str, extracted: dict[str, Any]) -> str:
    """Render the working draft as it would appear on disk (frontmatter
    + body). The CONFIRM phase shows this to the user before save; the
    engine writes essentially the same thing on save (write_guide
    re-stamps timestamps and authored_by)."""
    audience = extracted.get("audience") or ""
    tone = extracted.get("tone") or ""
    do_say = extracted.get("do_say") or []
    dont_say = extracted.get("dont_say") or []
    body = (extracted.get("body_outline") or "").strip()

    lines = ["---"]
    lines.append(f"bot_id: {bot_id}")
    if audience:
        lines.append(f"audience: {audience!r}" if " " in audience else f"audience: {audience}")
    if tone:
        lines.append(f"tone: {tone!r}" if " " in tone else f"tone: {tone}")
    if do_say:
        lines.append("do_say:")
        for item in do_say:
            lines.append(f"  - {item}")
    if dont_say:
        lines.append("dont_say:")
        for item in dont_say:
            lines.append(f"  - {item}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {bot_id.title()} — Team Guide")
    lines.append("")
    if body:
        lines.append(body)
    else:
        lines.append("(no body yet — the user can add one before saving)")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Conversational approval (slice 5b8) — pitch a Better Engine rec and
# field the user's reply
# ─────────────────────────────────────────────────────────────────────────────


_REC_PENDING_VARIANTS = ("pitch", "clarify", "context", "all_caught_up", "push")


def _rec_pending_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Render the systemAppend for the rec_pending phase. The engine
    selects a variant via ``ctx['variant']`` so the bot's prompt matches
    the conversational state:

      * ``"pitch"`` (default) — first-time pitch of a rec
      * ``"clarify"`` — bot asked but the user's reply was ambiguous;
        re-ask with the parser's best-guess phrased as a question
      * ``"context"`` — user asked for more info; show extended context
        + re-show the action options
      * ``"all_caught_up"`` — terminal: the queue is empty; finalize
    """
    variant = (ctx.get("variant") or "pitch").lower()
    if variant not in _REC_PENDING_VARIANTS:
        variant = "pitch"

    if variant == "all_caught_up":
        return _rec_pending_all_caught_up_block(ctx)

    rec = extracted.get("_pending_rec") or {}
    bot_id = ctx.get("bot_id") or rec.get("scope_id") or "your bot"
    voice = ctx.get("voice") if isinstance(ctx.get("voice"), dict) else None

    if variant == "context":
        return _rec_pending_context_block(rec, bot_id)

    if variant == "clarify":
        return _rec_pending_clarify_block(rec, bot_id, ctx)

    if variant == "push":
        return _rec_pending_push_block(rec, bot_id, voice=voice)

    if variant == "inprocess_followup":
        return _rec_pending_inprocess_followup_block(rec, bot_id, voice=voice)

    return _rec_pending_pitch_block(rec, bot_id, voice=voice)


def build_rec_pending_block(
    *,
    variant: str = "pitch",
    rec: dict[str, Any] | None = None,
    bot_id: str = "",
    best_guess_action: str | None = None,
) -> str:
    """Public helper for callers that want the rec_pending block without
    going through the phase-name switch. Used by the dispatcher to
    render the first-turn pitch when starting a fresh session."""
    extracted = {"_pending_rec": rec or {}}
    ctx: dict[str, Any] = {"variant": variant, "bot_id": bot_id}
    if best_guess_action:
        ctx["best_guess_action"] = best_guess_action
    return _rec_pending_block(extracted, ctx)


def _rec_pending_summary(rec: dict[str, Any]) -> tuple[str, str, str]:
    """Pull (title, detail, context) from a Recommendation dict, preferring
    the member_bot variants when set. Returns empty strings when fields
    aren't present so the prompt can fall back gracefully."""
    title = (
        rec.get("member_bot_title")
        or rec.get("title")
        or "(untitled recommendation)"
    )
    detail = rec.get("member_bot_detail") or rec.get("detail") or ""
    context = rec.get("context") or ""
    return str(title).strip(), str(detail).strip(), str(context).strip()


# Per-action-kind pitch semantics. Mirrors the admin UI's _actSemantics map
# in index.html so the chat surface frames "yes" the same way the click
# surface does. ``verb`` is the action verb the user might naturally
# echo back ("apply this", "take this on", "build it"). ``yes_means`` is
# what actually happens on the proposal lifecycle when the user accepts —
# critical so the user isn't surprised. ``follow_up`` is whether more
# operator action is required after accept (None = autonomous, "manual"
# = user must come back and Mark complete, "external" = forge owns the
# remainder).
_KIND_PITCH_SEMANTICS: dict[str, dict[str, str | None]] = {
    "ConfigPatch": {
        "verb": "apply this patch",
        "yes_means": (
            "the change writes to the bot's config immediately and the "
            "verify daemon will check whether the predicted improvement "
            "lands"
        ),
        "follow_up": None,
    },
    "TierAdjustment": {
        "verb": "adjust the tier",
        "yes_means": (
            "the routing tier rewrites immediately; the bot uses the new "
            "tier on its next session"
        ),
        "follow_up": None,
    },
    "ManifestUpdate": {
        "verb": "update the manifest",
        "yes_means": "the named app's manifest is patched in place",
        "follow_up": None,
    },
    "DeprecateApp": {
        "verb": "deprecate this app",
        "yes_means": (
            "the app is marked deprecated; the bot stops surfacing it "
            "but data is preserved"
        ),
        "follow_up": None,
    },
    "SoulEdit": {
        "verb": "apply the personality edit",
        "yes_means": (
            "the bot's SOUL.md or AGENTS.md gets the edit; reversible "
            "via the revert plan"
        ),
        "follow_up": None,
    },
    "AgentsAppend": {
        "verb": "append this note",
        "yes_means": (
            "the note is added to AGENTS.md — additive only, doesn't "
            "replace existing sections"
        ),
        "follow_up": None,
    },
    "ThrottleGenerator": {
        "verb": "throttle the generator",
        "yes_means": (
            "the named generator's capacity gets reduced — auto-reverts "
            "after the configured window if set"
        ),
        "follow_up": None,
    },
    "PauseGenerator": {
        "verb": "pause the generator",
        "yes_means": "the named generator stops emitting until resumed",
        "follow_up": None,
    },
    "InstallApp": {
        "verb": "install this app",
        "yes_means": "the named gallery app is installed for the bot",
        "follow_up": None,
    },
    "Investigation": {
        "verb": "take this on",
        "yes_means": (
            "the proposal moves to your In Process queue. Do whatever "
            "the context describes, then come back and tell me you've "
            "handled it (Mark complete) or that it turned out to be a "
            "non-issue (Dismiss)"
        ),
        "follow_up": "manual",
    },
    "WorkflowInstruction": {
        "verb": "take this on",
        "yes_means": (
            "I'll write the markdown instruction file into the bot's "
            "workspace, and the proposal moves to your In Process queue. "
            "Walk through the file, then come back and Mark complete"
        ),
        "follow_up": "manual",
    },
    "BuildApp": {
        "verb": "build this app",
        "yes_means": (
            "the manifest goes to the forge; over the next few minutes "
            "to hours the forge designs, builds, and tests the app. "
            "I'll let you know when it's done — or when something went "
            "wrong"
        ),
        "follow_up": "external",
    },
    "VetoAnnotation": {
        "verb": "acknowledge",
        "yes_means": (
            "this just records that you saw the guardian's concern; "
            "nothing else changes"
        ),
        "follow_up": None,
    },
}

# Default semantics for recommendations that don't carry an L1-L6
# ``action_kind`` (non-proposal-derived: onboarding / scoreboard /
# compliance / whimsy). Stays generic since those flows don't have a
# proposal applier to describe.
_DEFAULT_PITCH_SEMANTICS: dict[str, str | None] = {
    "verb": "go ahead",
    "yes_means": "the engine records your acceptance and follows up",
    "follow_up": None,
}


def _kind_pitch_semantics(rec: dict[str, Any]) -> dict[str, str | None]:
    """Return the pitch semantics for this rec's action kind.

    Falls back to the generic semantics when the rec doesn't carry a
    recognized ``action_kind`` — covers non-proposal recs and any future
    action kind not yet in the semantics map.
    """
    kind = rec.get("action_kind")
    if isinstance(kind, str) and kind in _KIND_PITCH_SEMANTICS:
        return _KIND_PITCH_SEMANTICS[kind]
    return _DEFAULT_PITCH_SEMANTICS


def _format_lineage_clause(rec: dict[str, Any]) -> str:
    """Render the lineage summary as a one-line user-facing footnote
    (e.g. "Prior history: rejected twice, most recently 12 days ago.")
    appended to the rec_pending pitch. Returns an empty string when
    there's no history.

    The summary structure (set by ``engine._enrich_with_lineage``):
      {
        "total": int,
        "by_status": {status: count, ...},
        "latest": {status: ISO timestamp, ...},
      }

    Previously this rendered as an LLM-directive cue ("acknowledge this
    naturally in the pitch"), back when rec_pending was an agenda phase
    and the LLM produced the pitch in its own voice. After 2026-05-17
    rec_pending is verbatim/direct-send; this output now lands directly
    in the user-visible message.
    """
    summary = rec.get("_lineage_summary")
    if not isinstance(summary, dict):
        return ""
    by_status = summary.get("by_status") or {}
    if not isinstance(by_status, dict) or not by_status:
        return ""

    fragments: list[str] = []
    latest = summary.get("latest") or {}
    for status, count in by_status.items():
        if not isinstance(count, int) or count <= 0:
            continue
        when = latest.get(status) if isinstance(latest, dict) else None
        when_clause = f", most recently {_human_ago(when)}" if when else ""
        verb = _LINEAGE_VERB.get(str(status), str(status).replace("_", " "))
        if count == 1:
            fragments.append(f"{verb} once{when_clause}")
        elif count == 2:
            fragments.append(f"{verb} twice{when_clause}")
        else:
            fragments.append(f"{verb} {count} times{when_clause}")
    if not fragments:
        return ""

    return f"\n\nPrior history: " + "; ".join(fragments) + "."


# Past-tense verb fragments per terminal status. Keep these short — the
# pitch composes them into a sentence; we don't need full clauses.
_LINEAGE_VERB: dict[str, str] = {
    "succeeded": "approved and verified",
    "failed_reverted": "approved but reverted",
    "failed_flagged": "approved but verify failed",
    "failed_revert_failed": "approved but failed to revert",
    "rejected": "rejected",
    "dismissed": "dismissed",
    "superseded": "superseded by another proposal",
    "resolved_externally": "resolved on its own",
}


def _human_ago(iso_ts: str | None) -> str:
    """Render an ISO timestamp as a human-readable relative time
    ('12 days ago', '3 weeks ago'). Falls back to the raw string if
    parsing fails — the LLM can still incorporate it sensibly."""
    if not iso_ts:
        return "earlier"
    try:
        from datetime import datetime, timezone

        cleaned = iso_ts.rstrip("Z")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        seconds = delta.total_seconds()
        if seconds < 3600:
            return "less than an hour ago"
        if seconds < 86400:
            hours = int(seconds // 3600)
            return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
        if seconds < 86400 * 14:
            days = int(seconds // 86400)
            return f"{days} day ago" if days == 1 else f"{days} days ago"
        if seconds < 86400 * 60:
            weeks = int(seconds // (86400 * 7))
            return f"{weeks} week ago" if weeks == 1 else f"{weeks} weeks ago"
        months = int(seconds // (86400 * 30))
        return f"{months} month ago" if months == 1 else f"{months} months ago"
    except Exception:
        return iso_ts


def _rec_pending_pitch_block(
    rec: dict[str, Any],
    bot_id: str,
    voice: dict[str, str] | None = None,
) -> str:
    """User-facing pitch text for a Better Engine recommendation.

    Renders deterministically (template, not LLM directive). The
    ``voice`` param is accepted for backwards-compat but unused — voice
    cues only applied when the LLM produced the pitch in its own voice;
    under the verbatim/direct-send path (post 2026-05-17) the message
    is sent as-is.
    """
    del voice  # unused under verbatim render mode
    title, detail, context = _rec_pending_summary(rec)
    semantics = _kind_pitch_semantics(rec)
    yes_means = semantics.get("yes_means") or ""
    follow_up = semantics.get("follow_up")

    urgency = ""
    tags = rec.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.startswith("urgency:"):
                urgency = t[8:]
                break

    lines: list[str] = []
    header = f"Suggestion for {bot_id}"
    if urgency in ("critical", "high"):
        header += f" ({urgency} urgency)"
    lines.append(f"{header}:")
    lines.append("")
    lines.append(title)
    if detail:
        lines.append("")
        lines.append(detail)
    if context:
        lines.append("")
        lines.append(f"Context: {context}")

    if yes_means:
        lines.append("")
        lines.append(f'What "yes" does: {yes_means}.')
    if follow_up == "manual":
        lines.append(
            "After you say yes, this lands in your In Process queue — "
            "do the work, then come back and tell me you've handled it."
        )
    elif follow_up == "external":
        lines.append(
            "After you say yes, the forge takes over and reports back "
            "when it's done."
        )

    lineage = _format_lineage_clause(rec)
    if lineage:
        lines.append(lineage)

    lines.append("")
    lines.append("Reply: yes / no / snooze / next / why?")
    return "\n".join(lines)


def _rec_pending_inprocess_followup_block(
    rec: dict[str, Any],
    bot_id: str,
    voice: dict[str, str] | None = None,
) -> str:
    """User-facing follow-up for a manual-completion proposal that
    the user previously took on.

    Verbatim/direct-send under the 2026-05-17 conversion; ``voice`` is
    accepted for backwards-compat but unused.
    """
    del voice
    title, detail, context = _rec_pending_summary(rec)
    accepted_at = rec.get("_accepted_at_human") or "earlier"

    lines: list[str] = []
    lines.append(f"Checking in on what you took on {accepted_at}:")
    lines.append("")
    lines.append(title)
    if detail:
        lines.append("")
        lines.append(detail)
    if context:
        lines.append("")
        lines.append(f"Original context: {context}")
    lines.append("")
    lines.append(
        "How did it go? Reply: done / drop it / still working / next / "
        "remind me what this was?"
    )
    return "\n".join(lines)


def _format_voice_clause(voice: dict[str, str] | None) -> str:
    """Render the voice cues into a short instruction block the LLM
    consumes alongside the rest of the pitch prompt. Returns an empty
    string when nothing is on file so the prompt template doesn't grow
    a dangling 'Voice:' header on bots without a guide / SOUL / profile."""
    if not voice:
        return ""
    parts: list[str] = []
    if voice.get("tone"):
        parts.append(f"Tone: {voice['tone']}")
    if voice.get("do_say"):
        parts.append(f"Lean toward: {voice['do_say']}")
    if voice.get("dont_say"):
        parts.append(f"Avoid: {voice['dont_say']}")
    if voice.get("profile_communication_preferences"):
        # Cap so a wordy profile section doesn't drown the rest of the
        # prompt; the LLM only needs the headline cue.
        prefs = voice["profile_communication_preferences"]
        if len(prefs) > 240:
            prefs = prefs[:240].rstrip() + "…"
        parts.append(f"User communication preferences: {prefs}")
    if not parts:
        return ""
    return "\n\nVoice cues from this bot's setup:\n  - " + "\n  - ".join(parts)


def _rec_pending_clarify_block(
    rec: dict[str, Any], bot_id: str, ctx: dict[str, Any]
) -> str:
    """User-facing one-line clarification when the user's last reply
    didn't parse cleanly. Verbatim/direct-send under the 2026-05-17
    conversion."""
    del bot_id  # not used; title carries the rec reference
    title, _detail, _context = _rec_pending_summary(rec)
    best = (ctx.get("best_guess_action") or "").strip().lower()

    if best == "accept":
        guess = "go ahead with it"
    elif best == "reject":
        guess = "skip it"
    elif best == "snooze":
        guess = "come back to it later"
    elif best == "next":
        guess = "see a different one"
    elif best == "context":
        guess = "hear more about it first"
    else:
        guess = "go ahead, skip it, or come back later"

    return (
        f'Didn\'t quite catch that on "{title}" — did you want to {guess}? '
        "Reply: yes / no / snooze / next / why?"
    )


def _rec_pending_context_block(rec: dict[str, Any], bot_id: str) -> str:
    """User-facing context expansion when the user replied "why?" or
    similar. Verbatim/direct-send."""
    del bot_id
    title, detail, context = _rec_pending_summary(rec)

    lines: list[str] = []
    lines.append(f"More on {title}:")
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    if context:
        lines.append(context)
    else:
        lines.append(
            "No additional context on file beyond what was in the original "
            "pitch."
        )
    lines.append("")
    lines.append("Reply: yes / no / snooze / next?")
    return "\n".join(lines)


def _rec_pending_all_caught_up_block(ctx: dict[str, Any]) -> str:
    """User-facing terminal message when the rec queue is empty.
    Verbatim/direct-send."""
    bot_id = ctx.get("bot_id") or "your bot"
    return (
        f"You're all caught up on {bot_id}. "
        "Type `evo` any time to check again."
    )


def _rec_pending_push_block(
    rec: dict[str, Any],
    bot_id: str,
    voice: dict[str, str] | None = None,
) -> str:
    """User-facing nudge for an urgent recommendation surfaced via the
    push-preamble path. Verbatim/direct-send; ``voice`` is accepted for
    backwards-compat but unused."""
    del voice
    title, detail, _context = _rec_pending_summary(rec)
    accept_label = rec.get("accept_label") or "go ahead"

    lines: list[str] = []
    lines.append(f"Heads up — urgent suggestion for {bot_id}:")
    lines.append("")
    lines.append(title)
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    lines.append(f'What "yes" does: {accept_label}.')
    lines.append("")
    lines.append("Reply: yes / remind me later?")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Forge-via-messaging chain (slice 5b7b)
# ─────────────────────────────────────────────────────────────────────────────


def _forge_intro_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = extracted.get("name") or "the user"
    goals = extracted.get("top_goals") or []
    goal_phrase = ", ".join(str(g) for g in goals[:2]) if goals else "your goals"

    return _HEADER + (
        "\nPhase: forge_intro (one-turn opt-in for custom-app build).\n"
        "Goal: tell {name} that based on {goals} they mentioned earlier, "
        "the gallery didn't have a great fit, and you can build a custom "
        "app for them. Explain how it works in one sentence: a few "
        "back-and-forth turns to align on what it does, then you'll build "
        "it autonomously and DM them when it's ready (~5 min).\n"
        "Ask whether they want to try. Two short sentences plus the "
        "question; warm but not breathless. They reply yes / sure / "
        "let's try → opt in. They reply no / skip / not now → opt out, "
        "wizard wraps. Anything else (ambiguous) → re-ask politely.\n"
    ).replace("{name}", name).replace("{goals}", goal_phrase)


def _forge_design_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    user_name = extracted.get("name") or "the user"
    goals = extracted.get("top_goals") or []
    goals_line = (
        f"  - top_goals from earlier: {', '.join(str(g) for g in goals)}"
        if goals else "  (no goals captured earlier)"
    )

    have_lines = []
    for k in ("forge_app_name", "forge_description", "forge_capabilities",
              "forge_example_behaviors", "forge_conversational_summary"):
        v = extracted.get(k)
        if v:
            if isinstance(v, list):
                have_lines.append(f"  - {k}: {', '.join(str(x) for x in v)}")
            else:
                disp = str(v)
                if len(disp) > 240:
                    disp = disp[:240] + "…"
                have_lines.append(f"  - {k}: {disp}")
    have_block = "\n".join(have_lines) if have_lines else "  (nothing yet — let's start)"

    missing = []
    if not extracted.get("forge_app_name"):
        missing.append("a short name for the app (lowercase, hyphens fine)")
    if not extracted.get("forge_description"):
        missing.append("one or two sentences saying what it should do")
    if not extracted.get("forge_capabilities"):
        missing.append("which integrations / tools it'll need (slack, github, calendar, etc.)")
    if not extracted.get("forge_example_behaviors"):
        missing.append("a concrete behavior or two — 'when X happens, do Y'")
    missing_block = (
        "\n".join(f"  - {m}" for m in missing)
        if missing else "  (we have the basics; conversational summary should "
                        "still capture any nuance the user is adding)"
    )

    return _HEADER + (
        "\nPhase: forge_design (multi-turn manifest authoring loop).\n"
        f"User: {user_name}\n"
        f"{goals_line}\n"
        "What we've gathered for the proposed app:\n"
        f"{have_block}\n"
        "What we still want — ask about ONE thing per turn, conversationally:\n"
        f"{missing_block}\n"
        "Goal: make this feel like a design conversation between two "
        "thoughtful collaborators, not an interrogation. Acknowledge what "
        "they say, paraphrase it back, then ask the next thing. Push back "
        "when something seems off (e.g., they ask for a calendar feature "
        "but didn't mention having a calendar integration — surface that). "
        "Maintain a running conversational summary — when the user adds or "
        "modifies something, restate the whole picture in one paragraph "
        "so they can confirm you've got it.\n"
        "We exit this phase as soon as we have a name AND a description; "
        "the rest is bonus structure but worth gathering if it comes up "
        "naturally. The user can also say 'show me' / 'preview' / 'see it' "
        "to jump to the build-or-edit confirmation step on the next turn."
    )


def _forge_confirm_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    bot_id = ctx.get("bot_id") or "this bot"
    name = extracted.get("forge_app_name") or "(unnamed)"
    description = (extracted.get("forge_description") or "").strip()
    summary = (extracted.get("forge_conversational_summary") or "").strip()
    caps = extracted.get("forge_capabilities") or []
    behaviors = extracted.get("forge_example_behaviors") or []

    spec_lines = [f"**Proposed app: `{name}`** for `{bot_id}`"]
    if description:
        spec_lines.append("")
        spec_lines.append(f"**What it does:** {description}")
    if isinstance(caps, list) and caps:
        spec_lines.append("")
        spec_lines.append(f"**Uses:** {', '.join(str(c) for c in caps)}")
    if isinstance(behaviors, list) and behaviors:
        spec_lines.append("")
        spec_lines.append("**Example behaviors:**")
        for b in behaviors:
            spec_lines.append(f"  - {b}")
    if summary:
        spec_lines.append("")
        spec_lines.append(f"**Summary (will be saved verbatim with the manifest):**\n{summary}")
    spec_block = "\n".join(spec_lines)

    return (
        f"{spec_block}\n\n"
        "Reply `build` to queue this — you'll get a DM in ~5 minutes when "
        "it's ready.\n"
        "Reply `edit` to keep refining the design.\n"
        "Reply `cancel` to drop it."
    )


def render_forge_kickoff_wrap(app_name: str, eta_minutes: int = 5) -> str:
    """Wrap-message block for the post-build kickoff turn — separate
    from _wrap_block since the user didn't necessarily complete normal
    profile wrap (they detoured into forge). Speaks to the queued
    build, sets ETA, mentions where the notification will land."""
    return _HEADER + (
        f"\nPhase: forge wrap (after build kickoff).\n"
        "Goal: tell the user the build is queued — keep it warm and brief. "
        f"Mention the app name (`{app_name}`) and roughly when to expect "
        f"the DM (~{eta_minutes} min). Tell them they can run "
        f"`evo forge status` any time to check, or just go about their day "
        f"and you'll DM them.\n"
        "After this turn the wizard ends; subsequent messages from this "
        "user route normally."
    )


def render_forge_build_failed_wrap(app_name: str, reason: str) -> str:
    """Used when the wizard couldn't even kick off the build (manifest
    construction error, etc.) — different from a forge-engine failure
    that the notification queue surfaces later. This is a synchronous
    failure during the wizard turn."""
    return _HEADER + (
        "\nPhase: forge wrap (couldn't queue the build).\n"
        f"Goal: tell the user we couldn't queue the build for `{app_name}` "
        "right now. Briefly mention the reason (one sentence — be honest "
        f"without overwhelming): {reason}\n"
        "Reassure them their wizard progress is saved and they can try "
        "again with `evo forge` (when v2 ships) or by re-running `evo "
        "wizard`. Two warm sentences."
    )


# ─────────────────────────────────────────────────────────────────────────────
# App-create chain (Wave 3) — `evo app create`
# ─────────────────────────────────────────────────────────────────────────────


# All app_create renderers below are verbatim mode (declared in phases.py).
# Each returns the clean user-facing body; engine wraps for LLM-fallback.


def render_app_create_describe_intro() -> str:
    return (
        "**Create a new app for this bot**\n\n"
        f"{_evo_delim.EVO_HEADS_UP_NOTE}\n\n"
        "Describe the app you want in one or two sentences. A few examples "
        "to anchor on:\n"
        "  • \"track my running pace and weekly mileage\"\n"
        "  • \"send me a daily morning briefing from my email\"\n"
        "  • \"log my home electrical repairs\"\n\n"
        "I'll draft a full spec from your description and show it to you "
        "next turn — you can then `approve`, `iterate <feedback>`, or "
        "`cancel`."
    )


def render_app_create_describe_too_short() -> str:
    return (
        "I need a bit more to go on. Describe the app in a sentence or "
        "two — what should it do, what data does it track, what would "
        "make it useful?\n\n"
        "Or reply `cancel` to abort."
    )


def render_app_create_draft_failed() -> str:
    return (
        "Hit a hiccup while drafting that spec — probably an LLM key or "
        "connectivity issue. Try describing the app again, or reply "
        "`cancel` to abort (you can always start fresh with `evo app "
        "create`). If it keeps failing, check with your pod admin."
    )


def _format_draft_summary(draft: dict) -> str:
    """One-line-per-fact rendering of a SpecDraft for the LLM to relay."""
    name = str(draft.get("display_name") or "Unnamed App")
    desc = str(draft.get("description") or "").strip()
    tags = list(draft.get("application_tags") or [])
    reqs = dict(draft.get("requirements") or {})
    integrations = reqs.get("integrations") or []
    deps = list(draft.get("app_dependencies") or [])
    has_test = bool(str(draft.get("test_command") or "").strip())
    exempt = str(draft.get("test_exemption_reason") or "").strip()

    lines = [f"• Name: {name}"]
    if desc:
        lines.append(f"• What it does: {desc}")
    if tags:
        lines.append(f"• Tags: {', '.join(str(t) for t in tags)}")
    if integrations:
        lines.append(f"• Integrations: {', '.join(str(i) for i in integrations)}")
    if deps:
        dep_names = [
            str(d.get("display_name") or d.get("pkg_id") or "")
            for d in deps
            if isinstance(d, dict)
        ]
        if dep_names:
            lines.append(f"• Requires apps: {', '.join(dep_names)}")
    if has_test:
        lines.append("• Tests: bundled")
    elif exempt:
        lines.append(f"• Tests: exempt — {exempt}")
    return "\n".join(lines)


def render_app_create_review(
    draft: dict, *, iterated: bool = False, ambiguous_reply: bool = False,
) -> str:
    summary = _format_draft_summary(draft)
    header = "**Drafted spec**"
    intro = ""
    if iterated:
        header = "**Revised spec**"
        intro = "Incorporated your feedback — here's the updated draft:\n\n"
    elif ambiguous_reply:
        intro = (
            "I didn't catch that as `approve`, `iterate <feedback>`, or "
            "`cancel`. Here's the spec again:\n\n"
        )
    return (
        f"{header}\n\n"
        f"{intro}"
        f"{summary}\n\n"
        "Reply `approve` to queue this for install.\n"
        "Reply `iterate <feedback>` to revise (e.g. `iterate add weekly summary`).\n"
        "Reply `cancel` to drop it."
    )


def render_app_create_iterate_empty(draft: dict) -> str:
    summary = _format_draft_summary(draft)
    return (
        "What would you like changed? Tell me what to add, remove, or "
        "rename — e.g. `iterate add a weekly summary`, `iterate drop the "
        "test exemption`, `iterate rename to morning-roundup`.\n\n"
        f"Current draft:\n{summary}\n\n"
        "Or reply `approve` to ship as-is, `cancel` to drop it."
    )


def render_app_create_iterate_cap_hit(draft: dict, cap: int) -> str:
    summary = _format_draft_summary(draft)
    return (
        f"**Iteration cap hit** — we've revised this app {cap} times in "
        "this session, which is the limit (keeps costs in check).\n\n"
        f"Current draft:\n{summary}\n\n"
        "Reply `approve` to ship this draft.\n"
        "Reply `cancel` to drop it (you can start fresh with `evo app create`)."
    )


def render_app_create_cancelled(app_name: str) -> str:
    """Terminal — user cancelled."""
    return _HEADER + (
        "\nPhase: app_create wrap (cancelled).\n"
        f"Goal: acknowledge that you dropped `{app_name}` — no manifest "
        "written, no forge job. Two short sentences max. Mention `evo "
        "gallery` for existing apps and `evo app create` if they want to "
        "try a different idea."
    )


def render_app_create_queued(*, display_name: str, job_id: str) -> str:
    """Terminal — forge job queued."""
    return _HEADER + (
        "\nPhase: app_create wrap (queued).\n"
        f"Goal: tell the user `{display_name}` is queued for install and "
        "should appear in `evo apps` within a few minutes once forge "
        f"finishes. Mention the short job id ({job_id[:16]}) once so they "
        "can reference it if they need to check status. Two warm "
        "sentences max."
    )


def render_app_create_failed(draft: dict, reason: str) -> str:
    """Terminal — couldn't queue the install (manifest write / job create)."""
    name = str(draft.get("display_name") or "the app")
    return _HEADER + (
        "\nPhase: app_create wrap (couldn't queue).\n"
        f"Goal: tell the user we drafted `{name}` but couldn't queue it "
        f"for install. Brief one-sentence reason: {reason}\n"
        "Tell them their progress is saved and to try `evo app create` "
        "again, or ask their pod admin if it persists. Two sentences."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Google setup chain — `evo setup-google`
#
# Per-bot under PR #1123 / PR 2: each bot has its own OAuth Application,
# typically tied to a different Google Cloud project / org / billing
# path (Team_bot_a = Example Corp' org, Team_bot_c = Star Springs Ranch's org,
# Admin_bot = personal). The wizard runs from the bot being set up — its
# prompts reference that specific bot. Two bots can opt-in to share by
# copying each other's client_secret_ref.
#
# These prompts drive a chat-mediated walkthrough of the manual Google
# Cloud Console work (Evolve can't programmatically register a Cloud
# project on the operator's behalf), but every other surface — collecting
# URLs, validating client IDs, persisting credentials, surfacing the
# redirect URI to whitelist — is driven from chat.
# ─────────────────────────────────────────────────────────────────────────────


# All google_setup renderers are verbatim-mode (declared in phases.py).
# Each returns the clean user-facing body; the engine's
# ``TurnResult.__post_init__`` wraps it for LLM-fallback when needed.


def render_google_setup_intro(bot_id: str = "this bot") -> str:
    return (
        f"**Setting up Google Workspace access for `{bot_id}`**\n\n"
        f"{_evo_delim.EVO_HEADS_UP_NOTE}\n\n"
        f"We'll wire `{bot_id}` up to its own Google OAuth client — typically "
        "tied to whichever Google account / organization this bot belongs "
        "to (e.g. a personal Google account, a company's Google Workspace "
        "org, a household's shared account). Each bot gets its own; they "
        "don't have to share.\n\n"
        "You'll need:\n"
        "  • Access to https://console.cloud.google.com under the right "
        "Google account for this bot (5 min of work)\n"
        "  • Your admin server's externally-reachable URL — Google needs "
        "to redirect back to it after consent (one URL for the whole "
        "machine; you've probably already set this if other bots have run "
        "this wizard)\n\n"
        f"After this, `evo connect google` from `{bot_id}` gives the operator "
        "a one-tap consent link. Reply `ready` to begin, or `cancel` to "
        "abort."
    )


def render_google_setup_admin_url() -> str:
    return (
        "**Step 1 of 4 — admin base URL**\n\n"
        "What's the URL where Google can reach your admin server? This is the "
        "base, e.g.:\n"
        "  • `https://your-admin.tailscale.app`  (Tailscale funnel)\n"
        "  • `https://abcd1234.ngrok.io`  (ngrok)\n"
        "  • `https://evolve.your-domain.com`  (real DNS)\n\n"
        "It must be reachable from the public internet — `localhost` and "
        "`mini.local` won't work because Google's OAuth servers can't "
        "reach them. If you don't have one yet, set up a Tailscale funnel "
        "or ngrok tunnel pointed at the admin server's port, then come "
        "back here.\n\n"
        "Paste the base URL (no path, no trailing slash). Or reply "
        "`cancel` to abort."
    )


def render_google_setup_admin_url_invalid(error: str) -> str:
    # Restate the original prompt so the user has the example URLs +
    # cancel hint right there — no scrollback needed. Same principle
    # applies to every ``*_invalid`` render below: error context first,
    # full original ask second.
    return (
        f"**That URL didn't validate** — {error}\n\n"
        + render_google_setup_admin_url()
    )


def render_google_setup_console(
    admin_base_url: str, bot_id: str = "this bot",
) -> str:
    redirect_uri = f"{admin_base_url}/api/admin/onboard/google/callback"
    return (
        "**Step 2 of 4 — Google Cloud Console**\n\n"
        "Make sure you're signed into the Google account this bot should "
        f"use (not necessarily your personal one — if `{bot_id}` belongs to "
        "an org, sign in with that org's admin account first). Then open "
        "https://console.cloud.google.com and do these in order:\n\n"
        "1. **Project**: create a new project (or pick an existing one). "
        f"Name it something like `evolve-{bot_id}` so you can find it "
        "later.\n\n"
        "2. **Enable APIs**: in the search bar, find and enable each:\n"
        "   • Gmail API\n"
        "   • Google Calendar API\n"
        "   • Google Drive API\n"
        "   • Google Docs API\n"
        "   • Google Sheets API\n"
        "   • Google Slides API\n\n"
        "3. **OAuth consent screen** (left nav → APIs & Services → "
        "OAuth consent screen):\n"
        "   • User type: **External** (unless this bot lives in a "
        "Workspace org — then Internal is fine)\n"
        f"   • App name: `Evolve — {bot_id}` (or whatever fits)\n"
        "   • Support email: your email\n"
        "   • Developer contact: your email\n"
        "   • Save and continue through the scopes / test-users steps "
        "(leave defaults; scopes get requested per-bot at consent time)\n\n"
        "4. **Create OAuth Client ID** (Credentials → Create Credentials → "
        "OAuth client ID):\n"
        "   • Application type: **Web application**\n"
        f"   • Name: `Evolve admin — {bot_id}`\n"
        "   • Authorized redirect URI — paste exactly this:\n"
        f"     `{redirect_uri}`\n"
        "   • Create — Google will show you a Client ID and Client Secret.\n\n"
        "Keep the Client ID and Client Secret tab open — I'll ask for "
        "each next. Reply `done` when you're ready, or `cancel` to abort."
    )


def render_google_setup_client_id() -> str:
    return (
        "**Step 3 of 4 — Client ID**\n\n"
        "Paste your Client ID. It should end in `.apps.googleusercontent.com` "
        "and look something like:\n"
        "  `1234567890-abc...xyz.apps.googleusercontent.com`\n\n"
        "Or reply `cancel` to abort."
    )


def render_google_setup_client_id_invalid(error: str) -> str:
    return (
        f"**That client ID didn't validate** — {error}\n\n"
        "It's the long string in the **Client ID** column on the "
        "Credentials page — copy the full value again.\n\n"
        + render_google_setup_client_id()
    )


def render_google_setup_client_secret(bot_id: str = "this bot") -> str:
    return (
        "**Step 4 of 4 — Client Secret**\n\n"
        "Paste your Client Secret. Modern ones start with `GOCSPX-`. The "
        f"secret gets stored on this bot (`{bot_id}`) in its own "
        "`auth-profiles.json` (chmod 600, root-owned) — never in "
        "`network.json`, never in plaintext logs, never on a different "
        "bot.\n\n"
        "Or reply `cancel` to abort."
    )


def render_google_setup_client_secret_invalid(
    error: str, bot_id: str = "this bot",
) -> str:
    return (
        f"**That client secret didn't validate** — {error}\n\n"
        + render_google_setup_client_secret(bot_id)
    )


def render_google_setup_saved(*, admin_base_url: str, bot_id: str) -> str:
    return (
        f"**Google OAuth client saved for `{bot_id}`** ✓\n\n"
        f"  • `client_id` → `network.json → bots[{bot_id}].googleOAuthClient`\n"
        f"  • `client_secret` → `{bot_id}`'s own `auth-profiles.json` "
        "(profile: `_evolve_google_oauth_client`, chmod 600)\n"
        "  • `adminBaseUrl` → `network.json → adminBaseUrl` "
        "(unchanged — same admin server URL for every bot)\n\n"
        f"**Next:** authorize `{bot_id}` against this OAuth app. Run:\n\n"
        "  `evo connect google`\n\n"
        f"...from this same bot. That generates a one-time link the operator "
        "clicks to grant consent — pick the Google account this bot should "
        f"act as. Tokens land in `{bot_id}`'s `auth-profiles.json` and "
        "gallery apps like Morning Briefing pick them up automatically.\n\n"
        "**For other bots:** each bot needs its own setup. Run "
        "`evo setup-google` from `team_bot_a`, `team_bot_c`, `admin_bot`, etc. — typically "
        "with a different Google Cloud project per bot (different orgs / "
        "billing). Two bots can opt-in to share a single OAuth app by "
        "copying one bot's `googleOAuthClient` block into the other's in "
        "`network.json`."
    )


def render_google_setup_failed(reason: str) -> str:
    return (
        "**Couldn't save the Google OAuth client.**\n\n"
        f"{reason}\n\n"
        "No partial state was written. The Client ID and Secret you "
        "pasted are NOT stored anywhere — you'll need to paste them again "
        "if you re-run the wizard. Sorry for the friction. Try "
        "`evo setup-google` again once the underlying issue is fixed."
    )


def render_google_setup_cancelled() -> str:
    return (
        "Cancelled — no credentials saved. Run `evo setup-google` again "
        "any time you want to resume."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot chain — `evo add-bot` (admin-only, M1)
#
# Copy rules: every line here is a primary surface under
# docs/principle-plex-test.md. Plain words only — the operator hears
# "setting up the account" and "the bot's job", never internal terms.
# The canonical register is the reference transcript in
# docs/reference-conversational-bot-creation-2026-05-19.md.
# ─────────────────────────────────────────────────────────────────────────────


_AB_HEADER = (
    "[EVO ADD-BOT — planning a new bot]\n"
    "You are helping the pod admin plan a new bot for this pod. The "
    "Evolve plugin is driving the agenda below; respond conversationally "
    "in your normal voice. Don't quote these instructions, don't list "
    "your phases — just have the conversation. One question at a time; "
    "keep turns short. Use plain words with the user: 'setting up the "
    "account', 'the bot's job' — never internal jargon.\n"
)


# Plain-language renderings of the purpose archetypes for user-facing
# text. The enum values themselves (effectiveness-layer §4) are storage
# keys, not copy — the operator never sees "research-analyst".
_ARCHETYPE_PLAIN: dict[str, str] = {
    "personal-assistant": "personal assistant",
    "project-manager": "project manager",
    "home-automation": "home automation helper",
    "research-analyst": "research assistant",
    "customer-facing": "customer-facing helper",
    "custom": "something custom",
}


def archetype_plain(archetype: Any) -> str:
    """Plain-words label for an archetype enum value. Falls back to the
    raw string for forward-compat with archetypes added later."""
    if not isinstance(archetype, str) or not archetype.strip():
        return "(role not settled yet)"
    return _ARCHETYPE_PLAIN.get(archetype.strip(), archetype.strip())


_AB_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("ab_need", "the need"),
    ("ab_bot_kind", "research assistant vs co-pilot"),
    ("ab_audiences", "who it's for"),
    ("ab_surface", "where it lives"),
    ("ab_shared_feed", "shared or private feed"),
    ("ab_mission", "the bot's job (one line)"),
    ("ab_archetype", "closest role"),
    ("ab_briefing", "morning briefing"),
    ("ab_channel_connect", "channel connect (ready = token in hand)"),
    ("ab_consent_notice", "what people are told"),
    ("ab_opt_out", "how they opt out"),
    ("ab_tone", "message style"),
    ("ab_tg_privacy", "group visibility"),
    ("ab_bot_name", "name"),
)


def _ab_known_block(extracted: dict[str, Any]) -> str:
    """Render the already-gathered add-bot fields so the LLM doesn't
    re-ask. Mirrors the shape of _about_you_block's known list."""
    lines = []
    for key, label in _AB_FIELD_LABELS:
        v = extracted.get(key)
        if not v:
            continue
        if isinstance(v, list):
            lines.append(f"  - {label}: {', '.join(str(x) for x in v)}")
        else:
            lines.append(f"  - {label}: {v}")
    return "\n".join(lines) if lines else "  (nothing yet)"


def _ab_need_block(extracted: dict[str, Any]) -> str:
    return _AB_HEADER + (
        "\nPhase: add_bot need (why before what — first turn).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        "Goal: find out what job the new bot would do, before any talk of "
        "names or apps. Open with one short line saying you'll help plan "
        "a new bot for the pod, then ask what they want it to take on — "
        "the underlying problem, not a feature list. If they describe the "
        "need, one good follow-up: is this bot more of a research "
        "assistant (it surfaces things, they decide) or a co-pilot (it "
        "acts and produces work on their behalf)? Resist jumping to "
        "integrations or apps — that comes much later."
    )


def _ab_audience_block(extracted: dict[str, Any]) -> str:
    return _AB_HEADER + (
        "\nPhase: add_bot audience (who interacts).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        "Goal: learn who this bot is for — just the admin, or a group, "
        "team, or household too? Where would people talk to it (a "
        "Telegram group, Slack, direct messages)? If more than one "
        "audience emerges, ask whether everyone should see the same "
        "content or whether the admin wants a private feed for things "
        "only they see — that answer is load-bearing for everything "
        "downstream. Ask one thing at a time."
    )


def _ab_purpose_block(extracted: dict[str, Any]) -> str:
    return _AB_HEADER + (
        "\nPhase: add_bot purpose (confirm the bot's job in plain words).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        "Goal: mirror the bot's purpose back in one plain sentence — its "
        "job and who it serves — and get an explicit yes or a correction. "
        "Something like: \"So the bot's job is X, for Y — did I get that "
        "right?\" If it isn't clear what kind of bot this is, ask plainly "
        "which is closest: a personal assistant, a project manager, a "
        "home automation helper, a research assistant, something "
        "customer-facing, or something else entirely. Never say "
        "'archetype' to the user — that's an internal word."
    )


def _ab_pack_selection(extracted: dict[str, Any]) -> list[dict]:
    """The admin's current starter-app selection (AB_SHAPE state)."""
    sel = extracted.get("ab_pack_apps")
    if not isinstance(sel, list):
        return []
    return [a for a in sel if isinstance(a, dict) and a.get("name")]


def _ab_shape_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Agenda guidance for the starter-app proposal (delta §5). The
    engine classifies the reply deterministically; this block shapes how
    the bot voices the proposal and the cost/time preview. ``shape_note``
    carries an update or correction from a prior reply."""
    pack = extracted.get("_ab_pack") or {}
    note = str(ctx.get("shape_note") or "").strip()
    note_line = (
        f"\nFIRST, relay this plainly: {note}\n" if note else ""
    )

    none_reason = str(pack.get("none_reason") or "").strip()
    if none_reason:
        return _AB_HEADER + (
            "\nPhase: add_bot starter apps (none to offer — be honest).\n"
            "What we know so far:\n"
            f"{_ab_known_block(extracted)}\n"
            f"{note_line}"
            f"Goal: for this kind of bot, {none_reason} "
            "Don't oversell and don't pad. One short turn: say it "
            "plainly, then ask if they're happy to carry on without "
            "starter apps — any answer moves us along."
        )

    selection = _ab_pack_selection(extracted)
    app_lines = "\n".join(
        f"  - {a.get('name')}: {a.get('blurb') or 'part of the starter set'}"
        for a in selection
    ) or "  (none selected right now)"
    missing = [str(m) for m in (pack.get("missing") or []) if m]
    missing_line = (
        "\nBe honest that these pieces of the set aren't available yet "
        "and won't be included: " + ", ".join(missing) + ".\n"
        if missing else ""
    )
    est_usd = pack.get("est_usd")
    est_minutes = pack.get("est_minutes")
    if selection and est_usd:
        cost_line = (
            f"Give the preview in one plain sentence: {len(selection)} "
            f"app{'s' if len(selection) != 1 else ''}, about "
            f"{est_minutes} minutes and ${est_usd:.2f} to build — then "
            "ask: OK?\n"
        )
    else:
        cost_line = ""
    return _AB_HEADER + (
        "\nPhase: add_bot starter apps (the role suggests a starter set).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        f"{note_line}"
        "Goal: propose this starter set of apps for the new bot, in "
        "plain words — what each one does for them, briefly:\n"
        f"{app_lines}\n"
        f"{missing_line}"
        f"{cost_line}"
        "They can say yes to take the set, adjust it by name (\"drop "
        "the evening sweep\", \"add it back\"), or say none to start "
        "with no apps — more can always be added later. Answer "
        "questions about any app from the list above; don't invent "
        "apps that aren't on it."
    )


def _ab_briefing_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Agenda guidance for the morning-briefing default (delta §6):
    default-on, explicit opt-out turn, 7am-mentioned-changeable — no
    extra turn for the time."""
    note = str(ctx.get("briefing_note") or "").strip()
    note_line = (
        f"\nFIRST, relay this plainly: {note}\n" if note else ""
    )
    in_pack = any(
        "briefing" in str(a.get("name") or "").lower()
        for a in _ab_pack_selection(extracted)
    )
    in_pack_line = (
        "The starter set they accepted already includes the morning "
        "briefing app, so this is just the on/off call.\n"
        if in_pack else ""
    )
    return _AB_HEADER + (
        "\nPhase: add_bot morning briefing (one default, one explicit "
        "yes/no).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        f"{note_line}"
        f"{in_pack_line}"
        "Goal: offer the one default we set up for new bots, in words "
        "close to these: \"One default I'll set up: a short morning "
        "briefing — your schedule and anything that needs you, "
        "delivered here each morning around 7. Want it? You can say "
        "no, or change the time later.\" Take a clear yes or no; if "
        "they decline, accept it gracefully — no second pitch, no "
        "nagging."
    )


def _ab_channel_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Agenda guidance for the optional channel connect (offer-now /
    auto-activate-later, 2026-06-11 design sync): one easy offer, a
    paste or a skip, no pressure — skipping is a first-class answer
    because the briefing sets itself up when a channel connects later."""
    note = str(ctx.get("channel_note") or "").strip()
    note_line = f"\nFIRST, relay this plainly: {note}\n" if note else ""
    briefing_on = str(extracted.get("ab_briefing") or "") == "on"
    why_line = (
        "the morning briefing they just said yes to needs one to land"
        if briefing_on else
        "people need a place to talk to the bot"
    )
    return _AB_HEADER + (
        "\nPhase: add_bot channel connect (optional — paste now or "
        "skip).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        f"{note_line}"
        "Goal: they said the bot will live on Telegram, and "
        f"{why_line}. Offer, in words close to these: \"If you have "
        "the bot's Telegram token handy — the one @BotFather gives "
        "you — paste it here and I'll hook the chat up as soon as the "
        "bot is built. Don't have it? Say skip; you can connect one "
        "later from the admin page and everything we set up here will "
        "follow.\" Take the token or the skip; never push twice."
    )


def _ab_consent_tone_block(
    extracted: dict[str, Any], ctx: dict[str, Any] | None = None,
) -> str:
    """Agenda guidance for the consent + tone phase (delta §7) — the
    non-skippable questions: who can see what this bot collects, how
    they opt out, and what the bot should sound like."""
    ack = str((ctx or {}).get("channel_ack") or "").strip()
    ack_line = f"FIRST: {ack}\n" if ack else ""
    if _phases.ab_group_audience(extracted):
        consent_goal = (
            "This bot serves more than just the admin, so the people in "
            "its group need to know what it does with what it sees. "
            "Propose a short intro notice the bot will pin or post — "
            "draft the actual wording and let them adjust it — plus an "
            "opt-out anyone can use (a reaction like 🤐 on a message to "
            "keep it out, or just telling the admin). Get an explicit "
            "yes on both the notice wording and the opt-out.\n"
        )
    else:
        consent_goal = (
            "This bot serves one person, so the consent answer is about "
            "what the bot keeps from your conversations and how to turn "
            "that off. Propose a one-line statement of that posture — "
            "e.g. \"This bot keeps notes from our chats to do its job; "
            "tell it 'stop keeping notes' any time\" — and let them "
            "adjust the wording. Get an explicit yes.\n"
        )
    if _phases.ab_telegram_group_surface(extracted):
        tg_line = (
            "Also ask, in plain words: should the bot see every message "
            "in the group, or only messages that mention it? (Telegram "
            "lets them pick either; this matters for what the bot can "
            "react to.)\n"
        )
    else:
        tg_line = ""
    return _AB_HEADER + (
        "\nPhase: add_bot consent + tone (required — never finish a bot "
        "without these answers).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        f"{ack_line}"
        f"Goal, in two parts. First, consent: {consent_goal}"
        f"{tg_line}"
        "Second, tone: ask how the bot should sound. The house default "
        "is short and direct — a brief header, one fact per line, a "
        "conversational close, no alarm-style labels — offer that and "
        "let them confirm or adjust. One question at a time; don't "
        "move on until both parts have clear answers."
    )


def _ab_name_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    taken = ctx.get("name_taken")
    taken_line = ""
    if taken:
        taken_line = (
            f"\nIMPORTANT: the name '{taken}' is already in use on this "
            "pod — open by saying so plainly and ask for a different "
            "name (you can propose fresh candidates).\n"
        )
    return _AB_HEADER + (
        "\nPhase: add_bot naming (the role is locked — now the name).\n"
        "What we know so far:\n"
        f"{_ab_known_block(extracted)}\n"
        f"{taken_line}"
        "Goal: propose three or four short names that fit the bot's job, "
        "each with a few words of rationale, and let the admin pick or "
        "counter with their own. Mention naturally that the bot's "
        "everyday name and its account name can differ — the account "
        "name is just the lowercase form (e.g. \"Atlas\" lives on the "
        "account \"atlas\"). Don't move on until they've clearly picked "
        "one."
    )


def render_add_bot_plan(
    extracted: dict[str, Any],
    *,
    note: str | None = None,
) -> str:
    """Verbatim plan echo — the canonical text the admin confirms.

    M1 honesty rule: the render says outright that nothing is built yet.
    The engine appends nothing; this is the whole message."""
    name = str(extracted.get("ab_bot_name") or "").strip() or "(unnamed)"
    account = _phases.account_name_for(name) or "(to be set)"
    mission = str(extracted.get("ab_mission") or "").strip() or "(not settled yet)"
    role = archetype_plain(extracted.get("ab_archetype"))
    audiences = extracted.get("ab_audiences")
    if isinstance(audiences, list) and audiences:
        for_line = ", ".join(str(a) for a in audiences)
    else:
        for_line = "(not settled yet)"
    surface = str(extracted.get("ab_surface") or "").strip()
    feed = str(extracted.get("ab_shared_feed") or "").strip()

    lines = [
        "Here's the plan:",
        "",
        f"Bot:    {name}  (account: {account})",
        f"Job:    {mission}",
        f"Role:   {role}",
        f"For:    {for_line}",
    ]
    if surface:
        lines.append(f"Where:  {surface}")
    if feed:
        lines.append(f"Feed:   {feed}")

    # M3 defaults — apps, briefing, consent + tone (delta §3.2: the plan
    # echoes everything the conversation decided).
    selection = _ab_pack_selection(extracted)
    pack = extracted.get("_ab_pack") or {}
    if selection:
        apps_line = ", ".join(str(a.get("name")) for a in selection)
        est_usd = pack.get("est_usd")
        est_minutes = pack.get("est_minutes")
        if est_usd:
            apps_line += (
                f"  (about {est_minutes} min and ${float(est_usd):.2f} "
                "to build)"
            )
        lines.append(f"Apps:   {apps_line}")
    elif extracted.get("_ab_pack") is not None:
        lines.append("Apps:   none to start — add any time later")
    briefing = str(extracted.get("ab_briefing") or "").strip()
    if briefing == "on":
        lines.append("Brief:  morning briefing each day around 7 (changeable)")
    elif briefing == "off":
        lines.append("Brief:  no morning briefing (your call, recorded)")
    channel_connect = str(extracted.get("ab_channel_connect") or "").strip()
    if channel_connect == "ready":
        ch = _channel_display(str(extracted.get("_ab_channel") or "telegram"))
        lines.append(f"Chat:   {ch} token in hand — hooked up right after the build")
    elif channel_connect == "skipped":
        lines.append("Chat:   connect a channel later from the admin page")
    consent = str(extracted.get("ab_consent_notice") or "").strip()
    if consent:
        lines.append(f"Notice: {consent}")
    opt_out = str(extracted.get("ab_opt_out") or "").strip()
    if opt_out:
        lines.append(f"OptOut: {opt_out}")
    tone = str(extracted.get("ab_tone") or "").strip()
    if tone:
        lines.append(f"Tone:   {tone}")
    tg = str(extracted.get("ab_tg_privacy") or "").strip()
    if tg:
        lines.append(f"Sees:   {tg}")

    lines += [
        "",
        "Nothing is built yet — this is the plan, and it's only saved "
        "once you confirm.",
        "",
        "Reply **yes** to save it, tell me what to change (\"make it "
        "weekly\", \"call it Almanac\"), or **cancel** to drop it.",
    ]
    body = "\n".join(lines)
    if note:
        body = note + "\n\n" + body
    return body


def render_add_bot_cancelled() -> str:
    return (
        "No problem — plan dropped. Nothing was saved and nothing was "
        "built. Say `evo add-bot` any time to plan a new bot."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot M2 — credentials walk + build progress + smoke wrap
#
# Same copy rules as above: these are primary surfaces. Progress lines
# name outcomes, never stages ("✓ Bot account created", never the
# pipeline's internal stage names). Honesty rules (delta §3.2): failures
# are relayed truthfully — what failed and what was cleaned up — with no
# "fixed itself" framing, and a bot that fails its final check is never
# left half-running.
# ─────────────────────────────────────────────────────────────────────────────


def _ab_display_name(extracted: dict[str, Any]) -> str:
    return str(extracted.get("ab_bot_name") or "the new bot").strip()


def _ab_credentials_block(extracted: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Agenda guidance for the credential walk. The engine classifies the
    reply deterministically; this block only shapes how the bot voices
    the ask. ``cred_note`` carries a correction from a prior reply (bad
    paste, unknown borrow source) that the bot should relay first."""
    name = _ab_display_name(extracted)
    candidates = [
        str(c) for c in (extracted.get("_ab_borrow_candidates") or []) if c
    ]
    note = str(ctx.get("cred_note") or "").strip()
    note_line = (
        f"\nFIRST, relay this correction plainly: {note}\n" if note else ""
    )
    if candidates:
        borrow_line = (
            "  - Use a copy of another bot's key — available from: "
            + ", ".join(candidates)
            + ". (They'd reply e.g. \"use "
            + candidates[0]
            + "'s key\". The copy is independent afterwards — replacing "
            "the original later doesn't affect the new bot.)\n"
        )
    else:
        borrow_line = (
            "  (No other bot here has a key that can be copied, so "
            "don't offer that.)\n"
        )
    return _AB_HEADER + (
        f"\nPhase: add_bot credentials (plan for {name} is confirmed and "
        "saved — one thing is needed before building).\n"
        f"{note_line}"
        f"Goal: {name} needs its own Claude key to think. Present the "
        "choices in plain words and ask which they'd like:\n"
        f"{borrow_line}"
        "  - Paste a fresh key (an Anthropic API key — starts with "
        "sk-ant- — from console.anthropic.com).\n"
        "  - Skip for now — nothing gets built yet; the plan stays "
        "saved and they can pick this up later.\n"
        "They can also say cancel. Don't invent other options, don't "
        "ask for any other credentials, and never repeat a pasted key "
        "back to them."
    )


# Outcome language for the build pipeline's stages — the operator hears
# what was accomplished, never the internal stage name. Keyed by the
# stage strings ``provisioning`` emits (a stable contract — see
# STAGES_IN_ORDER there). Each value is (doing, done): the in-progress
# phrasing and the completed ✓ line.
_AB_STAGE_OUTCOMES: dict[str, tuple[str, str]] = {
    # Pre-stage from provision_driver: resolving a borrowed key before
    # anything is created.
    "borrow_credential":   ("reading the key to copy", "Key copied"),
    "validate_inputs":     ("running the pre-flight checks", "Checks passed"),
    "create_macos_user":   ("creating the bot's account", "Bot account created"),
    "create_openclaw_dir": ("setting up its home folder", "Home folder ready"),
    "openclaw_onboard":    ("connecting it to Claude", "Connected to Claude"),
    "add_bot_to_network":  ("adding it to your pod", "Added to your pod"),
    "deploy_bot":          ("switching it on", "Switched on"),
    "seed_model_config":   ("picking its starting model", "Starting model picked"),
    # Only fires on the --skip-deploy path (the wizard always deploys), but the
    # stage string is a stable contract so keep its outcome language here too.
    "seed_content_scan_docs": ("seeding its starter docs", "Starter docs seeded"),
}


def _ab_stage_doing(stage: str) -> str:
    return _AB_STAGE_OUTCOMES.get(stage, (f"working on {stage}", ""))[0]


def _ab_progress_lines(events: list) -> tuple[list[str], str | None]:
    """Collapse raw (stage, status, detail) events into ✓ outcome lines
    plus the in-flight stage's doing-phrase (or None if nothing is
    mid-flight). Skipped stages are omitted — they're internal routing,
    not outcomes the operator needs."""
    done: list[str] = []
    doing: str | None = None
    last: dict[str, str] = {}
    order: list[str] = []
    for ev in events:
        stage, status = str(ev[0]), str(ev[1])
        if stage not in last:
            order.append(stage)
        last[stage] = status
    for stage in order:
        status = last[stage]
        if status == "ok":
            label = _AB_STAGE_OUTCOMES.get(stage, ("", ""))[1]
            if label:
                done.append(f"✓ {label}")
        elif status == "start":
            doing = _ab_stage_doing(stage)
    return done, doing


def render_add_bot_provision_kickoff(extracted: dict[str, Any]) -> str:
    """Verbatim render the moment the build starts."""
    name = _ab_display_name(extracted)
    return (
        f"On it — building **{name}** now. I'll create its account, "
        "connect it to Claude, and switch it on. This usually takes a "
        "couple of minutes.\n\n"
        "Message me anytime (a simple \"status\" works) and I'll tell "
        "you where things stand."
    )


def render_add_bot_provision_progress(
    snap: dict[str, Any], *, note: str | None = None,
) -> str:
    """Verbatim progress report while the build is running."""
    name = str(snap.get("display_name") or "the new bot")
    done, doing = _ab_progress_lines(snap.get("events") or [])
    lines: list[str] = []
    if note:
        lines += [note, ""]
    lines.append(f"Building **{name}** — here's where things stand:")
    lines.append("")
    lines += done or ["(just getting started)"]
    if doing:
        lines.append(f"… right now: {doing}")
    lines += [
        "",
        "Still working — check back with me in a minute.",
    ]
    return "\n".join(lines)


# Plain-words translation of the cleanup actions a failed build runs.
# Keyed by substrings of the pipeline's rollback labels (a best-effort
# match — unrecognized labels fall through verbatim, which is the honest
# choice for cleanup we can't describe more plainly).
def _ab_cleanup_line(raw: str) -> str:
    failed_suffix = ""
    base = raw
    if "(FAILED:" in raw:
        base = raw.split("(FAILED:", 1)[0].strip()
        failed_suffix = (
            " — this cleanup step did NOT finish; worth a look from the "
            "admin page"
        )
    if "macOS user" in base:
        line = "removed the bot's account"
    elif ".openclaw" in base:
        line = "removed its home folder setup"
    elif "network.json" in base:
        line = "took it back out of the pod"
        if "kept the saved plan" in base:
            line += " (your plan is safe)"
    else:
        line = base
    return f"- {line}{failed_suffix}"


def render_add_bot_provision_failed(snap: dict[str, Any]) -> str:
    """Verbatim, honest failure report: what finished, what failed (with
    the technical detail preserved), what was cleaned up, and the two
    ways forward. No softening, no "fixed itself" framing."""
    name = str(snap.get("display_name") or "the new bot")
    done, _doing = _ab_progress_lines(snap.get("events") or [])
    failed_stage = str(snap.get("failed_stage") or "")
    error = str(snap.get("error") or "").strip()
    rollback = [str(r) for r in (snap.get("rollback_log") or [])]

    lines = [f"I couldn't finish building **{name}**. Here's exactly what happened:"]
    if done:
        lines += ["", "What finished first:"] + done
    lines += [
        "",
        f"What failed: {_ab_stage_doing(failed_stage)} didn't work.",
    ]
    if error:
        lines.append(f"The technical detail, in case you need it: {error}")
    if rollback:
        lines += ["", "I cleaned up everything from this attempt:"]
        lines += [_ab_cleanup_line(r) for r in rollback]
        lines += [
            "",
            "So right now: no half-built bot is left on your pod, and "
            "your plan is still saved.",
        ]
    else:
        lines += [
            "",
            "Nothing had been created yet, so there's nothing to clean "
            "up — and your plan is still saved.",
        ]
    lines += [
        "",
        "Say **retry** to try the build again, or **stop** to leave it "
        "for later.",
    ]
    return "\n".join(lines)


def render_add_bot_provision_lost(
    extracted: dict[str, Any], *, in_members: bool,
) -> str:
    """Verbatim render when the session says a build was running but no
    record of it exists anymore — the admin service restarted mid-run.
    Honest about what we know and don't."""
    name = _ab_display_name(extracted)
    if in_members:
        seen = (
            f"What I can see: **{name}** did land in your pod, so the "
            "build most likely finished. Check it in the Bots list on "
            "the admin page — and if it looks right there, you're done."
        )
    else:
        seen = (
            f"What I can see: **{name}** is not in your pod, so the "
            "build most likely didn't finish."
        )
    return (
        f"I have to be honest: I lost track of {name}'s build — that "
        "usually means the service that runs it restarted mid-way.\n\n"
        f"{seen}\n\n"
        "Say **retry** to run the build again (I'll need the key "
        "again), or **stop** to leave things as they are."
    )


def render_add_bot_success_wrap(
    extracted: dict[str, Any],
    snap: dict[str, Any],
    *,
    queued: dict[str, Any] | None = None,
) -> str:
    """Terminal render: built, switched on, passed its final check.

    ``queued`` (M3) reports what finalize set in motion::

        {
          "apps":          [name, ...],   # installs that queued OK
          "apps_failed":   [name, ...],   # installs that did NOT queue
          "est_minutes":   int,
          "briefing":      "on" | "off" | "",
          "guide_written": bool,
        }

    Honesty rules apply: a failed queue or guide write is said plainly,
    never papered over.
    """
    name = str(snap.get("display_name") or _ab_display_name(extracted))
    done, _doing = _ab_progress_lines(snap.get("events") or [])
    source = str(snap.get("credential_source") or "")
    lines = [f"Done ✓ **{name}** is up and running.", ""]
    lines += done
    lines.append("✓ Passed its final check — it's ready to think")
    if source.startswith("borrow:"):
        src_bot = source.split(":", 1)[1]
        lines += [
            "",
            f"One note: {name} runs on a copy of {src_bot}'s key. The "
            f"copy is independent — if you ever replace {src_bot}'s "
            f"key, {name} keeps its own.",
        ]

    q = queued or {}
    apps = [str(a) for a in (q.get("apps") or [])]
    apps_failed = [str(a) for a in (q.get("apps_failed") or [])]
    if apps:
        est = q.get("est_minutes")
        eta = f" — about {est} minutes" if est else ""
        lines += [
            "",
            f"Its apps are being built now{eta}, nothing for you to do: "
            + ", ".join(apps) + ". You can watch them land on the "
            "Apps page.",
        ]
    if apps_failed:
        lines += [
            "",
            "I could NOT start these app builds: "
            + ", ".join(apps_failed) + ". You can install them from "
            "the app gallery on the admin page.",
        ]

    # Channel connect outcome (offer-now half, 2026-06-11 decision).
    # Honesty rules: connected is stated with the handle when we have
    # it; a failure or a restart-eaten token is said plainly with the
    # recovery step — never papered over.
    channel = q.get("channel") or {}
    ch_status = str(channel.get("status") or "none")
    ch_name = _channel_display(str(channel.get("channel") or ""))
    ch_connected = ch_status == "connected"
    if ch_connected:
        handle = str(extracted.get("_ab_channel_username") or "").strip()
        at = f" (@{handle})" if handle else ""
        lines += [
            "",
            f"{name} is connected to {ch_name}{at} — the chat works as "
            "soon as the apps finish.",
        ]
    elif ch_status == "failed":
        err = str(channel.get("error") or "").strip()
        why = f" ({err})" if err else ""
        lines += [
            "",
            f"One thing did NOT finish: the {ch_name} hookup{why}. You "
            "can connect it from the Bots list on the admin page — "
            "nothing else is affected.",
        ]
    elif ch_status == "lost":
        lines += [
            "",
            f"One honest note: the {ch_name} token you pasted didn't "
            "survive a restart on my side, so the chat is NOT connected "
            "yet. Paste it again on the Bots list on the admin page.",
        ]

    briefing = str(q.get("briefing") or "")
    if briefing == "on" and ch_connected:
        recipient_note = (
            "" if channel.get("recipient_recorded") else
            f" One step left so it knows who to message: set yourself "
            f"as {name}'s person on the admin page (Users → Set "
            "primary user)."
        )
        lines += [
            "",
            f"Your morning briefing is set up — the first one lands on "
            f"{ch_name} at the next 7 o'clock. Change the time any "
            f"time on the admin page.{recipient_note}",
        ]
    elif briefing == "on":
        lines += [
            "",
            f"Your morning briefing is saved and ready — it switches "
            f"itself on the moment {name} gets a place to chat, and "
            "the first one lands the next morning at 7. Connect a "
            "channel any time from the admin page.",
        ]
    elif briefing == "off":
        lines += [
            "",
            "No morning briefing, as you asked — that's recorded, and "
            "nothing will nag you about it.",
        ]
    if q.get("guide_written"):
        lines += [
            "",
            f"I've written down the ground rules you set — what people "
            f"are told, how they opt out, and how {name} should sound. "
            f"{name} reads them at the start of every conversation.",
        ]
    elif q.get("guide_failed"):
        lines += [
            "",
            "One thing did NOT save: the ground rules (what people are "
            "told, the bot's tone). Worth re-doing from the admin page "
            "— I'd rather tell you than pretend.",
        ]

    if ch_connected:
        lines += [
            "",
            f"What's next: say hello to {name} on {ch_name} — and see "
            "everything else about it on the admin page.",
        ]
    else:
        lines += [
            "",
            f"What's next: {name} doesn't have a place to chat yet. Open "
            "the Bots list on the admin page to connect one (Telegram, "
            "Slack, and friends) — and to see everything else about it.",
        ]
    return "\n".join(lines)


# Projection of the channel registry's ``display_label`` column (product-name
# form, unlike safety_summary's prose form).
#
# WIDENING (deliberate, M1-B1): this table covered 6 of the registry's 9
# channels. The 3 it gained (signal / email / webhook) render identically to
# what the ``.capitalize()`` fallback below already produced — "Signal",
# "Email", "Webhook" — so the widening is provably a no-op, pinned by
# test_channel_registry_projections.
_CHANNEL_DISPLAY_NAMES = _channel_registry.labels_where(lambda c: True)


def _channel_display(channel_id: str) -> str:
    """Operator-facing channel name — data table with a capitalize
    fallback so an unmapped id still reads like a product name."""
    cid = (channel_id or "").lower()
    return _CHANNEL_DISPLAY_NAMES.get(cid, cid.capitalize() or "the channel")


def render_add_bot_smoke_failed(
    extracted: dict[str, Any], *, summary: str,
) -> str:
    """Verbatim render when the built bot fails its final check. The
    admin decides: try the check again, or switch the bot off — it is
    never left looking alive while broken."""
    name = _ab_display_name(extracted)
    detail = f" The check said: {summary}" if summary.strip() else ""
    return (
        f"**{name}** is built, but it did NOT pass its final check — I "
        f"won't pretend otherwise.{detail}\n\n"
        "I don't want to leave you a bot that looks alive but isn't, "
        "so it's your call:\n"
        "- **try again** — run the check once more (a freshly started "
        "bot sometimes needs a minute)\n"
        f"- **switch it off** — I'll turn {name} off; you can finish "
        "setting it up later from the Bots list on the admin page\n\n"
        "Which would you like?"
    )


def render_add_bot_quarantined(
    extracted: dict[str, Any], *, ok: bool, detail: str = "",
) -> str:
    """Terminal render after switching a failed bot off."""
    name = _ab_display_name(extracted)
    if ok:
        return (
            f"Done — **{name}** is switched off. Nothing half-working "
            "is left running.\n\n"
            "Its account and your plan are still there: pick it up any "
            "time from the Bots list on the admin page."
        )
    extra = f" ({detail})" if detail.strip() else ""
    return (
        f"I tried to switch **{name}** off but could NOT confirm it "
        f"stopped{extra}. Please check it in the Bots list on the "
        "admin page — I'd rather tell you that straight than guess."
    )


def render_add_bot_paused(
    extracted: dict[str, Any], *, reason_line: str,
) -> str:
    """Terminal render for every "stop here, keep the plan" path —
    skipping the key, cancelling at the credential step, or stopping
    after a failed build."""
    name = _ab_display_name(extracted)
    return (
        f"{reason_line}\n\n"
        f"**{name}** isn't built — but your plan is saved, so nothing "
        "you decided is lost. When you're ready (key in hand), say "
        "`evo add-bot` and we'll pick up right here."
    )
