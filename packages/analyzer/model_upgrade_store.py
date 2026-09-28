"""model_upgrade_store — the DURABLE STORES behind automatic model upgrades.

Spec: [internal/spec-model-auto-upgrade-2026-07-30.md](../../internal/spec-model-auto-upgrade-2026-07-30.md)
§Cadence, §Apply path, §Rollback — **Phase 3a**. Phase 1
(``model_auto_upgrade``, #3515) is the eligibility engine; Phase 2
(``model_liveness``, #3556) is the dispatch guard. This module is the third
leg: the state those two phases assume exists and nothing has ever written.

**Nothing here mutates any tier config.** No ``network.json``, no
``evolve-tiers.json``, no rungs, no roles, no routing. P3a is storage +
schema only — the apply path (P3b) and the rollback command (P3c) are the
callers, and they land separately. If a future edit to this module reaches
for a config writer, it is in the wrong file.

Why this exists at all
──────────────────────
``compute_auto_upgrades(network, first_seen=…)`` takes the first-seen ledger
as an **injected** mapping. When P3a landed, its only production caller (the
admin route ``evolve_admin.web.model_discovery_adopt.auto_upgrade_report``)
passed nothing, so ``visible_days`` was ``None`` on every live pod and
operator decision 1's guarantee — "the veto window comes from the item's own
age, not the calendar" — had no clock behind it.

**That gap is closed.** The route now passes
``first_seen=read_first_seen(shared_dir)`` (READ-only; the clock is advanced
by the daily ``model_discovery`` generator, never by page views, because a
clock advanced by page views would never start on a pod nobody opens). The
reference pod carries a populated ledger and the preview column reports real
``visible_days``, so ``HOLD_NOT_VISIBLE_LONG_ENOUGH`` is live rather than
theoretical. Do not re-derive "the clock is dead" from this section.

Three stores, all under ``{shared_dir}/model_upgrades/``::

    first-seen.json          the veto clock  (per-rung key)
    skips.json               operator "skip this version"  (per-family+version key)
    applied/<batch_id>.json  one weekly run's audit record  (rollback source)

All three are owned by the ``evolve`` user under ``{shared_dir}``, so writes
are temp-file + ``os.replace`` **in the destination directory** — no ``/tmp``
staging, no ``sudo`` (CLAUDE.md §"Arbiter (L1-L6) on-disk layout"; same shape
as ``model_discovery.write_listings_cache``).

Stamp-once, and why the clock is never derived
──────────────────────────────────────────────
First detection writes the timestamp. Re-detection **never** overwrites it.

The obvious "simplification" here is to skip the ledger and read the veto
clock off the existing freshness Signal's ``first_observed_at`` — the Signal
store already stamps that, and it looks like the same fact. It is not. A
provider listing blip (an HTTP failure, a rate limit, a transient omission)
makes the upgrade undetected for one run; the freshness Signal for it
sweep-resolves; the next run re-observes and mints a **fresh** Signal with a
**fresh** timestamp. The veto clock silently restarts, and an upgrade the
operator has been looking at for six days is suddenly one day old again. The
same listing noise that ``model_auto_upgrade``'s rule 2 refuses to read as
"nothing newer" must not be readable as "never seen before" either.

Hence: an upgrade that is detected, disappears for a run, and returns keeps
its **original** stamp. The ledger is the only thing in the system that can
promise that, which is exactly why it is a ledger and not a derivation.

Pruning errs toward waiting, never toward applying
──────────────────────────────────────────────────
Entries absent from detection for :data:`PRUNE_AFTER_DAYS` are dropped, and
a key detected on the current run is **never** pruned regardless of what the
clock says — pruning a live upgrade's stamp would restart its veto window
and re-open a decision the operator already had time to make. The prune runs
against the loaded snapshot *before* this run's refresh, so that exemption is
what actually protects a long-stale-but-currently-detected entry rather than
being masked by statement order.

Note the direction of the residual risk: an over-eager prune makes a
*tracked* upgrade wait longer (it re-stamps at now and the floor restarts),
never apply sooner. Only the overwrite hazard above runs the other way, and
stamp-once closes it.

A missing ENTRY is a different matter — see below.

An absent ledger is NOT a safe default, and P3b must treat it as a hold
──────────────────────────────────────────────────────────────────────
``model_auto_upgrade._judge`` evaluates the ``minVisibleDays`` floor only
when the key has a stamp::

    seen = _parse_iso(first_seen.get(decision.key()))
    if seen is not None:
        ...  # the floor, and HOLD_NOT_VISIBLE_LONG_ENOUGH

So **no entry means the floor is skipped, not failed** — the decision stays
eligible on that guard with ``visible_days=None``. That is deliberate and
harmless for Phase 1, which ships dark and only renders a preview column.
It is NOT harmless for the apply path: an unwritable ``{shared_dir}``, ACL
drift, a deactivated ``model_discovery`` generator, or a deleted file all
produce an empty ledger, and P3b reading it would apply upgrades the
operator had **zero** days to veto — precisely the guarantee operator
decision 1 exists to give.

**Contract for P3b:** a decision whose ``visible_days`` is ``None`` is
HELD, not applied. The ledger is the evidence that a veto window elapsed;
absence of evidence is not evidence of elapse — the same rule the cost guard
already applies to an unknown price. This module cannot enforce it (it does
not run the apply path), so it is stated here, in the spec addendum, and it
is the first thing P3b's tests should pin.

Two key spaces, deliberately different
──────────────────────────────────────
``first-seen`` is keyed **per rung**
(``model_auto_upgrade.upgrade_key`` = provider + rung slug + target): a rung
is *where the write lands*, the same version bump landing in two rungs is two
separate applies, and each one gets its own veto window.

A **skip** is keyed **per (family, target version)** (:func:`skip_key`), per
spec §Cadence's own wording: "records a per-(family, version) suppression".
Skipping Opus 5.0 means skipping it — not skipping it in the Standard rung
while it quietly applies in Power. An operator who declines a version has
declined the version.

Not to be conflated with the Phase-4 acknowledgement
────────────────────────────────────────────────────
Spec §"Permanently-held upgrades: nag once, then go quiet" describes a
*third* per-(family, version) thing: an acknowledgement that stops a
**held** row occupying the card. It is display-only, it never suppresses an
apply, and it belongs to Phase 4. It is **not** ``skips.json`` and must not
be folded into it — a skip suppresses an *eligible* upgrade (an operator
decision with apply-path consequences at step 5), an acknowledgement quiets
an upgrade the system has already decided it will never apply. If Phase 4
needs storage for it, it gets its own key space (``acknowledgements.json``
under the same directory), because the two answer different questions and
collapsing them would make "I have seen this" mean "do not do this".

Who writes the clock
────────────────────
The first-seen stamp is folded into the **daily** ``model_discovery``
generator run (``generators.model_discovery.observe.observe_signals``) —
never a page render. If page views advanced the clock, a pod whose operator
never opens the AI Optimization page would never start one, and the veto
window would be a function of who happened to look. The stamp step is
pure-read plus one small write: no LLM dispatch, no probe, no HTTP of its
own (it rides the listing pass that run already made).

"Daily" is the cadence, not the only entry point, and the difference is
worth naming: ``observe_signals`` is also reachable from the generator
runner's on-demand ``run_one_generator`` and, through that, from the canary
soak probe, which runs a *candidate* build's generator against the live
``{shared_dir}``. Both are bounded by stamp-once — the worst case is a clock
started up to a day early, which shortens no veto window materially. A
future breaking change to the ledger's on-disk shape does NOT enjoy that
bound, because ungated candidate code would write it to the live pod before
promotion; such a change needs a forward-compatible reader here first (see
``_load_entries``, which today tolerates the flat and enveloped v1 shapes).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import model_discovery as _md

#: Directory under ``{shared_dir}`` holding every auto-upgrade store.
STORE_DIRNAME = "model_upgrades"

FIRST_SEEN_NAME = "first-seen.json"
SKIPS_NAME = "skips.json"
APPLIED_DIRNAME = "applied"

#: Doc-shape version, carried on every store document. Bumped only on a
#: breaking shape change; readers below tolerate an absent version so a
#: hand-written or pre-versioned file still reads.
SCHEMA_VERSION = 1

#: An entry absent from detection for this long is pruned from the first-seen
#: ledger. Long on purpose: the ledger's whole job is to survive listing
#: noise, and a provider that has been unlistable for three straight months
#: is its own monitored condition, not something to quietly forget.
PRUNE_AFTER_DAYS = 90


# ── Paths ────────────────────────────────────────────────────────────────────


def store_dir(shared_dir: Path) -> Path:
    """``{shared_dir}/model_upgrades`` — the root of every store here."""
    return Path(shared_dir) / STORE_DIRNAME


def first_seen_path(shared_dir: Path) -> Path:
    return store_dir(shared_dir) / FIRST_SEEN_NAME


def skips_path(shared_dir: Path) -> Path:
    return store_dir(shared_dir) / SKIPS_NAME


def applied_dir(shared_dir: Path) -> Path:
    return store_dir(shared_dir) / APPLIED_DIRNAME


def batch_path(shared_dir: Path, batch_id: str) -> Path:
    return applied_dir(shared_dir) / f"{batch_id}.json"


# ── Low-level IO ─────────────────────────────────────────────────────────────


def _write_json(path: Path, doc: Any) -> Path:
    """Atomic temp-file + rename **in the destination directory**.

    Same filesystem → the rename is atomic, and the temp file inherits the
    directory's ACL, so no ``/tmp`` staging and no ``sudo`` (these paths live
    under ``{shared_dir}``, owned by the ``evolve`` user). Mirrors
    ``model_discovery.write_listings_cache``.

    Atomicity is per-WRITE, not per read-modify-write: nothing here holds a
    lock, so two overlapping runs can each load, mutate and store, and the
    later writer's snapshot loses the earlier one's new key. That direction is
    safe — a lost stamp is re-stamped on the next run and the upgrade waits
    *longer* — but it is a real weakening of the disappear-and-return
    guarantee under concurrency, so it is stated rather than assumed away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        # Never leave a half-written temp behind for the next run to trip on.
        # ``suppress`` rather than a handler: a cleanup failure must not mask
        # the real write error, which is re-raised immediately below.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def _envelope(doc: Any, key: str) -> Any:
    """The ``key`` section of a versioned store document, or the document
    itself when it carries no envelope — so a hand-written / pre-versioned
    flat file still reads."""
    if isinstance(doc, Mapping) and key in doc:
        return doc.get(key)
    return doc


