"""The directory store — a ``people`` table on the records layer (was ``{shared_dir}/directory/{bot_id}.json``).

**Since the records layer (brief ``app-store-and-ledger-verbs``):** the rows
live in ``{shared_dir}/apps/evolve.directory/{bot_id}/data.sqlite`` (a ``people``
table plus a ``contacts_seen`` ledger — :mod:`.records`), written only by the
daemon. The JSON below is read ONCE, when the store is created, and left in
place; a process that may not create the store (root, an operator shell)
keeps reading the JSON exactly as before. Every function here keeps its
signature and its return shape — the change is the backing, not the API. The
per-mutation audit JSONL under ``directory/log/`` is unchanged.

Spec: ``internal/spec-user-directory-2026-06-22.md`` §4. This is the **sibling store** to
``roster_overlay``: it holds *only* the directory-owned, additive fields of a
``Person`` — ``person_id``, ``emails``, ``contact``, operator/bot-added ``identities``,
``names`` overrides, ``profile_ref`` — keyed by the **same stable identity key** the
overlay uses (``"<platform>:<stable_id>"`` via :func:`roster_overlay.identity_key`). The
roster (allowlist + overlay + name + activity, joined by ``roster_resolver``) keeps
owning ``membership`` + base identity; the two are joined into one ``Person`` by
:mod:`evolve_admin.user_directory.resolver` — which is the **only** module that reads
this file (invariant #1 / the R3 forward-compat seam in spec §4).

Storage discipline mirrors ``roster_overlay`` exactly:

  * lives under ``{shared_dir}`` (evolve-owned ACL) → **plain atomic writes**, no
    ``/tmp``-staging + sudo (CLAUDE.md);
  * writes are temp-file + ``os.replace``;
  * every mutation appends one JSONL line to ``{shared_dir}/directory/log/{date}.jsonl``.

It diverges from the overlay in one deliberate way: the overlay is forced **0644**
because the bot's TS ``roleResolver`` reads it directly per turn; the directory store is
**not** read by the bot directly (Phase 3's bot path goes through the server-side
resolver) and it holds contact **PII** (emails), so we keep it **0600** rather than
world-readable. Enforced PII-at-rest hardening (encryption / key custody) is roadmap R3
(spec §8) and out of scope here; this is the conservative local default.

Phase 1 exposes ``load`` for the resolver, and the low-level ``mint_person_id`` /
``upsert_entry`` write helpers **for tests and later phases only** — nothing in the UI
or the bot calls the write path yet (spec §10 invariant #2).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso as _now_iso

from .. import roster_overlay as ro
from . import model
from . import records as _records


log = logging.getLogger(__name__)


CURRENT_VERSION = 1

# Directory-owned fields a write may touch. Deliberately excludes ``membership`` /
# roles / admission — those mutate only through the existing fail-closed operator-gated
# path (spec §10 invariant #2). ``upsert_entry`` has a fixed keyword-only signature with
# no generic field path, so ``membership``/role/admission are unreachable by construction
# (not by a runtime check) — keep it that way: never add a ``**kwargs`` / generic setter.
WRITABLE_FIELDS: tuple[str, ...] = (
    "emails", "contact", "identities", "names", "profile_ref")


# ── Path / load / save (mirrors roster_overlay) ──────────────────────────


def directory_path(shared_dir: Path, bot_id: str) -> Path:
    """Per-bot directory store path."""
    return Path(shared_dir) / "directory" / f"{bot_id}.json"


def _empty_directory(bot_id: str) -> dict[str, Any]:
    return {"bot_id": bot_id, "version": CURRENT_VERSION, "persons": {}}


def load_directory(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """Read the directory store. Returns the empty shape when absent or unreadable so
    callers treat "no directory yet" as the default state without branching (same
    contract as ``roster_overlay.load_overlay``).

    Reads the records store when it exists (creating it and importing the JSON
    once, when this process is the daemon user and a JSON is there to import);
    otherwise the legacy JSON, unchanged."""
    try:
        if _records.ensure_migrated(shared_dir, bot_id, create=False):
            return {"bot_id": bot_id, "version": CURRENT_VERSION,
                    "persons": _records.load_persons(shared_dir, bot_id)}
    except Exception as e:  # noqa: BLE001 — same contract as an unreadable JSON
        log.warning("directory store read for %s failed: %s — using empty", bot_id, e)
        return _empty_directory(bot_id)
    p = directory_path(shared_dir, bot_id)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return _empty_directory(bot_id)
    except (PermissionError, OSError) as e:
        log.warning("directory read for %s failed: %s — using empty", bot_id, e)
        return _empty_directory(bot_id)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("directory parse for %s failed: %s — using empty", bot_id, e)
        return _empty_directory(bot_id)
    if not isinstance(data, dict):
        return _empty_directory(bot_id)
    base = _empty_directory(bot_id)
    for k, v in base.items():
        data.setdefault(k, v)
    if not isinstance(data.get("persons"), dict):
        data["persons"] = {}
    data["bot_id"] = bot_id  # don't trust a stale on-disk bot_id
    return data


def save_directory(shared_dir: Path, bot_id: str,
                   directory: dict[str, Any]) -> None:
    """Write every Person in ``directory`` as a ``people`` row, in ONE transaction.

    Each row is replaced whole and gets one ``contacts_seen`` entry. A Person
    absent from ``directory`` is NOT deleted — no directory path has ever
    deleted a Person, and the ledger would still name them.

    Mode: the store is ``0600`` from creation (``app_store.ensure_store``) and
    its WAL inherits that mode. Deploy's ``chmod -R a+rX {shared_dir}`` would
    widen it; ``secret_config_perms.check_app_record_store_modes`` re-tightens
    it in ``ensure_pod_perms``, the same self-heal the JSON had
    (``check_shared_directory_modes``, which still covers the legacy file and
    the ``log/`` JSONL).
    """
    _records.ensure_migrated(shared_dir, bot_id, create=True)
    persons = {k: v for k, v in (directory.get("persons") or {}).items()
               if isinstance(v, dict)}
    if persons:
        _records.write_entries(shared_dir, bot_id, persons, by="system",
                               kind="captured", note="save_directory")


# ── Entry access ─────────────────────────────────────────────────────────


def get_entry(directory: dict, platform: str,
              stable_id: str) -> "dict | None":
    """The directory entry for an identity, or ``None`` if the store has no row.

    Keyed by ``roster_overlay.identity_key`` so the directory and the overlay agree on
    one identity-key form.
    """
    return (directory.get("persons") or {}).get(
        ro.identity_key(platform, stable_id))


# ── Stable person_id minting ─────────────────────────────────────────────


def person_id_for(bot_id: str, platform: str, stable_id: str) -> str:
    """Deterministic ``person_id`` for an identity — ``"pers_<16-hex>"``.

    Keyed on the **stable identity** (bot + platform + stable_id), so it survives
    handle/email churn (spec §2) and is stable across reloads **without** a write — the
    resolver can return a stable id for an identity that has never been persisted. It is
    a one-way digest (no PII recoverable from the id). ``mint_person_id`` persists this
    value; a future phase that merges two identities into one Person can override the
    stored ``person_id`` while this remains the default for an unmerged identity.
    """
    h = hashlib.sha256(
        f"{bot_id}\x00{platform}\x00{stable_id}".encode()).hexdigest()
    return f"pers_{h[:16]}"


def mint_person_id(shared_dir: Path, bot_id: str, platform: str,
                   stable_id: str, *, by: str = "system") -> str:
    """Ensure the identity has a persisted ``person_id``; return it (mint-once, reuse).

    If the entry already carries a ``person_id`` it is returned unchanged. Otherwise the
    deterministic :func:`person_id_for` value is recorded into a (possibly new) entry,
    persisted atomically, and an audit line is written. Idempotent: a second call returns
    the same id without rewriting.
    """
    key = ro.identity_key(platform, stable_id)
    minted: dict[str, str] = {}

    def _apply(directory: dict) -> "dict | None":
        persons = directory.setdefault("persons", {})
        entry = persons.get(key)
        if isinstance(entry, dict) and entry.get("person_id"):
            minted["pid"] = str(entry["person_id"])
            return None  # already minted: no write, no ledger entry
        pid = person_id_for(bot_id, platform, stable_id)
        entry = dict(entry or {})
        entry["person_id"] = pid
        # person_id is a system-minted surrogate, not an actor assertion — record it
        # with the neutral/weakest provenance regardless of who triggered the mint.
        entry.setdefault("audit", []).append(model.AuditEntry(
            field="person_id", from_=None, to=pid, by=by,
            source="channel-captured", at=_now_iso()).to_dict())
        persons[key] = entry
        minted["pid"] = pid
        return entry

    wrote = _write_one(shared_dir, bot_id, key, _apply, by=by, kind="minted",
                       note="mint_person_id")
    if wrote is not None:
        _audit_log_append(shared_dir, {
            "ts": _now_iso(), "action": "mint_person_id", "target_bot": bot_id,
            "by": by, "identity": key, "person_id": minted["pid"],
        })
    return minted["pid"]


# ── Provenance-preserving field merge (invariant #3) ─────────────────────


def _norm_addr(addr: Any) -> str:
    """Casefold+strip an email address for identity comparison (mirrors the resolver)."""
    return str(addr or "").strip().casefold()


def merge_emails_preserving_stronger(
    existing: "list[dict]", incoming: "list[dict]", incoming_provenance: str,
) -> "list[dict]":
    """Merge an ``incoming`` emails list over ``existing``, **keeping any existing row
    whose provenance strictly outranks the incoming write** (spec §10 invariant #3).

    ``upsert_entry`` stamps every incoming row with the *call's* provenance and otherwise
    **replaces** the whole field — which on its own would let a weaker write (the bot's
    ``bot-asserted``) silently destroy or downgrade a stronger one (the operator's
    ``operator-verified``). This merge is the guard: an existing row strictly stronger than
    the incoming provenance is **preserved verbatim** (address, rank, provenance, verified)
    and an incoming row that collides with it (same address, case-insensitively) is dropped
    — the stronger row wins. Then single-primary is re-enforced with the **strongest**
    provenance keeping ``primary`` (a protected operator primary beats an incoming bot
    primary; the bot cannot demote it).

    It is unconditional and **safe for every caller**: the operator path writes the
    strongest provenance (``operator-verified``), which nothing can outrank, so no row is
    ever protected and the behavior is the historical whole-field replace. Only a *weaker*
    write (bot / channel) ever triggers preservation — exactly where the invariant bites.

    Scope of protection (spec §10 invariant #3): ONLY strictly-stronger rows are preserved.
    A ``bot-asserted`` write therefore fully owns the weaker tiers — it may add, edit, or
    **drop by omission** a ``channel-captured`` row (or one of its own prior rows). That is
    intended: the spec promises protection for ``operator-verified`` data only, and the bot
    is the right authority over its own and platform-captured contact rows. Deleting an
    operator-verified row, by contrast, is impossible here (it is always re-emitted).
    """
    protected = [
        r for r in existing
        if isinstance(r, dict)
        and model.provenance_outranks(r.get("provenance"), incoming_provenance)
    ]
    protected_addrs = {_norm_addr(r.get("addr")) for r in protected}
    merged: "list[dict]" = list(protected)
    for r in incoming:
        if _norm_addr(r.get("addr")) in protected_addrs:
            continue  # a strictly-stronger existing row already owns this address
        merged.append(r)
    return _enforce_single_primary(merged)


def _enforce_single_primary(rows: "list[dict]") -> "list[dict]":
    """Collapse a merged email list to at most one ``rank:"primary"`` (strongest wins).

    Defense for the merge path: a *protected* (stronger) primary plus an *incoming*
    (weaker) primary would otherwise both survive. The strongest-provenance primary keeps
    ``primary``; the rest are demoted to ``secondary`` (deterministic, row order stable).
    Mirrors ``model.coerce_emails``'s read-side repair, on raw dicts. Does not mutate the
    caller's rows (copies the ones it demotes)."""
    primaries = [r for r in rows if isinstance(r, dict) and r.get("rank") == "primary"]
    if len(primaries) <= 1:
        return rows
    keep = primaries[0]
    for r in primaries[1:]:
        if model.provenance_outranks(r.get("provenance"), keep.get("provenance")):
            keep = r
    out: "list[dict]" = []
    for r in rows:
        if isinstance(r, dict) and r.get("rank") == "primary" and r is not keep:
            r = {**r, "rank": "secondary"}
        out.append(r)
    return out


def merge_identities_preserving_stronger(
    existing: "list[dict]", incoming: "list[dict]", incoming_provenance: str,
) -> "list[dict]":
    """Identities counterpart of :func:`merge_emails_preserving_stronger`.

    Keyed by ``(platform, id)``; an identity's provenance is its ``source`` field. An
    existing identity whose ``source`` strictly outranks the incoming write is preserved
    and a colliding incoming identity is dropped. Same unconditional/safe contract."""
    def _ik(r: dict) -> tuple[str, str]:
        return (str(r.get("platform") or ""), str(r.get("id") or ""))

    protected = [
        r for r in existing
        if isinstance(r, dict)
        and model.provenance_outranks(r.get("source"), incoming_provenance)
    ]
    protected_keys = {_ik(r) for r in protected}
    merged: "list[dict]" = list(protected)
    for r in incoming:
        if _ik(r) in protected_keys:
            continue
        merged.append(r)
    return merged


# ── Low-level upsert (tests + later phases — not UI/bot-exposed) ──────────


def upsert_entry(
    shared_dir: Path,
    bot_id: str,
    platform: str,
    stable_id: str,
    *,
    by: str,
    provenance: str,
    emails: "list[dict] | None" = None,
    contact: "dict | None" = None,
    identities: "list[dict] | None" = None,
    names: "dict | None" = None,
    profile_ref: "str | None" = None,
) -> dict[str, Any]:
    """Write directory-owned fields for an identity; return the new entry.

    The single low-level write helper. It is **scaffolding** for Phase 2/3 + tests —
    nothing in the UI or the bot calls it yet. Hard constraints (spec §10 invariant #2):

      * It can only touch :data:`WRITABLE_FIELDS`. ``membership`` / roles / admission are
        *not* writable here — those go through the existing fail-closed path.
      * ``provenance`` must be in :data:`model.PROVENANCE`; it stamps every email/identity
        row that doesn't carry its own provenance, and every per-field audit entry.

    Provenance is never lost (invariant #3): each changed field appends an
    :class:`model.AuditEntry` to the entry's ``audit`` list **and** a line to the daily
    audit log. ``emails`` are validated through :class:`model.Email` (single-primary +
    provenance enum) **before** anything is written, so a bad write raises ``ValueError``
    and leaves the store untouched.

    Provenance is also never *downgraded* (invariant #3, the clobber half): ``emails`` and
    ``identities`` are merged through :func:`merge_emails_preserving_stronger` /
    :func:`merge_identities_preserving_stronger`, which **keep any existing row whose
    provenance strictly outranks this write**. So a ``bot-asserted`` write can add and edit
    its own (and channel-captured) rows but can never destroy or downgrade an
    ``operator-verified`` one. For the operator path (``operator-verified`` — the strongest)
    nothing is ever protected, so the merge is the historical whole-field replace: the guard
    only bites a *weaker* writer, which is exactly where the invariant matters.
    """
    if provenance not in model.PROVENANCE:
        raise ValueError(
            f"upsert_entry: provenance must be one of {model.PROVENANCE}, "
            f"got {provenance!r}")

    # Validate + normalize emails up front (raises before any write). The upsert's
    # ``provenance`` STAMP WINS — it is spread *after* the caller's row dict, so a
    # caller-supplied row-level ``provenance``/``source`` is ignored. This closes the
    # forgery vector the spec forbids (invariant #2/#3): the bot/operator cannot assert a
    # stronger provenance than the write itself carries, and the stored row can never
    # diverge from the audit entry's recorded source.
    norm_emails: "list[dict] | None" = None
    if emails is not None:
        parsed = [
            model.Email.from_dict({**e, "provenance": provenance})
            for e in emails
        ]
        primaries = [e for e in parsed if e.rank == "primary"]
        if len(primaries) > 1:
            raise ValueError(
                f"upsert_entry: {len(primaries)} primary emails supplied; "
                f"at most one rank:'primary' is allowed")
        norm_emails = [e.to_dict() for e in parsed]

    norm_identities: "list[dict] | None" = None
    if identities is not None:
        norm_identities = [
            model.Identity.from_dict({**i, "source": provenance}).to_dict()
            for i in identities
        ]

    updates: dict[str, Any] = {}
    if norm_emails is not None:
        updates["emails"] = norm_emails
    if contact is not None:
        updates["contact"] = dict(contact)
    if norm_identities is not None:
        updates["identities"] = norm_identities
    if names is not None:
        updates["names"] = dict(names)
    if profile_ref is not None:
        updates["profile_ref"] = profile_ref

    def _apply(directory: dict) -> dict[str, Any]:
        persons = directory.setdefault("persons", {})
        key = ro.identity_key(platform, stable_id)
        entry = dict(persons.get(key) or {})
        # Mint person_id on first write so an upserted entry is always resolvable.
        if not entry.get("person_id"):
            entry["person_id"] = person_id_for(bot_id, platform, stable_id)
        audit = list(entry.get("audit") or [])
        now = _now_iso()
        for fld, new_val in updates.items():
            # Provenance-preserving merge (invariant #3): a weaker write never clobbers a
            # stronger existing row. A no-op for the strongest provenance (operator), so
            # the operator path keeps its whole-field-replace semantics.
            if fld == "emails":
                new_val = merge_emails_preserving_stronger(
                    list(entry.get("emails") or []), new_val, provenance)
            elif fld == "identities":
                new_val = merge_identities_preserving_stronger(
                    list(entry.get("identities") or []), new_val, provenance)
            old_val = entry.get(fld)
            if old_val == new_val:
                continue
            entry[fld] = new_val
            audit.append(model.AuditEntry(
                field=fld, from_=old_val, to=new_val, by=by,
                source=provenance, at=now).to_dict())
        entry["audit"] = audit
        persons[key] = entry
        return entry

    return _mutate(
        shared_dir, bot_id, action="upsert_entry", by=by,
        identity=ro.identity_key(platform, stable_id), mutate_fn=_apply,
        kind=_records.KIND_BY_PROVENANCE[provenance])


# ── Mutation orchestrator + audit log (mirrors roster_overlay) ────────────


def _write_one(shared_dir: Path, bot_id: str, identity: str,
               mutate_fn: "Callable[[dict], dict | None]", *, by: str, kind: str,
               note: str) -> "dict | None":
    """Read ONE Person row, mutate it, write it back + one ``contacts_seen`` entry
    — all inside one write transaction under the store's per-file lock, so two
    concurrent writes to the same Person serialize instead of losing one.

    ``mutate_fn`` gets ``{"bot_id", "version", "persons": {identity: entry}}``
    (the Person alone, or no persons yet) and returns the entry to write, or
    None to write nothing.
    """
    from .. import app_ledger, app_store

    _records.ensure_migrated(shared_dir, bot_id, create=True)
    with app_store.open_for_verb(
            Path(shared_dir), _records.DIRECTORY_APP_ID, app_store.PLATFORM, "put",
            bot_id, write=True) as (conn, app, _inst):
        current = _records.read_entry(conn, identity)
        directory = {"bot_id": bot_id, "version": CURRENT_VERSION,
                     "persons": {identity: current} if current is not None else {}}
        result = mutate_fn(directory)
        if result is None:
            return None
        app_store.put_in(conn, app, _records.PEOPLE,
                         _records.row_from_entry(identity, directory["persons"][identity]),
                         caller=app_store.PLATFORM, by=by)
        app_ledger.append_in(conn, app.schema, {
            "thing_id": identity, "kind": kind, "at": app_store._now(),  # noqa: SLF001
            "by": str(by)[:200], "amount": None, "counterparty": None, "note": note})
        return result


def _mutate(shared_dir: Path, bot_id: str, *, action: str, by: str,
            identity: str,
            mutate_fn: "Callable[[dict], dict]",
            kind: str = "captured") -> dict[str, Any]:
    """Load one Person → ``mutate_fn(directory)`` → write the row + a ledger entry
    → audit-log line.

    ``mutate_fn`` mutates the directory dict in place and returns the affected entry
    (recorded as the audit ``after`` payload).
    """
    result = _write_one(shared_dir, bot_id, identity, mutate_fn, by=by, kind=kind,
                        note=action)
    assert result is not None
    _audit_log_append(shared_dir, {
        "ts": _now_iso(), "action": action, "target_bot": bot_id,
        "by": by, "identity": identity, "after": result,
    })
    return result


def _audit_log_append(shared_dir: Path, record: dict[str, Any]) -> None:
    """Append one JSONL line to today's directory audit log.

    Failures log but do not raise — a mutation that wrote successfully must not be
    reported as failed because the observability append hit a transient I/O error (same
    discipline as ``roster_overlay._audit_log_append``). UTC date for the filename so
    cross-timezone reads find the same file.
    """
    try:
        today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        log_dir = Path(shared_dir) / "directory" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(log_dir / f"{today}.jsonl", "a") as f:
            f.write(line)
    except (PermissionError, OSError) as e:
        log.warning("directory audit log append failed: %s", e)
