"""arbiter.store — Filesystem-backed Proposal persistence.

Proposals live under ``{shared_dir}/proposals/{subdir}/{proposal_id}.json``
where ``subdir`` is one of:
  - ``pending``  — awaiting routing or user action
  - ``snoozed``  — deferred until ``snoozed_until``
  - ``applied``  — applier ran; verify daemon owns them next
  - ``archived`` — terminal states (succeeded / failed_* / rejected / dismissed / superseded)

The current status of the Proposal is authoritative. The subdir is a
physical index for efficient filtering and to mirror how the verify
daemon + snooze-wake loop already traverse the tree.

This module is deliberately minimal: atomic write, single-file read, a
glob-backed iterator, and a move helper. Mutating sequences (the
coalesce branch of write_proposal, move_proposal, the dedup scan) hold
a cross-process flock — concurrent writers are real (admin-ui, heal,
verify, generator runner, subscriber dispatch, evo MCP tools, per-bot
daemons). See internal/spec-state-store-and-deploy-resilience-2026-06-10.md
§1.4.

Lock ordering invariant: proposals → signals only. write_proposal calls
into signals.store (backrefs) while holding the proposals lock; nothing
in signals.store may ever call back into this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterator, Literal

from evolve_util import atomic_write_json
from schema.proposal import Proposal, action_to_dict
from store_lock import StoreLockTimeout, locked as _flock  # noqa: F401 — re-exported


log = logging.getLogger(__name__)


Subdir = Literal["pending", "snoozed", "applied", "archived"]

_STATUS_TO_SUBDIR: dict[str, Subdir] = {
    "draft": "pending",
    "pending": "pending",
    "approved_auto": "pending",
    "approved_human": "pending",
    # Slice 3 (2026-06-04): dispatched proposals stay in pending/
    # subdir while in flight — the operator still has the option to
    # cancel and the result callback resolves to applied/failed which
    # then move subdirs the usual way.
    "dispatched": "pending",
    "snoozed": "snoozed",
    "applied": "applied",
    "succeeded": "archived",
    "failed_reverted": "archived",
    "failed_flagged": "archived",
    "failed_revert_failed": "archived",
    "rejected": "archived",
    "dismissed": "archived",
    "superseded": "archived",
    "resolved_externally": "archived",
}

_ALL_SUBDIRS: tuple[Subdir, ...] = ("pending", "snoozed", "applied", "archived")

# Pod-wide Proposals carry ``bot_id="<pod>"`` (Proposal.bot_id may not be
# empty) while the runner buckets pod-wide emissions under ``""``. The sweep
# below must collapse the same spellings so a pod-wide proposal's stored
# bot_id and its emission bucket key agree. Kept in sync with
# generator_runner._bucket_key.
_POD_SENTINEL = "<pod>"


def _bucket_key(bot_id: str | None) -> str:
    """Canonical bucketing key: ``None`` / ``""`` / ``"<pod>"`` → ``""``."""
    if not bot_id or bot_id == _POD_SENTINEL:
        return ""
    return bot_id


def proposals_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "proposals"


LOCK_FILE_NAME = ".store.lock"


def lock_path(shared_dir: Path) -> Path:
    return proposals_root(shared_dir) / LOCK_FILE_NAME


def locked(shared_dir: Path):
    """Public critical-section guard for callers whose check-then-write
    spans multiple store calls (e.g. the runner's find_open_duplicate →
    refresh-or-write sequence). Reentrant — the store's own locking
    nests inside it."""
    return _flock(lock_path(shared_dir))


def proposal_path(
    shared_dir: Path, proposal_id: str, *, subdir: Subdir
) -> Path:
    return proposals_root(shared_dir) / subdir / f"{proposal_id}.json"


def subdir_for_status(status: str) -> Subdir:
    return _STATUS_TO_SUBDIR.get(status, "pending")


# ─────────────────────────────────────────────────────────────────────────────
# Write / read
# ─────────────────────────────────────────────────────────────────────────────

# Proposal writes pass mode=0o644 (world-readable): a proposal written by
# one user (e.g. the security_bot applier) must stay readable by every
# other system daemon (the evolve-run auto-resolve sweep, admin UI).
# mkstemp's default 0o600 would break that cross-daemon coordination; the
# contents are not sensitive (proposals are intentionally inspectable).
_PROPOSAL_FILE_MODE = 0o644


def write_proposal(
    proposal: Proposal,
    shared_dir: Path,
    *,
    subdir: Subdir | None = None,
    maintain_signal_backrefs: bool = True,
    coalesce: bool = True,
) -> Path:
    """Write a proposal to disk under the subdir matching its status.

    If ``subdir`` is given, it overrides the status-derived default (rare
    — the verify daemon wants to force-promote an applied proposal back
    for review, for example).

    ``maintain_signal_backrefs`` (default True) attaches this proposal's
    id to ``Signal.motivated_proposals`` for every entry in
    ``proposal.motivating_signals``. Idempotent — the signal store's
    ``attach_proposal`` is a no-op when the link already exists, so
    re-writes during status transitions don't grow the list. Set False
    to skip when the caller is doing bulk migrations and will batch-fix
    backrefs separately. Failures here are logged but never raise — a
    missing backref is recoverable, but a write_proposal that aborts
    leaves the canonical record half-written.

    ``coalesce`` (default True) folds this proposal into an existing
    pending parent when both share a non-empty ``coalesce_key`` — see
    :func:`_coalesce_into_parent`. Set False on rewrites (status
    transitions, in-place updates) where the proposal is the parent
    itself and re-coalescing would no-op or self-merge.

    Holds the store lock across the whole sequence: the coalesce branch
    is find-then-merge (two concurrent writers of one coalesce_key must
    not both miss the parent lookup and both become parents), and the
    backref maintenance nests the signals lock inside the proposals
    lock per the module's lock-ordering invariant.
    """
    with _flock(lock_path(shared_dir)):
        if coalesce and proposal.coalesce_key:
            coalesced = _coalesce_into_parent(proposal, shared_dir)
            if coalesced is not None:
                return coalesced

        target_subdir: Subdir = subdir or subdir_for_status(proposal.status)
        path = proposal_path(shared_dir, proposal.id, subdir=target_subdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, proposal.to_dict(), mode=_PROPOSAL_FILE_MODE)

        if maintain_signal_backrefs and proposal.motivating_signals:
            _maintain_signal_backrefs(proposal, shared_dir)

        return path


def _find_pending_by_coalesce_key(
    shared_dir: Path, coalesce_key: str
) -> tuple[Proposal, Path] | None:
    """Locate the existing pending parent for a coalesce_key, if any.

    Only scans ``pending/`` — snoozed / archived parents do not
    accumulate new sub-findings (the operator already decided about
    them). Returns the first match; coalesce_keys are expected to be
    unique-per-root-cause so a single match is the steady state.
    """
    root = proposals_root(shared_dir) / "pending"
    if not root.exists():
        return None
    for path in sorted(root.glob("*.json")):
        parent = load_proposal_file(path)
        if parent is None:
            continue
        if parent.coalesce_key == coalesce_key:
            return parent, path
    return None


def _sub_finding_record(proposal: Proposal) -> dict:
    """Project the sub-finding's identity, display, and action fields onto
    a parent.

    The ``trigger_observation`` field is the dedup key on re-runs.

    ``action`` carries the folded proposal's serialized action so a folded
    item stays *independently actionable* from the drill-down, not merely
    display-only. The coalesce machinery was first built for
    investigate-once findings (app_audit / app_permission_review) where the
    folded items are correctly display-only; ``model_discovery`` is the first
    user whose folded items (``AdoptModel`` proposals) are N independently
    actionable, so the admin UI / server reconstruct each model's adopt
    control from this field (design-recommendation-legibility-2026-06-12.md,
    Bite 2). Carrying it is generic — any coalesced group of actionable
    proposals becomes per-item adoptable for free; display-only generators
    simply never render the control.
    """
    return {
        "id": proposal.id,
        "trigger_observation": (
            proposal.trigger_observations[0]
            if proposal.trigger_observations
            else ""
        ),
        "problem": proposal.problem,
        "admin_surface_summary": proposal.admin_surface_summary,
        "summary": proposal.summary,
        "action": action_to_dict(proposal.action),
        "provenance_signals": dict(proposal.provenance.signals or {}),
        "dismiss_signature": proposal.dismiss_signature,
        "created_at": proposal.created_at,
    }


def _coalesce_into_parent(
    proposal: Proposal, shared_dir: Path
) -> Path | None:
    """Fold ``proposal`` into the existing pending parent sharing its
    ``coalesce_key``, if one exists.

    Returns the parent's path on a successful merge, or ``None`` when no
    parent was found (caller writes the proposal as the new parent).

    Dedup is per-trigger_observation: an incoming finding whose
    trigger_observation already appears in ``parent.sub_findings`` (or
    matches the parent's own trigger_observation) is a no-op — re-runs
    don't grow the list. This keeps the parent stable across cycles.
    """
    located = _find_pending_by_coalesce_key(shared_dir, proposal.coalesce_key or "")
    if located is None:
        return None
    parent, parent_path = located
    if parent.id == proposal.id:
        # Rewriting the parent in place — no merge.
        return None

    incoming_trigger = (
        proposal.trigger_observations[0]
        if proposal.trigger_observations
        else ""
    )
    parent_trigger = (
        parent.trigger_observations[0]
        if parent.trigger_observations
        else ""
    )
    if incoming_trigger and incoming_trigger == parent_trigger:
        # Re-emission of the very first finding; parent already covers it.
        return parent_path
    if incoming_trigger and any(
        sf.get("trigger_observation") == incoming_trigger
        for sf in parent.sub_findings
    ):
        return parent_path

    parent.sub_findings.append(_sub_finding_record(proposal))
    atomic_write_json(parent_path, parent.to_dict(), mode=_PROPOSAL_FILE_MODE)
    return parent_path


def _maintain_signal_backrefs(proposal: Proposal, shared_dir: Path) -> None:
    """Attach this proposal's id to each motivating Signal's
    ``motivated_proposals`` list.

    Best-effort: missing signal files (e.g. resolved/archived after the
    proposal was created), permission errors, and other store failures
    are logged but never raised. The proposal's own write has already
    succeeded by the time this runs — perturbing that flow over a
    denormalized backref would be wrong.
    """
    # Local import to avoid a hard arbiter → signals cycle at module-
    # load time; the signals module imports schema which is shared.
    try:
        from signals import store as signals_store
    except ImportError:
        log.debug(
            "signals.store unavailable — skipping motivated_proposals "
            "backref for proposal %s", proposal.id,
        )
        return

    for sig_id in proposal.motivating_signals:
        if not sig_id:
            continue
        try:
            located = signals_store.find_signal(shared_dir, sig_id)
        except Exception as e:  # noqa: BLE001
            log.debug(
                "could not load motivating signal %s for proposal %s: %s",
                sig_id, proposal.id, e,
            )
            continue
        if located is None:
            # find_signal returns None when no file exists in any subdir.
            # The signal's archived copy could in principle still receive
            # the backref, but find_signal walks all subdirs already; if
            # we got None the signal really isn't on disk. The proposal's
            # motivating_signals list is the canonical link regardless.
            continue
        # find_signal returns (Signal, Path, Subdir) — we only need the
        # Signal for attach_proposal, which re-derives the path.
        signal, _path, _subdir = located
        try:
            signals_store.attach_proposal(
                signal, shared_dir, proposal_id=proposal.id,
            )
        except Exception as e:  # noqa: BLE001
            log.debug(
                "attach_proposal failed for signal %s ← proposal %s: %s",
                sig_id, proposal.id, e,
            )


def load_proposal_file(path: Path) -> Proposal | None:
    """Load a Proposal from a specific path, or None if unreadable."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Proposal.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def find_proposal(
    shared_dir: Path, proposal_id: str
) -> tuple[Proposal, Path, Subdir] | None:
    """Locate a proposal by id across subdirs; return (prop, path, subdir)."""
    for sd in _ALL_SUBDIRS:
        path = proposal_path(shared_dir, proposal_id, subdir=sd)
        proposal = load_proposal_file(path)
        if proposal is not None:
            return proposal, path, sd
    return None


