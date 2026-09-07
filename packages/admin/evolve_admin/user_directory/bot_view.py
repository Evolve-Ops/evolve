"""Bot-facing projection of a ``Person`` — the sanitized read the bot is allowed to see.

Spec: ``internal/spec-user-directory-2026-06-22.md`` §5 (``directory_lookup`` + the per-turn
digest) and §10 invariants #1 (one read path) and #5 (directory ≠ private profile).

Both bot-facing consumers resolve through ``resolve_person`` / ``resolve_persons`` — THE
one read path (invariant #1) — and then project the resulting ``Person`` through THIS
module before it crosses to the bot:

  * :func:`person_to_bot_dict` — the full directory-owned view of one ``Person`` for the
    ``directory_lookup`` tool, **minus** the behavioral-profile link and the audit trail.
  * :func:`build_digest` — a size-bounded list of slim rows (display, handle, identity,
    primary email, role) for the per-turn roster digest.

Why a dedicated sanitizer rather than serializing ``Person.to_dict()`` on the wire: the
bot must NEVER receive the behavioral profile (invariant #5 — Phase 4 gates surfacing
that, and only to the *admin*) nor the per-field audit trail (which carries operator
login names and prior-value PII history). :func:`person_to_bot_dict` is the single
chokepoint that drops them, and it is an **allow-list** (it names the fields it emits)
so a future ``Person`` field can never silently leak to the bot — a new sensitive field
is omitted until someone deliberately adds it here.
"""

from __future__ import annotations

from typing import Any

from . import model


# The per-turn digest is injected into the LLM's system prompt every turn (TTL-cached),
# so it must stay small — a large roster cannot be allowed to blow the context budget.
# The caller (the route) passes the cap; this is the shared default the route uses.
DEFAULT_DIGEST_CAP = 25


def person_to_bot_dict(person: "model.Person") -> dict[str, Any]:
    """The bot-facing serialization of one ``Person`` — directory fields only.

    An **allow-list** projection (spec §10 invariant #5): it emits who-this-is /
    how-to-reach-them / what-they-may-do, and **deliberately omits**:

      * ``profile_ref`` — the link to the private behavioral profile. The profile itself
        is never in a ``Person``; even the *pointer* stays server-side. Surfacing profile
        content is Phase 4, gated separately and only to the operator.
      * ``audit`` — the per-field provenance trail. It records operator login names and
        prior values (themselves PII — e.g. a corrected email), which the bot has no need
        for and must not see.

    Because it is an allow-list, adding a new field to ``Person`` does NOT auto-expose it
    to the bot; a new field must be added here on purpose.
    """
    return {
        "person_id": person.person_id,
        "names": dict(person.names),
        "identities": [i.to_dict() for i in person.identities],
        "emails": [e.to_dict() for e in person.emails],
        "contact": dict(person.contact),
        "membership": person.membership.to_dict() if person.membership else None,
        # OMITTED on purpose (invariant #5 / privacy): profile_ref, audit. See docstring.
    }


def is_blocked(person: "model.Person") -> bool:
    """True for a Person whose membership role is ``blocked``.

    A blocked identity is normally absent already (the block flow revokes it from the
    channel allowFrom, so ``resolve_roster`` doesn't surface it), but the block index and
    the allowFrom revoke are TWO separate writes (``roster_overlay`` — the block is sticky
    *because* the two can be out of sync), so during that window ``resolve_roster`` can
    still return a record with ``role="blocked"``. Both bot-facing read paths filter it
    explicitly as defense in depth — a blocked user must never be advertised to the bot,
    via the digest **or** the lookup tool (the two must agree). Shared helper so there is
    one blocked-filter, not two divergent ones.
    """
    return person.membership is not None and person.membership.role == "blocked"


def _digest_handle(person: "model.Person") -> str:
    """The first ``@handle`` across this Person's identities, normalized to ``@`` prefix."""
    for ident in person.identities:
        if ident.handle:
            h = ident.handle.strip()
            if h:
                return h if h.startswith("@") else f"@{h}"
    return ""


def _digest_identity(person: "model.Person") -> str:
    """The Person's base identity as ``"<platform>:<id>"`` — the stable id the bot should
    use instead of hand-typing one (the whole point of the digest). The resolver puts the
    channel-captured base identity first, so ``identities[0]`` is the right anchor."""
    if not person.identities:
        return ""
    base = person.identities[0]
    return f"{base.platform}:{base.id}"


def _digest_row(person: "model.Person") -> dict[str, Any]:
    """One slim digest row — the compact "one line per Person" payload (spec §5 piece 3).

    Carries exactly the fields the line renders: display name, ``@handle``, the stable
    identity, the primary email, and the role (or ``"contact"`` for a Person with no
    membership). No profile, no audit, no secondary emails — the digest is an at-a-glance
    index; ``directory_lookup`` is the path to the full record.
    """
    membership = person.membership
    role = membership.role if membership is not None else "contact"
    pe = person.primary_email
    return {
        "person_id": person.person_id,
        "display": str(person.names.get("display") or ""),
        "handle": _digest_handle(person),
        "identity": _digest_identity(person),
        "primary_email": pe.addr if pe is not None else "",
        "role": role,
    }


def build_digest(
    persons: "list[model.Person]", *, cap: int = DEFAULT_DIGEST_CAP,
) -> dict[str, Any]:
    """Build the size-bounded per-turn digest from resolved ``Person``\\ s.

    Filters out blocked Persons, sorts admitted users before contacts (then by display
    name, then ``person_id`` for a total deterministic order — the SAME ordering the
    admin Users-page GET uses, so the bot's digest and the operator's list agree), caps
    to ``cap`` rows, and projects each to a slim :func:`_digest_row`.

    Returns ``{persons: [row, ...], total, shown, cap}`` where ``total`` is the count
    *before* the cap (so the renderer can show an "+N more" overflow line) and ``shown``
    is ``len(persons)``.
    """
    visible = [p for p in persons if not is_blocked(p)]
    visible.sort(key=lambda p: (
        p.membership is None,  # admitted users first, then contacts
        str(p.names.get("display") or "").casefold(),
        p.person_id,
    ))
    total = len(visible)
    shown = visible[: max(0, cap)]
    return {
        "persons": [_digest_row(p) for p in shown],
        "total": total,
        "shown": len(shown),
        "cap": cap,
    }
