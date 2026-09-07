"""Wizard turn engine — orchestrates state, extraction, advancement, and
prompt rendering.

Two entry points:

  * :func:`start_session` — invoked when the user types ``evo wizard``.
    Creates fresh ``in_progress`` state at the initial phase and returns
    the systemAppend that drives the bot's first reply.

  * :func:`process_turn` — invoked for every subsequent user message
    while the wizard is active. Runs extraction on the user's message
    against the current phase's targets, advances when the exit condition
    is met, commits the profile when WRAP terminates, returns the next
    systemAppend.

Both return :class:`TurnResult`, which the route handler maps onto the
wire envelope the plugin consumes.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). Every ``pkg_id`` in this module is a GALLERY
# CATALOG key: the selection map is keyed by it, the app-pick prompt renders it,
# the pack constants name it, and ``new_pkg_id()`` MINTS one for a forge build.
# None of them is a manifest read — the wizard runs before the app exists on the
# bot. ``c.get("display_name") or c.get("pkg_id")`` is a label fallback, not an
# identity chain: the alternative is a human string.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import audit as _audit
from .. import identity as _identity
from ... import external_ids as _ext_ids
from . import config as _config
from . import extractor as _extractor
from . import intent as _intent
from . import phases as _phases
from . import prompts as _prompts
from . import state as _state
from . import user_profile_writer as _profile_writer
from .. import _delimiters as _evo_delim


@dataclass
class TurnResult:
    """What the route handler returns to the plugin for one wizard turn.

    Render-mode dispatch (see ``phases.RenderMode``):

      * **verbatim** phases populate ``direct_send_message`` with the
        exact body to display to the user. ``system_append`` carries a
        STAY-SILENT injection so the LLM produces nothing. The plugin's
        wizard-turn path direct-sends via channel transport (Telegram
        Bot API, etc.) and the LLM is never asked to render the body.

      * **agenda** phases populate ``system_append`` with guidance for
        the LLM to interpret. ``direct_send_message`` is ``None``. The
        plugin attaches systemAppend as today; the LLM engages naturally.

    The route handler / plugin checks which field is set and routes
    accordingly. This mirrors the (older, working) pattern that
    non-wizard ``evo`` subcommands have used since #1054 — see
    :class:`evolve_admin.evo.dispatch.DispatchResult`.
    """

    system_append: str
    wizard_session_id: str | None  # None on terminal — plugin clears state
    phase: str
    completed: bool
    # When True, ``network`` was mutated in place and the route handler
    # must persist via :func:`evolve_admin.config.save_network`. Set by
    # the challenge handler when admin/primary claims are applied.
    network_dirty: bool = False
    # Verbatim mode: the body to direct-send to the user. Auto-populated
    # in ``__post_init__`` from the phase's render_mode when not set
    # explicitly — verbatim phases get ``direct_send_message`` =
    # ``system_append`` so the plugin direct-sends without each call site
    # having to plumb the field. Agenda phases get ``None``.
    direct_send_message: str | None = None
    # Short human-readable description of what wizard the user is in
    # (mirrors DispatchResult.subcommand_brief — same purpose: lets the
    # plugin's stay-silent directive brief the LLM with the actual
    # wizard's meaning instead of a generic "evo flow"). Optional;
    # auto-derived from the audience field of the wizard state when
    # not set explicitly.
    subcommand_brief: str | None = None

    def __post_init__(self) -> None:
        # Auto-route based on the phase's declared render_mode. Verbatim
        # phases (the default — see ``phases.RenderMode``) get
        # direct-sent by the plugin; agenda phases keep the LLM in the
        # loop. No call site has to think about it — the phase is
        # authoritative.
        #
        # For verbatim phases, the body comes in via ``system_append``
        # (the renderer's only output). We:
        #   - Copy the body to ``direct_send_message`` so the plugin can
        #     direct-send via channel transport (Telegram Bot API, etc.)
        #   - Wrap ``system_append`` with the "respond verbatim"
        #     framing — the LLM-fallback path uses it when direct-send
        #     fails (no chatId, no Bot token, network blip). Mirrors
        #     :func:`evolve_admin.evo.handlers.help._speak_verbatim`.
        if self.direct_send_message is None and self.system_append:
            try:
                phase = _phases.get_phase(self.phase)
            except Exception:
                phase = None
            if phase is not None and phase.render_mode == "verbatim":
                body = self.system_append
                self.direct_send_message = body
                self.system_append = _verbatim_wrap(body)
        # Apply visual delimiters to the user-visible body. See
        # ``evo._delimiters`` for rationale — until upstream OC fires
        # post-LLM hooks reliably we can't suppress the LLM's
        # contradictory chime-in, so framing the canonical content
        # lets the operator distinguish trustworthy from hallucinated.
        # Idempotent: skips re-wrap if already delimited.
        if self.direct_send_message and not _evo_delim.is_wrapped(self.direct_send_message):
            self.direct_send_message = _evo_delim.wrap(self.direct_send_message)


def _verbatim_wrap(body: str) -> str:
    """Wrap a verbatim-mode wizard body with the LLM-fallback framing.

    Used only when the plugin's direct-send path failed and the LLM has
    to render the body itself. Mirrors
    :func:`evolve_admin.evo.handlers.help._speak_verbatim` and the
    plugin's ``KeywordHandler.buildKeywordInjection`` so behavior is
    identical across the dispatch and wizard paths.
    """
    return (
        "IMPORTANT: The user is mid-wizard. The plugin's direct-send "
        "transport was unavailable, so respond ONLY with the following "
        "message, verbatim. Do not add commentary, framing, or any "
        "additional text:\n\n" + body
    )


# ─────────────────────────────────────────────────────────────────────────────
# Session lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def start_session(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    audience: _state.Audience = "primary",
    role: str = "primary",
) -> TurnResult:
    """Begin (or resume) a wizard for ``(bot_id, user_key)``.

    ``role`` is the caller's resolved role — "admin", "primary", or
    "secondary". Admins and primaries start at GREET (the existing
    onboarding flow). Secondaries — callers who aren't yet recognized as
    either — start at CHALLENGE so they can prove identity via passphrase
    before the wizard collects any personal info.

    If a completed run exists we restart at the audience-appropriate
    initial phase. If an in-progress run exists we resume from where it
    left off, regardless of role (we don't suddenly demote a user who
    already authenticated mid-wizard)."""
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active():
        st = existing
    else:
        initial = _phases.initial_phase(audience, role)
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience=audience,
            initial_phase=initial,
        )

    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def start_guide_draft(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    seed: dict[str, Any] | None = None,
) -> TurnResult:
    """Begin (or resume) a guide-draft session for ``(bot_id, user_key)``.

    ``seed`` pre-populates the extracted dict — passed in from the
    dispatcher when an existing guide is being edited rather than
    authored from scratch. If a completed run exists we restart at
    GUIDE_GATHER (with seed merged in); if an in-progress run exists we
    resume from where it left off (seed ignored — don't clobber the
    user's mid-draft state).
    """
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active() and existing.audience == "guide_drafter":
        st = existing
    else:
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="guide_drafter",
            initial_phase=_phases.PHASE_GUIDE_GATHER,
        )
        if seed:
            st.extracted.update(seed)
            _state.write_state(shared_dir, st)

    # When seed is non-empty, the prompt builder needs to know we're
    # editing rather than authoring fresh. Pass via context.
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={"editing_existing": bool(seed)},
        ),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def start_push_preamble(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    network: dict[str, Any] | None = None,
) -> TurnResult | None:
    """Slice 5b8 day 2 (scaffolding only — not yet wired to the plugin).

    Check whether ``bot_id`` has a security_critical / operational_urgent
    rec pending for ``user_key``'s primary surface, and if so start a
    rec_pending wizard session with the ``push`` pitch variant. The
    intent: a future plugin pass calls this at the top of a non-evo
    user turn, gets back a TurnResult that prepends a "before we dive
    in — I noticed X" preamble to the bot's next reply, and the user's
    follow-up routes through the existing wizard turn endpoint.

    Returns ``None`` (and writes no state) when:
      * ``conversational_approval.push_preamble_enabled`` is False
        (default); operators opt in explicitly.
      * No rec is queued, or the top rec isn't urgent.
      * BetterEngine import / ctor fails.

    No tests fail on the False default — this is dormant code paths
    until day 3+ ships actual push delivery.
    """
    cfg = _config.resolve(shared_dir, bot_id)
    if not cfg.enabled or not cfg.push_preamble_enabled:
        return None

    try:
        from ...better_engine.engine import BetterEngine
    except Exception:
        return None
    net = network if isinstance(network, dict) else {}
    try:
        engine = BetterEngine(shared_dir, net)
        rec = engine.get_top(surface="member_bot", scope_id=bot_id)
    except Exception:
        return None
    if rec is None:
        return None
    try:
        rec_dict = rec.to_dict()
    except Exception:
        return None
    if not _is_urgent_rec(rec_dict):
        return None

    # Don't clobber an active wizard session — push only fires when the
    # caller would otherwise be talking to the bot normally.
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active():
        return None

    st = _state.initialize(
        shared_dir,
        bot_id=bot_id,
        user_key=user_key,
        audience="approver",
        initial_phase=_phases.PHASE_REC_PENDING,
    )
    st.extracted["_pending_rec"] = dict(rec_dict)
    st.extracted["_initial_surface"] = "member_bot"
    st.extracted["_initial_scope_id"] = bot_id
    st.extracted["_recs_shown"] = 1
    st.extracted["_push_preamble"] = True  # diagnostic flag, not consumed
    _state.write_state(shared_dir, st)

    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={"variant": "push"},
        ),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def _enrich_with_lineage(shared_dir: Path, rec: dict[str, Any]) -> dict[str, Any]:
    """Attach a ``_lineage_summary`` field to the rec when it's
    proposal-derived AND has past terminal-status proposals with the
    same fingerprint. The pitch prompt reads this field and weaves a
    one-line acknowledgement of prior history into the framing.

    Best-effort: any error returns the rec unmodified so the chat flow
    degrades to lineage-blind pitching rather than failing the turn.
    """
    if not isinstance(rec, dict):
        return rec
    try:
        from .. import arbiter_bridge as _bridge

        proposal_id = _bridge.proposal_id_for_rec(rec)
        if not proposal_id:
            return rec
        summary = _bridge.proposal_lineage_summary(shared_dir, proposal_id)
    except Exception:
        return rec

    if summary:
        rec["_lineage_summary"] = summary
    return rec


def _is_urgent_rec(rec: dict[str, Any]) -> bool:
    """A rec qualifies as urgent for push-mode if it carries a
    ``urgency:critical`` or ``urgency:high`` tag, OR a
    ``type:security`` / ``type:operational_urgent`` tag (covering both
    the v1 priority-tag scheme and the v2 type taxonomy)."""
    if not isinstance(rec, dict):
        return False
    tags = rec.get("tags") or []
    if not isinstance(tags, list):
        return False
    urgent_markers = {
        "urgency:critical", "urgency:high",
        "type:security_critical", "type:operational_urgent",
    }
    for t in tags:
        if isinstance(t, str) and t.lower() in urgent_markers:
            return True
    # Fall back to inspecting the rec.type field for v2 RSI shapes.
    rec_type = (rec.get("type") or "").lower()
    return rec_type in ("security_critical", "operational_urgent")


def start_rec_pending(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    rec: dict[str, Any] | None,
    surface: str = "member_bot",
    pending_kind: str = "inbox",
) -> TurnResult:
    """Begin (or resume) a conversational-approval session for ``(bot_id,
    user_key)``. Slice 5b8.

    ``rec`` is the top recommendation the dispatcher just fetched (a
    Recommendation.to_dict() shape), or ``None`` when the queue is empty.
    On the empty path we initialize state at REC_PENDING and immediately
    finalize with the "all caught up" wrap so the bot can deliver the
    appropriate one-liner — no follow-up turns expected.

    On the populated path we initialize state with ``_pending_rec`` (and
    the surface / scope_id we fetched against, for chaining to next recs)
    stashed under underscore-prefixed keys so :func:`profile.commit`
    doesn't pollute the user profile when the session ends. The first
    turn renders the pitch; subsequent user replies route through
    :func:`_handle_rec_pending` via :func:`process_turn`.

    ``pending_kind`` distinguishes inbox pitches ("here's a new proposal")
    from in-process follow-ups ("you took this on earlier — done?"). The
    value is stored on state so the handler can pick the right intent
    parsing (``allow_complete=True`` for inprocess) and route ``yes`` /
    ``done`` to the right bridge call.
    """
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active() and existing.audience == "approver":
        # Resume: don't reset extracted, just re-render the current
        # variant. (Resumption is rare in practice — a fresh bare ``evo``
        # is the usual entry — but handling it defensively keeps the
        # plugin's wizardSessionId-based router from getting wedged.)
        st = existing
    else:
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="approver",
            initial_phase=_phases.PHASE_REC_PENDING,
        )

    if not rec:
        # Queue empty — finalize with the "all caught up" wrap on this
        # same turn so the plugin clears its session state and the bot
        # delivers the friendly one-liner. No state.extracted writes.
        return _finalize_rec_pending(
            shared_dir, st, bot_id, user_key,
            variant="all_caught_up",
        )

    # Stash the rec + surface so the handler can chain to next recs on
    # the same surface. Underscore prefix keeps these out of any future
    # profile commit (also mirrored by the rec_pending-specific
    # finalize, which doesn't commit at all).
    rec_with_lineage = _enrich_with_lineage(shared_dir, dict(rec))
    st.extracted["_pending_rec"] = rec_with_lineage
    st.extracted["_initial_surface"] = surface
    st.extracted["_initial_scope_id"] = bot_id
    st.extracted["_pending_kind"] = pending_kind
    st.extracted["_recs_shown"] = int(st.extracted.get("_recs_shown") or 0) + 1
    _state.write_state(shared_dir, st)

    variant = (
        "inprocess_followup" if pending_kind == "inprocess" else "pitch"
    )
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={"variant": variant},
        ),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def start_google_setup(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Begin (or resume) the ``evo setup-google`` wizard for ``bot_id``.

    The wizard is per-bot under PR #1123: each bot has its own OAuth
    client (typically tied to a different Google Cloud project / org /
    billing path). Operators run ``evo setup-google`` once per bot.

    The flow:

      1. ``GOOGLE_SETUP_INTRO``: pitch the setup for this bot, wait for
         "ready" / "go".
      2. ``GOOGLE_SETUP_ADMIN_URL``: collect the externally-reachable
         base URL for the admin server. Needed before the console step
         because we display the exact redirect URI the operator must
         whitelist. (One URL per machine — bots share this.)
      3. ``GOOGLE_SETUP_CONSOLE``: one-turn render of the Google Cloud
         Console checklist for this bot's OAuth Application. Wait for
         "done".
      4. ``GOOGLE_SETUP_CLIENT_ID``: collect + validate.
      5. ``GOOGLE_SETUP_CLIENT_SECRET``: collect + validate, then
         commit ``client_id`` to ``network.bots[bot_id].googleOAuthClient``
         and ``client_secret`` to ``bot_id``'s ``auth-profiles.json``
         (via :func:`_commit_google_setup`).

    Resumes an in-flight session at whatever phase it was at; otherwise
    starts fresh at INTRO. The session is keyed on (bot_id, user_key)
    like every other wizard chain.
    """
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active() and existing.audience == "google_setup":
        st = existing
    else:
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="google_setup",
            initial_phase=_phases.PHASE_GOOGLE_SETUP_INTRO,
        )
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def start_app_create(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Begin (or resume) an ``evo app create`` session for ``(bot_id,
    user_key)``. Wave 3.

    The flow:

      1. ``APP_CREATE_DESCRIBE``: ask the user to describe the app.
      2. On their reply: stash description, call ``_build_draft``,
         advance to ``APP_CREATE_REVIEW``.
      3. ``APP_CREATE_REVIEW``: render the draft, route the user's
         next reply (approve / iterate <feedback> / cancel) via
         :func:`_handle_app_create_review`.

    Resumes an in-flight session at whatever phase it was at; otherwise
    starts fresh at DESCRIBE. The session is keyed on (bot_id, user_key)
    just like every other wizard chain.
    """
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active() and existing.audience == "app_creator":
        st = existing
    else:
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="app_creator",
            initial_phase=_phases.PHASE_APP_CREATE_DESCRIBE,
        )
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def start_add_bot(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Begin (or resume) an ``evo add-bot`` session for ``(bot_id,
    user_key)``. Admin-only — the dispatcher gates the subcommand before
    this is ever called.

    The conversational bot-creation wizard (delta spec §9, M1 + M2): the
    chain runs need → audience → purpose → name → plan, then on a
    confirmed plan walks the credential (borrow / paste / skip), builds
    the bot through the provision substrate with progress relayed in
    outcome language, and wraps on a smoke check that never leaves a
    broken bot running. The new bot's ``purpose{}`` block (the
    effectiveness-layer §4 field, ``captured: "declared"``) lands in its
    network.json bot block via the route handler's save_network path —
    the wizard itself never writes privileged files, and the build runs
    in the admin server, never in evo's process.

    Resumes an in-flight session at whatever phase it was at; otherwise
    starts fresh at AB_NEED. Keyed on (bot_id, user_key) like every
    other wizard chain.
    """
    existing = _state.read_state(shared_dir, bot_id, user_key)
    if existing and existing.is_active() and existing.audience == "add_bot":
        st = existing
    else:
        st = _state.initialize(
            shared_dir,
            bot_id=bot_id,
            user_key=user_key,
            audience="add_bot",
            initial_phase=_phases.PHASE_AB_NEED,
        )
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def process_turn(
    shared_dir: Path,
    *,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None = None,
) -> TurnResult | None:
    """Process one mid-wizard user message. Returns ``None`` if no active
    wizard exists for ``(bot_id, user_key)`` — the route handler maps
    that to a 404 so the plugin can clear its session state.

    ``network`` is required when the current phase is CHALLENGE (the
    challenge handler needs it to read passphrases and apply claims).
    For other phases it's unused. The route handler always passes it in;
    the parameter is optional for tests that exercise non-challenge
    phases without setting up a full network dict."""
    st = _state.read_state(shared_dir, bot_id, user_key)
    if st is None or not st.is_active():
        return None

    # Slice 5b8: rec_pending sessions auto-expire after
    # ``conversational_approval.pending_expiry_minutes`` of inactivity.
    # The rec stays in the better-engine queue; the user can re-enter
    # via ``evo``. We only expire approver-audience sessions — primary
    # / guide_drafter wizards are explicitly long-lived multi-turn
    # flows that shouldn't drop on the floor for sitting idle. Default
    # is 60 minutes — see ``conversational_approval.pending_expiry_minutes``
    # in ``better_engine_config`` for the rationale.
    if st.audience == "approver" and _rec_pending_expired(shared_dir, st, bot_id):
        st.current_phase = _phases.PHASE_REC_PENDING
        _state.mark_completed(shared_dir, st)
        return None  # plugin clears its session; bot resumes normally

    phase = _phases.get_phase(st.current_phase)
    if phase is None:
        # Defensive: state references a phase the engine doesn't know
        # (e.g. partial deploy mid-rollout). Restart at initial phase.
        st.current_phase = _phases.initial_phase(st.audience)
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=st.current_phase,
            completed=False,
        )

    # CHALLENGE has its own deterministic flow (passphrase match +
    # in-band claims) — split out to keep process_turn readable.
    if phase.name == _phases.PHASE_CHALLENGE:
        return _handle_challenge(shared_dir, st, bot_id, user_key, user_message, network)

    # GUIDE_CONFIRM is a deterministic save/cancel/edit gate — no LLM
    # extractor, just keyword matching on the user's reply.
    if phase.name == _phases.PHASE_GUIDE_CONFIRM:
        return _handle_guide_confirm(shared_dir, st, bot_id, user_key, user_message)

    # GALLERY_RECS classifies the user's reply against the candidates
    # we surfaced — accept (one or more) / dismiss-all / ambiguous.
    if phase.name == _phases.PHASE_GALLERY_RECS:
        return _handle_gallery_recs(shared_dir, st, bot_id, user_key, user_message)

    # FORGE_INTRO opt-in / opt-out gate.
    if phase.name == _phases.PHASE_FORGE_INTRO:
        return _handle_forge_intro(shared_dir, st, bot_id, user_key, user_message)

    # FORGE_CONFIRM build / edit / cancel gate (FORGE_DESIGN itself
    # uses the standard extractor flow plus a preview-keyword force-advance).
    if phase.name == _phases.PHASE_FORGE_CONFIRM:
        return _handle_forge_confirm(shared_dir, st, bot_id, user_key, user_message)

    # REC_PENDING (5b8) — Better Engine recommendation awaiting a yes/no/snooze/
    # next/context decision. The handler owns its own state transitions:
    # records feedback, chains to the next rec, or finalizes.
    if phase.name == _phases.PHASE_REC_PENDING:
        return _handle_rec_pending(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # APP_PROMOTE_CONFIRM (AL-1.7) — a rename was proposed and echoed back;
    # this turn is the user confirming, re-renaming, or backing out. Without
    # this branch the phase has no handler, ``_always_exit`` fires and the turn
    # falls through to the PRIMARY-chain finaliser — which commits a user
    # profile and ends the session while the proposal sits untouched. That was
    # the state an independent review caught on #3734.
    if phase.name == _phases.PHASE_APP_PROMOTE_CONFIRM:
        return _handle_promote_confirm(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # APP_CREATE chain (Wave 3) — `evo app create`. Both phases own
    # their own state transitions: DESCRIBE drafts the spec then advances
    # to REVIEW; REVIEW classifies approve/iterate/cancel.
    if phase.name == _phases.PHASE_APP_CREATE_DESCRIBE:
        return _handle_app_create_describe(
            shared_dir, st, bot_id, user_key, user_message,
        )
    if phase.name == _phases.PHASE_APP_CREATE_REVIEW:
        return _handle_app_create_review(
            shared_dir, st, bot_id, user_key, user_message,
        )

    # AB_PLAN (add-bot chain) — verbatim plan echo awaiting confirm /
    # cancel / change-feedback. The handler owns its own state
    # transitions and the purpose{} write on confirm (M2: confirm then
    # advances into the credential walk).
    if phase.name == _phases.PHASE_AB_PLAN:
        return _handle_add_bot_plan(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # Add-bot M3 phases — starter-app proposal + briefing default. Both
    # engine-driven: the proposal universe is a fixed list (name matching
    # beats LLM extraction) and the briefing is a yes/no.
    if phase.name == _phases.PHASE_AB_SHAPE:
        return _handle_add_bot_shape(
            shared_dir, st, bot_id, user_key, user_message, network,
        )
    if phase.name == _phases.PHASE_AB_BRIEFING:
        return _handle_add_bot_briefing(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # Add-bot channel offer (offer-now / auto-activate-later) — engine-
    # driven: a pasted channel token must never pass through LLM
    # extraction, same rule as the API key below.
    if phase.name == _phases.PHASE_AB_CHANNEL:
        return _handle_add_bot_channel(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # Add-bot M2 phases — credential walk, build progress, smoke wrap.
    # All three are engine-driven (deterministic reply handling; a
    # pasted API key must never pass through LLM extraction).
    if phase.name == _phases.PHASE_AB_CREDENTIALS:
        return _handle_add_bot_credentials(
            shared_dir, st, bot_id, user_key, user_message, network,
        )
    if phase.name == _phases.PHASE_AB_PROVISION:
        return _handle_add_bot_provision(
            shared_dir, st, bot_id, user_key, user_message, network,
        )
    if phase.name == _phases.PHASE_AB_SMOKE_WRAP:
        return _handle_add_bot_smoke_wrap(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # GOOGLE_SETUP chain — `evo setup-google`. Five engine-driven phases;
    # every phase accepts `cancel` to abort cleanly.
    if phase.name == _phases.PHASE_GOOGLE_SETUP_INTRO:
        return _handle_google_setup_intro(
            shared_dir, st, bot_id, user_key, user_message,
        )
    if phase.name == _phases.PHASE_GOOGLE_SETUP_ADMIN_URL:
        return _handle_google_setup_admin_url(
            shared_dir, st, bot_id, user_key, user_message,
        )
    if phase.name == _phases.PHASE_GOOGLE_SETUP_CONSOLE:
        return _handle_google_setup_console(
            shared_dir, st, bot_id, user_key, user_message,
        )
    if phase.name == _phases.PHASE_GOOGLE_SETUP_CLIENT_ID:
        return _handle_google_setup_client_id(
            shared_dir, st, bot_id, user_key, user_message,
        )
    if phase.name == _phases.PHASE_GOOGLE_SETUP_CLIENT_SECRET:
        return _handle_google_setup_client_secret(
            shared_dir, st, bot_id, user_key, user_message, network,
        )

    # 1. Extract fields from the user's message under the current phase's
    #    schema. Best-effort — empty result is fine.
    if phase.has_extractor:
        new_fields = _extractor.extract_fields(
            user_message=user_message,
            targets=phase.targets,
            current_state=st.extracted,
        )
        if new_fields:
            st.extracted.update(
                {k: v for k, v in new_fields.items() if _meaningful(v)}
            )

    # 1.5. GUIDE_GATHER lets the user say "show me" / "preview" to
    # force-advance to the confirm step before all fields are filled —
    # operators often want to save partial guides as a starting point.
    if phase.name == _phases.PHASE_GUIDE_GATHER:
        if _looks_like_preview_request(user_message):
            st.current_phase = _phases.PHASE_GUIDE_CONFIRM
            _state.write_state(shared_dir, st)
            return TurnResult(
                system_append=_build_prompt(shared_dir, st, bot_id),
                wizard_session_id=user_key,
                phase=st.current_phase,
                completed=False,
            )

    # 1.6. FORGE_DESIGN: same preview-keyword shortcut as guide drafting —
    # users can jump to the confirm step before all fields are filled if
    # they want to see the working draft.
    if phase.name == _phases.PHASE_FORGE_DESIGN:
        if _looks_like_preview_request(user_message):
            st.current_phase = _phases.PHASE_FORGE_CONFIRM
            _state.write_state(shared_dir, st)
            return TurnResult(
                system_append=_build_prompt(shared_dir, st, bot_id),
                wizard_session_id=user_key,
                phase=st.current_phase,
                completed=False,
            )

    # 2. Advance phase if exit condition is met. When we advance INTO a
    #    terminal phase (next_phase is None), finalize immediately so the
    #    bot renders the wrap message AND the wizard ends in a single
    #    turn — otherwise the user would see the same summary twice.
    if phase.exit_condition(st.extracted):
        if phase.next_phase is None:
            # Terminal phase fired its own exit — finalize.
            return _finalize(shared_dir, st, bot_id, user_key)
        st.current_phase = phase.next_phase

        # If the new phase is GALLERY_RECS but we have no candidates to
        # show (gallery sparse / everything already installed), skip
        # past it rather than burning a turn on a "no recommendations"
        # prompt. Land at FORGE_INTRO if the forge trigger fires (goals
        # present, no apps accepted), else WRAP. We have to check the
        # forge trigger here too — _advance_from_gallery isn't called
        # when we skip, but the same opt-in opportunity should apply.
        if st.current_phase == _phases.PHASE_GALLERY_RECS:
            try:
                from . import gallery_recs as _gr
                candidates = _gr.load_candidates(
                    shared_dir, bot_id, st.extracted, top_k=3,
                )
            except Exception:
                candidates = []
            if not candidates:
                if _should_offer_forge(st.extracted):
                    st.current_phase = _phases.PHASE_FORGE_INTRO
                else:
                    st.current_phase = _phases.PHASE_WRAP

        # Entering the add-bot starter-apps phase: load the role's pack
        # from the gallery (seam-injected in tests) so the proposal —
        # apps, what's missing, cost/time preview — is pinned in state
        # before the first render. Honest no-pack roles get the reason
        # stored instead.
        if st.current_phase == _phases.PHASE_AB_SHAPE:
            _ab_seed_pack_proposal(shared_dir, st, network)

        new_phase = _phases.get_phase(st.current_phase)
        # Don't auto-finalize when the next phase is GUIDE_CONFIRM,
        # FORGE_CONFIRM, or AB_PLAN — those are interactive (gate on
        # user input) and must render their own prompt before any
        # decision is made.
        _interactive_terminal = {
            _phases.PHASE_GUIDE_CONFIRM,
            _phases.PHASE_FORGE_CONFIRM,
            _phases.PHASE_AB_PLAN,
        }
        if new_phase is None or (
            new_phase.next_phase is None
            and new_phase.name not in _interactive_terminal
        ):
            # We just stepped into a terminal phase; close out now so
            # the bot's reply on this same turn is the wrap message.
            return _finalize(shared_dir, st, bot_id, user_key)

    _state.write_state(shared_dir, st)

    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Challenge phase handler
# ─────────────────────────────────────────────────────────────────────────────


_DECLINE_KEYWORDS = {"skip", "no", "nope", "pass", "later", "cancel", "stop"}

# 5b4: tour-request keywords. When a CHALLENGE caller types one of these
# instead of a passphrase or decline, we read it as "I'm not the primary
# but I want to learn how this bot is used" and route them into the
# secondary chain rather than ending the wizard.
_TOUR_KEYWORDS = {
    "tour", "use", "show", "show me", "how", "how do i", "help", "info",
    "guide", "introduce", "intro", "explain",
}


def _looks_like_tour_request(text: str, tokens: list[str]) -> bool:
    """Match tour intent against the user's reply. Word-token match for
    the single-word keywords; substring check for the multi-word ones
    so 'show me' / 'how do i use this' both land."""
    token_set = set(tokens)
    for kw in _TOUR_KEYWORDS:
        if " " not in kw:
            if kw in token_set:
                return True
        else:
            if kw in text:
                return True
    return False


def _handle_challenge(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Handle the user's reply to the passphrase challenge.

    Three outcomes:
      * One or both passphrases match → apply claims, advance to GREET,
        return greet prompt (with claim acknowledgment).
      * User's message is a recognized decline ("skip", "no", etc.) →
        end the wizard with a friendly close-out.
      * Anything else → also end the wizard, with a slightly different
        framing ("didn't recognize that as a passphrase") so the user
        knows nothing was recorded.

    A primary-passphrase match against a bot whose primary is already
    someone else is not a hard error — admin claim (if also matched)
    still proceeds, and we explain the conflict.
    """
    if network is None:
        # Defensive: shouldn't happen — the route always passes network.
        # End the challenge gracefully so the user isn't stuck.
        return _close_challenge_no_claim(
            shared_dir, st, user_key, gave_attempt=False,
        )

    text = (user_message or "").strip().lower()
    if not text:
        return _close_challenge_no_claim(
            shared_dir, st, user_key, gave_attempt=False,
        )

    # Explicit decline — short-circuit before passphrase matching so users
    # who happened to use "no" as their passphrase aren't accidentally
    # claimed. (If anyone configures their passphrase as "skip" they get
    # what they deserve.)
    tokens = text.split()
    if any(t in _DECLINE_KEYWORDS for t in tokens):
        return _close_challenge_no_claim(
            shared_dir, st, user_key, gave_attempt=False,
        )

    # Tour-request — the user signaled they want to learn how this bot is
    # used without claiming primary. Switch audience to secondary and
    # route into the bot-guide-driven tour chain. Checked AFTER decline
    # but BEFORE passphrase match so a tour intent doesn't get hijacked
    # by an unfortunate passphrase collision.
    if _looks_like_tour_request(text, tokens):
        return _route_to_secondary_tour(shared_dir, st, bot_id, user_key)

    # Take up to two tokens for passphrase match — same shape as
    # `evo claim` accepts. Extra tokens after that are ignored (the user
    # might have typed "darwin please" — we still match darwin).
    candidates = tokens[:2]
    matched_admin = any(_identity.matches_admin_passphrase(network, t) for t in candidates)
    matched_primary = any(_identity.matches_primary_passphrase(network, t) for t in candidates)

    if not matched_admin and not matched_primary:
        return _close_challenge_no_claim(
            shared_dir, st, user_key, gave_attempt=True,
        )

    # We need (channel, sender_external_id) to apply claims. user_key is
    # "ext:<channel>:<id>" for non-pod-keyed callers (which is the
    # CHALLENGE case — pod-keyed users are admins / known primaries who
    # never enter CHALLENGE). Parse defensively and bail to no-claim if
    # the key shape is unexpected.
    channel, sender_external_id = _parse_user_key(user_key)
    if not channel or not sender_external_id:
        return _close_challenge_no_claim(
            shared_dir, st, user_key, gave_attempt=True,
        )

    network_dirty = False
    claimed_admin = False
    claimed_primary = False
    conflict_reason: str | None = None

    if matched_admin:
        already_admin = _identity.is_admin(network, channel, sender_external_id)
        try:
            _identity.claim_admin(
                network, channel=channel, external_id=sender_external_id,
            )
        except _identity.ClaimError:
            pass  # Validation failure — leave claimed_admin=False
        else:
            if not already_admin:
                claimed_admin = True
                network_dirty = True
                _try_audit(
                    shared_dir,
                    actor=_audit.api_actor(),
                    action="claim_admin",
                    bot_id="<pod>",
                    channel=channel,
                    from_external_id=None,
                    to_external_id=str(sender_external_id),
                )

    if matched_primary:
        # Tolerant reader (M1-B2) — a list-shaped entry indexed raw wrote
        # the literal "['12345']" as the audit "from" value.
        prior = _ext_ids.first_id_for(
            ((network.get("bots") or {}).get(bot_id) or {}).get("primary_user"),
            channel,
        )
        try:
            _identity.claim_primary(
                network, bot_id, channel=channel,
                external_id=sender_external_id,
            )
        except _identity.ClaimError as e:
            msg = str(e)
            if "already recorded" in msg:
                conflict_reason = (
                    "the primary passphrase matched, but another user is "
                    "already recorded as primary on this bot — ask your "
                    f"pod admin to reassign via "
                    f"`evolve-admin evo reassign-primary` if that's wrong."
                )
            else:
                conflict_reason = (
                    f"the primary passphrase matched, but the claim was "
                    f"refused: {msg}"
                )
        else:
            new_value = str(sender_external_id)
            if str(prior or "") != new_value:
                claimed_primary = True
                network_dirty = True
                _try_audit(
                    shared_dir,
                    actor=_audit.api_actor(),
                    action="claim",
                    bot_id=bot_id,
                    channel=channel,
                    from_external_id=(str(prior) if prior is not None else None),
                    to_external_id=new_value,
                )

    if not claimed_admin and not claimed_primary:
        # All matched passphrases bounced (e.g. only-primary match against
        # an existing different primary). Close out and surface the
        # reason if we have one.
        return _close_challenge_no_claim(
            shared_dir, st, user_key,
            gave_attempt=True,
            extra_reason=conflict_reason,
        )

    # Successful claim → advance to GREET with an acknowledgment prompt.
    st.current_phase = _phases.PHASE_GREET
    _state.write_state(shared_dir, st)

    return TurnResult(
        system_append=_prompts.build_challenge_outcome(
            claimed_admin=claimed_admin,
            claimed_primary=claimed_primary,
            conflict_reason=conflict_reason,
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GREET,
        completed=False,
        network_dirty=network_dirty,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gallery recommendations handler (slice 5b5)
# ─────────────────────────────────────────────────────────────────────────────


def _handle_gallery_recs(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Process the user's reply during GALLERY_RECS.

    Behavior:
      * dismiss-all → record dismissed pkg_ids, advance to WRAP and
        finalize same turn (terminal).
      * accept → record accepted pkg_ids (and dismissed counterparts
        from the same prompt), advance + finalize.
      * ambiguous → re-render the gallery prompt with the same
        candidates so the user can take another shot.

    On accept, we additionally resolve pkg_ids → display names and
    store them under ``apps_accepted`` (display names) so the WRAP
    prompt reads naturally without needing to look up packages itself.
    """
    from . import gallery_recs as _gr

    # Re-load candidates: cheap, and lets the classifier match against
    # the same set the prompt rendered.
    candidates = _gr.load_candidates(
        shared_dir, bot_id, st.extracted, top_k=3,
    )

    if not candidates:
        # Nothing to recommend → just advance to wrap.
        return _advance_from_gallery(shared_dir, st, bot_id, user_key)

    classification = _gr.classify_reply(user_message, candidates)
    intent = classification["intent"]

    if intent == "ambiguous":
        # Stay in GALLERY_RECS, re-render so the bot can re-ask.
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"gallery_candidates": candidates},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_GALLERY_RECS,
            completed=False,
        )

    accepted_ids = list(classification.get("accepted") or [])
    dismissed_ids = list(classification.get("dismissed") or [])

    # Look up display names for the accepted pkgs so WRAP can speak
    # about them naturally.
    # identity: see resolve_app_id — gallery catalog rows keyed by their own primary key; display_name-or-key is a LABEL fallback.
    name_by_id = {c.get("pkg_id"): (c.get("display_name") or c.get("pkg_id"))
                  for c in candidates if c.get("pkg_id")}
    accepted_names = [name_by_id[pid] for pid in accepted_ids if pid in name_by_id]

    # Merge into state.extracted (don't clobber prior runs)
    prior_accepted = st.extracted.get("apps_accepted") or []
    prior_dismissed = st.extracted.get("apps_dismissed") or []
    st.extracted["apps_accepted"] = list(_unique_preserve_order(
        list(prior_accepted) + accepted_names,
    ))
    st.extracted["apps_dismissed"] = list(_unique_preserve_order(
        list(prior_dismissed) + dismissed_ids,
    ))

    return _advance_from_gallery(shared_dir, st, bot_id, user_key)


def _advance_from_gallery(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Advance from GALLERY_RECS — into FORGE_INTRO when goals exist
    but no apps were accepted (the spec's §4.2 phase 7 trigger), or
    straight into WRAP and finalize same turn otherwise."""
    if _should_offer_forge(st.extracted):
        st.current_phase = _phases.PHASE_FORGE_INTRO
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_FORGE_INTRO,
            completed=False,
        )
    st.current_phase = _phases.PHASE_WRAP
    return _finalize(shared_dir, st, bot_id, user_key)


# ─────────────────────────────────────────────────────────────────────────────
# Conversational approval handler (slice 5b8)
# ─────────────────────────────────────────────────────────────────────────────


# After this many consecutive unknown / off-topic replies, we finalize
# the session without recording any action — the user has clearly moved
# on and the bot should answer normally next turn. The pending rec
# stays in the better-engine queue, so the user can come back to it via
# ``evo``. Hard-coded (not in config) — flipping this carries surprising
# UX implications and shouldn't be operator-tunable in v1.
_MAX_UNKNOWN_STREAK = 1


def _handle_rec_pending(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Process a user reply during the conversational-approval phase.

    Routing:
      * ``accept`` / ``reject`` / ``next`` → record via Better Engine,
        chain to next rec on the same surface, or finalize when empty.
      * ``snooze`` → record via Better Engine snooze (the engine owns
        the escalation schedule; ``snooze_hint_days`` from stage 2 is
        captured on state.extracted for future use but doesn't override
        the stored schedule today).
      * ``context`` → re-render with the context variant; no state
        transition.
      * ``unknown`` → first time, re-render with the clarify variant;
        if it happens again, finalize without action and let the bot
        resume normal conversation. The rec stays pending in the queue.
    """
    rec = st.extracted.get("_pending_rec") or {}
    rec_id = rec.get("id") if isinstance(rec, dict) else None
    surface = st.extracted.get("_initial_surface") or "member_bot"
    scope_id = st.extracted.get("_initial_scope_id") or bot_id
    pending_kind = st.extracted.get("_pending_kind") or "inbox"

    cfg = _config.resolve(shared_dir, bot_id)

    # ``allow_complete`` is gated on the in-process surface so an
    # accidental "I'm done" reply on an inbox pitch doesn't fire a
    # wrong-shaped transition.
    allow_complete = pending_kind == "inprocess"

    # ``allow_refine`` is gated to inbox proposals — refining an
    # in-process proposal feels wrong (the user already accepted it,
    # they should mark complete or dismiss, not iterate). The rec must
    # also be proposal-derived (carry a proposal_id) for refine to do
    # anything; non-proposal recs (onboarding, scoreboard, etc.) have
    # nothing to refine.
    proposal_id_on_rec: str | None = None
    try:
        from .. import arbiter_bridge as _bridge

        proposal_id_on_rec = _bridge.proposal_id_for_rec(rec)
    except Exception:
        proposal_id_on_rec = None
    allow_refine = pending_kind == "inbox" and bool(proposal_id_on_rec)

    # AL-1.7 — promotion offers carry two answers ``parse_intent`` has no
    # vocabulary for. Design §7.2 puts the offer on THIS flow ("via the existing
    # conversational-approval flow (PHASE_REC_PENDING, approver audience)"), so
    # the hop is here rather than in a parallel handler. Everything that is not
    # a "never" or a rename falls straight through to parse_intent below — which
    # is every plain yes and no, i.e. the common case.
    from . import promote_handlers as _promote

    promote_route = _promote.promote_route_for_rec(rec, user_message or "")
    if promote_route.phase:
        return _handle_promotion_reply(
            shared_dir, st, bot_id, user_key, network,
            rec=rec if isinstance(rec, dict) else {},
            route=promote_route,
        )

    pitch_summary = _summarize_pitch_for_intent(rec)
    result = _intent.parse_intent(
        user_message or "",
        pitch_summary=pitch_summary,
        pending_rec_exists=bool(rec_id),
        allow_context=bool((rec.get("context") or "").strip()) if isinstance(rec, dict) else False,
        allow_complete=allow_complete,
        allow_refine=allow_refine,
        llm_enabled=cfg.llm_intent_parse_enabled,
    )

    # Unknown / low-confidence replies → clarify once; finalize on the
    # next miss so the bot doesn't get stuck in a clarify loop.
    if (
        result.action == "unknown"
        or result.confidence < cfg.confidence_threshold
    ):
        streak = int(st.extracted.get("_unknown_streak") or 0) + 1
        if streak > _MAX_UNKNOWN_STREAK:
            # User has wandered off the proposal; finalize with no action
            # taken and let the next message land on normal bot logic.
            return _finalize_rec_pending(
                shared_dir, st, bot_id, user_key,
                variant="all_caught_up",
            )
        st.extracted["_unknown_streak"] = streak
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={
                    "variant": "clarify",
                    "best_guess_action": (
                        result.action if result.action != "unknown" else None
                    ),
                },
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_REC_PENDING,
            completed=False,
        )

    # Reset the unknown streak — we got a real action.
    if "_unknown_streak" in st.extracted:
        st.extracted.pop("_unknown_streak", None)

    if result.action == "context":
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"variant": "context"},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_REC_PENDING,
            completed=False,
        )

    if result.action == "refine":
        # The user gave substantive feedback on the proposal's wording.
        # Dispatch to the bridge → arbiter.refine → bot's LLM rewrites
        # the proposal in place. Then refresh state with the revised
        # rec and re-pitch so the user sees the iteration before
        # deciding accept/reject. Refine doesn't move the lifecycle —
        # the proposal stays in pending; only its prose changes.
        if not proposal_id_on_rec:
            # Should not be reachable since allow_refine guards on this,
            # but defensively fall through to clarification.
            return TurnResult(
                system_append=_build_prompt(
                    shared_dir, st, bot_id,
                    extra_ctx={"variant": "clarify"},
                ),
                wizard_session_id=user_key,
                phase=_phases.PHASE_REC_PENDING,
                completed=False,
            )
        try:
            from .. import arbiter_bridge as _bridge

            br = _bridge.refine_proposal(
                shared_dir,
                proposal_id_on_rec,
                user_message or "",
                network=network or {},
            )
        except Exception as e:
            import sys
            print(
                f"[evo.wizard.engine] WARNING: refine call failed: {e}",
                file=sys.stderr,
            )
            br = None

        if br is None or not br.ok or not br.rec_dict:
            # Refine failed (LLM error / API key / etc.). Tell the user
            # we couldn't iterate and re-pitch the original; they can
            # accept / reject / try a different phrasing.
            return TurnResult(
                system_append=_build_prompt(
                    shared_dir, st, bot_id,
                    extra_ctx={"variant": "clarify"},
                ),
                wizard_session_id=user_key,
                phase=_phases.PHASE_REC_PENDING,
                completed=False,
            )

        # Refresh state with the revised rec and re-pitch.
        st.extracted["_pending_rec"] = _enrich_with_lineage(
            shared_dir, dict(br.rec_dict)
        )
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"variant": "pitch"},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_REC_PENDING,
            completed=False,
        )

    # accept / reject / snooze / next all touch the Better Engine. We
    # require a rec_id to record feedback; if it's missing, finalize
    # cleanly so the user isn't left hanging.
    if not rec_id:
        return _finalize_rec_pending(
            shared_dir, st, bot_id, user_key,
            variant="all_caught_up",
        )

    # Snooze duration: a stage-2 LLM-extracted hint overrides the
    # engine's escalation schedule; absent that, snooze stays on the
    # escalation schedule (1, 2, 4, 7 days). The config
    # ``default_snooze_days`` is reserved for future surfaces (e.g. an
    # explicit "snooze for the default period" UI button) and isn't
    # applied here so repeated snoozes still push out further when the
    # user keeps deferring without naming a duration.
    snooze_hint = result.snooze_hint_days  # None unless stage 2 extracted
    _record_rec_action(
        shared_dir,
        rec=rec if isinstance(rec, dict) else {"id": str(rec_id)},
        action=result.action,
        snooze_days_hint=snooze_hint,
        network=network,
    )

    # AL-1.7 — a promotion snooze must also land ON THE DRAFT, not only on the
    # proposal. ``app_promotion.evaluate_offer`` reads
    # ``promotion_snoozed_until`` as check 2 of 5, and until this call existed
    # NOTHING in the repo wrote it: a user who said "not for 30 days" was
    # re-offered 25 hours later, because the only other brake is the 24h
    # cadence cap. Round 5 of the #3734 review found it and named the class —
    # **a consumer with no producer**, which no mutation can catch, because
    # every test constructed the field by hand.
    if result.action == "snooze" and _promote.is_promotion_rec(rec):
        _apply_promotion_snooze(
            shared_dir, bot_id, rec if isinstance(rec, dict) else {},
            days=snooze_hint,
        )

    # Audit the action so identity-change-style auditing also covers
    # proposal approvals. Permissive — audit failures don't block the
    # user's session.
    channel, sender_external_id = _parse_user_key(user_key)
    _try_audit(
        shared_dir,
        actor=_audit.api_actor(),
        action=f"rec_{result.action}",
        bot_id=bot_id,
        channel=channel or "<n/a>",
        from_external_id=None,
        to_external_id=sender_external_id or "<unknown>",
        reason=str(rec_id),
    )

    # Try to chain to the next rec so the user can zip through the
    # queue without having to type ``evo`` again. Two sources to check:
    #
    #   1. If the current rec was an in-process follow-up, look for
    #      another in-process proposal for this bot before falling
    #      through to the inbox. Keeps the user focused on closing out
    #      the in-process queue first.
    #   2. Otherwise (inbox surface, or in-process queue exhausted):
    #      ask BetterEngine for the next member_bot rec.
    #
    # Empty everywhere → finalize with "all caught up".
    next_rec_dict: dict[str, Any] | None = None
    next_kind = "inbox"

    if pending_kind == "inprocess":
        try:
            from .. import arbiter_bridge as _bridge
            from ...better_engine.proposal_reader import (
                proposal_to_recommendation,
            )

            remaining = _bridge.list_in_process_for_bot(shared_dir, bot_id)
            for proposal in remaining:
                if proposal.id == rec_id:
                    continue  # the one we just acted on
                ip_rec = proposal_to_recommendation(proposal)
                if ip_rec is None:
                    continue
                next_rec_dict = ip_rec.to_dict()
                next_kind = "inprocess"
                break
        except Exception:
            next_rec_dict = None

    if next_rec_dict is None:
        next_rec = _fetch_next_rec(
            shared_dir, network,
            surface=str(surface),
            scope_id=str(scope_id),
            after_rec_id=str(rec_id),
        )
        if next_rec:
            next_rec_dict = dict(next_rec)
            next_kind = "inbox"

    if not next_rec_dict:
        return _finalize_rec_pending(
            shared_dir, st, bot_id, user_key,
            variant="all_caught_up",
        )

    st.extracted["_pending_rec"] = _enrich_with_lineage(
        shared_dir, dict(next_rec_dict)
    )
    st.extracted["_pending_kind"] = next_kind
    st.extracted["_recs_shown"] = int(st.extracted.get("_recs_shown") or 0) + 1
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={
                "variant": (
                    "inprocess_followup" if next_kind == "inprocess" else "pitch"
                )
            },
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_REC_PENDING,
        completed=False,
    )


def _handle_promotion_reply(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    network: dict[str, Any] | None,
    *,
    rec: dict,
    route: "Any",
) -> TurnResult:
    """Route AL-1.7's two extra answers: "never" and a rename.

    Design ``design-app-spec-and-discovery-2026-08-15.md`` §7.2. Both write
    through the SAME server-side surfaces the rest of the flow uses —
    ``_record_rec_action`` for the proposal lifecycle, the manifest write for
    the shield — so the bot's LLM still holds no privileged tool.

    * **never** → the draft gets ``do_not_offer`` AND the proposal is rejected.

      The shield's durability is NOT free and was NOT free when this was first
      written: ``native_write.mint_scanner_detection`` rebuilds the Instance
      from scratch on a re-mint, and until AL-1.7 added the field to its
      carry-forward allowlist the shield was lost on the very next
      re-discovery. That was asserted here as fact for three review rounds
      without being run; a round-4 independent review measured it. It holds on
      the **v7-arc** path now, and see ``promote_handlers.apply_never_shield``
      for the legacy path's status.

      Both the shield and the rejection, deliberately:
      rejecting alone leaves the draft eligible and the next scan re-offers it,
      which is exactly the "'never' is honored" mitigation failing.
    * **rename** → the proposed name is parked on state and the session moves to
      ``PHASE_APP_PROMOTE_CONFIRM``, which echoes it back. The id is slugged
      from the name (design §7.2), so a mis-heard name would become a durable
      identity that §7.3a's ADOPT rule then protects forever — hence the echo.
    """
    from . import promote_handlers as _promote

    action = getattr(route, "action", "")

    if action == "ambiguous":
        # An accept marker AND a never phrase in one message. Nothing
        # irreversible is written and NOTHING is recorded — the session stays on
        # the offer so the next reply decides it. Asking is the whole point:
        # picking either branch silently is what made the earlier revision
        # shield an acceptance permanently.
        st.extracted[_promote.PROMOTE_OFFER_STATE_KEY] = {
            "rec_id": rec.get("id"),
            "answer": "ambiguous",
        }
        # Render on the OFFER phase with its own variant: the generic
        # REC_PENDING clarify text names neither the ambiguity nor the "never"
        # option, so a user who meant never would have to find the words again.
        prior_phase = st.current_phase
        st.current_phase = _phases.PHASE_APP_PROMOTE_OFFER
        message = _build_prompt(
            shared_dir, st, bot_id, extra_ctx={"variant": "ambiguous"},
        )
        # The SESSION stays on REC_PENDING so the next reply lands back on the
        # normal offer routing — only the render came from the offer phase.
        st.current_phase = prior_phase
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=message,
            direct_send_message=message,
            wizard_session_id=user_key,
            phase=_phases.PHASE_REC_PENDING,
            completed=False,
        )

    if action == "never":
        shielded, durable = _apply_promotion_never(shared_dir, bot_id, rec)
        _record_rec_action(
            shared_dir,
            rec=rec or {"id": str(rec.get("id") or "")},
            action="reject",
            snooze_days_hint=None,
            network=network,
        )
        channel, sender_external_id = _parse_user_key(user_key)
        _try_audit(
            shared_dir,
            actor=_audit.api_actor(),
            action="app_promotion_never",
            bot_id=bot_id,
            channel=channel or "<n/a>",
            from_external_id=None,
            to_external_id=sender_external_id or "<unknown>",
            reason=str(rec.get("id") or ""),
        )
        # Terminal, but NOT through ``_finalize_rec_pending``: that helper
        # rewrites ``current_phase`` back to REC_PENDING and renders the generic
        # "all caught up" wrap, which would tell a user who just said "never"
        # exactly nothing about whether they will be asked again. The
        # acknowledgement has to come from this phase's own block.
        for key in (
            "_pending_rec", "_initial_surface", "_initial_scope_id",
            "_recs_shown", "_unknown_streak",
        ):
            st.extracted.pop(key, None)
        st.current_phase = _phases.PHASE_APP_PROMOTE_OFFER
        st.extracted[_promote.PROMOTE_OFFER_STATE_KEY] = {
            "rec_id": rec.get("id"),
            "answer": "never",
            "shield_written": shielded,
            "shield_durable": durable,
        }
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={
                    "variant": "never",
                    "shield_written": shielded,
                    "shield_durable": durable,
                },
            ),
            wizard_session_id=None,  # terminal — plugin clears its session state
            phase=_phases.PHASE_APP_PROMOTE_OFFER,
            completed=True,
        )

    # rename — park the name and move to CONFIRM. Nothing is conferred yet.
    st.current_phase = _phases.PHASE_APP_PROMOTE_CONFIRM
    st.extracted[_promote.PROMOTE_OFFER_STATE_KEY] = {
        "rec_id": rec.get("id"),
        "answer": "rename",
        "proposed_name": getattr(route, "name", ""),
    }
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={
                "variant": "rename",
                "proposed_name": getattr(route, "name", ""),
                **_promotion_identity_ctx(
                    shared_dir, bot_id, rec, getattr(route, "name", "")
                ),
            },
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_APP_PROMOTE_CONFIRM,
        completed=False,
    )


