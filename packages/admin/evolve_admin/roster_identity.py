"""evolve_admin.roster_identity — D1: one person, one roster row across platforms.

Operator decision **D1** (spec ``internal/spec-users-meta-2026-06-15.md`` §M1,
invariant 6): *a person is ONE roster row; platform is a multi-valued attribute
of that row, not a row discriminator.* Invariant 2's ``(platform, stable_id)``
stays the **admission key** — what the channel boundary matches on — but it is
an index INTO a person, never the person's identity. When an already-known
person turns up on a second platform, admission must resolve to their existing
row and append to its ``external_ids``; it must never mint a second row.

This module is the primitive that makes that possible. It is deliberately NOT
identity matching.

The central constraint — we cannot detect that two ids are one human
====================================================================

A Discord snowflake and a Telegram user id share nothing. There is no signal
that reliably links them. **So nothing here ever infers identity from a display
name, a username, an email, or any fuzzy comparison.** Roles attach to admitted
identities (invariant 2), so a wrong link is a *privilege transfer*: someone
sets their Telegram display name to an admin's and inherits admin. Every lookup
below is an exact string match on the recorded id, and every *new* link is an
operator assertion made through :func:`link_external_id` — never a guess.

If two ids cannot be linked without an operator asserting it, the correct
behavior is to keep them separate and make the choice visible. A stranger IS a
new person until an operator says otherwise.

What a "person row" is today
============================

The rows that carry ``external_ids`` in ``network.json`` are:

* ``bots.<bot_id>.primary_user`` — a genuine person row: one block, one human,
  ``{channel: [id, ...]}`` across platforms. :class:`PersonRef` kind
  ``"primary_user"``.
* ``pod.admins`` — **a bag of admin identities, not a person.** Its
  ``external_ids`` holds every pod admin's ids mixed together. It is resolvable
  (an id in it IS already known, which is what admission needs) but it is *not*
  a valid link target: appending to it asserts "this id is a pod admin", which
  is a privilege grant, not a same-person assertion. :func:`link_external_id`
  refuses it and points at :func:`evolve_admin.evo.identity.claim_admin`, which
  already appends (D1-correct) rather than replacing.

There is deliberately no new on-disk store here. D1 is served by resolving
*into* the rows that already exist; a general person table is a much larger
build and is not what this bite is for.

Relationship to ``user_directory`` — deliberately not the same thing
====================================================================

``evolve_admin.user_directory`` has its own ``Person`` with a ``person_id``,
keyed **per identity** (``storage.person_id_for(bot_id, platform, stable_id)``
is a digest of the admission key, so one human on two platforms gets two ids).
Its docstring names the fix: *"a future phase that merges two identities into
one Person can override the stored person_id."* That merge — collapsing two
**pre-existing** rows — is the separately deferred phase, and it is explicitly
out of scope here.

This module is the other half of the problem, and the half that comes first:
**preventing** the duplicate at the source rather than merging duplicates after
the fact (spec §M1, D1). It resolves an admission key against the
``network.json`` rows that already model a person across platforms, so a known
person's second platform is recognised at admission time. Nothing here writes
the directory store, and :func:`person_key_for` is a *row* key, not a
``person_id`` — when the merge phase lands, that is where the two notions get
unified. Keeping them separate until then is intentional: silently equating
them would be exactly the kind of guess this module exists to refuse.

Seams
=====

* :func:`resolve_admission` — the admission-path consult. "Is this
  ``(channel, external_id)`` already a person this bot knows?" Used by the
  pairing auto-approver's trigger resolution and by the Users-page approve
  path. Unknown ⇒ ``person is None`` ⇒ the admission still proceeds and the id
  becomes a new roster row, which is correct.
* :func:`link_external_id` — **the seam the M1-B4 operator UI calls** to assert
  "this platform id is the same person as this existing row". Appends via
  :func:`evolve_admin.external_ids.add_external_id`; refuses to create a row,
  and refuses an id already recorded on a different row unless ``force=True``
  (the same escape hatch precedent as ``identity.claim_primary``).
* :func:`person_key_for` — the stable string a projection (``RosterRecord``,
  a Users-page group header) uses to say "these two rows are one person".

Reads and mutates the in-memory ``network`` dict only; the caller persists via
:func:`evolve_admin.config.save_network`. Import-light on purpose — stdlib plus
:mod:`evolve_admin.external_ids` (which is itself stdlib +
:mod:`evolve_admin.channel_registry`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import external_ids as _ext_ids


__all__ = [
    "PersonRef",
    "POD_ADMINS",
    "PRIMARY_USER",
    "POD_ADMIN",
    "primary_user_ref",
    "person_rows",
    "row_block",
    "row_exists",
    "ids_for_row",
    "rows_holding",
    "resolve_person",
    "person_key_for",
    "PersonLinkError",
    "link_external_id",
    "unlink_external_id",
    "AdmissionResolution",
    "resolve_admission",
]


PRIMARY_USER = "primary_user"
POD_ADMIN = "pod_admin"


# ── Person references ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PersonRef:
    """A reference to one ``external_ids``-carrying row in ``network.json``.

    ``kind`` is :data:`PRIMARY_USER` (then ``bot_id`` names the bot whose
    ``primary_user`` block this is) or :data:`POD_ADMIN` (``bot_id`` is None;
    see the module docstring — that row is a bag, not a person).

    Frozen + hashable so callers can compare and set-dedupe refs; ``key`` is
    the stable string form for projections and log lines.
    """

    kind: str
    bot_id: Optional[str] = None

    @property
    def key(self) -> str:
        """Stable identifier — ``"pod_admin"`` / ``"primary_user:<bot_id>"``."""
        return f"{self.kind}:{self.bot_id}" if self.bot_id else self.kind

    @property
    def is_person(self) -> bool:
        """True iff this row models ONE human (i.e. not the pod-admin bag)."""
        return self.kind == PRIMARY_USER

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key


POD_ADMINS = PersonRef(POD_ADMIN)
"""The pod-admin identity bag. Resolvable; never a link target."""


def primary_user_ref(bot_id: str) -> PersonRef:
    """Ref to ``bots.<bot_id>.primary_user``."""
    return PersonRef(PRIMARY_USER, bot_id)


def person_rows(
    network: Any, *, bot_id: Optional[str] = None,
) -> tuple[PersonRef, ...]:
    """Every ``external_ids``-carrying row, in **resolution order**.

    Pod admins first, then ``primary_user`` rows. That order mirrors the
    existing role precedence everywhere else in the codebase (``resolve_role``
    checks pod-admin before primary; the auto-approver's ``"known pod admin"``
    trigger outranks ``"bot primary owner"``), so a lookup that returns the
    first hit returns the same row those paths would pick.

    ``bot_id`` scopes to the rows one bot's admission decision can see — the
    pod-admin bag plus that bot's own primary. Without it, every bot's primary
    row is included, sorted by bot id for determinism.

    Rows are listed whether or not they currently exist on disk; use
    :func:`row_exists` to filter.
    """
    if bot_id is not None:
        return (POD_ADMINS, primary_user_ref(bot_id))
    bots = network.get("bots") if isinstance(network, dict) else None
    ids = sorted(bots) if isinstance(bots, dict) else []
    return (POD_ADMINS, *(primary_user_ref(b) for b in ids))


# ── Row access ───────────────────────────────────────────────────────────


def row_block(network: Any, ref: PersonRef) -> Optional[dict]:
    """The raw dict backing ``ref``, or None when the row does not exist.

    Never creates anything — this is the read side.
    """
    if not isinstance(network, dict) or not isinstance(ref, PersonRef):
        return None
    if ref.kind == POD_ADMIN:
        pod = network.get("pod")
        admins = pod.get("admins") if isinstance(pod, dict) else None
        return admins if isinstance(admins, dict) else None
    if ref.kind == PRIMARY_USER and ref.bot_id:
        bots = network.get("bots")
        bot = bots.get(ref.bot_id) if isinstance(bots, dict) else None
        block = bot.get("primary_user") if isinstance(bot, dict) else None
        return block if isinstance(block, dict) else None
    return None


def row_exists(network: Any, ref: PersonRef) -> bool:
    """True iff ``ref``'s block is present in ``network``."""
    return row_block(network, ref) is not None


