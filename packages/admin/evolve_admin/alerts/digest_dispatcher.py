"""Digest flusher — Phase G of the alert-subscriptions spec.

The dispatcher (``dispatcher.send``) appends events to
``{shared_dir}/alerts/digest-pending/{daily,weekly}.jsonl`` when an
operator's subscription for the catalog event uses a digest frequency.
This module drains those queues at the digest hour and pushes one
bundled message per flush.

Usage
-----

CLI::

    python3 -m evolve_admin.alerts.digest_dispatcher --frequency daily \\
        --shared-dir /Users/Shared/evolve

Library::

    from evolve_admin.alerts.digest_dispatcher import flush
    outcome = flush(shared_dir, network, frequency="daily", now=now)

Idempotency
-----------

The active queue (``digest-pending/daily.jsonl`` /
``digest-pending/weekly.jsonl``) is rotated to a ``.flushing-<iso>``
filename atomically before reading. New events that arrive during the
flush land in a fresh queue file; the next flush picks them up. On a
successful send the rotated file is renamed ``.flushed-<date>``; on
failure it's renamed back to the active queue path so the next tick
retries.

Therefore: running ``flush`` when the queue is empty does nothing.
Running it twice in a row when there are events sends one bundle,
then noops the second time. Safe to schedule from cron without
co-ordination.

Render shape
------------

One single message per flush. A multi-category flush keeps the generic
header and sections by catalog category; a single-category flush is
titled by the topic itself:

::

    📋 Daily digest — 5 events

    💰 Cost (2)
      • 💰 Daily spend over threshold  (×2)
    ⚠️ System (3)
      • ⚠️ Stalled cron (team_bot_a / ai.evolve.team_bot_a.measure)
      • ⚠️ Application tests failing

    — Subscription: "Daily spend over soft threshold" · manage in Reports → Subscriptions
    — Subscription: "Scheduled job hasn't run" · manage in Reports → Subscriptions

A single-category weekly flush of a recurring event rolls up instead of
listing each occurrence:

::

    🔄 Updates — past week

      • 🔄 Evolve improved 16 times this week. Your pod stays up to date automatically.

    — Subscription: "New Evolve code available" · manage in Reports → Subscriptions

Per-event line = first non-empty line of the event's rendered message
(what would have been the immediate push), since that's already the
event's title. Several records sharing a recurring catalog_event collapse
to one line (registry template, or a ``(×N)`` multiplier on the
short-line). The operator drills into the Alerts UI for full bodies.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog as _cat
from . import dispatcher as _dispatch
from . import grace as _grace
from . import subscriptions as _subs
from .dispatcher import (
    DispatchOutcome,
    DispatchResult,
    Severity,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover  (Python < 3.9; we ship 3.10)
    ZoneInfo = None  # type: ignore


# Display label per category. Kept here rather than on the Category
# enum because this is purely a digest-renderer concern; the catalog
# itself doesn't have a "user-friendly section header" need outside
# this codepath.
_CATEGORY_DIGEST_LABELS: dict[_cat.Category, str] = {
    _cat.Category.SECURITY:  "🛡️ Security",
    _cat.Category.COST:      "💰 Cost",
    _cat.Category.SYSTEM:    "⚠️ System",
    _cat.Category.DECISIONS: "📋 Decisions",
    _cat.Category.UPDATES:   "🔄 Updates",
    _cat.Category.SUMMARIES: "📊 Summaries",
}


# Rollup templates for recurring catalog events. When a single digest
# carries N records of the same ``catalog_event`` AND that event is keyed
# here, the digest renders ONE summary line (via ``str.format(count=N)``)
# instead of N near-identical short-lines. This is the fix for the
# "16 identical '🔄 origin/main has new commits' lines" digest — a busy
# recurring producer gets one human sentence, not a log dump.
#
# Mirrors the ``_CATEGORY_DIGEST_LABELS`` pattern (module-level dict, edit
# to tune wording). Catalog events with no entry here fall back to the
# per-line listing with identical-line collapse (see ``_collapsed_lines``).
_DIGEST_ROLLUP_TEMPLATES: dict[str, str] = {
    "updates.evolve_repo": (
        "🔄 Evolve improved {count} times this week. "
        "Your pod stays up to date automatically."
    ),
}


def _digest_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "alerts" / "digest-pending"


def _active_queue_path(shared_dir: Path, frequency: str) -> Path:
    if frequency not in ("daily", "weekly"):
        raise ValueError(f"frequency must be 'daily' or 'weekly', got {frequency!r}")
    return _digest_dir(shared_dir) / f"{frequency}.jsonl"


def _iso_z(now: datetime) -> str:
    """Format a GIVEN datetime as seconds-precision UTC Z — not a "now"
    primitive (takes the flush timestamp as a parameter)."""
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_rotate(active: Path, rotated: Path) -> bool:
    """Atomically rename ``active`` → ``rotated``. Returns False when
    the active path didn't exist (nothing to do)."""
    try:
        os.replace(active, rotated)
        return True
    except FileNotFoundError:
        return False


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Malformed line — skip, don't take down the whole flush.
            # Operator gets the rest of the queue.
            continue
    return out


def _short_line(message: str) -> str:
    """Reduce a multi-line event body to a single per-event digest line.

    Heuristic: take the first non-empty line. That's the event's
    title (e.g. "💰 Daily spend over threshold") rendered by the
    catalog body_template. Good enough for a digest where the
    operator drills into the Alerts UI for full context.
    """
    for raw in message.splitlines():
        line = raw.strip()
        if line:
            return line
    return "(empty event body)"


def _category_for(catalog_event_key: str | None) -> _cat.Category | None:
    if not catalog_event_key:
        return None
    entry = _cat.by_key(catalog_event_key)
    return entry.category if entry else None


