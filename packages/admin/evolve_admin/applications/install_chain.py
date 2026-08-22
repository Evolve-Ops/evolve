"""
install_chain.py — Dependency-ordered install-job chains for gallery bundles.

Audit slate S5a (docs/audit-gallery-framework-2026-07-02.md §2): installing a
package with unmet required ``app_dependencies`` (EA Pack, Project Pack — or
any app whose foundation isn't installed yet) used to hard-fail preflight and
leave the operator to hand-order 6-7 individual installs. This module turns
that into ONE chain: the dependency closure is topologically sorted and run
as a sequence of ordinary forge install jobs, foundations before behaviors.

The sequential-run pattern is the add-bot wizard's (``evo/wizard/pack_driver``:
one daemon thread, one blocking ``run_forge_job`` at a time, same
``InstallRunner`` seam) with the two policies the gallery needs layered on:

  * **Stop-on-first-failure.** A failed link marks every later link
    ``blocked`` (with ``blocked_by`` naming the failed package) instead of
    racing behaviors ahead of a dead foundation. The wizard's queue instead
    continues — its promise is per-app, ours is per-suite.
  * **Persisted, resumable state.** The chain lives at
    ``{shared_dir}/forge/chains/<chain_id>.json`` and survives restarts.
    Resume re-runs from the first non-succeeded link; links whose package is
    already installed SKIP (same ``installed_state`` check preflight uses),
    so resume can never double-install.

Per-link behavior matches a single gallery install exactly: the link runs its
own OAuth prerequisite evaluation (a gap suspends the chain resumably in
``awaiting_oauth`` — the existing sweeper resumes the job, then re-enters the
chain via the ``install_chain_id`` stamp on the job) and its own hard
preflight (non-integration build blockers fail the link).

Chain states:    running | awaiting_oauth | blocked | failed | complete
Link states:     pending | skipped | running | awaiting_oauth | succeeded |
                 failed | blocked

The status fields on the chain JSON are authoritative for what the advancer
last did; ``chain_view`` additionally reconciles link job statuses read-only
(e.g. a sweeper-abandoned OAuth job shows failed on GET without waiting for
the next advance).

Seams (tests substitute instead of patching subprocess/forge — the
pack_driver convention):

  * :func:`set_install_runner` — replaces the blocking per-job run.
  * :func:`set_force_sync` — runs the advance loop inline, no thread.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from evolve_util import atomic_write_json as _atomic_write

from .ids import new_chain_id, now_iso

log = logging.getLogger(__name__)


def _canonical_pkg_id(pkg_id: str) -> str:
    """Resolve a pkg_id through the gallery's retired→surviving alias map.

    Used to key chain identity and the per-app slot lock so a package
    reached via its retired id and via its surviving id is treated as one
    (audit S5a adversarial finding — a legacy alias otherwise defeated the
    duplicate-chain guard and the slot lock).
    """
    from .gallery import LEGACY_KEY_MIGRATION
    return LEGACY_KEY_MIGRATION.get(pkg_id, pkg_id)


class DependencyCycleError(ValueError):
    """Raised when a package's app_dependencies graph contains a cycle."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(
            "dependency cycle in gallery app_dependencies: "
            + " → ".join(cycle)
        )


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class ChainLink:
    """One package in the chain, in topological position ``order``."""

    pkg_id: str
    app_id: str
    display_name: str
    order: int
    state: str = "pending"      # pending | skipped | running | awaiting_oauth |
                                # succeeded | failed | blocked
    job_id: str | None = None
    blocked_by: str | None = None   # pkg_id of the failed link upstream
    detail: str = ""
    projected_cost_mid_usd: float | None = None
    projected_cost_high_usd: float | None = None


@dataclass
class InstallChain:
    chain_id: str               # c- prefixed
    root_pkg_id: str            # the package the operator clicked Install on
    bot_id: str
    links: list[ChainLink] = field(default_factory=list)
    status: str = "running"     # running | awaiting_oauth | blocked | failed | complete
    created_at: str = ""
    last_updated: str = ""
    operator_confirmed: bool = False


TERMINAL_CHAIN_STATES: tuple[str, ...] = ("complete",)


# ── Storage ───────────────────────────────────────────────────────────────────


def _chains_dir(shared_dir: Path) -> Path:
    return shared_dir / "forge" / "chains"


