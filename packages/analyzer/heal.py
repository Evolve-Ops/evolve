#!/usr/bin/env python3
"""
heal.py — Evolve self-healing monitor.

Runs on the PRIMARY bot every 5 minutes. Monitors all network members,
attempts immediate recovery when a gateway is down, records incidents,
and escalates recurring failures to the proposal pipeline.

Replaces the dumb cron restart pattern with an intelligent monitor that:
  1. Checks all bots' gateways via HTTP health check
  2. Attempts restart if down (immediate response, no human needed)
  3. Records every incident with context in shared_dir/incidents/
  4. Detects patterns → generates investigation proposals
  5. Detects config drift → generates config_change proposals

The dumb cron (gateway-selfheal.sh) can stay as a safety net.
heal.py is the intelligent layer on top.

Usage:
  python3 heal.py                    # check all network members once
  python3 heal.py --bot-id team_bot_a       # check a specific bot only
  python3 heal.py --dry-run          # check and report, no restarts
  python3 heal.py --daemon           # run continuously (testing)

Incident schema (shared_dir/incidents/YYYY-MM-DD/{bot_id}-{ts}.json):
  {
    "bot_id": "team_bot_a",
    "detected_at": "...",
    "type": "gateway_down" | "gateway_slow" | "restart_attempted" |
            "restart_succeeded" | "restart_failed" | "config_drift",
    "detail": "...",
    "port": 19000,
    "response_time_ms": null,
    "healer_version": "0.1.0"
  }

Thresholds (configurable in network.json heal block):
  failuresBeforeProposal: 3   (failures in window before generating proposal)
  windowHours: 24             (rolling window for failure counting)
  slowThresholdMs: 3000       (response time before "slow" incident)
  restartCooldownMin: 10      (don't restart same bot more than once per N minutes)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from evolve_config import load_config, get_shared_dir, get_members, get_alerts, get_bot_user, is_module_enabled, get_module_config, resolve_network_path, CANONICAL_SHARED_DIR, user_home
from evolve_util import now_iso, atomic_write_json
from secret_redaction import redact_json as _redact_for_drift_compare


def _html_escape(value) -> str:
    """HTML-escape an interpolated value for Telegram parse_mode=HTML.

    Lazy import from the admin package — heal.py may execute when the
    admin package is partially-installed, so the import is best-effort
    with a stdlib fallback. Same pattern as report.py and audit.py.
    """
    try:
        from evolve_admin.alerts.catalog import html_escape
        return html_escape(value)
    except Exception:
        import html as _html
        return _html.escape("" if value is None else str(value), quote=False)


# Process titles for the openclaw gateway across versions. Source of truth is
# evolve_admin.ocadmin._GATEWAY_PROC_MARKERS — kept in sync here so heal's
# probe loop stays free of admin-package imports.
# Openclaw < 2026.4.29 used "openclaw-gateway" as the process title; newer
# versions run as `node .../openclaw/dist/entry.js` (or one of the variants below).
_GATEWAY_PROC_MARKERS = (
    "openclaw-gateway",
    "openclaw/dist/entry.js",
    "openclaw/dist/index.js",
    "openclaw/openclaw.mjs",
)


def _is_gateway_proc_line(line: str, user: str) -> bool:
    """Return True if a `ps auxww` line is an openclaw gateway for `user`."""
    parts = line.split()
    if not parts or parts[0] != user:
        return False
    return any(m in line for m in _GATEWAY_PROC_MARKERS)


def _parse_log_ts(line: str) -> str | None:
    """Extract a leading ISO timestamp from a log line, or None if absent.

    Openclaw gateway error lines look like:
      `2026-05-05T06:09:11.967-07:00 [agent/embedded] [context-overflow-diag] ...`
    so the first whitespace-delimited token is the timestamp.
    """
    if not line:
        return None
    raw_ts = line.split(None, 1)[0]
    try:
        # `replace("T", " ")` keeps this parser working on older Python where
        # fromisoformat does not accept the full ISO 8601 form. `rstrip("Z")`
        # handles UTC-suffixed timestamps from non-openclaw producers.
        datetime.fromisoformat(raw_ts.rstrip("Z").replace("T", " "))
        return raw_ts
    except (ValueError, IndexError):
        return None


_LOG_LINE_MAX_AGE_S = 24 * 3600


def _get_proc_start_time(pid: int) -> datetime | None:
    """Return the start time of process `pid` as a UTC-aware datetime, or None."""
    try:
        import subprocess as _sp
        r = _sp.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
        raw = r.stdout.strip()
        if not raw:
            return None
        # lstart format on macOS: "Wed May  7 15:35:27 2026"
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _drop_stale_log_lines(
    lines: list[str],
    *,
    keep: "Callable[[str], bool] | None" = None,
) -> list[tuple[str, str | None]]:
    """Walk `lines` in file order; anchor untimed entries to preceding timed
    entries; drop entries older than 24h or with no anchor.

    OpenClaw writes multi-line error blocks where only the first line carries
    the ISO timestamp prefix — the JSON body and retry messages that follow
    are untimed:

        2026-05-09T10:09:16.284-07:00 [agent/embedded] context overflow ...
        [memory] embeddings rate limited; retrying in 1004ms
        [memory] sync failed (watch): Error: openai embeddings failed: 429 {
            "error": { "message": "You exceeded your current quota..." }
        }

    Each untimed line inherits the timestamp of the most recent preceding
    timed line so a multi-line block ages out as a unit. Untimed lines with
    no preceding timed anchor are dropped: a buffer that begins mid-block has
    lost the context to date those lines, and keeping them produced the bug
    where post-fix log displays surfaced pre-fix error spam forever on quiet
    bots (see team_bot_a's May-9 memorySearch reload — 429 blocks from before the
    fix kept appearing in the maintenance UI for >48h after).

    The caller passes the full unfiltered buffer in file order so anchors
    update correctly as we walk. `keep` is an optional predicate (e.g.
    ``lambda l: "error" in l.lower()``) that limits which lines are emitted
    in the result; non-matching lines still update the running anchor so a
    matching line later in the buffer can inherit a fresh timestamp from a
    timed header that doesn't itself contain "error".

    Returns `[(line, ts_str_or_inherited), ...]` in original order. The
    second element is the line's own timestamp when present, otherwise the
    inherited anchor — never None for kept entries.
    """
    now = datetime.now(timezone.utc)
    last_anchor_ts: str | None = None
    last_anchor_dt: datetime | None = None
    kept: list[tuple[str, str | None]] = []
    for line in lines:
        own_ts = _parse_log_ts(line)
        if own_ts:
            try:
                dt = datetime.fromisoformat(own_ts.rstrip("Z").replace("T", " "))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_anchor_ts = own_ts
                last_anchor_dt = dt
            except (ValueError, OverflowError):
                # Malformed but parseable-prefix timestamp; treat as untimed.
                pass
        if keep is not None and not keep(line):
            continue
        # Drop untimed lines with no anchor — we can't tell if they're fresh.
        if last_anchor_dt is None:
            continue
        if (now - last_anchor_dt).total_seconds() > _LOG_LINE_MAX_AGE_S:
            continue
        kept.append((line, own_ts or last_anchor_ts))
    return kept


# ── Error categorization (mirror of _ERR_*_PATTERNS in web/index.html) ───────
# Kept in lockstep with the JS so heal.py and the frontend agree on the
# (provider, condition) key for any given log line. If a new pattern is added,
# update both places.
_PROVIDER_PATTERNS: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(r"candidate=([a-z0-9_/-]+)", re.I), lambda m: m.group(1).split("/")[0]),
    (re.compile(r"provider=([a-z0-9_/-]+)", re.I),  lambda m: m.group(1).split("/")[0]),
    (re.compile(r"ENETUNREACH\s+(?:149\.154\.|91\.108\.|5\.28\.)", re.I), lambda m: "telegram"),
    (re.compile(r"ENETUNREACH", re.I),              lambda m: "network"),
]

_CONDITION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"context overflow", re.I),         "context_overflow"),
    (re.compile(r"rate_limit|exceeded your current quota|reached your specified API usage limits", re.I), "quota_exceeded"),
    (re.compile(r"ENETUNREACH", re.I),              "unreachable"),
    (re.compile(r"pricing bootstrap failed", re.I), "pricing_fail"),
    (re.compile(r"LLM request timed out|FailoverError.*timed out", re.I), "timeout"),
    (re.compile(r"invalid.api.key|unauthorized|auth.*fail", re.I), "auth_failed"),
    (re.compile(r"candidate_failed", re.I),         "model_failed"),
]

# Providers we trust the success-comparison logic for. Other providers
# (Brave search, GitHub tool calls, plugin-defined providers) lack a clean
# success signal in turns-jsonl — a turn record from those wouldn't necessarily
# mean "the provider's API answered usefully." Leave their chips at the tier 1
# fade rather than risk a false "recovered" marker.
_RECOVERY_TRUSTED_PROVIDERS = frozenset({"openai", "anthropic", "google"})

_RECOVERED_DECAY_S = 24 * 3600


def _categorize_error_line(line: str) -> tuple[str | None, str | None]:
    """Parse (provider, condition) out of a log line. Either may be None."""
    provider: str | None = None
    for rx, extract in _PROVIDER_PATTERNS:
        m = rx.search(line)
        if m:
            provider = extract(m)
            break
    condition: str | None = None
    for rx, cond in _CONDITION_PATTERNS:
        if rx.search(line):
            condition = cond
            break
    return provider, condition


def _ts_to_utc_dt(ts: str | None) -> "datetime | None":
    """Parse any ISO timestamp into a tz-aware UTC datetime, or None on failure.

    The error log emits local time with offset (`...T06:09:11.967-07:00`) while
    heal.py's `ts` field is `...Z` UTC. Lexical comparison between the two is
    wrong — `06:09-07:00` (= 13:09Z) sorts as if it were earlier than `13:00Z`.
    Always normalize through this helper before comparing.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z").replace("T", " "))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _ts_after(a: str | None, b: str | None) -> bool:
    """True iff timestamp `a` represents a later instant than `b` (UTC-normalized)."""
    da, db = _ts_to_utc_dt(a), _ts_to_utc_dt(b)
    if da is None or db is None:
        return False
    return da > db


