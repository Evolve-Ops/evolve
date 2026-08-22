"""``resolve_person`` — the single read path that joins roster + directory into a Person.

Spec: ``docs/spec-user-directory-2026-06-22.md`` §4 + §10 invariant #1. Every consumer
(the Users-page GET today; bot injection + the bot lookup tool in Phase 3) resolves
through this module, and **nothing else reads the directory store** — that one
indirection is the R3 forward-compat seam (spec §8) and what keeps the admin's and the
bot's view of a person from diverging.

The join, per identity:

  * ``membership`` + base identity + display name + activity ← the **roster**
    (``roster_resolver.resolve_roster`` / ``resolve_display_name`` / ``resolve_email``).
    ``membership`` is present **iff** the identity is admitted (in the resolved roster);
    absent ⇒ a pure **contact**.
  * ``person_id`` + ``emails`` + ``contact`` + extra ``identities`` + ``names`` overrides
    + ``profile_ref`` ← the **directory store** (``user_directory.storage``). Empty in
    Phase 1 (the write path isn't wired) — which is correct, not a gap.

``resolve_person(shared_dir, network, bot_id, ref)`` resolves ``ref`` by stable id,
``@handle``, or email; :func:`resolve_persons` returns the whole directory (admitted
users *and* contacts) as one list. Both accept pre-loaded ``overlay`` / ``activity`` /
``directory`` / ``roster`` so a caller resolving many identities (the GET) does the I/O
once.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import capabilities as cap
from .. import roster_overlay as ro
from .. import roster_resolver as rr
from . import model
from . import storage


log = logging.getLogger(__name__)


# ── The join: one identity → one Person ──────────────────────────────────


def build_person(
    network: dict,
    bot_id: str,
    platform: str,
    stable_id: str,
    *,
    roster_record: "rr.RosterRecord | None",
    entry: "dict | None",
    overlay: "dict | None" = None,
) -> model.Person:
    """Assemble one ``Person`` from a roster record + a directory entry — the SOLE join.

    ``roster_record`` present ⇒ an admitted user (``membership`` is built from it);
    ``None`` ⇒ a contact (``membership is None``). ``entry`` is the directory store row
    (``None`` ⇒ no directory-owned fields yet). This is the one place the two halves are
    combined; callers never hand-merge them.
    """
    overlay = overlay or {}

    # person_id: persisted value if present, else the deterministic default (stable
    # across reloads without a write — see storage.person_id_for).
    person_id = (entry or {}).get("person_id") or storage.person_id_for(
        bot_id, platform, stable_id)

    # names: roster display name, then operator/bot-stored name overrides win on top.
    names: dict[str, Any] = {}
    if roster_record and roster_record.display_name:
        names["display"] = roster_record.display_name
    stored_names = (entry or {}).get("names")
    if isinstance(stored_names, dict):
        names.update({k: v for k, v in stored_names.items() if v})

    # membership ← roster (a view over the existing roles, not a re-fork).
    membership: "model.Membership | None" = None
    if roster_record is not None:
        caps = sorted(cap.resolve_role_capabilities(
            roster_record.role,
            overlay_role_bindings=(overlay.get("role_bindings") or {})))
        membership = model.Membership(
            admitted=True,
            role=roster_record.role,
            capabilities=caps,
            channels=[roster_record.platform],
        )

    # identities: base identity from the roster (channel-captured) + extras from the
    # store, deduped by (platform, id).
    base_handle = roster_record.handle if roster_record else None
    identities = _merge_identities(
        platform, stable_id, base_handle,
        admitted=roster_record is not None, entry=entry)

    # Directory-owned PII fields (all empty/None in Phase 1).
    #
    # NB: the roster's single channel-captured email (roster_record.email — Slack today)
    # is intentionally NOT folded into emails[] here. Phase 1 keeps the join
    # unambiguous — emails[] is exactly the directory store's content — and that email
    # stays surfaced via the existing approved-entry ``email`` field. Merging the
    # channel-captured roster email into emails[] is a Phase-2 decision (it lands with
    # the email-management UI), not a silent Phase-1 behavior change.
    emails = model.coerce_emails((entry or {}).get("emails"))
    contact = dict((entry or {}).get("contact") or {})
    profile_ref = (entry or {}).get("profile_ref") or None
    audit = model.coerce_audit((entry or {}).get("audit"))

    return model.Person(
        person_id=person_id,
        names=names,
        identities=identities,
        emails=emails,
        contact=contact,
        membership=membership,
        profile_ref=profile_ref,
        audit=audit,
    )


def _merge_identities(
    platform: str, stable_id: str, base_handle: "str | None",
    *, admitted: bool, entry: "dict | None",
) -> list[model.Identity]:
    """Stored identities + a synthesized base identity for ``(platform, stable_id)``.

    Deduped by ``(platform, id)``. The base identity's ``source`` is ``channel-captured``
    for an admitted user (the roster learned it from the channel/allowlist); for a
    contact with no stored identity for its own key it falls back to ``channel-captured``
    as a neutral default (the upsert path normally records the real provenance).
    """
    stored = model.coerce_identities((entry or {}).get("identities"))
    have = {(i.platform, i.id) for i in stored}
    out = list(stored)
    if (platform, stable_id) not in have:
        out.insert(0, model.Identity(
            platform=platform, id=stable_id, handle=base_handle,
            source="channel-captured"))
    return out


# ── Public resolve entry points ──────────────────────────────────────────


def resolve_person(
    network: dict,
    bot_id: str,
    ref: Any,
    *,
    shared_dir: "Path | None" = None,
    overlay: "dict | None" = None,
    activity: "dict[str, dict[str, Any]] | None" = None,
    directory: "dict | None" = None,
    roster: "list[rr.RosterRecord] | None" = None,
) -> "model.Person | None":
    """Resolve a single ``Person`` by ``ref`` (stable id, ``@handle``, or email).

    Returns ``None`` when ``ref`` matches neither an admitted roster identity nor a
    directory entry. ``membership`` is determined **authoritatively** from ``roster``
    (the resolved admitted set) — an id present there is an admitted user, an id absent
    is a contact — so a contact can never be mislabeled admitted, nor vice-versa.

    ``ref`` forms:
      * ``(platform, stable_id)`` tuple — exact identity (fast path; the GET uses this).
      * ``"@handle"`` — matched against roster/identity handles (case-insensitive).
      * ``"someone@example.com"`` — matched against directory emails / the roster email.
      * any other string — matched as a bare ``stable_id`` first, then (when no id
        matches) as a **display name** (case-insensitive, against the roster display
        name and the directory ``names`` overrides). The name fallback is what lets the
        bot's ``directory_lookup`` resolve "Dana Lopez" — a single canonical matcher both
        the tool and any other consumer share (invariant #1).

    ``overlay`` / ``activity`` / ``directory`` / ``roster`` may be pre-loaded by the
    caller to avoid repeat I/O; any left ``None`` are loaded here.
    """
    shared_dir = Path(shared_dir) if shared_dir is not None else rr._shared_dir(network)
    if overlay is None:
        overlay = ro.load_overlay(shared_dir, bot_id)
    if directory is None:
        directory = storage.load_directory(shared_dir, bot_id)
    if roster is None:
        roster = rr.resolve_roster(
            network, bot_id, shared_dir=shared_dir,
            overlay=overlay, activity=activity)

    index = {(r.platform, r.stable_id): r for r in roster}
    target = _match_ref(ref, index, directory)
    if target is None:
        return None
    platform, stable_id = target
    rec = index.get((platform, stable_id))
    entry = storage.get_entry(directory, platform, stable_id)
    if rec is None and entry is None:
        return None
    return build_person(
        network, bot_id, platform, stable_id,
        roster_record=rec, entry=entry, overlay=overlay)


def resolve_identity(
    network: dict,
    bot_id: str,
    ref: Any,
    *,
    shared_dir: "Path | None" = None,
    overlay: "dict | None" = None,
    activity: "dict[str, dict[str, Any]] | None" = None,
    directory: "dict | None" = None,
    roster: "list[rr.RosterRecord] | None" = None,
) -> "tuple[str, str] | None":
    """Resolve ``ref`` to the ``(platform, stable_id)`` store key it names, or ``None``.

    The same canonical match :func:`resolve_person` runs (``_match_ref`` over the roster
    index + the directory) — exposed without building the full ``Person`` so the **write**
    path (``directory_upsert``) can find the exact entry key to upsert into. Sharing the one
    matcher is invariant #1: the bot's write targets the *same* identity the bot's read
    would resolve, and a ``ref`` that names another bot's person (absent from this bot's
    roster/directory) returns ``None`` — never a foreign key. Returns ``None`` for an
    unresolvable ref (the caller then decides whether to mint a new contact)."""
    shared_dir = Path(shared_dir) if shared_dir is not None else rr._shared_dir(network)
    if overlay is None:
        overlay = ro.load_overlay(shared_dir, bot_id)
    if directory is None:
        directory = storage.load_directory(shared_dir, bot_id)
    if roster is None:
        roster = rr.resolve_roster(
            network, bot_id, shared_dir=shared_dir,
            overlay=overlay, activity=activity)
    index = {(r.platform, r.stable_id): r for r in roster}
    return _match_ref(ref, index, directory)


def resolve_persons(
    network: dict,
    bot_id: str,
    *,
    shared_dir: "Path | None" = None,
    overlay: "dict | None" = None,
    activity: "dict[str, dict[str, Any]] | None" = None,
    directory: "dict | None" = None,
    roster: "list[rr.RosterRecord] | None" = None,
) -> list[model.Person]:
    """The whole per-bot directory as ``Person``\\ s — admitted users **and** contacts.

    Admitted users come from the roster (``membership`` present); contacts are directory
    entries with no matching roster identity (``membership is None``). One model, one
    resolver (invariant #4). Used by tests + later phases (the contacts list, the bot's
    directory digest); the Users-page GET resolves per-identity instead.
    """
    shared_dir = Path(shared_dir) if shared_dir is not None else rr._shared_dir(network)
    if overlay is None:
        overlay = ro.load_overlay(shared_dir, bot_id)
    if directory is None:
        directory = storage.load_directory(shared_dir, bot_id)
    if roster is None:
        roster = rr.resolve_roster(
            network, bot_id, shared_dir=shared_dir,
            overlay=overlay, activity=activity)

    persons: list[model.Person] = []
    seen: set[tuple[str, str]] = set()
    for rec in roster:
        entry = storage.get_entry(directory, rec.platform, rec.stable_id)
        persons.append(build_person(
            network, bot_id, rec.platform, rec.stable_id,
            roster_record=rec, entry=entry, overlay=overlay))
        seen.add((rec.platform, rec.stable_id))
    for platform, stable_id, entry in _directory_identities(directory):
        if (platform, stable_id) in seen:
            continue
        persons.append(build_person(
            network, bot_id, platform, stable_id,
            roster_record=None, entry=entry, overlay=overlay))
    return persons


# ── ref matching ─────────────────────────────────────────────────────────


def _directory_identities(directory: dict) -> list[tuple[str, str, dict]]:
    """``(platform, stable_id, entry)`` for every keyed directory row.

    Keys are ``roster_overlay.identity_key`` form (``"<platform>:<stable_id>"``); a
    malformed key (no ``:``) is skipped.
    """
    out: list[tuple[str, str, dict]] = []
    for key, entry in (directory.get("persons") or {}).items():
        if not isinstance(entry, dict) or ":" not in key:
            continue
        platform, _, stable_id = key.partition(":")
        out.append((platform, stable_id, entry))
    return out


def _match_ref(
    ref: Any,
    index: dict[tuple[str, str], "rr.RosterRecord"],
    directory: dict,
) -> "tuple[str, str] | None":
    """Resolve ``ref`` to a ``(platform, stable_id)`` known to either store, or ``None``."""
    if isinstance(ref, (tuple, list)) and len(ref) == 2:
        return str(ref[0]), str(ref[1])
    if not isinstance(ref, str):
        return None
    s = ref.strip()
    if not s:
        return None
    if s.startswith("@"):
        return _match_handle(s[1:], index, directory)
    if "@" in s:
        return _match_email(s, index, directory)
    return _match_stable_id(s, index, directory) or _match_name(s, index, directory)


def _match_stable_id(
    sid: str, index: dict[tuple[str, str], "rr.RosterRecord"], directory: dict,
) -> "tuple[str, str] | None":
    for (platform, stable_id) in index:
        if stable_id == sid:
            return platform, stable_id
    for platform, stable_id, entry in _directory_identities(directory):
        if stable_id == sid:
            return platform, stable_id
        # Symmetry with _match_handle/_match_email (which already scan the stored
        # identity/email rows): match an id carried in a stored ``identities[]`` row even
        # when it isn't the entry's own key. Without this, a person keyed on (say) an email
        # but holding a ``slack:U1`` identity is invisible to a bare-id lookup — and the
        # write path would mint a DUPLICATE row for them (breaks invariant #1: read and
        # write must resolve to the same identity).
        for ident in model.coerce_identities(entry.get("identities")):
            if ident.id == sid:
                return platform, stable_id
    return None


def _match_name(
    name: str, index: dict[tuple[str, str], "rr.RosterRecord"], directory: dict,
) -> "tuple[str, str] | None":
    """Resolve a bare string against display names (case-insensitive, exact).

    Checks the roster's resolved ``display_name`` and the directory store's ``names``
    overrides (``display`` / ``goes_by`` / ``legal``). Exact (whole-string) casefold
    match — a deliberately conservative matcher (no fuzzy/substring).

    **Ambiguity misses.** If the name matches more than one *distinct* identity (two
    different people share a display name), this returns ``None`` rather than an
    arbitrary one — a lookup must never silently resolve to the wrong person. A partial
    name also misses. The precise paths (id / ``@handle`` / email) are how a caller
    disambiguates a collided name.
    """
    n = name.strip().casefold()
    if not n:
        return None
    matches: set[tuple[str, str]] = set()
    for (platform, stable_id), rec in index.items():
        if rec.display_name and rec.display_name.strip().casefold() == n:
            matches.add((platform, stable_id))
    for platform, stable_id, entry in _directory_identities(directory):
        names = entry.get("names")
        if not isinstance(names, dict):
            continue
        if any(isinstance(v, str) and v.strip().casefold() == n
               for v in names.values()):
            matches.add((platform, stable_id))
    # Exactly one distinct identity ⇒ resolve it; zero or ambiguous ⇒ miss.
    return next(iter(matches)) if len(matches) == 1 else None


def _match_handle(
    handle: str, index: dict[tuple[str, str], "rr.RosterRecord"], directory: dict,
) -> "tuple[str, str] | None":
    h = handle.lstrip("@").casefold()
    for (platform, stable_id), rec in index.items():
        rh = (rec.handle or "").lstrip("@").casefold()
        if rh and rh == h:
            return platform, stable_id
    for platform, stable_id, entry in _directory_identities(directory):
        for ident in model.coerce_identities(entry.get("identities")):
            ih = (ident.handle or "").lstrip("@").casefold()
            if ih and ih == h:
                return platform, stable_id
    return None


def _match_email(
    addr: str, index: dict[tuple[str, str], "rr.RosterRecord"], directory: dict,
) -> "tuple[str, str] | None":
    a = addr.strip().casefold()
    # Directory emails first — they are the canonical contact-email store.
    for platform, stable_id, entry in _directory_identities(directory):
        for email in model.coerce_emails(entry.get("emails")):
            if email.addr.strip().casefold() == a:
                return platform, stable_id
    # Then the roster's single channel-captured email (Slack today).
    for (platform, stable_id), rec in index.items():
        if rec.email and rec.email.strip().casefold() == a:
            return platform, stable_id
    return None
