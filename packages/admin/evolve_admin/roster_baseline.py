"""roster_baseline — the expected-state store for group-allowlist drift.

Background (R1a PR3, spec internal/spec-users-meta-2026-06-15.md §"Deferred to a
follow-up (PR3)"). After PR2 the group allowlist the gateway enforces
(``openclaw.json::channels.<ch>`` group list, resolved by
``roster_resolver.effective_group_allowlist``) is the SAME artifact the admin
curates via the ``/users/group-allowlist/{approve,revoke}`` routes. That closes
the governance gap for changes made *through* the admin surface — but an
OpenClaw-CLI edit, a hand-edit, or a membership-sync that widens
``channels.<ch>.allowFrom`` out-of-band is invisible to the operator between
admin GETs. This module is the safety-net's memory: a per-bot snapshot of the
**expected** (admin-recorded) group allowlist that
``roster_allowlist_drift_monitor`` compares the live on-disk config against.

Contract — who advances the baseline
====================================

The baseline is "what the admin last wrote" (or, absent any admin write, the
first live state the monitor observed). Two writers, and only two:

  * The admin write path (``routes_bot_users._group_allowlist_write``) records
    the baseline after every successful approve/revoke — so an admin change
    never reads as drift. This is the trust anchor.
  * The monitor **seeds** the baseline once, on first sight of a bot with no
    baseline yet, adopting the current live config as expected (there is no
    prior trusted state to compare against). After the seed the monitor NEVER
    advances the baseline again — otherwise a widening would be adopted on the
    next tick and the Signal would self-resolve, defeating the safety net.

Everything else (out-of-band CLI/hand edits) diverges from the baseline and is
what the monitor fires on. A drift Signal clears when the operator either
reverts the file OR approves the new senders via the admin route (which
re-records the baseline to match).

Storage
=======

``{shared_dir}/roster_baselines/<bot_id>.json`` — owned by the ``evolve`` user
(both writers run as evolve; the dir is under ``{shared_dir}`` which carries the
right ownership), so writes are a plain atomic temp-file + ``os.replace`` with
no ``/tmp`` staging or sudo. The snapshot is the exact shape
``roster_resolver.read_group_allowlists`` returns — ``{channel: [sorted id,
...]}`` for allowlist-gated, non-empty channels only — so the admin-write
snapshot and the monitor's live read are computed by the same seam and compare
apples to apples.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import roster_resolver as rr

# Sub-directory under {shared_dir} holding one baseline file per bot.
BASELINES_SUBDIR = "roster_baselines"


def baselines_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / BASELINES_SUBDIR


def baseline_path(shared_dir: Path, bot_id: str) -> Path:
    return baselines_dir(shared_dir) / f"{bot_id}.json"


def _normalize(allowlists: dict[str, list[str]]) -> dict[str, list[str]]:
    """Canonicalize a snapshot: drop empty channels, sort ids, sort channels.

    Sorting makes the on-disk snapshot stable across runs so a byte-diff (and
    the monitor's set comparison) is order-insensitive; dropping empty channels
    matches ``read_group_allowlists`` (which omits gated-but-empty channels), so
    the admin-write snapshot and a live read of the same config are identical.
    """
    out: dict[str, list[str]] = {}
    for ch in sorted(allowlists):
        ids = sorted({str(x) for x in (allowlists.get(ch) or []) if str(x)})
        if ids:
            out[ch] = ids
    return out


def read_baseline(shared_dir: Path, bot_id: str) -> dict[str, list[str]] | None:
    """Return the recorded expected group allowlists for ``bot_id``, or ``None``.

    ``None`` means "no baseline recorded yet" (never approved/revoked via the
    admin route and never seeded). A malformed/unreadable baseline file is also
    treated as ``None`` — the monitor re-seeds rather than crashing on a
    corrupt snapshot.
    """
    path = baseline_path(shared_dir, bot_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    channels = raw.get("channels") if isinstance(raw, dict) else None
    if not isinstance(channels, dict):
        return None
    # Re-normalize on read so a hand-edited baseline still compares cleanly.
    coerced = {
        ch: v for ch, v in channels.items()
        if isinstance(ch, str) and isinstance(v, list)
    }
    return _normalize(coerced)


def write_baseline(
    shared_dir: Path,
    bot_id: str,
    allowlists: dict[str, list[str]],
    *,
    source: str,
) -> dict[str, list[str]]:
    """Atomically persist the expected group allowlists for ``bot_id``.

    ``source`` records who advanced the baseline (``"admin_write"`` or
    ``"monitor_seed"``) for audit/debugging. Returns the normalized snapshot
    that was written. Atomic via temp-file + ``os.replace`` in the same dir.
    """
    normalized = _normalize(allowlists)
    directory = baselines_dir(shared_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "bot_id": bot_id,
        "channels": normalized,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{bot_id}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, str(baseline_path(shared_dir, bot_id)))
    except BaseException:
        # Best-effort cleanup of the temp file on any failure; the write is
        # atomic (os.replace) so a stale .tmp is the only debris to sweep.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return normalized


def snapshot_from_config(
    network: dict,
    bot_id: str,
    oc_config: dict,
    *,
    channels: "tuple[str, ...] | None" = None,
) -> dict[str, list[str]]:
    """Compute the effective group-allowlist snapshot from an in-memory config.

    Runs the same ``roster_resolver.read_group_allowlists`` seam the live read
    uses, but against an already-parsed ``oc_config`` (the config the admin
    write just persisted) — so the recorded baseline exactly matches what a
    fresh disk read would produce, with no extra read and no read/write race.
    """
    return _normalize(
        rr.read_group_allowlists(
            network, bot_id, channels=channels, oc_reader=lambda: oc_config
        )
    )


def record_baseline_from_config(
    network: dict,
    bot_id: str,
    oc_config: dict,
    *,
    shared_dir: Path | None = None,
    source: str = "admin_write",
) -> dict[str, list[str]]:
    """Snapshot + persist the baseline for ``bot_id`` from a parsed config.

    Convenience for the admin write path: derives ``shared_dir`` from
    ``network.json::sharedDir`` (override with the kwarg for tests) and records
    the effective group allowlists implied by ``oc_config``.
    """
    if shared_dir is None:
        # Cross-platform default via platform_profile (macOS /Users/Shared/evolve,
        # Linux /var/lib/evolve) — never a hardcoded /Users literal. In practice
        # ``load_network`` always populates sharedDir, so this is the belt.
        from platform_profile import get_profile
        shared_dir = Path(network.get("sharedDir") or get_profile().shared_dir_default)
    snap = snapshot_from_config(network, bot_id, oc_config)
    return write_baseline(shared_dir, bot_id, snap, source=source)


# Sentinel distinguishing "read failed" (unreadable) from "genuinely empty".
UNREADABLE = object()


def read_current_allowlists(
    network: dict,
    bot_id: str,
    *,
    channels: "tuple[str, ...] | None" = None,
    oc_reader: "Callable[[], dict | None] | None" = None,
) -> "dict[str, list[str]] | object":
    """Read the live on-disk effective group allowlists for ``bot_id``.

    Returns the ``UNREADABLE`` sentinel when ``openclaw.json`` cannot be read
    (missing or EACCES even via the sudo-cat fallback) — the monitor must treat
    that as an ``unreadable`` condition, NOT a silent "clean/empty" pass (a
    monitor that can't read must not look clean). Otherwise returns the
    normalized ``{channel: [id, ...]}`` snapshot (``{}`` when the config reads
    fine but has no gated group allowlists).

    ``oc_reader`` is injectable for tests; production builds the same
    direct-read-then-``sudo /bin/cat`` reader ``roster_resolver`` uses.
    """
    reader = oc_reader or rr._default_openclaw_reader(network, bot_id)
    oc = reader()
    if oc is None:
        return UNREADABLE
    return _normalize(
        rr.read_group_allowlists(
            network, bot_id, channels=channels, oc_reader=lambda: oc
        )
    )