def iter_proposals(
    shared_dir: Path, *, subdirs: tuple[Subdir, ...] = ("pending",)
) -> Iterator[Proposal]:
    """Yield every readable Proposal from the given subdirs."""
    root = proposals_root(shared_dir)
    for sd in subdirs:
        dir_path = root / sd
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.json")):
            proposal = load_proposal_file(path)
            if proposal is not None:
                yield proposal


def move_proposal(
    proposal: Proposal,
    shared_dir: Path,
    *,
    from_subdir: Subdir,
    to_subdir: Subdir | None = None,
    expected_status: str | None = None,
) -> Path:
    """Rewrite the proposal in the destination subdir and remove the source.

    ``to_subdir`` defaults to whatever the current status maps to.

    **Atomicity.** The transition is done as: write the updated content
    into the *source* path (via `atomic_write_json`'s tempfile + replace
    pattern), then `os.replace(src, dest)`. Both steps are atomic on the
    same filesystem, and they are sequenced such that no failure mode can
    leave the proposal in BOTH subdirs at once — the previous
    write-dest-then-unlink-src order would do exactly that when the unlink
    failed (e.g. EACCES from a foreign-owned source file under a sticky-bit
    parent dir), leaving the dismiss endpoint reporting a half-success and
    the proposal duplicated across subdirs.

    **Concurrency (spec §1.4).** Holds the store lock, and verifies under
    it that the source file still exists — without that check, a mover
    that lost a race re-creates the already-moved source via the staging
    write and then renames it to a *different* destination, leaving the
    proposal in two subdirs at once (the dismiss-vs-approve race). A
    vanished source raises ``OSError`` (lost race detected, not papered
    over).

    ``expected_status`` (optional): when given, the on-disk source is
    re-read under the lock and must still carry this status, else
    ``OSError`` — CAS for callers whose load→transition→move sequence
    spans the lock (the sweeps pass their pre-transition status so a
    concurrent operator action wins over a background sweep).

    Failure modes:
      - Write of the updated content fails → src may be untouched (its
        prior content is intact via tempfile semantics); raises ``OSError``.
      - ``os.replace(src, dest)`` fails (e.g. EACCES, cross-device) → src
        has the updated content, dest is unchanged; raises ``OSError``. The
        caller sees a real error and the proposal is in exactly ONE place
        (src) with the new status — retrying the transition once the
        underlying error is fixed (e.g. dir owner) is safe.

    Raises ``OSError`` from either step rather than swallowing it.
    """
    dest_subdir: Subdir = to_subdir or subdir_for_status(proposal.status)
    src = proposal_path(shared_dir, proposal.id, subdir=from_subdir)
    dest = proposal_path(shared_dir, proposal.id, subdir=dest_subdir)
    with _flock(lock_path(shared_dir)):
        if src == dest:
            # No move; just persist updated content in place.
            atomic_write_json(dest, proposal.to_dict(), mode=_PROPOSAL_FILE_MODE)
            return dest
        if not src.exists():
            raise OSError(
                f"move_proposal: source vanished (lost a concurrent "
                f"transition race?): {src}"
            )
        if expected_status is not None:
            on_disk = load_proposal_file(src)
            if on_disk is None or on_disk.status != expected_status:
                raise OSError(
                    f"move_proposal: status changed under us "
                    f"(expected {expected_status!r}, found "
                    f"{getattr(on_disk, 'status', None)!r}): {src}"
                )
        # Stage updated content at src (atomic), then atomically rename
        # to dest.
        atomic_write_json(src, proposal.to_dict(), mode=_PROPOSAL_FILE_MODE)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        return dest


