"""Admin per-bot directory routes — emails + contact attributes (Phase 2).

Spec: ``internal/spec-user-directory-2026-06-22.md`` §6 (the Person card) + §10
invariants. These routes are the **operator's write path** into the directory
store (``{shared_dir}/directory/{bot_id}.json``) for the directory-owned fields
of a :class:`evolve_admin.user_directory.model.Person`: contact ``emails``
(add / edit / delete / set-primary / verify) and the open ``contact`` map.

Hard architectural boundaries (invariant #2 — "the bot may assert identity, never
authority"):

  * **Every write goes through Phase 1's** ``user_directory.storage.upsert_entry``,
    whose keyword-only signature can touch only :data:`...storage.WRITABLE_FIELDS`
    (``emails`` / ``contact`` / ``identities`` / ``names`` / ``profile_ref``).
    ``membership`` / role / admission are **unreachable by construction** from this
    module — there is no code path here that loads, writes, or even imports the
    roster overlay's mutators. Admitting a contact runs the *existing* fail-closed
    admission flow (``routes_bot_users`` ``/users/approve``), never these routes.
  * **Operator edits stamp ``provenance:"operator-verified"``** (the strongest;
    outranks ``bot-asserted`` — invariant #3). The provenance is hardcoded in this
    module, never taken from the request body. The bot's own write path is the
    Phase-3 ``directory_upsert`` plugin tool, which stamps ``bot-asserted`` — a
    *different* entry point, not these admin routes.
  * **Forgery guard.** When a gateway-attested requester is present
    (``X-Requester-Identity``), the write is gated on the ``bot.roster.mutate``
    capability — the SAME gate ``routes_bot_users``' identity-patch/block routes
    use — so a non-privileged caller relayed through a bot gateway cannot reach
    these routes to forge ``operator-verified``. With no header (the pure admin-UI
    path) the UI is the trusted client and the gate is a no-op, exactly as for the
    page's other mutations.

Reads (the GET) go through ``resolve_persons`` — the one read path (invariant
#1) — so admitted users *and* contacts come from the same join the bot will use.
The write helpers read the directory store's *own* current ``emails`` to compute
the field delta (a store-internal read-modify-write, distinct from a display
consumer); the full recomputed list is handed to ``upsert_entry``, which
re-validates the single-primary invariant before writing.

Storage discipline: the directory store lives under ``{shared_dir}`` (evolve-
owned ACL), so ``upsert_entry`` writes it with plain atomic temp-file+rename —
no ``/tmp``-staging + sudo (CLAUDE.md). On-disk PII protection (0600 self-heal
after deploy's ``a+rX`` widen) is enforced pod-wide by
``secret_config_perms.check_shared_directory_modes`` /
``tighten_shared_directory_tree`` (the Phase-2 prerequisite).

Endpoints (all under ``/api/admin/bots/<bot_id>/directory``):

  GET  /                              → ``{bot_id, persons: [Person.to_dict(), ...]}``
  POST /<platform>/<stable_id>/email  → body ``{op, ...}`` (add|update|delete|set_primary|set_verified)
  POST /<platform>/<stable_id>/contact→ body ``{contact: {...}}``  (replace the open contact map)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from ..config import load_network
from ..user_directory import model
from ..user_directory import resolver as udr
from ..user_directory import storage as uds
# Reuse the page's battle-tested requester-attribution + capability gate so the
# directory write routes and the rest of the Users page share ONE auth surface
# (no second, divergent implementation of the forgery guard). ``_shared_dir_from_net``
# is reused for the same reason platform-path-lint wants — one home for the
# sharedDir fallback literal, not a second copy. All three are no-ops / pure
# helpers on the trusted admin-UI path (no X-Requester-Identity header).
from .routes_bot_users import (
    _check_capability,
    _requester_label,
    _shared_dir_from_net,
)


log = logging.getLogger(__name__)


# Directory edits are an operator action; the directory store records this as the
# strongest provenance (spec §2: operator entered/confirmed in the UI). Hardcoded
# — never read from the request — so a write through this module can only ever be
# ``operator-verified`` (it is the operator's surface).
_OPERATOR_PROVENANCE = "operator-verified"

# Same gate as the page's identity-patch / block / unblock routes. A no-op for
# the header-less UI path; a real check for a gateway-attested requester.
_DIRECTORY_WRITE_CAPABILITY = "bot.roster.mutate"

_EMAIL_OPS: tuple[str, ...] = (
    "add", "update", "delete", "set_primary", "set_verified")


def register_routes(app: Flask, network_path: Path) -> None:
    """Register the per-bot directory endpoints on ``app``."""

    @app.get("/api/admin/bots/<bot_id>/directory")
    def bot_directory_list(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        try:
            persons = udr.resolve_persons(net, bot_id)
        except Exception as exc:  # noqa: BLE001 — never 500 the directory list
            log.warning("resolve_persons for %s failed: %s", bot_id, exc)
            return jsonify({"bot_id": bot_id, "persons": []})
        # Stable order: admitted users first, then contacts; alphabetical within
        # each group by display name (falling back to the person_id so the order
        # is total and deterministic for the UI + tests).
        persons.sort(key=lambda p: (
            p.membership is None,
            str(p.names.get("display") or "").casefold(),
            p.person_id,
        ))
        return jsonify({
            "bot_id": bot_id,
            "persons": [p.to_dict() for p in persons],
        })

    @app.post("/api/admin/bots/<bot_id>/directory/<platform>/<stable_id>/email")
    def bot_directory_email(bot_id: str, platform: str,
                            stable_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        ok, reason = _check_capability(net, bot_id, _DIRECTORY_WRITE_CAPABILITY)
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        body = request.get_json(silent=True) or {}
        op = (body.get("op") or "").strip()
        if op not in _EMAIL_OPS:
            return jsonify({
                "error": f"invalid 'op' {op!r}; expected one of {list(_EMAIL_OPS)}",
            }), 400
        shared = _shared_dir(net)
        current = _current_emails(shared, bot_id, platform, stable_id)
        try:
            new_emails = apply_email_op(current, op, body)
        except _EmailOpError as e:
            return jsonify({"error": str(e)}), 400
        return _write_and_render(
            net, shared, bot_id, platform, stable_id, emails=new_emails)

    @app.post("/api/admin/bots/<bot_id>/directory/<platform>/<stable_id>/contact")
    def bot_directory_contact(bot_id: str, platform: str,
                              stable_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        ok, reason = _check_capability(net, bot_id, _DIRECTORY_WRITE_CAPABILITY)
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        body = request.get_json(silent=True) or {}
        contact = body.get("contact")
        if not isinstance(contact, dict):
            return jsonify({"error": "body must carry a 'contact' object"}), 400
        # Coerce to a flat str→(str|None) open map: drop empty keys, stringify
        # scalar values, reject nested structures (the UI edits a flat attribute
        # table; nested blobs would silently round-trip as junk).
        try:
            clean = _clean_contact(contact)
        except _EmailOpError as e:
            return jsonify({"error": str(e)}), 400
        shared = _shared_dir(net)
        return _write_and_render(
            net, shared, bot_id, platform, stable_id, contact=clean)


# ── Write orchestration ───────────────────────────────────────────────────


def _write_and_render(net: dict, shared: Path, bot_id: str, platform: str,
                      stable_id: str, **fields: Any) -> Any:
    """Apply a directory-owned field write via ``upsert_entry`` and return the
    re-resolved Person.

    ``fields`` is forwarded verbatim to ``upsert_entry`` and may carry ONLY
    ``emails`` / ``contact`` here (the two this module edits) — ``membership`` is
    not a parameter of ``upsert_entry`` at all, so it is structurally
    unreachable. Provenance is the hardcoded operator value; ``by`` is the
    resolved requester label so the audit trail names who edited the field.
    """
    by = _requester_label()
    try:
        uds.upsert_entry(
            shared, bot_id, platform, stable_id,
            by=by, provenance=_OPERATOR_PROVENANCE, **fields)
    except ValueError as e:
        # e.g. the single-primary invariant tripped in storage's re-validation.
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"directory write failed: {e}"}), 500
    person = udr.resolve_person(net, bot_id, (platform, stable_id))
    return jsonify({
        "ok": True,
        "person": person.to_dict() if person is not None else None,
    })


def _current_emails(shared: Path, bot_id: str, platform: str,
                    stable_id: str) -> list[dict]:
    """The identity's current ``emails`` as plain dicts (store-internal RMW read).

    Reads the directory store's own row to compute the field delta — distinct
    from a *display* read (which goes through ``resolve_person`` per invariant
    #1). Returns ``[]`` when the identity has no entry yet (the operator is adding
    the first email), so ``upsert_entry`` mints the entry on write.
    """
    directory = uds.load_directory(shared, bot_id)
    entry = uds.get_entry(directory, platform, stable_id)
    return [e.to_dict() for e in model.coerce_emails((entry or {}).get("emails"))]


# ── Pure email-op transform (unit-testable; no I/O) ───────────────────────


class _EmailOpError(Exception):
    """A bad email/contact request — surfaced as a 400."""


def apply_email_op(current: list[dict], op: str, body: dict) -> list[dict]:
    """Apply one email operation to ``current`` and return the NEW emails list.

    Pure (no I/O) so the single-primary invariant + each op is unit-testable in
    isolation. Returns a list of ``{addr, rank, verified}`` dicts (provenance is
    intentionally omitted — ``upsert_entry`` stamps every row with the call's
    ``operator-verified``, the per-field provenance model of the Phase-1 store).

    Single-primary is enforced atomically here: any op that promotes an address
    to ``primary`` demotes all others to ``secondary`` in the same returned list,
    so the result always carries at most one primary (``upsert_entry``
    re-validates as defense in depth).

    Raises :class:`_EmailOpError` (→ 400) on a malformed address, a duplicate
    add, or an op whose target address isn't present.
    """
    rows = [dict(e) for e in current]  # copy — never mutate the caller's list

    if op == "add":
        addr = _valid_addr(body.get("addr"))
        if _find(rows, addr) is not None:
            raise _EmailOpError(f"email {addr!r} is already on this person")
        rank = _valid_rank(body.get("rank") or "secondary")
        rows.append({"addr": addr, "rank": rank, "verified": bool(body.get("verified"))})
        if rank == "primary":
            _demote_others(rows, keep=addr)
        return rows

    if op == "update":
        addr = _valid_addr(body.get("addr"))
        idx = _find(rows, addr)
        if idx is None:
            raise _EmailOpError(f"no email {addr!r} on this person to update")
        row = rows[idx]
        if body.get("new_addr") is not None:
            new_addr = _valid_addr(body.get("new_addr"))
            if not _same_addr(new_addr, addr) and _find(rows, new_addr) is not None:
                raise _EmailOpError(f"email {new_addr!r} is already on this person")
            row["addr"] = new_addr
        if body.get("rank") is not None:
            row["rank"] = _valid_rank(body.get("rank"))
        if body.get("verified") is not None:
            row["verified"] = bool(body.get("verified"))
        if row["rank"] == "primary":
            _demote_others(rows, keep=row["addr"])
        return rows

    if op == "delete":
        addr = _valid_addr(body.get("addr"))
        idx = _find(rows, addr)
        if idx is None:
            raise _EmailOpError(f"no email {addr!r} on this person to delete")
        del rows[idx]
        return rows

    if op == "set_primary":
        addr = _valid_addr(body.get("addr"))
        idx = _find(rows, addr)
        if idx is None:
            raise _EmailOpError(f"no email {addr!r} on this person to set primary")
        for i, r in enumerate(rows):
            r["rank"] = "primary" if i == idx else "secondary"
        return rows

    if op == "set_verified":
        addr = _valid_addr(body.get("addr"))
        idx = _find(rows, addr)
        if idx is None:
            raise _EmailOpError(f"no email {addr!r} on this person to verify")
        rows[idx]["verified"] = bool(body.get("verified"))
        return rows

    raise _EmailOpError(f"unknown email op {op!r}")  # unreachable: route pre-validates


def _find(rows: list[dict], addr: str) -> "int | None":
    for i, r in enumerate(rows):
        if _same_addr(str(r.get("addr") or ""), addr):
            return i
    return None


def _same_addr(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _demote_others(rows: list[dict], *, keep: str) -> None:
    for r in rows:
        if not _same_addr(str(r.get("addr") or ""), keep):
            r["rank"] = "secondary"


def _valid_addr(raw: Any) -> str:
    addr = str(raw or "").strip()
    # Deliberately light validation: a non-empty token with one ``@`` and a dot
    # in the domain. The store accepts any string; this just blocks the obvious
    # fat-finger / empty submit. Full RFC-5322 validation is not the operator's
    # job here (they may be recording a known-good address the bot surfaced).
    if not addr or addr.count("@") != 1:
        raise _EmailOpError(f"{raw!r} is not a valid email address")
    local, _, domain = addr.partition("@")
    if not local or "." not in domain:
        raise _EmailOpError(f"{raw!r} is not a valid email address")
    return addr


def _valid_rank(raw: Any) -> str:
    rank = str(raw or "").strip()
    if rank not in model.EMAIL_RANKS:
        raise _EmailOpError(
            f"rank must be one of {list(model.EMAIL_RANKS)}, got {raw!r}")
    return rank


def _clean_contact(contact: dict) -> dict[str, Any]:
    """Normalize the open ``contact`` map: non-empty string keys → scalar values.

    Drops blank keys and blank values (so deleting an attribute = clearing its
    field in the UI). Rejects nested objects/lists — the contact table is a flat
    attribute map; a nested blob would round-trip as opaque junk the UI can't
    edit. Scalars are stringified (``phone``/``org``/``notes`` are all text).
    """
    out: dict[str, Any] = {}
    for k, v in contact.items():
        key = str(k or "").strip()
        if not key:
            continue
        if isinstance(v, (dict, list)):
            raise _EmailOpError(
                f"contact attribute {key!r} must be a scalar, not a nested object")
        if v is None:
            continue
        val = str(v).strip()
        if val:
            out[key] = val
    return out


# ── Small shared helpers ──────────────────────────────────────────────────


def _bot_exists(network: dict, bot_id: str) -> bool:
    return bot_id in (network.get("bots") or {})


def _shared_dir(network: dict) -> Path:
    # One home for the sharedDir fallback literal — reuse the page's helper
    # rather than re-hardcoding "/Users/Shared/evolve" (platform-path-lint).
    return _shared_dir_from_net(network)
