"""Daily enforcer for ``expires_at`` on per-bot openclaw.json overrides.

Phase 5 of ``internal/spec-openclaw-json-derived-artifact-2026-05-24.md``.

Walks ``{shared_dir}/sandbox/overrides/<bot_id>.json`` for every bot in
the pod. For each override that carries an ``expires_at`` timestamp:

  - Lapsed (``expires_at < now``): delete the override (the next deploy's
    materializer applies the shipped default) and emit a one-shot
    ``producer=openclaw_overrides_expiry, type=expired`` Signal so the
    operator knows the cleanup happened. The Signal does NOT auto-resolve
    on subsequent scans — it's an audit notification, not a live
    condition. Operator dismisses or it ages out via retention.

  - Within the pre-expiry window (``0 < expires_at - now <= 7 days``):
    emit a ``type=pre_expiry`` Signal at ``info`` severity (per-emit
    override of the producer's default ``warn``). Sweep-resolves if the
    operator extends ``expires_at`` past the window, deletes the
    override, or the override actually expires (a separate ``expired``
    Signal takes over).

  - Far-future or unset: skip silently.

The scanner is idempotent. Reading and parsing every overrides file is
cheap (the pod has ~7 bots × small dicts); running this every day at
04:00 keeps the enforcement window tight without burning resources.

Why a separate daily cron rather than firing on the materializer's
deploys: deploys aren't on a guaranteed cadence — a bot can go weeks
between redeploys. The cron guarantees the expiry actually happens
within a day of ``expires_at``.

Failure modes that DON'T fire a Signal (intentional):
  - Malformed ``expires_at`` strings — already rejected at write-time
    by Phase 2's ``_validate_expires_at``. If we encounter one anyway
    (manually-edited file), log a warning and skip. The operator can
    fix or revert via the Customizations UI.
  - ``OverrideStateError`` (overrides file corrupt) — log a warning
    and skip that bot. The Customizations UI will surface the same
    error to the operator when they try to interact.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_SHARED_DIR
from .config_sandbox import (
    OverrideEntry,
    OverrideStateError,
    delete_override,
    iter_all_overrides,
)


_log = logging.getLogger(__name__)


PRODUCER = "openclaw_overrides_expiry"
TYPE_EXPIRED = "expired"
TYPE_PRE_EXPIRY = "pre_expiry"

# How far in advance to warn that an override is about to lapse. The
# spec §11.5 picked 7 days as the analogue of cert-expiry tooling: long
# enough that the operator has a chance to extend or annotate during a
# weekend, short enough that the warning is actionable.
DEFAULT_PRE_EXPIRY_WINDOW_DAYS = 7


@dataclass
class ExpiryScanResult:
    """What the scanner did. Returned by ``scan()`` for caller logging."""

    expired: list[tuple[str, str, OverrideEntry]] = field(default_factory=list)
    pre_expiry: list[tuple[str, str, OverrideEntry, int]] = field(default_factory=list)
    unparseable: list[tuple[str, str, str]] = field(default_factory=list)  # (bot, key, expires_at_raw)
    bot_errors: dict[str, str] = field(default_factory=dict)
    scanned_entry_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# expires_at parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_expires_at(raw: str) -> datetime | None:
    """Parse an ``expires_at`` string into a UTC-aware datetime, or None.

    Phase 2's ``_validate_expires_at`` (overrides.py) accepts:
      - ISO date: ``"2026-08-01"``
      - ISO datetime, optionally with trailing Z: ``"2026-08-01T14:00:00Z"``
      - ISO datetime with explicit timezone: ``"2026-08-01T14:00:00-07:00"``
        (preserved as-is; comparison is tz-aware).

    On a date-only value, treats expiry as end-of-day UTC: "expires
    2026-08-01" means "valid through 2026-08-01 23:59:59 UTC." This
    matches operator intuition ("expires Aug 1" = "still good on Aug 1")
    but has a side effect: the 7-day pre-expiry window for a date-only
    value is effectively ~6.5 days of visibility from a noon-ish scan
    time. Datetime values use the full window. If you need precise
    pre-expiry timing, write a datetime, not a date.

    Returns None when the string can't be parsed — caller treats as
    "unknown, skip with warning" so a manually-corrupted file doesn't
    crash the scanner.
    """
    if not raw or not isinstance(raw, str):
        return None
    # Use endswith rather than rstrip("Z") so a malformed "ZZ" doesn't
    # silently parse as the same value.
    s = raw[:-1] if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Date-only or naive datetime — interpret as UTC.
        if "T" not in s:
            # End of day: 23:59:59 UTC of the named date.
            dt = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# Signal store lazy import (matches the Phase 1 validator pattern)
# ─────────────────────────────────────────────────────────────────────────────


def _import_signals():
    """Lazy-import signals.store + schema.signal. Same pattern as Phase 1's
    openclaw_config_validator. Returns ``(store, schema)`` or
    ``(None, None)`` if either import fails.
    """
    try:
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
        return store, schema
    except Exception:
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────────────────────────────────────


def scan(
    shared_dir: Path = DEFAULT_SHARED_DIR,
    *,
    now: datetime | None = None,
    pre_expiry_window_days: int = DEFAULT_PRE_EXPIRY_WINDOW_DAYS,
) -> ExpiryScanResult:
    """Walk every override and act on expiries.

    Returns an ``ExpiryScanResult`` summarizing what happened. Always
    returns; per-bot/per-key failures land on ``unparseable`` or
    ``bot_errors`` rather than raising.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    result = ExpiryScanResult()
    store, schema = _import_signals()
    pre_expiry_kept: set[str] = set()
    window = timedelta(days=pre_expiry_window_days)

    # Iterate every override across every bot in one pass.
    try:
        entries = list(iter_all_overrides(shared_dir))
    except Exception as e:
        _log.warning(
            "openclaw_overrides_expiry: iter_all_overrides failed (%s: %s); "
            "scanner cannot proceed",
            type(e).__name__, e,
        )
        return result

    # Once a bot's overrides file becomes unreadable (OverrideStateError
    # from the strict write-time read inside ``delete_override``), every
    # subsequent key on that bot will hit the same error. Skip cheaply
    # via this set rather than retry-and-log-N-times.
    failed_bots: set[str] = set()

    for bot_id, key, entry in entries:
        result.scanned_entry_count += 1
        if bot_id in failed_bots:
            continue
        if not entry.expires_at:
            continue

        expiry = _parse_expires_at(entry.expires_at)
        if expiry is None:
            _log.warning(
                "openclaw_overrides_expiry: %s/%s has unparseable expires_at=%r; "
                "skipping. Fix via Customizations UI or revert.",
                bot_id, key, entry.expires_at,
            )
            result.unparseable.append((bot_id, key, entry.expires_at))
            continue

        delta = expiry - now
        if delta.total_seconds() <= 0:
            # Lapsed. Delete the override; emit one-shot expired Signal.
            try:
                delete_override(shared_dir, bot_id, key)
                result.expired.append((bot_id, key, entry))
                _emit_expired(store, schema, shared_dir, bot_id, key, entry)
            except OverrideStateError as e:
                _log.warning(
                    "openclaw_overrides_expiry: %s overrides file unreadable, "
                    "skipping remaining keys on this bot: %s",
                    bot_id, e,
                )
                result.bot_errors[bot_id] = f"OverrideStateError: {e}"
                failed_bots.add(bot_id)
                continue
            except Exception as e:
                _log.warning(
                    "openclaw_overrides_expiry: %s/%s delete failed: %s",
                    bot_id, key, e,
                )
                result.bot_errors.setdefault(bot_id, f"{type(e).__name__}: {e}")
                continue
        elif delta <= window:
            # Within the pre-expiry window.
            days_left = max(0, delta.days)
            result.pre_expiry.append((bot_id, key, entry, days_left))
            sig = _emit_pre_expiry(
                store, schema, shared_dir, bot_id, key, entry, delta,
            )
            if sig is not None:
                pre_expiry_kept.add(sig)
        # Else: far-future; nothing to do.

    # Sweep-resolve pre_expiry Signals that are no longer in the keep set
    # — operator extended expires_at, deleted the override, or the
    # override actually expired (separate "expired" Signal took over).
    if store is not None:
        try:
            store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=pre_expiry_kept,
                reason="auto-resolve: override extended, removed, or lapsed",
                types={TYPE_PRE_EXPIRY},
            )
        except Exception as e:
            _log.warning(
                "openclaw_overrides_expiry: sweep_resolve failed (%s: %s)",
                type(e).__name__, e,
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Signal emission helpers
# ─────────────────────────────────────────────────────────────────────────────


def _emit_expired(
    store, schema, shared_dir: Path, bot_id: str, key: str, entry: OverrideEntry,
) -> str | None:
    if store is None or schema is None:
        return None
    signature = schema.make_signature(PRODUCER, TYPE_EXPIRED, f"{bot_id}:{key}")
    body_lines = [
        f"Override **{key}** on bot **{bot_id}** lapsed at "
        f"`{entry.expires_at}` and has been removed.",
        "",
        f"The next deploy of this bot will materialize the shipped default "
        f"for `{key}`. If you still want the override, recreate it via the "
        f"Customizations card under Settings → Bot.",
    ]
    if entry.note:
        body_lines.extend(["", f"Original note: {entry.note}"])
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=TYPE_EXPIRED,
            flavor="maintenance",
            scope="bot",
            bot_id=bot_id,
            title=f"{bot_id}: override expired and removed ({key})",
            body="\n".join(body_lines),
            details={
                "bot_id": bot_id,
                "key": key,
                "expires_at": entry.expires_at,
                "set_by": entry.set_by,
                "set_at": entry.set_at,
                "value": entry.value,
            },
        )
        return signature
    except Exception as e:
        _log.warning(
            "openclaw_overrides_expiry: observe(expired) failed for %s/%s: %s",
            bot_id, key, e,
        )
        return None


