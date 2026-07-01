"""Sweep pod-wide per-bot derived state on retire / delete.

``retire_bot`` archives the bot's ``.openclaw/`` + metrics + manifest, stops
its services, and removes it from ``network.json`` — but many OTHER pod-wide
stores under ``{shared_dir}`` are keyed by ``bot_id`` and would otherwise be
left orphaned. A bot named ``nova`` retired on 2026-06-10 stayed visible in the
admin UI (Alerts + Improvements) afterwards because its Signals kept firing and
its onboarding recommendations were never dropped; a sweep of ``{shared_dir}``
found leftovers in 11+ distinct stores. This module makes the next retirement
clean automatically.

Single source of truth is :data:`ORPHAN_STORES` — a new per-bot store added by
some future monitor/feature is a one-line addition here, and the guard test in
``tests/test_retire_orphans.py`` fails if a known store pattern stops being
covered.

Every action is **best-effort**: a failure on any store is logged via
``result.log(...)`` and never raised, so derived-state cleanup can never block a
retire. ``dry_run`` is honoured throughout (each action logs "would …" and
touches nothing).

Disposition per store:

* **Signals** — *resolved*, not deleted. Sweep-style producers (audit,
  content_scan, pod_health) already auto-resolve their own Signals on the next
  run via ``signals.store.sweep_resolve``; the non-sweep producers
  (install_integrity_monitor, session_economics, plugin/mcp/hook monitors) fire
  forever. We transition every active (firing | snoozed) Signal with
  ``bot_id == <bot>`` to ``resolved`` so the resolved record is kept for the
  History tab.
* **Recommendations** — recs whose ``scope_id`` / ``action_args.bot_id`` /
  ``bot:<bot>`` tag target the retired bot are dropped from
  ``better-engine/recommendations.json``.
* **Inventories / status / locks / snapshots** — inert per-bot files and dirs;
  deleted outright.
* **Per-bot namespace dir** (``{shared_dir}/<bot>/``) — holds cost-cascade /
  spans / turns state written by the *bot's own* process, so its leaf files are
  owned by the bot's macOS UID (mode 600), not ``evolve``. A plain
  ``shutil.rmtree`` from the admin (evolve) process hits ``EACCES``; we retry
  via ``sudo /bin/rm -rf`` — the same root/sudo path service-stop and
  account-delete use (see :func:`_sudo_rm_rf`).
* **annotations / metrics dirs** — their content is archived (metrics) or
  intentionally left to time-bounded retention; we only remove the now-empty
  shell, never delete live content here.

Out of scope (left alone): ``incidents/<date>/<bot>-*.json`` and ``logs/`` —
time-bounded historical records that auto-prune; rewriting append-only logs is
not this sweep's job.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a hard import cycle with retire.py at runtime
    from .retire import RetireResult


# ── Store kinds ──────────────────────────────────────────────────────────────

RESOLVE_SIGNALS = "resolve_signals"
DROP_RECS = "drop_recommendations"
DELETE_FILE = "delete_file"
DELETE_DIR = "delete_dir"
DELETE_DIR_BOT_OWNED = "delete_dir_bot_owned"  # may hold bot-owned files → sudo
REMOVE_DIR_IF_EMPTY = "remove_dir_if_empty"


@dataclass(frozen=True)
class OrphanStore:
    """One pod-wide store keyed by ``bot_id`` that retire must clean up.

    ``rel`` is the path relative to ``{shared_dir}`` with ``{bot}`` standing
    in for the bot id; it is ``None`` for the two stores that aren't simple
    path deletes (signals, recommendations).
    """

    name: str
    kind: str
    rel: str | None = None


# ── Registry — the single source of truth ────────────────────────────────────
#
# Keep this list aligned with the per-bot stores other modules write. The
# empirical list below was recovered by sweeping {shared_dir} after the nova
# retirement (2026-06-10); each entry cites the writer so the next person can
# trace it. test_retire_orphans.py::test_registry_covers_known_stores fails if
# a known pattern drops off this list.
ORPHAN_STORES: tuple[OrphanStore, ...] = (
    # Active observations — resolved, not deleted (keep the resolved record).
    OrphanStore("signals", RESOLVE_SIGNALS),
    # Onboarding / app-quality recs targeting the bot (better_engine).
    OrphanStore("recommendations", DROP_RECS),
    # Capability inventories (plugins/mcp/hooks/permissions monitors).
    OrphanStore("plugins-inventory", DELETE_FILE, "plugins/inventory/{bot}.json"),
    OrphanStore("mcp-inventory", DELETE_FILE, "mcp/inventory/{bot}.json"),
    OrphanStore("hooks-inventory", DELETE_FILE, "hooks/inventory/{bot}.json"),
    OrphanStore("permissions-inventory", DELETE_FILE, "permissions/inventory/{bot}.json"),
    # Per-bot status snapshot.
    OrphanStore("status", DELETE_FILE, "status/{bot}.json"),
    # Cost-watchdog per-bot config snapshots dir.
    OrphanStore("cost-watchdog-snapshots", DELETE_DIR, "cost_watchdog/config_snapshots/{bot}"),
    # Content-scan per-bot results dir.
    OrphanStore("content-scan-results", DELETE_DIR, "content-scan/results/{bot}"),
    # Backup SSH pubkey.
    OrphanStore("pubkey", DELETE_FILE, "pubkeys/{bot}.pub"),
    # Audit state marker (security/last-oc-audit-<bot>).
    OrphanStore("last-oc-audit", DELETE_FILE, "security/last-oc-audit-{bot}"),
    # Applier lock (deploy.APPLY_LOCK_TEMPLATE).
    OrphanStore("apply-lock", DELETE_FILE, "apply_processed-{bot}.lock"),
    # {shared_dir}/<bot>/ — cost-cascade / spans / turns state. Leaf files are
    # written by the bot's own process (bot-owned, mode 600) → privileged rm.
    OrphanStore("per-bot-namespace", DELETE_DIR_BOT_OWNED, "{bot}"),
    # Now-empty shells left after archive / retention handles their content.
    OrphanStore("annotations", REMOVE_DIR_IF_EMPTY, "annotations/{bot}"),
    OrphanStore("metrics", REMOVE_DIR_IF_EMPTY, "metrics/{bot}"),
)


def per_bot_store_paths(shared_dir: Path | str, bot_id: str) -> list[Path]:
    """Concrete ``{shared_dir}`` paths this sweep targets (path-keyed stores).

    Excludes the signals + recommendations stores (which aren't simple path
    deletes). Used by the guard test and by callers that want to assert the
    sweep left nothing behind.
    """
    sd = Path(shared_dir)
    out: list[Path] = []
    for store in ORPHAN_STORES:
        if store.rel is None:
            continue
        out.append(sd / store.rel.format(bot=bot_id))
    return out


# ── Entry point ──────────────────────────────────────────────────────────────


def sweep_orphan_state(
    shared_dir: Path | str,
    bot_id: str,
    *,
    dry_run: bool,
    result: "RetireResult",
) -> None:
    """Resolve / drop / delete every pod-wide per-bot store for ``bot_id``.

    Best-effort and non-fatal: each store is wrapped so a failure is logged on
    ``result`` and the sweep moves on. Mirrors the ``clear_template_installs``
    step it sits next to in ``retire_bot``.
    """
    sd = Path(shared_dir)
    for store in ORPHAN_STORES:
        try:
            if store.kind == RESOLVE_SIGNALS:
                _resolve_bot_signals(sd, bot_id, dry_run=dry_run, result=result)
            elif store.kind == DROP_RECS:
                _drop_bot_recommendations(sd, bot_id, dry_run=dry_run, result=result)
            else:
                assert store.rel is not None  # every path-kind has a template
                path = sd / store.rel.format(bot=bot_id)
                if store.kind == DELETE_FILE:
                    _delete_file(path, store, dry_run, result)
                elif store.kind == DELETE_DIR:
                    _delete_dir(path, store, dry_run, result, bot_owned=False)
                elif store.kind == DELETE_DIR_BOT_OWNED:
                    _delete_dir(path, store, dry_run, result, bot_owned=True)
                elif store.kind == REMOVE_DIR_IF_EMPTY:
                    _remove_dir_if_empty(path, store, dry_run, result)
                else:  # pragma: no cover — registry/code drift
                    result.log(
                        f"[warn] orphan-sweep: unknown kind {store.kind!r} "
                        f"for store {store.name!r}"
                    )
        except Exception as exc:  # never let cleanup block the retire
            result.log(
                f"[warn] orphan-sweep {store.name} for {bot_id} "
                f"failed (non-fatal): {exc}"
            )


# ── Path-keyed store actions ─────────────────────────────────────────────────


def _delete_file(
    path: Path, store: OrphanStore, dry_run: bool, result: "RetireResult",
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if dry_run:
        result.log(f"[dry-run] would delete {store.name}: {path}")
        return
    try:
        # missing_ok handles the race where the file vanishes between the
        # exists() guard above and this unlink — no need to swallow it.
        path.unlink(missing_ok=True)
        result.log(f"swept orphan {store.name}: {path}")
    except OSError as e:
        result.log(f"[warn] could not delete {store.name} {path}: {e}")


def _delete_dir(
    path: Path,
    store: OrphanStore,
    dry_run: bool,
    result: "RetireResult",
    *,
    bot_owned: bool,
) -> None:
    if not path.exists():
        return
    if dry_run:
        result.log(f"[dry-run] would delete {store.name} dir: {path}")
        return
    try:
        shutil.rmtree(path)
        result.log(f"swept orphan {store.name} dir: {path}")
        return
    except FileNotFoundError:
        return
    except (PermissionError, OSError) as e:
        if not bot_owned:
            result.log(f"[warn] could not delete {store.name} dir {path}: {e}")
            return
        # Bot-owned content (e.g. {bot}/cascade/tier1_active.json, mode 600,
        # owned by the bot's macOS UID) — evolve can't unlink it. Retry as root
        # via the same sudo path stop-services / account-delete use. Works
        # under `sudo evolve-admin retire-bot` (CLI runs as root) and, in the
        # admin-daemon path, via the §9d `rm -rf {shared_dir}/*` sudoers grant.
        if _sudo_rm_rf(path) and not path.exists():
            result.log(f"swept orphan {store.name} dir (via sudo): {path}")
        else:
            result.log(
                f"[warn] could not delete {store.name} dir {path} "
                f"even via sudo (direct error: {e})"
            )


def _remove_dir_if_empty(
    path: Path, store: OrphanStore, dry_run: bool, result: "RetireResult",
) -> None:
    if not path.is_dir():
        return
    try:
        entries = list(path.iterdir())
    except OSError as e:
        result.log(f"[warn] could not scan {store.name} dir {path}: {e}")
        return
    if entries:
        # Live content — its archival / retention is handled elsewhere; only
        # the empty shell is ours to remove.
        result.log(
            f"left {store.name} dir in place ({len(entries)} item(s) remain): {path}"
        )
        return
    if dry_run:
        result.log(f"[dry-run] would remove empty {store.name} dir: {path}")
        return
    try:
        path.rmdir()
        result.log(f"removed empty {store.name} dir: {path}")
    except OSError as e:
        result.log(f"[warn] could not rmdir {store.name} {path}: {e}")


def _sudo_rm_rf(path: Path) -> bool:
    """``sudo /bin/rm -rf <path>`` — returns True on rc 0. Best-effort."""
    try:
        r = subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── Signals ──────────────────────────────────────────────────────────────────


def _signals_store() -> Any | None:
    """Lazy-import ``signals.store`` from the evolve-analyzer package.

    Returns ``None`` if the package isn't importable (mirrors the
    ``importlib.import_module("signals.store")`` pattern used across the admin
    package — signals lives in a separately-installed package).
    """
    try:
        return importlib.import_module("signals.store")
    except Exception:
        return None


def _resolve_bot_signals(
    shared_dir: Path, bot_id: str, *, dry_run: bool, result: "RetireResult",
) -> None:
    store = _signals_store()
    if store is None:
        result.log("[warn] signals package unavailable — skipping signal resolve")
        return
    try:
        active = [
            s for s in store.iter_signals(shared_dir, subdirs=("firing", "snoozed"))
            if getattr(s, "bot_id", None) == bot_id
        ]
    except Exception as e:
        result.log(f"[warn] could not scan signals for {bot_id}: {e}")
        return
    if not active:
        return
    if dry_run:
        result.log(
            f"[dry-run] would resolve {len(active)} active signal(s) for {bot_id}"
        )
        return
    resolved = 0
    for sig in active:
        try:
            store.apply_transition(
                sig, "resolved", shared_dir,
                actor="retire", reason=f"bot {bot_id} retired",
            )
            resolved += 1
        except Exception as e:
            result.log(
                f"[warn] could not resolve signal {getattr(sig, 'id', '?')}: {e}"
            )
    if resolved:
        result.log(f"resolved {resolved} active signal(s) for {bot_id}")


# ── Recommendations ──────────────────────────────────────────────────────────


def _rec_targets_bot(rec: Any, bot_id: str) -> bool:
    """True if ``rec`` is scoped to / acts on / tagged for ``bot_id``."""
    if getattr(rec, "scope_id", None) == bot_id:
        return True
    args = getattr(rec, "action_args", None)
    if isinstance(args, dict) and args.get("bot_id") == bot_id:
        return True
    tags = getattr(rec, "tags", None)
    if isinstance(tags, (list, tuple)) and f"bot:{bot_id}" in tags:
        return True
    return False


def _drop_bot_recommendations(
    shared_dir: Path, bot_id: str, *, dry_run: bool, result: "RetireResult",
) -> None:
    recs_path = shared_dir / "better-engine" / "recommendations.json"
    if not recs_path.exists():
        return
    try:
        from .better_engine.storage import (
            load_recommendations,
            save_recommendations,
        )
    except Exception as e:
        result.log(
            f"[warn] better_engine storage unavailable — skipping rec sweep: {e}"
        )
        return
    try:
        recs = load_recommendations(recs_path)
    except Exception as e:
        result.log(f"[warn] could not read recommendations.json: {e}")
        return
    kept = [r for r in recs if not _rec_targets_bot(r, bot_id)]
    dropped = len(recs) - len(kept)
    if dropped == 0:
        return
    if dry_run:
        result.log(
            f"[dry-run] would drop {dropped} recommendation(s) targeting {bot_id}"
        )
        return
    try:
        # save_recommendations writes the bare list (dropping the optional
        # source_freshness wrapper); the 15-min better_engine_refresh
        # repopulates that metadata on its next pass.
        save_recommendations(kept, recs_path)
        result.log(f"dropped {dropped} recommendation(s) targeting {bot_id}")
    except Exception as e:
        result.log(f"[warn] could not rewrite recommendations.json: {e}")