def _chain_path(chain_id: str, shared_dir: Path) -> Path:
    return _chains_dir(shared_dir) / f"{chain_id}.json"


def save_chain(chain: InstallChain, shared_dir: Path) -> None:
    chain.last_updated = now_iso()
    path = _chain_path(chain.chain_id, shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, asdict(chain))


def load_chain(chain_id: str, shared_dir: Path) -> InstallChain | None:
    path = _chain_path(chain_id, shared_dir)
    if not path.exists():
        return None
    try:
        return _chain_from_dict(json.loads(path.read_text()))
    except Exception:
        # A structurally invalid chain JSON (missing chain_id, bad shape) is
        # unreadable, not a 500 — GET detail should 404 like a missing file,
        # matching list_chains, which already skips it.
        return None


def _chain_from_dict(data: dict) -> InstallChain:
    import dataclasses as _dc

    link_fields = {f.name for f in _dc.fields(ChainLink)}
    links = [
        ChainLink(**{k: v for k, v in raw.items() if k in link_fields})
        for raw in data.pop("links", [])
        if isinstance(raw, dict)
    ]
    chain_fields = {f.name for f in _dc.fields(InstallChain)}
    cleaned = {k: v for k, v in data.items() if k in chain_fields}
    return InstallChain(links=links, **cleaned)


def list_chains(shared_dir: Path, *, bot_id: str | None = None) -> list[InstallChain]:
    """All chains, newest first, optionally filtered to one bot."""
    out: list[InstallChain] = []
    d = _chains_dir(shared_dir)
    if not d.is_dir():
        return out
    for p in d.glob("c-*.json"):
        try:
            chain = _chain_from_dict(json.loads(p.read_text()))
        except Exception:
            continue
        if bot_id and chain.bot_id != bot_id:
            continue
        out.append(chain)
    out.sort(key=lambda c: c.created_at, reverse=True)
    return out


def find_incomplete_chain(
    root_pkg_id: str, bot_id: str, shared_dir: Path,
) -> InstallChain | None:
    """The newest non-complete chain for (root package, bot), if any.

    This is the concurrent-duplicate guard: the install route reuses (and
    resumes) an existing incomplete chain instead of minting a second one
    whose links would race the first over the same (bot, app) slots.
    """
    canon = _canonical_pkg_id(root_pkg_id)
    for chain in list_chains(shared_dir, bot_id=bot_id):
        if (
            _canonical_pkg_id(chain.root_pkg_id) == canon
            and chain.status not in TERMINAL_CHAIN_STATES
        ):
            return chain
    return None


# Guards the find→create window so two concurrent install requests for the
# same (root package, bot) can't both observe "no chain" and mint twins.
_find_or_create_guard = threading.Lock()


def find_or_create_chain(
    root_pkg_id: str,
    bot_id: str,
    shared_dir: Path,
    *,
    operator_confirmed: bool = False,
    projections: Optional[dict] = None,
) -> "tuple[InstallChain, bool]":
    """Atomic duplicate guard for the install route.

    Returns ``(chain, created)`` — ``created`` False means an incomplete
    chain for this (root package, bot) already existed and was returned
    instead (the caller resumes it rather than starting a competitor).
    """
    with _find_or_create_guard:
        existing = find_incomplete_chain(root_pkg_id, bot_id, shared_dir)
        if existing is not None:
            return existing, False
        chain = create_chain(
            root_pkg_id, bot_id, shared_dir,
            operator_confirmed=operator_confirmed,
            projections=projections,
        )
        return chain, True


# ── Dependency closure (topo order) ───────────────────────────────────────────


