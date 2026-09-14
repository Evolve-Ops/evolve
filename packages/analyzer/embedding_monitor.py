"""embedding_monitor — emit Signals when embedding providers fail repeatedly.

Tails each bot's ``~/.openclaw/logs/gateway.err.log`` and surfaces:

  embedding_provider_failing   — hard failures (auth/quota/4xx/5xx)
                                 cross a threshold within a window;
                                 severity escalates by error class.
  embedding_rate_limit_storm   — chatty 429-then-retry events exceed
                                 a high-water mark (warn-level only;
                                 OpenClaw is recovering on its own).

Failure classes parsed from the log:
  auth_failed     — HTTP 401/403, "invalid api key", "unauthorized"
  quota_exceeded  — HTTP 429, "rate_limit", "exceeded your quota"
  bad_request     — HTTP 400/404
  provider_error  — HTTP 5xx
  unknown         — anything else that matched the failure prefix

Designed to run hourly under launchd as the evolve user. Reads only —
takes no corrective action. (OpenClaw's own ``memorySearch.fallback``
handles failover; this monitor exists to make the failure visible so an
operator rotates the dead key instead of letting it shadow the chain.)

Relevant log lines OpenClaw emits:
  [memory] sync failed (session-start): Error: openai embeddings failed: 403 {...}
  [memory] sync failed (search): Error: openai embeddings failed: 429 {...}
  [memory] sync failed (watch): Error: gemini embeddings failed: 500 {...}
  [memory] embeddings rate limited; retrying in 574ms
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import (
    bot_home as _bot_home,
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "embedding_monitor"

# ── Thresholds (tunable via network.json embedding_monitor block) ─────────────
DEFAULTS: dict[str, Any] = {
    # Window we look back over when counting failures.
    "window_hours": 1,
    # Thresholds per failure class — # of failures within window to fire.
    # Auth failures fire on the first occurrence (a revoked key isn't
    # going to fix itself; one occurrence is enough signal).
    "fail_threshold_auth": 1,
    # 429s flap during retry storms; need a few before signalling.
    "fail_threshold_quota": 5,
    # 5xx are usually transient; require a steady stream.
    "fail_threshold_provider": 10,
    # Other 4xx are misconfigs; one is enough.
    "fail_threshold_bad_request": 1,
    # Severity escalation for quota — alert once we exceed 2x threshold.
    "alert_multiplier": 2,
    # Rate-limit storms: warn at this volume of retry messages.
    "rate_limit_storm_threshold": 50,
    # Soft cap on file bytes to read; gateway.err.log can grow large.
    "log_tail_bytes": 512 * 1024,
}


# ── Log parsing ──────────────────────────────────────────────────────────────
# Format observed on the mini:
#   2026-03-30T23:56:25.736-07:00 [memory] sync failed (search): Error: openai embeddings failed: 403 {
# Older lines may have lost their timestamp prefix (rotation). Both must parse.

_HARD_FAIL_RX = re.compile(
    r"\[memory\] sync failed \((?P<reason>[a-z0-9_-]+)\): "
    r"Error: (?P<provider>[a-z0-9_-]+) embeddings failed: (?P<status>\d{3})",
    re.IGNORECASE,
)
_RATE_LIMIT_RX = re.compile(r"\[memory\] embeddings rate limited", re.IGNORECASE)
_TS_PREFIX_RX = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)",
)


def classify_status(status: int) -> str:
    """Map an HTTP status to a failure class. See module docstring."""
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        return "quota_exceeded"
    if 500 <= status < 600:
        return "provider_error"
    if 400 <= status < 500:
        return "bad_request"
    return "unknown"


def parse_log(text: str, *, since: datetime | None = None) -> dict[str, Any]:
    """Parse a gateway.err.log blob into structured failure aggregates.

    Returns:
        {
          "hard_fails": [
            {"ts": datetime, "provider": str, "status": int,
             "error_class": str, "reason": str},
            ...
          ],
          "rate_limit_count": int,
        }

    OpenClaw writes the `[memory] sync failed (...): Error: <provider>
    embeddings failed: <status>` header line WITHOUT a leading ISO
    timestamp — only surrounding non-memory log lines carry timestamps.
    Each untimed match here inherits the timestamp of the most recent
    preceding timed line so a multi-line block ages out as a unit.

    Untimed matches with no preceding timed anchor in the buffer are
    dropped: we have no way to tell whether they're 1 minute or 1 week
    old, and the prior "conservatively include" rule produced false-
    positive alerts (e.g. team_bot_a's May-2 OpenAI 429 spam kept firing
    `quota_exceeded` signals for days after team_bot_a had switched to gemini,
    because the May-2 untimed headers passed the `since` filter
    unconditionally).
    """
    hard_fails: list[dict[str, Any]] = []
    rate_limit_count = 0
    last_anchor_ts: datetime | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        own_ts = _parse_ts(line)
        if own_ts is not None:
            last_anchor_ts = own_ts

        if _RATE_LIMIT_RX.search(line):
            effective_ts = own_ts or last_anchor_ts
            if effective_ts is None:
                continue
            if since is not None and effective_ts < since:
                continue
            rate_limit_count += 1
            continue

        m = _HARD_FAIL_RX.search(line)
        if not m:
            continue
        effective_ts = own_ts or last_anchor_ts
        if effective_ts is None:
            continue
        if since is not None and effective_ts < since:
            continue
        status = int(m.group("status"))
        hard_fails.append(
            {
                "ts": effective_ts,
                "provider": m.group("provider").lower(),
                "status": status,
                "error_class": classify_status(status),
                "reason": m.group("reason").lower(),
            }
        )

    return {"hard_fails": hard_fails, "rate_limit_count": rate_limit_count}


def _parse_ts(line: str) -> datetime | None:
    m = _TS_PREFIX_RX.match(line)
    if not m:
        return None
    raw = m.group("ts")
    # Normalise "Z" suffix for fromisoformat.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # fromisoformat in 3.10 doesn't accept "+0700"; only "+07:00".
    if len(raw) >= 5 and raw[-5] in "+-" and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except ValueError:
        return None


# ── On-disk reader ───────────────────────────────────────────────────────────


def _read_with_sudo_fallback(path: Path, max_bytes: int) -> str | None:
    """Read up to ``max_bytes`` from the tail of ``path``.

    Mirrors cost_watchdog's pattern: try direct read, fall back to
    ``sudo /bin/cat`` if the evolve user lacks ACL access. Returns the
    final ``max_bytes`` of file content (or full content if smaller).
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except (PermissionError, OSError, FileNotFoundError):
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True,
            timeout=5,
        )
        if r.returncode == 0:
            data = r.stdout
            if len(data) > max_bytes:
                data = data[-max_bytes:]
            return data.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def read_gateway_errors(
    bot_id: str,
    config: dict[str, Any] | None = None,
    *,
    max_bytes: int,
) -> str | None:
    path = _bot_home(bot_id, config) / ".openclaw" / "logs" / "gateway.err.log"
    return _read_with_sudo_fallback(path, max_bytes)