def _load_provider_last_success(bot_id: str, shared_dir: Path) -> dict[str, str]:
    """Scan today + yesterday turns-jsonl files; return {provider: latest_ts_iso}.

    Restricted to providers in _RECOVERY_TRUSTED_PROVIDERS — for everyone else,
    a turn record doesn't reliably mean "the provider's API answered well."

    Reads the world-readable shared-dir path (`{shared_dir}/{bot_id}/turns/`).
    Bot-home fallbacks would require sudo and the 24h success window comfortably
    fits in the shared dir's retention, so we skip the fallback to keep this fast.
    """
    last_ts: dict[str, str] = {}
    for days_back in (0, 1):
        d = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        path = shared_dir / bot_id / "turns" / f"turns-{d}.jsonl"
        try:
            if not path.exists():
                continue
            for raw in path.read_text().splitlines():
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                provider = (rec.get("provider") or "").strip()
                if not provider or provider == "unknown":
                    model = (rec.get("model") or "").strip()
                    if "/" in model:
                        provider = model.split("/")[0]
                if provider not in _RECOVERY_TRUSTED_PROVIDERS:
                    continue
                if provider not in last_ts or _ts_after(ts, last_ts[provider]):
                    last_ts[provider] = ts
        except (PermissionError, OSError):
            continue
    return last_ts


