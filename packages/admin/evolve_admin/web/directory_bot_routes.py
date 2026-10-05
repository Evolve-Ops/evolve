"""Bot-facing user-directory routes — ``/api/directory/{lookup,digest,upsert}``.

Spec: [internal/spec-user-directory-2026-06-22.md](../../../internal/spec-user-directory-2026-06-22.md)
§5 (the read tools + the per-turn digest + the **write** tool) + §10 invariants. This is
the bot-side integration: the read half (Phase 3a — ``lookup``/``digest``) lets the bot
resolve its own ``Person``\\ s through THE one read path and render a per-turn roster
digest; the **write half** (Phase 3b — ``upsert``) lets the bot record/correct a person's
identity + contact data into the canonical store *instead of* its own ``USER.md`` prose
(the §0 address-book bug). Neither half ever reaches another bot's directory or the
private behavioral profile.

The Evolve OC plugin (loaded in every bot's gateway, running as the bot's own macOS
user) reaches these routes over the admin daemon's **unix socket**
(``{shared}/admin-daemon.sock``):

  POST /api/directory/lookup  body ``{ref}``                    → ``{person: <bot_dict>|null}``
  GET  /api/directory/digest                                    → ``{persons:[row...], total, shown, cap}``
  POST /api/directory/upsert  body ``{ref, emails?, contact?, identities?, names?}``
                                                                → ``{ok, created, person: <bot_dict>|null}``

THE IDENTITY MODEL (mirrors ``google_bot_routes`` — the cross-bot-safe primitive):

  The calling bot is bound **server-side** from the kernel-reported peer uid of the
  unix-socket connection (``peer_auth.resolve_peer_bot_id``) — NEVER from a request
  field. A bot literally cannot present another bot's uid, so every call is scoped to the
  caller's OWN directory **by construction** (the "no cross-bot read/write" invariant). A
  ``ref`` that matches a person in a *different* bot's directory simply doesn't resolve
  here, and a write always lands in the caller's own ``{shared}/directory/{bot}.json``.
  TCP requests (the admin-UI binding) and unknown peer uids get 403 — a browser session
  must never act as a bot.

ONE READ PATH (invariant #1):

  Every endpoint resolves through ``resolve_person`` / ``resolve_persons`` /
  ``resolve_identity`` — the SAME canonical match the admin Users page GET uses. The
  write tool targets the *same* identity the read tools would resolve.

THE BOT ASSERTS IDENTITY, NEVER AUTHORITY (invariant #2):

  ``upsert`` stamps every write ``provenance:"bot-asserted"`` — HARDCODED in the route,
  never read from the body — and forwards only the four directory-owned fields
  (``emails`` / ``contact`` / ``identities`` / ``names``) to
  ``storage.upsert_entry``, whose keyword-only signature cannot touch ``membership`` /
  roles / admission at all. A body carrying any authority key (``role`` / ``membership`` /
  ``capabilities`` / ``admission`` / …) or the private ``profile_ref`` is rejected
  outright (400). The bot can describe *who someone is and how to reach them*; it can
  never grant *what they may do*.

PROVENANCE NEVER FORGED OR DOWNGRADED (invariant #3):

  ``storage.upsert_entry`` stamps the call's provenance over any per-row provenance (so
  the bot can't claim ``operator-verified``) **and** merges
  emails/identities preserving any existing row whose provenance strictly outranks the
  write (so a ``bot-asserted`` write can never destroy or downgrade an
  ``operator-verified`` email/identity).

NO PROFILE LEAK (invariant #5):

  Every ``Person`` returned is projected through ``user_directory.bot_view`` before
  serialization, which strips the behavioral-profile link (``profile_ref``) and the audit
  trail. The bot never receives either.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from ..config import load_network
from ..user_directory import bot_view
from ..user_directory import model
from ..user_directory import resolver as udr
from ..user_directory import storage as uds
from . import peer_auth
# One home for the sharedDir fallback literal (platform-path-lint) — the SAME helper the
# Phase-2 operator directory routes use, so the bot write path and the admin write path
# resolve the store location identically.
from .routes_bot_users import _shared_dir_from_net
from .routes_shared import _audit_log_entry


log = logging.getLogger(__name__)

# Per-turn digest cap — bounds the injected block so a large roster can't blow the LLM
# context budget. The bot side TTL-caches the result, so this is the worst-case row count
# on the wire and in the prompt.
DIGEST_CAP = bot_view.DEFAULT_DIGEST_CAP

# Soft per-bot store bound: a CREATE that would push the directory past this many Persons
# is refused (updates to existing Persons are always allowed). Bounds runaway/spammy
# contact creation so one misbehaving bot can't grow its directory file without limit
# (spec §10 / auditor "unbounded growth" case). Generous — a real address book is in the
# hundreds; this only catches a runaway. The per-turn digest is independently capped
# (DIGEST_CAP), so the prompt budget is bounded regardless of store size.
MAX_DIRECTORY_PERSONS = 2000

# Per-Person size bounds. The person-count cap above bounds the NUMBER of Persons; these
# bound the size of any ONE Person's write, so a bot cannot inflate its directory file
# (which ``save_directory`` rewrites whole on every write) with a giant attribute value or
# thousands of identity/email rows while staying under the person cap. Generous — a real
# contact has a handful of emails/identities and short attributes — so they only catch a
# runaway. Exceeding any is a clean 400, before any write.
MAX_EMAILS_PER_PERSON = 25
MAX_IDENTITIES_PER_PERSON = 25
MAX_CONTACT_KEYS = 50
MAX_ADDR_LEN = 320          # RFC-5321 maximum email length
MAX_FIELD_LEN = 1024        # any contact/name value, or an identity id/platform/handle

# Body keys the write tool refuses outright. ``upsert_entry`` already makes
# membership/roles/admission UNREACHABLE (its signature has no such parameter), but the
# route rejects them loudly so the "identity, never authority" boundary (invariant #2) is
# explicit, audited, and testable rather than silently ignored. ``profile_ref`` is the
# link to the private behavioral profile — the bot must never set it (invariant #5).
_FORBIDDEN_BODY_KEYS: frozenset[str] = frozenset({
    "membership", "role", "roles", "capabilities", "capability",
    "admitted", "admission", "channels", "profile_ref", "profile", "audit",
})


def _ref_kind(ref: str) -> str:
    """Classify a lookup ``ref`` WITHOUT logging its raw value.

    The raw ref is often PII (an email address, a person's name), so the audit trail
    records only its *shape* — ``handle`` / ``email`` / ``id`` / ``name`` — plus whether
    it hit. Mirrors ``resolver._match_ref``'s dispatch so the label matches what actually
    happened.
    """
    s = (ref or "").strip()
    if s.startswith("@"):
        return "handle"
    if "@" in s:
        return "email"
    if s.startswith("pers_"):
        return "id"
    return "name"


def _audit(bot_id: str, tool: str, details: dict) -> None:
    """Audit one bot-facing directory call: ``(bot_id, tool, details)``.

    Best-effort by contract (``_audit_log_entry`` swallows its own I/O errors), so this
    never breaks a tool call. The high-frequency digest fetch is intentionally NOT audited
    (it fires on a TTL timer, not a bot decision) — the bot-initiated ``lookup`` and
    ``upsert`` are, recording the ref *kind* + outcome (and, for a write, which fields and
    create-vs-update), never the raw PII ref or any field value. The per-field provenance
    trail of a write lives in the store's own audit (``directory/log/``); this is the
    bot-call envelope.
    """
    _audit_log_entry(
        f"directory.bot_call.{tool}",
        bot_id,
        {**details, "surface": "bot_plugin"},
    )


# ── upsert request parsing (validate → 400 BEFORE any write) ──────────────────


class _BadUpsert(Exception):
    """A malformed ``directory_upsert`` body — surfaced as a clean 400, never a 500."""


def _parse_emails(raw: object) -> "list[dict] | None":
    """Validate the body's ``emails`` into ``{addr, rank, verified}`` rows, or ``None``.

    Returns ``None`` for an absent OR empty list — an empty list is treated as "nothing
    to say about emails", NOT "delete them all": the bot tool only adds/edits emails, it
    cannot clear them (deletion is an operator action). Drops any caller-supplied
    ``provenance`` (``upsert_entry`` stamps ``bot-asserted``) and forces ``verified=False``
    — a bot can assert an address exists but cannot mark it *verified* (only the operator,
    or a channel-capture flow, confirms). Raises :class:`_BadUpsert` on a malformed
    address or >1 ``primary`` (``upsert_entry`` re-checks, but a clean message here)."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _BadUpsert("'emails' must be a list of {addr, rank} objects")
    if len(raw) > MAX_EMAILS_PER_PERSON:
        raise _BadUpsert(f"at most {MAX_EMAILS_PER_PERSON} emails per person")
    out: "list[dict]" = []
    for e in raw:
        if not isinstance(e, dict):
            raise _BadUpsert("each email must be an object with an 'addr'")
        addr = str(e.get("addr") or "").strip()
        if not addr or addr.count("@") != 1 or len(addr) > MAX_ADDR_LEN:
            raise _BadUpsert(f"{e.get('addr')!r} is not a valid email address")
        local, _, domain = addr.partition("@")
        if not local or "." not in domain:
            raise _BadUpsert(f"{addr!r} is not a valid email address")
        rank = e.get("rank")
        if rank is not None and rank not in model.EMAIL_RANKS:
            raise _BadUpsert(
                f"email rank must be one of {list(model.EMAIL_RANKS)}, got {rank!r}")
        # Fresh row — only addr/rank/verified. No provenance (stamped bot-asserted),
        # verified forced False (a bot cannot self-certify verification).
        out.append({"addr": addr, "rank": rank or "secondary", "verified": False})
    if sum(1 for r in out if r["rank"] == "primary") > 1:
        raise _BadUpsert("at most one email may be rank:'primary'")
    return out or None


def _parse_identities(raw: object) -> "list[dict] | None":
    """Validate the body's ``identities`` into ``{platform, id, handle?}`` rows, or ``None``.

    Drops any caller-supplied ``source`` (``upsert_entry`` stamps ``bot-asserted``).
    Raises :class:`_BadUpsert` on a row missing a non-empty ``platform``/``id``."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _BadUpsert("'identities' must be a list of {platform, id} objects")
    if len(raw) > MAX_IDENTITIES_PER_PERSON:
        raise _BadUpsert(f"at most {MAX_IDENTITIES_PER_PERSON} identities per person")
    out: "list[dict]" = []
    for i in raw:
        if not isinstance(i, dict):
            raise _BadUpsert("each identity must be an object with platform + id")
        platform = str(i.get("platform") or "").strip()
        ident = str(i.get("id") or "").strip()
        if not platform or not ident:
            raise _BadUpsert("each identity needs a non-empty 'platform' and 'id'")
        if len(platform) > MAX_FIELD_LEN or len(ident) > MAX_FIELD_LEN:
            raise _BadUpsert("identity platform/id is too long")
        row: dict = {"platform": platform, "id": ident}
        handle = i.get("handle")
        if handle:
            handle = str(handle).strip()
            if len(handle) > MAX_FIELD_LEN:
                raise _BadUpsert("identity handle is too long")
            row["handle"] = handle
        out.append(row)
    return out or None


def _parse_open_map(raw: object, label: str) -> "dict | None":
    """Validate ``contact`` / ``names`` into a flat ``str → str`` map, or ``None``.

    Drops blank keys/values; rejects nested objects/lists (the maps are flat attribute
    tables — a nested blob would round-trip as opaque junk). Returns ``None`` when the
    cleaned map is empty, so an all-blank submit is a no-op rather than a write."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _BadUpsert(f"'{label}' must be an object of scalar attributes")
    if len(raw) > MAX_CONTACT_KEYS:
        raise _BadUpsert(f"at most {MAX_CONTACT_KEYS} {label} attributes")
    out: dict = {}
    for k, v in raw.items():
        key = str(k or "").strip()
        if not key:
            continue
        if len(key) > MAX_FIELD_LEN:
            raise _BadUpsert(f"{label} attribute name is too long")
        if isinstance(v, (dict, list)):
            raise _BadUpsert(
                f"{label} attribute {key!r} must be a scalar, not a nested object")
        if v is None:
            continue
        val = str(v).strip()
        if len(val) > MAX_FIELD_LEN:
            raise _BadUpsert(
                f"{label} attribute {key!r} is too long "
                f"(max {MAX_FIELD_LEN} characters)")
        if val:
            out[key] = val
    return out or None


def _derive_new_contact_key(
    ref: str, identities: object, emails: object,
) -> "tuple[str, str] | None":
    """Pick the ``(platform, stable_id)`` key for a brand-new contact, or ``None``.

    The address-book case (spec §0): the bot records someone it emails who is not a user.
    A new contact is a ``Person`` **without** ``membership`` — keyed on a stable identity,
    NOT minting any new id scheme (it reuses ``upsert_entry``'s ``person_id`` mint).
    Precedence:

      1. An explicit messaging identity in the body (``identities[0]`` platform+id) — the
         most stable handle.
      2. The ``ref`` itself, when it is an email address.
      3. An email supplied in the body (the primary, else the first).

    Returns ``None`` when none is available (a name alone) — the caller then returns a
    clean "give me an email or identity to create" error rather than minting an
    un-keyable record. Email keys are casefolded so ``Dana@…`` and ``dana@…`` are one
    contact (matches the resolver's case-insensitive email match)."""
    for i in (identities if isinstance(identities, list) else []):
        if isinstance(i, dict):
            platform = str(i.get("platform") or "").strip()
            ident = str(i.get("id") or "").strip()
            if platform and ident:
                return platform, ident
    if isinstance(ref, str) and "@" in ref and not ref.strip().startswith("@"):
        return "email", ref.strip().casefold()
    chosen: "str | None" = None
    for e in (emails if isinstance(emails, list) else []):
        if isinstance(e, dict):
            addr = str(e.get("addr") or "").strip()
            if not addr:
                continue
            if e.get("rank") == "primary":
                chosen = addr
                break
            if chosen is None:
                chosen = addr
    if chosen:
        return "email", chosen.casefold()
    return None


def register_directory_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/directory/*`` routes on the Flask app."""

    @app.post("/api/directory/lookup")
    def api_directory_lookup() -> ResponseReturnValue:
        """Resolve one ``Person`` in the *calling* bot's directory by ``ref``.

        Body: ``{"ref": "@dana" | "Dana Lopez" | "dana@example.net" | "pers_…"}``. The
        acting bot is the unix-socket peer uid, bound server-side — any ``bot`` in the
        body is ignored, and the lookup can only ever read the caller's own directory.
        An unresolvable ref (including one that names another bot's person) returns
        ``{"person": null}`` — never another bot's data, never an error.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            # No peer-bound identity (TCP, or unknown uid) → never serve directory data.
            # Fail-closed; this is the cross-bot boundary.
            return jsonify({"error": "caller not a recognized bot"}), 403

        body = request.get_json(silent=True) or {}
        ref = body.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            return jsonify({"error": "ref is required"}), 400
        ref = ref.strip()

        net = load_network(network_path)
        try:
            person = udr.resolve_person(net, bot_id, ref)
        except Exception as exc:  # noqa: BLE001 — never 500 a bot read; degrade to a miss
            log.warning("directory_lookup resolve for %s failed: %s", bot_id, exc)
            return jsonify({"person": None})

        # A blocked person is never advertised to the bot — same exclusion the digest
        # applies (bot_view.is_blocked). The two read paths MUST agree; suppressing here
        # keeps the lookup from exposing a blocked user's identities/emails during the
        # window where the block index and the allowFrom revoke are out of sync.
        suppressed_blocked = person is not None and bot_view.is_blocked(person)
        if suppressed_blocked:
            person = None

        _audit(bot_id, "lookup", {
            "ref_kind": _ref_kind(ref),
            "hit": person is not None,
            "suppressed_blocked": suppressed_blocked,
        })
        return jsonify({
            "person": bot_view.person_to_bot_dict(person) if person is not None else None,
        })

    @app.get("/api/directory/digest")
    def api_directory_digest() -> ResponseReturnValue:
        """The size-bounded roster digest for the *calling* bot.

        Returns ``{persons:[row...], total, shown, cap}`` — admitted users (non-blocked)
        and named contacts, sorted + capped, each a slim row (display, handle, identity,
        primary email, role). The bot injects this every turn (TTL-cached) so it stops
        hand-typing IDs/emails. Resolves through ``resolve_persons`` (the one read path)
        and projects through ``bot_view`` (no profile, no audit). Bound to the caller via
        the socket peer uid; 403 for TCP / unknown uid.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            return jsonify({"error": "caller not a recognized bot"}), 403

        net = load_network(network_path)
        try:
            persons = udr.resolve_persons(net, bot_id)
        except Exception as exc:  # noqa: BLE001 — never 500; degrade to an empty digest
            log.warning("directory digest resolve for %s failed: %s", bot_id, exc)
            return jsonify({"persons": [], "total": 0, "shown": 0, "cap": DIGEST_CAP})

        return jsonify(bot_view.build_digest(persons, cap=DIGEST_CAP))

    @app.post("/api/directory/upsert")
    def api_directory_upsert() -> ResponseReturnValue:
        """Record/correct a person's identity + contact data in the *calling* bot's
        directory — the write half that closes the §0 address-book bug.

        Body: ``{"ref": <who>, "emails"?, "contact"?, "identities"?, "names"?}``. The
        acting bot is the unix-socket peer uid (bound server-side); the write lands only in
        that bot's own ``directory/{bot}.json`` and is stamped ``provenance:"bot-asserted"``
        (HARDCODED — never from the body). ``ref`` resolves to an existing Person (admitted
        user OR saved contact) → **update**; if it resolves to nobody, a **new contact**
        (a Person *without* membership) is minted when an email or identity is available to
        key on. Returns ``{ok, created, person}`` where ``person`` is the sanitized
        bot-view (no profile_ref / audit) or ``null``.

        Never touches membership/roles/admission (invariant #2 — rejected at the door and
        structurally unreachable in ``upsert_entry``); never forges or downgrades
        ``operator-verified`` data (invariant #3 — enforced in ``upsert_entry``); never 500s
        (validation → 400, I/O fault → graceful error)."""
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            return jsonify({"error": "caller not a recognized bot"}), 403

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        # The bot asserts identity, NEVER authority (invariant #2): refuse any
        # membership/role/admission key or the private-profile link outright.
        forbidden = sorted(_FORBIDDEN_BODY_KEYS & set(body.keys()))
        if forbidden:
            return jsonify({
                "error": (
                    f"directory_upsert cannot set {forbidden}; it records identity and "
                    "contact only — never roles, membership, admission, or the behavioral "
                    "profile (those are operator-gated)"),
            }), 400

        ref = body.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            return jsonify({"error": "ref is required"}), 400
        ref = ref.strip()

        try:
            emails = _parse_emails(body.get("emails"))
            identities = _parse_identities(body.get("identities"))
            contact = _parse_open_map(body.get("contact"), "contact")
            names = _parse_open_map(body.get("names"), "names")
        except _BadUpsert as e:
            return jsonify({"error": str(e)}), 400

        if emails is None and identities is None and contact is None and names is None:
            return jsonify({
                "error": ("nothing to write — provide at least one of emails, contact, "
                          "identities, names"),
            }), 400

        net = load_network(network_path)
        shared = _shared_dir_from_net(net)

        # Resolve the target identity through the ONE canonical matcher (invariant #1) —
        # the same match the read tools run, scoped to this bot by construction.
        try:
            target = udr.resolve_identity(net, bot_id, ref)
        except Exception as exc:  # noqa: BLE001 — degrade cleanly; never write blind
            log.warning("directory_upsert resolve for %s failed: %s", bot_id, exc)
            return jsonify({
                "ok": False,
                "error": "the directory is temporarily unavailable; try again shortly",
            }), 503

        # A resolved-but-blocked person is invisible to the bot (read AND write) — no-op,
        # advertise nothing, write nothing (parity with the lookup's blocked suppression).
        if target is not None:
            try:
                existing_person = udr.resolve_person(net, bot_id, target)
            except Exception:  # noqa: BLE001
                existing_person = None
            if existing_person is not None and bot_view.is_blocked(existing_person):
                _audit(bot_id, "upsert", {
                    "ref_kind": _ref_kind(ref), "created": False,
                    "ok": False, "outcome": "blocked_noop"})
                return jsonify({"ok": True, "created": False, "person": None})

        try:
            directory = uds.load_directory(shared, bot_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("directory_upsert load for %s failed: %s", bot_id, exc)
            return jsonify({
                "ok": False,
                "error": "the directory is temporarily unavailable; try again shortly",
            }), 503

        if target is None:
            key = _derive_new_contact_key(ref, body.get("identities"), body.get("emails"))
            if key is None:
                return jsonify({
                    "ok": False,
                    "error": ("no one matches that reference. To create a new contact, "
                              "include an email address or a messaging identity "
                              "(platform + id) — a name alone has no stable handle."),
                }), 422
            platform, stable_id = key
            # The original ``ref`` may have been a name that missed while the email /
            # identity we just derived ALREADY belongs to an existing Person (e.g. the bot
            # referenced them by a new name but their email is on file). Re-resolve the
            # derived handle through the canonical matcher so we UPDATE that person instead
            # of minting a duplicate row (invariant #1).
            try:
                owner = udr.resolve_identity(
                    net, bot_id, stable_id, directory=directory)
            except Exception:  # noqa: BLE001 — best-effort dedup; fall back to minting
                owner = None
            if owner is not None:
                target = owner
                platform, stable_id = owner
        else:
            platform, stable_id = target

        existing_entry = uds.get_entry(directory, platform, stable_id)
        # "created" ⇔ a brand-NEW Person: the ref resolved to nobody (target is None) AND
        # no directory row already holds the derived key. An admitted roster user with no
        # directory row yet resolves (target set) → this is an UPDATE that mints its first
        # directory row, not a new Person.
        created = target is None and existing_entry is None

        # Bound runaway/spam growth: refuse a CREATE past the cap; updates always allowed.
        if created and len(directory.get("persons") or {}) >= MAX_DIRECTORY_PERSONS:
            log.warning(
                "directory_upsert: %s at person cap (%d) — refusing new contact",
                bot_id, MAX_DIRECTORY_PERSONS)
            return jsonify({
                "ok": False,
                "error": (f"this directory is at its {MAX_DIRECTORY_PERSONS}-contact cap; "
                          "an operator must prune it before new contacts can be added"),
            }), 429

        # ``contact`` / ``names`` are open maps with no per-key provenance, so the bot
        # AUGMENTS them (shallow-merge over the stored map) rather than replacing — a bot
        # write adds/updates keys but never wipes operator-entered attributes wholesale.
        # (emails/identities carry per-row provenance and are merged-preserving-stronger
        # inside upsert_entry, so they get the strict invariant-#3 protection instead.)
        if contact is not None:
            contact = {**((existing_entry or {}).get("contact") or {}), **contact}
        if names is not None:
            names = {**((existing_entry or {}).get("names") or {}), **names}

        try:
            uds.upsert_entry(
                shared, bot_id, platform, stable_id,
                by=bot_id, provenance="bot-asserted",
                emails=emails, identities=identities,
                contact=contact, names=names)
        except ValueError as e:
            # e.g. the single-primary invariant tripped in storage's re-validation.
            return jsonify({"ok": False, "error": str(e)}), 400
        except OSError as e:
            log.warning("directory_upsert write for %s failed: %s", bot_id, e)
            return jsonify({
                "ok": False,
                "error": "the directory write failed; try again shortly",
            }), 503

        try:
            person = udr.resolve_person(net, bot_id, (platform, stable_id))
        except Exception as exc:  # noqa: BLE001 — write succeeded; just can't re-render
            log.warning("directory_upsert re-resolve for %s failed: %s", bot_id, exc)
            person = None
        # Defensive: never advertise a blocked person even post-write.
        if person is not None and bot_view.is_blocked(person):
            person = None

        _audit(bot_id, "upsert", {
            "ref_kind": _ref_kind(ref),
            "created": created,
            "fields": sorted(
                f for f, v in (("emails", emails), ("identities", identities),
                               ("contact", contact), ("names", names)) if v is not None),
            "ok": True,
        })
        return jsonify({
            "ok": True,
            "created": created,
            "person": bot_view.person_to_bot_dict(person) if person is not None else None,
        })