def _dedup_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse records sharing ``(catalog_event, collapse_key)``.

    The dispatcher's digest queue is append-only — every ``DEFERRED``
    enqueue tacks a new line on, even if the same Signal was already
    queued earlier in the window. Without this, a digest-only Signal
    that keeps firing for a day shows up N times in the digest (one
    line per minute the source ran). The most recent record wins so
    timestamps stay fresh.

    The collapse key is the record's ``digest_collapse_key`` (written by
    ``dispatcher._enqueue_digest``), which is the caller's ``dedup_key``
    when one was supplied, or an ``auto:<source>:<event>:<body_hash>``
    fallback otherwise. The fallback collapses recurring records with an
    *identical body* (the storm/resolve paths pass ``dedup_key=None`` for
    no-cooldown but still recur byte-for-byte) while leaving genuinely
    distinct events — different bodies, e.g. forge job completions or
    per-SHA commit notices — uncollapsed, preserving the original
    "don't collapse me" intent of ``dedup_key=None``.

    Legacy on-disk records written before ``digest_collapse_key`` existed
    fall back to the raw ``dedup_key``; if that's also absent the record
    passes through unchanged (the old behaviour).
    """
    seen: dict[tuple[str, str], int] = {}  # (event, collapse_key) → out-index
    out: list[dict[str, Any]] = []
    for rec in records:
        ck = rec.get("digest_collapse_key") or rec.get("dedup_key")
        if not ck:
            out.append(rec)
            continue
        key = (rec.get("catalog_event") or "", ck)
        if key in seen:
            # Replace the older record so the most recent ts + message win.
            out[seen[key]] = rec
        else:
            seen[key] = len(out)
            out.append(rec)
    return out


def _cancel_transient_pairs(
    shared_dir: Path, records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Elide fire+clear pairs of a within-grace transient (L1).

    Spec: spec-delta-transient-delivery-grace-2026-06-26 §L1. When a signal's
    **fire and its matching resolve both land in the same digest window** AND
    the signal's lifetime was shorter than its severity grace (§L3), BOTH
    lines are dropped — the net effect of a "nothing to do" blip is silence.

    Pairing is by ``coalesce_key`` (the Signal signature, stamped on both the
    fire and the resolve digest record by ``signal_notifier`` via
    ``digest_meta``). A record is a *fire* or *resolve* by its ``kind`` field;
    records without a ``coalesce_key``/``kind`` are pass-through (legacy
    records, or events that never declared a pairing — never cancelled).

    Invariants enforced here:

      * **``alert`` is never cancelled.** A critical that fired and cleared
        still surfaces — the cancel is gated on the Signal's OWN severity
        (``signal_severity``, not the INFO wrapper the resolve send uses).
      * **Only within-grace pairs cancel.** The resolve record carries
        ``lifetime_seconds`` (created→resolved); we cancel only when that is
        shorter than the severity grace. A warn that PERSISTED past grace
        before clearing keeps both lines (the operator wanted to know it was
        broken for a meaningful stretch). A resolve with no measurable
        lifetime is NOT cancelled (fail-loud: when in doubt, surface it).
      * **Cancellation is delivery-only** — this drops digest *lines*; the
        Signal store's firing/resolve history is untouched (Alerts page
        still shows everything).

    Returns a new list with cancelled fire+resolve records removed, original
    order otherwise preserved.
    """
    # Index resolve records by coalesce_key so a fire can find its mate. A
    # within-window transient produces exactly one fire + one resolve for a
    # signature; if the queue somehow carries several, the first cancellable
    # resolve claims the pair (the rest fall through to normal rendering).
    resolves_by_key: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        if rec.get("kind") != "resolve":
            continue
        key = rec.get("coalesce_key")
        if key:
            resolves_by_key.setdefault(key, []).append(i)

    drop: set[int] = set()
    for i, rec in enumerate(records):
        if rec.get("kind") != "fire" or i in drop:
            continue
        key = rec.get("coalesce_key")
        if not key:
            continue
        # The Signal's own severity decides eligibility — never cancel an
        # alert pair regardless of how the resolve was dispatched.
        severity = rec.get("signal_severity") or rec.get("severity") or "warn"
        if severity == "alert":
            continue
        mate_idx = None
        for j in resolves_by_key.get(key, []):
            if j in drop:
                continue
            mate = records[j]
            lifetime = mate.get("lifetime_seconds")
            if lifetime is None:
                continue  # unmeasurable lifetime → don't cancel (fail-loud)
            if _grace.within_grace(shared_dir, severity, float(lifetime)):
                mate_idx = j
                break
        if mate_idx is not None:
            drop.add(i)
            drop.add(mate_idx)

    if not drop:
        return records
    return [rec for i, rec in enumerate(records) if i not in drop]


def _firing_signatures(shared_dir: Path) -> set[str] | None:
    """The set of Signal signatures currently in ``signals/firing/``.

    Routes through the sanctioned ``signals.store`` API (NOT a raw directory
    glob): the store is the single read path that the Phase D SQLite swap will
    keep correct — a hand-rolled ``signals/firing/*.json`` glob would silently
    return empty after the swap, and an empty firing set here would let the
    roll-up collapse a *still-firing* alert (the exact cardinal-invariant
    violation this whole step guards against). See
    internal/spec-state-store-and-deploy-resilience-2026-06-10.md §1.1.

    The import is lazy (the analyzer package is installed at runtime alongside
    the admin daemon, but the admin package must still import cleanly without
    it). Returns ``None`` (NOT an empty set) on any failure so the caller can
    distinguish "nothing is firing" from "I couldn't tell" and fail SAFE (skip
    the roll-up, keep everything loud) in the latter case.
    """
    try:
        store = importlib.import_module("signals.store")
        return {
            sig.signature
            for sig in store.iter_signals(Path(shared_dir), subdirs=("firing",))
            if getattr(sig, "signature", None)
        }
    except Exception:
        return None


def _recovered_flap_line(catalog_event: str | None, bot_id: str | None, n: int) -> str:
    """One plain-language retrospective line for a recovered-flap group.

    ``n`` is the number of distinct signatures (e.g. setup files) of the same
    ``catalog_event`` that flapped-and-recovered on ``bot_id`` in this window.
    The condition's human label comes from the catalog; falls back to the raw
    key (or a generic phrase) on catalog drift.
    """
    entry = _cat.by_key(catalog_event) if catalog_event else None
    label = entry.label if entry is not None else (catalog_event or "an alert")
    count = f"{n} × " if n > 1 else ""
    where = f" on {bot_id}" if bot_id else ""
    return (
        f"↩️ Recovered after flapping: {count}{label}{where} "
        "(see Alerts → History)"
    )