# ── Detectors ────────────────────────────────────────────────────────────────


_CLASS_TO_THRESHOLD_KEY = {
    "auth_failed":     "fail_threshold_auth",
    "quota_exceeded":  "fail_threshold_quota",
    "provider_error":  "fail_threshold_provider",
    "bad_request":     "fail_threshold_bad_request",
}

_CLASS_BLURB = {
    "auth_failed":    "Authentication failure (key revoked, wrong project, or scope mismatch).",
    "quota_exceeded": "Quota / rate-limit exceeded — provider is rejecting requests.",
    "provider_error": "Provider 5xx errors — outage or backend instability.",
    "bad_request":    "Request rejected (4xx) — model not available or request shape invalid.",
}


def detect_provider_failures(
    bot_id: str,
    parsed: dict[str, Any],
    *,
    thresholds: dict[str, Any],
) -> list[dict]:
    """Bucket hard_fails by (provider, error_class) and emit Signals over threshold."""
    out: list[dict] = []
    buckets: dict[tuple[str, str], list[dict]] = {}
    for fail in parsed["hard_fails"]:
        key = (fail["provider"], fail["error_class"])
        buckets.setdefault(key, []).append(fail)

    for (provider, error_class), fails in buckets.items():
        threshold_key = _CLASS_TO_THRESHOLD_KEY.get(error_class)
        if threshold_key is None:
            continue
        threshold = int(thresholds[threshold_key])
        count = len(fails)
        if count < threshold:
            continue

        alert_at = threshold * int(thresholds["alert_multiplier"])
        severity = "alert" if count >= alert_at or error_class == "auth_failed" else "warn"

        # Sample status code to display — pick the most common.
        status = max((f["status"] for f in fails), key=lambda s: sum(1 for f in fails if f["status"] == s))
        last_ts = max((f["ts"] for f in fails if f["ts"] is not None), default=None)

        scope_key = f"{bot_id}/{provider}/{error_class}"
        out.append(
            {
                "signature": make_signature(PRODUCER, "provider_failing", scope_key),
                "producer": PRODUCER,
                "type": "provider_failing",
                "flavor": "maintenance",
                "severity": severity,
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: {provider} embeddings — "
                    f"{error_class.replace('_', ' ')} ({count}× in window)"
                ),
                "body": (
                    f"{_CLASS_BLURB.get(error_class, 'Embedding API failure.')} "
                    f"Provider `{provider}` returned HTTP {status} "
                    f"on {count} memory_search call(s) within the window. "
                    f"OpenClaw will fall through to the configured fallback if one is set; "
                    f"if not, memory search is degraded for this bot. "
                    f"Rotate the key or remove `{provider}` from the embedding chain "
                    f"in AI Optimization → Embedding providers."
                ),
                "details": {
                    "bot_id": bot_id,
                    "provider": provider,
                    "error_class": error_class,
                    "http_status": status,
                    "failure_count": count,
                    "threshold": threshold,
                    "last_failure_at": last_ts.isoformat() if last_ts else None,
                    "reasons": sorted({f["reason"] for f in fails}),
                    # Severity framework: operations vector. The bot's
                    # memory_search is degraded; cost ramifications are
                    # downstream of the operational concern. Magnitude
                    # tracks the severity escalation already computed
                    # above (auth_failed or count >= alert_at → 3).
                    "vector": "operations",
                    "magnitude": 3 if severity == "alert" else 2,
                    "severity_active": True,
                    "what_it_means": (
                        f"{bot_id}'s embedding provider `{provider}` "
                        f"returned HTTP {status} on {count} memory "
                        f"search call(s) in the recent window. "
                        f"{_CLASS_BLURB.get(error_class, 'Embedding API failure.')} "
                        "OpenClaw falls through to the next provider "
                        "in `memorySearch.fallback` if one is set; "
                        "without a fallback, memory search is silently "
                        "degraded — the bot keeps responding but stops "
                        "recalling prior context. auth_failed and "
                        "bad_request are deploy-time bugs; quota and "
                        "provider 5xx may resolve on their own."
                    ),
                    "fix_steps": (
                        f"1. Open AI Optimization → Embedding providers "
                        f"for `{bot_id}`\n"
                        f"2. If `{error_class}` is `auth_failed` or "
                        "`bad_request`: rotate the API key on the "
                        f"provider account, paste it into the keystore, "
                        "and restart the gateway:\n"
                        "   ssh pod_admin_user@mini sudo launchctl kickstart "
                        f"-k system/ai.openclaw.gateway.{bot_id}\n"
                        "3. If `quota_exceeded` (429): either upgrade "
                        "the provider plan or move this bot to a "
                        "different provider in the embedding chain\n"
                        "4. If `provider_error` (5xx): wait it out; the "
                        "auto-resolve will clear when the provider "
                        "recovers. Persistent 5xx → remove from chain\n"
                        "5. Verify by tailing the gateway error log:\n"
                        "   ssh pod_admin_user@mini sudo tail -f "
                        f"/Users/{bot_id}/.openclaw/logs/gateway.err.log"
                    ),
                },
                "config_hint": {
                    "page": "ai-optimization",
                    "section": "embedding-providers",
                    "bot_id": bot_id,
                    "provider": provider,
                },
            }
        )
    return out


