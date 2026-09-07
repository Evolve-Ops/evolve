"""home_chat_routes — Flask routes for the Home conversational chat.

Two endpoints:

    POST /api/home/chat
    body: {message: str, history?: [{role, text}]}
    flow: dispatcher first → Haiku fallback for free-form prose.

    GET /api/home/narrative
    query: ?refresh=1 to bypass cache
    flow: read cache → return if fresh; else generate via Haiku + write
    cache. Cache key is SHA1 of the pod-state digest so anything
    changing in firing signals / proposals / bot status invalidates.

Caps both LLM paths against the shared ``DAILY_CALL_CAP`` per pod;
over-cap responses surface as the dispatcher reply (chat) or empty
text (narrative) so the UI keeps the rule-based fallback visible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
from datetime import datetime, timezone
import traceback
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, make_response, request

from . import home_chat as _hc

# Same pattern as evo_routes — load network + dispatcher lazily so the
# admin server can import even when analyzer paths are missing.
log = logging.getLogger(__name__)

# Body-supplied session_id constraint. OC uses session_id as the JSONL
# filename under shared_dir, so path-traversal / glob / unicode quirks
# would let a malicious client write outside the session directory.
# Constrain to plain ASCII letters / digits / dot / dash / underscore,
# 1-128 chars — wide enough to fit the multi-session UI's ids
# (``admin-ui-home-s1abc...``) without inviting filesystem surprises.
_SAFE_SESSION_ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,128}$"
_SAFE_SESSION_ID_RE = re.compile(_SAFE_SESSION_ID_PATTERN)

# Browser-stable anon-fallback cookie. Set on first /api/home/chat hit
# that lacks a client-provided ``session_id`` so the next turn from the
# same browser lands on the SAME OC session. Defense-in-depth for
# the #1367 session-memory-loss bug: even if the client temporarily
# stops sending ``session_id`` (cache invalidation, legacy state),
# consecutive turns share a thread instead of fragmenting into
# fresh ``admin-ui-anon-<uuid>`` sessions per request. ``HttpOnly``
# so JS can't accidentally clobber it; ``SameSite=Lax`` because the
# admin UI is same-origin only.
_ADMIN_UI_SESSION_COOKIE = "admin_ui_session"
_COOKIE_MAX_AGE_S = 60 * 60 * 24 * 365  # 1 year — anon thread continuity
# Cookie value is 16 hex chars (8 random bytes) — enough entropy for
# per-browser isolation on a single-operator pod, small enough to keep
# the resulting ``admin-ui-anon-<value>`` session id short.
_COOKIE_VALUE_BYTES = 8


#: Ceiling applied to ``dailyCap`` when ``tiers.json`` is NOT owned by a
#: trusted (operator-side) uid. Deliberately the same number as the
#: no-file default, so an untrusted file can never push the cap ABOVE
#: the product default. It can still land anywhere at or below it —
#: including 10 itself, so this bounds the magnitude of a tamper, it
#: does NOT preserve the operator's value. See the limitation paragraph
#: in ``_read_user_tier_override``.
_UNTRUSTED_DAILY_CAP_CEILING = 10

#: Cap on how much of ``tiers.json`` is read. The file is a small config
#: blob (``userTierOverride``, ``cascade``, ``tier*``); a bot that can
#: replace it could otherwise point the reader at something unbounded.
_TIERS_READ_MAX_BYTES = 1 << 20


def _read_tiers_file(path: Path) -> "tuple[str | None, int | None]":
    """Read ``tiers.json`` as ``(text, owner_uid)`` without following links.

    ONE ``O_NOFOLLOW``-anchored open serves both the content and the
    ownership check, so there is no stat-then-open window in which the
    entry could be swapped, and the uid returned is provably the uid of
    the bytes returned.

    Why not ``Path.stat()`` / ``Path.read_text()`` (the shape this
    started as, caught in review): both follow symlinks, and the bot
    holds ``add_file`` on this directory — verified, an unprivileged
    account can plant ``tiers.json -> /etc/hosts`` under the #3565 ACE
    and make ``stat().st_uid`` report **0**, i.e. the most-trusted uid
    in :func:`_trusted_uids`. ``O_NOFOLLOW`` refuses the open with
    ``ELOOP`` instead. This is the same lesson
    ``tier_prefs_acl._resolve_bot_dir`` (#3571) records two files away:
    on this surface, never a call that follows.

    ``O_NONBLOCK`` + an ``S_ISREG`` assertion cover the other thing the
    ACE permits: ``mkfifo tiers.json`` (verified plantable) would
    otherwise block ``read_text()`` forever inside a Flask worker on
    ``/api/home/chat``.

    Returns ``(None, None)`` for every failure — missing file, symlink,
    FIFO/device, EACCES, undecodable bytes. Never raises.
    """
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, None
        raw = b""
        while len(raw) < _TIERS_READ_MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        return raw.decode("utf-8"), st.st_uid
    except (OSError, UnicodeDecodeError):
        return None, None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                # Nothing to recover from an EBADF on close, but don't go
                # silent — a leaked fd in a long-lived Flask worker matters.
                log.debug("closing tiers.json fd failed", exc_info=True)


def _trusted_uids() -> "set[int] | None":
    """uids whose writes to ``tiers.json`` are operator-authoritative.

    The legitimate writers are ``evolve-admin models cap`` /
    ``models user-tier-control`` (``cli.py``), which run either as the
    admin-server user (this process) or as root under
    ``sudo evolve-admin``. Nothing bot-side writes this file — the
    plugin only READS it (``ModelRouter``/``TurnObserver`` treat
    ``{sharedDir}/{botId}/tiers.json`` as a legacy fallback source).

    Returns ``None`` where the platform has no ``getuid`` (i.e. the
    question is unanswerable). Callers must then skip the trust gate
    rather than treat every file as untrusted — failing closed there
    would clamp every pod's cap forever on a platform the admin server
    doesn't even run on.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    return {0, getuid()}


def _read_user_tier_override(shared_dir: Path, bot_id: str) -> dict:
    """Read ``{shared_dir}/{bot_id}/tiers.json::userTierOverride`` and
    return a defaults-filled dict:

      {"enabled": bool, "dailyCap": int}

    internal/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out +
    adjustable daily cap". Both fields are operator-tunable per-bot.

    Defaults (when file/section missing):
      enabled  → True   (chip renders)
      dailyCap → 10     (per network.json::tier1.maxPerDayPerBot legacy
                         default; matches DEFAULT_TIERS in models.py)

    Never raises — any I/O or JSON failure returns the defaults. The
    feature must work on pods that don't yet have a tiers.json file
    written.

    TRUST NOTE (#3566 audit A-1). ``dailyCap`` here is not decoration:
    the Power gate in ``/api/home/chat`` downgrades Power → standard
    once today's tier1 usage reaches it, and ``dailyCap: 0`` is the
    operator's "Power disabled for this bot" opt-out. Unlike the Max
    gate — whose ceiling comes from ``network.json::models.roleCaps.max``,
    which no bot can write — this ceiling comes from a file in a
    directory the bot holds a write ACE on: PR #3565 granted the bot
    ``add_file,delete_child`` on ``{sharedDir}/{botId}/``, and a macOS
    ACL names a DIRECTORY, not a file, so the same-dir temp + rename
    that grant exists for also replaces ``tiers.json`` (reproduced).

    So the file is trusted only when its owner is, established by
    :func:`_read_tiers_file`'s ``O_NOFOLLOW`` fstat. A ``tiers.json``
    whose owner is not in :func:`_trusted_uids` is still read — dropping
    it entirely would let a bot CANCEL its own opt-out just by
    corrupting the file — but its ``dailyCap`` may not exceed
    :data:`_UNTRUSTED_DAILY_CAP_CEILING`.

    WHAT THIS DOES AND DOES NOT BUY (stated precisely, because an
    imprecise version of this same claim is what made A-1 get filed as a
    nit in the first place). The clamp is one-way relative to the
    PRODUCT DEFAULT, not relative to operator intent:

    * it does bound the magnitude — a bot that forges ``dailyCap: 100``
      gets 10, so it cannot buy itself 10× the Power spend;
    * it does NOT preserve the operator's value. The operator's setting
      lives ONLY in this file, so a replacement destroys it. An operator
      who set the documented ``{enabled: false, dailyCap: 0}`` opt-out
      and then had the file clobbered ends up at ``{enabled: true,
      dailyCap: 10}`` — a real 0 → 10 widening of exactly the control
      A-1 is about.

    Closing that gap needs the operator's value to live somewhere the
    bot cannot write. Recorded as the follow-up on the
    ``tier_prefs_acl`` module docstring, together with why the ACE
    itself is not narrowed here.
    """
    out = {"enabled": True, "dailyCap": 10}
    if not bot_id:
        return out
    tiers_path = shared_dir / bot_id / "tiers.json"
    text, uid = _read_tiers_file(tiers_path)
    if text is None:
        return out
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return out
    trusted_uids = _trusted_uids()
    # ``None`` = platform can't answer; skip the gate rather than clamp
    # every pod forever. See _trusted_uids.
    trusted = trusted_uids is None or (uid is not None and uid in trusted_uids)

    override = (data or {}).get("userTierOverride") or {}
    if isinstance(override.get("enabled"), bool):
        out["enabled"] = override["enabled"]
    cap = override.get("dailyCap")
    # bool is a subclass of int — ``dailyCap: true`` must not read as 1.
    # routes_admin_config validates the same field the same way.
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and 0 <= cap <= 100:
        out["dailyCap"] = int(cap)

    if not trusted and out["dailyCap"] > _UNTRUSTED_DAILY_CAP_CEILING:
        # Log only when the clamp actually BITES. This reader runs on
        # every Power-tier chat turn and every tier-config poll, so an
        # unconditional warning would grow admin-ui.err.log without
        # bound — and would misreport "clamping" on a mis-owned file
        # whose cap was already under the ceiling.
        log.warning(
            "tiers.json for bot %s is owned by uid %s, not an operator uid "
            "(%s) — clamping dailyCap %d → %d. A non-operator-owned "
            "tiers.json means the per-bot shared-dir write ACE (#3565) was "
            "used to replace an operator-authored file; inspect `ls -l %s`.",
            bot_id, uid, sorted(trusted_uids or ()),
            out["dailyCap"], _UNTRUSTED_DAILY_CAP_CEILING, tiers_path,
        )
        out["dailyCap"] = _UNTRUSTED_DAILY_CAP_CEILING
    return out


def register_home_chat_routes(
    app: Flask, network_path: Path
) -> None:
    """Mount the /api/home/chat endpoint."""
    from ..config import load_network as _load_network
    from ..evo import dispatch as _dispatch

    def _shared_dir() -> Path:
        return Path(
            _load_network(network_path).get(
                "sharedDir", "/Users/Shared/evolve"
            )
        )

    @app.post("/api/home/chat")
    def api_home_chat():
        """Route the admin UI chat through evo's OC gateway.

        Phase 4.1 of internal/spec-evo-oc-native-2026-05-19.md: the chat is
        no longer answered by an admin-side Haiku call. Every message
        forwards to evo's actual agent via the proxy
        (``evolve_admin.evo.proxy.send_to_evo``), so the operator
        always talks to the same model that handles Telegram, with
        the same SOUL/MEMORY/TOOLS context + the same 12 tools shipped
        in #1273/#1279/#1282.

        Page context: when the JS frontend includes a ``page_context``
        block, the proxy wraps it as a ``<page-context>`` XML block at
        the top of the user message. evo's AGENTS.md teaches the model
        how to read that block — see "Page Context" in
        ``packages/analyzer/evolve_bot/AGENTS.md``.

        Response shape stays compatible with the legacy frontend:
        ``{reply, source, session_id, model?}``. The legacy
        ``suggested_actions`` field is no longer populated — Phase 4.2
        will replace it with proper tool-call confirmation buttons.
        ``cap_status`` is dropped because OC has its own cost ledger
        + budget guard; the parallel home_chat usage cap is removed.
        """
        from ..evo.proxy import (
            ProxyResult, derive_session_id, read_thread_stats, send_to_evo,
        )

        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        page_context = body.get("page_context") or {}
        if not isinstance(page_context, dict):
            page_context = {}
        # PWA Phase 1.1.B: the drop/paste primitive uploads files via
        # /api/chat-uploads first and forwards their metadata here so
        # evo sees a textual reference at the top of the user message.
        # OC's read_file tool can pull the bytes by path if needed.
        attachments_in = body.get("attachments") or []
        if not isinstance(attachments_in, list):
            attachments_in = []
        attachments_in = [a for a in attachments_in if isinstance(a, dict)][:16]
        # Authority tier ("ask" / "auto-small" / "auto"). Phase 4.1
        # doesn't gate tool calls yet (Phase 4.2 work). We still echo
        # the tier back so the frontend's existing display logic doesn't
        # break. Validated for hygiene only.
        authority = str(body.get("authority") or "ask").lower()
        if authority not in {"ask", "auto-small", "auto"}:
            authority = "ask"
        # Operator tier preference (Auto / Fast / Standard / Power) —
        # internal/spec-user-tier-control-2026-05-26.md. Per-turn, not
        # persisted: frontend sends with each message, server validates
        # + plumbs to the plugin via EVOLVE_TIER_PREFERENCE on the
        # subprocess env. Unknown values fall back to "auto" rather
        # than to a specific tier — the operator's intent on a
        # malformed value is unknown, let the classifier decide.
        tier_pref = str(body.get("tier") or "auto").lower()
        if tier_pref not in {"auto", "fast", "standard", "power", "max"}:
            tier_pref = "auto"
        # tier_capped: set True iff Power was downgraded because the
        # tier1 daily cap is exhausted for the primary bot today.
        # Computed below once we have network + primary bot id.
        tier_capped = False
        if not message and not attachments_in:
            return jsonify({"error": "message is required"}), 400

        # OC reserves leading-slash text as control commands (``/approve``,
        # ``/grant``, ``/reload``, etc.). The admin-UI chat is a tool-calling
        # surface, never a control-plane surface. Reject leading-slash sends
        # with an explanatory message — the operator's intent is almost
        # certainly to invoke a tool, not to drive OC's control plane.
        #
        # Defense-in-depth Fix 4 from
        # internal/diagnosis-evo-exec-approval-leak-2026-05-21.md: closes the
        # ``/approve <id>`` side-channel that lets the operator drive OC's
        # exec-approvals workflow from the admin-UI chat. Phase E.4
        # (2026-05-25) removed the primary-bot exec-deny carve-out but
        # this guard stays in place: the admin-UI chat is for tool calls,
        # not control-plane, regardless of evo's exec mode.
        # Only rejects when the LSTRIPPED message starts with a SINGLE ``/``
        # — guards against false positives like "in /tmp the file is..."
        # (slash mid-sentence is fine) and ``//comment`` (not OC slash-
        # commands; let those pass).
        if _is_leading_slash_command(message):
            return jsonify({
                "source": "evo",
                "reply": (
                    "That looks like an OpenClaw control command "
                    "(`/approve` etc.). This chat doesn't route those — it "
                    "only calls registered evo tools. If you're trying to "
                    "approve something, look for the confirmation button "
                    "below the message that prompted it, or ask me in "
                    "plain English."
                ),
                "session_id": "",
                "authority": authority,
                # Echo tier so the frontend's composer stays in sync
                # even when the request short-circuits — see
                # internal/spec-user-tier-control-2026-05-26.md §UI.
                "tier": tier_pref,
                "effective_tier": tier_pref,
                "tier_capped": False,
            })

        # Per-page session id — each admin UI page keeps its own
        # conversation thread with evo (spec §3.5). MEMORY.md is shared
        # across sessions; short-term context is not. An operator-
        # supplied session_id wins (lets the frontend pin a thread per
        # browser-side conversation; needed for the Chat page's
        # multi-session UI added in #1339 so each session gets its own
        # OC memory instead of all of them sharing 'admin-ui-home').
        page_id = (page_context.get("page_id") or "").strip() or None
        raw_session_id = (body.get("session_id") or "").strip()
        if raw_session_id and not _SAFE_SESSION_ID_RE.fullmatch(raw_session_id):
            # Untrusted value — OC uses session_id as the JSONL filename
            # under shared_dir, so path-traversal / glob / unicode quirks
            # would let a malicious client write outside the session
            # directory. Reject loudly rather than silently fall back so
            # the frontend bug surfaces.
            return jsonify({
                "error": (
                    "invalid session_id; must match "
                    f"{_SAFE_SESSION_ID_PATTERN!r}"
                ),
            }), 400
        # #1367 follow-up: stable anon fallback. When the client omits
        # ``session_id`` AND ``page_id`` is empty (so the page-derived
        # path also can't produce a stable id), key the anon id off
        # a per-browser cookie. Without this, consecutive turns from
        # the same browser would each mint a fresh
        # ``admin-ui-anon-<uuid>`` and fragment into separate OC
        # sessions — the diagnosed root cause of session-memory loss
        # across idle gaps (#1367). The Chat-page client now always
        # sends ``session_id`` (Part 1), so the cookie path is
        # defense-in-depth for other surfaces (Telegram, third-party
        # tools posting to /api/home/chat) and for transient
        # client-side states where the body field is missing.
        browser_id, cookie_minted = _resolve_admin_ui_session_cookie(request)
        session_id = raw_session_id or derive_session_id(
            page_id, request_id=browser_id,
        )

        # Session context (spec §3.7 lever #1). Frontend sends
        # ``local_time`` (browser's clock; the admin server has no idea
        # what timezone the operator is in). ``operator_id`` and
        # ``authority`` are server-known. ``recent_actions`` is
        # auto-filled by send_to_evo from OC's session jsonl.
        # ``session_age_seconds`` + ``user_turn_count`` come from
        # ``read_thread_stats`` — these drive the in-prompt "this is
        # turn N of an ongoing thread" signal that closes the
        # "fresh session" confabulation (#1402 follow-up).
        #
        # ``surface`` / ``surface_type`` plumb through from page_context
        # (when the client supplied it) so the session-context block
        # renders an explicit ``Surface: admin_ui / laptop`` line. The
        # model uses that line to gate CLI emission per the surface-
        # aware help-style spec (Phase 1 — §2.3.4). The page_context
        # block carries the same attributes for legacy callers; the
        # session-context line is the single canonical source for the
        # surface decision.
        stats = read_thread_stats(session_id)
        # Resolve the admin bot's own id from the network rather than
        # hardcoding "evolve" (memory note
        # ``feedback_explicit_pod_membership``: network.json is the only
        # source of truth for membership — no hardcoded bot names). The
        # admin-UI tray always talks to the pod's PRIMARY bot, so its id
        # IS the assistant's self-identity. ``primary_bot_id`` already
        # falls back to "evolve" for legacy/pre-membership pods (its own
        # internal fallback), so on a renamed pod this resolves to "evo".
        # We do NOT add an outer ``or "evolve"``: substituting the literal
        # would mislabel the assistant's identity on an "evo"-primary pod.
        # ``bot_id`` is optional in the session context (renders a Bot line
        # only when set), so a None on a genuinely misconfigured pod simply
        # omits the line rather than asserting a wrong name. ``is_admin_bot``
        # stays unconditional for this route — admin authority is conferred
        # by the SURFACE (the MCP layer gates on
        # EVOLVE_CALLER_SURFACE=admin_ui), not by the bot name.
        try:
            from primary_bot import (  # type: ignore
                primary_bot_id as _primary_bot_id,
            )
            _admin_bot_id = _primary_bot_id(_load_network(network_path))
        except Exception:  # noqa: BLE001
            _admin_bot_id = None
        surface_type = (page_context.get("surface_type") or "").strip()
        if surface_type not in ("laptop", "mobile"):
            # Defensive default — favor "laptop" when uncertain. Spec
            # §2.2: mis-detecting a real mobile as laptop is the
            # failure we want to avoid; the inverse (laptop seen as
            # mobile) only suppresses CLI, which is recoverable.
            surface_type = "laptop" if surface_type == "" else "laptop"
        session_ctx = {
            "operator_id": "pod_admin",   # v1: no auth, one operator
            "authority": authority,
            # tier_preference is the *effective* tier (post-cap), set
            # below after the cap check so the model sees what's
            # actually running, not what the operator asked for. The
            # placeholder here gets overwritten before send_to_evo.
            "tier_preference": tier_pref,
            "local_time": (body.get("local_time") or "").strip(),
            "session_age_seconds": stats["age_seconds"],
            "user_turn_count": stats["user_turn_count"],
            "surface": "admin_ui",
            "surface_type": surface_type,
            # Bot self-identity. A bot cannot observe its own identity
            # unless told each turn (memory note
            # ``feedback_bot_cannot_observe_own_routing``). Without it
            # the model confabulates *member-bot* authorization walls on
            # a cross-bot question and deflects the operator to "the
            # admin UI chat" / "the evolve bot via Telegram" — the very
            # surface they are already on, and to itself. This route IS
            # the admin-UI tray, and the caller IS the evolve/admin bot,
            # the most-privileged caller (the MCP tool layer already
            # gates it as admin via EVOLVE_CALLER_SURFACE=admin_ui in
            # proxy.py). The *server* asserts this — the frontend drawer
            # is never trusted to. ``format_session_context`` renders
            # these into the <session-context> block so the model is
            # told who it is every turn. ``bot_id`` is resolved from the
            # network above (not hardcoded) so the line names the real
            # primary bot on every pod.
            "bot_id": _admin_bot_id,
            "is_admin_bot": True,
        }

        outgoing_message = _inline_attachments(message, attachments_in)

        # Role daily-cap gate.
        #
        # When the operator picks "Max" we check today's max usage against
        # the configured roleCaps.max cap (default 5) and degrade to "Power"
        # if exhausted.  The degraded value then falls through the existing
        # Power cap gate below so max→power→standard works in a single pass.
        #
        # When the operator picks "Power" we check today's tier1 / power
        # usage against the configured cap (default 10) and degrade to
        # "Standard" if exhausted.
        #
        # Server-side gate keeps the cap accounting in one place (Python)
        # and gives the frontend a clean `tier_capped: true` signal.
        # See internal/spec-user-tier-control-2026-05-26.md "tier1 daily-cap
        # handling" and spec-model-rungs-and-roles §max semantics.
        effective_tier = tier_pref
        if tier_pref in ("max", "power"):
            try:
                _net = _load_network(network_path)
                _shared = Path(_net.get("sharedDir", "/Users/Shared/evolve"))
                from primary_bot import (  # type: ignore
                    primary_bot_id as _primary_bot_id,
                )
                from models import (  # type: ignore
                    get_tier_usage_today as _get_tier_usage_today,
                )
                _pbot = _primary_bot_id(_net) or ""
                if _pbot:
                    # ── Max cap gate ───────────────────────────────────────
                    # Check roleCaps.max.maxPerDayPerBot from network.json
                    # (default 5). On cap hit degrade to power; the power
                    # gate below may degrade further to standard.
                    if effective_tier == "max":
                        _max_cap = (
                            (_net.get("models") or {})
                            .get("roleCaps") or {}
                        ).get("max") or {}
                        _max_daily_cap = _max_cap.get("maxPerDayPerBot", 5)
                        _max_used = _get_tier_usage_today(
                            _pbot, _shared, tier="max",
                        ).get("max", 0)
                        if _max_used >= _max_daily_cap:
                            effective_tier = "power"
                            tier_capped = True

                    # ── Power cap gate ────────────────────────────────────
                    # Per-bot override beats network.json fallback. Cap=0
                    # means "Power disabled for this bot" — every Power
                    # request downgrades + flags as capped. See spec
                    # "Per-bot opt-out + adjustable daily cap".
                    if effective_tier == "power":
                        _override = _read_user_tier_override(_shared, _pbot)
                        _cap = _override["dailyCap"]
                        _used = _get_tier_usage_today(
                            _pbot, _shared, tier="tier1",
                        ).get("tier1", 0)
                        if _used >= _cap:
                            effective_tier = "standard"
                            tier_capped = True
            except Exception:  # noqa: BLE001
                # Cap check failure should never block the chat —
                # fail open (do not downgrade). Bug visibility comes
                # from the operator noticing they're still on the
                # elevated tier when the cap should have fired.
                log.exception(
                    "role cap check failed; passing tier=%s through", tier_pref
                )

        # Sync the session-context block to the effective tier (the
        # plugin sees the env var directly; the model sees this line,
        # so they must agree on what tier ran). Auto / unknown stays
        # empty so the line is omitted in the prompt.
        session_ctx["tier_preference"] = (
            effective_tier if effective_tier in {"fast", "standard", "power", "max"} else ""
        )

        # Per-page server-side reshape. Pages whose client-built
        # ``summary`` is too sparse for evo to answer common questions
        # inline (forcing per-bot fan-out into ``pod_state(query="audit")`` etc.)
        # get their summary replaced with a denser server-built one
        # that reads canonical state. Scoped surgically — only pages
        # listed in ``_PAGE_RESHAPERS`` are touched; everyone else
        # ships the client pack unchanged. See
        # ``issues/features/feature-2026-06-04-002-thin-security-page-chat-context.md``.
        try:
            page_context = _maybe_reshape_page_context(
                page_context, network_path,
            )
        except Exception:  # noqa: BLE001 — reshape must never block chat
            log.exception(
                "page-context reshape failed; passing client pack through"
            )

        # SSE branch. When the client opts into a streaming response
        # (``Accept: text/event-stream``), we run the same send_to_evo
        # invocation but interleave tool-call activity events from the
        # session jsonl as they arrive. Reduces the silent-wait
        # window on heavy-context turns (Security page, large
        # Recommendations pages) — see
        # ``issues/features/feature-2026-06-04-001-stream-home-chat-responses.md``.
        # The buffered JSON path below stays as-is for any caller that
        # doesn't ask for streaming (tests, scripted callers).
        if _wants_sse(request):
            return _build_streaming_response(
                outgoing_message,
                session_id=session_id,
                network_path=network_path,
                page_context=page_context,
                session_context=session_ctx,
                tier_preference=effective_tier,
                authority=authority,
                tier_pref=tier_pref,
                effective_tier=effective_tier,
                tier_capped=tier_capped,
                cookie_mint_value=(browser_id if cookie_minted else None),
                proxy_result_to_json=_proxy_result_to_json,
                send_to_evo=send_to_evo,
            )

        result = send_to_evo(
            outgoing_message,
            session_id=session_id,
            network_path=network_path,
            page_context=page_context,
            session_context=session_ctx,
            tier_preference=effective_tier,
            # Phase 2 authorization. /api/home/chat is the admin-UI
            # chat drawer; UI access is the authorization layer (memory
            # note ``feedback_ui_authorization_presumed``). Pass the
            # surface explicitly so the MCP server's authorization
            # gate treats this caller as admin instead of falling
            # through to the conservative default.
            caller_surface="admin_ui",
        )
        resp = make_response(jsonify(_proxy_result_to_json(
            result, authority,
            tier=tier_pref,
            effective_tier=effective_tier,
            tier_capped=tier_capped,
        )))
        if cookie_minted:
            # First chat from this browser without a prior cookie. Set
            # ``admin_ui_session`` so subsequent anonymous-fallback
            # turns key off the same value and land on the same OC
            # thread (#1367 follow-up).
            resp.set_cookie(
                _ADMIN_UI_SESSION_COOKIE,
                browser_id,
                max_age=_COOKIE_MAX_AGE_S,
                httponly=True,
                samesite="Lax",
                # admin UI runs same-origin only; no need for Secure
                # (it'd break local-http deploys for no benefit).
            )
        return resp

    @app.get("/api/home/chat/tier-config")
    def api_home_chat_tier_config():
        """Return the operator tier control's per-bot config.

        Shape::

            {
              enabled:     bool,   # whether the chip renders at all
              dailyCap:    int,    # Power (Opus) daily cap
              used:        int,    # Power turns used today
              maxDailyCap: int,    # Max (Fable) daily cap (default 5)
              maxUsed:     int,    # Max turns used today
            }

        Frontend reads at chat load + after each tier_capped response to
        keep the UI in sync. Defense in depth — the backend always
        enforces the caps regardless of what the UI does. Spec:
        internal/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out"
        and spec-model-rungs-and-roles §max semantics.
        """
        try:
            net = _load_network(network_path)
            shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
            from primary_bot import primary_bot_id as _pbid  # type: ignore
            from models import get_tier_usage_today as _usage  # type: ignore
            bot = _pbid(net) or ""
            override = _read_user_tier_override(shared, bot) if bot else {
                "enabled": True, "dailyCap": 10,
            }
            used = (_usage(bot, shared, tier="tier1").get("tier1", 0)) if bot else 0
            # Max (Fable) cap — from network.json::models.roleCaps.max.
            # Default 5 matches ModelRouter's default for roleCaps.max.
            _max_cap_cfg = (
                (net.get("models") or {}).get("roleCaps") or {}
            ).get("max") or {}
            max_daily_cap = int(_max_cap_cfg.get("maxPerDayPerBot") or 5)
            max_used = (_usage(bot, shared, tier="max").get("max", 0)) if bot else 0
            return jsonify({
                "enabled": override["enabled"],
                "dailyCap": override["dailyCap"],
                "used": used,
                "maxDailyCap": max_daily_cap,
                "maxUsed": max_used,
            })
        except Exception:  # noqa: BLE001
            # Fail open: chip renders, default caps. Better than an
            # error that hides the control entirely.
            log.exception("tier-config endpoint failed; returning defaults")
            return jsonify({
                "enabled": True, "dailyCap": 10, "used": 0,
                "maxDailyCap": 5, "maxUsed": 0,
            })

    def _proxy_result_to_json(
        result: "ProxyResult",
        authority: str,
        *,
        tier: str = "auto",
        effective_tier: str = "auto",
        tier_capped: bool = False,
    ) -> dict:
        """Translate the proxy's ProxyResult into the JSON shape the
        frontend's chat-drawer code expects.

        Source taxonomy:
          ``evo``        — normal reply from the agent (green/normal bubble)
          ``proxy_warn`` — empty_reply specifically (yellow bubble;
                           "we ran the model and it (apparently) did the
                           work, but no closing text turn fired"). Phase 1
                           of the surface-aware help-style spec §8 +
                           diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md.
          ``proxy_error`` — subprocess/timeout/openclaw-rc failure (red
                           bubble — actual subprocess or OC binary failure).

        Tier echo (internal/spec-user-tier-control-2026-05-26.md):
          ``tier``           — what the operator asked for (echo of body.tier)
          ``effective_tier`` — what we actually forwarded to the plugin
                               (differs from ``tier`` only when the
                               tier1 daily cap downgraded Power → Standard)
          ``tier_capped``    — True iff the cap-driven downgrade fired;
                               the frontend uses this to render the
                               "Power capped today — used Standard" line
        """
        payload: dict[str, Any] = {
            "reply": result.text,
            "session_id": result.session_id,
            "authority": authority,
            "tier": tier,
            "effective_tier": effective_tier,
            "tier_capped": tier_capped,
        }
        if result.error == "empty_reply":
            # The yellow-bubble case: subprocess succeeded, OC parsed
            # successfully, but ``payloads[0].text`` was empty. The
            # proxy already synthesized a confirmation message from the
            # session-jsonl tool calls (when any ran); the frontend
            # renders this with warning, not error, styling so the
            # operator's mental model matches reality.
            payload["source"] = "proxy_warn"
            payload["error"] = result.error
        elif result.error == "gateway_down":
            # Evo's gateway isn't responding (probe confirmed). The
            # frontend offers a "Talk to diagnostic LLM" affordance so
            # the operator can still get clear-headed help during an
            # outage instead of an opaque empty bubble. Distinct from
            # generic proxy_error so the UI doesn't surface the fallback
            # button on every transient failure.
            payload["source"] = "gateway_down"
            payload["error"] = result.error
            payload["fallback"] = "diagnostic"
        elif result.error:
            payload["source"] = "proxy_error"
            payload["error"] = result.error
        else:
            payload["source"] = "evo"
        if result.model:
            payload["model"] = result.model
        if result.usage:
            payload["usage"] = result.usage
        if result.run_id:
            payload["run_id"] = result.run_id
        return payload

    @app.post("/api/diagnostic/chat")
    def api_diagnostic_chat():
        """Outage-only fallback chat — talks to a fast-tier diagnostic LLM.

        Used by the Chat UI when evo's gateway is down (the proxy returns
        ``error="gateway_down"``). Routes through the same pod-wide
        LLM credential the home-page narrative already uses; the
        LLM has read-only context (firing signals + recent state) and
        no tools — it's a sounding-board, not a remote-control.

        Body shape:
          { message: str, history?: [{role, text}], ... }

        Response shape:
          { reply, source, model, cost_usd, cap_status }

        ``source`` mirrors the narrative endpoint values:
          "llm"           — diagnostic call succeeded
          "no_llm"        — no LLM provider credentialed for the pod
          "cap_exceeded"  — daily cap reached
          "llm_error"     — generation failed
        """
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        history_in = body.get("history") or []
        if not isinstance(history_in, list):
            history_in = []
        # Normalize history entries into the {role, text} shape used by
        # _sanitize_history. The UI emits role="evo" for assistant turns
        # (matches the home_chat conversation log); the diagnostic
        # endpoint accepts either "evo" or "assistant" so callers can
        # post either form. Tolerate ``content`` as an alias for
        # ``text`` so non-UI callers can use the API-native shape.
        history: list[dict] = []
        for entry in history_in[-20:]:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role")
            text = entry.get("text") or entry.get("content")
            if role == "assistant":
                role = "evo"  # _sanitize_history maps evo → assistant
            if role in ("user", "evo") and isinstance(text, str) and text:
                history.append({"role": role, "text": text})

        if not message:
            return jsonify({"error": "message is required"}), 400

        network = _load_network(network_path)
        shared_dir = _shared_dir()

        chat_cfg = network.get("home_chat") or {}
        daily_cap = int(chat_cfg.get("daily_cap") or _hc.DEFAULT_DAILY_CALL_CAP)

        target = _resolve_chat_target(network, chat_cfg.get("model") or "")
        if target is None:
            return jsonify({
                "reply": (
                    "Diagnostic LLM unavailable — no LLM provider is "
                    "credentialed for the pod's primary bot. Bring up evo "
                    "or add a provider API key to restore chat."
                ),
                "source": "no_llm",
                "cap_status": _hc.cap_status(shared_dir, daily_cap=daily_cap),
                "error": "no_llm_credential",
            })

        cap = _hc.cap_status(shared_dir, daily_cap=daily_cap)
        if cap["remaining"] <= 0:
            return jsonify({
                "reply": (
                    "Diagnostic LLM daily cap exceeded. Raise "
                    "home_chat.daily_cap in network.json if this is a real "
                    "outage that's outlasted the cap."
                ),
                "source": "cap_exceeded",
                "cap_status": cap,
            })

        digest = _build_pod_state_digest_safe(network_path, shared_dir)
        try:
            out = _hc.generate_diagnostic_reply(
                user_message=message,
                history=history,
                pod_state_digest=digest,
                api_key=target.api_key,
                model=target.model,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("diagnostic chat generation failed: %s", exc)
            return jsonify({
                "reply": (
                    "Diagnostic LLM call failed. Check admin-server logs "
                    "and the LLM provider's API health. The pod is still observable "
                    "via the Overview page and any external liveness probes "
                    "you have configured."
                ),
                "source": "llm_error",
                "error": str(exc),
                "cap_status": cap,
            })

        new_cap = _hc.bump_usage(shared_dir, cost_usd=out["cost_usd"])
        return jsonify({
            "reply": out["text"],
            "model": out["model"],
            "cost_usd": out["cost_usd"],
            "input_tokens": out["input_tokens"],
            "output_tokens": out["output_tokens"],
            "source": "llm",
            "cap_status": {
                "used": int(new_cap["calls"]),
                "remaining": max(0, daily_cap - int(new_cap["calls"])),
                "daily_cap": daily_cap,
                "cost_today_usd": round(float(new_cap["cost_usd"]), 4),
            },
        })

    @app.get("/api/home/narrative")
    def api_home_narrative():
        """Return a cached or fresh LLM-generated narrative summary.

        Query params:
          refresh=1   bypass the cache and regenerate (counts against the
                      daily cap; useful for the operator's manual ↻).

        Response:
          {text, model, cost_usd, source, generated_at, cap_status}

        ``source`` is one of:
          "cache"        — fresh cached entry returned, no LLM call made
          "llm"          — newly generated via the model
          "no_llm"       — no API key configured; UI keeps the rule-based fallback
          "cap_exceeded" — daily LLM cap reached; UI keeps the rule-based fallback
          "llm_error"    — generation failed; UI keeps the rule-based fallback
        """
        network = _load_network(network_path)
        shared_dir = _shared_dir()

        chat_cfg = network.get("home_chat") or {}
        daily_cap = int(chat_cfg.get("daily_cap") or _hc.DEFAULT_DAILY_CALL_CAP)
        model_override = (
            chat_cfg.get("narrative_model") or chat_cfg.get("model") or ""
        )
        ttl_seconds = int(
            chat_cfg.get("narrative_cache_ttl_seconds")
            or _hc.DEFAULT_NARRATIVE_CACHE_TTL_SECONDS
        )

        refresh = request.args.get("refresh", "").strip() in ("1", "true", "yes")

        # Build the digest once; the cache key is its SHA1 so any change
        # in firing signals / proposals / bot status invalidates the
        # cached narrative without the operator having to refresh.
        digest = _build_pod_state_digest_safe(network_path, shared_dir)
        digest_h = _hc.digest_hash(digest)
        cache = _hc.read_narrative_cache(shared_dir)

        if not refresh and _hc.cache_is_fresh(
            cache, digest_hash_str=digest_h, ttl_seconds=ttl_seconds
        ):
            assert isinstance(cache, dict)
            return jsonify({
                "text": cache.get("text") or "",
                "model": cache.get("model"),
                "cost_usd": cache.get("cost_usd", 0.0),
                "source": "cache",
                "generated_at": cache.get("generated_at"),
                "cap_status": _hc.cap_status(shared_dir, daily_cap=daily_cap),
            })

        target = _resolve_chat_target(network, model_override)
        if target is None:
            return jsonify({
                "text": "",
                "source": "no_llm",
                "cap_status": _hc.cap_status(shared_dir, daily_cap=daily_cap),
                "error": "no_llm_credential",
            })

        cap = _hc.cap_status(shared_dir, daily_cap=daily_cap)
        if cap["remaining"] <= 0:
            return jsonify({
                "text": "",
                "source": "cap_exceeded",
                "cap_status": cap,
            })

        try:
            out = _hc.generate_narrative(
                pod_state_digest=digest,
                api_key=target.api_key,
                model=target.model,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("home_chat narrative generation failed: %s", exc)
            return jsonify({
                "text": "",
                "source": "llm_error",
                "error": str(exc),
                "cap_status": cap,
            })

        new_cap = _hc.bump_usage(shared_dir, cost_usd=out["cost_usd"])
        _hc.write_narrative_cache(
            shared_dir,
            digest_hash_str=digest_h,
            text=out["text"],
            cost_usd=out["cost_usd"],
            model=out["model"],
            input_tokens=out["input_tokens"],
            output_tokens=out["output_tokens"],
        )
        return jsonify({
            "text": out["text"],
            "model": out["model"],
            "cost_usd": out["cost_usd"],
            "input_tokens": out["input_tokens"],
            "output_tokens": out["output_tokens"],
            "source": "llm",
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z"),
            "cap_status": {
                "used": int(new_cap["calls"]),
                "remaining": max(0, daily_cap - int(new_cap["calls"])),
                "daily_cap": daily_cap,
                "cost_today_usd": round(float(new_cap["cost_usd"]), 4),
            },
        })


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


# Cookie value charset: hex pairs only. Defensive — if a malicious /
# corrupt cookie value arrives, we don't echo it back to OC (which
# would use it in a JSONL filename). Anything outside this charset
# causes us to mint a fresh value instead.
_ADMIN_UI_COOKIE_VALUE_RE = re.compile(r"^[A-Fa-f0-9]{4,64}$")


def _is_leading_slash_command(message: str) -> bool:
    """Return True when ``message`` reads as an OC control slash-command.

    Triggers when the LSTRIPPED message starts with a single ``/`` followed
    by a non-``/`` character. Fails *open* for:

      * ``"in /tmp the file is..."`` — slash mid-sentence, not leading.
      * ``"//comment"`` — double-slash; not an OC control command.
      * ``""`` / whitespace-only — empty; let the existing 400 path handle.

    Closes Fix 4 of the 2026-05-21 exec-approval-leak diagnosis. The OC
    slash-command surface (``/approve``, ``/grant``, ``/reload``, …) is OC's
    control plane; the admin-UI chat is a tool-calling surface only.
    """
    if not message:
        return False
    stripped = message.lstrip()
    if not stripped.startswith("/"):
        return False
    # Reject single-slash control commands; allow ``//`` (uncommon, but
    # not an OC slash-command shape).
    if stripped.startswith("//"):
        return False
    return True


def _resolve_admin_ui_session_cookie(req) -> tuple[str, bool]:
    """Return ``(browser_id, minted)`` for the per-browser anon-fallback.

    Reads the ``admin_ui_session`` cookie if present and well-formed;
    otherwise mints a fresh value the caller will set on the response.

    The returned ``browser_id`` keys ``derive_session_id``'s anon
    fallback so consecutive turns from the same browser share an OC
    session even when the client never sends ``session_id``.
    Defense-in-depth against the #1367 session-memory-loss bug.

    ``minted=True`` signals the caller should ``set_cookie`` on the
    response. We don't try to refresh max_age on every hit — Flask's
    default cookie handling treats present values as durable, and the
    1-year max_age gives ample headroom for occasional sweeps.
    """
    existing = req.cookies.get(_ADMIN_UI_SESSION_COOKIE) or ""
    if existing and _ADMIN_UI_COOKIE_VALUE_RE.fullmatch(existing):
        return existing, False
    fresh = secrets.token_hex(_COOKIE_VALUE_BYTES)
    return fresh, True


def _unwrap_dispatch_reply(result) -> str:
    """Pull the operator-facing prose off a DispatchResult, stripping
    the `═══ evo ═══` delimiter banner the JS would otherwise strip
    client-side. Falls back across the available fields."""
    raw = (
        getattr(result, "direct_send_message", None)
        or getattr(result, "direct_message", None)
        or ""
    )
    return _strip_evo_delim(raw)


_EVO_OPEN_PREFIXES = ("═══ evo ═══", "═══ evo ")
_EVO_CLOSE_SUFFIX = "═══ end evo ═══"


def _strip_evo_delim(s: str) -> str:
    if not s:
        return ""
    out = s.strip()
    # Strip leading "═══ evo ═══" or "═══ evo X ═══" line.
    for prefix in _EVO_OPEN_PREFIXES:
        if out.startswith(prefix):
            # Drop everything up to and including the closing "═══" of
            # the first line.
            nl = out.find("\n")
            if nl >= 0:
                out = out[nl + 1 :].lstrip("\n")
            else:
                out = ""
            break
    if out.endswith(_EVO_CLOSE_SUFFIX):
        out = out[: -len(_EVO_CLOSE_SUFFIX)].rstrip("\n")
    return out.strip()


def _looks_like_command(text: str) -> bool:
    """True when the user's text reads as a short subcommand invocation
    rather than a natural-language question.

    Heuristic: ≤3 whitespace-separated tokens AND the first token is a
    known subcommand. "help", "evo help", "cost 7d", "alerts firing"
    qualify. "help me fix the workspace issue", "what's going on", "show
    me alerts" don't — they read as prose and should go to the LLM
    where the conversational prompt handles them correctly.

    The old route auto-prepended "evo " to every non-prefixed message
    and dispatched it; for natural-language questions starting with a
    command-shaped word ("help me fix X") the dispatcher mangled them.
    This guard prevents that misroute.
    """
    if not text:
        return False
    parts = text.strip().split()
    if not parts or len(parts) > 3:
        return False
    first = parts[0].lower()
    # Strip a literal "evo" prefix before checking the registry — both
    # "help" and "evo help" should qualify.
    if first == "evo":
        if len(parts) < 2:
            return False
        first = parts[1].lower()
    return first in _known_subcommand_names()


def _known_subcommand_names() -> set[str]:
    """Set of subcommands that actually WORK in the admin chat surface.

    Delegates to ``home_chat.working_subcommand_names()`` which reads
    the live registry and filters out:
      * stub handlers (return "coming soon" — broken-promise buttons)
      * chat-incompatible commands (need channel/sender context the
        admin route can't provide — e.g. `better`, `claim`, `profile`)

    Used by two callers, both of which need to agree:
      * the dispatcher gate (_looks_like_command) — so natural-language
        prose like "help me fix X" isn't treated as a subcommand even
        though "help" is in the registry
      * the LLM-action filter (extract_suggested_actions) — so a
        hallucinated or stub-only command can never surface as a
        clickable button

    Falls back to a small hand-coded list when home_chat itself isn't
    importable (defensive)."""
    try:
        return _hc.working_subcommand_names()
    except Exception:
        return {
            "help", "alerts", "cost", "usage", "health",
            "integrations", "apps", "app-audit", "fail",
            "gallery", "fun",
        }


def _dispatch_recognized(result, dispatch_reply: str) -> bool:
    """True when the dispatcher returned a real subcommand match.

    The dispatcher tags free-form text with the user's first word as
    the (unrecognized) ``subcommand`` field — so a literal name check
    isn't enough. We consult the subcommand registry; if the tagged
    name resolves to a real ``EvoSubcommand``, treat as recognized.
    """
    sub = (getattr(result, "subcommand", "") or "").strip().lower()
    if not sub:
        return False
    try:
        from ..evo import subcommands as _sc
        if _sc.lookup(sub) is not None:
            return True
    except Exception:
        pass
    return False


def _inline_attachments(message: str, attachments: list[dict]) -> str:
    """Prepend a brief operator-attached block so evo sees the file refs.

    Binary contents stay on disk under {shared_dir}/chat-uploads/…; the
    OC subprocess can ``read_file`` them by path when it needs to. For
    v1 we just want evo aware of WHAT was attached and WHERE — image
    passthrough is a follow-on once the OC input schema accepts blobs.
    """
    if not attachments:
        return message
    from .chat_upload_routes import describe_attachments
    block = describe_attachments(attachments)
    if not block:
        return message
    if not message:
        return block
    return f"{block}\n\n{message}"


def _prepend_page_context(message: str, page_context: dict) -> str:
    """Wrap the operator's prompt with a small page-context framing line
    so the LLM knows which admin-UI surface they're asking about.

    Shape returned by the JS drawer:
      {page_id, page_label, state?: {...arbitrary JSON-shaped snapshot...}}

    Empty dict or missing page_id → return the message unchanged (the
    Chat-page path uses this, no framing needed). State is serialized
    as JSON so the LLM can read structured page data verbatim; we cap
    the serialized length so a chatty pack can't blow the prompt.
    """
    if not page_context or not page_context.get("page_id"):
        return message
    label = (
        page_context.get("page_label")
        or page_context.get("page_id")
        or "current"
    )
    state = page_context.get("state") or {}
    state_line = ""
    if isinstance(state, dict) and state:
        try:
            blob = json.dumps(state, separators=(",", ":"))[:1500]
            state_line = f"\nPage state snapshot: {blob}"
        except (TypeError, ValueError):
            state_line = ""
    return (
        f"[Operator is on the '{label}' page.{state_line}]\n\n{message}"
    )


# ─────────────────────────────────────────────────────────────────────
# SSE streaming for /api/home/chat
# ─────────────────────────────────────────────────────────────────────


def _wants_sse(req) -> bool:  # noqa: ANN001 — Flask Request type
    """Return True when the client opts into a streaming response.

    Two opt-in vectors:
      * ``Accept: text/event-stream`` header (the standard)
      * ``?stream=1`` query param (handy for browser DevTools "Open in
        new tab" + curl smoke tests where setting Accept is awkward)

    Both are explicit — there's no implicit upgrade. Tests, scripted
    callers, and the legacy frontend keep getting buffered JSON until
    they opt in.
    """
    try:
        if req.args.get("stream") == "1":
            return True
        accept = req.headers.get("Accept", "")
        # Don't use ``in`` substring match — the default Accept value
        # from browsers is ``*/*`` which would not contain our token
        # but a paranoid header like ``text/html, */*`` would still
        # be matched by a naive ``in`` against the substring. Mimetype
        # parse is cheap and correct.
        for chunk in accept.split(","):
            mt = chunk.split(";", 1)[0].strip().lower()
            if mt == "text/event-stream":
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _build_streaming_response(
    message: str,
    *,
    session_id: str,
    network_path: Path,
    page_context: dict,
    session_context: dict,
    tier_preference: str,
    authority: str,
    tier_pref: str,
    effective_tier: str,
    tier_capped: bool,
    cookie_mint_value: str | None,
    proxy_result_to_json: Callable[..., dict],
    send_to_evo: Callable[..., Any],
):
    """Compose the SSE Response for one chat turn.

    Imports the streaming helpers lazily so the route module's
    import path stays cheap for non-streaming callers. The done
    event payload is shaped via the same ``_proxy_result_to_json``
    used by the buffered branch, so client-side handling of the
    final reply is identical in both modes.
    """
    from flask import Response as _Response
    from ..evo.streaming import iter_sse_events
    from ..evo.proxy import _OC_SESSIONS_DIR_DEFAULT
    import os as _os

    # Resolve the session jsonl path. Mirrors send_to_evo's view of
    # where OC writes — the OC_SESSIONS_DIR env var takes priority
    # for pods that overrode the default.
    sessions_dir = Path(
        _os.environ.get("OC_SESSIONS_DIR") or _OC_SESSIONS_DIR_DEFAULT
    )
    jsonl_path = sessions_dir / f"{session_id}.jsonl"

    def _run_subprocess() -> Any:
        return send_to_evo(
            message,
            session_id=session_id,
            network_path=network_path,
            page_context=page_context,
            session_context=session_context,
            tier_preference=tier_preference,
            caller_surface="admin_ui",
        )

    def _done_serializer(
        result: Any, error_text: str | None, _session_id: str,
    ) -> dict:
        """Match the buffered branch's response shape.

        When the subprocess worker itself raised (not just an OC error
        — that comes back as a ProxyResult with error set), synthesize
        a minimal ProxyResult-shaped surrogate so the JSON shaper
        downstream produces the same fields.
        """
        if result is None:
            # Synthesize a proxy_error shape so the frontend handles
            # it identically to a buffered subprocess failure.
            from ..evo.proxy import ProxyResult as _PR
            surrogate = _PR(
                text=(
                    "evo's gateway worker crashed: "
                    + (error_text or "unknown error")
                ),
                session_id=_session_id,
                error=error_text or "subprocess_worker_failed",
            )
            return proxy_result_to_json(
                surrogate, authority,
                tier=tier_pref,
                effective_tier=effective_tier,
                tier_capped=tier_capped,
            )
        return proxy_result_to_json(
            result, authority,
            tier=tier_pref,
            effective_tier=effective_tier,
            tier_capped=tier_capped,
        )

    def _generator():
        yield from iter_sse_events(
            session_id=session_id,
            jsonl_path=jsonl_path,
            run_subprocess=_run_subprocess,
            done_serializer=_done_serializer,
        )

    resp = _Response(_generator(), mimetype="text/event-stream")
    # Headers that proxies / browsers care about for SSE:
    #   * Cache-Control no-store — never cache an event stream.
    #   * X-Accel-Buffering no — disables nginx response buffering
    #     when an admin install sits behind nginx; harmless otherwise.
    #   * Connection keep-alive — explicit so reverse proxies don't
    #     downgrade to close after the first chunk.
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    if cookie_mint_value:
        resp.set_cookie(
            _ADMIN_UI_SESSION_COOKIE,
            cookie_mint_value,
            max_age=_COOKIE_MAX_AGE_S,
            httponly=True,
            samesite="Lax",
        )
    return resp


def _reshape_security(
    client_summary: dict, shared_dir: Path,
) -> dict:
    """Server-side rebuild of the Security-page ``summary`` blob.

    The client-built pack ships per-bot rows with title-only finding
    strings — evo then has to fan out into ``pod_state(query="audit", bot_id=…)``
    for every affected bot to answer common questions, which is the
    root cause of the Security-page chat timeouts observed 2026-06-04.
    This rebuild coalesces findings across bots, inlines remediation
    text, and inlines firing-signal detail (producer, occurrence
    count, affected bot) so the model can answer from context alone.

    Client-provided ``active_subtab`` + ``backup_drift_items`` are
    threaded through as hints — drift detection costs git per bot, so
    we don't re-run it server-side per chat turn.
    """
    from .security_chat_context import build_security_state

    active_subtab = client_summary.get("active_subtab")
    if not isinstance(active_subtab, str):
        active_subtab = None
    drift_hint = client_summary.get("backup_drift_items")
    if not isinstance(drift_hint, list):
        drift_hint = None
    return build_security_state(
        shared_dir,
        active_subtab=active_subtab,
        backup_drift_hint=drift_hint,
    )


# Each reshaper takes the client-built summary dict and a resolved
# shared_dir, and returns the new summary dict.
_Reshaper = Callable[[dict, Path], dict]


# Registry of per-page reshapers. Keys match the JS ``page_id``
# constants in index.html (e.g. ``data-page="security"`` → "security").
# Adding a page is a one-line registry entry plus the corresponding
# reshape function. Pages not in the registry pass through unchanged.
_PAGE_RESHAPERS: dict[str, _Reshaper] = {
    "security": _reshape_security,
}


def _maybe_reshape_page_context(
    page_context: dict, network_path: Path,
) -> dict:
    """Apply the page's server-side summary reshape, if registered.

    Returns the original ``page_context`` (same object identity) when
    no reshaper is registered for the page so the cheap path stays
    cheap. When a reshaper runs, returns a NEW dict so the original
    isn't mutated (defensive against any caller that might still hold
    a reference).
    """
    if not isinstance(page_context, dict):
        return page_context
    page_id = (page_context.get("page_id") or "").strip()
    reshaper = _PAGE_RESHAPERS.get(page_id)
    if reshaper is None:
        return page_context
    client_summary = page_context.get("summary")
    if not isinstance(client_summary, dict):
        client_summary = {}
    # ``load_network`` is imported inside ``register_routes`` for the
    # request-handler closures; the module-scope helper imports it
    # locally to stay self-contained.
    from ..config import load_network
    net = load_network(network_path)
    shared_dir = Path(net.get("sharedDir", "/Users/Shared/evolve"))
    new_summary = reshaper(client_summary, shared_dir)
    reshaped = dict(page_context)
    reshaped["summary"] = new_summary
    return reshaped


def _resolve_chat_target(network: dict, model_override: str = ""):
    """Resolve the (provider, model, key) target for a Home LLM call via
    the provider-agnostic ``infra_llm`` resolver (#3466).

    ``model_override`` is the ``network.json::home_chat.model`` (or
    ``narrative_model``) pin: honored when provider-qualified AND its
    provider is credentialed on the primary bot; otherwise resolution
    walks the pod's fast-tier config / credentialed providers. Returns
    None when no LLM provider is credentialed — caller surfaces that
    explicitly so the operator can wire it up in the Maintenance UI.
    """
    try:
        from infra_llm import credentialed_target, resolve_infra_llm  # type: ignore

        target = None
        if model_override:
            target = credentialed_target(model_override, network=network)
            if target is None:
                log.warning(
                    "home_chat.model override %r has no credentialed "
                    "provider (bare ids need a provider/ prefix) — "
                    "resolving normally",
                    model_override,
                )
        return target or resolve_infra_llm("fast", network=network)
    except Exception as exc:  # noqa: BLE001
        log.warning("home_chat target resolution failed: %s", exc)
        return None


def _build_pod_state_digest_safe(
    network_path: Path, shared_dir: Path
) -> str:
    """Best-effort pod-state digest. Each piece is wrapped in its own
    try/except so missing/stale data doesn't blank the prompt.

    Pending proposals are passed through
    :func:`arbiter.still_motivated.is_still_motivated` so a proposal
    whose underlying condition has demonstrably cleared (motivating
    signals all inactive, or claim metric already at target) is
    silently filtered out — and archived in place as a side effect so
    the on-disk state catches up. Defense-in-depth against producer-side
    sweeps that haven't fired yet on this cycle.
    """
    firing: list[dict] = []
    proposals: list[dict] = []
    bots: dict[str, dict] = {}
    host_health: dict | None = None
    active_breakers: list[dict] = []

    # Firing signals — mirrors the Reports → Alerts page's canonical
    # definition (state=firing, severity != info, all flavors). Without
    # min_severity, info-tier advisories the UI hides by default get
    # rolled into the digest count, so evo reports a larger number than
    # the operator sees. See spec-alerts-count-normalization-2026-06-06.
    try:
        from signals import store as signals_store  # type: ignore
        sigs = list(
            signals_store.iter_active(
                shared_dir, state="firing", min_severity="warn"
            )
        )
        firing = [s.to_dict() for s in sigs]
    except Exception:
        firing = []

    # Tripped circuit/cost breakers. These live in a separate store from
    # Signals — a breaker trip does NOT auto-fire a Signal — so without
    # this block a paused bot is invisible to the narrative.
    try:
        from breakers import store as breakers_store  # type: ignore
        active_breakers = [r.to_json() for r in breakers_store.list_active(shared_dir)]
    except Exception:
        active_breakers = []

    # Pod-wide pause state ("Pause all bots" button). Same shape as
    # breakers — file-based store under {shared_dir}/recovery/, no
    # Signal emitted on transition.
    pause_state: dict | None = None
    try:
        from ..recovery import read_pause_state
        pause_state = read_pause_state(shared_dir)
    except Exception:
        pause_state = None

    # Pod health summary — backs the "Pod health: X issues detected"
    # banner on the Overview page. Read from the Better Engine cache
    # (written after every health-check run) rather than re-running
    # all checks on a chat request.
    pod_health: dict | None = None
    try:
        cache = shared_dir / "better-engine" / "cache" / "health-latest.json"
        if cache.exists():
            import json as _json
            pod_health = _json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pod_health = None

    # Pending proposals — re-validate against current state and archive
    # any whose condition has cleared (best-effort: filter errors leave
    # the proposal in the digest rather than blanking the briefing).
    try:
        from arbiter import store as arbiter_store  # type: ignore
        from arbiter.still_motivated import (  # type: ignore
            archive_stale,
            is_still_motivated,
        )

        for p in arbiter_store.iter_proposals(shared_dir, subdirs=("pending",)):
            try:
                verdict = is_still_motivated(p, shared_dir)
            except Exception:  # noqa: BLE001 — re-validation must not blank digest
                verdict = None
            if verdict is False:
                # Cleared since the producer's last cycle. Fire-and-forget
                # archive so the next read agrees; either way, skip the
                # proposal so the LLM doesn't narrate stale state.
                try:
                    archive_stale(
                        p,
                        shared_dir,
                        reason="home_briefing: condition cleared at read time",
                        actor="home_briefing",
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue
            proposals.append(p.to_dict())
    except Exception:
        proposals = []

    # Bot status from network_status
    try:
        from ..config import network_status
        data = network_status(network_path)
        bots = data.get("bots") or {}
    except Exception:
        bots = {}

    # Host health
    try:
        from .. import host_health as _hh
        host_health = _hh.snapshot()
    except Exception:
        host_health = None

    return _hc.build_pod_state_digest(
        firing_signals=firing,
        pending_proposals=proposals,
        bots=bots,
        host_health=host_health,
        active_breakers=active_breakers,
        pause_state=pause_state,
        pod_health=pod_health,
    )