def _build_recovered_alerts(
    recent_errors: list[str],
    recent_errors_ts: list[str | None],
    last_success: dict[str, str],
    previous_recovered: list[dict],
) -> list[dict]:
    """Compute the new `recovered_alerts` list (sticky across heal cycles, 24h decay).

    Algorithm:
      1. Categorize each current error. If `last_success[provider] > error_ts`,
         the error is recovered — record an entry keyed by (provider, condition).
         Otherwise the same key is "actively broken" and any carried-forward
         recovered entry for that key gets demoted (dropped).
      2. Carry forward previous recovered entries that are <24h old AND not
         demoted by step 1 — this is the sticky behavior. The chip persists
         even after the underlying error log line drops out of recent_errors.
      3. Overlay step 1's new recovered entries (latest line wins on collision).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_RECOVERED_DECAY_S)

    current_active_keys: set[str] = set()
    new_recovered: dict[str, dict] = {}
    for line, ts in zip(recent_errors, recent_errors_ts):
        provider, condition = _categorize_error_line(line)
        if not condition:
            continue
        key = f"{provider or ''}:{condition}"
        recovered_ts = (
            last_success.get(provider) if provider in _RECOVERY_TRUSTED_PROVIDERS else None
        )
        if recovered_ts and ts and _ts_after(recovered_ts, ts):
            new_recovered[key] = {
                "provider": provider,
                "condition": condition,
                "error_ts": ts,
                "error_line": line,
                "recovered_ts": recovered_ts,
            }
        else:
            current_active_keys.add(key)

    out: dict[str, dict] = {}
    for entry in previous_recovered or []:
        key = f"{entry.get('provider') or ''}:{entry.get('condition') or ''}"
        if key in current_active_keys:
            continue  # demoted by a fresh active error of the same kind
        error_dt = _ts_to_utc_dt(entry.get("error_ts"))
        if error_dt is None or error_dt < cutoff:
            continue  # decay
        out[key] = entry

    for key, entry in new_recovered.items():
        out[key] = entry

    return list(out.values())

# Tool-suspend guard — see SKILL spec §5.5.5c. If the meta-test
# (tool-meta-tests M1) detects that heal's check_gateway gives wrong
# answers, the worker writes a suspend flag and heal must stop acting
# until the meta-test goes green again. The 2026-04-26 third addendum
# documented why: heal had been killing healthy gateways for an
# unknown duration because openclaw's health response shape changed.
try:
    from evolve_admin.tool_suspend import guard as _suspend_guard
except ImportError:
    # Defensive: if evolve_admin isn't on the path (e.g. heal running
    # standalone in a test env), fall back to a no-op so heal still
    # functions. The meta-test catalog still files findings; only the
    # automatic suspend-skipping is disabled in this edge case.
    def _suspend_guard(_tool: str, log_fn=None, repo_root=None) -> bool:
        return False

HEALER_VERSION = "0.1.0"


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Incident:
    bot_id: str
    detected_at: str
    type: str          # gateway_down | gateway_slow | restart_attempted |
                       # restart_succeeded | restart_failed | config_drift
    detail: str = ""
    port: int = 0
    response_time_ms: float | None = None
    healer_version: str = HEALER_VERSION

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class BotStatus:
    bot_id: str
    port: int
    healthy: bool
    response_time_ms: float | None
    error: str = ""
    restarted: bool = False
    restart_succeeded: bool | None = None
    conduct_injected: bool | None = None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve self-healing monitor")
    parser.add_argument("--network", default=str(resolve_network_path()))
    parser.add_argument("--bot-id", default=None, help="Check only this bot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    config = load_config(args.network)

    if not is_module_enabled(config, "healing"):
        print("[heal] healing module is disabled — exiting.")
        import sys; sys.exit(0)

    shared_dir = get_shared_dir(config)
    # Read heal thresholds from module config (falls back to legacy 'heal' key)
    module_cfg = get_module_config(config, "healing")
    heal_cfg = {
        "failuresBeforeProposal": module_cfg.get("failuresBeforeAlert", config.get("heal", {}).get("failuresBeforeProposal", 3)),
        "windowHours": config.get("heal", {}).get("windowHours", 24),
        "slowThresholdMs": module_cfg.get("slowThresholdMs", config.get("heal", {}).get("slowThresholdMs", 3000)),
        "restartCooldownMin": module_cfg.get("restartCooldownMin", config.get("heal", {}).get("restartCooldownMin", 10)),
        "checkTimeoutSec": config.get("heal", {}).get("checkTimeoutSec", 5),
        # `oc_health` subprocess timeout. Defaults to 30s — more generous
        # than oc_command's global 15s default — because heavy bots (esp.
        # the primary, with 7+ plugins) routinely take 7s+ and occasionally
        # cross 15s, triggering a kill-and-restart cycle. Per-bot override
        # via network.bots[bot_id].heal.ocHealthTimeoutSeconds.
        "ocHealthTimeoutSeconds": module_cfg.get(
            "ocHealthTimeoutSeconds",
            config.get("heal", {}).get("ocHealthTimeoutSeconds", 30),
        ),
        # Retry the health probe once before marking down. Catches transient
        # failures like WebSocket 1006 abnormal closure (gateway dropped the
        # connection mid-handshake during plugin churn) — a different failure
        # mode from the timeout case above. Total attempts including the first.
        "healthMaxAttempts": module_cfg.get(
            "healthMaxAttempts",
            config.get("heal", {}).get("healthMaxAttempts", 2),
        ),
        "healthRetryBackoffSeconds": module_cfg.get(
            "healthRetryBackoffSeconds",
            config.get("heal", {}).get("healthRetryBackoffSeconds", 5),
        ),
    }

    if args.daemon:
        while True:
            run_once(config, shared_dir, heal_cfg, args.bot_id, args.dry_run)
            time.sleep(args.interval)
    else:
        run_once(config, shared_dir, heal_cfg, args.bot_id, args.dry_run)


def run_once(config: dict, shared_dir: Path, heal_cfg: dict, only_bot: str | None, dry_run: bool) -> None:
    # Tool-suspend guard — runs first, before any bot checks. If the
    # meta-test caught heal making wrong decisions (e.g. interpreting
    # healthy gateways as unhealthy), the worker has set the suspend
    # flag. We MUST skip this cycle and exit cleanly — the alternative
    # is heal continuing to harm the system while we investigate.
    # The flag is cleared automatically by the worker's step 5a re-verify
    # when the meta-test goes green again.
    if _suspend_guard("heal", log_fn=lambda m: print(f"[evolve/heal] {m}")):
        return

    # Recovery pause guard (sprint B2.e). If the operator hit the panic
    # button, the pause flag exists at {shared_dir}/recovery/pause-state.json
    # and we MUST NOT restart any gateway — that would fight the operator's
    # action. We continue probing (so the dashboard still shows status) but
    # skip every restart attempt. Fails open: if the check itself errors,
    # heal continues normally rather than wedging.
    #
    # We read the flag file directly here rather than importing
    # evolve_admin.recovery — a one-line flag check is cheaper than a
    # deferred import in the hot path.
    paused = False
    try:
        pause_flag = Path(shared_dir) / "recovery" / "pause-state.json"
        if pause_flag.exists():
            data = json.loads(pause_flag.read_text(encoding="utf-8"))
            paused = bool(isinstance(data, dict) and data.get("paused"))
        if paused:
            print(f"[evolve/heal] pod is PAUSED — skipping all restart attempts this cycle")
    except (OSError, json.JSONDecodeError, ValueError):
        # Defensive: a missing/corrupt flag should NEVER block heal.
        # Continue with paused=False (normal operation).
        paused = False

    # Circuit-breaker guard + TTL reaper (spec
    # docs/spec-circuit-breakers-2026-05-21.md §3.5).
    #
    # Heal reads per-bot and pod-wide L2 ("full halt") breaker files at
    # {shared_dir}/breakers/<scope>/full.json. If a file exists and is
    # not expired, the bot is intentionally down — skip its restart.
    # If a file exists but its expires_at is in the past, this cycle
    # is the reaper: delete the file (auto-recovery) and write an
    # audit log entry. After reaping, the bot is no longer blocked and
    # its restart will be attempted on the same cycle, so a timed L2
    # trip auto-clears with no admin intervention.
    #
    # Same direct-file-read approach as pause-state above — heal-side
    # logic must not import from packages/admin and stays loadable in
    # slim subprocess contexts. Fail-open on every error path: a
    # missing or corrupt file means "no block" and heal proceeds.
    # Cost-family TTL reaper (L1 "cost" + "cost_l2"). _read_l2_breaker_state
    # below only ever reads full.json, so until 2026-07 expired L1 cost
    # trips were never reaped — three auto:spend_alert trips from June sat
    # on disk for 7+ weeks until a manual `breaker reset`. Skipped while
    # paused: the reap routes through enforce_reset, which kickstarts /
    # bootstraps gateways, and that would fight the operator's panic button.
    if not paused:
        _reap_expired_cost_breakers(shared_dir, config, dry_run)

    l2_blocked_bots, l2_pod_blocked = _read_l2_breaker_state(shared_dir)
    if l2_pod_blocked:
        print(f"[evolve/heal] pod-wide L2 breaker tripped — skipping restarts")
    if l2_blocked_bots:
        print(f"[evolve/heal] L2 breaker tripped on: "
              f"{', '.join(sorted(l2_blocked_bots))}")

    bots_cfg = config.get("bots", {})
    members = get_members(config)

    if only_bot:
        members = [only_bot]

    if not members:
        print(f"[evolve/heal] No members configured — nothing to check.")
        return

    statuses: list[BotStatus] = []

    for bot_id in members:
        # Resolve OS username — may differ from bot_id when one bot lives on a personal/shared account
        os_user = bots_cfg.get(bot_id, {}).get("user", bot_id)

        port = _get_port(bot_id, bots_cfg, shared_dir)
        if not port:
            print(f"[evolve/heal] {bot_id}: cannot determine port — skipping")
            continue

        # Repair openclaw.json BEFORE checking gateway — a corrupted config
        # (e.g. agents.main) causes crash-loops and OC CLI failures, so this
        # must run regardless of gateway health to break the circular failure.
        oc_config_path = user_home(os_user) / ".openclaw" / "openclaw.json"
        conduct_file = shared_dir / "POD_CONDUCT.md"
        if not conduct_file.exists():
            print(f"[heal] warning: {conduct_file} does not exist — skipping injection check for {bot_id}")
            conduct_injected = None
        else:
            conduct_injected = check_pod_conduct_injection(
                bot_id, oc_config_path, os_user=os_user, conduct_src=conduct_file,
            )

        oc_health_timeout = _resolve_oc_health_timeout(bot_id, bots_cfg, heal_cfg)
        status = check_gateway(bot_id, port, heal_cfg, oc_health_timeout=oc_health_timeout)
        status.conduct_injected = conduct_injected

        statuses.append(status)

        if not status.healthy:
            _record_incident(shared_dir, Incident(
                bot_id=bot_id, detected_at=now_iso(),
                type="gateway_down", detail=status.error, port=port,
            ))

            # Recovery-pause guard (B2.e): if the operator hit the panic
            # button, a "down" gateway is the EXPECTED state. Record the
            # incident for visibility but never restart — that would
            # fight the operator and instantly resurrect every bot.
            # Same logic applies to L2 circuit breakers — a "down"
            # gateway under an active L2 trip is intended state.
            bot_l2_blocked = bot_id in l2_blocked_bots or l2_pod_blocked
            if paused:
                print(f"[evolve/heal] {bot_id}: down — POD PAUSED, not restarting")
            elif bot_l2_blocked:
                print(f"[evolve/heal] {bot_id}: down — L2 BREAKER tripped, not restarting")
            elif not dry_run and not _in_restart_cooldown(bot_id, shared_dir, heal_cfg):
                print(f"[evolve/heal] {bot_id}: down — attempting restart")
                _record_incident(shared_dir, Incident(
                    bot_id=bot_id, detected_at=now_iso(),
                    type="restart_attempted", port=port,
                    detail=f"Gateway at :{port} not responding",
                ))
                success = restart_gateway(bot_id, os_user=os_user)
                incident_type = "restart_succeeded" if success else "restart_failed"
                _record_incident(shared_dir, Incident(
                    bot_id=bot_id, detected_at=now_iso(),
                    type=incident_type, port=port,
                ))
                status.restarted = True
                status.restart_succeeded = success

                if success:
                    print(f"[evolve/heal] {bot_id}: restart succeeded")
                else:
                    print(f"[evolve/heal] {bot_id}: restart FAILED")
                    _alert_failure(bot_id, port, config)
            elif dry_run:
                print(f"[evolve/heal] {bot_id}: [dry-run] would attempt restart")

        elif status.response_time_ms and status.response_time_ms > heal_cfg.get("slowThresholdMs", 3000):
            _record_incident(shared_dir, Incident(
                bot_id=bot_id, detected_at=now_iso(),
                type="gateway_slow", port=port,
                response_time_ms=status.response_time_ms,
                detail=f"Response time {status.response_time_ms:.0f}ms",
            ))
            print(f"[evolve/heal] {bot_id}: slow ({status.response_time_ms:.0f}ms)")
        else:
            print(f"[evolve/heal] {bot_id}: OK ({status.response_time_ms:.0f}ms)")

    # Write status files to shared_dir/status/ for each checked bot
    for status in statuses:
        _write_status_file(status.bot_id, bots_cfg, shared_dir, status, config)

    # Pattern detection — runs once across all checked bots, aggregating
    # affected bots into a single pod-wide watchdog event rather than
    # filing one investigation proposal per bot.
    if not dry_run:
        _check_patterns(statuses, shared_dir, config, heal_cfg)

    # Git-based drift detection — compare live openclaw.json vs last backup commit
    if not dry_run:
        for status in statuses:
            drifts = detect_backup_drift(status.bot_id, shared_dir, config)
            if drifts:
                for drift in drifts:
                    print(f"[heal] {status.bot_id}: DRIFT — {drift}")
                _alert_backup_drift(status.bot_id, drifts, config)
                # Coalesce repeated detections of the SAME drift (same drifted
                # key-set) onto one incident within the window instead of
                # minting a new file every tick — config_drift never
                # self-resolves until baseline is accepted, so an
                # un-coalesced writer bursts hundreds of identical files/day.
                _record_incident(
                    shared_dir,
                    Incident(
                        bot_id=status.bot_id,
                        detected_at=now_iso(),
                        type="config_drift",
                        detail="; ".join(drifts),
                    ),
                    signature=_config_drift_signature(drifts),
                    coalesce_window_hours=heal_cfg.get("windowHours", 24),
                )
                _emit_watchdog_alert(
                    shared_dir=shared_dir,
                    event_type="config_drift_unexplained",
                    severity="alert",
                    bot_id=status.bot_id,
                    details={
                        "drifts": drifts,
                        "summary": f"Unexplained config drift on {status.bot_id} vs git backup",
                        "guidance": "Check git backup vs live state; look for unauthorized modification.",
                    },
                    dedup_window_hours=heal_cfg.get("windowHours", 24),
                )
            else:
                print(f"[heal] {status.bot_id}: drift check OK")


# ── Health check ──────────────────────────────────────────────────────────────

def _read_l2_breaker_state(shared_dir: str | Path) -> tuple[set[str], bool]:
    """Return (per-bot-blocked set, pod-wide-blocked bool) for L2 breakers.

    Reads every ``{shared_dir}/breakers/<scope>/full.json`` file and:
      - If the file exists and expires_at is in the future (or None),
        the scope is blocked.
      - If the file exists and expires_at has passed, this cycle is the
        TTL reaper: delete the file and append an "auto_recover" entry
        to ``{shared_dir}/breakers/log/<YYYY-MM-DD>.jsonl``, and treat
        the scope as no-longer-blocked.

    Same direct-file-read approach as the pause-state guard above —
    must work without importing breakers.store (heal runs in slim
    subprocess contexts). Fail-open at every error path: anything
    unparseable is treated as "no block" so heal proceeds normally.

    Spec: docs/spec-circuit-breakers-2026-05-21.md §3.5.
    """
    breakers_root = Path(shared_dir) / "breakers"
    blocked_bots: set[str] = set()
    pod_blocked = False

    if not breakers_root.is_dir():
        return blocked_bots, pod_blocked

    now = datetime.now(timezone.utc)

    for scope_dir in breakers_root.iterdir():
        # Skip the audit-log subdir.
        if not scope_dir.is_dir() or scope_dir.name == "log":
            continue
        full_file = scope_dir / "full.json"
        if not full_file.is_file():
            continue

        try:
            data = json.loads(full_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt — fail-open. Don't block, don't delete (operator
            # may want to inspect).
            continue
        if not isinstance(data, dict):
            continue

        expires_at = data.get("expires_at")
        is_active = True
        if expires_at is not None:
            try:
                s = str(expires_at)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                exp = datetime.fromisoformat(s)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now >= exp:
                    is_active = False
            except (ValueError, TypeError):
                # Malformed expiry — fail-safe (assume active to avoid
                # accidentally clearing a real trip).
                is_active = True

        if not is_active:
            # Reaper: delete the file and log the auto_recover.
            try:
                full_file.unlink()
            except OSError:
                # Best-effort. If we can't delete, the trip stays — heal
                # will reach the same conclusion next cycle.
                pass
            try:
                _write_breaker_audit_auto_recover(
                    shared_dir=Path(shared_dir),
                    scope=scope_dir.name, breaker_type="full",
                    record=data, now=now,
                )
            except OSError:
                pass
            print(f"[evolve/heal] reaped expired L2 breaker on "
                  f"{scope_dir.name} (trip_id="
                  f"{(data.get('trip_id') or '')[:8]})")
            continue

        # Active trip.
        if scope_dir.name == "pod":
            pod_blocked = True
        else:
            blocked_bots.add(scope_dir.name)

    return blocked_bots, pod_blocked


def _write_breaker_audit_auto_recover(
    *, shared_dir: Path, scope: str, breaker_type: str,
    record: dict, now: datetime, note: str | None = None,
) -> None:
    """Append an auto_recover entry to today's breaker audit log.

    Mirrors the audit-log schema in breakers.store._append_audit
    but inlined here so heal.py stays self-contained (no breakers.store
    import). One-line JSONL append; O_APPEND is atomic for sub-PIPE_BUF
    writes."""
    log_dir = shared_dir / "breakers" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
    entry = {
        "action": "auto_recover",
        "scope": scope,
        "type": breaker_type,
        "trip_id": record.get("trip_id"),
        "initiated_by": "heal:reaper",
        "expires_at": record.get("expires_at"),
        "reaped_at": now.isoformat(),
    }
    if note:
        entry["note"] = note
    line = json.dumps(entry, sort_keys=True) + "\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# ── Cost-family TTL reaper (L1 "cost" + "cost_l2") ───────────────────────────
#
# _read_l2_breaker_state above owns full.json (block-state + reap in one
# self-contained pass, no imports). The cost family needs more than a file
# delete: enforce_reset restores the heartbeat stash + exec-approvals stash
# (and, for cost_l2, bootstraps the booted-out gateway — heal's own
# restart_gateway can't resurrect a bootout'd service). Bare-deleting the
# file would leave a bot whose heartbeat was disabled at trip time
# heartbeat-less forever, so the reap routes through
# breakers_enforce.enforce_reset and only deletes the file once that
# succeeds.

_BREAKERS_ENFORCE_RESET = None


def _get_breakers_enforce_reset():
    """Return breakers_enforce.enforce_reset, or None when evolve_admin
    isn't importable. Cached after first success — same lazy-seam
    pattern as spend_alert._get_l2_enforce_trip."""
    global _BREAKERS_ENFORCE_RESET
    if _BREAKERS_ENFORCE_RESET is not None:
        return _BREAKERS_ENFORCE_RESET
    try:
        from evolve_admin.breakers_enforce import enforce_reset  # type: ignore[import]
        _BREAKERS_ENFORCE_RESET = enforce_reset
        return _BREAKERS_ENFORCE_RESET
    except Exception:
        return None


def _reap_expired_cost_breakers(
    shared_dir: str | Path, config: dict, dry_run: bool,
) -> None:
    """TTL-reap expired non-"full" breaker trips (spec
    docs/spec-circuit-breakers-2026-05-21.md §3.5).

    For each breaker file whose expires_at has passed:
      1. Run breakers_enforce.enforce_reset — restores the heartbeat
         stash + exec-approvals stash (L1 cost) or bootstraps the
         gateway (cost_l2), and clears the spend-cap enforcement flag.
      2. Only then delete the file and append an "auto_recover" audit
         entry (same schema as the full.json reap above).

    A bot that has been removed from network.json makes enforce_reset
    raise ValueError; its on-disk residue (breaker file + stashes +
    empty scope dir) is cleared directly instead — there is no bot to
    restore the stashes into, and erroring forever on every cycle
    would leave the residue as a phantom trip.

    Fail-safe direction on everything else: an enforce failure leaves
    the trip file in place so the next 5-min cycle retries — readers
    already filter on expiry (spend_caps.get_active_breakers,
    breakers.store.list_active), so the lingering file can't render
    as an active breaker in the meantime.
    """
    shared_dir = Path(shared_dir)
    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        print(f"[evolve/heal] breakers.store import failed ({exc}) — "
              f"cost-breaker TTL reap skipped this cycle")
        return
    try:
        expired = [
            r for r in _bstore.list_all(shared_dir)
            if r.type != "full" and _bstore.is_expired(r)
        ]
    except Exception as exc:
        print(f"[evolve/heal] breaker listing failed ({exc}) — "
              f"cost-breaker TTL reap skipped this cycle")
        return
    if not expired:
        return

    enforce_reset = _get_breakers_enforce_reset()
    now = datetime.now(timezone.utc)

    for rec in expired:
        scope, btype = rec.bot_id, rec.type
        trip_short = (rec.trip_id or "")[:8]
        if dry_run:
            print(f"[evolve/heal] [dry-run] would reap expired {btype} "
                  f"breaker on {scope} (trip_id={trip_short})")
            continue
        if enforce_reset is None:
            # Without enforce_reset we can't restore the stashes; deleting
            # the file anyway would orphan them. Leave the trip for a
            # cycle where the import works, but say so — silence is how
            # the June trips survived 7+ weeks.
            print(f"[evolve/heal] evolve_admin.breakers_enforce unavailable "
                  f"— expired {btype} breaker on {scope} NOT reaped")
            continue
        try:
            result = enforce_reset(
                scope=scope, breaker_type=btype,
                network=config, shared_dir=shared_dir,
            )
        except ValueError:
            # Bot no longer in network.json — clear the residue directly.
            _clear_defunct_breaker_residue(shared_dir, rec, now=now)
            continue
        except Exception as exc:
            print(f"[evolve/heal] reap of {scope}/{btype} enforce_reset "
                  f"raised ({exc}) — leaving trip file for next cycle")
            continue
        if not result.ok:
            print(f"[evolve/heal] reap of {scope}/{btype} enforce_reset "
                  f"reported failure — leaving trip file for next cycle")
            continue

        try:
            breaker_path = shared_dir / "breakers" / scope / f"{btype}.json"
            breaker_path.unlink(missing_ok=True)
        except OSError as exc:
            # Enforce already ran (idempotent — no stash left to re-restore),
            # so the next cycle just retries the delete.
            print(f"[evolve/heal] could not delete {scope}/{btype} breaker "
                  f"file ({exc}) — next cycle retries")
            continue
        try:
            _write_breaker_audit_auto_recover(
                shared_dir=shared_dir, scope=scope, breaker_type=btype,
                record=rec.to_json(), now=now,
            )
        except OSError as exc:
            print(f"[evolve/heal] audit-log write failed for {scope}/{btype} "
                  f"reap ({exc})")
        print(f"[evolve/heal] reaped expired {btype} breaker on {scope} "
              f"(trip_id={trip_short})")


def _clear_defunct_breaker_residue(
    shared_dir: Path, rec, *, now: datetime,
) -> None:
    """Clear an expired trip's on-disk residue for a bot that's gone
    from network.json.

    Seen live 2026-07-31: bot "ledger" was removed from the pod but its
    breakers/ledger/ dir survived, and enforce_reset's ValueError made
    every reap attempt error forever. There is no bot to restore the
    stashes into, so delete the breaker file, both stash files, and the
    scope dir once empty (rmdir fails harmlessly if another breaker
    type is still present).
    """
    scope_dir = shared_dir / "breakers" / rec.bot_id
    for name in (f"{rec.type}.json", "heartbeat-stash.json",
                 "exec-approvals-stash.json"):
        try:
            (scope_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            print(f"[evolve/heal] could not delete residue "
                  f"{rec.bot_id}/{name} ({exc}) — next cycle retries")
    try:
        _write_breaker_audit_auto_recover(
            shared_dir=shared_dir, scope=rec.bot_id, breaker_type=rec.type,
            record=rec.to_json(), now=now,
            note="bot not in network.json; residue cleared without stash restore",
        )
    except OSError as exc:
        print(f"[evolve/heal] audit-log write failed for {rec.bot_id}/"
              f"{rec.type} residue clear ({exc})")
    try:
        scope_dir.rmdir()
    except OSError:
        # Intentional: rmdir failing means the dir isn't empty (another
        # breaker type is still present) — that's control flow, not an error.
        pass
    print(f"[evolve/heal] reaped expired {rec.type} breaker on "
          f"{rec.bot_id} (bot no longer in network.json; residue cleared)")


def _resolve_oc_health_timeout(bot_id: str, bots_cfg: dict, heal_cfg: dict) -> int:
    """Resolve the oc_health subprocess timeout for a bot.

    Per-bot override (network.bots[bot_id].heal.ocHealthTimeoutSeconds) wins
    over the heal_cfg default. Allows the primary bot — which has more
    plugins and routinely crosses the global 15s default — to be configured
    with a longer timeout without changing other bots' behavior.
    """
    bot_heal = (bots_cfg.get(bot_id) or {}).get("heal") or {}
    override = bot_heal.get("ocHealthTimeoutSeconds")
    if isinstance(override, (int, float)) and override > 0:
        return int(override)
    return int(heal_cfg.get("ocHealthTimeoutSeconds", 30))


def check_gateway(
    bot_id: str, port: int, heal_cfg: dict,
    *, oc_health_timeout: int | None = None,
) -> BotStatus:
    """Check gateway health via openclaw health --json, with retry-on-failure.

    Tolerates transient gateway connection drops (e.g. WebSocket 1006
    abnormal closure during plugin churn — the actual mechanism behind
    the evolve restart cycle that PR #352's timeout knob did NOT fix
    because the call fails fast, not slow). First probe failure → wait
    `healthRetryBackoffSeconds` (default 5s) → retry once → only mark
    down if both attempts fail.

    `oc_health_timeout` (seconds) overrides the default 15s subprocess
    timeout in oc_command. When None, defaults to heal_cfg's
    `ocHealthTimeoutSeconds` (30s).

    `heal_cfg` keys recognized:
      - ocHealthTimeoutSeconds (int, default 30) — per-attempt timeout
      - healthMaxAttempts (int, default 2) — total attempts including the first
      - healthRetryBackoffSeconds (int, default 5) — sleep between attempts
    """
    if oc_health_timeout is None:
        oc_health_timeout = int(heal_cfg.get("ocHealthTimeoutSeconds", 30))
    max_attempts = max(1, int(heal_cfg.get("healthMaxAttempts", 2)))
    backoff = max(0, int(heal_cfg.get("healthRetryBackoffSeconds", 5)))

    status: BotStatus | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            time.sleep(backoff)
        status = _probe_gateway_once(bot_id, port, oc_health_timeout)
        if status.healthy:
            return status
    # All attempts failed — return the last status (with its error message)
    return status if status is not None else BotStatus(
        bot_id=bot_id, port=port, healthy=False, response_time_ms=None,
        error="check_gateway: no probe attempts executed",
    )


def _probe_gateway_once(bot_id: str, port: int, oc_health_timeout: int) -> BotStatus:
    """One health-probe attempt. Returns BotStatus(healthy, error, response_ms).

    Factored out of check_gateway so the retry loop can call it twice
    without duplicating the response-shape detection. Pure function — no
    sleep, no I/O beyond the oc_health subprocess.
    """
    start = time.monotonic()
    status = BotStatus(bot_id=bot_id, port=port, healthy=False, response_time_ms=None)
    try:
        # Lazy import of the runtime seam to avoid circular imports
        from runtime.agent_runtime import get_runtime
        result = get_runtime().health(bot_id, timeout=oc_health_timeout)
        elapsed = (time.monotonic() - start) * 1000
        if result is None:
            status.error = "openclaw health returned no data"
            return status
        # OC health response shape has drifted across openclaw versions; accept
        # any of the known top-level liveness markers we have observed:
        #   - {"ok": true, ...}              — current openclaw 2026.4.x
        #   - {"status": "ok", ...}          — older shape
        #   - {"gateway": {"reachable": true}} — older shape
        #   - {"healthy": true}              — generic catch-all
        # Without the "ok" check, heal.py mistakes every healthy gateway as
        # down and kills it via `launchctl kickstart -k` on every 5-min cycle.
        # The resulting per-bot restart cascade wedges the mini at the TCP
        # layer (3 gateways at 100–177% CPU during restart, 43-min outage
        # observed 2026-04-26T07:22Z–08:06Z).
        status_ok = (
            result.get("ok") is True
            or result.get("status") == "ok"
            or bool(result.get("gateway", {}).get("reachable"))
            or bool(result.get("healthy"))
        )
        status.healthy = bool(status_ok)
        status.response_ms = elapsed
        status.response_time_ms = elapsed
        if not status_ok:
            status.error = str(result)
    except Exception as e:
        status.error = str(e)
    return status


# ── Restart ───────────────────────────────────────────────────────────────────

def restart_gateway(bot_id: str, os_user: str | None = None) -> bool:
    """Attempt to restart a bot's gateway.

    heal.py runs as the evolve user, not as the bot user, so we cannot use
    gui/{uid} — that would target evolve's LaunchAgent domain, not the bot's.

    Strategy (mirrors deploy.py):
      1. If a system LaunchDaemon plist exists, restart via the Scheduler
         seam (sudo launchctl kickstart -k; evolve has this sudo grant in
         /etc/sudoers.d/evolve).
      2. Otherwise fall back to: sudo -u {os_user} openclaw gateway restart.
         This works regardless of whether the bot uses a LaunchAgent or LaunchDaemon.

    os_user: resolved macOS username (may differ from bot_id when one bot lives on a personal/shared account).
             Falls back to bot_id if not provided.
    """
    if os_user is None:
        os_user = bot_id
    label = f"ai.openclaw.{bot_id}-gateway"
    system_plist = Path(f"/Library/LaunchDaemons/{label}.plist")

    if system_plist.exists():
        try:
            # Lazy import of the runtime seam — same pattern as agent_runtime
            # above. ⚠️ restart is kickstart -k: kills live gateway traffic;
            # callers own the cooldown/canary rules, not this function.
            from runtime.scheduler import get_scheduler
            ok, _out = get_scheduler().restart(label)
            if ok:
                time.sleep(3)
                return True
        except Exception:
            pass

    # Fallback: ask openclaw to restart its own gateway as the bot user
    try:
        r = subprocess.run(
            ["sudo", "-u", os_user, "env", f"HOME={user_home(os_user)}",
             "openclaw", "gateway", "restart"],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0:
            time.sleep(3)
            return True
    except Exception:
        pass

    return False


def _in_restart_cooldown(bot_id: str, shared_dir: Path, heal_cfg: dict) -> bool:
    """Return True if we restarted this bot too recently."""
    cooldown_min = heal_cfg.get("restartCooldownMin", 10)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_min)

    incidents_dir = shared_dir / "incidents"
    if not incidents_dir.exists():
        return False

    for day_dir in sorted(incidents_dir.iterdir(), reverse=True)[:2]:
        if not day_dir.is_dir():
            continue
        for f in day_dir.glob(f"{bot_id}-*.json"):
            try:
                inc = json.loads(f.read_text())
                if inc.get("type") in ("restart_attempted", "restart_succeeded"):
                    ts = datetime.fromisoformat(inc["detected_at"].replace("Z", "+00:00"))
                    if ts > cutoff:
                        return True
            except Exception:
                pass
    return False


_GATEWAY_ERROR_SNIPPET_MAX = 200


def _last_gateway_error(err_path: Path) -> tuple[str, str | None] | None:
    """Read the most recent recent-error line from a gateway.err.log file.

    Returns ``(line, ts_or_none)`` if a recent error exists, else None.
    Used to enrich the gateway_instability signal so the operator sees
    the actual error (e.g. "openai embeddings failed: 429") instead of
    just a count + generic "check the logs" guidance.

    Returns None when the file doesn't exist, is unreadable, or contains
    no recent error lines — caller must handle gracefully.
    """
    if not err_path.exists():
        return None
    try:
        lines = err_path.read_text().splitlines()[-200:]
    except (PermissionError, OSError):
        return None
    # Keywords cover the gateway.err.log error categories observed in practice:
    # explicit "Error" / "failed", HTTP 429s from provider 429-storms, fetch
    # "timeout" and process "abort" lines that bracket the underlying failures.
    error_lines = [
        l for l in lines
        if any(kw in l.lower() for kw in ("error", "fail", "timeout", "abort"))
        or "429" in l
    ]
    kept = _drop_stale_log_lines(error_lines)
    if not kept:
        return None
    line, ts = kept[-1]
    if len(line) > _GATEWAY_ERROR_SNIPPET_MAX:
        line = line[:_GATEWAY_ERROR_SNIPPET_MAX - 1] + "…"
    return line, ts


def _gateway_err_log_path(bot_id: str, config: dict) -> Path:
    """Resolve the bot's gateway.err.log path. Factored out so tests can
    monkey-patch the lookup without touching real /Users/* paths."""
    user = get_bot_user(bot_id, config)
    return user_home(user) / ".openclaw" / "logs" / "gateway.err.log"


# ── Pattern detection ─────────────────────────────────────────────────────────

def _check_patterns(
    statuses: list[BotStatus], shared_dir: Path, config: dict, heal_cfg: dict
) -> None:
    """Aggregate-across-bots pattern detection.

    Looks at gateway_down / restart_failed incidents in the last ``windowHours``
    for every bot just checked, and emits a single pod-wide
    ``gateway_instability`` watchdog event if any bot crossed threshold. This
    replaced an earlier per-bot ``investigation`` proposal — those weren't
    proposals, they were alerts, and were filling the approval queue with
    near-duplicate cards.
    """
    window_hours = heal_cfg.get("windowHours", 24)
    threshold = heal_cfg.get("failuresBeforeProposal", 3)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    incidents_dir = shared_dir / "incidents"
    if not incidents_dir.exists():
        return

    affected: dict[str, dict] = {}
    for status in statuses:
        bot_id = status.bot_id
        down = 0
        restart_failed = 0
        for day_dir in incidents_dir.iterdir():
            if not day_dir.is_dir():
                continue
            for f in day_dir.glob(f"{bot_id}-*.json"):
                try:
                    inc = json.loads(f.read_text())
                    ts = datetime.fromisoformat(inc["detected_at"].replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                    if inc.get("type") == "gateway_down":
                        down += 1
                    elif inc.get("type") == "restart_failed":
                        restart_failed += 1
                except Exception:
                    pass
        if down >= threshold:
            entry = {"gateway_down": down, "restart_failed": restart_failed}
            # Enrich with the last error from gateway.err.log so the alert is
            # actionable without ssh'ing into the host. Best-effort — the
            # log may be unreadable from the audit user, in which case the
            # alert falls back to the bare counts.
            last = _last_gateway_error(_gateway_err_log_path(bot_id, config))
            if last is not None:
                entry["last_error"] = last[0]
                if last[1]:
                    entry["last_error_ts"] = last[1]
            affected[bot_id] = entry

    if not affected:
        return

    ranked = sorted(affected.items(), key=lambda kv: -kv[1]["gateway_down"])
    bots_summary = ", ".join(
        f"{b} ({d['gateway_down']}× down, {d['restart_failed']} restart-fail)"
        for b, d in ranked
    )
    # If any affected bot surfaced a last_error, build a more directed
    # guidance line — most gateway flapping has a single chatty root cause
    # (e.g. embedding 429 storm jams event loop → fetch timeouts → restart).
    any_with_error = next((d for _, d in ranked if d.get("last_error")), None)
    if any_with_error is not None:
        guidance = (
            "Recent gateway error surfaced in the alert. If embeddings are "
            "rate-limited, fix the provider chain first — the retry storm "
            "saturates the event loop and triggers the restart cycle."
        )
    else:
        guidance = "Check gateway logs and process supervisor for the root cause."

    emitted = _emit_watchdog_alert(
        shared_dir=shared_dir,
        event_type="gateway_instability",
        severity="alert",
        bot_id=None,  # pod-wide
        details={
            "bots": affected,
            "window_hours": window_hours,
            "threshold": threshold,
            "summary": (
                f"Gateway instability across {len(affected)} bot(s) in last "
                f"{window_hours}h: {bots_summary}"
            ),
            "guidance": guidance,
        },
        dedup_window_hours=window_hours,
    )
    if emitted:
        # Dispatcher sends with parse_mode=HTML — use <b>/<code> rather
        # than Markdown *…* / `…`. Interpolated values (bot ids, error
        # strings) escaped via _html_escape so any literal <>& survives
        # Telegram's HTML parser. See report.py for the same fix.
        msg_lines = [
            "⚠️ <b>Evolve: Gateway Instability</b>",
            f"{len(affected)} bot(s) affected in last {window_hours}h:",
        ]
        for b, d in ranked:
            line = (
                f"  • <code>{_html_escape(b)}</code>: "
                f"{d['gateway_down']}× down, {d['restart_failed']} restart-fail"
            )
            if d.get("last_error"):
                line += f"\n    last error: {_html_escape(d['last_error'])}"
            msg_lines.append(line)
        msg_lines.append("Check the Alerts page in admin UI for details.")
        _alert_watchdog_event("\n".join(msg_lines), config)


# ── Watchdog event emission ───────────────────────────────────────────────────
#
# The actual emit + dedup primitives live in ``watchdog_alerts`` so other
# detectors (test_runner, etc.) can share them without depending on heal.
# Kept under the underscore-prefixed names heal callers expect, with
# heal-flavored logging via the ``log_prefix`` kwarg.


def _emit_watchdog_alert(
    *,
    shared_dir: Path,
    event_type: str,
    severity: str,
    bot_id: str | None,
    details: dict,
    dedup_window_hours: float,
) -> bool:
    """Thin wrapper around ``watchdog_alerts.emit_watchdog_alert`` that pins
    the heal-specific log prefix so existing log greps keep working."""
    from watchdog_alerts import emit_watchdog_alert
    return emit_watchdog_alert(
        shared_dir=shared_dir,
        event_type=event_type,
        severity=severity,
        bot_id=bot_id,
        details=details,
        dedup_window_hours=dedup_window_hours,
        log_prefix="evolve/heal",
    )


def _recent_watchdog_event_exists(
    shared_dir: Path, event_type: str, bot_id: str | None, window_hours: float
) -> bool:
    from watchdog_alerts import recent_watchdog_event_exists
    return recent_watchdog_event_exists(shared_dir, event_type, bot_id, window_hours)


# ── Config drift detection ────────────────────────────────────────────────────

def detect_config_drift(bot_id: str, shared_dir: Path, config: dict, os_user: str | None = None) -> list[str]:
    """
    Compare a bot's live openclaw.json against network.json expectations.
    Returns list of drift descriptions (empty = no drift).
    os_user: resolved macOS username (may differ from bot_id when one bot lives on a personal/shared account).
    """
    drifts = []
    bots_cfg = config.get("bots", {})
    expected_port = bots_cfg.get(bot_id, {}).get("port")
    if os_user is None:
        os_user = str(bots_cfg.get(bot_id, {}).get("user", bot_id))

    oc_path = user_home(os_user) / ".openclaw" / "openclaw.json"
    if not oc_path.exists():
        return []

    try:
        oc = json.loads(oc_path.read_text())
    except Exception:
        return [f"Cannot parse openclaw.json"]

    # Port drift
    actual_port = oc.get("gateway", {}).get("port")
    if expected_port and actual_port and actual_port != expected_port:
        drifts.append(f"Port mismatch: network.json says {expected_port}, openclaw.json says {actual_port}")

    # Bind address drift (should always be 127.0.0.1)
    bind = oc.get("gateway", {}).get("bind", "")
    if bind and bind not in ("127.0.0.1", "localhost", "loopback", ""):
        drifts.append(f"Gateway bind is {bind!r} — should be 127.0.0.1")

    return drifts


# ── Git-based backup drift detection ─────────────────────────────────────────

def _read_committed_file_from_workspace(
    workspace: Path, rel_path: str,
) -> dict | None:
    """Read a file from HEAD:<rel_path> in the backup workspace.

    Returns the parsed JSON dict, or None when the file doesn't exist
    in HEAD (e.g. evolve-tiers.json on a bot whose backup pre-dates the
    L10 audit fix). Other failures (corrupt JSON, git timeout) also
    return None — drift check fails open rather than alerting on
    detector errors.
    """
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(workspace),
             "show", f"HEAD:{rel_path}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _read_live_json(path: Path) -> dict | None:
    """Read a live config file, falling back to sudo cat when the
    evolve user lacks direct read access. Returns None on any failure.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, PermissionError, json.JSONDecodeError):
        try:
            result = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None


def detect_backup_drift_keys(bot_id: str, shared_dir: Path, config: dict, os_user: str | None = None) -> list[str]:
    """Like :func:`detect_backup_drift` but returns just the top-level key
    names that drifted (unexplained by the proposal pipeline). Used by the
    admin UI to render an "Accept as baseline" affordance — the caller
    only needs the keys, not human-readable descriptions.

    Empty list means either no drift, no committed baseline yet, or the
    drift is fully accounted for by recent apply-results.

    Diffs TWO files (per the L10 audit fix):
      • openclaw.json    — keys returned bare ("agents", "plugins", etc.)
      • evolve-tiers.json — keys returned namespaced as
        "tiers:<key>" (e.g. "tiers:cascade", "tiers:tierCascade") so
        operators can tell at a glance which file changed. Pre-L10 this
        file was outside drift detection entirely — manual edits of
        cascade.enabled or per-tier models silently changed live
        routing with zero alert.
    """
    if os_user is None:
        bots_cfg = config.get("bots", {})
        os_user = str(bots_cfg.get(bot_id, {}).get("user", bot_id))
    # Platform-keyed home resolution (/Users on macOS, /home on Linux);
    # a hardcoded /Users/ made config-drift detection silently no-op on
    # Linux (workspace never found). (W10-G #5.)
    oc_root = user_home(os_user or bot_id) / ".openclaw"
    workspace = oc_root / "workspace"

    # ── openclaw.json (canonical) ────────────────────────────────────────
    committed_oc = _read_committed_file_from_workspace(
        workspace, "evolve-backup/openclaw.json",
    )
    if committed_oc is None:
        return []
    live_oc = _read_live_json(oc_root / "openclaw.json")
    if live_oc is None:
        return []

    # Top-level keys we deliberately ignore — OpenClaw rewrites them on every
    # save and they aren't security-relevant. Tracking them produces a constant
    # stream of false-positive drift alerts that train operators to dismiss
    # the channel.
    #
    # ``meta``  – {lastTouchedVersion, lastTouchedAt}; touched on every save.
    #
    # NOTE (footprint audit 2026-06-28): the keys that dominate the drift
    # noise — ``agents``, ``plugins``, ``tools`` — are deliberately NOT in
    # this set. They carry security-/cost-relevant values (``agents.defaults
    # .model`` model routing, ``plugins.entries.*.enabled`` plugin state,
    # ``tools`` config) and the compare here is top-level-key-granular, so
    # allowlisting them would suppress genuine routing/plugin tampering.
    # Their churn is transient, not persistent (live diverges from baseline
    # only in the window between an authorized deploy/apply and the next
    # baseline commit), so the correct fix is keeping the baseline fresh
    # (see _refresh_local_drift_baseline on backup.py's cloud-skip paths) +
    # coalescing repeated detections (see #3314), NOT blanket suppression.
    _DRIFT_IGNORE_KEYS = frozenset({"meta"})

    # Snapshots written by backup.py have their secret values replaced with
    # stable hash markers (<REDACTED:sha256:…>). Apply the same redaction to
    # the live side so equality holds when secrets haven't rotated. Idempotent
    # — markers pass through unchanged, so pre-redaction backups also compare
    # correctly (the live side gets markers, the historical un-redacted side
    # keeps cleartext; that registers as drift, which "accept as baseline"
    # resolves by writing a freshly-redacted snapshot).
    committed_oc_r = _redact_for_drift_compare(committed_oc)
    live_oc_r = _redact_for_drift_compare(live_oc)

    oc_all_keys = (set(committed_oc_r) | set(live_oc_r)) - _DRIFT_IGNORE_KEYS
    oc_diffs = [k for k in sorted(oc_all_keys) if committed_oc_r.get(k) != live_oc_r.get(k)]

    # ── evolve-tiers.json (L10 audit follow-up) ─────────────────────────
    # The file the plugin actually reads for cascade routing. Skip
    # entirely when the backup workspace has no committed copy yet
    # (older backups won't have it; backup.py stages it now). When
    # present, compare and namespace the diffs to "tiers:<key>" so
    # operators can distinguish file-of-origin in the advisory.
    tiers_diffs: list[str] = []
    committed_tiers = _read_committed_file_from_workspace(
        workspace, "evolve-backup/evolve-tiers.json",
    )
    if committed_tiers is not None:
        live_tiers = _read_live_json(
            user_home(os_user) / ".openclaw" / "evolve-tiers.json"
        )
        if live_tiers is not None:
            tiers_all_keys = set(committed_tiers) | set(live_tiers)
            tiers_diffs = [
                f"tiers:{k}"
                for k in sorted(tiers_all_keys)
                if committed_tiers.get(k) != live_tiers.get(k)
            ]

    diffs = oc_diffs + tiers_diffs
    if not diffs:
        return []

    # Explained drift comes from TWO authorized sources:
    #   1. The proposal pipeline (RSI generators → arbiter → applier)
    #      records applies in shared_dir/proposals/apply-results/.
    #   2. The admin UI / wizard records writes in shared_dir/audit-log.jsonl.
    # Both are operator-authorized; only off-path writes (e.g. manual
    # `vim openclaw.json` or upstream OC schema rewrites) should surface
    # as "unexplained drift."
    applied_keys = _get_recently_applied_config_keys(bot_id, shared_dir)
    admin_ui_keys = _get_recent_admin_ui_writes(bot_id, shared_dir)
    explained_keys = applied_keys | admin_ui_keys
    # The audit log's `oc_keys` declaration uses bare names for
    # openclaw.json keys. For evolve-tiers.json writes, the same
    # admin-UI endpoints declare with the "tiers:" prefix (the
    # /api/admin/config/<bot>/tiers and /fallback / /cascade endpoints
    # touch evolve-tiers.json only) — see web/server.py audit calls.
    return [k for k in diffs if k not in explained_keys]


def detect_backup_drift(bot_id: str, shared_dir: Path, config: dict, os_user: str | None = None) -> list[str]:
    """Compare live openclaw.json against the last git backup commit for this bot.

    Returns a list of unexplained drift descriptions. An empty list means either
    no drift, or drift that is fully accounted for by recent apply-results.

    Reads the last committed openclaw.json from the bot's own workspace repo
    (at evolve-backup/openclaw.json, committed by backup.py).
    Silent no-op if backup.py hasn't committed anything yet.

    os_user: resolved macOS username (may differ from bot_id when one bot lives on a personal/shared account).
    """
    keys = detect_backup_drift_keys(bot_id, shared_dir, config, os_user)
    return [f"Unexplained config drift in {bot_id}: key '{k}' changed outside proposal pipeline"
            for k in keys]


# Canonical audit log filename under shared_dir (typically
# /Users/Shared/evolve). Writers (web/server.py::_audit_log_entry and
# evolve_admin/provisioning.py::_record_audit) append to this file
# JSON-line at a time; we tail it here.
_AUDIT_LOG_FILENAME = "audit-log.jsonl"

# How far back to credit operator-authorized writes as "explained drift."
# Matches the proposal apply-results retention window (7d) — longer than
# a typical backup interval so a fresh drift check after an admin-UI
# click always finds the matching audit entry.
_ADMIN_UI_DRIFT_TTL_HOURS = 168


def _get_recent_admin_ui_writes(
    bot_id: str,
    shared_dir: Path,
    hours: int = _ADMIN_UI_DRIFT_TTL_HOURS,
) -> set[str]:
    """Return top-level openclaw.json keys touched by recent admin-UI writes.

    Reads ``shared_dir/audit-log.jsonl`` and credits any entry whose
    ``bot_id`` matches, whose ``timestamp`` is within the TTL window,
    AND that carries a non-empty ``oc_keys`` array. The writer self-
    declares which top-level openclaw.json keys it touched; we just
    union them into the "explained drift" set.

    Designed to close the false-positive loop where any UI config write
    (Reconcile catalog, Seed defaults, AI Optimization edits, etc.)
    tripped "Unexplained config drift" alerts despite being operator-
    authorized. See sysadmin_watchdog/config_drift_unexplained.

    STRUCTURAL NOTE (replaces #1734's translation map):
      The previous implementation kept a remote map of audit action
      strings → top-level openclaw keys, which coupled every new
      write endpoint to a maintenance task in this file. The new
      contract: writers pass ``oc_keys`` to their audit-log call
      site — they already know what they wrote — and this reader
      consumes the field directly. No translation, no map.

      Entries without ``oc_keys`` (auth events, audit-only logs,
      network.json edits, anything that doesn't touch openclaw.json)
      are silently ignored here — they're not relevant to per-bot
      drift.

    Failures to read the log are silent (returns empty set) — the drift
    check still works against the proposal-applied set, just without
    the admin-UI credit. Better to over-alert than to under-alert when
    the audit log is missing/corrupt.
    """
    audit_path = shared_dir / _AUDIT_LOG_FILENAME
    if not audit_path.exists():
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    keys: set[str] = set()
    try:
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("bot_id") != bot_id:
                    continue
                oc_keys = entry.get("oc_keys")
                if not oc_keys or not isinstance(oc_keys, list):
                    # No structural drift-credit declared. May be a
                    # non-openclaw event (login, network.json edit) or a
                    # legacy entry written before #1734-followup landed.
                    continue
                ts_str = entry.get("timestamp", "")
                try:
                    # Tolerate both "...Z" and "...+00:00" suffixes
                    entry_dt = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00"),
                    )
                except (ValueError, AttributeError, TypeError):
                    continue
                # Normalize naive datetimes to UTC for safe comparison
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                if entry_dt < cutoff:
                    continue
                keys.update(str(k) for k in oc_keys if k)
    except OSError:
        pass
    return keys


def _get_recently_applied_config_keys(bot_id: str, shared_dir: Path) -> set[str]:
    """Return top-level openclaw.json keys touched by recent apply-results."""
    results_dir = shared_dir / "proposals" / "apply-results"
    if not results_dir.exists():
        return set()
    keys: set[str] = set()
    for f in sorted(results_dir.glob(f"{bot_id}-*.json")):
        try:
            r = json.loads(f.read_text())
            if r.get("status") != "applied":
                continue
            change = r.get("proposed_change", {})
            for k in change.keys():
                keys.add(k.split(".")[0])  # top-level key from dot-notation path
        except (OSError, json.JSONDecodeError):
            pass
    # Also check apply-results written by apply.py directly (different format)
    for f in sorted(results_dir.glob("*.json")):
        try:
            r = json.loads(f.read_text())
            if r.get("bot_id") != bot_id:
                continue
            if not r.get("success"):
                continue
            action = r.get("action_taken", "")
            if action.startswith("set_"):
                keys.add(action[4:].split(".")[0])
        except (OSError, json.JSONDecodeError):
            pass
    return keys


def _dispatch_heal(
    *,
    config: dict,
    catalog_event: str,
    dedup_key: str,
    severity_name: str = "error",
    payload: dict | None = None,
    message: str | None = None,
    cooldown_seconds: int | None = None,
) -> None:
    """Route a heal-side alert through the alerts dispatcher.

    Phase F: pass ``payload`` whenever the catalog body_template can
    render the event; fall back to ``message`` (legacy) only when the
    event's semantics don't map to a single template (e.g. variable-
    structure batches). At least one of payload/message must be set.

    Operator-tunable enable/cooldown via
    ``alerts.heal.{enabled, cooldown_seconds}`` plus per-event
    subscription preferences (security.config_drift /
    system.gateway_autorestart_failed / system.watchdog_event).
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[heal] dispatcher import failed; alert dropped: {exc}",
              file=sys.stderr)
        return

    severity_map = {
        "critical": Severity.CRITICAL,
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
    }
    shared_dir = Path(get_shared_dir(config))
    try:
        _dispatch_send(
            shared_dir=shared_dir,
            network=config,
            source="heal",
            message=message,
            payload=payload,
            severity=severity_map.get(severity_name, Severity.ERROR),
            dedup_key=dedup_key,
            catalog_event=catalog_event,
            cooldown_seconds=cooldown_seconds,
        )
    except Exception as exc:
        print(f"[heal] dispatcher.send raised; alert dropped: {exc}",
              file=sys.stderr)


def _alert_backup_drift(bot_id: str, drifts: list[str], config: dict) -> None:
    """Send alert for unexplained config drift detected vs git backup.

    Phase F: passes payload; catalog body_template renders.
    """
    drift_summary = ", ".join(drifts) if drifts else "unknown drift"
    # Dedup key includes a content hash of the sorted drift list — same
    # drifted keys → same dedup_key → cooldown-suppressed; partial fixes
    # (some keys reverted, others still drifted) → different key → fresh
    # alert. Matches the per-batch content-hash pattern audit.py uses
    # for the same reason: operators care about transitions in drift state,
    # not periodic confirmation that the same condition still holds.
    # Same fingerprint the incident writer uses to coalesce — single source
    # of truth so the page reminder and the incident file agree on "same drift".
    _drift_fingerprint = _config_drift_signature(drifts)
    # 24-hour cooldown for config_drift. Earlier this was 3600 (1h) and
    # produced the screenshot-evidenced spam pattern: same alert text re-paged
    # every hour as long as the underlying drift persisted, with no operator
    # action able to silence it short of accepting baseline. Per-content
    # dedup_key above handles "alert me when something changes"; this
    # cooldown caps the reminder cadence for identical content at one per
    # day — same semantic the audit source uses for batch findings.
    _dispatch_heal(
        config=config,
        catalog_event="security.config_drift",
        dedup_key=f"heal/config_drift/{bot_id}/{_drift_fingerprint}",
        cooldown_seconds=86_400,
        severity_name="error",
        payload={"bot_id": bot_id, "drift_summary": drift_summary},
    )


# ── Pod Conduct injection check ───────────────────────────────────────────────

def _read_with_sudo_fallback(path: Path) -> tuple[str | None, str]:
    """Read path as evolve user. Returns (text, source) where source is one of
    'direct', 'sudo', or 'failed'. Per CLAUDE.md: prefer direct read (works
    via macOS ACL on .openclaw/), fall back to /bin/cat via sudoers grant.

    Returns (None, 'failed') if both paths fail — caller MUST handle this
    explicitly rather than treating empty content as an empty file. The
    pre-fix bug was treating sudo-cat failure as "file is empty" and then
    overwriting the real file with empty content.
    """
    try:
        return path.read_text(), "direct"
    except (PermissionError, FileNotFoundError):
        pass
    r = subprocess.run(["sudo", "/bin/cat", str(path)], capture_output=True, text=True, timeout=5)
    if r.returncode == 0:
        return r.stdout, "sudo"
    return None, "failed"


def check_pod_conduct_injection(
    bot_id: str,
    config_path: Path,
    os_user: str | None = None,
    conduct_src: "Path | None" = None,
) -> bool:
    """Verify POD_CONDUCT.md is referenced in bot's workspace AGENTS.md.

    Also repairs any openclaw.json that contains the now-invalid
    ``agents.main`` key written by older versions of this code.  OC's schema
    only recognises ``agents.defaults``; the presence of ``agents.main`` causes
    "Unrecognized key: main" errors that break cron listing and model reads.

    os_user: resolved macOS username (may differ from bot_id when one bot lives on a personal/shared account).
             Falls back to bot_id if not provided.

    Pre-fix this function had two bugs that caused failure on every cycle:
    (1) sudo cat without full path & without matching sudoers grant for
    workspace/AGENTS.md → silent read failure; (2) on read failure, treated
    file as empty and would have overwritten existing AGENTS.md content with
    just the POD_CONDUCT reference if the write had succeeded. Now uses the
    CLAUDE.md-recommended direct-read pattern (ACL already grants this for
    .openclaw/) with sudo cat fallback, and ABORTS the write if the read
    failed (preserving existing content rather than destroying it).
    """
    if os_user is None:
        os_user = bot_id
    if conduct_src is None:
        # Fallback for direct callers/tests — resolve the platform-correct
        # shared dir rather than assuming the macOS /Users/Shared layout.
        conduct_src = get_shared_dir(load_config()) / "POD_CONDUCT.md"
    # config_path is ~/.openclaw/openclaw.json, so .parent is ~/.openclaw
    # and .parent / "workspace" gives ~/.openclaw/workspace. The pre-fix
    # used .parent.parent which gave ~/workspace (without .openclaw),
    # silently routing all reads/writes to a path that doesn't exist on
    # any bot. The PR #384 abort-on-unreadable change made this visible
    # in the heal log on the next cycle.
    workspace = config_path.parent / "workspace"  # ~/.openclaw/workspace
    agents_md = workspace / "AGENTS.md"

    # Repair: strip agents.main from openclaw.json if present
    config_text, _ = _read_with_sudo_fallback(config_path)
    if config_text is not None:
        try:
            config = json.loads(config_text)
            if "main" in config.get("agents", {}):
                print(f"[heal] Removing invalid agents.main from {bot_id} openclaw.json")
                config["agents"].pop("main")
                if not config["agents"]:
                    config.pop("agents")
                from staging import secure_stage
                tmp = secure_stage(json.dumps(config, indent=2))
                try:
                    r2 = subprocess.run(["sudo", "/bin/cp", str(tmp), str(config_path)],
                                        capture_output=True, text=True)
                    subprocess.run(["sudo", "/usr/sbin/chown", f"{os_user}:staff", str(config_path)],
                                   capture_output=True)
                    if r2.returncode == 0:
                        print(f"[heal] agents.main removed — restarting {bot_id} gateway")
                        from runtime.agent_runtime import get_runtime
                        get_runtime().gateway_restart(bot_id)
                finally:
                    tmp.unlink(missing_ok=True)
        except json.JSONDecodeError:
            pass

    # Check AGENTS.md for POD_CONDUCT.md reference
    existing, source = _read_with_sudo_fallback(agents_md)
    if existing is None:
        # Read failed via both paths. ABORT — never overwrite a file we
        # couldn't read first. Returning False here means the heal cycle
        # will retry next run; meanwhile AGENTS.md is preserved.
        print(f"[heal] Failed to read {agents_md} (direct + sudo cat both failed); "
              f"skipping POD_CONDUCT injection for {bot_id} this cycle")
        return False
    if "POD_CONDUCT.md" in existing:
        return True

    # Auto-repair: copy conduct file and add AGENTS.md reference
    print(f"[heal] POD_CONDUCT.md missing from {bot_id} AGENTS.md — injecting (source={source})")
    if Path(conduct_src).exists():
        conduct_dst = workspace / "POD_CONDUCT.md"
        subprocess.run(["sudo", "/bin/cp", conduct_src, str(conduct_dst)], capture_output=True)
        subprocess.run(["sudo", "/usr/sbin/chown", f"{os_user}:staff", str(conduct_dst)], capture_output=True)

    reference_line = "## Pod Conduct\nSee POD_CONDUCT.md — pod-wide behavioral rules that apply to all bots."
    new_content = existing.rstrip() + f"\n\n{reference_line}\n"
    from staging import secure_stage
    tmp = secure_stage(new_content, suffix=".md")
    try:
        wr = subprocess.run(["sudo", "/bin/cp", str(tmp), str(agents_md)],
                            capture_output=True, text=True)
        subprocess.run(["sudo", "/usr/sbin/chown", f"{os_user}:staff", str(agents_md)], capture_output=True)
    finally:
        tmp.unlink(missing_ok=True)

    if wr.returncode == 0:
        print(f"[heal] POD_CONDUCT.md injected into {bot_id} AGENTS.md")
        return True
    else:
        print(f"[heal] Failed to inject POD_CONDUCT.md into {bot_id}: {wr.stderr}")
        return False


# ── Status file writer ────────────────────────────────────────────────────────

def _extract_model_routing(config: dict) -> dict:
    """Extract model routing config summary for status file."""
    routing = config.get("models", {}).get("routing", {})
    tiers = config.get("models", {}).get("tiers", {})
    tier3_models = tiers.get("tier3", {}).get("models", [None]) or [None]
    return {
        "enabled": routing.get("enabled", False),
        "maintenance_tier": routing.get("maintenanceTier", "tier3"),
        "tier3_model": tier3_models[0],
    }


def _write_status_file(
    bot_id: str, bots_cfg: dict, shared_dir: Path, status: BotStatus, config: dict | None = None,
) -> None:
    """Write {shared_dir}/status/{bot_id}.json atomically. Never raises."""
    try:
        user = bots_cfg.get(bot_id, {}).get("user", bot_id)

        # Check process via ps auxww (lightweight liveness check). Use -ww so
        # the COMMAND column isn't truncated, otherwise the entry-point markers
        # for openclaw >= 2026.4.29 (e.g. .../openclaw/dist/entry.js) can be
        # cut off mid-path and the substring match silently fails.
        gateway_running = False
        gateway_pid = None
        try:
            proc = subprocess.run(
                ["ps", "auxww"], capture_output=True, text=True, timeout=10,
            )
            for line in proc.stdout.splitlines():
                if "grep" in line:
                    continue
                if not _is_gateway_proc_line(line, user):
                    continue
                gateway_running = True
                parts = line.split()
                if len(parts) > 1:
                    try:
                        gateway_pid = int(parts[1])
                    except ValueError:
                        pass
                break
        except Exception:
            pass

        # HTTP-reachable implies running. The process matcher above can miss the
        # gateway if a future openclaw version renames the entry point, but a
        # 200 from /evolve/status is unambiguous proof of life.
        if status.healthy and not gateway_running:
            gateway_running = True

        # Read recent errors from gateway.err.log (only accessible if running as this user)
        last_error: str | None = None
        last_error_ts: str | None = None
        recent_errors: list[str] = []
        recent_errors_ts: list[str | None] = []
        err_path = user_home(user) / ".openclaw" / "logs" / "gateway.err.log"
        # Errors that predate the current gateway process are pre-restart noise —
        # suppress them so a restart clears the error display immediately.
        proc_start: datetime | None = None
        if gateway_pid:
            proc_start = _get_proc_start_time(gateway_pid)
        try:
            if err_path.exists():
                lines = err_path.read_text().splitlines()[-100:]
                # Drop entries older than 24h before truncating to 3 — without this,
                # a quiet bot keeps the same days-old log line surfacing forever
                # because it survives in the last-100-line tail. We walk the full
                # tail (not pre-filtered to "error" lines) so anchors update in
                # file order and untimed body lines inherit the timestamp of the
                # block header that produced them — even when the header itself
                # doesn't contain the word "error".
                kept = _drop_stale_log_lines(
                    lines, keep=lambda l: "error" in l.lower(),
                )[-3:]
                if proc_start is not None:
                    # Filter out errors from before the current process started.
                    # _drop_stale_log_lines guarantees every kept item has an
                    # (own or inherited) timestamp, so a missing ts here means
                    # we genuinely can't place the line in time — drop it
                    # rather than guess it's fresh.
                    def _after_start(item: tuple[str, str | None]) -> bool:
                        ts_str = item[1]
                        if not ts_str:
                            return False
                        try:
                            dt = datetime.fromisoformat(ts_str.rstrip("Z").replace("T", " "))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            return dt >= proc_start
                        except (ValueError, OverflowError):
                            return False
                    kept = [item for item in kept if _after_start(item)]
                recent_errors = [l for l, _ in kept]
                recent_errors_ts = [t for _, t in kept]
                if recent_errors:
                    last_error = recent_errors[-1]
                    last_error_ts = recent_errors_ts[-1]
        except (PermissionError, OSError):
            pass  # running as a different user — log is inaccessible

        # Provider success timestamps + recovered-alert state machine.
        # A turn record exists only when the provider returned usable output, so
        # `last_success_ts > error_ts` for the same provider is durable evidence
        # that the underlying issue cleared. This lets the maintenance page show
        # `✓ recovered` chips instead of leaving stale red chips that send users
        # on wild goose chases for already-self-resolved API blips.
        last_success = _load_provider_last_success(bot_id, shared_dir)
        prev_recovered: list[dict] = []
        try:
            prev_path = shared_dir / "status" / f"{bot_id}.json"
            if prev_path.exists():
                prev_data = json.loads(prev_path.read_text())
                prev_recovered = prev_data.get("recovered_alerts") or []
        except (json.JSONDecodeError, PermissionError, OSError):
            pass
        recovered_alerts = _build_recovered_alerts(
            recent_errors, recent_errors_ts, last_success, prev_recovered,
        )
        providers_status = {p: {"last_success_ts": ts} for p, ts in last_success.items()}

        # Supplement with OC CLI data for richer status
        oc_data: dict | None = None
        try:
            from runtime.agent_runtime import get_runtime
            oc_data = get_runtime().status(bot_id)
        except Exception:
            pass

        ts_now = datetime.now(timezone.utc)
        status_data = {
            "bot_id": bot_id,
            "ts": ts_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_epoch": int(ts_now.timestamp()),
            "gateway_running": gateway_running,
            "gateway_reachable": status.healthy,
            "gateway_pid": gateway_pid,
            "last_error": last_error,
            "last_error_ts": last_error_ts,
            "recent_errors": recent_errors,
            # Parallel array — recent_errors_ts[i] is the ISO timestamp parsed
            # from the leading token of recent_errors[i], or None if absent.
            # The frontend uses this to render per-line age and mute stale
            # entries on the maintenance page.
            "recent_errors_ts": recent_errors_ts,
            # Per-provider last successful turn timestamp. Used by the frontend
            # to flip an active error chip to "✓ recovered" when the provider
            # has answered successfully since the error fired. Empty for
            # providers outside _RECOVERY_TRUSTED_PROVIDERS.
            "providers": providers_status,
            # Sticky list of (provider, condition) pairs whose last error has
            # been confirmed recovered. Persists for 24h after the original
            # error_ts even if the underlying log line drops out of recent_errors.
            "recovered_alerts": recovered_alerts,
            # OC-sourced fields (None if OC CLI unavailable)
            "oc_model": (
                (oc_data or {}).get("sessions", {}).get("defaults", {}).get("model")
                or (oc_data or {}).get("sessions", {}).get("defaultModel")
            ),
            "oc_channel_summary": (oc_data or {}).get("channelSummary", []),
            "oc_sessions_active": (oc_data or {}).get("agents", {}).get("sessions"),
            "oc_gateway_ms": (oc_data or {}).get("gateway", {}).get("latencyMs"),
            "conduct_injected": status.conduct_injected,
            "schema_version": 1,
            "model_routing": _extract_model_routing(config or {}),
        }

        status_dir = shared_dir / "status"
        status_dir.mkdir(parents=True, exist_ok=True)

        out = status_dir / f"{bot_id}.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(status_data, indent=2))
        tmp.rename(out)
        import os as _os
        _os.chmod(out, 0o644)  # world-readable — no secrets in status file

    except Exception as exc:
        print(f"[evolve/heal] {bot_id}: warning: could not write status file: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_port(bot_id: str, bots_cfg: dict, shared_dir: Path) -> int | None:
    # 1. From network.json bots block
    port = bots_cfg.get(bot_id, {}).get("port")
    if port:
        return int(port)
    # 2. From bot's own openclaw.json (use resolved OS user)
    os_user = bots_cfg.get(bot_id, {}).get("user", bot_id)
    oc_path = user_home(os_user) / ".openclaw" / "openclaw.json"
    if oc_path.exists():
        try:
            oc = json.loads(oc_path.read_text())
            return oc.get("gateway", {}).get("port")
        except Exception:
            pass
    return None


def _config_drift_signature(drifts: list[str]) -> str:
    """Stable signature for a set of drifted config keys.

    Same key-set → same signature (so periodic re-detection of an unresolved
    drift coalesces onto one incident); a different key-set → different
    signature (genuinely-new drift gets its own incident, never lost).
    Deliberately identical to the alert dedup fingerprint used in
    :func:`_alert_backup_drift` so the incident file and the page reminder
    agree on what "the same drift" means.
    """
    import hashlib
    return hashlib.sha256(
        "|".join(sorted(drifts)).encode("utf-8")
    ).hexdigest()[:16]


def _find_coalescible_incident(
    shared_dir: Path,
    bot_id: str,
    inc_type: str,
    signature: str,
    now_utc: datetime,
    window_hours: float,
) -> tuple[Path, dict] | None:
    """Return ``(path, data)`` of an existing same-signature incident still
    inside the coalescing window, or None.

    Searches the two most recent day-dirs so a window that straddles midnight
    still finds yesterday's open incident instead of minting a duplicate.
    """
    incidents_dir = shared_dir / "incidents"
    if not incidents_dir.exists():
        return None
    window_start = now_utc - timedelta(hours=window_hours)
    day_dirs = sorted(
        (d for d in incidents_dir.iterdir() if d.is_dir()), reverse=True
    )[:2]
    for day_dir in day_dirs:
        for f in day_dir.glob(f"{bot_id}-*-{inc_type}.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("signature") != signature:
                continue
            ts_raw = data.get("last_detected_at") or data.get("detected_at") or ""
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts < window_start:
                continue
            return f, data
    return None


def _record_incident(
    shared_dir: Path,
    incident: Incident,
    *,
    signature: str | None = None,
    coalesce_window_hours: float = 24.0,
) -> None:
    """Write an incident JSON to ``shared_dir/incidents/<day>/``.

    When ``signature`` is given the write is COALESCED: identical incidents
    (same bot, type and signature) within ``coalesce_window_hours`` collapse
    onto the FIRST file, bumping its ``count`` and ``last_detected_at`` in
    place rather than minting a new timestamped file every detection tick.
    This stops never-resolving conditions — notably config_drift, which
    re-fires on every heal tick until an operator accepts baseline — from
    bursting hundreds of identical files per day. Operators still see the
    condition is recurring (``count`` + ``first_detected_at`` /
    ``last_detected_at``), and genuinely-new drift (a different key-set →
    different signature) is never collapsed away.

    Without ``signature`` the original one-file-per-detection behavior is
    preserved unchanged.
    """
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    day_dir = shared_dir / "incidents" / day
    day_dir.mkdir(parents=True, exist_ok=True)

    if signature is not None:
        existing = _find_coalescible_incident(
            shared_dir,
            incident.bot_id,
            incident.type,
            signature,
            datetime.now(timezone.utc),
            coalesce_window_hours,
        )
        if existing is not None:
            path, data = existing
            data["count"] = int(data.get("count", 1)) + 1
            data["last_detected_at"] = incident.detected_at
            # Refresh the human-readable detail in case its wording shifted
            # while the underlying key-set (signature) stayed identical.
            if incident.detail:
                data["detail"] = incident.detail
            atomic_write_json(path, data)
            return
        # First sighting in this window → mint a fresh coalescing record.
        data = incident.to_dict()
        data["signature"] = signature
        data["count"] = 1
        data["first_detected_at"] = incident.detected_at
        data["last_detected_at"] = incident.detected_at
        out = day_dir / f"{incident.bot_id}-{signature[:8]}-{incident.type}.json"
        atomic_write_json(out, data)
        return

    # Default (non-coalesced) path — one file per detection.
    # Include microseconds so concurrent incidents from different bots (or a
    # rapid restart sequence) never collide on the same filename.
    ts = now.strftime("%H%M%S%f")
    out = day_dir / f"{incident.bot_id}-{ts}-{incident.type}.json"
    atomic_write_json(out, incident.to_dict())


def _alert_failure(bot_id: str, port: int, config: dict) -> None:
    """Phase F4: pass payload; catalog body_template + cli action render."""
    _dispatch_heal(
        config=config,
        catalog_event="system.gateway_autorestart_failed",
        dedup_key=f"heal/restart_failed/{bot_id}",
        severity_name="error",
        payload={"bot_id": bot_id, "port": port},
    )


def _alert_watchdog_event(msg: str, config: dict) -> None:
    """Send a notification for a freshly-emitted watchdog alert.

    Fires once per dedup window (controlled by the caller via
    ``_emit_watchdog_alert`` returning False when an event already exists).

    Phase F4: passes payload with the upstream-constructed ``msg`` as
    the ``title`` field. The catalog body_template wraps it under
    "⚠️ Watchdog event\\n{title}". The {title} placeholder accepts
    multi-line text — operator sees the catalog's compact header
    plus the upstream-rendered body.
    """
    # Hash the message to derive a stable per-event dedup_key.
    import hashlib
    msg_hash = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:16]
    _dispatch_heal(
        config=config,
        catalog_event="system.watchdog_event",
        dedup_key=f"heal/watchdog/{msg_hash}",
        severity_name="warning",
        payload={"title": msg},
    )


if __name__ == "__main__":
    main()