def detect_rate_limit_storm(
    bot_id: str,
    parsed: dict[str, Any],
    *,
    thresholds: dict[str, Any],
) -> list[dict]:
    count = int(parsed["rate_limit_count"])
    threshold = int(thresholds["rate_limit_storm_threshold"])
    if count < threshold:
        return []
    return [
        {
            "signature": make_signature(PRODUCER, "rate_limit_storm", bot_id),
            "producer": PRODUCER,
            "type": "rate_limit_storm",
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: embedding rate-limit storm "
                f"({count} retries in window)"
            ),
            "body": (
                f"{count} embedding-rate-limit retries in the last window. "
                f"OpenClaw is backing off and recovering, but the active "
                f"provider is consistently saturated. Consider lowering "
                f"memory-search frequency or moving to a provider with higher "
                f"rate caps."
            ),
            "details": {
                "bot_id": bot_id,
                "rate_limit_count": count,
                "threshold": threshold,
                # Severity framework: operations vector. Rate-limit
                # retries mean the active provider is saturated;
                # fallbacks bear retries cost-wise. Magnitude 2 — real
                # degradation but bot still functioning via retries.
                "vector": "operations",
                "magnitude": 2,
                "severity_active": True,
                "what_it_means": (
                    f"{bot_id} hit {count} embedding rate-limit "
                    "retries in the recent window. OpenClaw is "
                    "backing off and recovering on its own — memory "
                    "search still works — but the active embedding "
                    "provider is consistently saturated. Each retry "
                    "still costs API calls (provider charges retries "
                    "even when rate-limited) and adds latency to "
                    "every memory_search."
                ),
                "fix_steps": (
                    f"1. Open AI Optimization → Embedding providers "
                    f"for `{bot_id}`\n"
                    "2. Upgrade the active provider's rate-limit tier "
                    "on the provider account (often the cheapest fix)\n"
                    "3. Or swap to a provider with higher rate caps — "
                    "Voyage AI and Gemini have generous free tiers\n"
                    "4. Or reduce memory-search frequency by lowering "
                    "the bot's heartbeat cadence (which dominates "
                    "memory_search load for chatty bots)\n"
                    "5. Verify the storm has cleared by tailing:\n"
                    "   ssh pod_admin_user@mini sudo tail -f "
                    f"/Users/{bot_id}/.openclaw/logs/gateway.err.log "
                    "| grep 'rate limited'"
                ),
            },
            "config_hint": {
                "page": "ai-optimization",
                "section": "embedding-providers",
                "bot_id": bot_id,
            },
        }
    ]


