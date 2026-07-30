"""standing_alerts — the daily *inventory* line for the firing-Signal backlog.

``signal_notifier`` announces **transitions**: a Signal that starts firing,
and one that clears. By design it says nothing about **inventory** — the set
of Signals that are firing right now. Its cold-start guard
(``_sync_firing_as_known``) makes that gap permanent for whatever backlog
existed the first time the notifier ran: those signatures are recorded as
already-alerted without a push, and because they stay firing they never
re-fire, so they are never announced. The guard is correct (it exists to
prevent a flood, and to stop the suppressed-log runaway that filled 306 MB
on 2026-06-01) — what was missing is a channel that reports the standing set.

Measured on both live pods 2026-07-29 before this module existed:

===========  ==============  ================  ======================
pod          firing Signals  never announced   oldest never-announced
===========  ==============  ================  ======================
Linux VPS    36              34                2026-06-23
Mac mini     99              95                —
===========  ==============  ================  ======================

Where this runs
^^^^^^^^^^^^^^^
Nowhere new. ``pod_report`` already builds the one message the operator
reads every morning and dispatches it on a **natural daily cadence** via
``summaries.daily_pod_report`` (``Frequency.DAILY``, default-on) — a
catalog event that demonstrably delivers today. This module contributes a
compact section to that body; ``pod_report.run_report`` calls
:func:`build_section`. No new daemon, no new catalog event, and in
particular *not* a ``DAILY_DIGEST``-mapped event (the shape that left
``repo_puller_sudoers`` firing 12× over 24h with ``deliveries: []``).

Cadence
^^^^^^^
Once per ``alerts.standing_alerts.min_interval_hours`` (default 24), gated
on a watermark in ``{shared_dir}/signals/standing-digest-state.json`` so
the section is emitted at most once per window even though the pod-report
LaunchDaemon ticks hourly. Only a *scheduled* pod-report label advances the
watermark (see ``_SCHEDULED_LABELS``); ad-hoc runs — ``--force`` ("Manual"),
``--dry-run`` ("Dry-run"), and the admin UI's ``/api/reports-alerts/status``
poll ("Live") — render the current set but leave the window untouched, so
looking at the report never eats the operator's next scheduled section.

Content
^^^^^^^
Short enough to read on a phone (``docs/principle-plex-test.md``): a total,
a severity breakdown, the oldest age, how the set changed since the last
report, an explicit **never-announced** count, and the worst ``top_n``
entries — each with its own age and its *existing* remediation line
(``details.fix_steps`` → ``remediation.label`` → ``details.deeplink``, and
an honest "no fix on file" when there is none — never a fabricated one,
per ``docs/principle-alerts-explain-and-remediate.md``). The full list
lives on the Alerts page, which every rendering points at.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import _config_lookup
# _clean_title strips the producer-baked "{bot_id}:" prefix and the
# "({check_id}):" parenthetical that would otherwise double up in the
# rendered line. Shared with signal_notifier so both surfaces show the
# same headline for the same Signal.
from .signal_notifier import _clean_title

_log = logging.getLogger(__name__)

# pod_report labels that represent a real SCHEDULED delivery to the operator.
# ``should_run`` returns "Daily" for every scheduled cadence (daily / weekdays /
# weekly); "Weekly" is accepted for forward-compat if that ever splits.
#
# Everything else is ad-hoc: it renders the current standing set (an operator
# looking at the report wants the truth) but does NOT advance the window
# watermark. The allowlist is deliberately inverted rather than a deny-list of
# known ad-hoc labels, because a label we don't recognize must never be able to
# eat the operator's next scheduled section. Concrete case:
# ``/api/reports-alerts/status`` calls ``run_report(label="Live")`` on every
# admin-UI poll, and ``--force`` uses "Manual", ``--dry-run`` uses "Dry-run".
# With a deny-list, opening the Reports page in the morning would silently
# consume that day's standing section.
_SCHEDULED_LABELS = frozenset({"Daily", "Weekly"})

_DEFAULT_MIN_INTERVAL_HOURS = 24
_DEFAULT_TOP_N = 3

# Per-entry caps. The whole section has to survive being read on a phone,
# so a long producer title or a multi-sentence fix step is clipped rather
# than allowed to wrap for five lines.
_TITLE_CHARS = 64
_FIX_CHARS = 78

# Upper bound on the signature list we persist for the day-over-day delta.
# Past this, the delta is DROPPED rather than computed from a truncated set:
# a partial previous set would report the truncated tail as "cleared" and
# the same signatures as "new" every single day. Counts and the listing are
# unaffected.
_MAX_TRACKED_SIGNATURES = 500

# Producers whose Signals are counted in the inventory but never listed by
# name. ``pod_report`` mirrors its OWN report lines into the Signal store, so
# a pod_report Signal in the shortlist would print the same finding twice in
# one message — once as a bucket line above, once here. They stay in the
# totals (the inventory must agree with the Alerts page) and remain eligible
# to raise ``overall``; they just don't take a listing slot away from a
# condition the message hasn't already covered.
_LISTING_EXCLUDED_PRODUCERS = frozenset({"pod_report"})

_SEVERITY_ORDER = ("alert", "warn", "info")
_SEVERITY_RANK = {sev: i for i, sev in enumerate(reversed(_SEVERITY_ORDER))}
_EMOJI_BY_SEVERITY = {"alert": "🔴", "warn": "⚠️", "info": "ℹ️"}

_LEADING_STEP_NUMBER = re.compile(r"^\s*\d+\s*[.)]\s*")


def _signals_store() -> Any:
    """Lazy import — evolve-analyzer is installed at runtime, and the
    admin package must import cleanly without it (same contract as
    ``signal_notifier._signals_store``)."""
    return importlib.import_module("signals.store")


# ── Config ──────────────────────────────────────────────────────────────────


def _read_enabled(shared_dir: Path) -> bool:
    return bool(_config_lookup.lookup(
        shared_dir, "alerts.standing_alerts.enabled", True,
    ))


def _read_min_interval_hours(shared_dir: Path) -> int:
    raw = _config_lookup.lookup(
        shared_dir,
        "alerts.standing_alerts.min_interval_hours",
        _DEFAULT_MIN_INTERVAL_HOURS,
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL_HOURS


def _read_top_n(shared_dir: Path) -> int:
    raw = _config_lookup.lookup(
        shared_dir, "alerts.standing_alerts.top_n", _DEFAULT_TOP_N,
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TOP_N


# ── Watermark state ─────────────────────────────────────────────────────────


def state_path(shared_dir: Path) -> Path:
    return shared_dir / "signals" / "standing-digest-state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_path(shared_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1}
    return data if isinstance(data, dict) else {"version": 1}


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    p = state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ── Notifier state (the "was this ever announced?" source of truth) ─────────


def _load_notifier_state(shared_dir: Path) -> dict[str, Any]:
    """Read ``notifier-state.json`` through signal_notifier's own loader so
    the path and entry shape stay defined in exactly one place."""
    from . import signal_notifier as _sn
    state = _sn._load_state(shared_dir)
    sigs = state.get("signatures")
    return sigs if isinstance(sigs, dict) else {}


def _announced(sig: Any, entry: dict[str, Any]) -> bool:
    """True iff this exact Signal was actually pushed to the operator.

    Two positive proofs, in order of directness:

    1. ``Signal.deliveries`` is non-empty — a producer recorded a real
       dispatch against this Signal.
    2. ``notifier-state.json`` holds a fire-push for **this** signal id and
       that entry was NOT synthesized by the cold-start guard.

    The ``cold_start_synced`` exclusion is the whole point: those entries
    say "we decided not to tell the operator", and treating them as
    announcements is exactly the blind spot that hid 95 firing Signals on
    the mini. Entries with only ``permanent_failure_signal_id`` (every push
    for this id failed 4xx) are likewise not announcements.
    """
    if getattr(sig, "deliveries", None):
        return True
    if entry.get("cold_start_synced"):
        return False
    return bool(
        entry.get("alerted_for_signal_id") == sig.id
        and entry.get("last_fire_pushed_at")
    )


# ── Collection ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StandingEntry:
    """One firing Signal, reduced to what the digest line needs."""

    signal_id: str
    signature: str
    producer: str
    severity: str
    bot_id: str | None
    title: str
    age_seconds: float
    fix: str
    announced: bool
    cold_start_silenced: bool


@dataclass(frozen=True)
class StandingSummary:
    total: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    never_announced: int = 0
    cold_start_silenced: int = 0
    oldest_age_seconds: float = 0.0
    top: tuple[StandingEntry, ...] = ()
    remaining: int = 0
    signatures: tuple[str, ...] = ()
    # Day-over-day delta vs the previously emitted section. ``None`` when
    # there is no previous emission to compare against.
    new_since_last: int | None = None
    cleared_since_last: int | None = None
    # Highest severity among the never-announced entries (over the WHOLE
    # set, not just the shortlist), or None when everything standing has
    # already been announced. Drives pod_report's ``overall`` contribution:
    # an alert-severity condition the operator has never been told about is
    # unacknowledged breakage, so it must survive a ``notify_on: red_only``
    # gate.
    worst_never_announced_severity: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    window = text[:limit]
    pos = window.rfind(" ")
    if pos > limit // 2:
        return window[:pos].rstrip() + "…"
    return window + "…"


def _fix_line(sig: Any) -> str:
    """The Signal's OWN remediation, reduced to one line.

    Never invents advice — ``docs/principle-alerts-explain-and-remediate.md``
    treats hallucinated guidance as a bug. When the producer attached
    nothing actionable we say so and point at the investigation surface.
    """
    details = getattr(sig, "details", None) or {}
    steps = str(details.get("fix_steps") or "").strip()
    if steps:
        first = _LEADING_STEP_NUMBER.sub("", steps.splitlines()[0]).strip()
        if first:
            return _clip(first, _FIX_CHARS)
    rem = getattr(sig, "remediation", None)
    label = str(getattr(rem, "label", "") or "").strip() if rem else ""
    if label:
        return _clip(f"one-click fix on the Alerts page: {label}", _FIX_CHARS)
    deeplink = str(details.get("deeplink") or "").strip()
    if deeplink:
        return _clip(f"details at {deeplink}", _FIX_CHARS)
    # Honest that there is no action on file. Kept short deliberately: this
    # is the most-repeated string in the section, and the tail line already
    # points at the Alerts page.
    return "no fix on file"


def _age_phrase(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(0, int(seconds // 60))}m"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86_400)}d"


def _sort_key(entry: StandingEntry) -> tuple:
    """Severity first, then age (oldest first) — the operator's triage order.
    Never-announced breaks the tie so an invisible condition outranks one
    they have already seen."""
    return (
        -_SEVERITY_RANK.get(entry.severity, 0),
        0 if not entry.announced else 1,
        -entry.age_seconds,
    )


def collect(
    shared_dir: Path,
    *,
    now: datetime | None = None,
    top_n: int | None = None,
) -> StandingSummary:
    """Build the standing-alerts summary from the firing Signal store.

    Read-only: never writes to a Signal file, never dispatches. Producers on
    ``signal_notifier``'s direct-dispatch deny-list are included — they are
    genuinely firing and belong in an inventory, even though their
    *transitions* are announced on their own path.
    """
    now = now or datetime.now(timezone.utc)
    top_n = _read_top_n(shared_dir) if top_n is None else max(1, int(top_n))

    try:
        store = _signals_store()
    except Exception as exc:  # noqa: BLE001 — analyzer absent (unit-test / lint env)
        _log.debug("standing_alerts: signals store unavailable: %s", exc)
        return StandingSummary()

    notifier_sigs = _load_notifier_state(shared_dir)

    entries: list[StandingEntry] = []
    by_severity: dict[str, int] = {}
    for sig in store.iter_signals(shared_dir, subdirs=("firing",)):
        entry_state = notifier_sigs.get(sig.signature) or {}
        created = _parse_iso(getattr(sig, "created_at", None))
        age = max(0.0, (now - created).total_seconds()) if created else 0.0
        announced = _announced(sig, entry_state)
        entries.append(StandingEntry(
            signal_id=sig.id,
            signature=sig.signature,
            producer=sig.producer,
            severity=sig.severity,
            bot_id=getattr(sig, "bot_id", None),
            title=_clip(
                _clean_title(getattr(sig, "title", "") or "", getattr(sig, "bot_id", None))
                or sig.type,
                _TITLE_CHARS,
            ),
            age_seconds=age,
            fix=_fix_line(sig),
            announced=announced,
            cold_start_silenced=bool(
                not announced and entry_state.get("cold_start_synced")
            ),
        ))
        by_severity[sig.severity] = by_severity.get(sig.severity, 0) + 1

    if not entries:
        return StandingSummary()

    entries.sort(key=_sort_key)
    prev = _load_state(shared_dir)
    prev_sigs = prev.get("last_signatures")
    prev_set: set[str] | None = (
        set(str(s) for s in prev_sigs) if isinstance(prev_sigs, list) else None
    )
    current_set = {e.signature for e in entries}

    worst_unannounced: str | None = None
    for e in entries:
        if e.announced:
            continue
        if worst_unannounced is None or (
            _SEVERITY_RANK.get(e.severity, 0)
            > _SEVERITY_RANK.get(worst_unannounced, 0)
        ):
            worst_unannounced = e.severity

    listable = [
        e for e in entries if e.producer not in _LISTING_EXCLUDED_PRODUCERS
    ]
    top = tuple(listable[:top_n])

    return StandingSummary(
        total=len(entries),
        by_severity=by_severity,
        never_announced=sum(1 for e in entries if not e.announced),
        cold_start_silenced=sum(1 for e in entries if e.cold_start_silenced),
        oldest_age_seconds=max(e.age_seconds for e in entries),
        top=top,
        remaining=max(0, len(entries) - len(top)),
        signatures=tuple(sorted(current_set)),
        new_since_last=(
            None if prev_set is None else len(current_set - prev_set)
        ),
        cleared_since_last=(
            None if prev_set is None else len(prev_set - current_set)
        ),
        worst_never_announced_severity=worst_unannounced,
    )


# ── Rendering ───────────────────────────────────────────────────────────────


def render(summary: StandingSummary) -> str:
    """Render the section body, or ``""`` for an empty backlog.

    Plain text on purpose: ``pod_report`` hands this to the dispatcher as a
    catalog **payload** value, and ``catalog.render_event`` HTML-escapes
    every payload value on the way out. Pre-escaping here would
    double-escape.
    """
    if summary.is_empty:
        return ""

    plural = "s" if summary.total != 1 else ""
    head = f"🔔 Standing alerts — {summary.total} still open"
    if summary.never_announced:
        head += f", {summary.never_announced} never announced here"

    facts: list[str] = [
        f"{summary.by_severity[sev]} {sev}"
        for sev in _SEVERITY_ORDER
        if summary.by_severity.get(sev)
    ]
    facts.append(f"oldest {_age_phrase(summary.oldest_age_seconds)}")
    delta = _delta_phrase(summary)
    if delta:
        facts.append(delta)

    lines = [head, " · ".join(facts)]

    if summary.cold_start_silenced:
        lines.append(
            f"{summary.cold_start_silenced} of these were already open when "
            "alerting started, so no message was ever sent for them."
        )

    for e in summary.top:
        who = f"{e.bot_id}: " if e.bot_id else ""
        emoji = _EMOJI_BY_SEVERITY.get(e.severity, "⚠️")
        lines.append(
            f"{emoji} {who}{e.title} · {_age_phrase(e.age_seconds)} · {e.fix}"
        )

    if summary.remaining and summary.top:
        lines.append(
            f"+{summary.remaining} more standing alert{'s' if summary.remaining != 1 else ''}"
            " — Alerts → Active"
        )
    else:
        # Either everything standing is listed above, or nothing was listable
        # (every standing Signal came from a producer already itemized in this
        # same message) — "+N more" would be misleading in the second case.
        lines.append(f"Full list: Alerts → Active ({summary.total} alert{plural})")
    return "\n".join(lines)


def _delta_phrase(summary: StandingSummary) -> str:
    if summary.new_since_last is None:
        return ""
    parts: list[str] = []
    if summary.new_since_last:
        parts.append(f"+{summary.new_since_last} new")
    if summary.cleared_since_last:
        parts.append(f"-{summary.cleared_since_last} cleared")
    return " ".join(parts) if parts else "unchanged since the last report"


# ── Window gate + entrypoint ────────────────────────────────────────────────


def _within_window(shared_dir: Path, now: datetime) -> bool:
    """True when a section was already emitted inside the current window.

    Fails OPEN (returns False → emit) on anything it can't trust: no
    watermark, an unparseable one, or a watermark in the *future* (a clock
    jump, or a state file restored from a backup). Fail-closed here would
    mean silently withholding the section for up to a whole window, which is
    the exact failure mode this module exists to remove.
    """
    hours = _read_min_interval_hours(shared_dir)
    if hours <= 0:
        return False
    last = _parse_iso(_load_state(shared_dir).get("last_emitted_at"))
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = now - last
    if elapsed < timedelta(0):
        return False
    return elapsed < timedelta(hours=hours)


def mark_emitted(
    shared_dir: Path, summary: StandingSummary, *, now: datetime,
) -> None:
    """Advance the watermark and remember the set we just reported."""
    state = _load_state(shared_dir)
    state["version"] = 1
    state["last_emitted_at"] = now.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    state["last_total"] = summary.total
    state["last_never_announced"] = summary.never_announced
    if len(summary.signatures) <= _MAX_TRACKED_SIGNATURES:
        state["last_signatures"] = list(summary.signatures)
    else:
        # Too many to track honestly — drop the key so the next run reports
        # no delta rather than a wrong one.
        state.pop("last_signatures", None)
        state["last_signatures_omitted"] = len(summary.signatures)
    _save_state(shared_dir, state)


def build_section(
    shared_dir: Path,
    *,
    label: str = "Daily",
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """The single call ``pod_report`` makes.

    Returns ``(section_text, worst_never_announced_severity)``. An empty
    ``section_text`` means "contribute nothing" — an empty backlog, the
    feature disabled, or a section already emitted inside this window. The
    second element is ``None`` unless at least one *never-announced* entry
    made the shortlist; ``pod_report`` uses it so a standing invisible
    ``alert`` still clears a ``notify_on: red_only`` gate.

    Idempotent: a second call inside the window returns ``("", None)``.

    The watermark advances when the section is *assembled*, not when the
    pod report is confirmed delivered — same choice pod_report already makes
    for its since-last-digest watermark. A failed dispatch therefore costs
    one day's section, which is cheap because the set is *standing* (it is
    still there tomorrow). Advancing on delivery instead would double-report
    on every DEFERRED/BATCHED outcome, where the message did reach the
    operator via the digest but the result was not ``SENT``.
    """
    now = now or datetime.now(timezone.utc)
    if not _read_enabled(shared_dir):
        return "", None
    ad_hoc = label not in _SCHEDULED_LABELS
    if not ad_hoc and _within_window(shared_dir, now):
        return "", None

    summary = collect(shared_dir, now=now)
    text = render(summary)
    if not text:
        # Nothing standing. Deliberately do NOT advance the watermark: the
        # next non-empty backlog should be reported immediately rather than
        # waiting out a window this run never used.
        return "", None
    if not ad_hoc:
        mark_emitted(shared_dir, summary, now=now)
    return text, summary.worst_never_announced_severity


__all__ = (
    "StandingEntry",
    "StandingSummary",
    "build_section",
    "collect",
    "mark_emitted",
    "render",
    "state_path",
)