def _rollup_recovered_flaps(
    shared_dir: Path, records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse net-resolved fire+clear flaps into one retrospective line each.

    The layer ABOVE L1 (``_cancel_transient_pairs``). L1 silences a *true blip*
    — a within-grace transient — and deliberately exempts ``alert`` severity
    (the must-page contract). But a *daily digest* is retrospective by
    construction: a condition that broke and self-healed N hours ago is, at
    most, one summary line — even at ``alert`` severity. The immediate /
    must-page guarantee is untouched because it lives on the immediate delivery
    path, not here; by the time a record reaches the digest queue the page (or
    the explicit decision to digest it) has already happened. So this step is
    **severity-agnostic** — unlike L1, it does NOT exempt ``alert``.

    The bug it fixes (live evo-vps 2026-06-29): the content-scan producer mints
    "X missing or unreadable" at ``alert`` severity, so L1's alert carve-out
    keeps BOTH the 🔴 fire line and the 🟢 "Cleared on …" line for every one of
    7 identity files — 14 lines for a flap whose net state at flush is RESOLVED.

    A **recovered flap** for a signature (``coalesce_key``) is:

      1. it has BOTH a ``kind=fire`` and a ``kind=resolve`` record in this
         window (it broke *and* recovered here — not a standalone clear of
         something paged in a prior window, which still renders normally), AND
      2. it is NOT currently in ``signals/firing/`` — the authoritative net
         state. A signature that re-fired after the window's resolve is firing
         again; it stays loud (cardinal invariant: never silence a still-firing
         alert). If the firing set can't be read, this step is a no-op.

    Recovered-flap fire+resolve records are dropped and replaced by ONE
    synthetic summary record per ``(bot_id, catalog_event)`` group, so 7
    identity files on ``evo`` collapse to a single line, not 7. The synthetic
    record carries the rolled-up ``message`` + the group's ``catalog_event``
    (so it buckets into its category and keeps its subscription footer) and no
    ``kind``/``coalesce_key`` (inert — never re-paired or re-counted, idempotent
    if requeued). Original order is otherwise preserved.

    Delivery-only: the Signal store's firing/resolved history is untouched
    (Alerts → History still shows every transition).
    """
    firing = _firing_signatures(shared_dir)
    if firing is None:
        # Couldn't determine net state → fail safe, change nothing.
        return records

    # Bucket record indices by signature, tracking which kinds are present.
    fires_by_key: dict[str, list[int]] = {}
    resolves_by_key: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        key = rec.get("coalesce_key")
        if not key:
            continue
        kind = rec.get("kind")
        if kind == "fire":
            fires_by_key.setdefault(key, []).append(i)
        elif kind == "resolve":
            resolves_by_key.setdefault(key, []).append(i)

    # A recovered flap: fire AND resolve present this window AND not now firing.
    recovered_keys = {
        key
        for key in fires_by_key
        if key in resolves_by_key and key not in firing
    }
    if not recovered_keys:
        return records

    drop: set[int] = set()
    # Group the recovered signatures by (bot_id, catalog_event) so several
    # same-event recoveries on one bot collapse to a single line. Track each
    # group's earliest dropped record so its summary line lands in the same
    # position in the digest (stable order across flushes). Sorting the
    # recovered keys keeps grouping deterministic regardless of set ordering.
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for key in sorted(recovered_keys):
        idxs = fires_by_key[key] + resolves_by_key.get(key, [])
        drop.update(idxs)
        anchor = records[idxs[0]]
        gkey = (anchor.get("bot_id"), anchor.get("catalog_event"))
        g = groups.get(gkey)
        if g is None:
            g = {"keys": set(), "first_idx": min(idxs),
                 "severity": anchor.get("severity")}
            groups[gkey] = g
        g["keys"].add(key)
        g["first_idx"] = min(g["first_idx"], min(idxs))

    # Build the synthetic summary records, keyed by the position of each group's
    # earliest dropped record so the digest order is preserved on emit below.
    synthetic_at: dict[int, list[dict[str, Any]]] = {}
    for gkey, g in groups.items():
        bot_id, catalog_event = gkey
        n = len(g["keys"])
        synthetic = {
            "ts": records[g["first_idx"]].get("ts"),
            "source": "digest_dispatcher",
            "catalog_event": catalog_event,
            "severity": g.get("severity") or "info",
            "message": _recovered_flap_line(catalog_event, bot_id, n),
            # Inert: no kind/coalesce_key, so L1 + this step pass it through on
            # any requeue, and no dedup_key so _dedup_records leaves it alone.
            "recovered_flap_rollup": True,
        }
        synthetic_at.setdefault(g["first_idx"], []).append(synthetic)

    out: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        if i in synthetic_at:
            out.extend(synthetic_at[i])
        if i in drop:
            continue
        out.append(rec)
    return out


def _collapsed_lines(records: list[dict[str, Any]]) -> list[str]:
    """Per-record short-lines, with visually-identical lines collapsed.

    Fallback path for catalog events with no ``_DIGEST_ROLLUP_TEMPLATES``
    entry: list each event's short-line, but when several records render
    to the *same* short-line, emit it once with a ``(×N)`` multiplier.

    This is the second half of the de-dup story. ``_dedup_records``
    collapses records that share a ``dedup_key`` (the upstream re-enqueue
    case); this collapses records whose *rendered text* is identical even
    though their dedup_keys differ — the actual reported bug (16 distinct
    commit shas, one identical title line). First-seen order is preserved.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for rec in records:
        line = _short_line(rec.get("message") or "")
        if line not in counts:
            counts[line] = 0
            order.append(line)
        counts[line] += 1
    out: list[str] = []
    for line in order:
        n = counts[line]
        out.append(f"  • {line}  (×{n})" if n > 1 else f"  • {line}")
    return out


def _bucket_lines(bucket: list[dict[str, Any]]) -> list[str]:
    """Render one category bucket: roll up recurring events, collapse the rest.

    Groups the bucket's records by ``catalog_event`` (first-seen order).
    For an event with >1 record that is keyed in
    ``_DIGEST_ROLLUP_TEMPLATES``, emit a single rolled-up summary line.
    Otherwise fall back to ``_collapsed_lines`` (per-record short-lines
    with identical-text collapse).
    """
    by_event: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for rec in bucket:
        ev = rec.get("catalog_event") or ""
        if ev not in by_event:
            by_event[ev] = []
            order.append(ev)
        by_event[ev].append(rec)

    out: list[str] = []
    for ev in order:
        recs = by_event[ev]
        template = _DIGEST_ROLLUP_TEMPLATES.get(ev)
        if template is not None and len(recs) > 1:
            out.append(f"  • {template.format(count=len(recs))}")
        else:
            out.extend(_collapsed_lines(recs))
    return out


def _subscription_footer_lines(records: list[dict[str, Any]]) -> list[str]:
    """One attribution footer line per distinct subscription in the digest.

    Immediate alerts close with a ``subscription: <key>`` footer that the
    digest path drops (``_short_line`` keeps only the title line). Restore
    attribution here, but with the operator-facing HUMAN label (from the
    catalog) rather than the raw key, plus a manage hint. First-seen order.
    """
    seen: list[str] = []
    labels: dict[str, str] = {}
    for rec in records:
        ev = rec.get("catalog_event")
        if not ev or ev in labels:
            continue
        entry = _cat.by_key(ev)
        if entry is None:
            continue  # catalog drift — no human label to show
        labels[ev] = entry.label
        seen.append(ev)
    return [
        f'— Subscription: "{labels[ev]}" · manage in Reports → Subscriptions'
        for ev in seen
    ]


def _render_digest(records: list[dict[str, Any]], frequency: str) -> str:
    """Build the operator-facing digest body.

    Groups events by catalog category and renders a per-category block.
    Three behaviors keep the digest legible instead of a log dump:

      1. **Topic-named title when single-category** — when every record
         maps to one catalog Category, the title is that category's
         display label + a time-frame (e.g. ``🔄 Updates — past week``).
         Multi-category digests keep the generic ``📋 Weekly digest — N
         events`` header.
      2. **Recurring events roll up** — a catalog_event with several
         records collapses to one summary line (registry template) or to
         a ``(×N)`` multiplier on its short-line.
      3. **Subscription attribution footer** — each distinct subscription
         in the digest is named (human label) with a manage hint.

    Duplicate records (same catalog_event + dedup_key) are collapsed first
    — defense against append-only queue accumulation upstream.
    """
    records = _dedup_records(records)
    grouped: dict[_cat.Category | None, list[dict[str, Any]]] = {}
    for rec in records:
        cat = _category_for(rec.get("catalog_event"))
        grouped.setdefault(cat, []).append(rec)

    # Single-category iff exactly one bucket exists and it's a real
    # (recognized) Category — an all-uncategorized digest stays generic.
    real_cats = [c for c in grouped if c is not None]
    single_cat = real_cats[0] if (len(grouped) == 1 and len(real_cats) == 1) else None
    when = "today" if frequency == "daily" else "past week"

    if single_cat is not None:
        title = f"{_CATEGORY_DIGEST_LABELS.get(single_cat, single_cat.value)} — {when}"
    else:
        header_word = "Daily" if frequency == "daily" else "Weekly"
        total = len(records)
        plural = "" if total == 1 else "s"
        title = f"📋 {header_word} digest — {total} event{plural}"
    lines = [title, ""]

    # Render categories in the catalog's declared order so the digest
    # reads predictably across flushes. The per-category header is
    # redundant in a single-category digest (the title already names it),
    # so suppress it there.
    for cat in _cat.CATEGORY_DISPLAY_ORDER:
        bucket = grouped.get(cat) or []
        if not bucket:
            continue
        if single_cat is None:
            label = _CATEGORY_DIGEST_LABELS.get(cat, cat.value)
            lines.append(f"{label} ({len(bucket)})")
        lines.extend(_bucket_lines(bucket))

    # Catch-all bucket for records whose catalog_event isn't recognized
    # (catalog drift). Rare but render them so they don't silently vanish.
    orphan = grouped.get(None) or []
    if orphan:
        if single_cat is None:
            lines.append(f"❓ Uncategorized ({len(orphan)})")
        lines.extend(_bucket_lines(orphan))

    footer = _subscription_footer_lines(records)
    if footer:
        lines.append("")
        lines.extend(footer)

    return "\n".join(lines)


# Per-message size budget for a single digest chunk. Telegram's hard cap is
# 4096 chars; a bundle over it is rejected with HTTP 400 (a permanent
# failure) AND a multi-thousand-event bundle also blows the gateway's 10s
# send timeout. We pack records into chunks whose RENDERED body stays under
# this budget so each chunk is individually deliverable. Headroom below 4096
# absorbs the "(part k/n)" prefix and a little slack.
_DIGEST_CHUNK_CHAR_BUDGET = 3500
# Hard cap on events per chunk independent of size — bounds the render cost
# of the incremental packing below. Deliverability is governed by the CHAR
# budget; this is generous on purpose so a flood of identical-text events
# (which collapse to a single ``(×N)`` line at render time) packs into a few
# chunks instead of over-splitting into many near-empty messages.
_DIGEST_CHUNK_MAX_EVENTS = 200

# Max chunk-messages a single flush will send. A pathological backlog (the
# live 3594-event flood) would otherwise fan out into ~70 Telegram messages
# at once — a notification storm. Cap it: send this many, requeue the rest,
# let the next tick continue. Normal daily digests are 1-2 chunks so this is
# a no-op for them; it only paces the one-time drain of a huge backlog.
_DIGEST_MAX_CHUNKS_PER_FLUSH = 12


def _chunk_records(
    records: list[dict[str, Any]], frequency: str,
) -> list[list[dict[str, Any]]]:
    """Greedily pack ``records`` into chunks each rendering under the char
    budget (and the per-chunk event cap).

    Records are assumed already de-duplicated (``_dedup_records``). A single
    record whose own rendered body exceeds the budget still gets its own
    chunk — we never drop an event, we just send an over-budget single
    (Telegram may reject it, which then dead-letters via the normal send
    path rather than wedging the whole queue).
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for rec in records:
        trial = current + [rec]
        too_big = (
            len(_render_digest(trial, frequency)) > _DIGEST_CHUNK_CHAR_BUDGET
            or len(trial) > _DIGEST_CHUNK_MAX_EVENTS
        )
        if current and too_big:
            chunks.append(current)
            current = [rec]
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks


def _requeue_records(active: Path, records: list[dict[str, Any]]) -> None:
    """Append un-sent records back onto the active queue (no data loss).

    Append-mode write, matching the dispatcher's own ``_enqueue_digest``
    O_APPEND path — concurrency-safe against a producer enqueuing a fresh
    event mid-flush. Used to put back the events from chunks that failed
    (transiently) or were never attempted, so the next flush retries them.
    """
    if not records:
        return
    active.parent.mkdir(parents=True, exist_ok=True)
    with active.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _unlink_quiet(path: Path) -> None:
    # Best-effort: a source file we already consumed; a missing/locked file is
    # not worth surfacing (the events are already sent/requeued).
    with contextlib.suppress(OSError):
        path.unlink()


# Only adopt a ``.flushing-*`` file as a stranded orphan once it's older than
# this. A fresh one is almost certainly an OTHER flush's in-flight rotation —
# adopting it would double-process its events. The digest daemon fires hourly,
# so 30 min is comfortably longer than any real flush yet far shorter than the
# next tick, guaranteeing a genuinely-crashed flush is recovered next hour.
_ORPHAN_ADOPT_AFTER_SECONDS = 1800


def _stranded_orphans(
    digest_dir: Path, frequency: str, now: datetime,
) -> list[Path]:
    """``.flushing-*`` files old enough to be genuine crash leftovers.

    The embedded ISO timestamp is the rotation time. Files whose stamp is
    within ``_ORPHAN_ADOPT_AFTER_SECONDS`` of ``now`` are skipped — they may
    belong to a concurrent flush still in progress. Unparseable stamps are
    treated as stranded (better to re-send than to strand forever)."""
    out: list[Path] = []
    prefix = f"{frequency}.flushing-"
    for p in sorted(digest_dir.glob(f"{frequency}.flushing-*")):
        stamp = p.name[len(prefix):]
        try:
            rotated_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            stale = (now - rotated_at).total_seconds() >= _ORPHAN_ADOPT_AFTER_SECONDS
        except (ValueError, TypeError):
            stale = True  # unparseable stamp → adopt (don't strand it forever)
        if stale:
            out.append(p)
    return out


def flush(
    shared_dir: Path,
    network: dict[str, Any] | None,
    *,
    frequency: str,
    now: datetime | None = None,
) -> DispatchOutcome | None:
    """Drain the digest queue for ``frequency``, chunked and restart-resilient.

    Returns the dispatcher's :class:`DispatchOutcome` for the LAST chunk
    attempted, or ``None`` when there was nothing to flush.

    The caller decides *when* to flush (cron / launchd). This function is a
    one-shot with these guarantees:

      - **Empty queue → no-op.**
      - **Chunking.** A queue of N events is split into chunks each rendering
        under the Telegram size cap, sent as separate messages. A 3594-event
        backlog no longer dies on a single over-cap / gateway-timeout send.
      - **Retry/backoff.** Each chunk goes through ``dispatcher.send``, whose
        gateway path now retries across a restart window — so a transient
        gateway timeout mid-flush no longer aborts the drain.
      - **Re-drainable on partial failure.** A chunk that fails *transiently*
        (or any chunk after it) is appended back onto the active queue; the
        next flush retries exactly those events. Zero lost events, and the
        rotation file is always consumed (no orphan ``.flushing``).
      - **Dead-letter, not loop.** A chunk that fails *permanently* (bad body
        / chat-not-found) is dead-lettered by ``dispatcher.send`` and NOT
        requeued, so one poison chunk can't wedge the queue forever.
      - **Crash recovery.** Orphan ``.flushing-*`` files from a previous
        interrupted flush are adopted into this run (re-sent) rather than
        stranded — at-most-once-extra delivery in the rare crash window,
        traded for never losing an event.
    """
    if frequency not in ("daily", "weekly"):
        raise ValueError(f"frequency must be 'daily' or 'weekly', got {frequency!r}")
    now = now or datetime.now(timezone.utc)
    shared_dir = Path(shared_dir)

    active = _active_queue_path(shared_dir, frequency)
    digest_dir = _digest_dir(shared_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)

    # Adopt orphaned rotation files from a prior interrupted flush so their
    # events aren't stranded; they're processed alongside this run's fresh
    # rotation. Only files old enough to NOT be a concurrent flush's in-flight
    # rotation are adopted. (Glob BEFORE we create our own .flushing file.)
    orphans = _stranded_orphans(digest_dir, frequency, now)

    flush_iso = _iso_z(now)
    rotated = digest_dir / f"{frequency}.flushing-{flush_iso}"
    rotated_ok = _atomic_rotate(active, rotated)

    source_files = list(orphans)
    if rotated_ok:
        source_files.append(rotated)
    if not source_files:
        return None  # nothing active and no orphans → nothing to flush

    records: list[dict[str, Any]] = []
    for sf in source_files:
        records.extend(_read_jsonl(sf))
    records = _dedup_records(records)
    # L1 — cancel within-grace fire+clear pairs across the WHOLE window
    # before chunking, so a fire and its resolve (which may land in different
    # chunks) are matched as one set. Runs once on the full deduped list; an
    # all-cancelled window collapses to the empty path below (no digest at
    # all — exactly the "nothing to do" silence the spec wants).
    records = _cancel_transient_pairs(shared_dir, records)
    # Net-state roll-up (severity-agnostic) — AFTER L1, BEFORE chunking so a
    # fire and its resolve (which may otherwise land in different chunks) are
    # paired across the whole window. Collapses a condition that broke AND
    # self-healed within the window (net = RESOLVED, not currently firing) into
    # one retrospective line per (bot, catalog_event), instead of dumping the
    # full fire+clear transition log. Composes with L1: L1 already removed the
    # within-grace blips; what survives to here is the alert-severity / past-
    # grace flaps L1 leaves loud.
    records = _rollup_recovered_flaps(shared_dir, records)
    if not records:
        # Source files existed but carried no parseable records — consume
        # them and leave a single ``-empty`` marker (skipped by
        # _last_flush_ts) so we don't keep re-parsing, without firing an
        # empty digest to chat.
        for sf in source_files:
            _unlink_quiet(sf)
        with contextlib.suppress(OSError):
            (digest_dir / f"{frequency}.flushed-{flush_iso}-empty").write_text(
                "", encoding="utf-8",
            )
        return None

    chunks = _chunk_records(records, frequency)
    flush_date = now.astimezone(timezone.utc).date().isoformat()

    unsent: list[dict[str, Any]] = []
    any_sent = False
    last_outcome: DispatchOutcome | None = None
    aborted = False
    for idx, chunk in enumerate(chunks):
        if aborted or idx >= _DIGEST_MAX_CHUNKS_PER_FLUSH:
            # Either a prior chunk failed transiently (don't keep hammering a
            # sick gateway this tick) or we've hit the per-flush message cap
            # (don't storm the operator with a huge backlog at once). Requeue
            # the rest; the next flush picks them up.
            unsent.extend(chunk)
            continue
        body = _render_digest(chunk, frequency)
        if len(chunks) > 1:
            body = f"📋 (part {idx + 1}/{len(chunks)})\n{body}"
        outcome = _dispatch.send(
            shared_dir=shared_dir,
            network=network,
            source="digest_dispatcher",
            message=body,
            severity=Severity.INFO,
            # Distinct per-chunk dedup_key so chunk 2 isn't suppressed as a
            # cooldown/identical repeat of chunk 1 within the same flush.
            dedup_key=f"digest_dispatcher/{frequency}/{flush_date}/{flush_iso}/{idx}",
            # Subscription-completeness (spec-subscription-completeness-
            # 2026-06-24): the digest bundle is a meta message ABOUT the
            # alerting system, so it carries the meta.digest handle (the
            # constituent events were already subscription-filtered at
            # enqueue; this is the bundle envelope). meta.digest is
            # IMMEDIATE-only + default-on, so binding it does NOT re-enqueue
            # this bundle into its own queue (only daily/weekly-digest
            # frequencies re-enqueue) and does NOT suppress an enabled
            # operator's digest.
            catalog_event="meta.digest",
            now=now,
        )
        last_outcome = outcome
        if outcome.result == DispatchResult.SENT:
            any_sent = True
            continue
        if (
            outcome.result == DispatchResult.FAILED
            and outcome.is_permanent_failure
        ):
            # dispatcher.send already dead-lettered these events (durable +
            # surfaced). Consume them — requeuing a poison chunk would loop
            # forever. Keep going: a later chunk may still be deliverable.
            continue
        # Transient failure / suppressed / no-recipient → requeue this chunk
        # and stop attempting further chunks this tick.
        unsent.extend(chunk)
        aborted = True

    # Reconcile. Requeue FIRST (no-loss priority), then consume the source
    # files. A crash between these leaves the source files as orphans → next
    # flush re-adopts them (at-most-once-extra), never losing events.
    _requeue_records(active, unsent)
    for sf in source_files:
        _unlink_quiet(sf)

    # Leave a delivered-flush marker only when something actually went out,
    # so digest_health.last_flush_ts reflects real deliveries. ``-partial``
    # records that some events were requeued.
    if any_sent:
        suffix = "-partial" if unsent else ""
        marker = digest_dir / f"{frequency}.flushed-{flush_iso}{suffix}"
        with contextlib.suppress(OSError):
            marker.write_text(
                json.dumps({
                    "chunks": len(chunks),
                    "events": len(records),
                    "requeued": len(unsent),
                }) + "\n",
                encoding="utf-8",
            )

    return last_outcome


def _local_now(network: dict[str, Any] | None, utc_now: datetime) -> datetime:
    """Convert ``utc_now`` to the operator's local timezone.

    Uses ``resolve_pod_timezone`` so an explicit ``network.timezone``
    override wins, then ``/etc/localtime``, then ``America/Los_Angeles``.
    Falls back to UTC if zoneinfo is unavailable or parsing fails.
    """
    if ZoneInfo is None:
        return utc_now.astimezone(timezone.utc)
    try:
        from ..config import resolve_pod_timezone
        tz_name = resolve_pod_timezone(network or {})
        return utc_now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return utc_now.astimezone(timezone.utc)


# Default weekday for weekly digests. 0=Monday per Python convention.
DEFAULT_WEEKLY_DAY = 0


def flush_if_gated(
    shared_dir: Path,
    network: dict[str, Any] | None,
    *,
    frequency: str,
    now: datetime | None = None,
    weekly_day: int = DEFAULT_WEEKLY_DAY,
) -> DispatchOutcome | None:
    """Flush only if local time matches the operator's digest window.

    Daily digest: fires when ``local_hour == digest_hour_local``.
    Weekly digest: fires when ``local_weekday == weekly_day`` AND
    ``local_hour == digest_hour_local``.

    Returns the dispatcher outcome on a real flush, or ``None`` when
    gated out (no chat push, no queue rotation). Designed to be called
    every hour by a LaunchDaemon — exactly one tick per day (or per
    week) hits the gate and actually drains the queue.
    """
    if frequency not in ("daily", "weekly"):
        raise ValueError(f"frequency must be 'daily' or 'weekly', got {frequency!r}")
    now = now or datetime.now(timezone.utc)
    shared_dir = Path(shared_dir)

    digest_hour = _subs.read_digest_hour_local(shared_dir)
    local = _local_now(network, now)
    if local.hour != digest_hour:
        return None
    if frequency == "weekly" and local.weekday() != weekly_day:
        return None
    return flush(shared_dir, network, frequency=frequency, now=now)


# ── Pipeline health snapshot (read-only) ────────────────────────────────────
#
# The Dispatcher Health panel (Reports → Subscriptions) needs to surface a
# stuck digest queue so a silently-non-draining delivery mode can't hide
# behind "✓ all deliveries succeeded". A digest queue with events older than
# one flush-interval-plus-slack hasn't drained on schedule — that's the
# unambiguous "digests aren't flowing" signal (the live 2026-06-12 failure:
# 142 daily events stuck since Jun 10, zero successful flushes ever, while
# the health panel showed all-green).
#
# Queue *depth* alone is NOT a problem — a daily digest legitimately
# accumulates events all day and drains at the digest hour. The staleness
# trigger is the AGE of the oldest still-pending event, not the count.

# Daily fires once every ~24h; the 2h slack absorbs the gap between an
# event's enqueue and the next digest window.
_DAILY_STALE_SECONDS = 26 * 3600
# Weekly fires once every ~7d; 1d slack for the same reason.
_WEEKLY_STALE_SECONDS = 8 * 86_400

_STALE_SECONDS_BY_FREQUENCY: dict[str, int] = {
    "daily": _DAILY_STALE_SECONDS,
    "weekly": _WEEKLY_STALE_SECONDS,
}


def _is_parseable_ts(ts: str | None) -> bool:
    """True iff ``ts`` is a non-empty ISO timestamp the UI can format.

    The flush markers embed ``_iso_z(now)`` (``…T15:30:00Z``), which is
    valid ISO — but a corrupt or legacy marker filename could yield a
    stamp the client's ``new Date(ts)`` parses to ``Invalid Date`` → the
    "last delivered NaNd ago" banner. Gate the server side so only a
    genuinely parseable value reaches the client as a timestamp.
    """
    if not ts:
        return False
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        return False


def _pending_stats(path: Path, now: datetime) -> tuple[int, str | None, float | None]:
    """``(line_count, oldest_ts_iso, oldest_age_seconds)`` for a queue file.

    Read-only line scan. ``oldest_ts_iso`` / ``oldest_age_seconds`` are
    ``None`` when the queue is empty or no line carries a parseable ``ts``.
    """
    count = 0
    oldest_dt: datetime | None = None
    oldest_iso: str | None = None
    for rec in _read_jsonl(path):
        count += 1
        ts_raw = rec.get("ts")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if oldest_dt is None or ts < oldest_dt:
            oldest_dt = ts
            oldest_iso = str(ts_raw)
    age = (now - oldest_dt).total_seconds() if oldest_dt is not None else None
    return count, oldest_iso, age


def _last_flush_ts(digest_dir: Path, frequency: str) -> str | None:
    """Newest delivered-flush timestamp for ``frequency``, or ``None``.

    Parsed from the ``{frequency}.flushed-<iso>`` archive filenames that
    :func:`flush` leaves on a successful SENT. ``-empty`` archives (queue
    rotated but nothing sent) are skipped — they don't represent a digest
    the operator actually received. The embedded iso is fixed-width
    UTC-``Z`` so a lexical max is a chronological max.
    """
    if not digest_dir.exists():
        return None
    prefix = f"{frequency}.flushed-"
    best: str | None = None
    for p in digest_dir.glob(f"{frequency}.flushed-*"):
        stamp = p.name[len(prefix):]
        if stamp.endswith("-empty"):
            continue
        if best is None or stamp > best:
            best = stamp
    return best


def digest_health(
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only health snapshot of the digest pipeline.

    Returns, per frequency, the pending-queue depth, the age of the oldest
    still-pending event, the timestamp of the last delivered flush, and a
    ``stuck`` flag (pending events that are older than one
    flush-interval-plus-slack, or a non-empty queue that has never been
    flushed). The top-level ``stuck`` is true if either frequency is stuck.

    Used by the Dispatcher Health route; pure and side-effect-free so it
    can be called on every panel load without touching the queue.
    """
    now = now or datetime.now(timezone.utc)
    shared_dir = Path(shared_dir)
    digest_dir = _digest_dir(shared_dir)

    out: dict[str, Any] = {}
    any_stuck = False
    for frequency, stale_seconds in _STALE_SECONDS_BY_FREQUENCY.items():
        path = _active_queue_path(shared_dir, frequency)
        count, oldest_iso, oldest_age = _pending_stats(path, now)
        last_flush = _last_flush_ts(digest_dir, frequency)
        # Only count a real, parseable flush stamp. A malformed marker must
        # never reach the UI as a timestamp (the "last delivered NaNd ago"
        # bug) AND must not pass as "the daemon has flushed" for the
        # diagnosis below — a corrupt stamp is no evidence of a live daemon.
        if not _is_parseable_ts(last_flush):
            last_flush = None

        # Stuck = events are rotting. The oldest-pending-age measures it
        # directly. The second clause catches the live failure shape — a
        # non-empty queue with unparseable timestamps that has never been
        # flushed — so a malformed ts can't mask a dead pipeline.
        stuck = bool(
            count > 0 and oldest_age is not None and oldest_age > stale_seconds
        )
        if count > 0 and oldest_age is None and last_flush is None:
            stuck = True

        # Honest diagnosis when stuck. The old banner unconditionally blamed
        # the flush daemon ("Check the digest-flush daemon"), which sent the
        # operator chasing a HEALTHY daemon during the 2026-06 incident — the
        # daemon flushed daily on schedule; the queue was just full of
        # un-coalesced records (4804/4827 had no dedup_key) that the once-daily,
        # chunk-capped flush couldn't drain. Distinguish the two failure shapes:
        #   * never_flushed — a non-empty queue that has NEVER been delivered.
        #     The daemon really is down / mis-scheduled / has no recipient.
        #   * queue_growing — the daemon HAS flushed (last_flush present) but
        #     the queue is still aging past the stale threshold: throughput-
        #     bound, typically recurring signals re-enqueuing faster than one
        #     daily flush coalesces them. Point at the producers, not the daemon.
        diagnosis: str | None = None
        if stuck:
            diagnosis = "never_flushed" if last_flush is None else "queue_growing"

        out[frequency] = {
            "pending": count,
            "oldest_pending_ts": oldest_iso,
            "oldest_pending_age_hours": (
                round(oldest_age / 3600, 1) if oldest_age is not None else None
            ),
            # Already normalized above: a real parseable stamp or None.
            "last_flush_ts": last_flush,
            "stuck": stuck,
            "diagnosis": diagnosis,
        }
        any_stuck = any_stuck or stuck

    out["stuck"] = any_stuck
    return out


def _cli(argv: list[str]) -> int:
    """``python3 -m evolve_admin.alerts.digest_dispatcher`` entrypoint.

    Two modes:

      - default: flush unconditionally. ``--frequency`` required.
        For manual invocation or operator-triggered flushes.

      - ``--hourly``: self-gating tick. Tries both daily and weekly
        flushes; each returns None unless its local-time window matches.
        Designed to be invoked from a LaunchDaemon that fires every
        hour at :00. Exactly one tick per day (or per week) hits the
        gate; the rest are no-ops.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Drain the alert digest queue.")
    parser.add_argument("--frequency", default=None, choices=("daily", "weekly"),
                        help="One-shot flush at the given frequency (manual mode).")
    parser.add_argument("--hourly", action="store_true",
                        help="Self-gating hourly tick — flush only when local "
                             "time matches the configured digest window.")
    parser.add_argument("--shared-dir", required=True,
                        help="Pod-wide shared dir (e.g. /Users/Shared/evolve).")
    parser.add_argument("--network-json", default=None,
                        help="Optional path to network.json (defaults to <shared-dir>/network.json).")
    args = parser.parse_args(argv)

    if not args.hourly and not args.frequency:
        parser.error("either --frequency or --hourly is required")
    if args.hourly and args.frequency:
        parser.error("--hourly and --frequency are mutually exclusive")

    shared_dir = Path(args.shared_dir)
    # network.json lives INSIDE the shared dir (CANONICAL_NETWORK_JSON =
    # shared_dir / "network.json"), not in its parent. The earlier
    # ``shared_dir.parent`` default pointed at /Users/Shared/network.json,
    # which doesn't exist — so every flush loaded network=None and the
    # dispatcher returned no_recipient, leaving daily/weekly digests stuck
    # in the queue undelivered (observed 2026-06-12: 142 daily events
    # piled up, never sent).
    network_path = (Path(args.network_json) if args.network_json
                    else shared_dir / "network.json")
    network: dict[str, Any] | None = None
    if network_path.exists():
        try:
            network = json.loads(network_path.read_text(encoding="utf-8"))
        except Exception:
            network = None

    if args.hourly:
        # Self-gating tick: try both. Each returns None outside its window.
        outcomes = []
        for freq in ("daily", "weekly"):
            outcome = flush_if_gated(shared_dir, network, frequency=freq)
            outcomes.append((freq, outcome))
        any_sent = False
        for freq, outcome in outcomes:
            if outcome is None:
                continue
            any_sent = any_sent or outcome.result == DispatchResult.SENT
            print(f"[digest_dispatcher] {freq}: {outcome.result.value}"
                  f"{'' if outcome.error is None else f' ({outcome.error})'}")
        if not any(o for _, o in outcomes):
            print("[digest_dispatcher] hourly: gated out (outside digest window).")
        return 0

    outcome = flush(shared_dir, network, frequency=args.frequency)
    if outcome is None:
        print(f"[digest_dispatcher] {args.frequency}: queue empty; nothing to flush.")
        return 0
    print(f"[digest_dispatcher] {args.frequency}: {outcome.result.value}"
          f"{'' if outcome.error is None else f' ({outcome.error})'}")
    return 0 if outcome.result == DispatchResult.SENT else 1


# ── LaunchDaemon ───────────────────────────────────────────────────────────


DIGEST_LABEL = "ai.evolve.evolve.digest-flush"
DIGEST_PLIST = f"/Library/LaunchDaemons/{DIGEST_LABEL}.plist"
DIGEST_INTERVAL_SECONDS = 3600   # hourly


def _digest_job_spec(
    *,
    evolve_admin_path: str | None = None,
    interval_seconds: int = DIGEST_INTERVAL_SECONDS,
    log_dir: str | None = None,
):
    """The digest-flush LaunchDaemon spec.

    Hourly StartInterval with a small jitter so we don't lockstep with
    other hourly daemons. ``--hourly`` makes the script self-gate to
    the operator's configured digest hour; outside that window the
    tick is a cheap no-op.

    No ``RunAtLoad`` — first flush should happen at the next digest
    hour, not at install time (the queue is usually empty on install).

    Paths default to the active platform profile (W10-D): macOS renders
    /Users/Shared byte-identically (parity golden under the conftest MACOS
    pin); a Linux pod renders /var/lib, so the systemd unit carries no
    /Users leak.
    """
    from platform_profile import get_profile

    from ..runtime import JobSpec
    _prof = get_profile()
    if evolve_admin_path is None:
        evolve_admin_path = _prof.venv_evolve_admin
    if log_dir is None:
        log_dir = f"{_prof.shared_dir_default}/logs"
    return JobSpec(
        label=DIGEST_LABEL,
        program_args=[evolve_admin_path, "digest-flush", "--hourly"],
        user="evolve",
        start_interval=interval_seconds,
        run_at_load=False,
        stdout_path=f"{log_dir}/digest-flush.log",
        stderr_path=f"{log_dir}/digest-flush.err.log",
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        jitter_seconds=60,
    )


def render_plist(
    *,
    evolve_admin_path: str | None = None,
    interval_seconds: int = DIGEST_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> str:
    """Render the digest-flush LaunchDaemon plist as a string.

    Pure — caller writes to disk. See :func:`_digest_job_spec` for the
    job semantics (incl. the profile-keyed path defaults).
    """
    from ..runtime import render_launchd_plist
    return render_launchd_plist(_digest_job_spec(
        evolve_admin_path=evolve_admin_path,
        interval_seconds=interval_seconds,
        log_dir=log_dir,
    ))


def install_launchd() -> bool:
    """Install ai.evolve.evolve.digest-flush. Idempotent.

    The daemon's hourly tick is a cheap no-op unless local time matches
    the operator's configured ``digest_hour_local`` — installing it
    before any operator opts into digest mode is safe (empty queue
    + outside-window gate = noop every hour).

    Goes through the Scheduler seam. ``install()`` skips the
    bootout+bootstrap bounce when the on-disk plist is byte-identical;
    that's fine for this hourly tick daemon (each tick is a fresh
    process), EXCEPT when the job isn't actually registered with
    launchd (an earlier bootstrap failed after the plist write, or a
    manual bootout). The legacy ritual re-registered unconditionally,
    so compensate with remove+install in that case.
    """
    import logging
    log = logging.getLogger(__name__)
    from ..runtime import get_scheduler

    sched = get_scheduler()
    res = sched.install(_digest_job_spec())
    if res.ok and res.skipped and not sched.status(DIGEST_LABEL)["managed"]:
        sched.remove(DIGEST_LABEL)
        res = sched.install(_digest_job_spec())
    if not res.ok:
        log.warning("digest-flush install: %s", res.message)
        return False
    log.info("digest-flush install: bootstrapped %s every %ds",
             DIGEST_LABEL, DIGEST_INTERVAL_SECONDS)
    return True


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