# ── Runner ───────────────────────────────────────────────────────────────────


def _thresholds_for_bot(bot_id: str, config: dict[str, Any]) -> dict[str, Any]:
    cfg = (config.get("embedding_monitor") or {})
    base = dict(DEFAULTS)
    base.update(cfg.get("defaults") or {})
    base.update((cfg.get("bots") or {}).get(bot_id) or {})
    return base


def collect_for_bot(
    bot_id: str,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict]:
    thresholds = _thresholds_for_bot(bot_id, config)
    now_dt = now or datetime.now(timezone.utc)
    since = now_dt - timedelta(hours=int(thresholds["window_hours"]))

    text = read_gateway_errors(
        bot_id, config, max_bytes=int(thresholds["log_tail_bytes"])
    )
    if text is None:
        return []

    parsed = parse_log(text, since=since)

    detections: list[dict] = []
    detections += detect_provider_failures(bot_id, parsed, thresholds=thresholds)
    detections += detect_rate_limit_storm(bot_id, parsed, thresholds=thresholds)
    return detections


def _emit_to_observability(
    bot_id: str,
    detection: dict,
    shared_dir: Path,
) -> None:
    """Record an embedding-failure detection as an observability span.

    Triple-write alongside the JSONL log (durable record) and the
    Signal store. v1.5-1 — Best-effort; failures never break the
    Signal emit.
    """
    try:
        from observability import get_client
    except ImportError:
        return
    try:
        client = get_client({}, shared_dir=shared_dir)
    except Exception:
        return

    details = detection.get("details") or {}
    provider = details.get("provider")
    error_class = details.get("error_class")
    status = details.get("http_status")
    now = datetime.now(timezone.utc)
    try:
        client.record_event(
            name=f"embedding_monitor.{detection.get('type', 'finding')}",
            producer=PRODUCER,
            bot_id=bot_id,
            start_time=now,
            end_time=now,
            provider=provider,
            tags=[
                detection.get("flavor", "maintenance"),
                detection.get("severity", "warn"),
                provider or "unknown_provider",
                error_class or "unknown_class",
            ],
            attributes={
                "event_type": detection.get("type"),
                "severity": detection.get("severity"),
                "details": dict(details),
                "config_hint": detection.get("config_hint"),
            },
            error_info={"http_status": status, "error_class": error_class}
                if status else None,
        )
    except Exception:
        return