def _format_time_left(delta: timedelta) -> tuple[str, int]:
    """Render a positive ``timedelta`` as operator-friendly text + integer
    days remaining.

    Returns ``(human_string, days_left)`` where ``human_string`` is one of
    "today" / "tomorrow" / "in N days" and ``days_left`` is the rounded-up
    integer for the Signal title. The truncation-to-zero of ``delta.days``
    produced "lapses in 0 days" for an 18h-away expiry, which read like
    "already expired" to operators — this picks a more obvious wording.
    """
    total_seconds = delta.total_seconds()
    if total_seconds < 86400:
        return "today", 1
    if total_seconds < 86400 * 2:
        return "tomorrow", 1
    days = int(total_seconds // 86400)
    return f"in {days} days", days


def _emit_pre_expiry(
    store, schema, shared_dir: Path, bot_id: str, key: str,
    entry: OverrideEntry, delta: timedelta,
) -> str | None:
    if store is None or schema is None:
        return None
    signature = schema.make_signature(PRODUCER, TYPE_PRE_EXPIRY, f"{bot_id}:{key}")
    time_left, days_left = _format_time_left(delta)
    body_lines = [
        f"Override **{key}** on bot **{bot_id}** lapses **{time_left}** "
        f"(`{entry.expires_at}`).",
        "",
        f"When it lapses the next deploy materializes the shipped default. "
        f"To keep the override, extend or remove its `expires_at` on the "
        f"Customizations card. To let it lapse, no action needed.",
    ]
    if entry.note:
        body_lines.extend(["", f"Override rationale: {entry.note}"])
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=TYPE_PRE_EXPIRY,
            flavor="maintenance",
            severity="info",   # advisory per-emit override of the producer's default
            scope="bot",
            bot_id=bot_id,
            title=f"{bot_id}: override expiring {time_left} ({key})",
            body="\n".join(body_lines),
            details={
                "bot_id": bot_id,
                "key": key,
                "expires_at": entry.expires_at,
                "days_left": days_left,
                "set_by": entry.set_by,
                "set_at": entry.set_at,
            },
        )
        return signature
    except Exception as e:
        _log.warning(
            "openclaw_overrides_expiry: observe(pre_expiry) failed for %s/%s: %s",
            bot_id, key, e,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point — python3 -m evolve_admin.openclaw_overrides_expiry
# ─────────────────────────────────────────────────────────────────────────────


def format_summary(result: ExpiryScanResult) -> str:
    """Render the operator-facing cron log summary.

    The first line every day is the count of entries scanned (so even a
    no-op day shows the scanner ran). Each section includes the actual
    expires_at / set_at timestamps so operators can answer "when did
    this happen" without cross-referencing the overrides file.
    """
    lines: list[str] = []
    lines.append(
        f"openclaw_overrides_expiry: scanned {result.scanned_entry_count} entries"
    )
    if result.expired:
        lines.append(f"  Expired and removed: {len(result.expired)}")
        for bot_id, key, entry in result.expired:
            lines.append(
                f"    {bot_id}: {key} "
                f"(expired {entry.expires_at}, was set {entry.set_at} by {entry.set_by})"
            )
    if result.pre_expiry:
        lines.append(f"  Approaching expiry: {len(result.pre_expiry)}")
        for bot_id, key, entry, days_left in result.pre_expiry:
            lines.append(
                f"    {bot_id}: {key} "
                f"(lapses {entry.expires_at}, ~{days_left}d left)"
            )
    if result.unparseable:
        lines.append(f"  ⚠ Unparseable expires_at: {len(result.unparseable)}")
        for bot_id, key, raw in result.unparseable:
            lines.append(f"    {bot_id}: {key} = {raw!r}")
    if result.bot_errors:
        lines.append(f"  ⚠ Bot errors: {len(result.bot_errors)}")
        for bot_id, err in sorted(result.bot_errors.items()):
            lines.append(f"    {bot_id}: {err}")
    if not (result.expired or result.pre_expiry
            or result.unparseable or result.bot_errors):
        lines.append("  (no action — every override is far from expiry)")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evolve-admin openclaw-expiry-scan",
        description=(
            "Daily enforcer for the expires_at field on per-bot openclaw.json "
            "overrides. Deletes lapsed overrides + emits Signals on the alerts "
            "page. Idempotent; safe to run manually. See spec-openclaw-json-"
            "derived-artifact-2026-05-24.md §11.5."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=DEFAULT_SHARED_DIR,
        help="Path to the evolve shared dir (default: %(default)s).",
    )
    parser.add_argument(
        "--pre-expiry-window-days", type=int,
        default=DEFAULT_PRE_EXPIRY_WINDOW_DAYS,
        help=(
            "Days before expiry at which to emit a pre_expiry Signal "
            "(default: %(default)s)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = scan(
        shared_dir=args.shared_dir,
        pre_expiry_window_days=args.pre_expiry_window_days,
    )
    print(format_summary(result))
    # Non-zero exit on per-bot file corruption so any external
    # "did the cron pass" check (launchctl print, monitoring wrapper)
    # surfaces the degradation. Unparseable expires_at on a single key
    # is a softer signal — log + 0 — since most of the scan still ran.
    return 1 if result.bot_errors else 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