def _read_json(path: Path) -> Any:
    """Parsed document, or ``None`` when absent/unreadable/corrupt.

    Missing is never an error in any of these stores: no ledger means no
    clock (the visibility floor simply is not evaluated), no skips file means
    no skips. A corrupt file reads the same way — the fail-safe direction is
    "this pod has no recorded suppression", not a crashed weekly job.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now(now: datetime | None) -> datetime:
    """The run clock, always tz-aware.

    A naive ``now`` is read as UTC rather than rejected — the same normalization
    ``_parse_iso`` applies to a naive stored timestamp. Without it a caller
    passing ``datetime.utcnow()`` would raise on the prune's datetime
    subtraction, and in the generator path that raise is swallowed, so the
    ledger would silently stop advancing.
    """
    dt = now or datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_list(value: Any) -> list:
    """A list of mappings from an untrusted document field.

    Shape-wrong values (a scalar where a list belongs) yield ``[]`` rather
    than raising. The stores' read contract is "missing or malformed reads as
    empty, never as an exception", and the record readers are what
    ``rollback-upgrade`` runs on — the one command that has to work when
    something has already gone wrong.
    """
    if not isinstance(value, list):
        return []
    return [dict(v) for v in value if isinstance(v, Mapping)]


def _as_dict(value: Any) -> dict:
    """A dict from an untrusted document field; anything else reads as ``{}``."""
    return dict(value) if isinstance(value, Mapping) else {}


def _as_strs(value: Any) -> list[str]:
    """A list of strings from an untrusted document field."""
    return [str(v) for v in value] if isinstance(value, list) else []


# ── Store 1: the first-seen ledger (the veto clock) ──────────────────────────


@dataclass(frozen=True)
class StampResult:
    """What one :func:`stamp_first_seen` pass did — reported, never silent.

    A stamping run that changed nothing still says what it saw: ``stamped``
    (clock started), ``kept`` (already had a stamp; stamp-once left it alone),
    ``pruned`` (absent long enough to forget).
    """

    stamped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    first_seen: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stamped": list(self.stamped),
            "kept": list(self.kept),
            "pruned": list(self.pruned),
            "tracked": len(self.first_seen),
        }


def _load_entries(shared_dir: Path) -> dict[str, dict[str, str]]:
    """The raw ledger as ``{key: {"first_seen": iso, "last_seen": iso}}``.

    Tolerates a flat ``{key: iso}`` document (a hand-written or pre-``last_seen``
    file) by reading the single timestamp as both bounds — the conservative
    reading, since it neither restarts a clock nor makes an entry look newer
    than it is.
    """
    raw = _envelope(_read_json(first_seen_path(shared_dir)), "entries")
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, val in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(val, str):
            if _parse_iso(val) is not None:
                out[key] = {"first_seen": val, "last_seen": val}
            continue
        if not isinstance(val, Mapping):
            continue
        first = val.get("first_seen")
        if _parse_iso(first) is None:
            continue
        last = val.get("last_seen")
        out[key] = {
            "first_seen": str(first),
            "last_seen": str(last) if _parse_iso(last) is not None else str(first),
        }
    return out


def read_first_seen(shared_dir: Path) -> dict[str, str]:
    """``{upgrade_key: first-detection ISO timestamp}`` — the exact shape
    ``model_auto_upgrade.compute_auto_upgrades(first_seen=…)`` consumes.

    The on-disk document carries a ``last_seen`` alongside each stamp because
    the prune rule is stated in terms of *absence from detection*, which a
    first-seen timestamp alone cannot express. That is an implementation
    detail of the file; the read API is the flat mapping the engine wants, so
    no caller has to know about it.
    """
    return {k: v["first_seen"] for k, v in _load_entries(shared_dir).items()}


def upgrade_keys(upgrades: Iterable[Any]) -> list[str]:
    """:func:`model_auto_upgrade.upgrade_key` over a sequence of
    ``model_discovery.VersionUpgrade`` rows (or anything carrying
    ``provider``/``rung_slug``/``latest_model``).

    Rows with no ``rung_slug`` are dropped: an unlocated upgrade has no apply
    target, so there is nothing for a veto window to protect.
    """
    from model_auto_upgrade import upgrade_key

    out: list[str] = []
    for up in upgrades or ():
        rung = getattr(up, "rung_slug", "") or ""
        latest = getattr(up, "latest_model", "") or ""
        if not rung or not latest:
            continue
        key = upgrade_key(getattr(up, "provider", "") or "", rung, latest)
        if key not in out:
            out.append(key)
    return out


def stamp_first_seen(
    shared_dir: Path,
    detected_keys: Iterable[str],
    *,
    now: datetime | None = None,
    prune_after_days: int | None = PRUNE_AFTER_DAYS,
) -> StampResult:
    """Record first detection for every key in ``detected_keys``; prune the
    long-absent. **Stamp-once** — an existing stamp is never overwritten.

    ``detected_keys`` is what THIS run saw. Each such key has its
    ``last_seen`` refreshed (that is the prune clock, and it is the only field
    a re-detection touches) and is **exempt from pruning outright** — the
    prune runs against the loaded snapshot *before* the refresh, so the
    exemption is what actually protects a long-stale-but-currently-detected
    entry rather than being masked by statement order.

    ``prune_after_days=None`` disables pruning for this call — the caller's
    escape hatch for a run whose detection was known-incomplete (a degraded
    provider listing contributes no keys, and a run that could not look does
    not get to forget).

    Returns a :class:`StampResult`. The ledger is rewritten on any run that
    detected or pruned anything, which in steady state is every run — a
    re-detection still has to advance ``last_seen``, or the prune clock would
    never move.
    """
    ts = _now(now)
    stamp = _iso(ts)
    detected = [k for k in dict.fromkeys(detected_keys or ()) if isinstance(k, str) and k]
    detected_set = set(detected)

    entries = _load_entries(shared_dir)
    # Prune off the LOADED snapshot, before this run's refresh — otherwise the
    # refresh alone would make every detected key look fresh and the exemption
    # below would be dead code that merely looks protective. Judging staleness
    # on what the ledger actually said, and exempting detection explicitly, is
    # what makes "a live upgrade's stamp is never pruned" a rule rather than a
    # side effect of statement order.
    pruned: list[str] = []
    if prune_after_days is not None and prune_after_days >= 0:
        cutoff = float(prune_after_days) * 86400.0
        for key in sorted(entries):
            if key in detected_set:
                continue  # never prune what this run just detected
            last = _parse_iso(entries[key].get("last_seen"))
            if last is None or (ts - last).total_seconds() > cutoff:
                pruned.append(key)
        for key in pruned:
            entries.pop(key, None)

    stamped: list[str] = []
    kept: list[str] = []
    for key in detected:
        cur = entries.get(key)
        if cur is None:
            entries[key] = {"first_seen": stamp, "last_seen": stamp}
            stamped.append(key)
            continue
        # Stamp-once: first_seen is immutable for the life of the entry.
        kept.append(key)
        entries[key] = {"first_seen": cur["first_seen"], "last_seen": stamp}

    if stamped or pruned or kept:
        _write_json(first_seen_path(shared_dir), {
            "version": SCHEMA_VERSION,
            "updated_at": stamp,
            "entries": entries,
        })
    return StampResult(
        stamped=stamped, kept=kept, pruned=pruned,
        first_seen={k: v["first_seen"] for k, v in entries.items()},
    )


# ── Store 2: skips ("Skip this version") ─────────────────────────────────────


def skip_key(family: str, latest_model: str) -> str:
    """Identity of a skip: ``(family, target version)`` — NOT the per-rung
    :func:`model_auto_upgrade.upgrade_key`.

    Spec §Cadence: Skip "records a per-(family, version) suppression and does
    not disable the policy". An operator who declines Opus 5.0 has declined
    the version, so the suppression cannot be scoped to whichever rung the
    card row happened to be about — it would otherwise apply in a sibling rung
    the operator never looked at.
    """
    fam = (family or "").strip().lower()
    target = _md._bare_id(latest_model or "").lower()
    return f"{fam}:{target}"


@dataclass(frozen=True)
class SkipRecord:
    """One recorded "skip this version" decision."""

    key: str
    family: str
    latest_model: str
    recorded_at: str
    actor: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "family": self.family,
            "latest_model": self.latest_model,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "note": self.note,
        }


def read_skips(shared_dir: Path) -> dict[str, SkipRecord]:
    """Every recorded skip, keyed by :func:`skip_key`.

    A missing file means **no skips**, never an error — a pod that has never
    skipped anything is the overwhelmingly common case and must not make the
    weekly job raise. A malformed file reads the same way, and individual
    junk entries are dropped rather than sinking the whole document: a
    half-corrupt file still suppresses the versions it can still name.
    """
    raw = _envelope(_read_json(skips_path(shared_dir)), "skips")
    out: dict[str, SkipRecord] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, val in raw.items():
        if not isinstance(key, str) or not key or not isinstance(val, Mapping):
            continue
        out[key] = SkipRecord(
            key=key,
            family=str(val.get("family") or ""),
            latest_model=str(val.get("latest_model") or ""),
            recorded_at=str(val.get("recorded_at") or ""),
            actor=str(val.get("actor") or ""),
            note=str(val.get("note") or ""),
        )
    return out


def is_skipped(
    skips: Mapping[str, SkipRecord] | None, family: str, latest_model: str,
) -> bool:
    """Whether this (family, target version) carries a recorded skip.

    Takes the already-read map so the apply path reads the file once per run
    rather than once per candidate (spec §Apply path step 5).
    """
    return skip_key(family, latest_model) in (skips or {})


def record_skip(
    shared_dir: Path,
    family: str,
    latest_model: str,
    *,
    actor: str = "",
    note: str = "",
    now: datetime | None = None,
) -> SkipRecord:
    """Record a skip. Idempotent by key — re-skipping keeps the ORIGINAL
    timestamp, so "when did we decline this?" stays answerable."""
    key = skip_key(family, latest_model)
    skips = read_skips(shared_dir)
    existing = skips.get(key)
    rec = SkipRecord(
        key=key,
        family=(family or "").strip().lower(),
        latest_model=_md._bare_id(latest_model or ""),
        recorded_at=existing.recorded_at if existing else _iso(_now(now)),
        actor=existing.actor if existing else actor,
        note=note or (existing.note if existing else ""),
    )
    skips[key] = rec
    _write_json(skips_path(shared_dir), {
        "version": SCHEMA_VERSION,
        "skips": {k: v.to_dict() for k, v in sorted(skips.items())},
    })
    return rec


def clear_skip(
    shared_dir: Path, family: str, latest_model: str,
) -> bool:
    """Undo a skip. Returns True when one was actually removed.

    A skip an operator cannot take back is a trap: the next version bump in
    that family surfaces fresh (the key is per-version), but the *skipped*
    version would otherwise be suppressed forever with no surface to reverse
    it.
    """
    key = skip_key(family, latest_model)
    skips = read_skips(shared_dir)
    if key not in skips:
        return False
    skips.pop(key)
    _write_json(skips_path(shared_dir), {
        "version": SCHEMA_VERSION,
        "skips": {k: v.to_dict() for k, v in sorted(skips.items())},
    })
    return True


# ── Store 3: the audit record (one batch per weekly run) ─────────────────────


#: A scope write that landed, and one that was attempted and failed. A failed
#: scope MUST be recorded rather than omitted (the run has to say what it
#: tried), and rollback MUST skip it — restoring ``models_before`` into a
#: document the apply never touched would revert whatever the operator did
#: there since.
SCOPE_APPLIED = "applied"
SCOPE_FAILED = "failed"


@dataclass(frozen=True)
class ScopeChange:
    """One config scope's rung, before and after — **verbatim**.

    ``scope`` uses ``model_auto_upgrade.apply_editable_documents``' labelling
    (``"pod"`` | ``"<bot_id>"`` | ``"<bot_id>:config"``) so the record names
    exactly the document the writer touched. Note that only the first two
    labels have a writer in v1 (pod → the ``AdoptModel`` applier's
    ``network.json`` save; bot → the ``oc_model.py`` write whitelist for
    ``evolve-tiers.json``). An upgrade located only in a bot's
    ``openclaw.json`` models block (``"<bot_id>:config"``) has no automated
    write path and is held, not hand-written — the label exists here because
    the enumeration produces it, not because a record should ever carry it.

    ``models_before`` is the whole prior ``models`` list, not a diff. Exact
    restoration beats reconstruction: a diff would have to assume nothing else
    moved the list between apply and rollback, and on a pod where an operator
    edits rungs by hand that assumption is wrong precisely when rollback
    matters most.

    ``rung_created`` disambiguates an empty ``models_before``: "the rung
    existed and was empty" and "the rung did not exist" restore differently,
    and ``_splice_rung`` can create a rung. ``cost_class`` carries the created
    rung's band so a rollback that has to remove it knows what it removed.
    """

    scope: str
    rung_slug: str
    models_before: list[str] = field(default_factory=list)
    models_after: list[str] = field(default_factory=list)
    status: str = SCOPE_APPLIED
    error: str = ""
    rung_created: bool = False
    cost_class: str = ""

    @property
    def applied(self) -> bool:
        return self.status == SCOPE_APPLIED

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "rung_slug": self.rung_slug,
            "models_before": list(self.models_before),
            "models_after": list(self.models_after),
            "status": self.status,
            "error": self.error,
            "rung_created": self.rung_created,
            "cost_class": self.cost_class,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "ScopeChange":
        status = str(d.get("status") or SCOPE_APPLIED)
        return ScopeChange(
            scope=str(d.get("scope") or ""),
            rung_slug=str(d.get("rung_slug") or ""),
            models_before=_as_strs(d.get("models_before")),
            models_after=_as_strs(d.get("models_after")),
            status=status if status in (SCOPE_APPLIED, SCOPE_FAILED) else SCOPE_FAILED,
            error=str(d.get("error") or ""),
            rung_created=bool(d.get("rung_created")),
            cost_class=str(d.get("cost_class") or ""),
        )


@dataclass(frozen=True)
class AppliedUpgrade:
    """One upgrade that was applied, across every scope it was written to.

    Multi-scope is load-bearing (operator-confirmed 2026-08-03): v1 applies to
    the pod scope *and* to any Custom bot whose own ``evolve-tiers.json``
    turned auto-upgrade on, because pod-only would make the per-bot toggle a
    lie. So the unit of the record is (entry, scope), not entry — a single
    ``models_before`` would silently describe one of several edits.
    """

    upgrade_key: str
    provider: str
    family: str
    current_model: str
    latest_model: str
    rung_slug: str
    scopes: list[ScopeChange] = field(default_factory=list)

    @property
    def applied_scopes(self) -> list[ScopeChange]:
        """Only the scopes whose write actually landed — the set a rollback
        may restore. A partial apply (pod ok, bot write failed) must not read
        as a clean two-scope apply."""
        return [s for s in self.scopes if s.applied]

    def to_dict(self) -> dict:
        return {
            "upgrade_key": self.upgrade_key,
            "provider": self.provider,
            "family": self.family,
            "current_model": self.current_model,
            "latest_model": self.latest_model,
            "rung_slug": self.rung_slug,
            "scopes": [s.to_dict() for s in self.scopes],
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "AppliedUpgrade":
        return AppliedUpgrade(
            upgrade_key=str(d.get("upgrade_key") or ""),
            provider=str(d.get("provider") or ""),
            family=str(d.get("family") or ""),
            current_model=str(d.get("current_model") or ""),
            latest_model=str(d.get("latest_model") or ""),
            rung_slug=str(d.get("rung_slug") or ""),
            scopes=[ScopeChange.from_dict(s) for s in _as_list(d.get("scopes"))],
        )


@dataclass(frozen=True)
class HeldUpgrade:
    """One upgrade the run considered and did NOT apply, with its reasons.

    Spec §Failure and safety posture: "a run that applies nothing must still
    say what it considered and why each item was held — otherwise this feature
    reproduces the 57-emitted/0-applied invisibility one layer down". The
    audit record is where that survives the run, so the reason stays
    retrievable even after Phase 4 stops showing a durably-held row on the
    card (suppression there is display-only, by design).

    ``holds`` carries ``model_auto_upgrade.HoldReason.to_dict()`` payloads
    verbatim — code, operator sentence, evidence, durability.
    """

    upgrade_key: str
    provider: str
    family: str
    current_model: str
    latest_model: str
    rung_slug: str
    holds: list[dict] = field(default_factory=list)

    @property
    def durable(self) -> bool:
        return any(bool(h.get("durable")) for h in self.holds)

    @property
    def reason_codes(self) -> list[str]:
        return [str(h.get("code") or "") for h in self.holds]

    def to_dict(self) -> dict:
        return {
            "upgrade_key": self.upgrade_key,
            "provider": self.provider,
            "family": self.family,
            "current_model": self.current_model,
            "latest_model": self.latest_model,
            "rung_slug": self.rung_slug,
            "holds": [dict(h) for h in self.holds],
            "durable": self.durable,
            "reason_codes": self.reason_codes,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "HeldUpgrade":
        return HeldUpgrade(
            upgrade_key=str(d.get("upgrade_key") or ""),
            provider=str(d.get("provider") or ""),
            family=str(d.get("family") or ""),
            current_model=str(d.get("current_model") or ""),
            latest_model=str(d.get("latest_model") or ""),
            rung_slug=str(d.get("rung_slug") or ""),
            holds=_as_list(d.get("holds")),
        )


@dataclass(frozen=True)
class UpgradeBatch:
    """One weekly apply run, recorded whole.

    The unit is a **batch** because that is the unit rollback reverts:
    ``sudo evolve-admin models rollback-upgrade`` with no argument undoes the
    most recent batch (spec §Rollback), and ``--id`` addresses one entry
    inside it.

    ``policy`` is the resolved :class:`model_auto_upgrade.AutoUpgradePolicy`
    in effect — recorded because "why did this apply?" is unanswerable a month
    later if the knobs have since changed, and a run that applied something
    under a waived cost guard must be able to say so.
    """

    batch_id: str
    ran_at: str
    policy: dict = field(default_factory=dict)
    applied: list[AppliedUpgrade] = field(default_factory=list)
    held: list[HeldUpgrade] = field(default_factory=list)
    skipped_providers: list[dict] = field(default_factory=list)
    notes: str = ""
    #: Set by :func:`mark_rolled_back` once P3c has reverted this batch. It
    #: exists so a second bare ``rollback-upgrade`` does not silently re-revert
    #: the same batch — which, after a deliberate re-adoption, would undo the
    #: operator's choice with no indication it had happened before.
    rolled_back_at: str = ""

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    @property
    def rolled_back(self) -> bool:
        return bool(self.rolled_back_at)

    def entry(self, upgrade_key: str) -> AppliedUpgrade | None:
        """The applied entry with this key — ``rollback-upgrade --id``'s
        lookup. First match wins; a batch is not expected to carry the same
        ``upgrade_key`` twice (it is one decision per rung per run)."""
        return next(
            (a for a in self.applied if a.upgrade_key == upgrade_key), None,
        )

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "ran_at": self.ran_at,
            "policy": dict(self.policy),
            "applied": [a.to_dict() for a in self.applied],
            "held": [h.to_dict() for h in self.held],
            "skipped_providers": [dict(p) for p in self.skipped_providers],
            "notes": self.notes,
            "rolled_back_at": self.rolled_back_at,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "UpgradeBatch":
        return UpgradeBatch(
            batch_id=str(d.get("batch_id") or ""),
            ran_at=str(d.get("ran_at") or ""),
            policy=_as_dict(d.get("policy")),
            applied=[AppliedUpgrade.from_dict(a) for a in _as_list(d.get("applied"))],
            held=[HeldUpgrade.from_dict(h) for h in _as_list(d.get("held"))],
            skipped_providers=_as_list(d.get("skipped_providers")),
            notes=str(d.get("notes") or ""),
            rolled_back_at=str(d.get("rolled_back_at") or ""),
        )


def new_batch_id(now: datetime | None = None) -> str:
    """Sortable batch id: ``<UTC compact timestamp>-<short random>``.

    Lexicographic order equals chronological order, which is what makes
    :func:`latest_batch` a directory listing rather than a full parse of every
    record. The random tail only guards two runs inside the same second.
    """
    ts = _now(now).astimezone(timezone.utc)
    return f"{ts.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def write_batch(shared_dir: Path, batch: UpgradeBatch) -> Path:
    """Persist one batch record. Atomic; returns the path.

    P3b calls this after an apply run; P3c reads it back. Shipping the
    writer and the reader with no in-tree caller is deliberate — the schema
    is the interface those two phases agree on, and pinning it here with its
    own tests is what stops P3c from having to reverse-engineer whatever P3b
    happened to emit.
    """
    return _write_json(batch_path(shared_dir, batch.batch_id), batch.to_dict())


def read_batch(shared_dir: Path, batch_id: str) -> UpgradeBatch | None:
    """One batch by id, or ``None`` when absent/unreadable."""
    doc = _read_json(batch_path(shared_dir, batch_id))
    if not isinstance(doc, Mapping):
        return None
    return UpgradeBatch.from_dict(doc)


def batch_ids(shared_dir: Path) -> list[str]:
    """Every recorded batch id, oldest first (ids sort chronologically)."""
    d = applied_dir(shared_dir)
    try:
        names = [p.stem for p in d.glob("*.json") if p.is_file()]
    except OSError:
        return []
    return sorted(names)


def iter_batches(shared_dir: Path) -> list[UpgradeBatch]:
    """Every readable batch, oldest first. Unreadable records are skipped,
    not raised — one corrupt file must not hide the rest of the history."""
    out: list[UpgradeBatch] = []
    for bid in batch_ids(shared_dir):
        batch = read_batch(shared_dir, bid)
        if batch is not None:
            out.append(batch)
    return out


def latest_batch(
    shared_dir: Path, *, skip_rolled_back: bool = False,
) -> UpgradeBatch | None:
    """The most recent batch — ``rollback-upgrade``'s default target.

    Walks newest-first and returns the first record that actually parses, so
    a corrupt newest file degrades to "roll back the one before it" rather
    than to "there is nothing to roll back".

    ``skip_rolled_back=True`` also walks past batches already reverted, so a
    repeated bare ``rollback-upgrade`` does not re-revert the same batch and
    quietly undo a deliberate re-adoption.
    """
    for bid in reversed(batch_ids(shared_dir)):
        batch = read_batch(shared_dir, bid)
        if batch is None:
            continue
        if skip_rolled_back and batch.rolled_back:
            continue
        return batch
    return None


def mark_rolled_back(
    shared_dir: Path, batch_id: str, *, now: datetime | None = None,
) -> UpgradeBatch | None:
    """Stamp a batch as reverted. Returns the updated record, or ``None`` when
    it could not be read.

    P3c calls this after a successful rollback. Re-marking keeps the ORIGINAL
    timestamp — "when was this undone?" stays answerable.
    """
    batch = read_batch(shared_dir, batch_id)
    if batch is None:
        return None
    if batch.rolled_back:
        return batch
    updated = UpgradeBatch(
        batch_id=batch.batch_id, ran_at=batch.ran_at, policy=batch.policy,
        applied=batch.applied, held=batch.held,
        skipped_providers=batch.skipped_providers, notes=batch.notes,
        rolled_back_at=_iso(_now(now)),
    )
    write_batch(shared_dir, updated)
    return updated


def find_applied(
    shared_dir: Path, upgrade_key: str, *, skip_rolled_back: bool = False,
) -> tuple[UpgradeBatch, AppliedUpgrade] | None:
    """The most recent batch that applied ``upgrade_key``, with that entry —
    ``rollback-upgrade --id <key>``'s lookup when no batch is named.

    ``skip_rolled_back=True`` ignores batches already reverted, for the same
    don't-re-revert reason as :func:`latest_batch`.
    """
    for batch in reversed(iter_batches(shared_dir)):
        if skip_rolled_back and batch.rolled_back:
            continue
        entry = batch.entry(upgrade_key)
        if entry is not None:
            return batch, entry
    return None


# ── Convenience: build the held-back half of a record from a Phase 1 report ──


def held_from_decisions(decisions: Sequence[Any]) -> list[HeldUpgrade]:
    """``model_auto_upgrade.UpgradeDecision`` rows → :class:`HeldUpgrade`
    records, so P3b's anti-invisibility obligation is one call rather than a
    hand-rolled projection at the call site."""
    out: list[HeldUpgrade] = []
    for d in decisions or ():
        out.append(HeldUpgrade(
            upgrade_key=d.key(),
            provider=getattr(d, "provider", "") or "",
            family=getattr(d, "family", "") or "",
            current_model=getattr(d, "current_model", "") or "",
            latest_model=getattr(d, "latest_model", "") or "",
            rung_slug=getattr(d, "rung_slug", "") or "",
            holds=[h.to_dict() for h in (getattr(d, "holds", None) or [])],
        ))
    return out