def resolve_dependency_closure(root_pkg_id: str, shared_dir: Path) -> list[dict]:
    """Topologically-ordered required-dependency closure, root package last.

    DFS post-order over ``app_dependencies`` entries with ``required: true``
    (optional deps are runtime enhancements — installing them uninvited would
    surprise on scope and cost). Handles nested bundles (a dependency that is
    itself a meta-package) by plain recursion; each package appears once.

    Raises:
        DependencyCycleError: the graph has a required-dep cycle (loud, with
            the cycle path — a broken gallery package must not hang installs).
        ValueError: a required dependency doesn't resolve in the gallery.
    """
    from .gallery import load_gallery_package

    order: list[dict] = []
    done: set[str] = set()
    in_stack: set[str] = set()
    stack_path: list[str] = []

    def _visit(requested_id: str) -> None:
        pkg = load_gallery_package(requested_id, shared_dir)
        if pkg is None:
            raise ValueError(
                f"required dependency {requested_id!r} not found in the gallery"
            )
        # Key the closure on the CANONICAL pkg_id (the loaded package's own
        # id, which load_gallery_package resolved through LEGACY_KEY_MIGRATION).
        # Keying on the requested id would double-visit a package reached once
        # via its retired id and once via its surviving id (a diamond across
        # the alias), inflating the chain + double-counting it in the cost
        # projection (audit S5a adversarial finding).
        # identity: see resolve_app_id — GALLERY CATALOG KEY, and the whole
        # point of this line is that it must be the key ``load_gallery_package``
        # canonicalized through ``LEGACY_KEY_MIGRATION`` (see the comment
        # above). ``resolve_app_id`` would return the same string for a
        # well-formed gallery package, but it is not alias-aware, so a package
        # reached via a retired id would silently stop collapsing onto its
        # survivor — reintroducing the audit S5a diamond.
        pkg_id = str(pkg.get("pkg_id") or requested_id)
        if pkg_id in done:
            return
        if pkg_id in in_stack:
            cycle = stack_path[stack_path.index(pkg_id):] + [pkg_id]
            raise DependencyCycleError(cycle)
        in_stack.add(pkg_id)
        stack_path.append(pkg_id)
        for dep in pkg.get("app_dependencies", []) or []:
            # identity: see resolve_app_id — catalog key: ``_visit`` resolves it
            # through ``load_gallery_package``, which is ``pkg_id``-indexed.
            dep_id = str(dep.get("pkg_id") or "")
            if dep_id and dep.get("required", True):
                _visit(dep_id)
        stack_path.pop()
        in_stack.discard(pkg_id)
        done.add(pkg_id)
        order.append(pkg)

    _visit(root_pkg_id)
    return order


def _app_id_for(pkg: dict) -> str:
    # Same slug derivation as the gallery install endpoint and pack_driver —
    # the slug is the manifest filename and the load_manifest key.
    # identity: see resolve_app_id — NOT applicable: this DERIVES the manifest
    # stem (``app_id``) from the package's display name, falling back to the
    # catalog key only when the package has no name. The resolver returns the
    # catalog key unconditionally, which would make every chained install land
    # its manifest at ``p-<hash>.json`` instead of the slug ``load_manifest``
    # and every other reader expects (AL-1.4 §6 delta 12).
    raw_name = str(pkg.get("name") or pkg.get("pkg_id") or "")
    return raw_name.lower().replace(" ", "-").replace("_", "-")


# ── Chain creation ────────────────────────────────────────────────────────────


def create_chain(
    root_pkg_id: str,
    bot_id: str,
    shared_dir: Path,
    *,
    operator_confirmed: bool = False,
    projections: Optional[dict] = None,
) -> InstallChain:
    """Build and persist a chain for the root package's dependency closure.

    Links whose package is already installed on the bot are born ``skipped``
    (they are re-checked at advance time anyway — states can change between
    creation and a much-later resume).

    ``projections`` maps pkg_id → ``{"mid_usd": float, "high_usd": float}``;
    stamped per link and onto each link's job at creation so cost_events and
    the post-completion reconciler see per-app baselines, not just the root's.
    """
    from .gallery import installed_state

    closure = resolve_dependency_closure(root_pkg_id, shared_dir)
    # Store the canonical root so a retired-id install and a surviving-id
    # install of the same package collapse onto one chain (find_incomplete_chain).
    # identity: see resolve_app_id — catalog key, alias-canonicalized as above.
    canonical_root = str(closure[-1].get("pkg_id")) if closure else _canonical_pkg_id(root_pkg_id)
    ts = now_iso()
    chain = InstallChain(
        chain_id=new_chain_id(),
        root_pkg_id=canonical_root,
        bot_id=bot_id,
        created_at=ts,
        last_updated=ts,
        operator_confirmed=operator_confirmed,
    )
    for i, pkg in enumerate(closure):
        # identity: see resolve_app_id — catalog key. Note the ``ChainLink``
        # built just below carries BOTH ``pkg_id`` and ``app_id`` (derived by
        # ``_app_id_for``) precisely because they are different strings; every
        # ``link.pkg_id`` read further down this module is this catalog key,
        # feeding ``load_gallery_package`` / ``installed_state`` /
        # ``preflight_check``, all of which are ``pkg_id``-indexed.
        pkg_id = str(pkg.get("pkg_id") or "")
        proj = (projections or {}).get(pkg_id) or {}
        link = ChainLink(
            pkg_id=pkg_id,
            app_id=_app_id_for(pkg),
            display_name=str(pkg.get("display_name") or pkg.get("name") or pkg_id),
            order=i,
            projected_cost_mid_usd=proj.get("mid_usd"),
            projected_cost_high_usd=proj.get("high_usd"),
        )
        if installed_state(pkg_id, bot_id, shared_dir) == "installed":
            link.state = "skipped"
            link.detail = "already installed"
        chain.links.append(link)
    save_chain(chain, shared_dir)
    return chain