def ids_for_row(network: Any, ref: PersonRef, channel: Any) -> list[str]:
    """Every id ``ref`` records on ``channel`` (possibly empty)."""
    return _ext_ids.ids_for(row_block(network, ref), channel)


# ── Resolution (exact match only — never heuristic) ──────────────────────


def rows_holding(
    network: Any,
    channel: Any,
    external_id: Any,
    *,
    bot_id: Optional[str] = None,
) -> tuple[PersonRef, ...]:
    """Every row that records this exact ``(channel, external_id)``.

    **Exact string match on the recorded id.** No name comparison, no fuzzy
    match, no normalization beyond the channel-case + whitespace handling
    :mod:`evolve_admin.external_ids` already applies to both sides.

    More than one row can legitimately hold the same id: one human is commonly
    both a pod admin and the primary of several bots. That is why this returns
    a tuple and :func:`resolve_person` returns the first — and why
    :func:`link_external_id` treats a hit as "needs an operator assertion",
    not as proof of a different human.
    """
    if external_id is None:
        return ()
    needle = str(external_id).strip()
    if not needle:
        return ()
    return tuple(
        ref for ref in person_rows(network, bot_id=bot_id)
        if _ext_ids.has_id(row_block(network, ref), channel, needle)
    )


def resolve_person(
    network: Any,
    channel: Any,
    external_id: Any,
    *,
    bot_id: Optional[str] = None,
) -> Optional[PersonRef]:
    """The row this ``(channel, external_id)`` indexes into, or None.

    THE resolution lookup — "is this admission key already known, and to
    whom?". First hit in :func:`person_rows` order (pod admins, then primaries).
    ``None`` means genuinely unknown: not "probably a new person", but "no row
    records this id", which is the only thing we can honestly know.
    """
    hits = rows_holding(network, channel, external_id, bot_id=bot_id)
    return hits[0] if hits else None


