"""permissions.bootstrap — seed the baseline from observed pod state.

Spec: internal/spec-permission-posture-2026-05-10.md §5.1.

On first run (or whenever an operator wants to re-snapshot), the
bootstrap reads every bot's openclaw.json and produces:

  - ``pod_default.permission_config`` = the modal value for each field
    across bots.
  - ``per_bot_overrides[<bot>].permission_config`` = each bot's
    deviation from the pod modal (only the differing fields, so
    overrides stay minimal).

It also seeds the per-bot cron_baseline files so the monitor can
detect silent additions from that point forward.

This matches reality on day one — the monitor's first run is then
silent on config drift. Real changes from there forward trip signals.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import baseline as _bl
from . import inventory as _inv
from .monitor import _write_cron_baseline


def _modal_value(values: list[Any]) -> Any:
    """Return the most common value in ``values``.

    Treats None as a valid value (a field being unset on most bots
    is real signal). Ties broken by first-occurrence.
    """
    if not values:
        return None
    # Counter doesn't hash unhashable types (e.g. lists). Coerce to a
    # tuple for counting then return the original.
    keys = []
    originals = []
    for v in values:
        if isinstance(v, list):
            keys.append(("list", tuple(v)))
        elif isinstance(v, dict):
            keys.append(("dict", tuple(sorted(v.items()))))
        else:
            keys.append(("scalar", v))
        originals.append(v)
    counts = Counter(keys)
    modal_key, _ = counts.most_common(1)[0]
    # Return the first original whose key matches
    for k, o in zip(keys, originals):
        if k == modal_key:
            return o
    return None


def derive_baseline(
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    home_override_by_bot: "dict[str, Path] | None" = None,
) -> dict:
    """Read live state for every bot, derive a baseline matching it.

    Returns a baseline dict in the same shape as DEFAULT_BASELINE. The
    pod_default reflects the modal value for each PermissionConfig
    field; per_bot_overrides records each bot's divergence.

    Bots whose openclaw.json is unreadable are skipped (their entries
    are simply absent — no override created).
    """
    home_override_by_bot = home_override_by_bot or {}

    # Collect each bot's observed PermissionConfig fields
    observed_by_bot: dict[str, dict[str, Any]] = {}
    for bid in bot_ids:
        inv = _inv.read_inventory(
            bid, config,
            home_override=home_override_by_bot.get(bid),
        )
        if inv.permission_config.read_error is not None:
            continue
        observed_by_bot[bid] = dict(inv.permission_config.fields)

    # Compute modal pod_default per field
    pod_default_pc: dict[str, Any] = {}
    for fld in _inv.PERMISSION_CONFIG_FIELDS:
        values = [obs.get(fld) for obs in observed_by_bot.values()]
        pod_default_pc[fld] = _modal_value(values)

    # Compute per-bot overrides (only the differing fields)
    per_bot_overrides: dict[str, dict[str, Any]] = {}
    for bid, obs in observed_by_bot.items():
        diffs = {fld: obs.get(fld) for fld in _inv.PERMISSION_CONFIG_FIELDS
                 if obs.get(fld) != pod_default_pc.get(fld)}
        if diffs:
            per_bot_overrides[bid] = {"permission_config": diffs}

    # Start from the default scaffold so denylist + thresholds get sensible
    # values, then overwrite pod_default.permission_config with observed.
    baseline = deepcopy(_bl.DEFAULT_BASELINE)
    baseline["pod_default"]["permission_config"] = pod_default_pc
    baseline["per_bot_overrides"] = per_bot_overrides
    return baseline


def bootstrap(
    shared_dir: Path,
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    overwrite: bool = False,
    seed_cron_baselines: bool = True,
    home_override_by_bot: "dict[str, Path] | None" = None,
) -> dict:
    """Derive a baseline from live state and write it.

    If a baseline file already exists and ``overwrite=False``, this
    is a no-op (returns the existing baseline). Pass ``overwrite=True``
    to re-snapshot.

    ``seed_cron_baselines=True`` (default) also writes a per-bot
    cron_baseline file capturing the current job ids — so the
    perm_cron_added_silently signal has a reference point from this
    point forward.

    Returns the baseline that ended up on disk.
    """
    existing_path = _bl.baseline_path(shared_dir)
    if existing_path.exists() and not overwrite:
        return _bl.load(shared_dir)

    baseline = derive_baseline(
        bot_ids, config, home_override_by_bot=home_override_by_bot,
    )
    _bl.write(baseline, shared_dir)

    if seed_cron_baselines:
        for bid in bot_ids:
            inv = _inv.read_inventory(
                bid, config,
                home_override=(home_override_by_bot or {}).get(bid),
            )
            job_ids = [j.id for j in inv.scheduled_invocations.jobs if j.id]
            _write_cron_baseline(shared_dir, bid, job_ids)

    return baseline