# ── Seams (pack_driver convention) ────────────────────────────────────────────


InstallRunner = Callable[[Path, str, str], None]  # (shared_dir, job_id, bot_id)
_install_runner: InstallRunner | None = None


def set_install_runner(fn: InstallRunner | None) -> None:
    global _install_runner
    _install_runner = fn


_force_sync = False


def set_force_sync(value: bool) -> None:
    global _force_sync
    _force_sync = value


def _default_install_runner(shared_dir: Path, job_id: str, bot_id: str) -> None:
    from . import forge_engine as _fe

    _fe.run_forge_job(job_id=job_id, shared_dir=shared_dir, bot_id=bot_id)


# ── Advance (the sequential worker) ───────────────────────────────────────────


# Per-chain advance locks. The admin server is a single process (Flask +
# daemon threads: sweeper, dispatch threads), so in-process locking is the
# whole concurrency story. Non-blocking acquire — a second advance while one
# is running returns immediately rather than queueing double work.
_advance_locks: dict[str, threading.Lock] = {}
_advance_locks_guard = threading.Lock()


def _lock_for(chain_id: str) -> threading.Lock:
    with _advance_locks_guard:
        return _advance_locks.setdefault(chain_id, threading.Lock())


# Per-(bot, canonical-pkg) app-slot locks. The per-CHAIN lock above serializes
# advances of ONE chain; it does NOT stop two DIFFERENT chains (e.g. EA Pack +
# Project Pack on the same bot, sharing Task Manager) from both reaching the
# same dependency and forge-building it twice over the same (bot, app) slot.
# The slot lock closes that: a link holds it across its installed-state check
# and the blocking build, so a second chain's same-slot link blocks until the
# first finishes, then sees "installed" and skips. Canonical pkg id so a
# retired alias and its surviving id map to one slot (audit S5a adversarial
# finding). Blocking acquire (unlike the chain lock) — the waiter should
# continue automatically once the shared dep lands, not park for a manual
# resume.
_slot_locks: dict[tuple[str, str], threading.Lock] = {}
_slot_locks_guard = threading.Lock()


def _slot_lock(bot_id: str, pkg_id: str) -> threading.Lock:
    key = (bot_id, _canonical_pkg_id(pkg_id))
    with _slot_locks_guard:
        return _slot_locks.setdefault(key, threading.Lock())


def _mark_blocked_after(chain: InstallChain, failed_link: ChainLink) -> None:
    for later in chain.links:
        if later.order > failed_link.order and later.state in ("pending", "blocked"):
            later.state = "blocked"
            later.blocked_by = failed_link.pkg_id  # identity: see resolve_app_id — catalog key, distinct from app_id
            later.detail = f"blocked by failed install of {failed_link.display_name}"


def _create_link_job(chain: InstallChain, link: ChainLink, shared_dir: Path):
    """Create the forge install job for a link and stamp chain membership.

    The ``install_chain_id`` stamp is what routes the job back into the chain
    when the OAuth sweeper resumes it (see oauth/sweeper._resume_job).
    """
    from .forge_jobs import create_install_job, save_job
    from .gallery import load_gallery_package

    pkg = load_gallery_package(link.pkg_id, shared_dir)  # identity: see resolve_app_id — catalog key, distinct from app_id
    if pkg is None:
        raise ValueError(f"package {link.pkg_id!r} disappeared from the gallery")  # identity: see resolve_app_id — catalog key, distinct from app_id
    job = create_install_job(
        pkg_id=link.pkg_id,  # identity: see resolve_app_id — catalog key, distinct from app_id
        app_id=link.app_id,
        bot_id=chain.bot_id,
        gallery_version=str(pkg.get("pkg_version") or ""),
        shared_dir=shared_dir,
        operator_confirmed=chain.operator_confirmed,
        projected_cost_mid_usd=link.projected_cost_mid_usd,
        projected_cost_high_usd=link.projected_cost_high_usd,
    )
    build_spec = pkg.get("build_spec", "")
    if build_spec:
        job.context_snapshot["build_spec"] = build_spec
    job.context_snapshot["install_chain_id"] = chain.chain_id
    save_job(job, shared_dir)
    return job