def _apply_promotion_never(
    shared_dir: Path, bot_id: str, rec: dict
) -> "tuple[bool, bool]":
    """Stamp the permanent shield on the draft this offer was about.

    Returns ``(written, durable)``.

    ``durable`` is the half that exists because of round 4's D1 finding.
    ``native_write`` carries the shield across a re-mint, so it survives on the
    **v7-arc** shape — but the LEGACY write path lives in ``scanner.py``, which
    brief §5 puts out of scope for this chip, and it re-writes a freshly
    generated dict without reading disk. On that shape the shield is still lost
    on the next re-discovery.

    The bot's acknowledgement is rendered off this flag rather than off a
    constant, because telling a legacy-shape user "the marker survives the next
    scan" would be the *exact* defect D1 was: a confident sentence about a layer
    nobody ran. On the operator's pod this is 30 v7-arc to 1 legacy, so the
    common answer is the reassuring one — but it is measured per manifest, not
    assumed.

    Best-effort by design: a failure here must not swallow the user's "never"
    — the proposal rejection still lands, and the caller logs. But it is NOT
    silent: the WARNING names the manifest, because a shield that quietly did
    not stick means the user gets asked again tomorrow having said never, and
    that is the single worst outcome design §10 names.
    """
    # ONE resolver, shared with the snooze path. It had an inline copy here
    # until round 6 pointed out that ``_promotion_manifest_stem``'s docstring
    # claimed it was "shared by the never and snooze paths" while this function
    # kept its own — a false claim about the module's own structure, which is
    # D1's shape at the smallest possible scale.
    stem = _promotion_manifest_stem(shared_dir, bot_id, rec)
    if not stem:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: promotion 'never' on {bot_id} had no "
            f"resolvable manifest_stem — the shield was NOT written and the "
            f"draft may be re-offered",
            file=sys.stderr,
        )
        return (False, False)

    from .promote_handlers import apply_never_shield
    from ...web.server import _read_manifest_as_bot, _write_manifest_as_bot

    manifest = _read_manifest_as_bot(bot_id, stem)
    if manifest is None:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: promotion 'never' on {bot_id}: no "
            f"manifest {stem!r} to shield — it may be re-offered",
            file=sys.stderr,
        )
        return (False, False)
    apply_never_shield(manifest)
    if not _write_manifest_as_bot(bot_id, stem, manifest):
        import sys
        print(
            f"[evo.wizard.engine] WARNING: promotion 'never' on {bot_id}: write "
            f"of the do_not_offer shield to {stem!r} FAILED — it may be re-offered",
            file=sys.stderr,
        )
        return (False, False)
    from ...applications.coherence_actions import _is_v7_arc

    return (True, bool(_is_v7_arc(manifest)))


