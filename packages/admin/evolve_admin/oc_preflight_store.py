"""oc_preflight_store.py — persistence, audit trail, and per-pair cache for
the OpenClaw upgrade preflight (``oc_preflight.py``).

hold-fix-4277-preflight-fails-closed-and-is-wired-to-the-upgrade, items 2
and 4 (internal/dispatch/reviews/pr-4277.md):

* item 4 — every run gets an audit record (actor, timestamp, from → to),
  persisted under the shared dir alongside the health ledger, never just
  held in memory.
* item 2 — the report is cached per exact from → to pair, so the OC card's
  Update control can gate on "has a passing preflight run for THIS pair"
  without re-fetching the target and re-running nine bots' worth of
  ``doctor``/``config validate`` on every page load.

New module rather than growing ``oc_preflight.py`` further or touching
``deploy.py``/``cli.py`` (both already at the size ceiling — see CLAUDE.md
guardrails on this chip).

Mirrors ``safe_upgrade.py``'s background-thread + atomic-write-json +
retention-sweep shape — the same pattern already proven for the sibling
"safe-upgrade" preflight system this sits next to.

hold-fix-4392-the-preflight-gate-accepts-a-report-of-any-age
(internal/dispatch/reviews/pr-4392.md finding 1): the pair cache above had
no age and no bot-set, so a green run from last week (or from before a bot
existed) satisfied the gate forever. ``pair_freshness_reason`` is the fix —
a pure evaluator the two gate call sites (the apply endpoint's 409, the
UI's gate box) both run against the SAME loaded entry, so they can never
disagree about what counts as fresh.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json as _atomic_write_json

from .config import DEFAULT_SHARED_DIR
from .oc_preflight import PreflightReport, preflight

REPORTS_SUBDIR = "oc_preflight/reports"
RETAIN_REPORTS = 20
_PAIR_PREFIX = "pair-"

# 24h is generous for a pod that redeploys daily (D-CS7 review note on
# #4392) — long enough that an operator checking, then applying a minute
# later, is never bothered by it, short enough that a report genuinely
# cannot describe a pod that has had a full day to change under it.
FRESHNESS_WINDOW_H = 24.0


def reports_dir(shared_dir: Path | None = None) -> Path:
    base = shared_dir or DEFAULT_SHARED_DIR
    return base / REPORTS_SUBDIR


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_report_id() -> str:
    return f"{_utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def pair_key(installed_version: str | None, target_version: str) -> str:
    """The cache key for one from → to pair — every character of both sides
    matters (a patch bump is a different pair), so this is a literal join,
    never a normalized/truncated one."""
    return f"{installed_version or 'unknown'}__{target_version}"


def _retention_sweep(root: Path, keep: int | None = None) -> None:
    # `keep` is resolved from the module global INSIDE the call, not as a
    # default-argument value — a default is bound once at def time, so a
    # test (or an operator) that changes RETAIN_REPORTS after import would
    # silently have no effect on the swept count.
    if keep is None:
        keep = RETAIN_REPORTS
    try:
        files = sorted(
            (p for p in root.glob("*.json") if not p.name.startswith(_PAIR_PREFIX)),
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def persist_report(
    report: PreflightReport,
    *,
    actor: str,
    installed_version: str | None,
    shared_dir: Path | None = None,
) -> str:
    """Stamp the audit fields and write the report + its per-pair cache entry.

    Two files, same content: ``<report_id>.json`` (the audit trail — one
    entry per run, retention-swept) and ``pair-<from>__<to>.json`` (the
    cache the Update control reads — always the most recent run for that
    exact pair, overwritten each time). Returns the report id.
    """
    report.actor = actor
    report.checked_at = _iso(_utcnow())
    report.installed_version = installed_version
    rid = _new_report_id()
    data = {"report_id": rid, **report.to_json()}

    root = reports_dir(shared_dir)
    root.mkdir(parents=True, exist_ok=True)
    # mode=0o644: may be written under sudo (root), read by the admin
    # server running as the evolve user — same contract as safe_upgrade.py.
    _atomic_write_json(root / f"{rid}.json", data, mode=0o644)
    _atomic_write_json(
        root / f"{_PAIR_PREFIX}{pair_key(installed_version, report.target_version)}.json",
        data, mode=0o644,
    )
    _retention_sweep(root)
    return rid


def load_report(report_id: str, shared_dir: Path | None = None) -> dict[str, Any] | None:
    path = reports_dir(shared_dir) / f"{report_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_cached_for_pair(
    installed_version: str | None, target_version: str, shared_dir: Path | None = None,
) -> dict[str, Any] | None:
    """The most recent report for this exact from → to pair, or None if this
    pair has never been checked — the raw entry, whatever its age.

    Deliberately not freshness-filtered: a caller that wants to know whether
    the entry can still gate an apply calls :func:`pair_freshness_reason` on
    the result, and a caller that only wants to DISPLAY the last run (the
    apply modal's "checked 3d ago" line) needs the entry even when stale.
    """
    path = reports_dir(shared_dir) / f"{_PAIR_PREFIX}{pair_key(installed_version, target_version)}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def latest_cache_for_installed(
    installed_version: str | None, shared_dir: Path | None = None,
) -> dict[str, Any] | None:
    """The most recently checked pair-cache entry whose ``installed_version``
    matches — the health check's read (item 5): it doesn't know which target
    the operator will pick next, only what's currently installed."""
    best: dict[str, Any] | None = None
    try:
        entries = list(reports_dir(shared_dir).glob(f"{_PAIR_PREFIX}*.json"))
    except OSError:
        return None
    for p in entries:
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if data.get("installed_version") != installed_version:
            continue
        if best is None or str(data.get("checked_at") or "") > str(best.get("checked_at") or ""):
            best = data
    return best


def _parse_checked_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def pair_freshness_reason(
    data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age_h: float = FRESHNESS_WINDOW_H,
    bot_set_hash: str | None = None,
) -> str | None:
    """Why *data* (a :func:`load_cached_for_pair` result) can no longer gate
    an apply, or None if it still can.

    ``"missing"`` — no entry (``data`` is None). ``"stale"`` — ``checked_at``
    is absent, unparsable, or older than *max_age_h*; unparsable is folded
    into stale rather than given its own reason because both mean the same
    thing to the caller: don't trust this, re-run it (fail closed rather
    than let an unreadable age read as fresh). ``"bot_set_changed"`` —
    *bot_set_hash* was given and doesn't match the report's; a report from
    before this field existed carries no hash at all, which never matches a
    real one, so an old-shaped report is correctly refused rather than
    silently grandfathered in.

    ``bot_set_hash=None`` skips that check entirely — the caller declining
    to state what the current registry looks like is not this function's
    business to second-guess.
    """
    if data is None:
        return "missing"
    checked = _parse_checked_at(data.get("checked_at"))
    if checked is None:
        return "stale"
    age_h = ((now or _utcnow()) - checked).total_seconds() / 3600.0
    if age_h > max_age_h:
        return "stale"
    if bot_set_hash is not None and data.get("bot_set_hash") != bot_set_hash:
        return "bot_set_changed"
    return None


def describe_age(data: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    """"3 d" / "5 h" / "12 min" — the age of *data*'s ``checked_at``, for a
    409 message ("preflight is {describe_age(...)} old — re-run")."""
    checked = _parse_checked_at((data or {}).get("checked_at"))
    if checked is None:
        return "an unreadable age"
    sec = max(0, ((now or _utcnow()) - checked).total_seconds())
    if sec < 3600:
        return f"{max(1, int(sec // 60))} min"
    if sec < 86400:
        return f"{int(sec // 3600)} h"
    return f"{int(sec // 86400)} d"


# ── Background-thread runner (for the HTTP POST endpoint) ────────────────────

_inflight_lock = threading.Lock()
_inflight_id: str | None = None


def inflight_report_id() -> str | None:
    with _inflight_lock:
        return _inflight_id


def start_background_check(
    network: dict[str, Any],
    target_version: str,
    *,
    actor: str,
    installed_version: str | None,
    shared_dir: Path | None = None,
    retired_models: "tuple[str, ...]" = (),
) -> tuple[str, str]:
    """Kick off a preflight on a background thread. Returns (report_id, status).

    If a check is already running, returns its report_id with
    status='running' rather than starting a second one — the target fetch
    and nine `sudo -u <bot>` round trips are expensive enough that two
    concurrent runs would just race each other for no benefit.
    """
    global _inflight_id
    with _inflight_lock:
        if _inflight_id is not None:
            return _inflight_id, "running"
        rid = _new_report_id()
        _inflight_id = rid

    def _worker(report_id: str) -> None:
        global _inflight_id
        try:
            report = preflight(network, target_version, retired_models=retired_models)
            report.actor = actor
            report.checked_at = _iso(_utcnow())
            report.installed_version = installed_version
            data = {"report_id": report_id, **report.to_json()}
            root = reports_dir(shared_dir)
            root.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(root / f"{report_id}.json", data, mode=0o644)
            _atomic_write_json(
                root / f"{_PAIR_PREFIX}{pair_key(installed_version, target_version)}.json",
                data, mode=0o644,
            )
            _retention_sweep(root)
        finally:
            with _inflight_lock:
                _inflight_id = None

    t = threading.Thread(target=_worker, args=(rid,), daemon=True)
    t.start()
    return rid, "running"