def advance_chain(
    chain_id: str,
    shared_dir: Path,
    *,
    retry_failed: bool = False,
) -> InstallChain | None:
    """Run the chain forward from the first non-succeeded link.

    Idempotent: succeeded/skipped links are never revisited; a link whose
    package is already installed (by this chain's own earlier attempt, a
    single install, or another chain) SKIPs; a link whose job is already
    complete is marked succeeded without re-running.

    ``retry_failed`` is the resume-after-failure policy switch: True (the
    operator's resume) clones a failed link's job for retry
    (``clone_for_retry`` — fresh job_id, ``is_retry`` wipes stale build
    files); False (automatic re-entry, e.g. the OAuth sweeper) stops at a
    failed link so nothing retries without an operator's intent.

    Returns the chain as persisted when the advancer released it, or None if
    the chain doesn't exist. If another thread is mid-advance, returns the
    current on-disk state without touching anything.
    """
    lock = _lock_for(chain_id)
    if not lock.acquire(blocking=False):
        return load_chain(chain_id, shared_dir)
    try:
        return _advance_locked(chain_id, shared_dir, retry_failed=retry_failed)
    finally:
        lock.release()


def _advance_locked(
    chain_id: str, shared_dir: Path, *, retry_failed: bool,
) -> InstallChain | None:
    chain = load_chain(chain_id, shared_dir)
    if chain is None:
        return None

    runner = _install_runner or _default_install_runner

    # Resume intent: un-block links a prior failure blocked so they are
    # eligible again once the failed link retries clean.
    if retry_failed:
        for link in chain.links:
            if link.state == "blocked":
                link.state = "pending"
                link.blocked_by = None
                link.detail = ""

    chain.status = "running"
    save_chain(chain, shared_dir)

    for link in sorted(chain.links, key=lambda lk: lk.order):
        if link.state in ("succeeded", "skipped"):
            continue

        # A link that failed without a job (preflight-failed, package gone)
        # only retries on an explicit resume — automatic re-entry (the OAuth
        # sweeper) must never retry a failure the operator hasn't seen.
        # Job-backed failures get the same policy in the reconcile below.
        if link.state == "failed" and not retry_failed and not link.job_id:
            chain.status = "failed"
            _mark_blocked_after(chain, link)
            save_chain(chain, shared_dir)
            return chain

        # Hold the app-slot lock across the installed-state check AND the
        # blocking build so a different chain's link for the same (bot, pkg)
        # cannot race a duplicate build: it blocks here, then re-checks under
        # the lock and skips the now-installed app.
        with _slot_lock(chain.bot_id, link.pkg_id):  # identity: see resolve_app_id — catalog key, distinct from app_id
            signal = _process_link(
                chain, link, shared_dir,
                retry_failed=retry_failed, runner=runner,
            )
        if signal == "stop":
            return chain
        # signal == "continue" → advance to the next link

    # Every link succeeded or skipped.
    chain.status = "complete"
    save_chain(chain, shared_dir)
    return chain