def _handle_promote_confirm(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """The turn AFTER a rename was echoed back (design §7.2).

    Three ways out, and the rename is only APPLIED on the first:

    * **confirm** → the new name is written to the manifest, the identity
      decision is re-run against it (:func:`app_promotion.resolve_rename` — under
      §7.3a's ADOPT a draft that already resolves KEEPS its id, so a rename
      changes the label and not the identity), the proposal's action is updated
      to match, and the approval is recorded through the same
      ``_record_rec_action`` every other answer uses.
    * **another rename** → re-echo. The user is still choosing.
    * **anything else** → treated as backing out: nothing is applied and the
      proposal stays pending. Deliberately NOT a reject — a user who wandered
      off mid-rename has not said no, and recording one would be putting words
      in their mouth.
    """
    from . import promote_handlers as _promote

    pending = st.extracted.get(_promote.PROMOTE_OFFER_STATE_KEY) or {}
    pending = pending if isinstance(pending, dict) else {}
    rec = st.extracted.get("_pending_rec") or {}
    rec = rec if isinstance(rec, dict) else {}
    proposed_name = str(pending.get("proposed_name") or "").strip()

    # A further rename in this turn replaces the parked name and re-echoes.
    reply = _promote.classify_promote_reply(user_message or "")
    if reply.action == "rename" and reply.name and reply.name != proposed_name:
        pending["proposed_name"] = reply.name
        st.extracted[_promote.PROMOTE_OFFER_STATE_KEY] = pending
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={
                    "variant": "rename",
                    "proposed_name": reply.name,
                    **_promotion_identity_ctx(shared_dir, bot_id, rec, reply.name),
                },
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_APP_PROMOTE_CONFIRM,
            completed=False,
        )

    cfg = _config.resolve(shared_dir, bot_id)
    result = _intent.parse_intent(
        user_message or "",
        pitch_summary=proposed_name,
        pending_rec_exists=True,
        allow_context=False,
        llm_enabled=cfg.llm_intent_parse_enabled,
    )
    confirmed = (
        result.action == "accept" and result.confidence >= cfg.confidence_threshold
    )
    if not confirmed:
        # Backed out. Nothing applied; the proposal stays pending so the offer
        # can still be answered later.
        st.current_phase = _phases.PHASE_APP_PROMOTE_CONFIRM
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"variant": "rename_cancelled", "proposed_name": proposed_name},
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_APP_PROMOTE_CONFIRM,
            completed=True,
        )

    applied = _apply_promotion_rename(shared_dir, bot_id, rec, proposed_name)
    _record_rec_action(
        shared_dir,
        rec=rec or {"id": str(rec.get("id") or "")},
        action="accept",
        snooze_days_hint=None,
        network=network,
    )
    channel, sender_external_id = _parse_user_key(user_key)
    _try_audit(
        shared_dir,
        actor=_audit.api_actor(),
        action="app_promotion_rename_accept",
        bot_id=bot_id,
        channel=channel or "<n/a>",
        from_external_id=None,
        to_external_id=sender_external_id or "<unknown>",
        reason=f"{rec.get('id') or ''}:{proposed_name}",
    )
    st.current_phase = _phases.PHASE_APP_PROMOTE_CONFIRM
    _state.mark_completed(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={
                "variant": "rename_applied",
                "proposed_name": proposed_name,
                "app_id": applied,
            },
        ),
        wizard_session_id=None,
        phase=_phases.PHASE_APP_PROMOTE_CONFIRM,
        completed=True,
    )


def _find_promotion_proposal(
    shared_dir: Path, proposal_id: str
) -> "tuple[Any, Literal['pending', 'snoozed']]":
    """The live proposal with this id, and the subdir it is in.

    ``(None, "pending")`` when nothing matches. Only ``pending`` and
    ``snoozed`` are searched: an offer that has been applied or archived is
    answered, and re-pointing it would rewrite a decision already taken.
    """
    from arbiter import store as _astore

    subdirs: "tuple[Literal['pending', 'snoozed'], ...]" = ("pending", "snoozed")
    for subdir in subdirs:
        for proposal in _astore.iter_proposals(shared_dir, subdirs=(subdir,)):
            if proposal.id == proposal_id:
                return proposal, subdir
    return None, "pending"


def _pod_defined_apps(shared_dir: Path) -> "dict[str, dict]":
    """The pod's DEFINED apps as grouping facts (ALPHA-3b). ``{}`` on any failure.

    One reader for the wizard, delegating to the sweep's — the offer and the
    rename must ask the same question of the same pod, or the id a user is
    shown at the offer is not the id their rename lands on.
    """
    try:
        from ...applications.app_promotion_sweep import pod_defined_app_facts
        from ...config import load_network

        return pod_defined_app_facts(shared_dir, network=load_network() or {})
    except Exception as e:  # noqa: BLE001 — reported, never swallowed
        import sys

        print(
            f"[evo.wizard.engine] WARNING: could not read the pod's defined apps "
            f"({e}) — promotion identity falls back to conferring a new id",
            file=sys.stderr,
        )
        return {}


def _promotion_identity_ctx(
    shared_dir: Path, bot_id: str, rec: dict, new_name: str
) -> "dict[str, Any]":
    """What the identity WOULD be if the user confirms ``new_name``.

    The rename echo tells the user *"the name becomes its permanent id"*, and
    since ALPHA-3b that sentence is only true on the conferral branch: a draft
    of an app a sibling bot already has ADOPTS that app's id, and the name is
    then a label. So the echo resolves the prospective decision — the same pure
    :func:`app_promotion.resolve_rename` the confirm hop will run, against the
    same pod facts — rather than asserting the conferral case.

    Resolved with the NEW name, not the offer's recorded mode: renaming away
    from the sibling's name is exactly what ENDS an adoption, and an echo built
    on the old mode would promise an identity the confirm hop then would not
    produce.

    Best-effort: ``{}`` on anything unreadable, which renders today's text.
    """
    try:
        from ...applications import app_promotion as _ap
        from ...web.server import _read_manifest_as_bot

        source_ref = rec.get("source_ref") if isinstance(rec, dict) else None
        proposal_id = (
            (source_ref or {}).get("proposal_id")
            if isinstance(source_ref, dict)
            else None
        )
        if not proposal_id or not new_name:
            return {}
        target, _subdir = _find_promotion_proposal(shared_dir, proposal_id)
        stem = getattr(getattr(target, "action", None), "manifest_stem", "") or ""
        if not stem:
            return {}
        manifest = _read_manifest_as_bot(bot_id, stem)
        if manifest is None:
            return {}
        outcome = _ap.resolve_rename(
            manifest,
            new_name=new_name,
            current_app_id=getattr(target.action, "app_id", "") or "",
            bot_id=bot_id,
            defined_apps=_pod_defined_apps(shared_dir),
        )
        if outcome is None:
            return {}
        return {
            "identity_mode": outcome.identity.mode,
            "adopted_from_bot": outcome.identity.adopted_from_bot,
        }
    except Exception as e:  # noqa: BLE001 — an echo must never fail the turn
        import sys

        # Reported, not swallowed: the fallback render is honest for a
        # conferral and WRONG for an adoption, so a silent {} here would be a
        # confident sentence about a layer that did not run.
        print(
            f"[evo.wizard.engine] WARNING: could not resolve the prospective "
            f"promotion identity for {bot_id} ({e}) — the rename echo falls back "
            f"to the conferral wording",
            file=sys.stderr,
        )
        return {}


def _apply_promotion_rename(
    shared_dir: Path, bot_id: str, rec: dict, new_name: str
) -> str:
    """Write the rename to the manifest and re-point the proposal's action.

    Returns the resulting app id, or "" when nothing could be applied. Failures
    are reported on stderr and never swallowed: a rename the user confirmed and
    the system dropped is the same class of silent loss as a "never" that did
    not stick.
    """
    import sys

    source_ref = rec.get("source_ref") if isinstance(rec, dict) else None
    proposal_id = (
        (source_ref or {}).get("proposal_id") if isinstance(source_ref, dict) else None
    )
    if not proposal_id or not new_name:
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id} had no "
            f"proposal id or no name — NOT applied",
            file=sys.stderr,
        )
        return ""

    from ...applications import app_promotion as _ap
    from ...web.server import _read_manifest_as_bot, _write_manifest_as_bot

    target, subdir = _find_promotion_proposal(shared_dir, proposal_id)
    if target is None:
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id}: proposal "
            f"{proposal_id!r} not found — NOT applied",
            file=sys.stderr,
        )
        return ""

    from schema.proposal import PromoteApp as _PromoteApp

    if not isinstance(target.action, _PromoteApp):
        # A stale rec pointing at a proposal whose action is something else
        # entirely. Narrowing here is not only for the type checker: without it
        # the writes below would set ``app_id`` on an unrelated Action and
        # persist it.
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id}: proposal "
            f"{proposal_id!r} carries a {type(target.action).__name__} action, not "
            f"PromoteApp — NOT applied",
            file=sys.stderr,
        )
        return ""
    action = target.action
    stem = action.manifest_stem or ""
    manifest = _read_manifest_as_bot(bot_id, stem) if stem else None
    if manifest is None:
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id}: no "
            f"manifest {stem!r} — NOT applied",
            file=sys.stderr,
        )
        return ""

    outcome = _ap.resolve_rename(
        manifest,
        new_name=new_name,
        current_app_id=action.app_id,
        # ALPHA-3b: the offer may have ADOPTED a sibling bot's id, and
        # re-running the decision without the pod's facts would confer a fresh
        # slug — the rename would move an id the confirmation turn exists to
        # protect. Same facts on both hops, or the two hops disagree.
        bot_id=bot_id,
        defined_apps=_pod_defined_apps(shared_dir),
    )
    if outcome is None:
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id}: "
            f"{new_name!r} does not resolve to a usable identity — NOT applied",
            file=sys.stderr,
        )
        return ""

    manifest["name"] = outcome.name
    if not _write_manifest_as_bot(bot_id, stem, manifest):
        print(
            f"[evo.wizard.engine] WARNING: promotion rename on {bot_id}: write of "
            f"{stem!r} FAILED — NOT applied",
            file=sys.stderr,
        )
        return ""

    # Re-point the proposal so the applier confers the id the user confirmed,
    # not the one the offer was minted with.
    action.app_id = outcome.identity.app_id
    action.identity_mode = outcome.identity.mode  # type: ignore[assignment]
    target.human_title = f"Make {outcome.name} an app"
    from arbiter import store as _astore

    _astore.write_proposal(target, shared_dir, subdir=subdir)
    return outcome.identity.app_id


