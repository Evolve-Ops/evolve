"""The ``Person`` data model — the unified per-bot user/contact record.

Spec: ``docs/spec-user-directory-2026-06-22.md`` §2. A ``Person`` is per-bot, keyed by a
stable Evolve-minted ``person_id`` that survives handle/email churn. It unifies an
**admitted user** (a ``Person`` *with* ``membership``) and a **contact** (the same
record *without* it) — one model, no second store (invariant #4).

The load-bearing invariants this module enforces (spec §10 #2/#3, and the task's
"Enforce" clause):

  * **Exactly one primary email.** ``emails`` carries at most one ``rank:"primary"``;
    the rest are ``secondary``. ``Person`` raises ``ValueError`` if two primaries are
    present — re-ranking is an atomic flip, not an accumulation.
  * **Provenance is a closed set and never lost.** Every directory-owned field carries a
    ``provenance`` in :data:`PROVENANCE` (``channel-captured`` < ``bot-asserted`` <
    ``operator-verified``); ``operator-verified`` outranks ``bot-asserted`` on conflict
    and display (:func:`provenance_outranks`). ``Email``/``Identity`` reject an unknown
    provenance at construction.

The dataclasses are plain value objects: ``to_dict`` serializes to the on-disk / wire
JSON shape (spec §2), ``from_dict`` parses it back. The resolver builds ``Person``\\ s
from the roster join + the directory store; the store validates on write so reads stay
cheap — but ``from_dict`` is *lenient* on individual rows (skips/repairs rather than
raising) so a corrupt store degrades to a partial directory, never a 500 on the page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger(__name__)


# ── Closed vocabularies (spec §2) ────────────────────────────────────────


PROVENANCE: tuple[str, ...] = (
    "channel-captured",   # e.g. Slack users.info / pairing meta — the platform told us
    "bot-asserted",       # the bot's directory_upsert tool write (Phase 3)
    "operator-verified",  # the operator entered/confirmed it in the admin UI (Phase 2)
)
"""Allowed ``provenance`` values, ordered weakest → strongest. The order is the
conflict/display precedence: ``operator-verified`` wins over ``bot-asserted`` wins over
``channel-captured`` (spec §2 "operator-verified outranks bot-asserted")."""

_PROVENANCE_RANK: dict[str, int] = {p: i for i, p in enumerate(PROVENANCE)}

EMAIL_RANKS: tuple[str, ...] = ("primary", "secondary")
"""An email is exactly one of these. At most one ``primary`` per Person."""


def provenance_outranks(a: "str | None", b: "str | None") -> bool:
    """True iff provenance ``a`` strictly outranks ``b`` (later in :data:`PROVENANCE`).

    Unknown/None provenance ranks below every known value, so a well-formed field always
    wins over a malformed one. Used by conflict resolution + display badging.
    """
    return _PROVENANCE_RANK.get(a or "", -1) > _PROVENANCE_RANK.get(b or "", -1)


# ── Parts of a Person ────────────────────────────────────────────────────


@dataclass
class Identity:
    """One messaging identity — "slack ID, telegram ID, names" (spec §2).

    ``source`` is the provenance of *this identity row* (how Evolve learned the bot can
    reach the person on this platform), drawn from :data:`PROVENANCE`. The base identity
    that comes from the roster allowlist is ``channel-captured``; operator/bot-added
    identities carry their own provenance.
    """

    platform: str
    id: str
    handle: "str | None" = None
    source: str = "channel-captured"

    def __post_init__(self) -> None:
        if self.source not in PROVENANCE:
            raise ValueError(
                f"Identity.source must be one of {PROVENANCE}, got {self.source!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "platform": self.platform, "id": self.id, "source": self.source}
        if self.handle:
            d["handle"] = self.handle
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Identity":
        return cls(
            platform=str(raw.get("platform") or ""),
            id=str(raw.get("id") or ""),
            handle=(raw.get("handle") or None),
            source=str(raw.get("source") or "channel-captured"),
        )


@dataclass
class Email:
    """A person's contact email — ``{addr, rank, provenance, verified}`` (spec §2).

    Distinct from ``correspondence.email_address`` (the bot's *send-as* From header,
    spec-multi-user-alias): this is *how to reach this person*, not *who mail is from*.
    """

    addr: str
    rank: str           # "primary" | "secondary"
    provenance: str     # channel-captured | bot-asserted | operator-verified
    verified: bool = False

    def __post_init__(self) -> None:
        if self.rank not in EMAIL_RANKS:
            raise ValueError(
                f"Email.rank must be one of {EMAIL_RANKS}, got {self.rank!r}")
        if self.provenance not in PROVENANCE:
            raise ValueError(
                f"Email.provenance must be one of {PROVENANCE}, got {self.provenance!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "addr": self.addr,
            "rank": self.rank,
            "provenance": self.provenance,
            "verified": bool(self.verified),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Email":
        return cls(
            addr=str(raw.get("addr") or ""),
            rank=str(raw.get("rank") or "secondary"),
            provenance=str(raw.get("provenance") or "channel-captured"),
            verified=bool(raw.get("verified")),
        )


@dataclass
class Membership:
    """The ``membership`` view — present ⇔ admitted user of *this* bot (spec §2).

    A *view over the existing roster* (overlay + allowlists), not a re-fork of roles:
    ``role``/``capabilities`` come straight from ``roster_resolver`` /
    ``capabilities``. ``directory_upsert`` (Phase 3) can never write this — admission +
    roles mutate only through the existing fail-closed operator-gated path (invariant
    #2).
    """

    admitted: bool
    role: str
    capabilities: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": bool(self.admitted),
            "role": self.role,
            "capabilities": list(self.capabilities),
            "channels": list(self.channels),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Membership":
        return cls(
            admitted=bool(raw.get("admitted")),
            role=str(raw.get("role") or "participant"),
            capabilities=[str(c) for c in (raw.get("capabilities") or [])],
            channels=[str(c) for c in (raw.get("channels") or [])],
        )


@dataclass
class AuditEntry:
    """One per-field provenance-trail entry — ``{field, from, to, by, source, at}``.

    ``source`` is the provenance the write asserted; ``at`` is an ISO timestamp; ``by``
    is the actor (operator login / bot id / ``"system"``). ``from_`` carries the prior
    value and serializes to the JSON key ``"from"`` (a Python reserved word).
    """

    field: str
    from_: Any
    to: Any
    by: str
    source: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "from": self.from_,
            "to": self.to,
            "by": self.by,
            "source": self.source,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            field=str(raw.get("field") or ""),
            from_=raw.get("from"),
            to=raw.get("to"),
            by=str(raw.get("by") or ""),
            source=str(raw.get("source") or ""),
            at=str(raw.get("at") or ""),
        )


# ── The Person ───────────────────────────────────────────────────────────


@dataclass
class Person:
    """The unified per-bot user/contact record (spec §2).

    ``membership is None`` ⇒ a pure contact (the bot-invitee / address-book case);
    present ⇒ an admitted user. ``person_id`` is stable across handle/email churn (keyed
    on the
    stable identity, not on any mutable field). Raises ``ValueError`` if ``emails``
    contains more than one ``rank:"primary"`` — the single-primary invariant.
    """

    person_id: str
    names: dict[str, Any] = field(default_factory=dict)
    identities: list[Identity] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    contact: dict[str, Any] = field(default_factory=dict)
    membership: "Membership | None" = None
    profile_ref: "str | None" = None
    audit: list[AuditEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        primaries = [e for e in self.emails if e.rank == "primary"]
        if len(primaries) > 1:
            raise ValueError(
                f"Person {self.person_id!r} has {len(primaries)} primary emails; "
                f"at most one rank:'primary' is allowed")

    @property
    def primary_email(self) -> "Email | None":
        """The single primary email, or ``None`` when the person has no primary."""
        for e in self.emails:
            if e.rank == "primary":
                return e
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "names": dict(self.names),
            "identities": [i.to_dict() for i in self.identities],
            "emails": [e.to_dict() for e in self.emails],
            "contact": dict(self.contact),
            "membership": self.membership.to_dict() if self.membership else None,
            "profile_ref": self.profile_ref,
            "audit": [a.to_dict() for a in self.audit],
        }


# ── Lenient coercion (read side — never raises) ──────────────────────────


def coerce_emails(raw_list: Any) -> list[Email]:
    """Parse a stored ``emails`` list into validated ``Email``\\ s, **never raising**.

    Rows with an unknown rank/provenance are dropped (logged), so a corrupt directory
    store degrades to a partial email list rather than 500-ing the page. The
    single-primary invariant is *repaired* rather than enforced here: if more than one
    ``primary`` survives, the strongest-provenance one keeps ``primary`` and the rest are
    demoted to ``secondary`` (deterministic, stable order). The write path
    (``storage.upsert_entry``) enforces single-primary strictly; this is the
    belt-and-suspenders read guard so :class:`Person` construction can't blow up.
    """
    out: list[Email] = []
    for raw in (raw_list or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Email.from_dict(raw))
        except ValueError as exc:
            log.warning("directory: dropping malformed email %r: %s", raw, exc)
    # Repair >1 primary: keep the strongest-provenance primary, demote the others.
    primaries = [e for e in out if e.rank == "primary"]
    if len(primaries) > 1:
        keep = primaries[0]
        for e in primaries[1:]:
            if provenance_outranks(e.provenance, keep.provenance):
                keep = e
        for e in primaries:
            if e is not keep:
                e.rank = "secondary"
        log.warning(
            "directory: repaired %d primary emails to a single primary (%s)",
            len(primaries), keep.addr)
    return out


def coerce_identities(raw_list: Any) -> list[Identity]:
    """Parse a stored ``identities`` list, dropping malformed rows (never raises)."""
    out: list[Identity] = []
    for raw in (raw_list or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Identity.from_dict(raw))
        except ValueError as exc:
            log.warning("directory: dropping malformed identity %r: %s", raw, exc)
    return out


def coerce_audit(raw_list: Any) -> list[AuditEntry]:
    """Parse a stored ``audit`` list (never raises)."""
    out: list[AuditEntry] = []
    for raw in (raw_list or []):
        if isinstance(raw, dict):
            out.append(AuditEntry.from_dict(raw))
    return out
