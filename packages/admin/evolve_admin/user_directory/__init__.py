"""The per-bot **user directory** — the unified ``Person`` model.

Spec: ``internal/spec-user-directory-2026-06-22.md`` (realizes the ``users`` aspect
charter roadmap R2). A ``Person`` unifies two populations the pod used to track in
separate, incompatible places:

  * **admitted users** — a ``Person`` *with* ``membership`` (a view over the existing
    roster: allowlist + overlay + name + activity, joined by ``roster_resolver``), and
  * **contacts** — the same record *without* ``membership`` (an address book the bot
    acts *toward*; the address-book case in spec §0).

This package is the Phase-1 *read + store scaffolding* (spec §9 row 1):

  * ``model`` — the ``Person`` dataclass and its parts, with the load-bearing
    invariants (single ``rank:"primary"`` email; per-field ``provenance``).
  * ``storage`` — the directory store at ``{shared_dir}/directory/{bot_id}.json`` holding
    only the *directory-owned* additive fields (``person_id``, ``emails``, ``contact``,
    operator/bot-added ``identities``, ``profile_ref``), keyed by the existing stable
    identity. Mirrors ``roster_overlay`` (atomic temp-file + rename, audit log under
    ``directory/log/``).
  * ``resolver`` — ``resolve_person`` / ``resolve_persons``: the **only** read path for
    consumers (Users-page GET, and — Phase 3 — bot injection + the bot lookup tool).
    Joins the roster projection with the directory store into one ``Person`` so admin
    and bot cannot diverge (spec invariant #1 / charter invariant #1).

Phase 1 deliberately ships **no** bot write surface and **no** membership mutation —
those are Phases 3 and the existing fail-closed admission path respectively (spec §10
invariant #2). The store's ``upsert``/``mint`` helpers exist for tests + later phases;
nothing in the UI or the bot calls them yet.
"""

from __future__ import annotations