#: Days a promotion offer is deferred when the user gives no duration. The
#: proposal's own snooze rides ``better_engine``'s escalation schedule (1, 2, 4,
#: 7); the DRAFT-side shield needs a single number, and 7 is the conservative
#: read of "not now" — long enough that a weekly rhythm is not interrupted,
#: short enough that a genuine yes is not lost for a month.
PROMOTION_DEFAULT_SNOOZE_DAYS = 7

#: Ceiling on a snooze. ``snooze_hint_days`` comes from a STAGE-2 LLM
#: extraction of free text, so it is untrusted input: "put it off till the heat
#: death of the universe" can arrive as any integer. A year is past every
#: legitimate "not now" and short of "never" — and "never" has its own answer,
#: which is reversible only through an operator today (§8.3 step 6), so a
#: snooze must not become one by arithmetic.
PROMOTION_MAX_SNOOZE_DAYS = 365


def _apply_promotion_snooze(
    shared_dir: Path, bot_id: str, rec: dict, *, days: int | None
) -> bool:
    """Stamp ``promotion_snoozed_until`` on the draft this offer was about.

    The producer for ``app_promotion.SNOOZE_UNTIL_FIELD``. Best-effort and
    loudly reported on failure, the same shape as ``_apply_promotion_never`` —
    a snooze that silently did not stick means the user is asked again
    tomorrow having said "not for a month".
    """
    import sys
    from datetime import datetime, timedelta, timezone

    from ...applications.app_promotion import SNOOZE_UNTIL_FIELD

    stem = _promotion_manifest_stem(shared_dir, bot_id, rec)
    if not stem:
        return False
    from ...web.server import _read_manifest_as_bot, _write_manifest_as_bot

    manifest = _read_manifest_as_bot(bot_id, stem)
    if manifest is None:
        print(
            f"[evo.wizard.engine] WARNING: promotion snooze on {bot_id}: no "
            f"manifest {stem!r} — the draft may be re-offered tomorrow",
            file=sys.stderr,
        )
        return False
    # ``isinstance(True, int)`` is True in Python, so a bool must be excluded
    # explicitly — otherwise ``days=True`` silently becomes a ONE-day snooze,
    # which reads as "ask me again tomorrow" from a user who said "later".
    # ``app_readiness`` already guards the same trap on ``invocation_count``.
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        span = PROMOTION_DEFAULT_SNOOZE_DAYS
    else:
        span = min(days, PROMOTION_MAX_SNOOZE_DAYS)
    until = datetime.now(timezone.utc) + timedelta(days=span)
    manifest[SNOOZE_UNTIL_FIELD] = until.isoformat().replace("+00:00", "Z")
    if not _write_manifest_as_bot(bot_id, stem, manifest):
        print(
            f"[evo.wizard.engine] WARNING: promotion snooze on {bot_id}: write "
            f"of {stem!r} FAILED — the draft may be re-offered tomorrow",
            file=sys.stderr,
        )
        return False
    return True


def _promotion_manifest_stem(shared_dir: Path, bot_id: str, rec: dict) -> str:
    """The ``manifest_stem`` on the PromoteApp action behind ``rec``, or "".

    Shared by the never and snooze paths so there is one place that knows how a
    rec resolves to a draft.
    """
    import sys

    source_ref = rec.get("source_ref") if isinstance(rec, dict) else None
    proposal_id = (
        (source_ref or {}).get("proposal_id") if isinstance(source_ref, dict) else None
    )
    if not proposal_id:
        return ""
    try:
        from arbiter import store as _astore

        for subdir in ("pending", "snoozed"):
            for proposal in _astore.iter_proposals(shared_dir, subdirs=(subdir,)):
                if proposal.id == proposal_id:
                    return getattr(proposal.action, "manifest_stem", "") or ""
    except Exception as e:  # noqa: BLE001 — reported, never swallowed
        print(
            f"[evo.wizard.engine] WARNING: could not resolve the manifest for "
            f"promotion proposal {proposal_id!r} on {bot_id}: {e}",
            file=sys.stderr,
        )
    return ""


def _rec_pending_expired(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
) -> bool:
    """Has this rec_pending session been idle long enough that we should
    treat it as expired? Compares ``state.updated_at`` against
    ``conversational_approval.pending_expiry_minutes`` from config.

    Permissive: any error parsing the timestamp / loading config falls
    through to "not expired" so a bad timestamp can't kick a user out
    of a session they're actively using.
    """
    raw = (st.updated_at or "").strip()
    if not raw:
        return False
    try:
        from datetime import datetime, timezone
        # State writes ISO-8601 with a trailing 'Z'; strip and parse UTC.
        cleaned = raw.rstrip("Z")
        last = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except Exception:
        return False

    cfg = _config.resolve(shared_dir, bot_id)
    if cfg.pending_expiry_minutes <= 0:
        return False  # 0 / negative disables expiry

    from datetime import datetime, timedelta, timezone
    age = datetime.now(timezone.utc) - last
    return age > timedelta(minutes=cfg.pending_expiry_minutes)


def _summarize_pitch_for_intent(rec: dict[str, Any]) -> str:
    """Build a compact pitch string for the stage-2 intent prompt — just
    enough for the LLM to decide if a reply maps to the action. Prefer
    member_bot_* variants to match what the user actually saw."""
    if not isinstance(rec, dict):
        return ""
    title = rec.get("member_bot_title") or rec.get("title") or ""
    detail = rec.get("member_bot_detail") or rec.get("detail") or ""
    parts = []
    if title:
        parts.append(str(title).strip())
    if detail:
        parts.append(str(detail).strip())
    return "\n".join(parts)[:600]


def _record_rec_action(
    shared_dir: Path,
    *,
    rec: dict,
    action: str,
    snooze_days_hint: int | None,
    network: dict[str, Any] | None,
) -> None:
    """Apply a follow-up action against the recommendation queue.

    Asymmetric routing (Step 1 of the unify plan):

      * Proposal-derived rec (``source_ref["proposal_id"]`` set) →
        route through ``arbiter_bridge`` so the proposal's lifecycle
        actually advances (accept runs the applier, dismiss writes the
        rejection log + cooldown, snooze sets ``snoozed_until``). ALSO
        call ``BetterEngine.record_feedback`` so the learning model
        still sees the chat-side signal — that's how Bayesian weights
        keep tracking the user's preferences across both surfaces.

      * Non-proposal rec (onboarding / scoreboard / compliance /
        whimsy — no proposal_id) → keep using
        ``BetterEngine.record_feedback`` only. There's no proposal to
        act on.

    Best-effort: on any failure we log and move on rather than wedge
    the chat session.

    Mapping:
      * accept → arbiter accept_proposal + record_feedback("accepted")
      * reject → arbiter dismiss_proposal + record_feedback("rejected")
      * next   → arbiter dismiss_proposal + record_feedback("rejected", reason="ignored")
      * snooze → arbiter snooze_proposal + record_feedback / engine.snooze
                 (``snooze_days_hint`` is honored when set; otherwise
                 default 7 days for the proposal side, escalation
                 schedule for the BetterEngine learning side)
    """
    rec_id = str(rec.get("id") or "") if isinstance(rec, dict) else ""
    if not rec_id:
        # Defensive: no rec id → nothing to record. Caller already
        # gated on this, but keep the helper robust.
        return

    # Step A: route the proposal-side action through the arbiter bridge
    # if this rec carries a proposal_id. Non-proposal recs skip this
    # step entirely.
    proposal_id: str | None = None
    try:
        from .. import arbiter_bridge as _bridge

        proposal_id = _bridge.proposal_id_for_rec(rec)
    except Exception as e:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: bridge import failed: {e}",
            file=sys.stderr,
        )
        proposal_id = None

    if proposal_id:
        try:
            from .. import arbiter_bridge as _bridge

            if action == "accept":
                _bridge.accept_proposal(shared_dir, proposal_id)
            elif action == "complete":
                # User finished the offline work for an in-process
                # proposal. Transitions ``applied → succeeded``. Only
                # valid when the proposal is in applied/ already; the
                # bridge enforces that rule.
                _bridge.complete_proposal(shared_dir, proposal_id)
            elif action in ("reject", "next"):
                reason = "ignored" if action == "next" else ""
                _bridge.dismiss_proposal(
                    shared_dir, proposal_id, reason=reason
                )
            elif action == "snooze":
                _bridge.snooze_proposal(
                    shared_dir, proposal_id, days=snooze_days_hint
                )
        except Exception as e:
            import sys
            print(
                f"[evo.wizard.engine] WARNING: arbiter bridge action "
                f"{action!r} on proposal {proposal_id!r} failed: {e}",
                file=sys.stderr,
            )

    # Step B: ALWAYS record the feedback signal in BetterEngine so the
    # learning model picks up the user's preferences regardless of
    # whether the rec came from a proposal or another adapter.
    try:
        from ...better_engine.engine import BetterEngine
    except Exception as e:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: BetterEngine import failed: {e}",
            file=sys.stderr,
        )
        return

    net = network if isinstance(network, dict) else {}
    try:
        engine = BetterEngine(shared_dir, net)
    except Exception as e:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: BetterEngine ctor failed: {e}",
            file=sys.stderr,
        )
        return

    try:
        if action in ("accept", "complete"):
            # Both accept and complete are positive learning signals.
            # accept = "I want this," complete = "I did this and we're
            # done." For Bayesian weight purposes both reinforce that
            # the user welcomes proposals of this shape.
            engine.record_feedback(rec_id, "accepted")
        elif action == "reject":
            engine.record_feedback(rec_id, "rejected")
        elif action == "next":
            engine.record_feedback(rec_id, "rejected", reason="ignored")
        elif action == "snooze":
            # Pass the days hint so a stage-2 "remind me next week"
            # actually moves snooze_until to +7d. None falls through to
            # the engine's escalation schedule (1, 2, 4, 7 days).
            override = (
                snooze_days_hint
                if snooze_days_hint is not None and snooze_days_hint > 0
                else None
            )
            engine.snooze(rec_id, days_override=override)
        # Unknown action → no-op; caller already handled clarify routing.
    except Exception as e:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: rec action {action!r} on "
            f"{rec_id!r} failed: {e}",
            file=sys.stderr,
        )


def _fetch_next_rec(
    shared_dir: Path,
    network: dict[str, Any] | None,
    *,
    surface: str,
    scope_id: str,
    after_rec_id: str,
) -> dict[str, Any] | None:
    """Return the next pending rec on ``surface`` for ``scope_id``,
    skipping ``after_rec_id``. Returns ``None`` on empty queue or any
    error (the caller will finalize with the all-caught-up wrap)."""
    try:
        from ...better_engine.engine import BetterEngine
    except Exception:
        return None

    net = network if isinstance(network, dict) else {}
    try:
        engine = BetterEngine(shared_dir, net)
    except Exception:
        return None

    try:
        from ...better_engine.storage import load_recommendations
        recs = load_recommendations(engine.recs_path)
        filtered = engine.filter_for_surface(recs, surface, scope_id)
    except Exception:
        return None

    for rec in filtered:
        rid = getattr(rec, "id", None)
        if rid and str(rid) != str(after_rec_id):
            try:
                return rec.to_dict()
            except Exception:
                # If to_dict shape changes, fall through and try the next.
                continue
    return None