def emit_signals_from_observability_spans(
    shared_dir: Path,
    *,
    bot_id: str | None = None,
    lookback_seconds: int = 3600,
    config: dict | None = None,
    client: Any = None,
) -> int:
    """Produce Signals from embedding-failure observability spans.

    This is the v1.5-1 path that proves end-to-end consumption: a span
    written by :func:`_emit_to_observability` (or any future producer
    that emits spans with ``producer=embedding_monitor``) is read here
    and turned into a Signal via :func:`signals.store.observe_from_opik`.

    Returns the number of spans converted into observe() calls. Safe to
    run alongside the existing log-tail detector — the find-or-create
    semantics of observe() dedup on signature; running both produces
    one Signal per finding.
    """
    try:
        from observability import SpanFilter, get_client
    except ImportError:
        return 0

    if client is None:
        try:
            client = get_client(config or {}, shared_dir=shared_dir)
        except Exception:
            return 0

    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=lookback_seconds)
    query = SpanFilter(
        producer=PRODUCER,
        bot_id=bot_id,
        since=since,
        until=now,
        error_only=True,
        limit=500,
    )

    emitted = 0
    for span in client.search_spans(query):
        attrs = dict(span.attributes or {})
        details = dict(attrs.get("details") or {})
        type_ = attrs.get("event_type") or "provider_failing"
        severity = attrs.get("severity") or "warn"
        # Build a signature suffix that matches the legacy log-tail
        # path so dedup collapses both producers' findings onto one
        # Signal.
        if type_ == "provider_failing":
            sig_suffix = f"{details.get('provider', 'unknown')}/{details.get('error_class', 'unknown')}"
        else:
            sig_suffix = type_
        try:
            signals_store.observe_from_opik(
                shared_dir,
                span,
                type=type_,
                flavor=attrs.get("flavor") or "maintenance",
                severity=severity,
                scope="bot",
                signature_suffix=sig_suffix,
            )
            emitted += 1
        except Exception:
            continue
    return emitted


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[set[str], int]:
    detections = collect_for_bot(bot_id, config, now=now)
    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[embedding_monitor] observe failed for {d['signature']}: {exc}",
                flush=True,
            )
        # v1.5-1: triple-write to observability. The Signal is the
        # durable record for the alerts UI; the span feeds Opik trace
        # search + cost rollups. Best-effort.
        _emit_to_observability(bot_id, d, shared_dir)
    return kept, len(detections)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="embedding_monitor — emit Signals for embedding-provider failures",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    primary = get_primary(config)
    members = get_members(config)
    all_bots = ([primary] if primary and primary not in members else []) + members
    all_bots = [b for b in all_bots if b]
    if args.bot:
        all_bots = [args.bot]

    all_kept: set[str] = set()
    total = 0
    for bot in all_bots:
        kept, n = run_for_bot(bot, shared_dir, config, dry_run=args.dry_run)
        all_kept |= kept
        total += n

    if args.dry_run:
        print(
            f"[embedding_monitor] dry-run: {len(all_bots)} bots, {total} would-fire",
            flush=True,
        )
        return

    try:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=all_kept,
            reason="auto-resolve: embedding_monitor condition cleared on next run",
        )
    except Exception as exc:
        resolved = []
        print(f"[embedding_monitor] sweep_resolve failed: {exc}", flush=True)

    print(
        f"[embedding_monitor] {len(all_bots)} bots, {total} firings, "
        f"{len(resolved)} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