def delete_proposal(
    shared_dir: Path, proposal_id: str, *, subdir: Subdir
) -> bool:
    """Remove a proposal file from a subdir. Returns True if deleted."""
    path = proposal_path(shared_dir, proposal_id, subdir=subdir)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Write-time dedup guard
# ─────────────────────────────────────────────────────────────────────────────


def find_open_duplicate(
    proposal: Proposal, shared_dir: Path
) -> Proposal | None:
    """Return the open (pending or snoozed) proposal with the same fingerprint, if any.

    Belt to ``arbiter.dedup``'s suspenders: the in-memory dedup hook in
    ``arbiter.ingest`` relies on a snapshot of open proposals collected
    once per cycle. If anything causes that snapshot to miss an existing
    duplicate (transient unreadable file, race with a concurrent write,
    process restart, etc.), this function catches it at write time by
    scanning the on-disk pending+snoozed proposals.

    Caller (the runner) should treat a hit exactly like an in-memory
    dedup hit: refresh the existing proposal instead of writing the new
    one — and should hold :func:`locked` across the whole
    check-then-write sequence so two concurrent runners (daily sweep +
    subscriber dispatch) can't both miss and both write.
    """
    # Local import: dedup imports schema.proposal which imports store —
    # keep the module-level import graph one-way.
    from arbiter.dedup import compute_fingerprint

    target_fp = compute_fingerprint(proposal)
    with _flock(lock_path(shared_dir)):
        for existing in iter_proposals(shared_dir, subdirs=("pending", "snoozed")):
            if existing.id == proposal.id:
                continue
            if compute_fingerprint(existing) == target_fp:
                return existing
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve for proposals
# ─────────────────────────────────────────────────────────────────────────────


