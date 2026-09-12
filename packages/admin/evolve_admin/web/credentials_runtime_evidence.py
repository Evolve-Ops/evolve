"""Runtime evidence for the Credentials tab: which providers actually billed.

Clause (d′) of the visibility rule (see :mod:`credentials_visibility`) needs
one fact: **has this provider served a turn on this bot recently?** A
provider with turns in the log is configured BY DEFINITION, whatever the
credential probes did or did not find — and a row we cannot show is a key
the operator cannot rotate while it is spending money.

Source is the per-bot turn log (``{turns_dir}/turns-<YYYY-MM-DD>.jsonl``,
one record per turn carrying ``provider``). Those filenames are **UTC**, so
this walks UTC days — reading them on local time drops or invents a day at
every boundary ([[feedback_turn_files_are_utc_named_readers_must_not_use_local]]).

Cost control, in order of how much they save:
  1. The caller invokes this LAZILY — only when a provider row would
     otherwise be hidden. On a healthy bot the runtime-store probe already
     found the key, every LLM row lists, and this module never runs.
  2. Results are memoised for :data:`CACHE_TTL_S` per bot. The admin server
     is long-lived, so this is a real cache, not a per-request one; the TTL
     is what keeps it from being permanent state
     ([[feedback_process_scoped_budget_is_wrong_in_a_long_lived_daemon]]).
  3. Days are read newest-first and the walk stops as soon as every
     candidate provider has been seen.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR

from ..config import load_network

#: Window the operator's rule is stated over: "turns in the bot's log in the
#: last 30 days".
DEFAULT_WINDOW_DAYS = 30

#: Seconds a scan result is reused. Five minutes: long enough that reloading
#: the Credentials tab is free, short enough that a provider that started
#: billing shows up while the operator is still looking at the page.
CACHE_TTL_S = 300

_cache: dict[str, tuple[float, frozenset[str]]] = {}


def clear_cache() -> None:
    """Drop the memo. Called by tests; harmless in production."""
    _cache.clear()


def _turns_dirs(bot_id: str, network_path: Path) -> list[Path]:
    """Candidate turn directories, best first.

    The server's own resolver goes first (it reads the bot's
    ``openclaw.json`` for a custom workspace); the canonical shared-dir
    layout is always appended. The shared dir is platform-keyed — a
    ``/Users/Shared`` literal here would miss every turn on a Linux pod
    ([[feedback_users_only_path_assumption_breaks_linux_silently]]).
    """
    out: list[Path] = []
    try:
        from .server import resolve_bot_paths
        paths = resolve_bot_paths(bot_id)
        candidates = paths.get("turns_dir_candidates") or [
            paths.get("turns_dir"), paths.get("turns_dir_fallback"),
        ]
        out = [Path(c) for c in candidates if c]
    except Exception:  # noqa: BLE001 — the canonical layout still applies
        out = []
    # The canonical layout is APPENDED, not used only as a fallback: the
    # server resolver reads the DEFAULT network.json, so a caller that passed
    # a different `network_path` (every test, and any pod whose config lives
    # elsewhere) would otherwise get candidates for the wrong shared dir and
    # a silent empty answer.
    net = load_network(network_path)
    shared = Path(net.get("sharedDir") or CANONICAL_SHARED_DIR)
    canonical = shared / bot_id / "turns"
    if canonical not in out:
        out.append(canonical)
    return out


def _providers_in_file(path: Path, out: set[str]) -> None:
    try:
        with path.open("r", errors="replace") as handle:
            for line in handle:
                if '"provider"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                provider = rec.get("provider") if isinstance(rec, dict) else None
                if isinstance(provider, str) and provider.strip():
                    out.add(provider.strip().lower())
    except OSError:
        return


def providers_with_recent_turns(
    bot_id: str,
    *,
    network_path: Path,
    window_days: int = DEFAULT_WINDOW_DAYS,
    wanted: frozenset[str] | None = None,
    now: datetime | None = None,
    use_cache: bool = True,
) -> frozenset[str]:
    """Providers that served at least one turn for *bot_id* in the window.

    *wanted*, when given, is the set the caller actually cares about: the
    day-walk stops early once all of them have been seen. Passing it never
    changes the answer for those providers, only how much of the log is read.

    Returns an empty set — never raises — when the bot has no turn log, the
    directory is unreadable, or the records carry no ``provider`` field.
    """
    if use_cache:
        hit = _cache.get(bot_id)
        if hit is not None and (time.time() - hit[0]) < CACHE_TTL_S:
            return hit[1]

    anchor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    dirs = _turns_dirs(bot_id, network_path)
    found: set[str] = set()
    for offset in range(max(window_days, 1)):
        day = (anchor - timedelta(days=offset)).strftime("%Y-%m-%d")
        for turns_dir in dirs:
            _providers_in_file(turns_dir / f"turns-{day}.jsonl", found)
        if wanted and wanted.issubset(found):
            break

    result = frozenset(found)
    if use_cache:
        _cache[bot_id] = (time.time(), result)
    return result


__all__ = [
    "CACHE_TTL_S",
    "DEFAULT_WINDOW_DAYS",
    "clear_cache",
    "providers_with_recent_turns",
]