def person_key_for(
    network: Any,
    bot_id: str,
    channel: Any,
    external_id: Any,
) -> Optional[str]:
    """Stable person key for one admitted identity on ``bot_id``, or None.

    The string a projection uses to collapse several platform rows onto one
    person (D1). ``None`` for an admitted id that no ``network.json`` row
    claims — the honest answer for a participant we only know as an admission
    key. Scoped to the bot, so it answers "who is this, from this bot's point
    of view".
    """
    ref = resolve_person(network, channel, external_id, bot_id=bot_id)
    return ref.key if ref else None


# ── Explicit link (the M1-B4 seam) ───────────────────────────────────────


class PersonLinkError(ValueError):
    """Raised when a ``(channel, external_id)`` link cannot be applied —
    validation, a missing target row, or a conflicting existing holder."""


def link_external_id(
    network: dict,
    ref: PersonRef,
    channel: Any,
    external_id: Any,
    *,
    force: bool = False,
) -> list[str]:
    """Attach ``(channel, external_id)`` to the EXISTING row ``ref``. D1's write.

    **This is the seam the M1-B4 operator UI calls** when a human asserts "this
    platform id is the same person as this row". It is an operator assertion,
    never an inference: nothing in this function looks at names.

    Semantics:

    * **Appends, never replaces.** Goes through
      :func:`evolve_admin.external_ids.add_external_id`, so the row's other ids
      on this channel (and every other channel) survive. Idempotent. This is
      what makes it a different operation from
      ``routes_pairing._set_primary_user``, which deliberately *replaces* a
      channel's list because the operator is explicitly re-pairing.
    * **Links, never creates.** A missing target row raises rather than being
      conjured — recording a *first* primary is ``identity.claim_primary``'s
      job, and silently creating rows here would reintroduce the duplicate D1
      exists to prevent.
    * **Refuses the pod-admin bag as a target.** ``pod.admins.external_ids``
      mixes every admin's ids, so appending to it grants pod-admin rather than
      asserting sameness. Use ``identity.claim_admin``.
    * **Refuses a conflicting holder.** If any *other* row already records this
      id, raise :class:`PersonLinkError` unless ``force=True`` — the same
      escape-hatch precedent as ``identity.claim_primary``'s ``force``. The
      check is deliberately **pod-wide**, not scoped to this bot: an id
      recorded as another bot's primary may well be the same human (one
      operator often owns several bots), but we cannot know that, and the
      failure mode of guessing wrong is a privilege transfer. So we refuse and
      make the operator say so.
    * ``force=True`` **appends; it does not move.** The other row keeps its
      record — un-linking there is a separate explicit call
      (:func:`unlink_external_id`), so one confirm can never silently strip a
      different person's admission key.

    Returns the row's new id list for ``channel``. Caller persists via
    :func:`evolve_admin.config.save_network`.
    """
    if not isinstance(network, dict):
        raise PersonLinkError("network must be a mutable dict")
    if not isinstance(ref, PersonRef):
        raise PersonLinkError("ref must be a PersonRef")
    if ref.kind == POD_ADMIN:
        raise PersonLinkError(
            "pod.admins is a bag of admin identities, not a person row — "
            "appending to it grants pod-admin rather than asserting that two "
            "ids are the same human; use evo.identity.claim_admin instead"
        )
    if ref.kind != PRIMARY_USER or not ref.bot_id:
        raise PersonLinkError(f"unsupported person row kind: {ref.kind!r}")

    cleaned_channel = _clean_channel(channel)
    if cleaned_channel is None:
        raise PersonLinkError("channel is required (non-empty string)")
    needle = str(external_id).strip() if external_id is not None else ""
    if not needle:
        raise PersonLinkError("external_id is required (non-empty)")

    block = row_block(network, ref)
    if block is None:
        raise PersonLinkError(
            f"no {ref.kind} row for {ref.bot_id!r} — link attaches an id to an "
            f"EXISTING person; record the row first (evo.identity.claim_primary)"
        )

    conflicts = tuple(
        other for other in rows_holding(network, cleaned_channel, needle)
        if other != ref
    )
    if conflicts and not force:
        raise PersonLinkError(
            f"{cleaned_channel}:{needle} is already recorded on "
            f"{', '.join(c.key for c in conflicts)} — refusing to link it to "
            f"{ref.key} on a guess; if that really is the same person, pass "
            f"force=True (the other row is left untouched)"
        )

    return _ext_ids.add_external_id(block, cleaned_channel, needle)