def sweep_resolve_proposals(
    shared_dir: Path,
    *,
    generator_id: str,
    emissions_by_bot: dict[str, set[str]],
    visited_bots: set[str],
    valid_bot_ids: set[str] | None = None,
    per_bot: bool = True,
    actor: str = "generator_runner",
    log_fn: Callable[[str], None] | None = None,
    emitted_coalesce_keys_by_bot: dict[str, set[str]] | None = None,
) -> int:
    """Archive pending/snoozed proposals owned by ``generator_id`` whose
    fingerprint was NOT re-emitted in the current cycle.

    Analogous to :func:`signals.store.sweep_resolve`: every comprehensive
    sweep-style monitor calls this at the end of a run so cleared
    conditions auto-archive instead of pinning the operator's queue.

    Decision per existing proposal owned by ``generator_id``:

      - Per-bot generator, bot is NOT in ``valid_bot_ids`` and not in
        ``visited_bots`` → archive as ``superseded`` (pod membership
        changed).
      - Per-bot generator, bot is in ``valid_bot_ids`` but we couldn't
        visit it this cycle (factory failed, ctx None) → SKIP. We have
        no signal for this bot; leaving the proposal alone is the
        safe default.
      - Otherwise (per-bot visited, or pod-wide): if the proposal is
        still emitted this cycle, skip. Else archive as
        ``resolved_externally``. "Still emitted" is fingerprint match
        OR — for a coalesced parent — *group* match (see below).

    ``emissions_by_bot`` maps ``bot_id`` (or ``""`` for pod-wide) to the
    set of proposal fingerprints emitted by the generator this cycle.
    ``visited_bots`` is the set of bot ids for which the runner built
    a context and called ``observe()`` (or ``""`` for pod-wide).
    ``valid_bot_ids`` is the pod-membership set — pass ``None`` to
    skip the membership-supersede branch (pod-wide generators).

    ``emitted_coalesce_keys_by_bot`` maps the same bucket key to the set
    of ``coalesce_key``s the generator emitted this cycle. A coalesced
    parent (one carrying a non-empty ``coalesce_key``) is preserved while
    *any* model in its group is still emitted — even if the parent's own
    per-model fingerprint went silent. Without this, a parent whose head
    model went silent (adopted into a rung, or delisted by the provider)
    while sibling models still fire would be archived for one cycle then
    re-created fresh next run — a flicker that also drops the operator's
    card-level snooze/dismiss state. Keying on the coalesce_key ties the
    card's lifetime to the whole group's. ``None`` (the default) disables
    the group-aware branch entirely — callers that don't coalesce keep the
    pure per-fingerprint behavior unchanged.

    Returns the number of proposals archived. Per-proposal errors are
    logged and the sweep continues.
    """
    # Local imports — state_machine + dedup pull schema.proposal which
    # would otherwise create an import cycle for store.
    from arbiter.dedup import compute_fingerprint
    from arbiter.state_machine import IllegalTransitionError, transition

    logger = log_fn or log.info
    archived = 0

    def _still_emitted(prop: Proposal, bucket_key: str) -> bool:
        """True if this proposal — or its coalesced group — was emitted
        this cycle. Fingerprint match keeps a normal proposal alive; group
        match keeps a coalesced parent alive while any group model fires."""
        if compute_fingerprint(prop) in emissions_by_bot.get(bucket_key, set()):
            return True
        if (
            emitted_coalesce_keys_by_bot is not None
            and prop.coalesce_key
            and prop.coalesce_key
            in emitted_coalesce_keys_by_bot.get(bucket_key, set())
        ):
            return True
        return False

    for existing in list(iter_proposals(shared_dir, subdirs=("pending", "snoozed"))):
        if existing.generator_id != generator_id:
            continue

        # Normalize the pod-wide sentinel so the key matches the runner's
        # emission bucket. For per-bot generators this is identity (real
        # bot ids pass through, "<pod>" never appears).
        key = _bucket_key(existing.bot_id)

        if per_bot and key:
            if valid_bot_ids is not None and key not in valid_bot_ids and key not in visited_bots:
                target = "superseded"
                reason = (
                    f"bot {key!r} no longer in pod membership; "
                    f"{generator_id} sweep cleanup"
                )
            elif key not in visited_bots:
                # Bot is in members but we couldn't visit it (factory
                # failure, ctx None). No signal → leave alone.
                continue
            elif _still_emitted(existing, key):
                continue  # still firing (per-model or group); preserve
            else:
                target = "resolved_externally"
                reason = (
                    f"detector silent for fingerprint "
                    f"{compute_fingerprint(existing)[:12]}…"
                )
        else:
            if _still_emitted(existing, key):
                continue
            target = "resolved_externally"
            reason = (
                f"detector silent for fingerprint "
                f"{compute_fingerprint(existing)[:12]}…"
            )

        located = find_proposal(shared_dir, existing.id)
        if located is None:
            continue
        # Re-loaded copy is fresher than the iteration snapshot — apply
        # the transition to it so a status change between the scan and
        # here is judged on current state.
        existing, _, src_subdir = located

        prev_status = existing.status
        try:
            transition(existing, target, actor=actor, reason=reason)  # type: ignore[arg-type]
        except IllegalTransitionError:
            continue
        try:
            # expected_status: a concurrent operator action (approve /
            # dismiss between our load and this move) wins over the
            # background sweep — the CAS inside move_proposal detects
            # the change and raises instead of clobbering it.
            move_proposal(
                existing, shared_dir,
                from_subdir=src_subdir,
                expected_status=prev_status,
            )
        except OSError as exc:
            logger(
                f"sweep_resolve_proposals: {generator_id}: failed to archive "
                f"{existing.id}: {exc}"
            )
            continue
        archived += 1

    return archived
