"""alerts_loop_monitor — Signal producer for dispatcher-log loop patterns.

Reads on-disk telemetry only (no LLM):
  - {shared_dir}/alerts/dispatcher.jsonl          — successful (and
    deferred-to-digest) send records, one JSON object per line.
  - {shared_dir}/alerts/delivery-failures.jsonl   — FAILED send records,
    same schema. Split out from dispatcher.jsonl so the operator's
    Recent Messages list isn't polluted with raw HTTP error payloads;
    this monitor must read both to keep its failure-cluster detection
    working after the split.

Two signal types, all under producer ``alerts_loop_monitor``:

  dispatcher_failures  — recurring FAILED sends from one source. Operator
                         doesn't see these in the normal alert flow
                         because a FAILED send (by definition) doesn't
                         arrive in chat. Surface it in the admin UI so
                         a broken Telegram chat_id / expired API token /
                         OpenClaw timeout doesn't silently swallow alerts.
  alert_repeat_loop    — same source + same message content sent N+
                         times in a window. The operator IS being
                         notified, but at a frequency that says the
                         underlying condition needs a structural fix
                         instead of repeated chat noise. Classic case:
                         heal sending "Config drift on evolve" hourly
                         when something needs to be either approved or
                         reverted via the proposal pipeline.

Each detection becomes a Signal via ``signals.store.observe()``.
Signatures that fired on a previous run but didn't fire this run are
auto-resolved via ``signals.store.sweep_resolve()``.

Pod-scoped (no per-bot fanout) — the dispatcher log is pod-wide. Cheap
to run; designed for hourly launchd cadence.

Configurable thresholds via ``network.json::alerts_loop_monitor``:

    {
      "alerts_loop_monitor": {
        "window_hours": 24,
        "failure_warn_threshold": 3,
        "failure_alert_threshold": 10,
        "repeat_threshold": 5,
        "ignore_repeats_from": ["report", "pod_report", "digest", "daily_digest"]
      }
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "alerts_loop_monitor"
LOG_FILENAME = "dispatcher.jsonl"
FAILURES_LOG_FILENAME = "delivery-failures.jsonl"

# Excerpt prefix length used to fingerprint "same message" for the
# repeat-loop detector. The dispatcher already truncates to its own
# audit-excerpt length; we hash a fixed window of that to keep the
# fingerprint stable across minor rendering tweaks (timestamps inside
# the body still vary, but the leading 120 chars are usually the
# template intro + the field that matters).
_EXCERPT_HASH_LEN = 120

# Sources that legitimately fire on a regular schedule (daily / hourly
# digests). Without this guard the repeat-loop detector would treat
# them as a problem. Operator can override via config.
_DEFAULT_IGNORE_REPEATS_FROM = (
    "report",
    "pod_report",
    "digest",
    "daily_digest",
    "weekly_digest",
)


# ─────────────────────────────────────────────────────────────────────────────
# Log reader
# ─────────────────────────────────────────────────────────────────────────────


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # The dispatcher writes RFC3339 with a Z suffix (`2026-05-17T12:00:00Z`).
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def read_recent_records(
    log_path: Path,
    *,
    since: datetime,
    now: datetime,
) -> list[dict]:
    """Return dispatcher log records with ts in [since, now].

    ``log_path`` is the *logical* log filename — pre-rotation a flat file
    at ``alerts/dispatcher.jsonl``, post-rotation a directory of date-
    partitioned files at ``alerts/dispatcher/<YYYY-MM-DD>.jsonl``. This
    reader handles both shapes so a deployment that hasn't yet
    transitioned still surfaces its history.

    Resilient to malformed lines (skipped) and truncated tails. Returns
    an empty list when nothing is present — first-run pods haven't
    dispatched anything yet.
    """
    out: list[dict] = []

    for line in _iter_log_lines(log_path, since=since):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue
        if ts < since or ts > now:
            continue
        out.append(rec)
    return out


def _iter_log_lines(log_path: Path, *, since: datetime) -> Iterable[str]:
    """Yield raw lines from the date-partitioned log dir + the legacy
    flat file (if either exists).

    ``log_path`` is the logical filename (``alerts/<basename>.jsonl``).
    The date dir lives next to it at ``alerts/<basename>/``.
    """
    parent = log_path.parent
    basename = log_path.stem  # strip .jsonl
    log_dir = parent / basename
    since_day_iso = since.astimezone(timezone.utc).date().isoformat()

    if log_dir.is_dir():
        candidates: list[Path] = []
        for p in log_dir.glob("*.jsonl"):
            stem = p.stem
            try:
                # date partitions before the since-day can't contribute
                # records inside the window.
                if stem < since_day_iso:
                    continue
            except TypeError:
                continue
            candidates.append(p)
        candidates.sort(key=lambda p: p.stem)
        for p in candidates:
            try:
                with p.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            yield line
            except OSError:
                continue

    if log_path.is_file():
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield line
        except OSError:
            return


def _excerpt_hash(message_excerpt: str | None) -> str:
    s = (message_excerpt or "")[:_EXCERPT_HASH_LEN]
    # short hex so the signature stays readable.
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Detectors — each returns a list of dicts ready for signals_store.observe()
# ─────────────────────────────────────────────────────────────────────────────


def _target_label(channel: str | None, chat_id: str | None) -> str:
    """Human-scannable delivery target, e.g. ``telegram:123456789``.

    Falls back to just the channel when no chat_id was logged, and to
    ``unknown`` when neither is present. Used in the Signal title/body so
    the operator immediately sees *which* destination is broken.
    """
    channel = (channel or "").strip() or "unknown"
    chat_id = (chat_id or "").strip()
    return f"{channel}:{chat_id}" if chat_id else channel


def _classify_failure(
    error: str | None, channel: str | None
) -> tuple[str, str, str]:
    """Map a delivery-failure ``error`` + ``channel`` to a remediation.

    Returns ``(reason_code, cause, remediation)``:
      - ``reason_code`` — short stable slug for details/telemetry.
      - ``cause`` — one-sentence plain-English explanation.
      - ``remediation`` — the concrete next step the operator must take.

    Never raises: a missing or odd error string falls through to a
    generic-but-actionable remediation rather than crashing the monitor.
    Match order is most-specific-first so e.g. Telegram "chat not found"
    is recognised before the broad 4xx bucket.
    """
    e = (error or "").strip().lower()
    ch = (channel or "").strip().lower()
    is_telegram = ch == "telegram" or "telegram" in e
    is_slack = ch == "slack" or "slack" in e

    # Telegram: the operator never /start-ed the bot, so it can't open a
    # DM. This is the live #3152 shape — the one error that's invisible
    # everywhere except this card, because the broken channel is the very
    # one the alert would otherwise travel over.
    if "chat not found" in e:
        return (
            "chat_not_found",
            "Telegram won't let the bot open this chat — a bot can't "
            "message you until you've started a conversation with it.",
            "Open Telegram, find the pod's bot, and send it /start (in a "
            "group, also confirm the bot was added). Then restart the "
            "gateway so the queued alerts retry. If the chat_id in "
            "network.json is wrong, correct it too.",
        )
    # Telegram: the recipient blocked the bot.
    if "blocked" in e:
        return (
            "bot_blocked",
            "Telegram reports the bot was blocked by the recipient, so it "
            "can't deliver messages.",
            "Open the chat with the pod's bot in Telegram and unblock / "
            "restart it; delivery resumes on the next send attempt.",
        )
    # Auth / token problems (Telegram 401, generic 'unauthorized').
    if "unauthorized" in e or "http 401" in e or "invalid token" in e:
        return (
            "bad_token",
            "The channel rejected the credential — the bot token is wrong, "
            "revoked, or expired.",
            "Check and, if needed, rotate the bot token in the bot's "
            "openclaw.json (and the alerts config in network.json), then "
            "re-test the channel.",
        )
    # Slack delivery problems (webhook revoked, channel archived, etc.).
    if is_slack and (
        "webhook" in e or "channel_not_found" in e or "no_active" in e
        or "http 4" in e
    ):
        return (
            "slack_channel",
            "Slack rejected the delivery — the webhook URL is revoked or the "
            "target channel is gone.",
            "Re-test the Slack webhook URL from the Channels settings page; "
            "re-create it if the channel was archived or the integration was "
            "removed.",
        )
    # Any other Telegram 4xx is a malformed request — usually a bad
    # chat_id / target rather than a credential problem.
    if is_telegram and "http 4" in e:
        return (
            "telegram_bad_request",
            "Telegram rejected the request (4xx) — most often a wrong or "
            "stale chat_id / target.",
            "Verify the configured chat_id / target for this channel in "
            "network.json's alerts config, then re-test.",
        )
    # Nothing matched — keep a useful generic step that still points at the
    # captured error and the place to inspect it.
    if not e:
        return (
            "unknown",
            "A delivery failure was logged but no error string was captured.",
            "Open Reports → Subscriptions → Delivery log to inspect the raw "
            "failure, then fix the channel target or credential it points to.",
        )
    return (
        "unknown",
        "The channel rejected delivery for an unrecognised reason.",
        "Open Reports → Subscriptions → Delivery log to inspect the failure, "
        f"then fix the channel target or credential. Latest error: {error}",
    )


def detect_dispatcher_failures(
    records: Iterable[dict],
    *,
    warn_threshold: int = 3,
    alert_threshold: int = 10,
) -> list[dict]:
    """Group records by source where result == FAILED.

    Severity: warn at ``warn_threshold`` failures in the window, alert at
    ``alert_threshold``. Below ``warn_threshold`` is silence — a single
    transient OpenClaw timeout isn't worth alerting on.

    The Signal names the delivery *target* and the *remediation* the
    operator must perform (parsed from the error). This is the only place
    a broken outbound channel can surface — a FAILED send never arrives in
    chat — so the card has to be self-sufficient (anchor: #3152, a
    Telegram channel 100% down for hours behind a "chat not found" the
    operator could fix in 30s once told how).
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if (r.get("result") or "").lower() != "failed":
            continue
        src = r.get("source") or "unknown"
        by_source[src].append(r)

    out: list[dict] = []
    for source, failures in by_source.items():
        n = len(failures)
        if n < warn_threshold:
            continue
        severity = "alert" if n >= alert_threshold else "warn"
        # Sample the error/channel/target from the most recent failure for
        # the body text. Earlier failures may have a different error
        # string; surfacing the latest is more useful at triage time.
        latest = max(failures, key=lambda r: r.get("ts") or "")
        err = latest.get("error") or "unknown"
        channel = latest.get("channel") or "unknown"
        target = _target_label(latest.get("channel"), latest.get("chat_id"))
        reason_code, cause, remediation = _classify_failure(
            latest.get("error"), latest.get("channel")
        )
        # Distinct targets in this cluster — if more than one the per-target
        # remediation may not cover them all, so we say so in the body.
        distinct_targets = {
            (
                (f.get("channel") or "").strip(),
                (f.get("chat_id") or "").strip(),
            )
            for f in failures
        }
        # Start of the outage within the window. Stable as new failures
        # pile up (unlike the count), so it's safe in the body without
        # churning every run. ISO strings sort chronologically.
        since = min((f.get("ts") or "" for f in failures), default="") or "unknown"

        # Title names the target + conveys SCALE via the severity word
        # (failing vs DOWN) rather than the raw count — the count
        # increments every send, and the title is refreshed on every
        # observe() bump, so embedding it would churn the card each run.
        title = (
            f"Alert delivery to {target} is DOWN — alerts may not be arriving"
            if severity == "alert"
            else f"Alert delivery to {target} is failing — {source} dispatcher"
        )
        multi_note = (
            ""
            if len(distinct_targets) <= 1
            else (
                f"\n\nNote: {len(distinct_targets)} distinct targets failed "
                "under this source; the remediation above is for the most "
                "recent. Check the Delivery log for the others."
            )
        )
        body = (
            f"Alert delivery from source `{source}` is failing — and because "
            "a FAILED send never arrives in chat, this card on the Alerts "
            "page is the only place it can reach you.\n\n"
            f"Target: `{target}`\n"
            f"Undelivered (failed) sends in the last 24h: {n}\n"
            f"First failure in window: {since}\n"
            f"Latest error: `{err}`\n\n"
            f"{cause}\n\n"
            f"What to do: {remediation}"
            f"{multi_note}"
        )
        # Severity framework: operations vector. Magnitude scales with
        # failure count — same anchors as the warn/alert escalation,
        # plus severity_active so the priority comp boosts an
        # ongoing-failure cluster.
        magnitude = 3 if n >= alert_threshold else 2
        out.append(
            dict(
                signature=make_signature(PRODUCER, "dispatcher_failures", source),
                producer=PRODUCER,
                type="dispatcher_failures",
                flavor="maintenance",
                severity=severity,
                scope="pod",
                title=title,
                body=body,
                details=dict(
                    source=source,
                    failure_count=n,
                    latest_error=err,
                    latest_channel=channel,
                    target=target,
                    failure_reason=reason_code,
                    since=since,
                    # The derived next step, carried structured so the card
                    # (and any downstream consumer) has it verbatim; also
                    # echoed into fix_steps below for the rendered "How to
                    # fix" section.
                    remediation=remediation,
                    vector="operations",
                    magnitude=magnitude,
                    severity_active=True,
                    what_it_means=(
                        f"Alert delivery to {target} has failed {n} time(s) "
                        "in the recent window. The dispatcher is the layer "
                        "between Signals and your chat — while it's failing, "
                        "alerts from every other producer aren't reaching "
                        f"you. {cause}"
                    ),
                    fix_steps=(
                        f"1. {remediation}\n"
                        "2. Confirm recovery on the Reports → Subscriptions → "
                        "Delivery log (this signal auto-resolves once sends "
                        "succeed again)\n"
                        f"3. Failing target: {target}\n"
                        f"4. Latest error: {err[:200] if err else '(none captured)'}"
                    ),
                ),
            )
        )
    return out