def unlink_external_id(
    network: dict, ref: PersonRef, channel: Any, external_id: Any,
) -> bool:
    """Detach ``(channel, external_id)`` from ``ref``. True iff removed.

    The undo for :func:`link_external_id` (a mis-asserted link is an operator
    error, and D1 would be unusable without a way back). Only ever touches
    ``ref``'s own row. Caller persists.
    """
    if not isinstance(ref, PersonRef):
        raise PersonLinkError("ref must be a PersonRef")
    if ref.kind == POD_ADMIN:
        raise PersonLinkError(
            "use evo.identity.revoke_admin to remove a pod-admin identity"
        )
    block = row_block(network, ref)
    if block is None:
        return False
    return _ext_ids.remove_external_id(block, channel, external_id)


# ── Admission consult ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdmissionResolution:
    """What the admission path learned about one ``(channel, external_id)``.

    ``person`` is the row this admission key indexes into, or ``None`` when no
    row records it. ``None`` is a legitimate, common outcome — see
    :attr:`mints_new_row`.
    """

    channel: str
    external_id: str
    person: Optional[PersonRef]

    @property
    def is_known(self) -> bool:
        """True iff an existing row already records this id (D1: resolve to
        it — do NOT treat the admission as a new person)."""
        return self.person is not None

    @property
    def mints_new_row(self) -> bool:
        """True iff admitting this id introduces a person we have never seen.

        **Not a bug.** A stranger IS a new person until an operator asserts
        otherwise; the alternative is guessing, which is the privilege-transfer
        hazard D1 exists to avoid. Linking them to an existing person later is
        :func:`link_external_id`.
        """
        return self.person is None

    @property
    def person_key(self) -> Optional[str]:
        return self.person.key if self.person else None

    def describe(self) -> str:
        """One-line, log-friendly summary."""
        if self.person is None:
            return (
                f"{self.channel}:{self.external_id} is unknown — admitting "
                f"creates a new person row"
            )
        return (
            f"{self.channel}:{self.external_id} resolves to {self.person.key} "
            f"— no new row"
        )


def resolve_admission(
    network: Any, bot_id: str, channel: Any, external_id: Any,
) -> AdmissionResolution:
    """Consult the lookup for an admission of ``(channel, external_id)`` on ``bot_id``.

    Scoped to the rows this bot's admission decision can see — the pod-admin
    bag and this bot's own primary — because that is the scope every existing
    admission trigger already uses (``roster_overlay.resolve_role``,
    ``pairing.auto_approver._resolve_auto_approve_reason``). A different bot's
    primary is not evidence about *this* bot's caller.

    Read-only: this decides nothing and writes nothing. The caller keeps its
    own admission policy; what it gains is D1's answer to "is this a person we
    already have a row for?" so it does not treat a known person's second
    platform as a stranger.
    """
    cleaned_channel = _clean_channel(channel) or ""
    needle = str(external_id).strip() if external_id is not None else ""
    person = (
        resolve_person(network, cleaned_channel, needle, bot_id=bot_id)
        if cleaned_channel and needle else None
    )
    return AdmissionResolution(
        channel=cleaned_channel, external_id=needle, person=person)


def _clean_channel(channel: Any) -> Optional[str]:
    """Canonical channel key, or None — same rule as ``external_ids``."""
    if not isinstance(channel, str):
        return None
    cleaned = channel.strip().lower()
    return cleaned or None