def _process_link(
    chain: InstallChain,
    link: ChainLink,
    shared_dir: Path,
    *,
    retry_failed: bool,
    runner: "InstallRunner",
) -> str:
    """Process one link under its app-slot lock.

    Returns ``"continue"`` (link resolved to succeeded/skipped; advance to the
    next) or ``"stop"`` (chain reached a terminal or suspended state — the
    caller returns the chain). Mutates + persists ``chain`` as it goes.
    """
    from .forge_jobs import TERMINAL_JOB_STATES, clone_for_retry, load_job
    from .gallery import installed_state, load_gallery_package, preflight_check
    from . import oauth_orchestrator as _oauth

    # ── Reconcile from the link's existing job, if any ───────────────────────
    job = load_job(link.job_id, shared_dir) if link.job_id else None
    just_cloned = False
    if job is not None:
        if job.status == "complete":
            link.state = "succeeded"
            link.detail = ""
            save_chain(chain, shared_dir)
            return "continue"
        if job.status == "awaiting_oauth":
            link.state = "awaiting_oauth"
            chain.status = "awaiting_oauth"
            save_chain(chain, shared_dir)
            return "stop"
        if job.status in ("running", "awaiting_approval", "approved"):
            # In flight on another thread (dispatch/sweeper). Never
            # double-run — leave the chain running; the next advance
            # (sweeper re-entry or resume) reconciles the outcome.
            link.state = "running"
            link.detail = f"job {job.job_id} in flight"
            save_chain(chain, shared_dir)
            return "stop"
        if job.status in TERMINAL_JOB_STATES:  # failed / cancelled / rejected
            if not retry_failed:
                link.state = "failed"
                link.detail = f"job {job.job_id} {job.status}"
                chain.status = "failed"
                _mark_blocked_after(chain, link)
                save_chain(chain, shared_dir)
                return "stop"
            job = clone_for_retry(
                job, shared_dir,
                superseded_reason="superseded by chain resume",
            )
            job.context_snapshot["install_chain_id"] = chain.chain_id
            from .forge_jobs import save_job as _save_job
            _save_job(job, shared_dir)
            link.job_id = job.job_id
            just_cloned = True
            save_chain(chain, shared_dir)
        # job.status == "queued" falls through: run it below.

    # ── Already-installed skip (the preflight check, shared) ─────────────────
    state = installed_state(link.pkg_id, chain.bot_id, shared_dir)  # identity: see resolve_app_id — catalog key, distinct from app_id
    if state == "installed":
        link.state = "skipped"
        link.detail = "already installed"
        save_chain(chain, shared_dir)
        return "continue"
    if state == "installing" and job is None:
        # A job outside this chain (single install, another chain not holding
        # this slot lock) holds the (bot, app) slot. Don't race it — park the
        # chain; resume after it finishes skips (installed) or proceeds.
        link.detail = (
            f"{link.display_name} is being installed outside this chain; "
            "resume after that install finishes"
        )
        chain.status = "blocked"
        save_chain(chain, shared_dir)
        return "stop"

    pkg = load_gallery_package(link.pkg_id, shared_dir)  # identity: see resolve_app_id — catalog key, distinct from app_id
    if pkg is None:
        link.state = "failed"
        link.detail = "package not found in the gallery"
        chain.status = "failed"
        _mark_blocked_after(chain, link)
        save_chain(chain, shared_dir)
        return "stop"

    # ── Per-link OAuth prerequisites (same path as a single install) ─────────
    # A sweeper-resumed queued job skips the gates (its OAuth check ran
    # seconds ago); a retry CLONE must NOT — the failure being retried
    # may itself be oauth_abandoned, and dispatching without the re-check
    # would build the app with its integration still missing.
    if job is None or job.status != "queued" or just_cloned:
        requirements = pkg.get("requirements", {})
        try:
            prereq = _oauth.evaluate_install_prerequisites(
                chain.bot_id, requirements, shared_dir=shared_dir,
            )
        except Exception as exc:
            log.warning(
                "install_chain %s: prereq check failed for %s: %s",
                chain.chain_id, link.pkg_id, exc,  # identity: see resolve_app_id — catalog key, distinct from app_id
            )
            prereq = {"satisfied": True, "missing": []}
        if not prereq.get("satisfied", True):
            if job is None:
                job = _create_link_job(chain, link, shared_dir)
                link.job_id = job.job_id
            _oauth.start_oauth_wait(
                job.job_id, prereq.get("missing", []), shared_dir,
                requirements=requirements,
            )
            link.state = "awaiting_oauth"
            chain.status = "awaiting_oauth"
            save_chain(chain, shared_dir)
            return "stop"

        # ── Per-link hard preflight (non-OAuth build blockers) ───────────────
        pf = preflight_check(link.pkg_id, chain.bot_id, shared_dir)  # identity: see resolve_app_id — catalog key, distinct from app_id
        hard_blockers = [
            item for item in (
                pf.get("app_dependencies", []) + pf.get("requirements", [])
            )
            if item.get("severity") == "build_blocker"
            and item.get("state") != "satisfied"
            and item.get("type") != "integration"
        ]
        if hard_blockers:
            link.state = "failed"
            link.detail = "preflight failed — " + "; ".join(
                f"{b.get('display_name') or b.get('id', '?')}: {b.get('message', '')}"
                for b in hard_blockers
            )
            chain.status = "failed"
            _mark_blocked_after(chain, link)
            save_chain(chain, shared_dir)
            return "stop"

    # ── Run the link's job to completion (wizard pattern: blocking) ──────────
    if job is None:
        job = _create_link_job(chain, link, shared_dir)
        link.job_id = job.job_id
    link.state = "running"
    link.detail = ""
    save_chain(chain, shared_dir)

    run_error = ""
    try:
        runner(shared_dir, job.job_id, chain.bot_id)
    except Exception as exc:
        run_error = str(exc)
        log.error(
            "install_chain %s: link %s (%s) run failed: %s",
            chain.chain_id, link.pkg_id, link.display_name, exc,  # identity: see resolve_app_id — catalog key, distinct from app_id
        )

    finished = load_job(job.job_id, shared_dir)
    final_status = str(getattr(finished, "status", "") or "unknown")
    if final_status == "complete":
        link.state = "succeeded"
        link.detail = ""
        save_chain(chain, shared_dir)
        return "continue"
    if final_status == "awaiting_oauth":
        # The run itself can park on OAuth in future flows; treat it the
        # same as a pre-run detection — suspend resumably.
        link.state = "awaiting_oauth"
        chain.status = "awaiting_oauth"
        save_chain(chain, shared_dir)
        return "stop"

    link.state = "failed"
    link.detail = run_error or f"job {job.job_id} finished {final_status}"
    chain.status = "failed"
    _mark_blocked_after(chain, link)
    save_chain(chain, shared_dir)
    return "stop"