def _finalize_rec_pending(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    *,
    variant: str = "all_caught_up",
) -> TurnResult:
    """Close out the conversational-approval session. Mirrors
    :func:`_finalize_guide`: marks state completed, builds a tailored
    wrap prompt, does NOT commit a user profile."""
    # Render the wrap message FIRST against the current state so the
    # block builder still sees ``_pending_rec`` if the variant cares.
    msg = _prompts.build_rec_pending_block(
        variant=variant,
        rec=(st.extracted.get("_pending_rec") if isinstance(st.extracted.get("_pending_rec"), dict) else None),
        bot_id=bot_id,
    )

    # Strip scratch fields before persisting the terminal state so an
    # admin reading the state file later doesn't think a rec is "still"
    # pending. We don't commit a profile so this is purely cosmetic for
    # the on-disk record, but it keeps the file readable.
    for k in (
        "_pending_rec", "_initial_surface", "_initial_scope_id",
        "_recs_shown", "_unknown_streak",
    ):
        st.extracted.pop(k, None)
    st.current_phase = _phases.PHASE_REC_PENDING
    _state.mark_completed(shared_dir, st)

    return TurnResult(
        system_append=msg,
        wizard_session_id=None,  # plugin clears its session state
        phase=_phases.PHASE_REC_PENDING,
        completed=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Forge-via-messaging trigger (slice 5b7b)
# ─────────────────────────────────────────────────────────────────────────────


def _should_offer_forge(extracted: dict[str, Any]) -> bool:
    """Spec §4.2 phase 7 trigger: fires when ``top_goals`` is non-empty
    AND ``apps_accepted`` is empty. The user articulated specific things
    they want, and the gallery didn't satisfy any of them."""
    goals = extracted.get("top_goals")
    accepted = extracted.get("apps_accepted")
    has_goals = bool(goals) if isinstance(goals, list) else bool(goals and str(goals).strip())
    has_accepts = bool(accepted) if isinstance(accepted, list) else False
    return has_goals and not has_accepts


def _unique_preserve_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Forge-via-messaging handlers (slice 5b7b)
# ─────────────────────────────────────────────────────────────────────────────


_FORGE_OPT_IN_PHRASES = frozenset({
    "go ahead", "let's try", "lets try", "let me try", "let's do it",
    "lets do it", "do it", "sounds good", "sure thing", "give it a shot",
})
_FORGE_OPT_IN_WORDS = frozenset({
    "yes", "ok", "okay", "sure", "yep", "yeah", "build", "try",
})
_FORGE_OPT_OUT_PHRASES = frozenset({
    "no thanks", "not now", "not interested", "maybe later", "another time",
    "not today",
})
_FORGE_OPT_OUT_WORDS = frozenset({
    "no", "skip", "later", "pass", "cancel", "nope", "nah", "stop",
})

_FORGE_BUILD_PHRASES = frozenset({
    "go ahead", "ship it", "do it", "do them", "sounds good",
    "looks good", "build it", "build", "let's build", "lets build",
    "good to go",
})
_FORGE_BUILD_WORDS = frozenset({
    "build", "yes", "ok", "okay", "go", "ship", "approve", "confirm",
})

_FORGE_EDIT_PHRASES = frozenset({
    "one more thing", "go back", "wait", "hold on",
})
_FORGE_EDIT_WORDS = frozenset({
    "edit", "change", "tweak", "back", "more", "redo", "fix", "revise",
})

_FORGE_CANCEL_PHRASES = frozenset({
    "don't build", "dont build", "shelve it", "throw it out", "scrap it",
    "not now", "maybe later",
})
_FORGE_CANCEL_WORDS = frozenset({
    "cancel", "no", "skip", "abort", "discard", "shelve", "stop",
})

_FORGE_SHOW_SPEC_PHRASES = frozenset({
    "show me the spec", "show full spec", "show me the markdown",
    "full spec", "the spec", "show spec",
})
_FORGE_SHOW_SPEC_WORDS = frozenset({"spec"})


def _classify_forge_intro(text: str) -> str:
    text = (text or "").strip().lower()
    if not text:
        return "ambiguous"
    tokens = set(re.findall(r"[a-zA-Z']+", text))
    # Opt-out wins over opt-in to avoid "no thanks but yes" misreads.
    if any(p in text for p in _FORGE_OPT_OUT_PHRASES):
        return "opt_out"
    if any(w in tokens for w in _FORGE_OPT_OUT_WORDS):
        return "opt_out"
    if any(p in text for p in _FORGE_OPT_IN_PHRASES):
        return "opt_in"
    if any(w in tokens for w in _FORGE_OPT_IN_WORDS):
        return "opt_in"
    return "ambiguous"


def _classify_forge_confirm(text: str) -> str:
    """Returns 'build' / 'edit' / 'cancel' / 'show_spec' / 'ambiguous'."""
    text = (text or "").strip().lower()
    if not text:
        return "ambiguous"
    tokens = set(re.findall(r"[a-zA-Z']+", text))
    # Show-spec wins over build/cancel — explicit affordance for users who
    # want to read the markdown before committing.
    if any(p in text for p in _FORGE_SHOW_SPEC_PHRASES):
        return "show_spec"
    # Cancel before build (so "don't build" doesn't false-classify).
    if any(p in text for p in _FORGE_CANCEL_PHRASES):
        return "cancel"
    if any(w in tokens for w in _FORGE_CANCEL_WORDS):
        return "cancel"
    if any(p in text for p in _FORGE_EDIT_PHRASES):
        return "edit"
    if any(w in tokens for w in _FORGE_EDIT_WORDS):
        return "edit"
    if any(p in text for p in _FORGE_BUILD_PHRASES):
        return "build"
    if any(w in tokens for w in _FORGE_BUILD_WORDS):
        return "build"
    return "ambiguous"


def _handle_forge_intro(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Opt-in / opt-out gate. Opt in → advance to FORGE_DESIGN. Opt out
    or ambiguous → wrap (silently for opt-out; with a "sorry, didn't
    catch that" note for ambiguous). Wrap finalizes the wizard since
    we've already passed gallery_recs and the user declined to forge."""
    intent = _classify_forge_intro(user_message)
    if intent == "opt_in":
        st.current_phase = _phases.PHASE_FORGE_DESIGN
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_FORGE_DESIGN,
            completed=False,
        )
    # Opt-out or ambiguous — both wrap. The wrap prompt notes whether
    # the wizard did or didn't propose forging so the bot can be tonally
    # accurate.
    st.current_phase = _phases.PHASE_WRAP
    return _finalize(shared_dir, st, bot_id, user_key)


def _handle_forge_confirm(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Build / edit / cancel / show_spec gate."""
    intent = _classify_forge_confirm(user_message)

    if intent == "edit":
        st.current_phase = _phases.PHASE_FORGE_DESIGN
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_FORGE_DESIGN,
            completed=False,
        )

    if intent == "cancel":
        # Wrap without queuing the build.
        st.current_phase = _phases.PHASE_WRAP
        return _finalize(shared_dir, st, bot_id, user_key)

    if intent == "show_spec":
        # Re-render the same confirm prompt — it already includes the
        # spec body. The bot is instructed in the prompt to render the
        # spec verbatim when this is the user's intent.
        st.extracted["_forge_show_spec_requested"] = True
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_FORGE_CONFIRM,
            completed=False,
        )

    if intent == "build":
        return _kick_off_forge_and_wrap(shared_dir, st, bot_id, user_key)

    # Ambiguous — re-render the confirm prompt; next turn gets another shot.
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=_phases.PHASE_FORGE_CONFIRM,
        completed=False,
    )


def _kick_off_forge_and_wrap(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Hand off to forge_handlers.kick_off_build, then finalize the
    wizard with a tailored wrap prompt — either 'queued, watch for the
    DM' or 'couldn't queue' depending on the result.

    The user-profile commit happens once up front so every exit path
    (import error, kickoff exception, success) writes the user's
    answers to disk. The forge_* / underscore scratch fields are
    filtered out by `profile.commit()` itself.
    """
    _commit_profile_safely(shared_dir, st, bot_id, user_key)

    try:
        from . import forge_handlers as _fh
    except ImportError as e:
        st.current_phase = _phases.PHASE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_forge_build_failed_wrap(
                app_name=str(st.extracted.get("forge_app_name") or "untitled-app"),
                reason=f"forge_handlers import failed: {e}",
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
        )

    try:
        result = _fh.kick_off_build(
            shared_dir, bot_id, user_key, dict(st.extracted),
        )
    except Exception as e:
        st.current_phase = _phases.PHASE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_forge_build_failed_wrap(
                app_name=str(st.extracted.get("forge_app_name") or "untitled-app"),
                reason=f"unexpected error: {e}",
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
        )

    # Stash the kickoff result on state for the wrap prompt to read.
    st.extracted["_forge_job_id"] = result.job_id or ""
    st.extracted["_forge_app_id"] = result.app_id or ""
    st.current_phase = _phases.PHASE_WRAP
    _state.mark_completed(shared_dir, st)

    if not result.ok:
        return TurnResult(
            system_append=_prompts.render_forge_build_failed_wrap(
                app_name=result.app_name,
                reason=result.error or "unknown",
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
            network_dirty=False,
        )

    return TurnResult(
        system_append=_prompts.render_forge_kickoff_wrap(
            app_name=result.app_name,
        ),
        wizard_session_id=None,
        phase=_phases.PHASE_WRAP,
        completed=True,
        network_dirty=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Guide-draft handlers (slice 5b6)
# ─────────────────────────────────────────────────────────────────────────────


# Keywords are split between PHRASES (multi-word, substring match against
# the full reply) and WORDS (single tokens, must match a whole word in the
# reply). Without the split, single-word "no" would match inside "not sure"
# and silently classify as cancel.
_PREVIEW_PHRASES = {"see it", "let me see", "show me"}
_PREVIEW_WORDS = {"show", "preview", "see"}
_SAVE_PHRASES = {"go ahead", "ship it", "do it", "looks good"}
_SAVE_WORDS = {"save", "yes", "ok", "okay", "good", "approve", "confirm"}
_EDIT_PHRASES = {"go back", "keep going"}
_EDIT_WORDS = {"edit", "change", "redo", "back", "tweak", "fix", "more"}
_CANCEL_PHRASES = {"don't save", "dont save", "throw away"}
_CANCEL_WORDS = {"cancel", "no", "nope", "discard", "abort"}


def _matches_keywords(
    text: str,
    tokens: set[str],
    *,
    phrases: set[str],
    words: set[str],
) -> bool:
    if any(kw in text for kw in phrases):
        return True
    if any(kw in tokens for kw in words):
        return True
    return False


def _looks_like_preview_request(text: str) -> bool:
    text = (text or "").strip().lower()
    if not text:
        return False
    tokens = set(text.split())
    return _matches_keywords(
        text, tokens, phrases=_PREVIEW_PHRASES, words=_PREVIEW_WORDS,
    )


def _classify_confirm_reply(text: str) -> str:
    """Classify a GUIDE_CONFIRM user reply. Order matters — we check
    cancel before save so 'no, don't save' doesn't read as 'no=wrong
    AND save=right'. Returns one of: 'save' | 'edit' | 'cancel' |
    'unclear'."""
    text = (text or "").strip().lower()
    if not text:
        return "unclear"
    tokens = set(text.split())

    if _matches_keywords(text, tokens, phrases=_CANCEL_PHRASES, words=_CANCEL_WORDS):
        return "cancel"
    if _matches_keywords(text, tokens, phrases=_SAVE_PHRASES, words=_SAVE_WORDS):
        return "save"
    if _matches_keywords(text, tokens, phrases=_EDIT_PHRASES, words=_EDIT_WORDS):
        return "edit"
    return "unclear"


def _handle_guide_confirm(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Process the user's reply to the guide preview.

    Save → writes the guide via :func:`evo.guide.write_guide`, audit-logs
    a ``guide_save`` event, finalizes the session.
    Cancel → finalizes without writing.
    Edit → bumps state back to GATHER (preserving extracted fields) so
    the user can refine.
    Unclear → re-renders the confirm prompt with a nudge."""
    intent = _classify_confirm_reply(user_message)

    if intent == "edit":
        st.current_phase = _phases.PHASE_GUIDE_GATHER
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_GUIDE_GATHER,
            completed=False,
        )

    if intent == "cancel":
        # Mark completed at confirm phase (no save). Finalize will pick
        # the right wrap variant via audience.
        return _finalize_guide(shared_dir, st, bot_id, user_key, saved=False)

    if intent == "save":
        wrote = _write_guide_from_state(shared_dir, st, bot_id, user_key)
        return _finalize_guide(shared_dir, st, bot_id, user_key, saved=wrote)

    # Unclear — re-render the confirm prompt; the next turn gets another shot.
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GUIDE_CONFIRM,
        completed=False,
    )


def _write_guide_from_state(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> bool:
    """Write the guide using the gathered fields. Best-effort — returns
    True on success, False on failure (caller surfaces the outcome to
    the user via the wrap message)."""
    try:
        from .. import guide as _guide
    except ImportError:
        return False

    extracted = st.extracted or {}
    audience = extracted.get("audience")
    tone = extracted.get("tone")
    do_say = extracted.get("do_say")
    dont_say = extracted.get("dont_say")
    body = (extracted.get("body_outline") or "").strip()

    fm: dict[str, Any] = {}
    if isinstance(audience, str) and audience:
        fm["audience"] = audience
    if isinstance(tone, str) and tone:
        fm["tone"] = tone
    if isinstance(do_say, list) and do_say:
        fm["do_say"] = do_say
    if isinstance(dont_say, list) and dont_say:
        fm["dont_say"] = dont_say

    body_md = body if body else f"# {bot_id} — Team Guide\n\n(no body content set yet)\n"
    if not body_md.lstrip().startswith("#"):
        # Standard markdown shape — header, blank line, body.
        body_md = f"# {bot_id.title()} — Team Guide\n\n{body_md}"

    # authored_by — best-effort: pull pod_user out of an ext: user_key
    # tail isn't useful, so leave authored_by unset and let write_guide
    # preserve any prior value.
    try:
        _guide.write_guide(
            shared_dir,
            bot_id,
            frontmatter=fm,
            body=body_md,
            authored_by=None,
        )
    except Exception:
        return False

    # Audit
    channel, sender_external_id = _parse_user_key(user_key)
    try:
        _audit.append_event(
            shared_dir,
            actor=_audit.api_actor(),
            action="guide_save",
            bot_id=bot_id,
            channel=channel or "<n/a>",
            from_external_id=None,
            to_external_id=sender_external_id or "<unknown>",
            reason=None,
        )
    except Exception:
        # Persist even if audit fails — losing audit on the rare IO blip
        # is preferable to losing the guide save.
        pass
    return True


def _finalize_guide(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    *,
    saved: bool,
) -> TurnResult:
    """Close out the guide-draft session. Builds a tailored wrap prompt
    so the bot can tell the user whether the guide was saved or
    discarded."""
    st.current_phase = _phases.PHASE_GUIDE_CONFIRM
    _state.mark_completed(shared_dir, st)

    msg = _build_guide_wrap_prompt(saved)
    return TurnResult(
        system_append=msg,
        wizard_session_id=None,  # plugin clears its session state
        phase=_phases.PHASE_GUIDE_CONFIRM,
        completed=True,
    )


def _build_guide_wrap_prompt(saved: bool) -> str:
    """One-off systemAppend for the guide draft's terminal turn —
    different from the canonical _wrap_block because there's no profile
    summary to recap, just a save/no-save acknowledgment."""
    from . import prompts as _p
    if saved:
        body = (
            "\nPhase: guide_draft wrap (saved).\n"
            "Goal: tell the user the bot guide was saved successfully and "
            "will be picked up at the next session start. Keep it short — "
            "one or two sentences. Mention that they can edit it any time "
            "with `evo guide` again."
        )
    else:
        body = (
            "\nPhase: guide_draft wrap (cancelled).\n"
            "Goal: acknowledge that the draft was not saved, briefly tell "
            "the user they can run `evo guide` again to start over or pick "
            "up where they left off. One or two warm sentences."
        )
    return _p._HEADER + body


def _route_to_secondary_tour(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Transition from CHALLENGE into the secondary chain — the user
    typed a tour-intent keyword instead of a passphrase or decline.

    Audience flips to ``secondary`` and the next phase is the bot-guide-
    driven SECONDARY_GREET. State is persisted; bot_guide is loaded and
    passed into the prompt builder so the greet can incorporate it.
    """
    st.audience = "secondary"
    st.current_phase = _phases.PHASE_SECONDARY_GREET
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=_phases.PHASE_SECONDARY_GREET,
        completed=False,
        network_dirty=False,
    )


def _close_challenge_no_claim(
    shared_dir: Path,
    st: _state.WizardState,
    user_key: str,
    *,
    gave_attempt: bool,
    extra_reason: str | None = None,
) -> TurnResult:
    """End the wizard from CHALLENGE with no claim applied. We keep this
    soft — the user can re-run ``evo wizard`` any time."""
    # Mark completed (skipped, technically — but 5b2 keeps the lifecycle
    # surface narrow at completed for v1 wizard plumbing).
    st.current_phase = _phases.PHASE_CHALLENGE
    _state.mark_completed(shared_dir, st)

    body = _prompts.build_challenge_dismissal(gave_passphrase_attempt=gave_attempt)
    if extra_reason:
        body = body + "\n\nAdditional context for the bot: " + extra_reason

    return TurnResult(
        system_append=body,
        wizard_session_id=None,
        phase=_phases.PHASE_CHALLENGE,
        completed=True,
        network_dirty=False,
    )


def _parse_user_key(user_key: str) -> tuple[str | None, str | None]:
    """Recover ``(channel, external_id)`` from an ``ext:<channel>:<id>``
    user_key. Returns ``(None, None)`` for non-ext keys (pod:, anon:)."""
    if not user_key.startswith("ext:"):
        return None, None
    parts = user_key.split(":", 2)
    if len(parts) != 3:
        return None, None
    channel, ext_id = parts[1], parts[2]
    if not channel or not ext_id:
        return None, None
    return channel, ext_id


def _try_audit(shared_dir: Path, **fields: Any) -> None:
    """Append an audit event, swallowing IO errors (logged) so a flaky
    audit write doesn't bring down the wizard turn."""
    try:
        _audit.append_event(shared_dir, **fields)
    except Exception as e:
        import sys
        print(
            f"[evo.wizard.engine] WARNING: audit write failed: {e}",
            file=sys.stderr,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Finalization
# ─────────────────────────────────────────────────────────────────────────────


def _commit_profile_safely(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> None:
    """Best-effort profile commit shared by every terminal path.

    Resolves the bot's home directory and macOS user name from network.json,
    maps ``state.extracted`` into v2 user-profile sections, and writes the
    file to ``~/<bot>/.openclaw/profiles/<user_key>.md`` via the sudo cp
    pattern. A write failure logs but does not block the user-facing wrap
    message — the wrap message is the user-visible signal of completion;
    the profile write is plumbing.

    Used by both the canonical ``_finalize`` path and the forge-kickoff
    wrap (which renders its own custom prompt rather than going through
    ``_finalize``, but still needs the user's profile fields written).
    """
    import sys

    try:
        from evolve_admin.config import bot_home, get_bot_user, load_network
    except ImportError as e:
        print(
            f"[evo.wizard.engine] WARNING: cannot import network helpers ({e}); "
            f"skipping profile commit for user_key={user_key} bot_id={bot_id}",
            file=sys.stderr,
        )
        return

    try:
        network = load_network()
    except Exception as e:  # noqa: BLE001
        print(
            f"[evo.wizard.engine] WARNING: cannot load network.json ({e}); "
            f"skipping profile commit for user_key={user_key} bot_id={bot_id}",
            file=sys.stderr,
        )
        return

    home = bot_home(bot_id, network)
    bot_user = get_bot_user(bot_id, network)

    try:
        _profile_writer.commit(
            extracted=dict(st.extracted),
            user_key=user_key,
            bot_id=bot_id,
            bot_home=home,
            bot_user=bot_user,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[evo.wizard.engine] WARNING: profile commit failed for "
            f"user_key={user_key} bot_id={bot_id}: {e}",
            file=sys.stderr,
        )


def _finalize(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Commit profile, mark state completed, return final systemAppend.

    Profile commit is best-effort: a write failure logs but does not
    block the user-facing wrap message. The wizard state's ``completed``
    transition is what the plugin uses to know the run is over.
    """
    _commit_profile_safely(shared_dir, st, bot_id, user_key)

    # Move state to the appropriate wrap phase explicitly so an admin
    # reading the state file sees a coherent "completed at wrap" record
    # rather than a mid-phase snapshot. Secondary chain has its own wrap
    # phase with a different tone; primary uses the canonical one.
    wrap_phase = (
        _phases.PHASE_SECONDARY_WRAP if st.audience == "secondary"
        else _phases.PHASE_WRAP
    )
    st.current_phase = wrap_phase
    _state.mark_completed(shared_dir, st)

    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=None,  # plugin clears its session state
        phase=wrap_phase,
        completed=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction helper — handles bot_guide loading for the
# secondary chain so callers don't have to.
# ─────────────────────────────────────────────────────────────────────────────


_SECONDARY_PHASE_NAMES = frozenset({
    _phases.PHASE_SECONDARY_GREET,
    _phases.PHASE_SECONDARY_ABOUT_YOU,
    _phases.PHASE_HOW_TO_USE,
    _phases.PHASE_SECONDARY_WRAP,
})

# Phases that benefit from having the bot guide loaded into prompt
# context — voice cues, do_say / dont_say, audience framing. Today the
# secondary chain reads the guide for content; rec_pending reads it so
# the bot's pitch matches the guide's declared tone.
_GUIDE_AWARE_PHASE_NAMES = _SECONDARY_PHASE_NAMES | frozenset({
    _phases.PHASE_REC_PENDING,
})


def _build_prompt(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    *,
    extra_ctx: dict[str, Any] | None = None,
) -> str:
    """Render the systemAppend for the current phase. Various phases
    need extra context: secondary phases get the bot guide loaded;
    GALLERY_RECS gets a freshly-loaded candidate list. Primary chain
    phases past PLATFORM_TOUR get a default empty context."""
    ctx: dict[str, Any] = {"bot_id": bot_id}
    if st.current_phase in _GUIDE_AWARE_PHASE_NAMES or st.audience == "secondary":
        guide = _load_bot_guide(shared_dir, bot_id)
        if guide is not None:
            ctx["bot_guide"] = guide

    # Slice 5b8 day 2: voice cues for the rec_pending pitch — bot guide
    # supplemented by SOUL.md and the RSI profile's Communication
    # Preferences section. Best-effort; absent sources are silently
    # skipped by voice_summary.
    if st.current_phase == _phases.PHASE_REC_PENDING:
        try:
            from . import voice as _voice
            voice_cues = _voice.voice_summary(
                shared_dir, bot_id,
                bot_guide=ctx.get("bot_guide"),
            )
            if voice_cues:
                ctx["voice"] = voice_cues
        except Exception:
            # Voice is decorative; never let a load failure crash the turn.
            pass
    if st.current_phase == _phases.PHASE_AB_PROVISION:
        # Entry/resume render for the add-bot build phase: surface the
        # live job snapshot when one exists; otherwise the prompt's
        # lost-track render needs to know whether the bot actually
        # landed (best-effort membership probe).
        try:
            from . import provision_driver as _pdrv
            job = _pdrv.get_job(bot_id, st.user_key)
        except Exception:
            job = None
        if job is not None:
            ctx["ab_job"] = job.snapshot()
        else:
            try:
                from evolve_admin.config import load_network as _ln
                members = {
                    str(m).lower() for m in (_ln().get("members") or [])
                }
            except Exception:
                members = set()
            account = str(st.extracted.get("_ab_account") or "")
            ctx["ab_in_members"] = bool(account) and account in members
    if st.current_phase == _phases.PHASE_GALLERY_RECS and "gallery_candidates" not in (extra_ctx or {}):
        # Load candidates lazily for the prompt — handler reloads them
        # for classification on the next turn (cheap).
        try:
            from . import gallery_recs as _gr
            ctx["gallery_candidates"] = _gr.load_candidates(
                shared_dir, bot_id, st.extracted, top_k=3,
            )
        except Exception:
            ctx["gallery_candidates"] = []
    if extra_ctx:
        ctx.update(extra_ctx)
    return _prompts.build(st.current_phase, st.extracted, context=ctx)


def _load_bot_guide(shared_dir: Path, bot_id: str):
    """Soft load — a missing guide module / parse failure / IO error
    returns None so the prompt builder can fall back to a generic
    framing rather than crash the secondary chain."""
    try:
        from .. import guide as _guide
    except ImportError:
        return None
    try:
        return _guide.load_for_session_surface(shared_dir, bot_id)
    except Exception:
        return None


def _meaningful(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# App-create chain (Wave 3) — `evo app create`
# ─────────────────────────────────────────────────────────────────────────────


# Per-session cap on `iterate <feedback>` rounds. Each iteration is one
# Sonnet call (~$0.05); without a cap a stuck user — or attacker — could
# burn unbounded budget in a single wizard session. 5 rounds is enough
# for genuine refinement; if a user genuinely needs more, they can
# `cancel` and start fresh.
_APP_CREATE_MAX_ITERATIONS = 5


def _handle_app_create_describe(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Stash the user's description, call the LLM drafter, advance to REVIEW.

    The user's reply IS the description — no LLM-extraction needed.
    Calling ``_build_draft`` is the expensive step (~10s Sonnet call).
    """
    description = (user_message or "").strip()
    if len(description) < 8:
        # Too short to be a real description — re-prompt without calling
        # the LLM. Common UX: user types "ok" or "yes" thinking they're
        # confirming something rather than describing.
        return TurnResult(
            system_append=_prompts.render_app_create_describe_too_short(),
            wizard_session_id=user_key,
            phase=_phases.PHASE_APP_CREATE_DESCRIBE,
            completed=False,
        )

    st.extracted["_app_description"] = description
    _state.write_state(shared_dir, st)

    draft = _call_build_draft(
        description=description,
        target_bots=[bot_id],
        shared_dir=shared_dir,
        version=1,
    )
    if draft is None:
        # LLM call failed — keep the wizard alive at DESCRIBE so the
        # user can retry. The prompt explains what happened.
        return TurnResult(
            system_append=_prompts.render_app_create_draft_failed(),
            wizard_session_id=user_key,
            phase=_phases.PHASE_APP_CREATE_DESCRIBE,
            completed=False,
        )

    st.extracted["_current_draft"] = draft
    st.current_phase = _phases.PHASE_APP_CREATE_REVIEW
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_app_create_review(draft),
        wizard_session_id=user_key,
        phase=_phases.PHASE_APP_CREATE_REVIEW,
        completed=False,
    )


def _handle_app_create_review(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    """Classify approve / iterate / cancel and route accordingly."""
    intent, feedback = _classify_app_create_review(user_message)
    draft = st.extracted.get("_current_draft") or {}

    if intent == "cancel":
        st.current_phase = _phases.PHASE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_app_create_cancelled(
                str(draft.get("display_name") or "the app"),
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
        )

    if intent == "iterate":
        if not feedback.strip():
            # Iterate with empty feedback — show the same draft + ask for
            # a concrete suggestion. Don't burn a Sonnet call on nothing.
            return TurnResult(
                system_append=_prompts.render_app_create_iterate_empty(draft),
                wizard_session_id=user_key,
                phase=_phases.PHASE_APP_CREATE_REVIEW,
                completed=False,
            )
        # Cap iterations to keep a single user from accidentally — or
        # an attacker from deliberately — burning unbounded Sonnet
        # calls in one session. Post-review fix (no cap meant unlimited
        # iteration cost). Cap is per-session; the user can `cancel`
        # and start fresh with `evo app create` if they need more
        # room.
        iteration_count = int(st.extracted.get("_iteration_count") or 0)
        if iteration_count >= _APP_CREATE_MAX_ITERATIONS:
            return TurnResult(
                system_append=_prompts.render_app_create_iterate_cap_hit(
                    draft, _APP_CREATE_MAX_ITERATIONS,
                ),
                wizard_session_id=user_key,
                phase=_phases.PHASE_APP_CREATE_REVIEW,
                completed=False,
            )
        new_draft = _call_build_draft(
            description=str(st.extracted.get("_app_description") or ""),
            target_bots=[bot_id],
            shared_dir=shared_dir,
            previous_draft=draft,
            feedback=feedback,
            version=int(draft.get("version") or 1) + 1,
        )
        if new_draft is None:
            return TurnResult(
                system_append=_prompts.render_app_create_draft_failed(),
                wizard_session_id=user_key,
                phase=_phases.PHASE_APP_CREATE_REVIEW,
                completed=False,
            )
        st.extracted["_current_draft"] = new_draft
        st.extracted["_iteration_count"] = iteration_count + 1
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_app_create_review(
                new_draft, iterated=True,
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_APP_CREATE_REVIEW,
            completed=False,
        )

    if intent == "approve":
        return _commit_app_create(shared_dir, st, bot_id, user_key, draft)

    # Ambiguous — re-render with a hint.
    return TurnResult(
        system_append=_prompts.render_app_create_review(
            draft, ambiguous_reply=True,
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_APP_CREATE_REVIEW,
        completed=False,
    )


_APPROVE_EXACT = frozenset({
    "approve", "yes", "y", "ok", "okay", "go", "build", "ship",
    "do it", "yep", "yeah", "sure", "let's do it", "lets do it",
    "go ahead", "ship it",
})
_CANCEL_EXACT = frozenset({
    "cancel", "no", "n", "stop", "nevermind", "never mind", "abort",
    "nope", "no thanks", "forget it", "drop it", "scrap it",
    "don't", "dont", "don't build", "dont build",
})
_ITERATE_PREFIXES: tuple[str, ...] = ("iterate", "edit", "change", "revise")


def _classify_app_create_review(text: str) -> tuple[str, str]:
    """Classify the user's REVIEW reply.

    Returns ``(intent, feedback)`` where:
      intent  ∈ "approve" | "iterate" | "cancel" | "unknown"
      feedback is the trailing text after "iterate " when intent is
              "iterate", else "".

    Strict matching: approve/cancel are exact-match against a curated
    synonym set, and iterate requires one of the prefix verbs. A reply
    like ``"ok but make it faster"`` is intentionally ``"unknown"``
    rather than ``"approve"`` — the user almost certainly wants to
    revise, and we'd rather re-prompt than ship the wrong build.
    """
    if not text:
        return "unknown", ""
    raw = text.strip()
    t = raw.lower()
    if not t:
        return "unknown", ""
    if t in _APPROVE_EXACT:
        return "approve", ""
    if t in _CANCEL_EXACT:
        return "cancel", ""
    for prefix in _ITERATE_PREFIXES:
        # "iterate" alone (no feedback) still matches; handler treats
        # empty feedback as a re-prompt.
        if t == prefix or t.startswith(prefix + " ") or t.startswith(prefix + ":"):
            return "iterate", raw[len(prefix):].lstrip(" :").strip()
    return "unknown", ""


def _call_build_draft(
    *,
    description: str,
    target_bots: list[str],
    shared_dir: Path,
    previous_draft: dict | None = None,
    feedback: str | None = None,
    version: int = 1,
) -> dict | None:
    """Wrap ``spec_routes._build_draft`` with error suppression.

    Returns ``None`` on import failure, missing API key, or LLM error.
    Centralized so both DESCRIBE and REVIEW handlers route through the
    same try/except shape.
    """
    try:
        from ...web.spec_routes import _build_draft
    except ImportError:
        return None
    try:
        return _build_draft(
            description=description,
            target_bots=target_bots,
            shared_dir=shared_dir,
            previous_draft=previous_draft,
            feedback=feedback,
            version=version,
        )
    except Exception:
        return None


def _commit_app_create(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    draft: dict,
) -> TurnResult:
    """Approve path: write manifest, queue forge job, finalize wizard.

    Mirrors :func:`web.spec_routes.api_specs_approve` for the single-bot
    case (the chat caller targets just this bot). Any failure becomes a
    wrap message — the wizard always terminates on approve.
    """
    try:
        from ...applications.manifest import (
            ApplicationManifest, save_manifest, MANIFEST_SOURCE_USER_CREATED,
            born_definition_status,
        )
        from ...applications.forge_jobs import create_install_job
        from ...applications.ids import new_pkg_id, now_iso
        from ...web.spec_routes import _derive_app_id, _dispatch_forge_job
    except ImportError as e:
        st.current_phase = _phases.PHASE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_app_create_failed(
                draft, f"required module missing: {e}",
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
        )

    try:
        app_id = _derive_app_id(str(draft.get("display_name") or "app"))
        pkg_id = new_pkg_id()
        # NOTE: ApplicationManifest doesn't accept SpecDraft.application_tags
        # directly — those map to the manifest's `tags` field. The
        # spec_routes equivalent has the same shape so we keep the
        # behavior identical.
        manifest = ApplicationManifest(
            id=app_id,
            name=str(draft.get("display_name") or app_id),
            bot_id=bot_id,
            display_name=str(draft.get("display_name") or app_id),
            description=str(draft.get("description") or ""),
            build_spec=str(draft.get("build_spec") or ""),
            tags=list(draft.get("application_tags") or []),
            requirements=dict(draft.get("requirements") or {}),
            app_dependencies=list(draft.get("app_dependencies") or []),
            test_command=str(draft.get("test_command") or ""),
            test_exemption_reason=str(draft.get("test_exemption_reason") or ""),
            usage=dict(draft.get("usage") or {}),
            pkg_id=pkg_id,
            status="updating",
            source=MANIFEST_SOURCE_USER_CREATED,
            # Operator-authored, so born DEFINED (design §4). Same gap the
            # reflex path had: without the explicit stamp this took the
            # dataclass default `discovered` and the app the operator just
            # asked evo to create read as a churnable scanner draft.
            definition_status=born_definition_status(MANIFEST_SOURCE_USER_CREATED),
            source_detail=f"evo app create:{user_key}",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        # Slice 3a: every new-app mint seeds the canonical privacy{} +
        # audience_scoping{} defaults (drafts were the gap Slice 2 left).
        from ...applications.privacy_scoping_validator import (
            seed_privacy_scoping_defaults,
        )
        seed_privacy_scoping_defaults(manifest)
        save_manifest(manifest, shared_dir)
        job = create_install_job(
            pkg_id=pkg_id, app_id=app_id, bot_id=bot_id,
            gallery_version="evo", shared_dir=shared_dir,
        )
        _dispatch_forge_job(job.job_id, bot_id)
    except Exception as e:
        st.current_phase = _phases.PHASE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_app_create_failed(
                draft, f"couldn't queue install: {e}",
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_WRAP,
            completed=True,
        )

    st.extracted["_forge_job_id"] = job.job_id
    st.extracted["_forge_app_id"] = app_id
    st.current_phase = _phases.PHASE_WRAP
    _state.mark_completed(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_app_create_queued(
            display_name=str(draft.get("display_name") or app_id),
            job_id=job.job_id,
        ),
        wizard_session_id=None,
        phase=_phases.PHASE_WRAP,
        completed=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Google setup chain — `evo setup-google`
# ─────────────────────────────────────────────────────────────────────────────


# Cancel words accepted at every phase of the wizard. Kept here (not in
# the classifier set used by app_create) because the google_setup flow
# may want different vocabulary later — e.g. accepting "skip" or "bail".
_GOOGLE_SETUP_CANCEL = {
    "cancel", "stop", "abort", "no", "n", "nope", "nevermind",
    "never mind", "quit", "exit",
}


def _is_google_cancel(text: str) -> bool:
    return (text or "").strip().lower() in _GOOGLE_SETUP_CANCEL


def _google_setup_cancel(
    shared_dir: Path,
    st: _state.WizardState,
    user_key: str,
) -> TurnResult:
    """Terminal — operator cancelled. No partial state written to disk.

    The cancel render is canonical text (a fixed close-out line), so
    it's verbatim regardless of the WRAP phase being agenda-mode by
    default. We populate ``direct_send_message`` explicitly: __post_init__
    keys auto-routing on the ``phase`` field, but here ``phase=WRAP`` is
    set for state-machine purposes (wizard finalization), not as a
    routing hint. The render itself is verbatim.
    """
    # Wipe transient secret from state before marking completed so the
    # file at rest never contains a half-committed credential.
    st.extracted.pop("_client_secret", None)
    st.current_phase = _phases.PHASE_WRAP
    _state.mark_completed(shared_dir, st)
    body = _prompts.render_google_setup_cancelled()
    return TurnResult(
        system_append=_verbatim_wrap(body),
        direct_send_message=body,
        wizard_session_id=None,
        phase=_phases.PHASE_WRAP,
        completed=True,
    )


def _handle_google_setup_intro(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    if _is_google_cancel(user_message):
        return _google_setup_cancel(shared_dir, st, user_key)
    # Any other reply advances. The intro prompt already told the
    # operator to reply "ready" / "go" / "ok" to continue; we don't
    # over-validate here.
    st.current_phase = _phases.PHASE_GOOGLE_SETUP_ADMIN_URL
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_google_setup_admin_url(),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GOOGLE_SETUP_ADMIN_URL,
        completed=False,
    )


def _validate_admin_base_url(raw: str) -> tuple[str, str | None]:
    """Return ``(normalized_url, error_or_None)``.

    Accepts ``http://host[:port]`` or ``https://host[:port]`` with no
    path / query / fragment. Trims trailing slash. Anything else returns
    an error string the prompt renderer surfaces to the operator.
    """
    s = (raw or "").strip()
    if not s:
        return "", "URL is empty."
    if not (s.startswith("http://") or s.startswith("https://")):
        return "", (
            "URL must start with `http://` or `https://`. "
            "(Got: " + repr(s[:40]) + ")"
        )
    # Trim trailing slash
    s = s.rstrip("/")
    # Reject paths / query / fragment — we only want the base.
    from urllib.parse import urlparse
    try:
        parsed = urlparse(s)
    except Exception as e:
        return "", f"Couldn't parse the URL: {e}"
    if not parsed.netloc:
        return "", "URL is missing a host. Try `https://your-host.tld`."
    if parsed.path and parsed.path != "/":
        return "", (
            "Drop the path — just the base URL. "
            "(Got path: " + repr(parsed.path) + ")"
        )
    if parsed.query or parsed.fragment:
        return "", "Drop the query/fragment — just the base URL."
    return s, None


def _handle_google_setup_admin_url(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    if _is_google_cancel(user_message):
        return _google_setup_cancel(shared_dir, st, user_key)
    url, err = _validate_admin_base_url(user_message)
    if err is not None:
        return TurnResult(
            system_append=_prompts.render_google_setup_admin_url_invalid(err),
            wizard_session_id=user_key,
            phase=_phases.PHASE_GOOGLE_SETUP_ADMIN_URL,
            completed=False,
        )
    st.extracted["_admin_base_url"] = url
    st.current_phase = _phases.PHASE_GOOGLE_SETUP_CONSOLE
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_google_setup_console(
            admin_base_url=url, bot_id=bot_id,
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GOOGLE_SETUP_CONSOLE,
        completed=False,
    )


def _handle_google_setup_console(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    if _is_google_cancel(user_message):
        return _google_setup_cancel(shared_dir, st, user_key)
    # Any non-cancel reply advances. The operator confirms they've
    # done the Console steps by typing anything (typically "done" or
    # "ready").
    st.current_phase = _phases.PHASE_GOOGLE_SETUP_CLIENT_ID
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_google_setup_client_id(),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GOOGLE_SETUP_CLIENT_ID,
        completed=False,
    )


_GOOGLE_CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"


def _validate_client_id(raw: str) -> tuple[str, str | None]:
    s = (raw or "").strip()
    if not s:
        return "", "Client ID is empty."
    # Strip surrounding quotes if the operator pasted with them.
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if not s.endswith(_GOOGLE_CLIENT_ID_SUFFIX):
        return "", (
            "That doesn't look like a Google OAuth client_id — they end "
            "in `" + _GOOGLE_CLIENT_ID_SUFFIX + "`. "
            "Double-check that you copied the full value (it's the long "
            "string in the Client ID column on the Credentials page)."
        )
    if len(s) < 40:
        return "", "Client ID seems too short — try copying the full value again."
    return s, None


def _handle_google_setup_client_id(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
) -> TurnResult:
    if _is_google_cancel(user_message):
        return _google_setup_cancel(shared_dir, st, user_key)
    client_id, err = _validate_client_id(user_message)
    if err is not None:
        return TurnResult(
            system_append=_prompts.render_google_setup_client_id_invalid(err),
            wizard_session_id=user_key,
            phase=_phases.PHASE_GOOGLE_SETUP_CLIENT_ID,
            completed=False,
        )
    st.extracted["_client_id"] = client_id
    st.current_phase = _phases.PHASE_GOOGLE_SETUP_CLIENT_SECRET
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_google_setup_client_secret(bot_id=bot_id),
        wizard_session_id=user_key,
        phase=_phases.PHASE_GOOGLE_SETUP_CLIENT_SECRET,
        completed=False,
    )


def _validate_client_secret(raw: str) -> tuple[str, str | None]:
    s = (raw or "").strip()
    if not s:
        return "", "Client secret is empty."
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    # Google's current format starts with `GOCSPX-` (Web/Installed apps).
    # Older secrets (pre-2022) don't have this prefix, so we accept both
    # but warn on length to catch obviously-truncated paste errors.
    if len(s) < 20:
        return "", "Client secret seems too short — try copying the full value again."
    return s, None


def _handle_google_setup_client_secret(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    if _is_google_cancel(user_message):
        return _google_setup_cancel(shared_dir, st, user_key)
    secret, err = _validate_client_secret(user_message)
    if err is not None:
        return TurnResult(
            system_append=_prompts.render_google_setup_client_secret_invalid(err, bot_id),
            wizard_session_id=user_key,
            phase=_phases.PHASE_GOOGLE_SETUP_CLIENT_SECRET,
            completed=False,
        )
    # Don't persist the secret in the wizard state file even
    # transiently. Pass it through to the commit step directly.
    client_id = str(st.extracted.get("_client_id") or "")
    admin_base_url = str(st.extracted.get("_admin_base_url") or "")
    return _commit_google_setup(
        shared_dir, st, bot_id, user_key,
        client_id=client_id,
        client_secret=secret,
        admin_base_url=admin_base_url,
        network=network,
    )


def _resolve_primary_bot_id(network: dict[str, Any] | None) -> str | None:
    """Pod-primary lookup. Unused by ``_commit_google_setup`` since PR 2
    of the per-bot OAuth refactor (the wizard targets the calling bot,
    not the pod primary). Kept module-level in case other call sites
    surface later — small, no side effects, no cost."""
    if isinstance(network, dict):
        try:
            from primary_bot import primary_bot_id  # type: ignore
            pid = primary_bot_id(network)
            if isinstance(pid, str) and pid:
                return pid
        except Exception:
            pass
        prim = network.get("primary")
        if isinstance(prim, str) and prim.strip():
            return prim.strip()
        members = network.get("members") or []
        if "evolve" in members:
            return "evolve"
    return None


def _commit_google_setup(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    *,
    client_id: str,
    client_secret: str,
    admin_base_url: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Persist the calling bot's Google OAuth client + admin base URL.

    Per-bot under PR #1123: ``client_id`` → ``network.bots[bot_id].googleOAuthClient``,
    ``client_secret`` → ``bot_id``'s ``auth-profiles.json`` under
    ``GOOGLE_CLIENT_SECRET_PROFILE_ID``. ``adminBaseUrl`` stays a
    top-level key (it's the admin server's URL — one per machine, not
    per-bot).

    The wizard targets ``bot_id`` (the bot the operator is in chat with),
    not the pod's primary bot. Two bots can share an OAuth app post-hoc
    by copying the ``client_secret_ref`` block from one bot's network
    config to the other's — explicit opt-in.

    Failures here surface to the operator with a clear reason — they
    can retry by re-running ``evo setup-google``.
    """
    # 1) Save credentials via the existing helper, targeting the
    # calling bot (not the pod primary).
    try:
        from ...web import server as _admin_server
    except ImportError as e:
        return _google_setup_failed(
            shared_dir, st, user_key,
            f"Couldn't load the admin server module: {e}.",
        )
    save = getattr(_admin_server, "_save_google_oauth_client", None)
    if save is None:
        # Module exists but the helper is defined inside a closure
        # (`register_credentials_routes`). Fall back to writing the
        # network.json fields directly + the auth-profiles entry.
        ok, err = _save_google_oauth_client_inline(
            shared_dir, bot_id,
            client_id=client_id, client_secret=client_secret,
        )
    else:
        try:
            ok, err = save(client_id, client_secret, bot_id)
        except Exception as exc:
            ok, err = False, f"{exc}"
    if not ok:
        return _google_setup_failed(
            shared_dir, st, user_key,
            f"Couldn't save the credentials: {err}.",
        )

    # 2) Write adminBaseUrl to network.json (separate top-level key —
    # the helper above only handles per-bot googleOAuthClient).
    try:
        from ...config import load_network, save_network
        net = load_network()
        net["adminBaseUrl"] = admin_base_url
        save_network(net)
    except Exception as exc:
        return _google_setup_failed(
            shared_dir, st, user_key,
            f"Saved client credentials, but couldn't write "
            f"`adminBaseUrl` to network.json: {exc}. "
            "Set it manually via the admin UI, then everything will "
            "work — your credentials are stored correctly.",
        )

    # 3) Finalize. The success render is verbatim — explicit
    # direct_send_message because phase=WRAP (state-machine final phase)
    # is agenda-mode and __post_init__ wouldn't auto-route.
    st.extracted.pop("_client_secret", None)  # belt + braces
    st.current_phase = _phases.PHASE_WRAP
    _state.mark_completed(shared_dir, st)
    body = _prompts.render_google_setup_saved(
        admin_base_url=admin_base_url, bot_id=bot_id,
    )
    return TurnResult(
        system_append=_verbatim_wrap(body),
        direct_send_message=body,
        wizard_session_id=None,
        phase=_phases.PHASE_WRAP,
        completed=True,
    )


def _google_setup_failed(
    shared_dir: Path,
    st: _state.WizardState,
    user_key: str,
    reason: str,
) -> TurnResult:
    """Terminal failure. Leaves the wizard completed (no resume) but
    surfaces the reason so the operator can act. Secrets are wiped.

    Verbatim render — same routing rationale as _google_setup_cancel."""
    st.extracted.pop("_client_secret", None)
    st.current_phase = _phases.PHASE_WRAP
    _state.mark_completed(shared_dir, st)
    body = _prompts.render_google_setup_failed(reason)
    return TurnResult(
        system_append=_verbatim_wrap(body),
        direct_send_message=body,
        wizard_session_id=None,
        phase=_phases.PHASE_WRAP,
        completed=True,
    )


def _save_google_oauth_client_inline(
    shared_dir: Path,
    primary_bot: str,
    *,
    client_id: str,
    client_secret: str,
) -> tuple[bool, str | None]:
    """Fallback persistence path for environments where the admin
    server's closure-bound ``_save_google_oauth_client`` isn't
    reachable (notably: tests that import the engine without running
    the Flask factory).

    Mirrors :func:`evolve_admin.web.server._save_google_oauth_client`'s
    storage layout — credentials in the Evolve-owned store at
    ``{shared_dir}/credentials/<primary_bot>/google_oauth_client.json``,
    not in openclaw's auth-profiles.json. See the canonical writer for
    the why (auth-profiles is openclaw's territory and its in-memory
    cache silently rewrites the file).
    """
    import json
    import os

    # 1) Write the credential file in the Evolve store. Atomic write
    # via temp + rename; chmod 600 best-effort.
    store_path = shared_dir / "credentials" / primary_bot / "google_oauth_client.json"
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        return False, f"cannot create {store_path.parent}: {exc}"
    payload = {
        "schema_version": 1,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        tmp = store_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, store_path)
    except (PermissionError, OSError) as exc:
        return False, f"credential-store write failed: {exc}"

    # 2) Write client_id + secret_bot pointer to network.json. The
    # secret itself never lives here — that's the whole point of the
    # store split.
    try:
        from ...config import load_network, save_network
        net = load_network()
        bots = net.setdefault("bots", {})
        if not isinstance(bots, dict):
            return False, "network.bots is not a dict"
        bot_cfg = bots.setdefault(primary_bot, {})
        if not isinstance(bot_cfg, dict):
            return False, f"network.bots[{primary_bot}] is not a dict"
        bot_cfg["googleOAuthClient"] = {
            "mode": "self_hosted",
            "client_id": client_id,
            "secret_bot": primary_bot,
        }
        save_network(net)
    except Exception as exc:
        return False, f"network.json write failed: {exc}"

    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot chain — `evo add-bot` (admin-only, M1)
# ─────────────────────────────────────────────────────────────────────────────


# Plan-confirmation vocabulary. Exact-match only (like app_create's
# _APPROVE_EXACT): "ok but make it weekly" must classify as
# change-feedback, never as a confirm. Bare "no" is deliberately in
# NEITHER set — at a confirm gate it's ambiguous between "wrong plan"
# and "don't create", so it falls through to the feedback path and the
# re-rendered plan tells the user how to say each unambiguously.
_AB_CONFIRM_EXACT = frozenset({
    "yes", "y", "yep", "yeah", "ok", "okay", "confirm", "approve",
    "save", "save it", "go ahead", "do it", "lock it in", "looks good",
    "good", "sounds good", "ship it", "sure", "that's right", "thats right",
})
_AB_CANCEL_EXACT = frozenset({
    "cancel", "stop", "abort", "quit", "exit", "nevermind", "never mind",
    "forget it", "drop it", "scrap it", "no thanks",
})


def _classify_add_bot_plan(text: str) -> str:
    """Classify the user's AB_PLAN reply: 'confirm' | 'cancel' | 'edit'.
    Everything that isn't an exact confirm/cancel is treated as
    change-feedback — the delta's "actually, make it weekly" path."""
    t = (text or "").strip().lower().rstrip(".!?,;").strip()
    if t in _AB_CANCEL_EXACT:
        return "cancel"
    if t in _AB_CONFIRM_EXACT:
        return "confirm"
    return "edit"


def _add_bot_all_targets() -> tuple[Any, ...]:
    """Union of extraction targets across the add-bot agenda phases,
    deduped by field name. Used by the AB_PLAN change-feedback path so a
    correction can touch any field in the plan, not just the last
    phase's."""
    seen: set[str] = set()
    out = []
    for ph in _phases.all_add_bot_phases():
        for t in ph.targets:
            if t.name not in seen:
                seen.add(t.name)
                out.append(t)
    return tuple(out)


def _add_bot_account_taken(
    network: dict[str, Any] | None, account: str
) -> bool:
    """True when ``account`` already names a pod member or a configured
    bot in network.json (or is one of evo's own reserved identities).
    Explicit-membership rule: network.json is the only source consulted.

    A bot block whose ONLY key is ``purpose`` is a previously *planned*
    bot (a prior add-bot run that never got provisioned) — that name is
    NOT taken: re-planning it just replaces the purpose on confirm."""
    if account in ("evo", "evolve"):
        return True
    if not isinstance(network, dict):
        return False
    members = network.get("members") or []
    if isinstance(members, list) and any(
        str(m).lower() == account for m in members
    ):
        return True
    bots = network.get("bots") or {}
    if not isinstance(bots, dict):
        return False
    for b, cfg in bots.items():
        if str(b).lower() != account:
            continue
        if isinstance(cfg, dict) and set(cfg.keys()) <= {"purpose"}:
            return False  # planned-only block — re-plannable
        return True
    return False


def _handle_add_bot_plan(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Process the admin's reply to the plan echo.

    Confirm → write the new bot's ``purpose{}`` block (effectiveness-layer
    §4 shape, ``captured: "declared"``, ``confidence: 1.0``) into its
    network.json bot block by mutating ``network`` in place and setting
    ``network_dirty`` — the route handler persists via save_network, the
    same admin-server write path that registers bots. Then finalize with
    the honest M1 wrap (plan saved; account creation comes with M2).

    Cancel → finalize without writing anything.

    Anything else → change-feedback: re-extract across the whole chain's
    targets and re-render the plan, so "actually, make it weekly" or
    "call it Almanac" lands without restarting."""
    intent = _classify_add_bot_plan(user_message)

    if intent == "cancel":
        return _ab_cancelled_terminal(
            shared_dir, st, phase=_phases.PHASE_AB_PLAN,
        )

    if intent == "edit":
        new_fields = _extractor.extract_fields(
            user_message=user_message,
            targets=_add_bot_all_targets(),
            current_state=st.extracted,
        )
        applied = False
        archetype_changed = False
        for k, v in (new_fields or {}).items():
            if not _meaningful(v):
                continue
            # Schema gates: a corrected archetype must still be on the
            # enum, and a corrected name must still yield an account
            # name — otherwise keep the prior (already-valid) value.
            if k == "ab_archetype" and _phases.normalize_archetype(v) is None:
                continue
            if k == "ab_bot_name" and not _phases.account_name_for(v):
                continue
            if k == "ab_archetype" and (
                _phases.normalize_archetype(v)
                != _phases.normalize_archetype(st.extracted.get(k))
            ):
                archetype_changed = True
            st.extracted[k] = v
            applied = True
        if applied:
            if archetype_changed:
                # The starter set follows the role — a role change at the
                # plan invalidates the proposal, so rebuild it (the plan
                # re-render shows the new set).
                _ab_seed_pack_proposal(shared_dir, st, network)
            _state.write_state(shared_dir, st)
            note = "Updated — here's the plan now:"
        else:
            note = (
                "I didn't catch that as a yes, a change, or a cancel — "
                "here's the plan again:"
            )
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id, extra_ctx={"plan_note": note},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PLAN,
            completed=False,
        )

    # Confirm. Validate the load-bearing fields one last time — the
    # phase exits gate on them, but the change-feedback path and resumed
    # sessions make re-checking cheap insurance.
    mission = str(st.extracted.get("ab_mission") or "").strip()
    archetype = _phases.normalize_archetype(st.extracted.get("ab_archetype"))
    account = _phases.account_name_for(st.extracted.get("ab_bot_name"))

    if not mission or archetype is None:
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"plan_note": (
                    "Before I save this, I still need the bot's job pinned "
                    "down — tell me in a sentence what it should do."
                )},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PLAN,
            completed=False,
        )

    if not account or _add_bot_account_taken(network, account):
        # Send the conversation back to naming. Drop the conflicting
        # name first — otherwise AB_NAME's exit re-fires on the very
        # next reply and bounces the same name straight back here.
        taken = str(st.extracted.get("ab_bot_name") or account)
        st.extracted.pop("ab_bot_name", None)
        st.current_phase = _phases.PHASE_AB_NAME
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id, extra_ctx={"name_taken": taken},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_NAME,
            completed=False,
        )

    if not isinstance(network, dict):
        # Defensive: the route always passes network. Without it we
        # can't stage the write — keep the session alive and be honest.
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"plan_note": (
                    "I couldn't save the plan just now — say **yes** to "
                    "try again, or **cancel**."
                )},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PLAN,
            completed=False,
        )

    # The purpose field, exactly as the effectiveness-layer spec §4
    # defines it — same keys, same enum, declared path.
    from evolve_util import now_iso as _now_iso
    bots = network.setdefault("bots", {})
    if not isinstance(bots, dict):
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"plan_note": (
                    "I couldn't save the plan just now — say **yes** to "
                    "try again, or **cancel**."
                )},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PLAN,
            completed=False,
        )
    bot_block = bots.setdefault(account, {})
    if not isinstance(bot_block, dict):
        bot_block = {}
        bots[account] = bot_block
    bot_block["purpose"] = {
        "archetype": archetype,
        "mission": mission,
        "captured": "declared",
        "confidence": 1.0,
        "reviewed_at": _now_iso(),
    }

    channel, sender_external_id = _parse_user_key(user_key)
    _try_audit(
        shared_dir,
        actor=_audit.api_actor(),
        action="add_bot_plan",
        bot_id=account,
        channel=channel or "<n/a>",
        from_external_id=None,
        to_external_id=sender_external_id or "<unknown>",
        reason=f"purpose declared via evo add-bot on {bot_id}",
    )

    # M2: the confirmed plan flows straight into the credential walk.
    # The purpose{} write above still persists first (network_dirty) —
    # if the admin walks away here, the planned bot survives and the
    # chain resumes at credentials.
    from . import provision_driver as _pdrv
    st.extracted["_ab_account"] = account
    st.extracted["_ab_borrow_candidates"] = _pdrv.borrow_candidate_bots(
        network,
    )
    st.current_phase = _phases.PHASE_AB_CREDENTIALS
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_CREDENTIALS,
        completed=False,
        network_dirty=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot M2 — credentials walk, build progress, smoke wrap
# Spec: internal/spec-add-bot-wizard-build-delta-2026-06-10.md §3.2 + §9 (M2).
# ─────────────────────────────────────────────────────────────────────────────


# Skip vocabulary for the credential walk. Exact-match like the
# confirm/cancel sets — "skip the checks later" must not classify.
_AB_SKIP_EXACT = frozenset({
    "skip", "skip it", "skip for now", "skip the key", "later",
    "not now", "no key", "maybe later", "skip this",
})

# Retry / stop vocabulary for the failed-build gate.
_AB_RETRY_EXACT = frozenset({
    "retry", "try again", "again", "re-run", "rerun", "run it again",
    "retry it", "try it again", "yes, retry",
})
_AB_STOP_EXACT = frozenset({
    "stop", "leave it", "stop here", "leave it for later", "later",
    "not now",
})

# Switch-off vocabulary for the failed-smoke gate.
_AB_OFF_EXACT = frozenset({
    "switch it off", "switch off", "turn it off", "off", "shut it off",
    "shut it down", "turn off",
})

# A pasted Anthropic API key. Matched against the RAW message (keys are
# case-sensitive); anything else sk-prefixed gets a "not a Claude key"
# correction instead of silently building a bot that can't think.
_AB_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_AB_OTHER_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")


def _ab_norm(text: str) -> str:
    return (text or "").strip().lower().rstrip(".!?,;").strip()


def _ab_credentials_reprompt(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    note: str,
) -> TurnResult:
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id, extra_ctx={"cred_note": note},
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_CREDENTIALS,
        completed=False,
    )


def _ab_finalize_paused(
    shared_dir: Path,
    st: _state.WizardState,
    *,
    phase: str,
    reason_line: str,
) -> TurnResult:
    """Terminal "keep the plan, build nothing" path shared by skip /
    cancel-at-credentials / stop-after-failure.

    The paused render is canonical text, but ``phase`` may be the
    agenda-mode credentials phase — so the direct-send routing is set
    explicitly here instead of relying on the render_mode auto-route."""
    from . import channel_driver as _chdrv
    _chdrv.clear_token(st.bot_id, st.user_key)
    st.current_phase = phase
    _state.mark_completed(shared_dir, st)
    body = _prompts.render_add_bot_paused(
        st.extracted, reason_line=reason_line,
    )
    return TurnResult(
        system_append=_verbatim_wrap(body),
        wizard_session_id=None,
        phase=phase,
        completed=True,
        direct_send_message=body,
    )


def _handle_add_bot_credentials(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Classify the admin's credential answer — borrow / paste / skip /
    cancel — validate it, and on a valid choice start the build.

    Deterministic by design: the pasted key is taken straight off the
    raw message (never through LLM extraction), and a borrow source is
    only accepted if it was in the candidate list loaded when the plan
    was confirmed."""
    raw = user_message or ""
    t = _ab_norm(raw)

    if t in _AB_CANCEL_EXACT:
        return _ab_finalize_paused(
            shared_dir, st,
            phase=_phases.PHASE_AB_CREDENTIALS,
            reason_line="No problem.",
        )
    if t in _AB_SKIP_EXACT:
        return _ab_finalize_paused(
            shared_dir, st,
            phase=_phases.PHASE_AB_CREDENTIALS,
            reason_line="No problem — skipping the key for now.",
        )

    # Paste path — a Claude key anywhere in the message wins.
    m = _AB_KEY_RE.search(raw)
    if m:
        return _ab_start_provision(
            shared_dir, st, bot_id, user_key, network,
            provider_api_key=m.group(0),
        )
    if _AB_OTHER_KEY_RE.search(raw):
        return _ab_credentials_reprompt(
            shared_dir, st, bot_id, user_key,
            "That doesn't look like a Claude key — they start with "
            "sk-ant-. Paste one of those, pick a bot to copy from, or "
            "say skip.",
        )

    # Borrow path — match candidate bot names as whole words.
    candidates = [
        str(c).lower()
        for c in (st.extracted.get("_ab_borrow_candidates") or [])
        if c
    ]
    tokens = set(re.findall(r"[a-z0-9_\-']+", t))
    matched = [c for c in candidates if c in tokens or f"{c}'s" in tokens]
    if len(matched) == 1:
        return _ab_start_provision(
            shared_dir, st, bot_id, user_key, network,
            borrow_from=matched[0],
        )
    if len(matched) > 1:
        return _ab_credentials_reprompt(
            shared_dir, st, bot_id, user_key,
            "More than one bot matched — which one should the key come "
            "from: " + ", ".join(matched) + "?",
        )
    # Named a pod bot that has no key to copy → say so plainly. Checked
    # BEFORE the bare-"borrow" shortcut so "borrow keyless_bot's key"
    # gets the correction instead of silently borrowing from whichever
    # single candidate exists.
    members = [
        str(mname).lower()
        for mname in ((network or {}).get("members") or [])
    ]
    named_non_candidates = [
        mname for mname in members
        if mname not in candidates and (mname in tokens or f"{mname}'s" in tokens)
    ]
    if named_non_candidates:
        options = (
            "available: " + ", ".join(candidates)
            if candidates else "no bot here has one I can copy"
        )
        return _ab_credentials_reprompt(
            shared_dir, st, bot_id, user_key,
            f"{named_non_candidates[0]} doesn't have a key I can copy "
            f"({options}). You can also paste a fresh key or say skip.",
        )
    # "borrow"-shaped reply with exactly one possible source → take it.
    borrow_ish = tokens & {"borrow", "copy", "share", "use", "borrowed"}
    if borrow_ish and len(candidates) == 1:
        return _ab_start_provision(
            shared_dir, st, bot_id, user_key, network,
            borrow_from=candidates[0],
        )

    return _ab_credentials_reprompt(
        shared_dir, st, bot_id, user_key,
        "I didn't catch that as a key choice. Options: copy another "
        "bot's key, paste a fresh one (starts with sk-ant-), skip for "
        "now, or cancel.",
    )


def _ab_start_provision(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    network: dict[str, Any] | None,
    *,
    provider_api_key: str | None = None,
    borrow_from: str | None = None,
) -> TurnResult:
    """Validated credential in hand — kick the build off and move the
    session to AB_PROVISION. The key rides only in the in-memory job
    record; the state file records just the choice made."""
    from . import provision_driver as _pdrv

    account = str(st.extracted.get("_ab_account") or "") or \
        _phases.account_name_for(st.extracted.get("ab_bot_name"))
    display_name = str(st.extracted.get("ab_bot_name") or account).strip()

    # The plan was confirmed earlier — re-check the name hasn't been
    # taken in the meantime (parallel session, manual add). Same
    # kick-back-to-naming move as the plan handler.
    members = {str(mname).lower() for mname in ((network or {}).get("members") or [])}
    if not account or account in members:
        st.extracted.pop("ab_bot_name", None)
        st.extracted.pop("_ab_account", None)
        st.current_phase = _phases.PHASE_AB_NAME
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"name_taken": display_name or account},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_NAME,
            completed=False,
        )

    _pdrv.start(
        bot_id=bot_id,
        user_key=user_key,
        account=account,
        display_name=display_name,
        provider_api_key=provider_api_key,
        borrow_from=borrow_from,
    )
    st.extracted["_ab_credential_choice"] = (
        f"borrow:{borrow_from}" if borrow_from else "paste"
    )
    st.current_phase = _phases.PHASE_AB_PROVISION
    _state.write_state(shared_dir, st)

    channel, sender_external_id = _parse_user_key(user_key)
    _try_audit(
        shared_dir,
        actor=_audit.api_actor(),
        action="add_bot_provision_start",
        bot_id=account,
        channel=channel or "<n/a>",
        from_external_id=None,
        to_external_id=sender_external_id or "<unknown>",
        reason=(
            f"build started via evo add-bot on {bot_id} "
            f"(credential: {st.extracted['_ab_credential_choice']})"
        ),
    )

    return TurnResult(
        system_append=_prompts.render_add_bot_provision_kickoff(st.extracted),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_PROVISION,
        completed=False,
    )


def _ab_ensure_planned_purpose(
    st: _state.WizardState, network: dict[str, Any] | None,
) -> bool:
    """After a failed build, make sure the planned bot's purpose{} block
    still exists in network.json (the pipeline's rollback restores it;
    this is the belt to that suspender — e.g. an interrupted rollback).
    Returns True when ``network`` was mutated and needs persisting."""
    if not isinstance(network, dict):
        return False
    account = str(st.extracted.get("_ab_account") or "")
    mission = str(st.extracted.get("ab_mission") or "").strip()
    archetype = _phases.normalize_archetype(st.extracted.get("ab_archetype"))
    if not account or not mission or archetype is None:
        return False
    bots = network.setdefault("bots", {})
    if not isinstance(bots, dict):
        return False
    if bots.get(account) is not None:
        # ANY existing block wins — either the rollback already restored
        # the plan, or someone else registered this name meanwhile (a
        # parallel session's full entry must not be clobbered with a
        # purpose-only block).
        return False
    from evolve_util import now_iso as _now_iso
    bots[account] = {
        "purpose": {
            "archetype": archetype,
            "mission": mission,
            "captured": "declared",
            "confidence": 1.0,
            "reviewed_at": _now_iso(),
        }
    }
    return True


def _handle_add_bot_provision(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """One poll turn against the running (or finished) build job."""
    from . import provision_driver as _pdrv

    t = _ab_norm(user_message)
    job = _pdrv.get_job(bot_id, user_key)

    # No record — the admin server restarted mid-build. Be honest about
    # what we can and can't see, and route retry back through the
    # credential walk (the key only ever lived in the lost record).
    if job is None:
        if t in _AB_RETRY_EXACT:
            st.current_phase = _phases.PHASE_AB_CREDENTIALS
            _state.write_state(shared_dir, st)
            return _ab_credentials_reprompt(
                shared_dir, st, bot_id, user_key,
                "Happy to try again — I do need the key again first.",
            )
        if t in _AB_STOP_EXACT or t in _AB_CANCEL_EXACT:
            return _ab_finalize_paused(
                shared_dir, st,
                phase=_phases.PHASE_AB_PROVISION,
                reason_line="Stopped.",
            )
        account = str(st.extracted.get("_ab_account") or "")
        in_members = account in {
            str(mname).lower()
            for mname in ((network or {}).get("members") or [])
        }
        return TurnResult(
            system_append=_prompts.render_add_bot_provision_lost(
                st.extracted, in_members=in_members,
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PROVISION,
            completed=False,
        )

    snap = job.snapshot()

    if snap["status"] == "running":
        note = None
        if t in _AB_CANCEL_EXACT or t in _AB_STOP_EXACT:
            note = (
                "It's mid-build — stopping halfway would leave a mess, "
                "so I'll let it finish and report honestly either way."
            )
        return TurnResult(
            system_append=_prompts.render_add_bot_provision_progress(
                snap, note=note,
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PROVISION,
            completed=False,
        )

    if snap["status"] == "failed":
        dirty = _ab_ensure_planned_purpose(st, network)
        if t in _AB_RETRY_EXACT:
            _pdrv.retry(job)
            return TurnResult(
                system_append=_prompts.render_add_bot_provision_kickoff(
                    st.extracted,
                ),
                wizard_session_id=user_key,
                phase=_phases.PHASE_AB_PROVISION,
                completed=False,
                network_dirty=dirty,
            )
        if t in _AB_STOP_EXACT or t in _AB_CANCEL_EXACT:
            _pdrv.clear_job(bot_id, user_key)
            result = _ab_finalize_paused(
                shared_dir, st,
                phase=_phases.PHASE_AB_PROVISION,
                reason_line="Stopped.",
            )
            result.network_dirty = dirty
            return result
        if not st.extracted.get("_ab_failure_audited"):
            st.extracted["_ab_failure_audited"] = True
            _state.write_state(shared_dir, st)
            channel, sender_external_id = _parse_user_key(user_key)
            _try_audit(
                shared_dir,
                actor=_audit.api_actor(),
                action="add_bot_provision_failed",
                bot_id=snap["account"],
                channel=channel or "<n/a>",
                from_external_id=None,
                to_external_id=sender_external_id or "<unknown>",
                reason=(
                    f"stage {snap.get('failed_stage')}: "
                    f"{snap.get('error') or 'unknown'}"
                ),
            )
        return TurnResult(
            system_append=_prompts.render_add_bot_provision_failed(snap),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_PROVISION,
            completed=False,
            network_dirty=dirty,
        )

    # Succeeded — move to the smoke wrap.
    st.current_phase = _phases.PHASE_AB_SMOKE_WRAP
    smoke = snap.get("smoke") or {}
    if smoke.get("status") == "ok":
        return _ab_finalize_built(
            shared_dir, st, bot_id, user_key, snap, network,
        )
    st.extracted["_ab_smoke_summary"] = str(smoke.get("summary") or "")
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_add_bot_smoke_failed(
            st.extracted, summary=st.extracted["_ab_smoke_summary"],
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_SMOKE_WRAP,
        completed=False,
    )


def _ab_finalize_built(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    snap: dict[str, Any],
    network: dict[str, Any] | None = None,
) -> TurnResult:
    """Terminal success: built, switched on, smoke check passed.

    M3 finalize defaults run here, in order (delta §5/§6/§7): queue the
    accepted starter-app builds (briefing folded in per the default-on
    decision), write the consent + tone ground rules to the bot's guide,
    and record the briefing decision on the new bot's network.json
    block. Every step is best-effort and reported honestly in the wrap —
    a failed default never un-builds the bot."""
    from . import provision_driver as _pdrv

    channel, sender_external_id = _parse_user_key(user_key)
    _try_audit(
        shared_dir,
        actor=_audit.api_actor(),
        action="add_bot_provisioned",
        bot_id=snap["account"],
        channel=channel or "<n/a>",
        from_external_id=None,
        to_external_id=sender_external_id or "<unknown>",
        reason=f"built via evo add-bot on {bot_id}; smoke check passed",
    )
    _pdrv.clear_job(bot_id, user_key)
    queued_info = _ab_run_finalize_defaults(shared_dir, st, user_key, network)
    st.current_phase = _phases.PHASE_AB_SMOKE_WRAP
    _state.mark_completed(shared_dir, st)
    return TurnResult(
        system_append=_prompts.render_add_bot_success_wrap(
            st.extracted, snap, queued=queued_info,
        ),
        wizard_session_id=None,
        phase=_phases.PHASE_AB_SMOKE_WRAP,
        completed=True,
        network_dirty=bool(queued_info.get("network_dirty")),
    )


def _handle_add_bot_smoke_wrap(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """The failed-smoke gate: the bot is built but didn't pass its final
    check. The admin picks: try the check again, or switch it off. A
    cancel counts as switch-off — never leave a broken bot running."""
    from . import provision_driver as _pdrv

    t = _ab_norm(user_message)
    account = str(st.extracted.get("_ab_account") or "")

    if t in _AB_RETRY_EXACT:
        smoke = _pdrv.run_smoke(account)
        if smoke.get("status") == "ok":
            job = _pdrv.get_job(bot_id, user_key)
            snap = job.snapshot() if job else {
                "account": account,
                "display_name": st.extracted.get("ab_bot_name") or account,
                "events": [],
                "credential_source": st.extracted.get(
                    "_ab_credential_choice") or "",
            }
            return _ab_finalize_built(
                shared_dir, st, bot_id, user_key, snap, network,
            )
        st.extracted["_ab_smoke_summary"] = str(smoke.get("summary") or "")
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_add_bot_smoke_failed(
                st.extracted, summary=st.extracted["_ab_smoke_summary"],
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_SMOKE_WRAP,
            completed=False,
        )

    if t in _AB_OFF_EXACT or t in _AB_CANCEL_EXACT or t in _AB_STOP_EXACT:
        ok, detail = _pdrv.quarantine(account)
        channel, sender_external_id = _parse_user_key(user_key)
        _try_audit(
            shared_dir,
            actor=_audit.api_actor(),
            action="add_bot_quarantined",
            bot_id=account,
            channel=channel or "<n/a>",
            from_external_id=None,
            to_external_id=sender_external_id or "<unknown>",
            reason=(
                f"smoke check failed after build; switch-off "
                f"{'succeeded' if ok else 'FAILED'}: {detail}"
            ),
        )
        _pdrv.clear_job(bot_id, user_key)
        from . import channel_driver as _chdrv
        _chdrv.clear_token(bot_id, user_key)
        st.current_phase = _phases.PHASE_AB_SMOKE_WRAP
        _state.mark_completed(shared_dir, st)
        return TurnResult(
            system_append=_prompts.render_add_bot_quarantined(
                st.extracted, ok=ok, detail=detail,
            ),
            wizard_session_id=None,
            phase=_phases.PHASE_AB_SMOKE_WRAP,
            completed=True,
        )

    return TurnResult(
        system_append=_prompts.render_add_bot_smoke_failed(
            st.extracted,
            summary=str(st.extracted.get("_ab_smoke_summary") or ""),
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_SMOKE_WRAP,
        completed=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add-bot M3 — starter apps, briefing default, finalize defaults
# Spec: internal/spec-add-bot-wizard-build-delta-2026-06-10.md §5 + §6 + §7.
# ─────────────────────────────────────────────────────────────────────────────


def _ab_seed_pack_proposal(
    shared_dir: Path,
    st: _state.WizardState,
    network: dict[str, Any] | None,
) -> None:
    """Pin the AB_SHAPE proposal in state: the role's starter pack
    resolved from the gallery, or the honest no-pack reason. Called on
    entry into AB_SHAPE and again if the role changes at the plan."""
    from . import pack as _pack

    archetype = _phases.normalize_archetype(st.extracted.get("ab_archetype"))
    sp = (
        _pack.load_starter_pack(archetype, shared_dir, network=network)
        if archetype else None
    )
    if sp is None or not sp.apps:
        reason = _pack.NO_PACK_REASONS.get(
            archetype or "", _pack.NO_PACK_FALLBACK,
        )
        st.extracted["_ab_pack"] = {
            "bundle_name": "",
            "apps": [],
            "missing": list(sp.missing) if sp else [],
            "none_reason": reason,
            "est_usd": 0.0,
            "est_minutes": 0,
        }
        st.extracted["ab_pack_apps"] = []
        return
    apps = [
        # identity: see resolve_app_id — the pack proposal names gallery packages to install; nothing is on the bot yet.
        {"pkg_id": a.pkg_id, "name": a.name, "blurb": a.blurb,
         "est_usd": a.est_usd}
        for a in sp.apps
    ]
    st.extracted["_ab_pack"] = {
        "bundle_name": sp.bundle_name,
        "apps": apps,
        "missing": list(sp.missing),
        "none_reason": "",
        "est_usd": _pack.estimate_usd(apps),
        "est_minutes": _pack.estimate_minutes(len(apps)),
    }
    st.extracted["ab_pack_apps"] = [dict(a) for a in apps]


def _ab_recompute_pack_estimate(st: _state.WizardState) -> None:
    """Refresh the cost/time preview after the selection changed."""
    from . import pack as _pack

    pack = st.extracted.get("_ab_pack") or {}
    sel = [
        a for a in (st.extracted.get("ab_pack_apps") or [])
        if isinstance(a, dict)
    ]
    pack["est_usd"] = _pack.estimate_usd(sel) if sel else 0.0
    pack["est_minutes"] = _pack.estimate_minutes(len(sel)) if sel else 0
    st.extracted["_ab_pack"] = pack


# Reply vocabulary for AB_SHAPE / AB_BRIEFING. Exact-match for the
# whole-message sets; token sets for verbs. "no thanks" lives in the
# decline sets AND in _AB_CANCEL_EXACT — the handlers check decline
# first, so at these gates it means "no to the offer", never "abort".
_AB_SHAPE_NONE_EXACT = frozenset({
    "no", "none", "nope", "no apps", "skip", "skip it", "skip the apps",
    "skip apps", "no thanks", "not now", "nothing", "no apps for now",
    "without apps", "start with none",
})
_AB_SHAPE_DROP_VERBS = frozenset({
    "drop", "remove", "without", "minus", "lose", "cut", "ditch",
    "except", "skip",
})
_AB_SHAPE_ADD_VERBS = frozenset({
    "add", "include", "keep", "also", "plus", "want", "back",
})
_AB_NEG_TOKENS = frozenset({"don't", "dont", "not", "no", "never"})
_AB_KEEP_ONLY_TOKENS = frozenset({"just", "only"})
_AB_QUESTION_TOKENS = frozenset({
    "what", "what's", "whats", "how", "why", "which", "does", "do",
    "is", "are", "can", "tell", "explain",
})
_AB_SHAPE_FILLER_TOKENS = frozenset({
    "the", "and", "a", "an", "app", "apps", "please", "one", "ones",
    "set", "those", "these", "them", "it", "me", "give", "take",
})

_AB_BRIEFING_NO_EXACT = frozenset({
    "no", "nope", "no thanks", "no thank you", "skip", "skip it",
    "not now", "no briefing", "none", "turn it off", "off", "opt out",
    "i'll pass", "pass",
})
_AB_BRIEFING_YES_TOKENS = frozenset({
    "yes", "yep", "yeah", "sure", "want", "keep", "ok", "okay",
    "definitely", "please",
})


def _ab_cancelled_terminal(
    shared_dir: Path, st: _state.WizardState, *, phase: str,
) -> TurnResult:
    """Terminal cancel during the planning half — nothing is saved
    before the plan confirm, so the cancelled render is exact."""
    # Drop any stashed channel token with the session — a cancelled
    # plan must leave no secret behind in the holding area.
    from . import channel_driver as _chdrv
    _chdrv.clear_token(st.bot_id, st.user_key)
    st.current_phase = phase
    _state.mark_completed(shared_dir, st)
    body = _prompts.render_add_bot_cancelled()
    return TurnResult(
        system_append=_verbatim_wrap(body),
        wizard_session_id=None,
        phase=phase,
        completed=True,
        direct_send_message=body,
    )


def _ab_norm_name(text: str) -> str:
    """Hyphens/underscores → spaces so "note-taker" and "note taker"
    compare equal."""
    return re.sub(r"[-_]+", " ", (text or "").lower())


def _ab_match_apps(apps: list[dict], text: str, tokens: set[str]) -> list[dict]:
    """Apps the reply references. Full-name matches win outright — "drop
    the pre-meeting brief" must hit Pre-Meeting Brief only, never also
    Meeting Note-taker via the shared word "meeting". Only when no full
    name appears does the looser word-token pass run ("drop the taker")."""
    norm_text = _ab_norm_name(text)
    full = [
        a for a in apps
        if _ab_norm_name(str(a.get("name") or "")) in norm_text
    ]
    if full:
        return full
    out = []
    for a in apps:
        words = re.findall(r"[a-z0-9]+", _ab_norm_name(str(a.get("name") or "")))
        if any(len(w) >= 4 and w in tokens for w in words):
            out.append(a)
    return out


def _ab_advance_past_shape(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Selection settled — move to the briefing offer, or straight to
    consent when no surface was captured (delta §3.2: the briefing turn
    is only offered when the bot has a place to deliver to)."""
    if str(st.extracted.get("ab_surface") or "").strip():
        st.current_phase = _phases.PHASE_AB_BRIEFING
    else:
        st.extracted["ab_briefing"] = "off"
        st.current_phase = _phases.PHASE_AB_CONSENT_TONE
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


def _ab_shape_reprompt(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    note: str | None = None,
) -> TurnResult:
    extra = {"shape_note": note} if note else None
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id, extra_ctx=extra,
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_SHAPE,
        completed=False,
    )


def _handle_add_bot_shape(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Classify the admin's starter-apps answer — accept / none /
    adjust-by-name / cancel — deterministically. The proposal universe
    is a fixed list, so name matching beats LLM extraction; anything
    that doesn't parse re-renders the proposal and lets the LLM answer
    questions conversationally."""
    raw = user_message or ""
    t = _ab_norm(raw)
    pack = st.extracted.get("_ab_pack") or {}

    # Decline-the-offer vocabulary outranks the cancel set ("no thanks"
    # here means no apps, not abort).
    if t in _AB_SHAPE_NONE_EXACT:
        st.extracted["ab_pack_apps"] = []
        _ab_recompute_pack_estimate(st)
        return _ab_advance_past_shape(shared_dir, st, bot_id, user_key)
    if t in _AB_CANCEL_EXACT:
        return _ab_cancelled_terminal(
            shared_dir, st, phase=_phases.PHASE_AB_SHAPE,
        )

    # No pack for this role — the render was the honest "no starter set"
    # turn; any non-cancel reply moves along.
    if pack.get("none_reason") or not (pack.get("apps") or []):
        st.extracted["ab_pack_apps"] = []
        return _ab_advance_past_shape(shared_dir, st, bot_id, user_key)

    if t in _AB_CONFIRM_EXACT:
        return _ab_advance_past_shape(shared_dir, st, bot_id, user_key)

    # Questions go back to the agenda LLM (it has the app list).
    tokens = set(re.findall(r"[a-z0-9']+", t))
    if "?" in raw or (tokens & _AB_QUESTION_TOKENS):
        return _ab_shape_reprompt(shared_dir, st, bot_id, user_key)

    universe = [a for a in pack["apps"] if isinstance(a, dict)]
    selection = {
        # identity: see resolve_app_id — the add-bot selection map is keyed by gallery package key, the value an install is dispatched with.
        str(a.get("pkg_id")): a
        for a in (st.extracted.get("ab_pack_apps") or [])
        if isinstance(a, dict)
    }
    matched = _ab_match_apps(universe, t, tokens)
    if not matched:
        return _ab_shape_reprompt(shared_dir, st, bot_id, user_key)

    neg = bool(tokens & _AB_NEG_TOKENS)
    keep_only = bool(tokens & _AB_KEEP_ONLY_TOKENS)
    drop = bool(tokens & _AB_SHAPE_DROP_VERBS)
    add = bool(tokens & _AB_SHAPE_ADD_VERBS)
    if neg:
        for a in matched:
            selection.pop(str(a.get("pkg_id")), None)
    elif keep_only:
        selection = {str(a.get("pkg_id")): dict(a) for a in matched}
    elif drop and not add:
        for a in matched:
            selection.pop(str(a.get("pkg_id")), None)
    elif add and not drop:
        for a in matched:
            selection[str(a.get("pkg_id"))] = dict(a)
    elif drop and add:
        return _ab_shape_reprompt(
            shared_dir, st, bot_id, user_key,
            "One change at a time, please — tell me what to drop, or "
            "what to add.",
        )
    else:
        # Bare names, no verbs: "keep exactly these" — but only when the
        # message is essentially just the names, so a sentence that
        # happens to mention one app never silently collapses the set.
        name_words = set()
        for a in matched:
            name_words |= set(re.findall(
                r"[a-z0-9]+", str(a.get("name") or "").lower(),
            ))
        leftovers = {
            w for w in re.findall(r"[a-z0-9]+", t)
            if w not in name_words and w not in _AB_SHAPE_FILLER_TOKENS
        }
        if leftovers:
            return _ab_shape_reprompt(shared_dir, st, bot_id, user_key)
        selection = {str(a.get("pkg_id")): dict(a) for a in matched}

    ordered = [
        dict(a) for a in universe if str(a.get("pkg_id")) in selection
    ]
    st.extracted["ab_pack_apps"] = ordered
    _ab_recompute_pack_estimate(st)
    _state.write_state(shared_dir, st)
    names = ", ".join(str(a.get("name")) for a in ordered) or "none"
    return _ab_shape_reprompt(
        shared_dir, st, bot_id, user_key,
        f"Updated — the set is now: {names}. A yes locks it in; they "
        "can keep adjusting or say none.",
    )


def _handle_add_bot_briefing(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """The explicit opt-out turn for the morning-briefing default
    (delta §6): a clear yes or no, decline accepted gracefully."""
    raw = user_message or ""
    t = _ab_norm(raw)
    tokens = set(re.findall(r"[a-z0-9']+", t))

    # Decline outranks cancel ("no thanks" means no briefing).
    declined = t in _AB_BRIEFING_NO_EXACT or (
        bool(tokens & _AB_NEG_TOKENS)
        and not (tokens & _AB_BRIEFING_YES_TOKENS)
    )
    if declined:
        st.extracted["ab_briefing"] = "off"
    elif t in _AB_CANCEL_EXACT:
        return _ab_cancelled_terminal(
            shared_dir, st, phase=_phases.PHASE_AB_BRIEFING,
        )
    elif t in _AB_CONFIRM_EXACT or (
        (tokens & _AB_BRIEFING_YES_TOKENS)
        and not (tokens & _AB_NEG_TOKENS)
    ):
        st.extracted["ab_briefing"] = "on"
    else:
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"briefing_note": (
                    "A simple yes or no works here — want the morning "
                    "briefing?"
                )},
            ),
            wizard_session_id=user_key,
            phase=_phases.PHASE_AB_BRIEFING,
            completed=False,
        )

    return _ab_advance_past_briefing(shared_dir, st, bot_id, user_key)


def _ab_channel_offer_kind(extracted: dict[str, Any]) -> str:
    """Which in-conversation channel connect the captured surface
    supports — ``"telegram"`` (the token-paste connect that exists
    today) or ``""`` (no offer; the wrap's auto-activate copy covers
    connecting later from the admin page)."""
    surface = str(extracted.get("ab_surface") or "").lower()
    return "telegram" if "telegram" in surface else ""


def _ab_advance_past_briefing(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
) -> TurnResult:
    """Briefing decided — offer the channel connect when the captured
    surface supports one in-conversation (offer-now half of the
    2026-06-11 decision), otherwise straight to consent."""
    if _ab_channel_offer_kind(st.extracted):
        st.current_phase = _phases.PHASE_AB_CHANNEL
    else:
        st.current_phase = _phases.PHASE_AB_CONSENT_TONE
    _state.write_state(shared_dir, st)
    return TurnResult(
        system_append=_build_prompt(shared_dir, st, bot_id),
        wizard_session_id=user_key,
        phase=st.current_phase,
        completed=False,
    )


# A pasted Telegram bot token (BotFather shape) — matched against the
# RAW message, same as the API key at AB_CREDENTIALS. The shape mirrors
# telegram_install._TOKEN_PATTERN without the full-string anchors.
_AB_TG_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_\-]{30,}")

# Skip vocabulary for the channel offer. "no" means "no token right
# now", never abort — decline outranks cancel, like the briefing turn.
_AB_CHANNEL_SKIP_EXACT = frozenset({
    "skip", "skip it", "skip for now", "later", "not now", "no",
    "nope", "no token", "don't have it", "dont have it",
    "i don't have it", "i dont have it", "not yet", "maybe later",
    "no thanks", "no thank you",
})


def _handle_add_bot_channel(
    shared_dir: Path,
    st: _state.WizardState,
    bot_id: str,
    user_key: str,
    user_message: str,
    network: dict[str, Any] | None,
) -> TurnResult:
    """Classify the channel-offer answer — pasted token / skip / cancel
    — deterministically. A valid token is verified live (Telegram's
    getMe) and stashed in memory for finalize; it never enters the
    state file and never round-trips through LLM extraction. Skipping
    is a first-class answer: the briefing auto-activates when a channel
    is connected later."""
    from . import channel_driver as _chdrv

    raw = user_message or ""
    t = _ab_norm(raw)

    m = _AB_TG_TOKEN_RE.search(raw)
    if m:
        token = m.group(0)
        result = _chdrv.verify_token(token)
        if not result.get("ok"):
            return TurnResult(
                system_append=_build_prompt(
                    shared_dir, st, bot_id,
                    extra_ctx={"channel_note": (
                        "Telegram didn't accept that token — check you "
                        "copied the whole line from @BotFather, paste it "
                        "again, or say skip."
                    )},
                ),
                wizard_session_id=user_key,
                phase=_phases.PHASE_AB_CHANNEL,
                completed=False,
            )
        _chdrv.stash_token(bot_id, user_key, channel="telegram", token=token)
        st.extracted["ab_channel_connect"] = "ready"
        st.extracted["_ab_channel"] = "telegram"
        username = str(result.get("bot_username") or "").strip()
        if username:
            st.extracted["_ab_channel_username"] = username
        st.current_phase = _phases.PHASE_AB_CONSENT_TONE
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(
                shared_dir, st, bot_id,
                extra_ctx={"channel_ack": (
                    "The Telegram token checked out — acknowledge briefly "
                    "that you'll hook it up as soon as the bot is built, "
                    "then move on."
                )},
            ),
            wizard_session_id=user_key,
            phase=st.current_phase,
            completed=False,
        )

    # Decline outranks cancel ("no" means no token right now).
    if t in _AB_CHANNEL_SKIP_EXACT or t in _AB_SKIP_EXACT:
        st.extracted["ab_channel_connect"] = "skipped"
        st.current_phase = _phases.PHASE_AB_CONSENT_TONE
        _state.write_state(shared_dir, st)
        return TurnResult(
            system_append=_build_prompt(shared_dir, st, bot_id),
            wizard_session_id=user_key,
            phase=st.current_phase,
            completed=False,
        )
    if t in _AB_CANCEL_EXACT:
        return _ab_cancelled_terminal(
            shared_dir, st, phase=_phases.PHASE_AB_CHANNEL,
        )

    # Anything else — likely a question; the agenda LLM answers and
    # re-asks (it has the offer copy).
    return TurnResult(
        system_append=_build_prompt(
            shared_dir, st, bot_id,
            extra_ctx={"channel_note": (
                "A pasted token or a skip works here — paste the "
                "@BotFather token, or say skip to connect later."
            )},
        ),
        wizard_session_id=user_key,
        phase=_phases.PHASE_AB_CHANNEL,
        completed=False,
    )


# Gallery surface slugs for the audience-scoping seed — the same open
# vocabulary the manifest schema's GROUP_CHANNELS uses.
_AB_SURFACE_SLUGS: tuple[tuple[str, str], ...] = (
    ("telegram", "telegram_group"),
    ("slack", "slack_channel"),
    ("discord", "discord_channel"),
)


def _ab_surface_slugs(extracted: dict[str, Any]) -> list[str]:
    surface = str(extracted.get("ab_surface") or "").lower()
    return [slug for key, slug in _AB_SURFACE_SLUGS if key in surface]


def _ab_write_bot_guide(
    shared_dir: Path,
    st: _state.WizardState,
    account: str,
    user_key: str,
) -> None:
    """Record the consent posture + tone in the new bot's guide — the
    file the bot reads at every session start and the secondary wizard
    shows to its users (delta §7: conduct/guide files are the prose
    home; the manifest privacy blocks on installed apps are seeded
    separately)."""
    from .. import guide as _guide

    ex = st.extracted
    frontmatter: dict[str, Any] = {}
    audiences = ex.get("ab_audiences")
    if isinstance(audiences, list) and audiences:
        frontmatter["audience"] = ", ".join(str(a) for a in audiences)
    tone = str(ex.get("ab_tone") or "").strip()
    if tone:
        frontmatter["tone"] = tone

    name = str(ex.get("ab_bot_name") or account).strip()
    body_lines = [f"# {name} — ground rules", ""]
    mission = str(ex.get("ab_mission") or "").strip()
    if mission:
        body_lines += [mission, ""]
    consent = str(ex.get("ab_consent_notice") or "").strip()
    if consent:
        body_lines.append(f"What people are told: {consent}")
    opt_out = str(ex.get("ab_opt_out") or "").strip()
    if opt_out:
        body_lines.append(f"How to opt out: {opt_out}")
    tg = str(ex.get("ab_tg_privacy") or "").strip()
    if tg:
        body_lines.append(f"What it sees in the group: {tg}")
    _guide.write_guide(
        shared_dir,
        account,
        frontmatter=frontmatter,
        body="\n".join(body_lines).strip() + "\n",
        authored_by="pod admin (via evo add-bot)",
    )


def _ab_run_finalize_defaults(
    shared_dir: Path,
    st: _state.WizardState,
    user_key: str,
    network: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the M3 finalize defaults for a freshly built bot and report
    what happened, honestly, for the success wrap:

      * queue the accepted starter-app installs (sequential, with the
        consent answers seeded onto each app's privacy block — manifest
        v24 made the blocks first-class, closing the double-write
        window the delta flagged);
      * fold the briefing default in: on → ensure the briefing app (and
        its calendar foundation) is queued; off → drop it from the set;
      * write the consent + tone ground rules to the bot's guide;
      * record the briefing decision on the bot's network.json block.
    """
    from . import pack as _pack
    from . import pack_driver as _packdrv

    ex = st.extracted
    account = str(ex.get("_ab_account") or "")
    briefing = str(ex.get("ab_briefing") or "")
    apps = [
        dict(a) for a in (ex.get("ab_pack_apps") or [])
        # identity: see resolve_app_id — finalize compares the selection against the pack CONSTANTS, both gallery package keys.
        if isinstance(a, dict) and a.get("pkg_id")
    ]
    if briefing == "on":
        have = {str(a.get("pkg_id")) for a in apps}
        if _pack.MORNING_BRIEFING_PKG_ID not in have:
            # Foundation before behavior — calendar feeds the briefing.
            if _pack.CALENDAR_SYNC_PKG_ID not in have:
                apps.append({
                    "pkg_id": _pack.CALENDAR_SYNC_PKG_ID,
                    "name": "Calendar Sync",
                })
            apps.append({
                "pkg_id": _pack.MORNING_BRIEFING_PKG_ID,
                "name": "Morning Briefing",
            })
    elif briefing == "off":
        apps = [
            a for a in apps
            if str(a.get("pkg_id")) != _pack.MORNING_BRIEFING_PKG_ID
        ]

    privacy_seed: dict[str, Any] = {}
    consent = str(ex.get("ab_consent_notice") or "").strip()
    if consent:
        privacy_seed["consent_notice"] = consent
    opt_out = str(ex.get("ab_opt_out") or "").strip()
    if opt_out:
        privacy_seed["opt_out_command"] = opt_out
    scoping_seed: dict[str, Any] | None = None
    if _phases.ab_group_audience(ex):
        scoping_seed = {
            "operator": "open",
            "approved_surfaces": _ab_surface_slugs(ex),
            "role_capabilities": {
                "operator_only": ["read", "write", "configure"],
                "audience": ["read"],
            },
            "operator_bypasses": [],
        }

    queued: list[Any] = []
    queue_crashed = False
    if account and apps:
        try:
            queued = _packdrv.queue_pack_installs(
                shared_dir,
                bot_id=account,
                apps=apps,
                privacy_seed=privacy_seed or None,
                audience_scoping_seed=scoping_seed,
                network=network,
                # M4 finding 8b: the captured mission (and the decided
                # briefing time) reach any build spec that declares the
                # matching {placeholder} — the briefing's day-one
                # no-data message reads config.mission.
                config_seed={
                    "mission": str(ex.get("ab_mission") or ""),
                    "delivery_time": "07:00",
                },
            )
        except Exception:
            queue_crashed = True

    guide_written = guide_failed = False
    tone = str(ex.get("ab_tone") or "").strip()
    if account and (consent or tone):
        try:
            _ab_write_bot_guide(shared_dir, st, account, user_key)
            guide_written = True
        except Exception:
            guide_failed = True

    network_dirty = False
    if account and briefing in ("on", "off") and isinstance(network, dict):
        bots = network.get("bots")
        block = bots.get(account) if isinstance(bots, dict) else None
        if isinstance(block, dict):
            from evolve_util import now_iso as _now_iso
            record: dict[str, Any] = {
                "enabled": briefing == "on",
                "decided_at": _now_iso(),
            }
            if briefing == "on":
                record["time"] = "07:00"
            block["briefing"] = record
            network_dirty = True

    # Channel connect (offer-now half, 2026-06-11 decision). Applied
    # AFTER the installs queue: the openclaw.json write fires the
    # channel-registration hook, and the already-queued briefing job is
    # what makes that hook's in-flight check a clean no-op. Honest
    # outcomes only — connected / failed / lost (daemon restart ate the
    # in-memory token) / skipped — the wrap renders each plainly.
    from . import channel_driver as _chdrv

    channel_report: dict[str, Any] = {"status": "none"}
    if account:
        try:
            channel_report = _chdrv.apply_stashed_channel(
                bot_id=st.bot_id or "", user_key=user_key, account=account,
                extracted=ex, network=network,
            )
        except Exception as exc:  # noqa: BLE001
            channel_report = {
                "status": "failed",
                "channel": str(ex.get("_ab_channel") or ""),
                "recipient_recorded": False,
                "network_dirty": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        if channel_report.get("network_dirty"):
            network_dirty = True

    ok_names = [q.name for q in queued if q.ok]
    failed_names = [q.name for q in queued if not q.ok]
    if queue_crashed:
        failed_names = [str(a.get("name") or a.get("pkg_id")) for a in apps]
    return {
        "apps": ok_names,
        "apps_failed": failed_names,
        "est_minutes": _pack.estimate_minutes(len(ok_names)) if ok_names else 0,
        "briefing": briefing,
        "guide_written": guide_written,
        "guide_failed": guide_failed,
        "network_dirty": network_dirty,
        "channel": channel_report,
    }