def detect_alert_repeat_loop(
    records: Iterable[dict],
    *,
    repeat_threshold: int = 5,
    ignore_sources: Iterable[str] = _DEFAULT_IGNORE_REPEATS_FROM,
) -> list[dict]:
    """Group SENT records by (source, excerpt_hash).

    Fires when the same (source, content) pair has been sent at least
    ``repeat_threshold`` times in the window. ``ignore_sources`` excludes
    legitimately periodic sources (digests, daily reports) where
    repeated SENTs are by design.
    """
    ignore = {s.lower() for s in ignore_sources}
    counts: Counter[tuple[str, str]] = Counter()
    samples: dict[tuple[str, str], dict] = {}
    for r in records:
        if (r.get("result") or "").lower() != "sent":
            continue
        src = (r.get("source") or "unknown").lower()
        if src in ignore:
            continue
        excerpt = r.get("message_excerpt") or ""
        key = (src, _excerpt_hash(excerpt))
        counts[key] += 1
        if key not in samples:
            samples[key] = r

    out: list[dict] = []
    for (source, eh), n in counts.items():
        if n < repeat_threshold:
            continue
        sample = samples[(source, eh)]
        # Trim the excerpt for the title (keep it scannable). The body
        # keeps the full sample for context.
        title_excerpt = (sample.get("message_excerpt") or "")[:60].rstrip()
        title = (
            f"{source} is on repeat: same alert sent {n}× in the last 24h"
        )
        body = (
            f"The sysadmin alert dispatcher sent {n} chat messages from "
            f"source `{source}` with the same opening — likely the same "
            f"underlying condition firing hourly without being resolved. "
            "When this happens it usually means an alert is being used as "
            "a recurring nag where what's needed is a structural fix "
            "(approve/revert the proposal, or fix the config that keeps "
            "drifting).\n\n"
            f"Message starts with: \"{title_excerpt}…\"\n\n"
            "Open Reports → Subscriptions → Messages to see the full "
            "history, then take whichever action the alert keeps asking "
            "for. Once the underlying condition clears, this signal "
            "auto-resolves."
        )
        out.append(
            dict(
                signature=make_signature(
                    PRODUCER, "alert_repeat_loop", f"{source}:{eh}"
                ),
                producer=PRODUCER,
                type="alert_repeat_loop",
                flavor="maintenance",
                severity="warn",
                scope="pod",
                title=title,
                body=body,
                details=dict(
                    source=source,
                    repeat_count=n,
                    excerpt=title_excerpt,
                    # Severity framework: operations vector — the system
                    # is sending real alerts that need a structural fix
                    # rather than recurring nag. Magnitude scales with
                    # repeat count: 5-10 = 1, 10+ = 2.
                    vector="operations",
                    magnitude=2 if n >= 10 else 1,
                    severity_active=True,
                    what_it_means=(
                        f"The same alert from {source!r} has fired {n} "
                        "times in the last 24 hours without the underlying "
                        "condition clearing. This usually means the alert "
                        "is being used as a recurring nag — the system "
                        "keeps detecting the same problem but no one has "
                        "taken the structural action it's asking for "
                        "(approve/revert a proposal, fix the drifting "
                        "config). Snoozing the signal doesn't fix this; "
                        "only the underlying cause does."
                    ),
                    fix_steps=(
                        "1. Open Reports → Subscriptions → Messages and "
                        f"filter to source `{source}`\n"
                        "2. Read the most recent message — it states the "
                        "specific action the alert is asking for\n"
                        "3. Take that action (often: approve a queued "
                        "proposal, run a deploy, or fix the source config "
                        "the alert keeps flagging)\n"
                        "4. Once the underlying condition clears, this "
                        "signal auto-resolves on the next monitor pass"
                    ),
                ),
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_config(network: dict[str, Any]) -> dict[str, Any]:
    cfg = (network.get("alerts_loop_monitor") or {}) if isinstance(network, dict) else {}
    return {
        "window_hours": int(cfg.get("window_hours") or 24),
        "failure_warn_threshold": int(cfg.get("failure_warn_threshold") or 3),
        "failure_alert_threshold": int(cfg.get("failure_alert_threshold") or 10),
        "repeat_threshold": int(cfg.get("repeat_threshold") or 5),
        "ignore_repeats_from": tuple(
            cfg.get("ignore_repeats_from") or _DEFAULT_IGNORE_REPEATS_FROM
        ),
    }


def collect_for_pod(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Run all detectors and return the union of Signal specs."""
    now = now or datetime.now(timezone.utc)
    cfg = _resolve_config(config)
    since = now - timedelta(hours=cfg["window_hours"])
    log_path = shared_dir / "alerts" / LOG_FILENAME
    failures_path = shared_dir / "alerts" / FAILURES_LOG_FILENAME
    # Failed sends live in their own JSONL post-PWA-polish-split (the
    # Messages list reads only the main log, the Dispatcher Health panel
    # reads only the failures log). We need both to detect failure
    # clusters AND repeat-SENT loops without missing either.
    records = (
        read_recent_records(log_path, since=since, now=now)
        + read_recent_records(failures_path, since=since, now=now)
    )
    detections: list[dict] = []
    detections.extend(
        detect_dispatcher_failures(
            records,
            warn_threshold=cfg["failure_warn_threshold"],
            alert_threshold=cfg["failure_alert_threshold"],
        )
    )
    detections.extend(
        detect_alert_repeat_loop(
            records,
            repeat_threshold=cfg["repeat_threshold"],
            ignore_sources=cfg["ignore_repeats_from"],
        )
    )
    return detections


def run(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[set[str], int, int]:
    """Collect detections, write Signals, sweep-resolve cleared ones.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    detections = collect_for_pod(shared_dir, config, now=now)
    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[alerts_loop_monitor] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: alerts_loop_monitor condition cleared on next run",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[alerts_loop_monitor] sweep_resolve failed: {exc}", flush=True)

    return kept, len(detections), n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="alerts_loop_monitor — Signal producer for dispatcher-log loop patterns",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(shared_dir, config, dry_run=args.dry_run)
    if args.dry_run:
        print(
            f"[alerts_loop_monitor] dry-run: {n_fired} would-fire",
            flush=True,
        )
        return
    print(
        f"[alerts_loop_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