def advance_chain_async(
    chain_id: str,
    shared_dir: Path,
    *,
    retry_failed: bool = False,
) -> None:
    """Advance in a daemon thread (or inline under the force_sync seam)."""
    if _force_sync:
        advance_chain(chain_id, shared_dir, retry_failed=retry_failed)
        return

    def _run() -> None:
        try:
            advance_chain(chain_id, shared_dir, retry_failed=retry_failed)
        except Exception:
            log.exception("install_chain: advance thread failed for %s", chain_id)

    threading.Thread(
        target=_run, daemon=True, name=f"install-chain-{chain_id}",
    ).start()


def resume_chain(chain_id: str, shared_dir: Path) -> InstallChain | None:
    """Operator resume: re-run from the first non-succeeded link, retrying a
    failed link via a fresh retry-clone job. Blocking; see
    ``advance_chain_async(retry_failed=True)`` for the route-facing form."""
    return advance_chain(chain_id, shared_dir, retry_failed=True)


# ── Read-side view (no writes) ────────────────────────────────────────────────


def chain_view(chain: InstallChain, shared_dir: Path) -> dict:
    """UI-ready dict with link states reconciled against live job statuses.

    GET must stay write-free (the advancer owns writes, under its lock), but
    persisted link state can trail reality — e.g. the sweeper abandons an
    awaiting_oauth job at 30 min, or a job completes between advances. The
    view layers the job's terminal truth over the stored link state.
    """
    from .forge_jobs import load_job

    links: list[dict] = []
    effective_chain_status = chain.status
    for link in sorted(chain.links, key=lambda lk: lk.order):
        d = asdict(link)
        if link.job_id and link.state in ("running", "awaiting_oauth", "pending"):
            job = load_job(link.job_id, shared_dir)
            job_status = str(getattr(job, "status", "") or "")
            if job_status == "complete":
                d["state"] = "succeeded"
            elif job_status in ("failed", "cancelled", "rejected"):
                d["state"] = "failed"
                d["detail"] = d.get("detail") or f"job {link.job_id} {job_status}"
                if effective_chain_status in ("running", "awaiting_oauth"):
                    effective_chain_status = "failed"
            d["job_status"] = job_status or None
        links.append(d)

    done = sum(1 for d in links if d["state"] in ("succeeded", "skipped"))
    return {
        "chain_id": chain.chain_id,
        "root_pkg_id": chain.root_pkg_id,
        "bot_id": chain.bot_id,
        "status": effective_chain_status,
        "created_at": chain.created_at,
        "last_updated": chain.last_updated,
        "links": links,
        "links_total": len(links),
        "links_done": done,
    }
