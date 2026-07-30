"""cost_watchdog — emit Signals for cost / efficiency antipatterns.

Reads on-disk telemetry only (no LLM):
  - cost_event JSONL via cost_ledger.read_events
  - openclaw cron records at <bot home>/.openclaw/cron/jobs.json
  - openclaw cron run logs at <bot home>/.openclaw/cron/runs/<cron_id>.jsonl
  - workspace MD file sizes via os.stat

Five signal types, all under producer "cost_watchdog":

  daily_spend_high       — bot's $/day exceeds threshold
  cost_spike             — 7d total cost > N× prior 7d AND > floor $ —
                           catches week-over-week trend shifts even when
                           daily levels stay below daily_spend_high
  session_quality        — mean maintenance_ratio over rolling window
                           above threshold — bot spending sessions on
                           housekeeping instead of productive work
  automation_dominance   — non-user_turn share over rolling window
                           exceeds threshold (catches "bot is mostly
                           automation, not actually being used")
  cron_wakes_agent       — config smell: shell-only cron with
                           sessionTarget != isolated + wakeMode == now
                           (each fire wakes the main agent and may
                           spawn an unintended heartbeat turn)
  cron_overactive        — actual fires/24h exceed declared cadence
                           by ≥ factor (catches stuck/looping crons)
  context_bloat          — workspace MD file (heartbeats.md, SOUL.md,
                           AGENTS.md, TOOLS.md) exceeds size threshold

Each detection becomes a Signal via signals.store.observe(). Signatures
that fired on a previous run but didn't fire today are auto-resolved
via signals.store.sweep_resolve() at the end of the run.

Thresholds default conservatively and are tunable via network.json:

    {
      "cost_watchdog": {
        "defaults": { "daily_spend_usd": 3.0, ... },
        "bots": { "admin_bot": { "daily_spend_usd": 5.0 } }
      }
    }

Designed to run hourly under launchd as the evolve user.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cost_ledger import (
    read_events,
    rollup_per_bot_day,
    rollup_per_session,
    rollup_per_trigger_kind,
)
from evolve_config import (
    bot_home as _bot_home,
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from metrics.resolvers.cost_metrics import classify_model_tier
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "cost_watchdog"


def _shared_dir_str() -> str:
    """Resolved {shared_dir} for operator-facing remediation text.

    The operator may be on either pod (the macOS and Linux shared-dir
    defaults differ) — never bake one platform's literal into a string.
    """
    try:
        return str(get_shared_dir(load_config()))
    except Exception:
        return str(get_shared_dir({}))

# Signal types this producer emits that get suppressed when the bot
# already has a relevant breaker tripped. Spec §5.5.2: "Don't pile on.
# When a bot already has L1 cost breaker tripped, don't fire new
# cost-anomaly signals on the same bot." We map each suppressible type
# to a breakers.suppression category so the helper picks the right
# breaker types per signal.
_SUPPRESSIBLE_TYPES_TO_CATEGORY: dict[str, str] = {
    "daily_spend_high":          "cost",
    "cost_spike":                "cost",
    "session_token_outlier":     "cost",
    "heartbeat_no_model_override": "cost",
    "model_override_violated":   "cost",
    "llm_workload_redundant_with_script": "cost",
    "heartbeat_cost_by_design":  "cost",
    "heartbeat_cadence_anomaly": "cost",
    "efficiency_drift":          "cost",
    "cache_envelope_growth":     "cost",
    # model_fallback_exhaustion is the "stop burning money on broken
    # registry" signal. Cost breaker tripped means the operator already
    # has the brake on; piling up CRITICALs adds nothing.
    "model_fallback_exhaustion": "cost",
    "automation_dominance":      "automation",
    "session_quality":           "automation",
    "cron_overactive":           "automation",
    # workspace_growth is intentionally *not* suppressed by the cost
    # breaker — the operator may have tripped the breaker BECAUSE of the
    # growth, and we want them to see which file is the culprit even
    # after the breaker is on.
    # config_drift is ALSO intentionally not suppressed — load-bearing
    # config changes warrant operator attention regardless of breaker
    # state. If anything, a tripped bot whose config just changed is the
    # CASE the operator most needs surfaced.
}

# Trigger kinds emitted by openclaw — anything not "user_turn" counts as
# automation. Listed here so a future trigger_kind addition is reviewed
# rather than silently absorbed.
USER_TRIGGER_KINDS = frozenset({"user_turn"})

DEFAULTS: dict[str, Any] = {
    "daily_spend_usd": 3.00,
    "automation_ratio": 0.95,
    "automation_min_turns": 50,
    "automation_window_days": 3,
    # context_bloat: default applies to any *.md in workspace/. Files whose
    # lowercase name starts with "heartbeat" use the wider _heartbeats
    # threshold (rolling logs grow faster than reference docs). Per-file
    # and per-bot overrides via cost_watchdog.bots.<bot>.context_bloat_files
    # silence intentionally-large files.
    "context_bloat_kb": 30,
    "context_bloat_kb_heartbeats": 50,
    "cron_overactive_factor": 1.5,
    "cron_overactive_window_hours": 24,
    # session_token_outlier: a session whose cost is N× the bot's median
    # session cost over the lookback window — catches stuck loops, runaway
    # subagents, retry storms. Sessions below the absolute floor never fire
    # (a 2-event $0.10 session shouldn't fire just because median is $0.05).
    "outlier_factor": 3.0,
    "outlier_min_session_events": 5,
    "outlier_min_cost_usd": 0.50,
    "outlier_window_days": 7,
    "outlier_max_per_run": 5,
    # heartbeat_no_model_override: fires when the bot's heartbeat block has
    # no model override and the bot's primary model is high-tier. Static
    # config check — no telemetry latency.
    # model_override_violated: fires when a heartbeat session billed any
    # turn on a model other than the configured `heartbeat.model` override.
    # Catches OC's leak where follow-up turns within a heartbeat session
    # re-resolve to defaults.model.primary (see
    # docs/forensic-security_bot-model-override-2026-05-21.md).
    "override_violation_min_cost_usd": 0.10,
    "override_violation_max_per_run": 5,
    # heartbeat_session_bloat: fires when a heartbeat session ran more
    # turns than the threshold. Three-tier severity — warn at >5, alert
    # at >15, critical (catalog-side) at >30. Catches the *structural*
    # failure mode (security_bot's 33-40 turn retry storms on 2026-05-20)
    # regardless of which model billed or what dollar amount accrued —
    # so even with the haiku-primary workaround a future bot with a
    # different misconfiguration is still caught by the turn-count
    # surface. See docs/incident-cost-alerting-blackout-2026-05-20.md.
    "heartbeat_bloat_warn_turns": 5,
    "heartbeat_bloat_alert_turns": 15,
    "heartbeat_bloat_critical_turns": 30,
    "heartbeat_bloat_max_per_run": 10,
    # cost_spike: 7d total cost > multiplier × prior-7d total AND > absolute
    # floor. Catches "something changed in the last week and spend tracked
    # with it" — distinct from daily_spend_high (which is a same-day
    # threshold). Migrated out of ScoreboardAdapter so the recommendation
    # flows through the canonical Signals → Proposals pipeline.
    "cost_spike_multiplier": 2.0,
    "cost_spike_floor_usd": 5.0,
    # session_quality: mean maintenance_ratio over the trailing window
    # above this threshold. "Maintenance ratio" is the share of sessions
    # spent on configuration / housekeeping (vs. productive work) per
    # the daily metrics file. Quiet days (session_count==0) are excluded
    # from the average so off-days don't dilute the signal. Migrated out
    # of ScoreboardAdapter.
    "maintenance_ratio_threshold": 0.50,
    "maintenance_ratio_window_days": 7,
    # workspace_growth: per-file size delta over the trailing window. Catches
    # the slow-creep variant of context_bloat — a file growing 5+ KB/day for
    # weeks crosses the absolute floor late but is operator-actionable from
    # the trajectory alone. Snapshots live at {shared_dir}/cost_watchdog/
    # workspace_snapshots/<bot>/<date>.json (one per day per bot). The
    # detector only fires when at least `workspace_growth_min_window_days` of
    # history exist (one-day blips are silenced) and the current size is
    # above `workspace_growth_min_current_kb` (tiny rotating files filtered).
    "workspace_growth_kb_per_day": 3.0,
    "workspace_growth_min_window_days": 5,
    "workspace_growth_min_current_kb": 20.0,
    "workspace_growth_window_days": 14,
    "workspace_growth_max_per_run": 5,
    # efficiency_drift: rolling 7d cost-per-call by (bot, model_tier) vs the
    # prior 21d baseline. The diagnostic axis cost_spike misses — a bot's
    # total spend can stay flat while cost-per-call climbs because the cache
    # envelope is growing. Security_bot's 2026-05 Haiku heartbeat blowout lived
    # here: cadence flat, per-call cost drifted Sonnet-ward over weeks.
    # min_*_calls floors keep noisy ratios on small samples from firing.
    "efficiency_drift_multiplier": 2.0,
    "efficiency_drift_cur_window_days": 7,
    "efficiency_drift_prior_window_days": 21,
    "efficiency_drift_min_cur_calls": 20,
    "efficiency_drift_min_prior_calls": 50,
    "efficiency_drift_max_per_run": 4,
    # cache_envelope_growth: rolling 7d cache_write_tokens-per-call vs prior
    # 21d. Direct proxy for "context envelope being shoved in." Distinct
    # from efficiency_drift because it isolates the bloat *mechanism*
    # (envelope size) from output-length / model-swap confounders.
    # min_cur_tokens_per_call is the absolute floor — 100→500 tokens/call
    # is mathematically a 5× jump but operationally irrelevant.
    "cache_envelope_multiplier": 2.0,
    "cache_envelope_cur_window_days": 7,
    "cache_envelope_prior_window_days": 21,
    "cache_envelope_min_cur_calls": 20,
    "cache_envelope_min_prior_calls": 50,
    "cache_envelope_min_cur_tokens_per_call": 5000,
    # config_drift_snapshot: daily diff of load-bearing openclaw.json fields.
    # Catches when a high-impact config (model.primary, heartbeat.model,
    # heartbeat.every, tools.exec.security) changes between snapshots —
    # no audit log required. Security_bot's haiku→sonnet primary reversion went
    # unnoticed for days because nothing watched that field; this closes
    # the gap.
    #
    # Snapshots live at {shared_dir}/cost_watchdog/config_snapshots/<bot>/
    # <YYYY-MM-DD>.json. Idempotent per-day write; last-write wins.
    "config_drift_max_per_run": 5,
    # model_fallback_exhaustion: fires CRITICAL when the OC gateway log
    # records ≥N "chain_exhausted with reason=model_not_found" events in
    # the trailing window. Catches the 2026-06-03 personal-bot mode —
    # agents.defaults.models slugs without matching models.providers
    # registry entries → every fallback step throws FailoverError →
    # whole chain exhausts on every turn. Each exhausted turn can bill
    # real tokens before the chain is declared dead, so the urgency
    # tier mirrors heartbeat_session_bloat. Threshold defaults to 3 in
    # 30 minutes — small enough to fire fast, large enough that a
    # single transient (e.g. one model briefly delisted upstream) does
    # not page.
    "fallback_exhaustion_window_minutes": 30,
    "fallback_exhaustion_threshold": 3,
    "fallback_exhaustion_log_tail_bytes": 512 * 1024,
    # llm_workload_redundant_with_script: HEARTBEAT.md non-comment content
    # above this floor + projected daily cost above the dollar floor fires
    # the signal. The dollar floor is set to $1/day so a tiny test bot
    # running a 5-line workload at $0.01/hb doesn't spam the operator.
    "llm_workload_min_chars": 500,
    "llm_workload_min_daily_cost_usd": 1.00,
    "llm_workload_min_sessions": 3,
    # heartbeat_cost_by_design: warn when projected daily heartbeat spend
    # exceeds N% of the bot's daily cap; alert when it equals or exceeds
    # the cap (trip is essentially inevitable). Quiet if cap unset.
    "heartbeat_cost_warn_fraction": 0.50,
    "heartbeat_cost_alert_fraction": 1.00,
    "heartbeat_cost_min_sessions": 3,
    # heartbeat_cadence_anomaly: actual projected-to-24h heartbeat fires vs
    # declared `every` cadence. Same shape as cron_overactive — warn at 1.5×,
    # alert at 3× (likely restart-storm or stuck-cron). min_extra_fires
    # silences low-volume tripwires (1 vs 0.5 declared is not actionable).
    "heartbeat_cadence_anomaly_factor": 1.5,
    "heartbeat_cadence_anomaly_alert_factor": 3.0,
    "heartbeat_cadence_anomaly_min_extra_fires": 6,
}


# Load-bearing openclaw.json fields whose changes should surface as
# Signals. Listed as dotpaths so the snapshot function can pluck them
# generically. Each entry is (dotpath, friendly_name) — the friendly
# name is used in Signal titles and body text.
#
# Expanding this list:
# - Add fields whose silent change would meaningfully alter cost,
#   safety, or behavior. Cosmetic fields don't earn a slot.
# - When the bot's primary model is on this list, the drift Signal
#   is the operator's main feedback when a manual revert / generator
#   bug / future-feature flap edits it.
_CONFIG_DRIFT_DOTPATHS: tuple[tuple[str, str], ...] = (
    ("agents.defaults.model.primary", "primary model"),
    ("agents.defaults.model.fallbacks", "model fallbacks"),
    ("agents.defaults.heartbeat.model", "heartbeat model"),
    ("agents.defaults.heartbeat.every", "heartbeat cadence"),
    ("tools.exec.security", "exec security mode"),
)


def _thresholds_for_bot(bot_id: str, config: dict[str, Any]) -> dict[str, Any]:
    cw_cfg = config.get("cost_watchdog") or {}
    out: dict[str, Any] = dict(DEFAULTS)
    out.update(cw_cfg.get("defaults") or {})
    out.update((cw_cfg.get("bots") or {}).get(bot_id) or {})
    return out


# ── On-disk readers ──────────────────────────────────────────────────────────


def _read_with_sudo_fallback(path: Path) -> str | None:
    """Read text from path; on PermissionError fall back to ``sudo /bin/cat``.

    Mirrors the audit.py pattern. Admin's evolve user has ACL read on
    .openclaw/, so the fallback is reached only on edge cases (newly
    deployed bot, ACL not yet applied).
    """
    try:
        return path.read_text()
    except (PermissionError, OSError):
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def read_cron_jobs(bot_id: str, config: dict[str, Any] | None = None) -> list[dict]:
    path = _bot_home(bot_id, config) / ".openclaw" / "cron" / "jobs.json"
    raw = _read_with_sudo_fallback(path)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
    elif isinstance(data, list):
        jobs = data
    else:
        return []
    return [j for j in jobs if isinstance(j, dict)]


def read_cron_runs(
    bot_id: str,
    cron_id: str,
    *,
    since_ms: int,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    path = (
        _bot_home(bot_id, config)
        / ".openclaw"
        / "cron"
        / "runs"
        / f"{cron_id}.jsonl"
    )
    raw = _read_with_sudo_fallback(path)
    if raw is None:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and ts >= since_ms:
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0))
    return out


# OC-managed workspace subdirs that the heartbeat reads as bootstrap
# context. Listed explicitly because the *.md files in these dirs are
# load-bearing for context envelope size, while unknown subdirs (user
# archives, app-specific data) are not. Security_bot's 2026-05 cost incident
# rode under the bloat threshold because workspace/memory/ wasn't
# scanned — the dated memory logs there grew to 196 KB unnoticed.
_WORKSPACE_SCANNED_SUBDIRS: tuple[str, ...] = (
    "memory",
    "memory/journal",
)


def workspace_md_sizes(
    bot_id: str, config: dict[str, Any] | None = None
) -> dict[str, int]:
    """Return ``{path: size_bytes}`` for *.md files in the bot's openclaw workspace.

    Scans ``<bot home>/.openclaw/workspace/`` top-level, plus a small
    allowlist of OC-managed subdirs (``memory/``, ``memory/journal/``)
    where the heartbeat-injected files live. Files in the top-level
    keep their bare name (``HEARTBEAT.md``); files in scanned subdirs
    are returned with the subdir prefix (``memory/2026-05-02.md``) so
    threshold lookups and per-file overrides can target them precisely.

    Unknown subdirs (user archives, app data) are NOT recursed — those
    routinely hold long-lived content that isn't loaded into context.

    Empty dict on any read failure — the detector treats absence as "no
    bloat" rather than emitting a noisy signal.
    """
    workspace = _bot_home(bot_id, config) / ".openclaw" / "workspace"
    out: dict[str, int] = {}

    def _add_md_files(dir_path: Path, prefix: str) -> None:
        try:
            entries = list(dir_path.iterdir())
        except (FileNotFoundError, PermissionError, OSError):
            return
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if not entry.name.lower().endswith(".md"):
                continue
            try:
                out[f"{prefix}{entry.name}"] = entry.stat().st_size
            except (FileNotFoundError, PermissionError, OSError):
                continue

    _add_md_files(workspace, "")
    for subdir in _WORKSPACE_SCANNED_SUBDIRS:
        _add_md_files(workspace / subdir, f"{subdir}/")
    return out


def _workspace_snapshot_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "cost_watchdog" / "workspace_snapshots" / bot_id


def write_workspace_snapshot(
    shared_dir: Path,
    bot_id: str,
    sizes: dict[str, int],
    *,
    today: str,
) -> None:
    """Persist today's per-file workspace sizes for the growth-rate detector.

    Idempotent: rewrites the day's snapshot on every run so the growth-rate
    detector always sees the most recent value for the current day. Last-write
    wins. Failures are swallowed — the snapshot is best-effort state for a
    drift detector; we don't want a write failure to break Signal emission.
    """
    if not sizes:
        return
    out_dir = _workspace_snapshot_dir(shared_dir, bot_id)
    out_path = out_dir / f"{today}.json"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sizes, sort_keys=True))
        tmp.replace(out_path)
    except (OSError, PermissionError):
        return


def read_workspace_snapshots(
    shared_dir: Path,
    bot_id: str,
    *,
    days: int,
    today: str,
) -> list[tuple[str, dict[str, int]]]:
    """Return [(date, sizes), ...] for snapshots in the trailing window.

    Includes ``today`` if present. Missing days are silently skipped — the
    growth-rate detector tolerates gaps (a quiet day produces no event).
    Returned list is sorted ascending by date so callers can do oldest-vs-newest
    comparisons without re-sorting.
    """
    try:
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return []
    snap_dir = _workspace_snapshot_dir(shared_dir, bot_id)
    out: list[tuple[str, dict[str, int]]] = []
    for i in range(days + 1):  # inclusive of `today` and the day `days` back
        d = today_date - timedelta(days=i)
        path = snap_dir / f"{d.isoformat()}.json"
        try:
            raw = path.read_text()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        sizes = {
            str(k): int(v)
            for k, v in data.items()
            if isinstance(v, (int, float)) and v >= 0
        }
        out.append((d.isoformat(), sizes))
    out.sort(key=lambda kv: kv[0])
    return out


def _config_snapshot_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "cost_watchdog" / "config_snapshots" / bot_id


def _dotpath_get(d: dict, dotpath: str) -> Any:
    """Pluck a dotted-path value from a nested dict. Returns None if any
    component is missing or not a dict.
    """
    cur: Any = d
    for part in dotpath.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def collect_config_snapshot(openclaw_json: dict | None) -> dict[str, Any]:
    """Return {dotpath: value} for the load-bearing fields we monitor.

    Missing fields are recorded as ``None`` so the diff distinguishes
    "field was removed" from "field was never present in the snapshot
    history." Values are serialized as JSON-safe primitives — dicts and
    lists pass through, which is fine for fallbacks / structured values.
    """
    if not isinstance(openclaw_json, dict):
        return {dp: None for dp, _ in _CONFIG_DRIFT_DOTPATHS}
    return {dp: _dotpath_get(openclaw_json, dp) for dp, _ in _CONFIG_DRIFT_DOTPATHS}


def write_config_snapshot(
    shared_dir: Path,
    bot_id: str,
    snapshot: dict[str, Any],
    *,
    today: str,
) -> None:
    """Persist today's config-field snapshot. Idempotent per-day.

    Best-effort write — failures are swallowed because a stale snapshot
    is preferable to breaking the rest of cost_watchdog. The drift
    detector treats absence as "no comparison available" rather than
    "no drift."
    """
    out_dir = _config_snapshot_dir(shared_dir, bot_id)
    out_path = out_dir / f"{today}.json"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, sort_keys=True, default=str))
        tmp.replace(out_path)
    except (OSError, PermissionError):
        return


def read_prior_config_snapshot(
    shared_dir: Path,
    bot_id: str,
    *,
    today: str,
    max_lookback_days: int = 7,
) -> tuple[str, dict[str, Any]] | None:
    """Return the most recent prior snapshot for diffing, or None.

    Scans up to ``max_lookback_days`` back from today (exclusive — today
    itself is what we're comparing TO). Returns ``(date, snapshot)`` for
    the newest prior found. Empty / unreadable snapshots are skipped.

    A 7-day lookback handles routine gaps (cost_watchdog didn't run,
    file was unreadable that day) without losing the drift signal.
    """
    try:
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return None
    snap_dir = _config_snapshot_dir(shared_dir, bot_id)
    for i in range(1, max_lookback_days + 1):
        d = today_date - timedelta(days=i)
        path = snap_dir / f"{d.isoformat()}.json"
        try:
            raw = path.read_text()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        return d.isoformat(), data
    return None


def read_today_turns(
    bot_id: str,
    *,
    network_path: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return today's turn records for ``bot_id`` via usage_analytics.

    Mirrors spend_alert._load_live_turns — same data source the Usage
    page and the burst detector use, so the bloat Signal and the UI
    will agree on which session and how many turns. Reads up to 2
    days so a heartbeat session straddling UTC midnight isn't
    truncated when the detector runs near the day boundary.

    Returns an empty list on any read failure — cost_watchdog detectors
    use an absent-data-means-no-fire stance to avoid noise on a slow /
    misconfigured turn-collector.
    """
    now = now or datetime.now(timezone.utc)
    try:
        from usage_analytics import load_turns  # type: ignore[import]
    except ImportError:
        return []
    try:
        return load_turns(
            bot_id,
            days=2,
            end_date=now,
            network_path=network_path,
        )
    except Exception:
        return []


def read_openclaw_json(
    bot_id: str, config: dict[str, Any] | None = None
) -> dict | None:
    """Read ``<bot home>/.openclaw/openclaw.json``. Returns None on failure.

    Same sudo-fallback pattern as read_cron_jobs. Used by the heartbeat
    config-smell detector — no telemetry latency, fires on the first run
    after a bot is deployed without an explicit heartbeat model override.
    """
    path = _bot_home(bot_id, config) / ".openclaw" / "openclaw.json"
    raw = _read_with_sudo_fallback(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def read_daily_metric(
    shared_dir: Path, bot_id: str, d: date
) -> dict | None:
    """Read ``{shared_dir}/metrics/{date}/{bot_id}.json``. None on failure.

    Per-day per-bot rollup written by the metrics pipeline; carries
    ``session_count``, ``maintenance_ratio``, ``total_cost_estimated``.
    Used by detect_maintenance_ratio_high; other detectors stick to the
    cost ledger.
    """
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _describe_cadence(schedule: dict) -> str:
    if schedule.get("kind") == "every":
        ms = schedule.get("everyMs")
        if isinstance(ms, int) and ms > 0:
            secs = ms / 1000
            if secs < 60:
                return f"{secs:g}s"
            mins = secs / 60
            if mins < 60:
                return f"{mins:g}min"
            hours = mins / 60
            return f"{hours:g}h"
    if schedule.get("kind") == "cron":
        return f"`{schedule.get('expr', '?')}`"
    return "?"


# ── Severity-framework retrofit ──────────────────────────────────────────────
# Spec: docs/spec-severity-framework-2026-05-18.md §2.2
# All cost_watchdog findings live on the `cost` vector. Magnitude comes
# from anchored dollar thresholds where the detector has a $/day number,
# and from a small fixed table for config-smell + drift detectors.
#
# Anchored $/day → magnitude (matches the spec table):
#   <  $1     → 0  (silence; we never fire below the producer threshold)
#   $1-5     → 1
#   $5-25    → 2
#   $25-100  → 3
#   $100+    → 4


def _cost_magnitude_for_usd(usd: float) -> int:
    """Map a $/day amount to the 0-4 magnitude anchors in the spec."""
    if usd < 1.0:
        return 0
    if usd < 5.0:
        return 1
    if usd < 25.0:
        return 2
    if usd < 100.0:
        return 3
    return 4


# ─── Schema v2 context enrichment (PR E) ──────────────────────────────────────
#
# Cost-related Signals carry per-event context (when, who, where) so the
# operator's alert card answers triage questions without manual digging.
# Source: schema v2 cost_event fields populated by cost_event_converter.py.
#
# Five fields ride along in ``details`` when available:
#   * timestamp_local   — ISO-8601 timestamp in the pod-local timezone
#   * user_id           — channel-native id (Slack U…, Telegram chat id, etc.)
#   * user_display_name — cached human-readable name; None when no cache hit
#   * channel_id        — channel-native id (Slack channel, Telegram chat)
#   * channel_kind      — one of slack_dm / slack_channel / telegram_dm /
#                         telegram_group / discord_dm / discord_channel /
#                         internal / None
#
# Backward compat: old cost_event records (schema v1) lack these fields;
# the helper returns a dict with None values so the Signal details still
# include the keys with consistent shape — the UI renders "(unknown)" in
# their place rather than omitting the labels.


def _to_local_ts_for_signal(ts_utc: str | None) -> str | None:
    """Convert a UTC ISO-8601 timestamp to pod-local ISO-8601 for Signal details.

    Used by detectors that have a raw UTC ts in hand but no source v2
    record to crib ``timestamp_local`` from (e.g. heartbeat_session_bloat
    reads turn-collector records, which don't carry timestamp_local).
    Returns None on parse failure — UI then shows "(unknown)".

    Defers the tz import + lookup to cost_event_converter so we have a
    single tz-resolution policy across the codebase. Failure to import
    falls back to None (Signal still emits, just without local time).
    """
    if not ts_utc or not isinstance(ts_utc, str):
        return None
    try:
        # Re-use the converter's tz resolution so test setups that
        # monkeypatch the network.json tz only have to do it in one place.
        from cost_event_converter import _resolve_pod_tz, _to_local_iso
    except Exception:
        return None
    try:
        return _to_local_iso(ts_utc, _resolve_pod_tz())
    except Exception:
        return None


def _v2_context_from_event(event: dict | None) -> dict[str, Any]:
    """Extract schema v2 enrichment from one cost_event record.

    Returns a dict with all five keys, using None for any field missing
    from the source record. Old v1 records produce all-None output —
    the renderer treats that as "(unknown)" rather than hiding the row.
    """
    if not isinstance(event, dict):
        event = {}
    return {
        "timestamp_local": event.get("timestamp_local"),
        "user_id": event.get("user_id"),
        "user_display_name": event.get("user_display_name"),
        "channel_id": event.get("channel_id"),
        "channel_kind": event.get("channel_kind"),
    }


def _v2_context_from_session_slot(slot: dict | None) -> dict[str, Any]:
    """Extract schema v2 enrichment from a rollup_per_session slot.

    Mirrors _v2_context_from_event but pulls the ``first_*`` fields the
    rollup tracks per-session. Used by session-scoped detectors
    (session_token_outlier) so the alert names the user who triggered
    the runaway session, not whoever happened to be on the last turn.
    """
    if not isinstance(slot, dict):
        slot = {}
    return {
        "timestamp_local": slot.get("first_timestamp_local"),
        "user_id": slot.get("first_user_id"),
        "user_display_name": slot.get("first_user_display_name"),
        "channel_id": slot.get("first_channel_id"),
        "channel_kind": slot.get("first_channel_kind"),
    }


def _v2_context_from_session_events(
    events: list[dict] | None,
) -> dict[str, Any]:
    """Extract schema v2 enrichment by walking a list of events for one session.

    Returns the v2 fields from the chronologically-earliest event with a
    user_id (or, failing that, the earliest with a channel_id). Used by
    detectors that already have the per-session event list in hand and
    don't want to re-run a rollup just for the context fields. When no
    event in the list carries any v2 data, returns all-None (which the
    UI renders as "(unknown)").
    """
    if not events:
        return _v2_context_from_event(None)
    sorted_events = sorted(
        events, key=lambda e: str(e.get("ts") or ""),
    )
    pick: dict | None = None
    for e in sorted_events:
        if e.get("user_id"):
            pick = e
            break
    if pick is None:
        for e in sorted_events:
            if e.get("channel_id"):
                pick = e
                break
    if pick is None:
        pick = sorted_events[0]
    return _v2_context_from_event(pick)


def _v2_context_from_day(events: list[dict] | None) -> dict[str, Any]:
    """Aggregate v2 enrichment across a day-or-window of mixed events.

    daily_spend_high doesn't have a single triggering session — the
    "who" is whichever user dominated the day. Returns:
      * timestamp_local — the latest event's local time (most recent
        activity, mirrors the "right now" framing of daily_spend_high)
      * user_id / user_display_name — the user_id with the most events
        among external-user events; None when day is all-automation
      * channel_id / channel_kind — same source event as user_id

    All-None output for automation-only days; the alert then says
    "Automation only" via the renderer rather than naming a user.
    """
    if not events:
        return _v2_context_from_event(None)
    # latest local time
    latest_local: str | None = None
    latest_ts: str | None = None
    user_counts: dict[str, int] = {}
    user_anchor: dict[str, dict] = {}
    for e in events:
        ts = str(e.get("ts") or "")
        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
            tl = e.get("timestamp_local")
            if isinstance(tl, str) and tl:
                latest_local = tl
        uid = e.get("user_id")
        if isinstance(uid, str) and uid:
            user_counts[uid] = user_counts.get(uid, 0) + 1
            user_anchor.setdefault(uid, e)
    if not user_counts:
        return {
            "timestamp_local": latest_local,
            "user_id": None,
            "user_display_name": None,
            "channel_id": None,
            "channel_kind": None,
        }
    top_uid, _top_n = max(user_counts.items(), key=lambda kv: kv[1])
    anchor = user_anchor[top_uid]
    return {
        "timestamp_local": latest_local,
        "user_id": top_uid,
        "user_display_name": anchor.get("user_display_name"),
        "channel_id": anchor.get("channel_id"),
        "channel_kind": anchor.get("channel_kind"),
    }


# ── Detectors ────────────────────────────────────────────────────────────────
# Each returns a list of dicts shaped for signals_store.observe(), so the
# runner can accumulate kept_signatures and call observe() uniformly.


def detect_daily_spend(
    bot_id: str,
    today_events: list[dict],
    *,
    threshold_usd: float,
    today: str,
) -> list[dict]:
    rollup = rollup_per_bot_day(today_events)
    today_slot = rollup.get((bot_id, today)) or {}
    spend = float(today_slot.get("cost_usd") or 0.0)
    if spend < threshold_usd:
        return []
    severity = "alert" if spend >= threshold_usd * 2 else "warn"
    # v2 enrichment: who/when across today's events. None for
    # automation-only days; UI shows "Automation only" in that case.
    v2_ctx = _v2_context_from_day(today_events)
    return [
        {
            "signature": make_signature(PRODUCER, "daily_spend_high", bot_id),
            "producer": PRODUCER,
            "type": "daily_spend_high",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": f"{bot_id}: daily spend ${spend:.2f} ≥ ${threshold_usd:.2f}",
            "body": (
                f"{bot_id} spent ${spend:.2f} on LLM calls so far today "
                f"(threshold ${threshold_usd:.2f}, {today_slot.get('event_count', 0)} events). "
                f"Open the cost dashboard and check the trigger_kind breakdown."
            ),
            "details": {
                "bot_id": bot_id,
                "date": today,
                "cost_usd": round(spend, 4),
                "threshold_usd": threshold_usd,
                "event_count": int(today_slot.get("event_count") or 0),
                **v2_ctx,
                # Severity framework: magnitude scales with actual $/day
                # (cap $1=>1, $5=>2, $25=>3, $100+=>4). Active outage
                # since the spend is happening today.
                "vector": "cost",
                "magnitude": _cost_magnitude_for_usd(spend),
                "severity_active": True,
                "what_it_means": (
                    f"{bot_id} has crossed its configured daily spend "
                    f"threshold of ${threshold_usd:.2f} for today. The "
                    "bot is still running, but every additional turn is "
                    "now compounding on top of an already-elevated day. "
                    "This is an absolute-dollar check, not a trend check "
                    "— a one-day spike from an outlier session will trip "
                    "this even if the bot is normally cheap."
                ),
                "fix_steps": (
                    f"1. Open Cost → Bot detail for `{bot_id}` to see "
                    "the trigger_kind breakdown\n"
                    "2. If automation (heartbeat, cron) dominates: "
                    "investigate the loop in Maintenance → Cron jobs\n"
                    "3. If a single session dominates: open the session "
                    "in Usage → look for retry storms or runaway "
                    "subagents\n"
                    "4. Raise the threshold for this bot (if the new "
                    "level is expected) by setting "
                    f"`cost_watchdog.bots.{bot_id}.daily_spend_usd` in "
                    "network.json\n"
                    f"5. Or hard-cap the bot via `bots.{bot_id}."
                    "daily_cap_usd` so the L1 cost breaker trips "
                    "automatically next time"
                ),
            },
        }
    ]


def detect_cost_spike(
    bot_id: str,
    cur_window_events: list[dict],
    prior_window_events: list[dict],
    *,
    multiplier: float,
    floor_usd: float,
    window_days: int = 7,
) -> list[dict]:
    """7d total cost > multiplier × prior-7d AND > absolute floor.

    Distinct from ``detect_daily_spend`` (same-day threshold) — catches
    week-over-week trend shifts, where the bot's spend has stepped up
    relative to its own recent baseline regardless of absolute level.

    Quiet if either window is empty or the prior window is zero
    (no baseline to compare against — daily_spend_high covers the
    first-week-of-spend case).
    """
    cur_total = sum(float(e.get("cost_usd") or 0.0) for e in cur_window_events)
    prior_total = sum(float(e.get("cost_usd") or 0.0) for e in prior_window_events)
    if cur_total <= floor_usd:
        return []
    if prior_total <= 0:
        return []
    if cur_total <= multiplier * prior_total:
        return []

    ratio = cur_total / prior_total
    severity = "alert" if ratio >= 5.0 else "warn"
    return [
        {
            "signature": make_signature(PRODUCER, "cost_spike", bot_id),
            "producer": PRODUCER,
            "type": "cost_spike",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {window_days}d cost ${cur_total:.2f} "
                f"is {ratio:.1f}× prior {window_days}d (${prior_total:.2f})"
            ),
            "body": (
                f"{bot_id} spent ${cur_total:.2f} over the past {window_days} days "
                f"(prior {window_days}d: ${prior_total:.2f}, ratio {ratio:.1f}×, "
                f"multiplier threshold {multiplier:.1f}×, floor ${floor_usd:.0f}). "
                f"Worth checking what changed — model upgrade, traffic spike, or "
                f"unintended automation."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "cost_cur_usd": round(cur_total, 2),
                "cost_prior_usd": round(prior_total, 2),
                "ratio": round(ratio, 3),
                "multiplier_threshold": multiplier,
                "floor_usd": floor_usd,
                # v2 enrichment — top user across the current window;
                # None for automation-driven spikes.
                **_v2_context_from_day(cur_window_events),
                # Severity framework: magnitude tracks current-window $/week;
                # active = the spend is happening now.
                "vector": "cost",
                "magnitude": _cost_magnitude_for_usd(cur_total / max(window_days, 1)),
                "severity_active": True,
                "what_it_means": (
                    f"{bot_id} spent ${cur_total:.2f} over the past "
                    f"{window_days} days — {ratio:.1f}× its prior "
                    f"{window_days}-day total of ${prior_total:.2f}. "
                    "Something changed in the recent window: a new model, "
                    "a new cron, increased traffic, or a regression that "
                    "drives more automation per user turn. This catches "
                    "week-over-week trends that stay under the daily "
                    "threshold."
                ),
                "fix_steps": (
                    f"1. Open Cost → Bot detail for `{bot_id}` and "
                    "switch the timeframe to 14 days — eyeball where "
                    "the step-change starts\n"
                    "2. Cross-check against recent merges:\n"
                    f"   ssh pod_admin_user@mini sudo grep {bot_id} "
                    f"{_shared_dir_str()}/logs/admin-actions.jsonl "
                    "| tail -20\n"
                    "3. Check for a model upgrade or new cron in "
                    f"`{_bot_home(bot_id)}/.openclaw/openclaw.json` and "
                    f"`{_bot_home(bot_id)}/.openclaw/cron/jobs.json`\n"
                    "4. If the new level is intentional, raise the "
                    "spike multiplier for this bot in "
                    f"`cost_watchdog.bots.{bot_id}.cost_spike_multiplier`"
                ),
            },
        }
    ]


def detect_maintenance_ratio_high(
    bot_id: str,
    daily_metrics: list[dict],
    *,
    threshold: float,
    window_days: int,
) -> list[dict]:
    """Mean maintenance_ratio across the window is above the threshold.

    Quiet days (session_count == 0) are excluded so a few off-days don't
    pull the average down — mirrors the original ScoreboardAdapter rule.
    Returns ``[]`` when the window has no qualifying days (no signal to
    emit from zero data).
    """
    ratios = [
        float(m.get("maintenance_ratio", 0.0) or 0.0)
        for m in daily_metrics
        if int(m.get("session_count", 0) or 0) > 0
    ]
    if not ratios:
        return []
    avg = sum(ratios) / len(ratios)
    if avg <= threshold:
        return []
    return [
        {
            "signature": make_signature(PRODUCER, "session_quality", bot_id),
            "producer": PRODUCER,
            "type": "session_quality",
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: maintenance ratio {avg:.0%} over last {window_days}d "
                f"(threshold {threshold:.0%})"
            ),
            "body": (
                f"{bot_id} spent {avg:.0%} of sessions on maintenance "
                f"(configuration / housekeeping) over the last {window_days} "
                f"days, above the {threshold:.0%} threshold. Either the bot "
                f"is doing too much background config work, or productive "
                f"sessions aren't completing successfully."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "maintenance_ratio_avg": round(avg, 4),
                "threshold": threshold,
                "qualifying_days": len(ratios),
                # Drift-style finding (no $/day to anchor against);
                # magnitude scales with how far above threshold.
                "vector": "cost",
                "magnitude": 2 if avg >= max(threshold, 0.0) + 0.2 else 1,
                "what_it_means": (
                    f"{bot_id} spent {avg:.0%} of its sessions on "
                    "maintenance work (configuration churn, housekeeping, "
                    "retries) over the recent window — above the "
                    f"{threshold:.0%} threshold. Either the bot is "
                    "spinning on background work that isn't producing "
                    "user value, or productive sessions are failing and "
                    "retrying. The user-facing turn ratio is too low to "
                    "justify the spend."
                ),
                "fix_steps": (
                    f"1. Open Usage → Bot detail for `{bot_id}` and "
                    "scan the recent session list for repeat patterns\n"
                    "2. Filter sessions by trigger_kind — does heartbeat "
                    "or a specific cron dominate?\n"
                    "3. If a cron is the culprit: review its payload in "
                    f"`{_bot_home(bot_id)}/.openclaw/cron/jobs.json` — "
                    "look for retry storms or stuck loops\n"
                    "4. If heartbeats dominate: check the bot's "
                    "`heartbeat.every` cadence vs. user activity — "
                    "consider raising the interval"
                ),
            },
        }
    ]


def detect_automation_dominance(
    bot_id: str,
    window_events: list[dict],
    *,
    ratio_threshold: float,
    min_turns: int,
    window_days: int,
) -> list[dict]:
    by_kind = rollup_per_trigger_kind(window_events)
    total = sum(int(slot["event_count"]) for slot in by_kind.values())
    if total < min_turns:
        return []
    user_turns = sum(
        int(slot["event_count"])
        for kind, slot in by_kind.items()
        if kind in USER_TRIGGER_KINDS
    )
    automation = total - user_turns
    ratio = automation / total if total else 0.0
    if ratio < ratio_threshold:
        return []
    top_kinds = sorted(
        (
            (k, int(v["event_count"]))
            for k, v in by_kind.items()
            if k not in USER_TRIGGER_KINDS
        ),
        key=lambda kv: -kv[1],
    )[:3]
    top_str = ", ".join(f"{k}={n}" for k, n in top_kinds) or "n/a"
    return [
        {
            "signature": make_signature(PRODUCER, "automation_dominance", bot_id),
            "producer": PRODUCER,
            "type": "automation_dominance",
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {ratio:.0%} of turns are automation "
                f"({automation}/{total} over {window_days}d)"
            ),
            "body": (
                f"{bot_id} has {automation} automation turns vs {user_turns} user "
                f"turns over the last {window_days} days. Top automation sources: "
                f"{top_str}. Either a cron is hammering or heartbeat cadence is "
                f"too high relative to actual use."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "automation_count": automation,
                "user_turn_count": user_turns,
                "automation_ratio": round(ratio, 4),
                "ratio_threshold": ratio_threshold,
                "top_automation_kinds": dict(top_kinds),
                # v2 enrichment — almost always all-None here since this
                # alert fires on automation-dominated bots, but a
                # uniformly-shaped details block keeps the UI renderer
                # simple.
                **_v2_context_from_day(window_events),
                # Severity framework: drift-style finding. Magnitude
                # bumps from 1 → 2 when the bot is dramatically
                # automation-dominated (≥90%) since the $/day impact
                # scales with how disproportionate it is.
                "vector": "cost",
                "magnitude": 2 if ratio >= 0.9 else 1,
                "what_it_means": (
                    f"{bot_id} is running mostly on its own: "
                    f"{automation} automation turns vs {user_turns} "
                    f"user turns over the last {window_days} days "
                    f"({ratio:.0%} automation). The bot is paying LLM "
                    "costs without producing user-facing work. Top "
                    f"automation sources: {top_str}. Either a cron is "
                    "hammering, heartbeat cadence is too high relative "
                    "to actual use, or the bot is effectively idle and "
                    "should be retired."
                ),
                "fix_steps": (
                    f"1. Open Maintenance → Cron jobs filtered to "
                    f"`{bot_id}` to identify the top automation source\n"
                    "2. If heartbeats dominate: raise the cadence in "
                    f"`{_bot_home(bot_id)}/.openclaw/openclaw.json` "
                    "(`agents.defaults.heartbeat.every`)\n"
                    "3. If a specific cron dominates: disable it or "
                    "switch its sessionTarget to isolated so it stops "
                    "waking the main agent\n"
                    "4. If the bot is genuinely unused, mark it for "
                    "retirement — automation-only spend isn't worth the "
                    "monthly run rate"
                ),
            },
        }
    ]


def detect_cron_wakes_agent(bot_id: str, crons: list[dict]) -> list[dict]:
    """Static config smell: shell-only cron that wakes the main agent.

    Detection rule (all must hold):
      - enabled
      - payload.kind == "systemEvent"   (it's just a shell command)
      - sessionTarget != "isolated"     (so the main agent gets woken)
      - wakeMode == "now"

    Each fire wakes the main agent and may spawn a heartbeat turn —
    pure waste for shell-only work. Fix is sessionTarget="isolated"
    or move to a launchd plist outside openclaw.
    """
    out: list[dict] = []
    for cron in crons:
        if not cron.get("enabled", True):
            continue
        cron_id = cron.get("id") or ""
        if not cron_id:
            continue
        payload = cron.get("payload") or {}
        if payload.get("kind") != "systemEvent":
            continue
        session_target = cron.get("sessionTarget") or "main"
        if session_target == "isolated":
            continue
        wake_mode = cron.get("wakeMode") or "now"
        if wake_mode != "now":
            continue
        name = cron.get("name") or cron_id
        cadence = _describe_cadence(cron.get("schedule") or {})
        scope_key = f"{bot_id}/{cron_id}"
        out.append(
            {
                "signature": make_signature(PRODUCER, "cron_wakes_agent", scope_key),
                "producer": PRODUCER,
                "type": "cron_wakes_agent",
                "flavor": "maintenance",
                "severity": "warn",
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: cron '{name}' is shell-only but wakes the main agent"
                ),
                "body": (
                    f"Cron '{name}' (every {cadence}) runs a shell command "
                    f"(payload.kind=systemEvent) but has sessionTarget="
                    f"\"{session_target}\" + wakeMode=\"{wake_mode}\", so each fire "
                    f"wakes the main agent and may spawn a heartbeat turn. "
                    f"Set sessionTarget=\"isolated\" or convert to a launchd plist."
                ),
                "details": {
                    "bot_id": bot_id,
                    "cron_id": cron_id,
                    "cron_name": name,
                    "session_target": session_target,
                    "wake_mode": wake_mode,
                    "cadence": cadence,
                    "shell": (payload.get("text") or "")[:200],
                    # Severity framework: config smell. Each individual
                    # fire is cheap but it compounds — magnitude 1.
                    "vector": "cost",
                    "magnitude": 1,
                    "what_it_means": (
                        f"Cron `{name}` on {bot_id} runs a shell "
                        "command but is configured to wake the main "
                        "agent every time it fires (sessionTarget="
                        f"`{session_target}`, wakeMode=`{wake_mode}`). "
                        "Every fire spawns a heartbeat-style agent turn "
                        "even though the work is pure shell — pure "
                        f"waste at the bot's primary-model rates. "
                        f"Compounded across every {cadence} interval."
                    ),
                    "fix_steps": (
                        f"1. Open Maintenance → Cron jobs and find "
                        f"`{name}` on `{bot_id}`\n"
                        "2. Set sessionTarget to `isolated` — the shell "
                        "command still runs, but no agent turn is "
                        "spawned\n"
                        "3. Or, if the cron doesn't need OC's "
                        "scheduling at all, move it to a launchd plist "
                        f"under `/Library/LaunchDaemons/` outside "
                        "openclaw\n"
                        "4. Verify by checking the next few cron runs "
                        "in Usage — they should no longer show as agent "
                        "turns"
                    ),
                },
            }
        )
    return out


def detect_cron_overactive(
    bot_id: str,
    crons: list[dict],
    *,
    factor: float,
    window_hours: int,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Fire when actual fires in the window exceed declared cadence × factor.

    Only applies to ``schedule.kind == "every"`` (predictable cadence).
    Cron-expression jobs are skipped because expected count is variable.
    """
    out: list[dict] = []
    now_dt = now or datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    since_ms = now_ms - window_hours * 3600 * 1000
    for cron in crons:
        if not cron.get("enabled", True):
            continue
        cron_id = cron.get("id") or ""
        if not cron_id:
            continue
        sched = cron.get("schedule") or {}
        if sched.get("kind") != "every":
            continue
        every_ms = sched.get("everyMs")
        if not isinstance(every_ms, int) or every_ms <= 0:
            continue
        expected = (window_hours * 3600 * 1000) / every_ms
        if expected <= 0:
            continue
        runs = read_cron_runs(bot_id, cron_id, since_ms=since_ms, config=config)
        actual = sum(1 for r in runs if r.get("action") == "finished")
        ratio = actual / expected
        if ratio < factor:
            continue
        name = cron.get("name") or cron_id
        scope_key = f"{bot_id}/{cron_id}"
        out.append(
            {
                "signature": make_signature(PRODUCER, "cron_overactive", scope_key),
                "producer": PRODUCER,
                "type": "cron_overactive",
                "flavor": "maintenance",
                "severity": "warn",
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: cron '{name}' fired {actual}× in {window_hours}h "
                    f"(expected ~{expected:.0f})"
                ),
                "body": (
                    f"Cron '{name}' is firing {ratio:.1f}× its declared cadence "
                    f"({_describe_cadence(sched)}). May indicate a stuck loop, "
                    f"duplicate scheduling, or a clock-skew bug. Inspect "
                    f"runs/{cron_id}.jsonl for tight intervals."
                ),
                "details": {
                    "bot_id": bot_id,
                    "cron_id": cron_id,
                    "cron_name": name,
                    "actual_fires": actual,
                    "expected_fires": round(expected, 1),
                    "ratio": round(ratio, 2),
                    "window_hours": window_hours,
                    "every_ms": every_ms,
                    # Severity framework: stuck-loop suspicion. Bump
                    # magnitude when the cron is firing ≥2× expected
                    # (probably an active drift, not a one-off).
                    "vector": "cost",
                    "magnitude": 2 if ratio >= 2.0 else 1,
                    "severity_active": ratio >= 2.0,
                    "what_it_means": (
                        f"Cron `{name}` on {bot_id} fired {actual} "
                        f"times in the last {window_hours}h — about "
                        f"{ratio:.1f}× its declared "
                        f"cadence of {_describe_cadence(sched)}. The "
                        "scheduler is either stuck in a loop, "
                        "double-scheduled, or hitting a clock-skew bug. "
                        "Even cheap crons add up at this firing rate."
                    ),
                    "fix_steps": (
                        f"1. Inspect the cron run log directly:\n"
                        f"   ssh pod_admin_user@mini sudo /bin/cat "
                        f"{_bot_home(bot_id)}/.openclaw/cron/runs/"
                        f"{cron_id}.jsonl | tail -50\n"
                        "2. Look for tight intervals between "
                        "consecutive `finished` records — a healthy "
                        "cron's gaps match its declared cadence\n"
                        f"3. If the cron is stuck looping, disable it "
                        f"via the admin UI (Maintenance → Cron jobs)\n"
                        "4. Restart the gateway to clear any in-memory "
                        "scheduler state:\n"
                        "   ssh pod_admin_user@mini sudo launchctl kickstart "
                        f"-k system/ai.openclaw.gateway.{bot_id}"
                    ),
                },
            }
        )
    return out


def _threshold_kb_for_file(filename: str, thresholds: dict[str, Any]) -> float:
    """Resolve the size threshold (KB) for a given workspace file.

    Precedence:
      1. Explicit per-file override at thresholds["context_bloat_files"][filename]
      2. "Heartbeat-style" rolling-log bucket if filename starts with "heartbeat"
         (case-insensitive — covers heartbeats.md, HEARTBEAT.md, heartbeat-log.md)
      3. Default reference-doc bucket
    """
    overrides = thresholds.get("context_bloat_files") or {}
    if filename in overrides:
        try:
            return float(overrides[filename])
        except (TypeError, ValueError):
            pass
    if filename.lower().startswith("heartbeat"):
        return float(
            thresholds.get("context_bloat_kb_heartbeats")
            or DEFAULTS["context_bloat_kb_heartbeats"]
        )
    return float(thresholds.get("context_bloat_kb") or DEFAULTS["context_bloat_kb"])


def detect_context_bloat(
    bot_id: str,
    sizes: dict[str, int],
    thresholds: dict[str, Any],
) -> list[dict]:
    out: list[dict] = []
    for name, size_bytes in sorted(sizes.items()):
        size_bytes = int(size_bytes or 0)
        threshold_kb = _threshold_kb_for_file(name, thresholds)
        threshold_bytes = int(threshold_kb * 1024)
        if size_bytes < threshold_bytes:
            continue
        size_kb = size_bytes / 1024
        scope_key = f"{bot_id}/{name}"
        out.append(
            {
                "signature": make_signature(PRODUCER, "context_bloat", scope_key),
                "producer": PRODUCER,
                "type": "context_bloat",
                "flavor": "maintenance",
                "severity": "warn",
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: {name} is {size_kb:.0f} KB "
                    f"(threshold {threshold_kb:.0f} KB)"
                ),
                "body": (
                    f"{name} in {bot_id}'s workspace is {size_kb:.0f} KB, larger "
                    f"than the {threshold_kb:.0f} KB target. This file loads into "
                    f"every turn's context — at Sonnet rates the cost compounds "
                    f"across automation turns. Trim, rotate, or add a per-file "
                    f"override to silence if intentionally large."
                ),
                "details": {
                    "bot_id": bot_id,
                    "filename": name,
                    "size_bytes": size_bytes,
                    "size_kb": round(size_kb, 1),
                    "threshold_kb": threshold_kb,
                    # Severity framework: file loads every turn so cost
                    # impact compounds. Bump magnitude when 3× over
                    # threshold (cost grows ~linearly with size).
                    "vector": "cost",
                    "magnitude": 2 if size_kb >= 3 * threshold_kb else 1,
                    "what_it_means": (
                        f"`{name}` in {bot_id}'s workspace is "
                        f"{size_kb:.0f} KB, larger than the "
                        f"{threshold_kb:.0f} KB target. This file is "
                        "loaded into every turn's context, so its size "
                        "compounds across automation turns at the "
                        "bot's primary-model rates. Heartbeat-heavy "
                        "bots pay this many times per hour."
                    ),
                    "fix_steps": (
                        f"1. Inspect the file:\n"
                        f"   ssh pod_admin_user@mini sudo /bin/cat "
                        f"{_bot_home(bot_id)}/.openclaw/workspace/{name}\n"
                        "2. Decide: trim (delete stale sections), "
                        "rotate (archive old content to a sibling "
                        f"file), or silence if intentional\n"
                        "3. To trim/rotate, use `evo revise` from the "
                        "operator chat or edit the file in place on "
                        "the mini\n"
                        f"4. To silence as intentional, set "
                        f"`cost_watchdog.bots.{bot_id}."
                        f"context_bloat_files.{name}` to a higher KB "
                        "value in network.json"
                    ),
                },
            }
        )
    return out


def _cache_write_per_call(events: list[dict]) -> tuple[int, int]:
    """Return ``(total_cache_write_tokens, event_count)`` over the events."""
    total = 0
    n = 0
    for e in events:
        total += int(e.get("cache_write_tokens") or 0)
        n += 1
    return total, n


def detect_cache_write_volume(
    bot_id: str,
    cur_window_events: list[dict],
    prior_window_events: list[dict],
    *,
    multiplier: float,
    cur_window_days: int,
    prior_window_days: int,
    min_cur_calls: int,
    min_prior_calls: int,
    min_cur_tokens_per_call: int,
) -> list[dict]:
    """Fire when cache_write_tokens/call is N× its prior baseline.

    Direct proxy for "context envelope being shoved in." When this fires
    alongside ``efficiency_drift``, attribution is automatic: the bot's
    per-call cost is up *because* its per-call envelope grew. When it fires
    alongside ``workspace_growth``, the file growing is the cache-write
    source — root cause without operator triage.

    ``min_cur_tokens_per_call`` is the absolute floor: a ratio jump from
    100 tokens to 500 tokens/call is mathematically a 5× spike but
    operationally irrelevant. Only fires when the current envelope is at
    least the floor (default 5000 tokens — the scale at which cache writes
    materially affect Haiku cost).
    """
    cur_total, cur_calls = _cache_write_per_call(cur_window_events)
    prior_total, prior_calls = _cache_write_per_call(prior_window_events)
    if cur_calls < min_cur_calls or prior_calls < min_prior_calls:
        return []
    cur_per_call = cur_total / cur_calls if cur_calls else 0
    prior_per_call = prior_total / prior_calls if prior_calls else 0
    if prior_per_call <= 0:
        return []
    if cur_per_call < min_cur_tokens_per_call:
        return []
    ratio = cur_per_call / prior_per_call
    if ratio < multiplier:
        return []
    severity = "alert" if ratio >= 2 * multiplier else "warn"
    return [
        {
            "signature": make_signature(
                PRODUCER, "cache_envelope_growth", bot_id
            ),
            "producer": PRODUCER,
            "type": "cache_envelope_growth",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: cache writes {cur_per_call:,.0f} tokens/call "
                f"({ratio:.1f}× prior {prior_window_days}d)"
            ),
            "body": (
                f"{bot_id} is writing {cur_per_call:,.0f} cache tokens per call on "
                f"average over the last {cur_window_days} days, up from "
                f"{prior_per_call:,.0f} over the prior {prior_window_days} days "
                f"({ratio:.1f}×, threshold {multiplier:.1f}×). Cache writes price "
                f"the context envelope — when this grows, something new is being "
                f"injected on every call (usually a workspace or memory file)."
            ),
            "details": {
                "bot_id": bot_id,
                "cur_tokens_per_call": int(cur_per_call),
                "prior_tokens_per_call": int(prior_per_call),
                "cur_window_days": cur_window_days,
                "prior_window_days": prior_window_days,
                "cur_calls": cur_calls,
                "prior_calls": prior_calls,
                "ratio": round(ratio, 3),
                "multiplier_threshold": multiplier,
                "vector": "cost",
                "magnitude": 2 if ratio >= 2 * multiplier else 1,
                "what_it_means": (
                    f"Each call by {bot_id} now writes {cur_per_call:,.0f} cache "
                    f"tokens on average, {ratio:.1f}× the prior baseline of "
                    f"{prior_per_call:,.0f}. Cache writes are the envelope "
                    "OpenClaw injects at session start — workspace bootstrap "
                    "files, memory loads, contextFiles. When this volume "
                    "climbs, the bot is paying the cache-write tax on every "
                    "call regardless of model. On Haiku at $0.375/MTok cache "
                    "writes, 50K extra tokens/call is ~$0.02/call — over a "
                    "month of heartbeats that's tens of dollars from a single "
                    "bloated file."
                ),
                "fix_steps": (
                    f"1. Cross-check `workspace_growth` and `context_bloat` "
                    f"Signals on {bot_id} — both producers of envelope growth\n"
                    f"2. Sort {bot_id}'s workspace + memory by size:\n"
                    f"   ssh pod_admin_user@mini sudo find "
                    f"{_bot_home(bot_id)}/.openclaw/workspace -type f -name '*.md' "
                    f"-printf '%s %p\\n' | sort -rn | head -10\n"
                    "3. The biggest file with recent mtime is almost always "
                    "the culprit — trim or rotate it\n"
                    f"4. If the new envelope is intentional, raise "
                    f"`cost_watchdog.bots.{bot_id}.cache_envelope_multiplier`"
                ),
            },
        }
    ]


def _aggregate_cost_per_call_by_tier(
    events: list[dict],
) -> dict[str, tuple[float, int]]:
    """Return ``{tier: (total_cost_usd, event_count)}`` over the events.

    Tier from ``classify_model_tier`` — "high" / "low" / "unknown". Security_bot's
    cost-per-call slow-creep lived in the "low" bucket (Haiku heartbeats); we
    report all three so the detector caller can decide which to surface.
    """
    acc: dict[str, list[float]] = {"high": [0.0, 0], "low": [0.0, 0], "unknown": [0.0, 0]}
    for e in events:
        tier = classify_model_tier(e.get("model"))
        slot = acc.setdefault(tier, [0.0, 0])
        slot[0] += float(e.get("cost_usd") or 0.0)
        slot[1] += 1
    return {k: (round(v[0], 6), int(v[1])) for k, v in acc.items()}


def detect_efficiency_drift(
    bot_id: str,
    cur_window_events: list[dict],
    prior_window_events: list[dict],
    *,
    multiplier: float,
    cur_window_days: int,
    prior_window_days: int,
    min_cur_calls: int,
    min_prior_calls: int,
    max_per_run: int,
) -> list[dict]:
    """Fire when cost-per-call by (bot, model_tier) is N× its prior baseline.

    The diagnostic axis ``detect_cost_spike`` misses: a bot's total spend can
    stay flat while cost-per-call climbs because the cache envelope is
    growing. Security_bot's 2026-05 incident sat here — Haiku calls drifted to
    ~$0.07/call (Sonnet-shape) while heartbeat cadence stayed constant.

    Per-tier so a model swap doesn't trigger spurious fires on the other
    tier's baseline. "unknown" tier is computed but never emitted — too
    uninterpretable to act on. Quiet when either window has fewer than
    the minimum call counts (small samples → noisy ratios).
    """
    cur = _aggregate_cost_per_call_by_tier(cur_window_events)
    prior = _aggregate_cost_per_call_by_tier(prior_window_events)

    candidates: list[tuple[str, float, float, int, int, float]] = []
    for tier in ("high", "low"):
        cur_cost, cur_calls = cur.get(tier, (0.0, 0))
        prior_cost, prior_calls = prior.get(tier, (0.0, 0))
        if cur_calls < min_cur_calls or prior_calls < min_prior_calls:
            continue
        if prior_cost <= 0:
            continue
        cur_cpc = cur_cost / cur_calls
        prior_cpc = prior_cost / prior_calls
        if prior_cpc <= 0:
            continue
        ratio = cur_cpc / prior_cpc
        if ratio < multiplier:
            continue
        candidates.append((tier, cur_cpc, prior_cpc, cur_calls, prior_calls, ratio))

    candidates.sort(key=lambda c: c[5], reverse=True)
    out: list[dict] = []
    for tier, cur_cpc, prior_cpc, cur_calls, prior_calls, ratio in candidates[:max_per_run]:
        scope_key = f"{bot_id}/{tier}"
        severity = "alert" if ratio >= 2 * multiplier else "warn"
        out.append(
            {
                "signature": make_signature(PRODUCER, "efficiency_drift", scope_key),
                "producer": PRODUCER,
                "type": "efficiency_drift",
                "flavor": "maintenance",
                "severity": severity,
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: {tier}-tier cost-per-call ${cur_cpc:.4f} "
                    f"is {ratio:.1f}× prior {prior_window_days}d (${prior_cpc:.4f})"
                ),
                "body": (
                    f"{bot_id}'s {tier}-tier calls now cost ${cur_cpc:.4f} each on "
                    f"average over the last {cur_window_days} days, up from "
                    f"${prior_cpc:.4f} over the prior {prior_window_days} days "
                    f"({ratio:.1f}×, threshold {multiplier:.1f}×). Call volume is "
                    f"normal — what's growing is the per-call envelope. Usually "
                    f"that's cache writes from a workspace file or memory dir "
                    f"that's been quietly accumulating."
                ),
                "details": {
                    "bot_id": bot_id,
                    "tier": tier,
                    "cur_cost_per_call_usd": round(cur_cpc, 6),
                    "prior_cost_per_call_usd": round(prior_cpc, 6),
                    "cur_window_days": cur_window_days,
                    "prior_window_days": prior_window_days,
                    "cur_calls": cur_calls,
                    "prior_calls": prior_calls,
                    "ratio": round(ratio, 3),
                    "multiplier_threshold": multiplier,
                    "vector": "cost",
                    "magnitude": 2 if ratio >= 2 * multiplier else 1,
                    "what_it_means": (
                        f"Each {tier}-tier call by {bot_id} is now ${cur_cpc:.4f} "
                        f"on average, {ratio:.1f}× the prior baseline of "
                        f"${prior_cpc:.4f}. {tier.capitalize()}-tier calls have a "
                        "natural ceiling — when they cost dramatically more than "
                        "they should, the bot is paying for context envelope size "
                        "(cache writes) on calls that should be near-free. The "
                        "same kind of drift Security_bot's 2026-05 heartbeat blowout "
                        "rode for weeks under the absolute spend cap."
                    ),
                    "fix_steps": (
                        f"1. Cross-check whether `workspace_growth` or "
                        f"`cache_envelope_growth` Signals are firing on "
                        f"{bot_id} — same root cause if so\n"
                        f"2. Inspect the cache-write share:\n"
                        f"   ssh pod_admin_user@mini python3 -m cost_ledger "
                        f"--bot {bot_id} --days {cur_window_days} --by-cache\n"
                        f"3. List the bot's workspace files sorted by size:\n"
                        f"   ssh pod_admin_user@mini sudo find "
                        f"{_bot_home(bot_id)}/.openclaw/workspace -type f -name '*.md' "
                        f"-printf '%s %p\\n' | sort -rn | head -10\n"
                        f"4. If a file is the culprit, trim or rotate it; if a new "
                        f"bootstrap doc was added intentionally, raise this "
                        f"detector's threshold via "
                        f"`cost_watchdog.bots.{bot_id}.efficiency_drift_multiplier`"
                    ),
                },
            }
        )
    return out


def detect_config_drift(
    bot_id: str,
    current_snapshot: dict[str, Any],
    prior: tuple[str, dict[str, Any]] | None,
    *,
    max_per_run: int,
) -> list[dict]:
    """Fire one Signal per dotpath whose value changed since the prior snapshot.

    Each fired Signal:
      * scope: bot, type: ``config_drift`` (one per changed field)
      * details carries (dotpath, friendly_name, prior_value, current_value, prior_snapshot_date)
      * severity: warn by default, alert when the dotpath is on the
        critical-impact list (currently the primary model field — every
        other path through cost can leak to whatever that field holds).

    No-op when no prior snapshot exists (first run for this bot) or
    when nothing changed. The current_snapshot is the caller's
    responsibility; the snapshot writer is invoked separately by
    ``collect_for_bot`` so the writer side has a stable contract
    (today's snapshot is always written; the detector consumes it).
    """
    if prior is None:
        return []
    prior_date, prior_snapshot = prior

    # Per-dotpath severity. Severity is the actionability dial:
    #   alert — silent change here is materially user-affecting (Security_bot
    #           2026-05-28: primary-model reversion silently re-routed every
    #           request to Sonnet rates; exec-security relax exposes the bot
    #           to commands the operator opted out of).
    #   warn  — change is cost- or behavior-relevant (heartbeat runs many
    #           times per day; cadence change shifts call volume).
    #   info  — change is rarely actionable on its own (fallback list
    #           reordering is dormant until the primary fails).
    severity_by_dotpath: dict[str, str] = {
        "agents.defaults.model.primary": "alert",
        "tools.exec.security": "alert",
        "agents.defaults.heartbeat.model": "warn",
        "agents.defaults.heartbeat.every": "warn",
        "agents.defaults.model.fallbacks": "info",
    }

    out: list[dict] = []
    for dotpath, friendly in _CONFIG_DRIFT_DOTPATHS:
        prior_v = prior_snapshot.get(dotpath)
        cur_v = current_snapshot.get(dotpath)
        if prior_v == cur_v:
            continue
        severity = severity_by_dotpath.get(dotpath, "warn")
        scope_key = f"{bot_id}/{dotpath}"
        # Format prior/current values for the body so the operator sees
        # the actual change without clicking through to the snapshot.
        # Title keeps a fixed short form (model fallback lists in particular
        # blow past 300 chars when JSON-dumped inline) — the diff lives in
        # body + details only. See signals.store.TITLE_SOFT_LIMIT.
        prior_str = json.dumps(prior_v, default=str)
        cur_str = json.dumps(cur_v, default=str)
        out.append(
            {
                "signature": make_signature(PRODUCER, "config_drift", scope_key),
                "producer": PRODUCER,
                "type": "config_drift",
                "flavor": "maintenance",
                "severity": severity,
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: {friendly} changed (since {prior_date})"
                ),
                "body": (
                    f"{bot_id}'s `{dotpath}` ({friendly}) changed from "
                    f"`{prior_str}` to `{cur_str}` between {prior_date} "
                    f"and today. This field is on the cost_watchdog "
                    f"config-drift watchlist because silent changes to "
                    f"it materially affect cost, safety, or behavior. "
                    f"If you intended this change, dismiss the Signal — "
                    f"the next sweep will see it as the new baseline. "
                    f"If you didn't, check the apply log + recent admin "
                    f"actions for what made the edit."
                ),
                "details": {
                    "bot_id": bot_id,
                    "dotpath": dotpath,
                    "friendly_name": friendly,
                    "prior_value": prior_v,
                    "current_value": cur_v,
                    "prior_snapshot_date": prior_date,
                    "vector": "config",
                    "magnitude": 2 if severity == "alert" else 1,
                    "what_it_means": (
                        f"`{dotpath}` is a load-bearing config field — "
                        "silent changes to it can materially shift cost "
                        "or safety posture without any other detector "
                        "noticing. The Security_bot 2026-05-28 incident was the "
                        "canonical case: primary model reverted from "
                        "haiku to sonnet outside any audited path, and "
                        "every cost-leak shape escalated to Sonnet "
                        "rates until spotted by spend total alone."
                    ),
                    "fix_steps": (
                        f"1. Decide: was this change intentional? If yes, "
                        f"dismiss the Signal — it'll auto-archive once "
                        "the snapshot stabilizes\n"
                        f"2. If no, inspect recent admin actions:\n"
                        f"   ssh pod_admin_user@mini sudo grep '{bot_id}' "
                        f"{_shared_dir_str()}/logs/admin-actions.jsonl "
                        f"| tail -10\n"
                        f"3. Check apply log for the bot:\n"
                        f"   ssh pod_admin_user@mini sudo grep -i "
                        f"'{dotpath.split('.')[-1]}' "
                        f"{_bot_home(bot_id)}/.openclaw/logs/evolve-apply.log\n"
                        f"4. Restore the prior value via the admin UI's "
                        f"config editor, or via:\n"
                        f"   sudo evolve-admin set {bot_id} "
                        f"'{dotpath}' <prior-value>"
                    ),
                },
            }
        )
        if len(out) >= max_per_run:
            break
    return out


def detect_workspace_growth_rate(
    bot_id: str,
    sizes_today: dict[str, int],
    snapshots_history: list[tuple[str, dict[str, int]]],
    *,
    growth_kb_per_day_threshold: float,
    min_window_days: int,
    min_current_kb: float,
    max_per_run: int,
) -> list[dict]:
    """Flag workspace files whose size is climbing faster than threshold KB/day.

    Catches the slow-creep mode that ``detect_context_bloat`` (current-size
    threshold) misses: a file growing 20 KB/day for weeks crosses the bloat
    floor late, but the trajectory is operator-actionable from week 1.

    The snapshot at index 0 of ``snapshots_history`` is the oldest available
    sample; we compare it to ``sizes_today`` and project growth/day. Files
    that only appear in ``sizes_today`` (no historical record) are skipped —
    no baseline to compare against. Files below ``min_current_kb`` are
    skipped to suppress noise on tiny rotating files.

    Returns ``[]`` if there aren't at least ``min_window_days`` of history
    (need a real trend, not a one-day blip).
    """
    if not snapshots_history:
        return []
    oldest_date, oldest_sizes = snapshots_history[0]
    try:
        oldest_d = datetime.strptime(oldest_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    # Find the most recent snapshot date as the "now" anchor — handles the
    # case where collect_for_bot snapshot wasn't written yet on first run.
    newest_date = snapshots_history[-1][0]
    try:
        newest_d = datetime.strptime(newest_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    window_days = (newest_d - oldest_d).days
    if window_days < min_window_days:
        return []

    out: list[dict] = []
    candidates: list[tuple[str, float, int, int]] = []
    for name, cur_bytes in sizes_today.items():
        cur_bytes = int(cur_bytes or 0)
        if cur_bytes < int(min_current_kb * 1024):
            continue
        old_bytes = int(oldest_sizes.get(name, 0) or 0)
        if old_bytes <= 0:
            # File didn't exist at the start of the window — could be a
            # new file or a rotation. Either way, no trend yet.
            continue
        delta_bytes = cur_bytes - old_bytes
        if delta_bytes <= 0:
            continue
        growth_kb_per_day = (delta_bytes / 1024) / window_days
        if growth_kb_per_day < growth_kb_per_day_threshold:
            continue
        candidates.append((name, growth_kb_per_day, cur_bytes, old_bytes))

    # Sort by growth rate descending so the worst offenders surface first
    candidates.sort(key=lambda c: c[1], reverse=True)
    for name, rate_kb_day, cur_bytes, old_bytes in candidates[:max_per_run]:
        cur_kb = cur_bytes / 1024
        old_kb = old_bytes / 1024
        scope_key = f"{bot_id}/{name}"
        # Severity escalates on doubling pace (file is set to outgrow itself
        # before the next window closes).
        severity = "alert" if rate_kb_day >= 2 * growth_kb_per_day_threshold else "warn"
        out.append(
            {
                "signature": make_signature(PRODUCER, "workspace_growth", scope_key),
                "producer": PRODUCER,
                "type": "workspace_growth",
                "flavor": "maintenance",
                "severity": severity,
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: {name} growing {rate_kb_day:.1f} KB/day "
                    f"({old_kb:.0f}→{cur_kb:.0f} KB over {window_days}d)"
                ),
                "body": (
                    f"{name} in {bot_id}'s workspace grew from {old_kb:.0f} KB to "
                    f"{cur_kb:.0f} KB over {window_days} days "
                    f"({rate_kb_day:.1f} KB/day). Catches drift before the absolute "
                    f"context_bloat threshold ({DEFAULTS['context_bloat_kb']} KB) is "
                    f"crossed — by the time it crosses, the operator has already "
                    f"been paying the cache-write tax for weeks."
                ),
                "details": {
                    "bot_id": bot_id,
                    "filename": name,
                    "current_kb": round(cur_kb, 1),
                    "previous_kb": round(old_kb, 1),
                    "window_days": window_days,
                    "growth_kb_per_day": round(rate_kb_day, 2),
                    "threshold_kb_per_day": growth_kb_per_day_threshold,
                    "vector": "cost",
                    "magnitude": 2 if rate_kb_day >= 2 * growth_kb_per_day_threshold else 1,
                    "what_it_means": (
                        f"`{name}` in {bot_id}'s workspace is growing at "
                        f"{rate_kb_day:.1f} KB/day. Files in this bot's "
                        "workspace are injected as bootstrap context on each "
                        "session — every kilobyte that lands here is paid for "
                        "on every Haiku heartbeat as cache-write tokens. The "
                        "absolute size hasn't crossed the bloat threshold "
                        "yet, but the trajectory will if the growth continues."
                    ),
                    "fix_steps": (
                        f"1. Inspect what's being appended:\n"
                        f"   ssh pod_admin_user@mini sudo /bin/cat "
                        f"{_bot_home(bot_id)}/.openclaw/workspace/{name} | tail -100\n"
                        "2. Identify the producer — usually a heartbeat or "
                        "cron writing summaries / audit output here\n"
                        "3. Fix at the source: switch the writer to "
                        "summary-only output, or rotate to dated files that "
                        "stop being referenced after N days\n"
                        f"4. If the growth is intentional, set "
                        f"`cost_watchdog.bots.{bot_id}."
                        f"workspace_growth_files.{name}` to a higher KB/day "
                        "value in network.json"
                    ),
                },
            }
        )
    return out


def detect_session_token_outlier(
    bot_id: str,
    window_events: list[dict],
    *,
    factor: float,
    min_session_events: int,
    min_cost_usd: float,
    max_per_run: int,
) -> list[dict]:
    """Flag individual sessions that cost N× the bot's median session cost.

    Catches the failure mode Security_bot exposed: a single heartbeat that hits a
    stuck loop or runs a runaway subagent — 119-message session vs 25–35
    normal — which can hide inside a daily total that's only modestly high.

    Median is computed across sessions with >= ``min_session_events`` events
    so trivially-small sessions don't anchor the baseline. Sessions below
    ``min_cost_usd`` never fire even if the multiplier is high (a $0.10
    session at 5× a $0.02 median isn't worth alerting on).
    """
    by_session = rollup_per_session(window_events)
    eligible = [
        (sid, slot)
        for sid, slot in by_session.items()
        if int(slot.get("event_count") or 0) >= min_session_events
        and sid  # skip empty session_id
    ]
    if len(eligible) < 3:
        # Need a few sessions for median to mean anything.
        return []
    costs = sorted(float(slot.get("cost_usd") or 0.0) for _, slot in eligible)
    n = len(costs)
    median = costs[n // 2] if n % 2 else (costs[n // 2 - 1] + costs[n // 2]) / 2
    if median <= 0:
        return []
    threshold = max(median * factor, min_cost_usd)
    outliers: list[tuple[str, dict, float]] = []
    for sid, slot in eligible:
        cost = float(slot.get("cost_usd") or 0.0)
        if cost < threshold:
            continue
        outliers.append((sid, slot, cost))
    outliers.sort(key=lambda t: -t[2])
    outliers = outliers[:max_per_run]
    out: list[dict] = []
    for sid, slot, cost in outliers:
        ratio = cost / median if median > 0 else 0.0
        kinds = list(slot.get("trigger_kinds") or [])
        kinds_str = "+".join(kinds) if kinds else "unknown"
        scope_key = f"{bot_id}/{sid}"
        out.append(
            {
                "signature": make_signature(
                    PRODUCER, "session_token_outlier", scope_key
                ),
                "producer": PRODUCER,
                "type": "session_token_outlier",
                "flavor": "maintenance",
                "severity": "warn",
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: session {sid[:8]} cost ${cost:.2f} "
                    f"({ratio:.1f}× median ${median:.2f})"
                ),
                "body": (
                    f"Session `{sid}` cost ${cost:.2f} on {kinds_str}, "
                    f"{ratio:.1f}× the bot's median session cost of "
                    f"${median:.2f} over the lookback window. Worth "
                    f"inspecting — common causes: a stuck loop, a runaway "
                    f"subagent, retry storms on a failing tool, or a "
                    f"heartbeat that did far more work than intended."
                ),
                "details": {
                    "bot_id": bot_id,
                    "session_id": sid,
                    "cost_usd": round(cost, 4),
                    "median_session_cost_usd": round(median, 4),
                    "ratio": round(ratio, 2),
                    "event_count": int(slot.get("event_count") or 0),
                    "trigger_kinds": kinds,
                    "first_ts": slot.get("first_ts"),
                    "last_ts": slot.get("last_ts"),
                    # v2 enrichment from the session's first-turn record.
                    # See rollup_per_session for the hold-strong policy on
                    # which event anchors these fields. For internal
                    # sessions (heartbeat / cron) all fields stay None.
                    **_v2_context_from_session_slot(slot),
                    # Severity framework: magnitude follows the absolute
                    # $ for the outlier session (same anchors as
                    # daily_spend_high). A $0.50 outlier is still mag 0
                    # even if 5× median.
                    "vector": "cost",
                    "magnitude": _cost_magnitude_for_usd(cost),
                    "what_it_means": (
                        f"One session on {bot_id} cost ${cost:.2f} — "
                        f"{ratio:.1f}× the bot's recent median session "
                        f"cost of ${median:.2f}. A single outlier "
                        "session can hide inside a daily total that "
                        "looks only modestly high; the typical cause "
                        "is a stuck loop, runaway subagent, retry "
                        "storm on a failing tool, or a heartbeat that "
                        "did far more work than intended."
                    ),
                    "fix_steps": (
                        f"1. Open the session in Usage → Bot detail "
                        f"for `{bot_id}` — filter by session_id "
                        f"`{sid[:8]}`\n"
                        "2. Scan the turn-by-turn view for repeat "
                        "patterns (same tool called repeatedly, same "
                        "model fallback, infinite-loop content)\n"
                        f"3. Trigger kinds: {kinds_str} — if it's a "
                        "heartbeat or cron, follow up on that "
                        "scheduler in `openclaw.json` / `cron/jobs.json`\n"
                        "4. Inspect the cost_event JSONL for the "
                        f"session:\n   ssh pod_admin_user@mini sudo grep "
                        f"'\"session_id\":\"{sid}\"' "
                        f"{_shared_dir_str()}/{bot_id}/cost_events/"
                        "*.jsonl | head -50"
                    ),
                },
            }
        )
    return out


def detect_heartbeat_no_model_override(
    bot_id: str,
    openclaw_json: dict | None,
) -> list[dict]:
    """Retired 2026-06-04. The Evolve plugin's ModelRouter
    (TurnObserver.resolveModelRouting → ModelRouter.resolveModelOverride)
    pre-classifies heartbeat sessions as ``background`` via the
    ``before_model_resolve`` hook and routes to ``tier3.models[0]`` from
    the bot's ``evolve-tiers.json`` — *before* OC ever consults
    ``agents.defaults.heartbeat.model``. Setting a literal there is dead
    config for the heartbeat path; this detector's "fix" advice was
    therefore misleading. The right knob is the bot's tier3.models[0]
    in evolve-tiers.json; if tier3 is unconfigured, ModelRouter logs a
    startup WARN on its own (ModelRouter.ts §_warnIfTier3Empty).

    Kept as a no-op stub so call sites and tests don't break; safe to
    delete entirely in a follow-up cleanup. See incident 2026-06-04
    (security-bot 2026-06-04 cost-cap trip) for the diagnostic that surfaced this.
    """
    del bot_id, openclaw_json  # unused — see retirement note above
    return []


# ── Heartbeat workload / cost-by-design helpers ──────────────────────────────


def _read_heartbeat_md(bot_id: str, config: dict[str, Any] | None) -> str | None:
    """Return the bot's workspace HEARTBEAT.md, or None if absent/unreadable."""
    path = (
        _bot_home(bot_id, config) / ".openclaw" / "workspace" / "HEARTBEAT.md"
    )
    return _read_with_sudo_fallback(path)


def heartbeat_workload_chars(content: str | None) -> int:
    """Count non-comment, non-blank characters in a HEARTBEAT.md.

    OC's heartbeat path skips the API call when HEARTBEAT.md is empty or
    comment-only (lines starting with ``#`` plus blank lines). Anything past
    that ceiling is *executable* workload that drives per-heartbeat cost.

    Intentionally conservative: treats `#` only at the line start as comment
    (so inline `#` inside backtick-quoted commands counts as workload), and
    counts characters not lines so a single fat instruction line isn't
    masked by a small line count.
    """
    if not content:
        return 0
    total = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        total += len(stripped)
    return total


def _parse_every_to_hours(every: Any) -> float | None:
    """Convert OC's heartbeat.every shape (``"30m"``, ``"2h"``, ``"1d"``) to hours.

    Returns None on missing / unparseable input — the caller treats None as
    "cadence unknown, skip the projection." Numeric input is interpreted as
    seconds (OC's other knob shape) and converted; bare ``"3600"`` → 1h.
    """
    if every is None:
        return None
    if isinstance(every, (int, float)):
        secs = float(every)
        return secs / 3600.0 if secs > 0 else None
    if not isinstance(every, str):
        return None
    s = every.strip().lower()
    if not s:
        return None
    # Strip trailing unit; default unit is seconds when bare.
    if s.endswith("ms"):
        try:
            return float(s[:-2]) / 3_600_000.0
        except ValueError:
            return None
    unit = s[-1] if s and s[-1].isalpha() else ""
    num_part = s[:-1] if unit else s
    try:
        n = float(num_part)
    except ValueError:
        return None
    if n <= 0:
        return None
    if unit == "h":
        return n
    if unit == "m":
        return n / 60.0
    if unit == "s" or unit == "":
        return n / 3600.0
    if unit == "d":
        return n * 24.0
    return None


def _median_heartbeat_cost_usd(events: list[dict]) -> tuple[float, int]:
    """Median per-session heartbeat cost and the sample size used.

    Aggregates events by session_id, sums cost per session, returns
    (median_cost_per_session, session_count). Sessions whose first event has
    no heartbeat marker (channel/source) are skipped. Returns (0.0, 0) when
    no qualifying sessions exist.
    """
    by_session: dict[str, dict[str, Any]] = {}
    for e in events:
        sid = e.get("session_id") or ""
        if not sid:
            continue
        bucket = by_session.setdefault(sid, {"cost": 0.0, "is_hb": False})
        ch = (e.get("channel") or "").strip().lower()
        src = (e.get("source") or "").strip().lower()
        if ch == "heartbeat" or src == "heartbeat":
            bucket["is_hb"] = True
        bucket["cost"] += float(e.get("cost_usd") or e.get("cost") or 0.0)
    hb_costs = sorted(b["cost"] for b in by_session.values() if b["is_hb"])
    if not hb_costs:
        return (0.0, 0)
    mid = len(hb_costs) // 2
    if len(hb_costs) % 2 == 1:
        median = hb_costs[mid]
    else:
        median = (hb_costs[mid - 1] + hb_costs[mid]) / 2.0
    return (median, len(hb_costs))


def _resolve_per_bot_cap(
    bot_id: str,
    config: dict[str, Any] | None,
    shared_dir: Path | None = None,
) -> float | None:
    """Per-bot daily hard cap from ``better-engine-config.json``.

    Resolution (mirrors ``spend_alert._resolve_per_bot_cap``):
      1. ``better-engine-config.json::bots.<bot>.budget.per_bot_daily_hard_usd``
         (canonical since the 2026 cost-cap normalization)
      2. ``network.json::dailySpendCapUsd`` (pod-wide cap; retained as the
         global fallback when no per-bot value is set)

    Returns None when unset or non-positive (zero / negative means "no cap").
    The per-bot ``network.json::bots.<bot>.daily_cap_usd`` path was removed
    in Phase 4 of the cost-cap normalization (2026-06).
    """
    # 1. Canonical BE config path.
    if shared_dir is not None:
        try:
            from better_engine_config import load as _load_be  # type: ignore
            be_config = _load_be(shared_dir)
            be_val = be_config.bots.get(bot_id, {}) \
                .get("budget", {}) \
                .get("per_bot_daily_hard_usd")
            if be_val is not None:
                try:
                    cap = float(be_val)
                except (TypeError, ValueError):
                    cap = 0.0
                if cap > 0:
                    return cap
        except Exception:
            # Import or read failure must not silently disable cap-driven
            # cost detectors; fall through to the global cap below.
            pass

    # 2. Pod-wide legacy global cap (network.json::dailySpendCapUsd).
    raw = (config or {}).get("dailySpendCapUsd")
    if raw is None:
        return None
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def detect_llm_workload_redundant_with_script(
    bot_id: str,
    heartbeat_md_content: str | None,
    openclaw_json: dict | None,
    today_events: list[dict],
    *,
    min_workload_chars: int,
    min_projected_daily_cost_usd: float,
    min_sessions_for_projection: int,
) -> list[dict]:
    """Fire when a bot's HEARTBEAT.md drives substantial LLM cost AND scripted coverage exists.

    Today's pattern (security-bot, 2026-06-04): the bot has a multi-KB
    HEARTBEAT.md telling Haiku to run audit checks every heartbeat, while
    ``packages/analyzer/audit.py`` + ``monitor_coverage.py`` already cover
    those checks in pure Python. The LLM heartbeat is duplicate work — every
    cycle bills $0.10+ to re-discover state the scripted path already
    surfaces as Signals for free.

    The detector intentionally does NOT assert that any specific check is
    covered — it raises the question and points the operator at the scripted
    coverage to compare. If the LLM workload IS intentional (the operator
    wants Haiku to make judgment calls the scripts can't), they can ack via
    policy acceptance.

    Heuristic:
      1. HEARTBEAT.md exists and has > ``min_workload_chars`` of non-comment,
         non-blank content (the off-switch is "make it comment-only").
      2. Projected daily cost — (24/cadence_hours) × median_per_hb_cost —
         exceeds ``min_projected_daily_cost_usd``.
      3. At least ``min_sessions_for_projection`` heartbeat sessions today
         (otherwise the median is too noisy to act on).

    Skips silently when any precondition is unmet: a tiny HEARTBEAT.md
    (= disabled) or a quiet day with no data isn't a signal.
    """
    workload_chars = heartbeat_workload_chars(heartbeat_md_content)
    if workload_chars < min_workload_chars:
        return []
    defaults = (openclaw_json or {}).get("agents", {}).get("defaults", {})
    every = defaults.get("heartbeat", {}).get("every")
    cadence_hours = _parse_every_to_hours(every)
    if cadence_hours is None or cadence_hours <= 0:
        # Cadence unknown — fall back to OC's 30-min default so the
        # projection still produces a number for visibility.
        cadence_hours = 0.5
    median_cost, sample = _median_heartbeat_cost_usd(today_events)
    if sample < min_sessions_for_projection or median_cost <= 0:
        return []
    expected_daily = (24.0 / cadence_hours) * median_cost
    if expected_daily < min_projected_daily_cost_usd:
        return []
    workload_kb = workload_chars / 1024.0
    return [
        {
            "signature": make_signature(
                PRODUCER, "llm_workload_redundant_with_script", bot_id
            ),
            "producer": PRODUCER,
            "type": "llm_workload_redundant_with_script",
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: HEARTBEAT.md drives ~${expected_daily:.2f}/day"
                f" — likely duplicates scripted coverage"
            ),
            "body": (
                f"{bot_id}'s `~/.openclaw/workspace/HEARTBEAT.md` carries "
                f"{workload_kb:.1f}KB of non-comment instructions; today's "
                f"{sample} heartbeat session(s) averaged ${median_cost:.3f} "
                f"each at a {every or '30m (OC default)'} cadence, projecting "
                f"to ${expected_daily:.2f}/day. Evolve's pure-Python audit "
                f"coverage (audit.py + monitor_coverage + cost_watchdog + "
                f"spend_alert + backup_signal) was designed to subsume LLM "
                f"audit heartbeats — running both is duplicate billing. "
                f"To disable the LLM path: replace HEARTBEAT.md content with "
                f"comments (`# Covered by audit.py`); OC's heartbeat skips "
                f"the API call when the file is comment-only. If the LLM "
                f"workload is intended, ack via "
                f"`cost_watchdog.policy_acceptance` for "
                f"`llm_workload_redundant_with_script`."
            ),
            "details": {
                "bot_id": bot_id,
                "heartbeat_md_workload_chars": workload_chars,
                "heartbeat_md_workload_kb": round(workload_kb, 2),
                "heartbeat_every": every,
                "cadence_hours": cadence_hours,
                "median_session_cost_usd": round(median_cost, 4),
                "today_session_sample": sample,
                "projected_daily_cost_usd": round(expected_daily, 2),
                "vector": "cost",
                "magnitude": 2,
                "what_it_means": (
                    f"Every heartbeat hands {workload_kb:.1f}KB of instructions "
                    "to the LLM to execute. At the observed cadence and "
                    f"per-session cost, that compounds to ~${expected_daily:.2f}/day. "
                    "The same audit/security/cost checks run in pure Python "
                    "via audit.py and friends and surface as Signals already."
                ),
                "fix_steps": (
                    f"1. SSH to the mini:\n"
                    f"   ssh pod-admin-user@mini\n"
                    f"2. Verify the scripted coverage already fires the "
                    f"Signals you depend on (search Signals page for {bot_id}).\n"
                    f"3. Replace the workload file with a comment-only stub:\n"
                    f"   sudo /bin/cp {_bot_home(bot_id)}/.openclaw/workspace/HEARTBEAT.md "
                    f"{_bot_home(bot_id)}/.openclaw/workspace/HEARTBEAT.md.bak\n"
                    f"   sudo /usr/bin/tee {_bot_home(bot_id)}/.openclaw/workspace/HEARTBEAT.md "
                    f"<<<'# Coverage handed off to audit.py + monitor_coverage'\n"
                    f"4. Next heartbeat cycle (within `{every or '30m'}`) skips "
                    f"the API call.\n"
                    f"5. Commit the change to workspace git so the audit_identity "
                    f"baseline updates."
                ),
            },
        }
    ]


def detect_heartbeat_cadence_anomaly(
    bot_id: str,
    openclaw_json: dict | None,
    today_events: list[dict],
    *,
    factor: float,
    alert_factor: float,
    min_extra_fires: int,
    now: datetime | None = None,
) -> list[dict]:
    """Fire when actual heartbeat fires/24h exceed declared cadence × ``factor``.

    Mirrors ``detect_cron_overactive`` for the heartbeat path. The 2026-06-04
    security-bot 2026-06-04 incident had this exact shape: configured cadence intent of "2h"
    (per HEARTBEAT.md header), no ``heartbeat.every`` field so OC fell through
    to 30-min default, plus a repo-puller redeploy storm firing extra
    heartbeats on every gateway kickstart. Actual count was 43 against an
    intended 12 — a 3.6× cadence anomaly that none of the existing detectors
    saw because cost-per-call didn't drift and turn-per-session was 1.

    Severity tiers:
      - warn when projected_24h / expected ≥ ``factor`` (default 1.5×)
      - alert when ratio ≥ ``alert_factor`` (default 3× — likely a restart
        loop, manual kickstart cascade, or stuck deploy job)

    Silent when the absolute extra count is below ``min_extra_fires`` (a
    2-vs-3 anomaly on a sleepy bot doesn't earn a Signal even at 1.5×).
    Time-of-day is normalized: actuals are projected to a full 24h by
    elapsed-fraction so an early-morning tick doesn't fire prematurely.
    """
    if not openclaw_json:
        return []
    defaults = (openclaw_json.get("agents") or {}).get("defaults") or {}
    heartbeat = defaults.get("heartbeat") or {}
    if not isinstance(heartbeat, dict):
        return []
    every = heartbeat.get("every")
    # Use OC's documented 30-min default when `every` is absent so a missing
    # cadence still gets evaluated (that's exactly the case we want to catch).
    cadence_hours = _parse_every_to_hours(every) or 0.5
    if cadence_hours <= 0:
        return []
    expected_fires_24h = 24.0 / cadence_hours

    by_session: dict[str, bool] = {}
    for e in today_events:
        sid = e.get("session_id") or ""
        if not sid:
            continue
        ch = (e.get("channel") or "").strip().lower()
        src = (e.get("source") or "").strip().lower()
        if ch == "heartbeat" or src == "heartbeat":
            by_session[sid] = True
    actual_fires = len(by_session)
    if actual_fires == 0:
        return []

    now_dt = now or datetime.now(timezone.utc)
    seconds_elapsed = (
        now_dt - now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    ).total_seconds()
    # Floor at 1h elapsed so a 7am tick doesn't get extrapolated 24× off a
    # single fire. The detector is meant for established trends, not boots.
    seconds_elapsed = max(seconds_elapsed, 3600.0)
    projected_24h = actual_fires * (86400.0 / seconds_elapsed)
    extra_fires = projected_24h - expected_fires_24h
    if extra_fires < min_extra_fires:
        return []
    ratio = projected_24h / expected_fires_24h
    if ratio < factor:
        return []
    severity = "alert" if ratio >= alert_factor else "warn"
    every_human = every or "30m (OC default)"
    return [
        {
            "signature": make_signature(
                PRODUCER, "heartbeat_cadence_anomaly", bot_id
            ),
            "producer": PRODUCER,
            "type": "heartbeat_cadence_anomaly",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: heartbeats firing {ratio:.1f}× declared "
                f"cadence ({actual_fires} so far today vs ~"
                f"{expected_fires_24h:.0f} budgeted)"
            ),
            "body": (
                f"{bot_id}'s heartbeat declared cadence is {every_human} "
                f"(~{expected_fires_24h:.0f} fires/24h), but {actual_fires} "
                f"have fired so far today, projecting to "
                f"{projected_24h:.0f} over the full day "
                f"({ratio:.1f}× declared). Common causes: gateway restart "
                f"storm (e.g. repo-puller redeploys triggering "
                f"kickstart-fires-immediate-heartbeat), a stuck deploy job "
                f"re-triggering kickstart, or a manual kickstart loop. "
                f"Check evolve-apply.log / repo-puller.log for the restart "
                f"cadence. Each extra fire bills at `tier3.models[0]` so "
                f"cost adds up fast on bots with large context envelopes."
            ),
            "details": {
                "bot_id": bot_id,
                "heartbeat_every": every,
                "cadence_hours": cadence_hours,
                "expected_fires_per_day": round(expected_fires_24h, 1),
                "actual_fires_so_far": actual_fires,
                "projected_fires_24h": round(projected_24h, 1),
                "ratio_vs_expected": round(ratio, 2),
                "seconds_elapsed_today": int(seconds_elapsed),
                "vector": "cost",
                "magnitude": 3 if severity == "alert" else 2,
                "what_it_means": (
                    f"At declared {every_human}, {bot_id} should see ~"
                    f"{expected_fires_24h:.0f} heartbeats/day. It's on "
                    f"track for {projected_24h:.0f} — something is firing "
                    f"heartbeats out-of-cadence."
                ),
                "fix_steps": (
                    "1. Identify the restart source — check the gateway "
                    "kickstart log:\n"
                    "   ssh pod-admin-user@mini sudo /bin/cat "
                    f"{_bot_home(bot_id)}/.openclaw/logs/openclaw.log | grep "
                    "'heartbeat: started\\|SIGTERM' | tail -50\n"
                    "2. If repo-puller-driven: check "
                    f"{_shared_dir_str()}/logs/repo-puller.log for "
                    "back-to-back redeploys; coalesce them at the puller "
                    "level.\n"
                    "3. If deploy-job-driven: check the launchd plist for "
                    "a wedged StartCalendarInterval that's firing more "
                    "often than intended.\n"
                    "4. If intentional (operator wants more frequent "
                    f"heartbeats): edit `agents.defaults.heartbeat.every` "
                    "to match the actual cadence so the budget aligns."
                ),
            },
        }
    ]


def detect_heartbeat_cost_by_design(
    bot_id: str,
    openclaw_json: dict | None,
    today_events: list[dict],
    daily_cap_usd: float | None,
    *,
    warn_fraction: float,
    alert_fraction: float,
    min_sessions_for_projection: int,
) -> list[dict]:
    """Fire when the bot's *design* (cadence × per-hb cost) is on track to use
    a large fraction of its daily cap — independent of whether HEARTBEAT.md
    is the cause.

    Distinct from ``daily_spend_high`` and the L1 cost breaker: those fire
    on *actuals* (spend crossed N today). This one fires on *projection*
    (at this rate, spend WILL cross N by midnight). The point is to give
    the operator a same-day knob to turn before the breaker trips, rather
    than after.

    Severity tiers:
      - warn  when expected ≥ ``warn_fraction`` × cap (default 50%)
      - alert when expected ≥ ``alert_fraction`` × cap (default 100% — trip
        is essentially inevitable absent a configuration change)

    No fire when ``daily_cap_usd`` is unset (operator opted out of caps)
    or when not enough heartbeat sessions have accrued today.
    """
    if not daily_cap_usd or daily_cap_usd <= 0:
        return []
    defaults = (openclaw_json or {}).get("agents", {}).get("defaults", {})
    every = defaults.get("heartbeat", {}).get("every")
    cadence_hours = _parse_every_to_hours(every) or 0.5
    median_cost, sample = _median_heartbeat_cost_usd(today_events)
    if sample < min_sessions_for_projection or median_cost <= 0:
        return []
    expected_daily = (24.0 / cadence_hours) * median_cost
    fraction = expected_daily / daily_cap_usd
    if fraction < warn_fraction:
        return []
    severity = "alert" if fraction >= alert_fraction else "warn"
    return [
        {
            "signature": make_signature(
                PRODUCER, "heartbeat_cost_by_design", bot_id
            ),
            "producer": PRODUCER,
            "type": "heartbeat_cost_by_design",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: heartbeat design projects "
                f"${expected_daily:.2f}/day ({fraction:.0%} of "
                f"${daily_cap_usd:.2f} cap)"
            ),
            "body": (
                f"At a {every or '30m (OC default)'} cadence with a median "
                f"per-session cost of ${median_cost:.3f} "
                f"(over {sample} session(s) today), {bot_id} is on track "
                f"to spend ${expected_daily:.2f}/day — {fraction:.0%} of "
                f"its ${daily_cap_usd:.2f} daily cap. This is a budget-by-"
                f"design problem, not a runaway: the bot will keep hitting "
                f"this number every day until cadence or per-session cost "
                f"changes. Options: (a) raise the cadence "
                f"(`agents.defaults.heartbeat.every`), (b) reduce per-"
                f"session workload (trim HEARTBEAT.md / context envelope), "
                f"(c) raise `bots.{bot_id}.daily_cap_usd` in network.json "
                f"if the design is intentional."
            ),
            "details": {
                "bot_id": bot_id,
                "heartbeat_every": every,
                "cadence_hours": cadence_hours,
                "median_session_cost_usd": round(median_cost, 4),
                "today_session_sample": sample,
                "projected_daily_cost_usd": round(expected_daily, 2),
                "daily_cap_usd": daily_cap_usd,
                "cap_fraction": round(fraction, 3),
                "vector": "cost",
                "magnitude": 3 if severity == "alert" else 2,
                "what_it_means": (
                    f"Even with no anomalies, the bot's heartbeat cadence "
                    f"and per-session cost multiply to ${expected_daily:.2f}/day. "
                    f"That's {fraction:.0%} of the configured cap, so any "
                    f"normal day will land near or over it."
                ),
                "fix_steps": (
                    f"1. Decide: is the cadence too tight, the per-session "
                    f"workload too heavy, or the cap too low?\n"
                    f"2. To slow cadence: edit "
                    f"`agents.defaults.heartbeat.every` in "
                    f"`{_bot_home(bot_id)}/.openclaw/openclaw.json` (e.g. "
                    f"`\"every\": \"4h\"`).\n"
                    f"3. To lighten workload: trim HEARTBEAT.md and any "
                    f"large workspace MD files the heartbeat reads.\n"
                    f"4. To raise the cap: edit "
                    f"`bots.{bot_id}.daily_cap_usd` in network.json.\n"
                    f"5. Apply: `sudo evolve-admin deploy {bot_id}`."
                ),
            },
        }
    ]


def _model_ref_eq(provider: str, model: str, override: str) -> bool:
    """True when (provider, model) matches the configured ``provider/model`` override.

    Override accepts either bare ``provider/model`` or just ``model``. cost_event
    records carry provider + bare model separately; this normalizes for compare.
    """
    raw = (override or "").strip().lower()
    if not raw:
        return False
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    # Defensive: some upstream paths can leave provider empty and pack
    # "provider/model" into the model field. Split before comparing so a
    # spurious mismatch can't fire on a config that's actually honored.
    if not p and "/" in m:
        p, m = m.split("/", 1)
    if "/" in raw:
        return raw == f"{p}/{m}"
    return raw == m


def _is_heartbeat_session(turns: list[dict]) -> bool:
    """True iff this session looks like a heartbeat session.

    Two surfaces — either is sufficient:
      - ``channel == "heartbeat"`` on any turn (the gateway-level marker
        OC writes for heartbeat-triggered prompts)
      - ``source`` ∈ {"heartbeat", "Heartbeat"} on any turn (the
        upstream-trigger marker, normalized by load_turns)

    Both are checked because some legacy records carry only one. A
    session that *starts* as a heartbeat retains the marker on every
    follow-up turn that the gateway dispatches — so any-match is the
    right semantic (versus first-turn-only, which would miss sessions
    that bleed across follow-ups under a different source label).
    """
    for t in turns:
        ch = (t.get("channel") or "").strip().lower()
        src = (t.get("source") or "").strip().lower()
        if ch == "heartbeat" or src == "heartbeat":
            return True
    return False


def _heartbeat_bloat_severity(
    turn_count: int,
    *,
    warn_turns: int,
    alert_turns: int,
    critical_turns: int,
) -> tuple[str, int, str] | None:
    """Map a heartbeat session's turn count to (signal_severity, magnitude, tier).

    Returns None when ``turn_count`` is at or below ``warn_turns`` (no
    fire). Otherwise returns:
      - signal_severity: "warn" | "alert" (the Signal schema cap is
        "alert" — there is no "critical" Signal severity)
      - magnitude: 1 | 2 | 3 (matches the cost-vector anchor table in
        docs/spec-severity-framework-2026-05-18.md §2.2 — the bigger
        the bloat, the bigger the absolute $ impact across many ticks)
      - tier: "warn" | "alert" | "critical" — used by the catalog
        dispatch to pick the right Telegram event variant
    """
    if turn_count <= warn_turns:
        return None
    if turn_count > critical_turns:
        return ("alert", 3, "critical")
    if turn_count > alert_turns:
        return ("alert", 2, "alert")
    return ("warn", 1, "warn")


def detect_heartbeat_session_bloat(
    bot_id: str,
    turns: list[dict],
    *,
    warn_turns: int,
    alert_turns: int,
    critical_turns: int,
    max_per_run: int,
) -> list[dict]:
    """Fire when a heartbeat session ran more turns than the threshold.

    Heartbeat sessions are mechanical context checks. A correctly-behaving
    one is 1-3 turns: load context, observe, write. Anything north of
    ~5 is structurally wrong — typically a retry storm (exec-approval
    timeout, tool-deny loop, fallback walk) — regardless of cost.

    Detection rule:
      1. Group ``turns`` by session_id.
      2. For each session that ``_is_heartbeat_session`` matches:
      3. If ``len(turns) > warn_turns``, fire a Signal at the tier
         indicated by ``_heartbeat_bloat_severity``.

    Signature dedup is per-(bot, session_id) so one Signal per bloated
    session lives in the store — repeated cost_watchdog runs against
    the same JSONL update observation_count + details but don't
    duplicate. ``max_per_run`` caps how many distinct bloated sessions
    fire per tick so a pathological dump doesn't flood the Alerts page.

    Returns observe()-ready dicts sorted by turn count descending so
    the caller's ``max_per_run`` cap keeps the worst offenders.
    """
    if warn_turns <= 0:
        return []
    by_session: dict[str, list[dict]] = {}
    for t in turns:
        sid = t.get("session_id") or ""
        if not sid:
            continue
        by_session.setdefault(sid, []).append(t)

    candidates: list[tuple[str, list[dict], int]] = []
    for sid, sess_turns in by_session.items():
        if not _is_heartbeat_session(sess_turns):
            continue
        count = len(sess_turns)
        if count <= warn_turns:
            continue
        candidates.append((sid, sess_turns, count))

    candidates.sort(key=lambda t: -t[2])
    candidates = candidates[:max_per_run]

    out: list[dict] = []
    for sid, sess_turns, count in candidates:
        rating = _heartbeat_bloat_severity(
            count,
            warn_turns=warn_turns,
            alert_turns=alert_turns,
            critical_turns=critical_turns,
        )
        if rating is None:
            continue
        severity, magnitude, tier = rating

        # Cost & model rollup for the body — the operator needs to know
        # whether this bloat actually cost real money (model_override
        # leak in flight?) or was cheap (haiku workaround active).
        sess_cost = 0.0
        models_seen: set[str] = set()
        first_ts: str | None = None
        last_ts: str | None = None
        for t in sess_turns:
            c = float(t.get("cost") or 0.0)
            sess_cost += c
            model = str(t.get("model") or "").strip()
            if model:
                models_seen.add(model)
            ts = (t.get("ts") or "").strip()
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

        models_str = ", ".join(sorted(models_seen)) or "unknown"
        scope_key = f"{bot_id}/{sid}"
        out.append(
            {
                "signature": make_signature(
                    PRODUCER, "heartbeat_session_bloat", scope_key
                ),
                "producer": PRODUCER,
                "type": "heartbeat_session_bloat",
                "flavor": "maintenance",
                "severity": severity,
                "scope": "bot",
                "bot_id": bot_id,
                "title": (
                    f"{bot_id}: heartbeat session {sid[:8]} ran "
                    f"{count} turns (threshold {warn_turns})"
                ),
                "body": (
                    f"Heartbeat session `{sid}` ran {count} turns "
                    f"costing ${sess_cost:.2f} on {models_str}. "
                    f"Correct-shape heartbeats are 1-3 turns; >{warn_turns} "
                    f"is structurally wrong (typically a retry storm — "
                    f"exec-approval timeout, tool-deny loop, fallback walk). "
                    f"Check the session in Usage → Bot detail."
                ),
                "details": {
                    "bot_id": bot_id,
                    "session_id": sid,
                    "turn_count": count,
                    "warn_threshold": warn_turns,
                    "alert_threshold": alert_turns,
                    "critical_threshold": critical_turns,
                    "tier": tier,
                    "cost_usd": round(sess_cost, 4),
                    "models_seen": sorted(models_seen),
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                    # v2 context — heartbeat sessions are bot-internal so
                    # user/channel ids stay None and channel_kind is
                    # "internal". timestamp_local is derived from last_ts
                    # so the alert card shows the wall-clock time the
                    # bloat completed at in pod-local tz. Done inline so
                    # we don't need a per-day network read for this
                    # detector.
                    "timestamp_local": _to_local_ts_for_signal(last_ts),
                    "user_id": None,
                    "user_display_name": None,
                    "channel_id": None,
                    "channel_kind": "internal",
                    "vector": "cost",
                    "magnitude": magnitude,
                    "severity_active": True,
                    # Hint for the runner's dispatcher branch — keeps the
                    # tier mapping in one place (here) rather than smearing
                    # it across the runner.
                    #
                    # Single catalog entry covers all tiers since the
                    # 2026-05-29 warn/critical collapse. The visual
                    # differentiation (emoji + "Likely runaway loop"
                    # suffix) is producer-owned, populated below as
                    # ``level_emoji`` + ``trail`` so the dispatcher
                    # template can splice them in.
                    "catalog_event": "cost.heartbeat_session_bloat",
                    "level_emoji": "🔴" if tier == "critical" else "💰",
                    "trail": (
                        " Likely runaway loop — investigate."
                        if tier == "critical" else ""
                    ),
                    "what_it_means": (
                        f"A single heartbeat session on {bot_id} ran "
                        f"{count} turns, far more than the "
                        f"{warn_turns}-turn budget for a healthy "
                        "heartbeat. Correct-shape heartbeats are 1-3 "
                        "turns (load context, observe, write). "
                        f"{count}-turn runs are structurally wrong — "
                        "typically a retry storm from an exec-approval "
                        "timeout, a tool-deny loop, or a model "
                        "fallback walk. This was the 2026-05-20 "
                        "cost-blackout signature."
                    ),
                    "fix_steps": (
                        f"1. Open the session in Usage → Bot detail "
                        f"for `{bot_id}`, session_id `{sid[:8]}`\n"
                        "2. Scan the turn timeline for the repeat "
                        "pattern (same tool / same model / same error)\n"
                        "3. If exec approvals are timing out: check "
                        f"`{_bot_home(bot_id)}/.openclaw/exec-approvals.json` "
                        "for stale entries\n"
                        f"4. Hard-cap to halt the bleed:\n"
                        f"   ssh pod_admin_user@mini sudo evolve-admin "
                        f"breaker trip {bot_id} cost --duration 24h\n"
                        "5. Then root-cause the loop before un-tripping"
                    ),
                },
            }
        )
    return out


def detect_model_override_violated(
    bot_id: str,
    today_events: list[dict],
    openclaw_json: dict | None,
    *,
    min_cost_usd: float,
    max_per_run: int,
) -> list[dict]:
    """Retired 2026-06-04. Same root cause as
    ``detect_heartbeat_no_model_override``: the Evolve plugin's ModelRouter
    pre-classifies every heartbeat session as ``background`` via the
    ``before_model_resolve`` hook and routes to ``tier3.models[0]`` from
    ``evolve-tiers.json``. The session-class cache holds across all turns
    of a session, so the historical "first prompt honors override, follow-
    ups leak to primary" failure mode this detector watched for is no
    longer reachable on the normal heartbeat path.

    Why it had to retire (not just rescope to the new shape): with
    ModelRouter active, *every* heartbeat session on a bot whose
    ``heartbeat.model`` literal still names Sonnet/Opus appears to "leak"
    to Haiku — but that's ModelRouter doing its job, not a leak. The
    detector was actively firing false positives on team-bot-a/team-bot-b/team-bot-c
    (2026-06-04 firings) calling intentional tier-3 routing a violation.

    The right tier-3-aware replacement would compare each heartbeat
    session's billed model against the bot's ``tier3.models[0]`` from
    evolve-tiers.json, not against ``agents.defaults.heartbeat.model``.
    That's a parallel detector with different input shape and is left for
    a follow-up.

    Kept as a no-op stub so call sites and tests don't break; safe to
    delete entirely in a follow-up cleanup.
    """
    del bot_id, today_events, openclaw_json, min_cost_usd, max_per_run
    return []


def read_openclaw_log_tail(
    bot_id: str,
    config: dict[str, Any] | None = None,
    *,
    max_bytes: int,
) -> str | None:
    """Return the final ``max_bytes`` of ``~/.openclaw/logs/openclaw.log``.

    Same sudo-fallback shape as the embedding_monitor reader. Returns
    None on any read failure — the detector treats absence as "nothing
    happening" rather than emitting a noisy infrastructure-gap signal.
    """
    path = _bot_home(bot_id, config) / ".openclaw" / "logs" / "openclaw.log"
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


def parse_fallback_exhaustion_events(
    text: str, *, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Pull ``chain_exhausted`` + ``model_not_found`` events from the log.

    Each OpenClaw log line is a JSON dict from tslog with keys
    ``"0"`` / ``"1"`` / ``"2"`` / ``"_meta"``. The event payload lives
    under ``"1"`` and the wall-clock timestamp under ``"_meta"["date"]``.

    Only lines whose payload has both:
        - ``fallbackStepFinalOutcome == "chain_exhausted"``
        - ``reason == "model_not_found"``
    are returned. Anything else (transient 5xx, rate-limit retries that
    eventually succeeded, embedding-side failures) is somebody else's
    detector — keeping the filter tight here is what makes the
    threshold trustworthy enough to fire CRITICAL.

    Lines that don't parse as JSON or lack the expected shape are
    skipped silently. The OpenClaw log mixes multi-line records with
    one-line records; tolerating malformed lines is required.
    """
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        # Cheap pre-filter: bail before paying the JSON parse cost on
        # the >99% of lines that obviously aren't the event we want.
        if "chain_exhausted" not in line or "model_not_found" not in line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        event = rec.get("1")
        if not isinstance(event, dict):
            continue
        if event.get("fallbackStepFinalOutcome") != "chain_exhausted":
            continue
        if event.get("reason") != "model_not_found":
            continue
        meta = rec.get("_meta") or {}
        ts_raw = meta.get("date")
        ts: datetime | None = None
        if isinstance(ts_raw, str):
            try:
                normalized = ts_raw.rstrip("Z") + ("+00:00" if ts_raw.endswith("Z") else "")
                ts = datetime.fromisoformat(normalized).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                ts = None
        if since is not None and (ts is None or ts < since):
            continue
        out.append({
            "ts": ts,
            "requested_provider": event.get("requestedProvider"),
            "requested_model": event.get("requestedModel"),
            "from_model": event.get("fallbackStepFromModel"),
            "error_preview": (event.get("errorPreview") or "")[:240],
        })
    return out


def detect_model_fallback_exhaustion(
    bot_id: str,
    events: list[dict[str, Any]],
    *,
    window_minutes: int,
    threshold: int,
) -> list[dict]:
    """Fire CRITICAL when the chain-exhausted event count breaches threshold.

    The OC contract: a turn that picks a model with no ``models.providers``
    registry entry walks the whole fallback chain throwing
    ``FailoverError`` at each step, then surfaces ``chain_exhausted``
    to the caller. Each walk can bill input tokens before the failure
    is final — so a steady stream of these is "money burning right
    now," not a cleanup task for tomorrow.

    Signature dedup is per-bot. One Signal per bot, updated as the
    count climbs; auto-resolved by sweep_resolve once the next
    cost_watchdog tick comes in clean (typically after the deploy
    reconciler refills the registry).
    """
    if threshold <= 0 or not events:
        return []
    if len(events) < threshold:
        return []

    requested = sorted({
        e["requested_model"] for e in events
        if isinstance(e.get("requested_model"), str)
    })
    requested_preview = ", ".join(f"`{m}`" for m in requested[:5])
    if len(requested) > 5:
        requested_preview += f", … (+{len(requested) - 5} more)"

    sample_error = ""
    for e in events:
        if e.get("error_preview"):
            sample_error = e["error_preview"]
            break

    last_ts = None
    for e in events:
        ts = e.get("ts")
        if isinstance(ts, datetime) and (last_ts is None or ts > last_ts):
            last_ts = ts

    return [
        {
            "signature": make_signature(
                PRODUCER, "model_fallback_exhaustion", bot_id
            ),
            "producer": PRODUCER,
            "type": "model_fallback_exhaustion",
            "flavor": "maintenance",
            "severity": "alert",
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: model fallback chain exhausting "
                f"({len(events)} times in {window_minutes}m)"
            ),
            "body": (
                f"OpenClaw's model failover walked the entire fallback "
                f"chain and threw `chain_exhausted` on {len(events)} "
                f"requests in the last {window_minutes} minutes. Each "
                f"walk can bill input tokens before the failure is "
                f"final — this is money burning now. Requested model(s): "
                f"{requested_preview}.\n\n"
                f"Most common cause: a model id in `agents.defaults.models` "
                f"lacks a matching entry in `models.providers[<prov>].models[]`. "
                f"The deploy reconciler "
                f"(`ensure_plugin_config`) fixes the gap; the audit "
                f"check `_check_provider_models_registry` shows the "
                f"specific slugs that are missing."
            ),
            "details": {
                "bot_id": bot_id,
                "event_count": len(events),
                "window_minutes": window_minutes,
                "threshold": threshold,
                "requested_models": requested,
                "sample_error_preview": sample_error,
                "last_event_at": last_ts.isoformat() if last_ts else None,
                "timestamp_local": _to_local_ts_for_signal(
                    last_ts.isoformat() if last_ts else None
                ),
                "vector": "cost",
                "magnitude": 3,
                "severity_active": True,
                "user_id": None,
                "user_display_name": None,
                "channel_id": None,
                "channel_kind": "internal",
                "what_it_means": (
                    f"{bot_id}'s OpenClaw gateway is rejecting every "
                    "model lookup in its fallback chain with "
                    "`model_not_found` and surfacing "
                    "`chain_exhausted` to the caller. Each rejection "
                    "may still bill input tokens for the attempted "
                    "request shape before failover gives up. The "
                    "2026-06-03 personal-bot incident hit this same "
                    "code path and burned $36 in two background turns "
                    "before the operator noticed."
                ),
                "fix_steps": (
                    f"1. Open Alerts → search for `audit_config` on "
                    f"`{bot_id}` to see which slugs are missing\n"
                    f"2. Redeploy to run the reconciler:\n"
                    f"   ssh pod_admin_user@mini sudo evolve-admin "
                    f"deploy {bot_id}\n"
                    f"3. Restart the gateway:\n"
                    f"   ssh pod_admin_user@mini sudo /bin/launchctl "
                    f"kickstart -k "
                    f"system/ai.evolve.openclaw.{bot_id}.gateway\n"
                    f"4. Hard-cap as a belt-and-suspenders while you "
                    f"verify:\n"
                    f"   ssh pod_admin_user@mini sudo evolve-admin "
                    f"breaker trip {bot_id} cost --duration 1h\n"
                    f"5. Confirm clear by tailing the gateway log:\n"
                    f"   ssh pod_admin_user@mini sudo tail -f "
                    f"{_bot_home(bot_id)}/.openclaw/logs/openclaw.log "
                    f"| grep chain_exhausted"
                ),
            },
        }
    ]


# ── Runner ───────────────────────────────────────────────────────────────────


def collect_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    today: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Run all detectors for one bot. Returns observe() kwargs dicts."""
    thresholds = _thresholds_for_bot(bot_id, config)
    now_dt = now or datetime.now(timezone.utc)
    if today is None:
        # Day rollover happens at midnight in the pod's local TZ — UTC
        # midnight is the wrong boundary for a pod-admin reading
        # "today's spend." spec-cost-caps-2026-06-05.md §"Day boundary".
        from pod_time import pod_today_str
        today = pod_today_str()

    today_events = list(read_events(bot_id, days=1, shared_dir=shared_dir, now=now_dt))
    window_events = list(
        read_events(
            bot_id,
            days=int(thresholds["automation_window_days"]),
            shared_dir=shared_dir,
            now=now_dt,
        )
    )
    outlier_events = list(
        read_events(
            bot_id,
            days=int(thresholds["outlier_window_days"]),
            shared_dir=shared_dir,
            now=now_dt,
        )
    )
    # cost_spike compares current 7d (now → -7d) against prior 7d
    # (-7d → -14d). Two windows are cheaper than reading 14d once and
    # splitting in Python, and `read_events` handles caching/dedup.
    cost_spike_cur_events = list(
        read_events(bot_id, days=7, shared_dir=shared_dir, now=now_dt)
    )
    cost_spike_prior_events = list(
        read_events(
            bot_id,
            days=7,
            shared_dir=shared_dir,
            now=now_dt - timedelta(days=7),
        )
    )
    crons = read_cron_jobs(bot_id, config)
    sizes = workspace_md_sizes(bot_id, config)
    # Persist today's snapshot and read back the trailing window — the
    # growth-rate detector needs historical samples to compute KB/day.
    # Stateful side-effect; write failures are swallowed inside the helper.
    write_workspace_snapshot(shared_dir, bot_id, sizes, today=today)
    workspace_history = read_workspace_snapshots(
        shared_dir,
        bot_id,
        days=int(thresholds["workspace_growth_window_days"]),
        today=today,
    )
    # efficiency_drift + cache_envelope_growth: rolling cur vs prior windows
    # on the same per-call event stream. Two reads (cheaper than one 28d
    # read split in Python — read_events handles caching/dedup).
    eff_cur_days = int(thresholds["efficiency_drift_cur_window_days"])
    eff_prior_days = int(thresholds["efficiency_drift_prior_window_days"])
    eff_cur_events = list(
        read_events(bot_id, days=eff_cur_days, shared_dir=shared_dir, now=now_dt)
    )
    eff_prior_events = list(
        read_events(
            bot_id,
            days=eff_prior_days,
            shared_dir=shared_dir,
            now=now_dt - timedelta(days=eff_cur_days),
        )
    )
    oc_json = read_openclaw_json(bot_id, config)
    # config_drift_snapshot: persist today's config snapshot and read
    # the most recent prior for diffing. Same side-effect pattern as
    # workspace_snapshot — write happens unconditionally so tomorrow
    # has a baseline; the detector only fires when an actual change
    # is observed between snapshots.
    config_snapshot_today = collect_config_snapshot(oc_json)
    write_config_snapshot(
        shared_dir, bot_id, config_snapshot_today, today=today,
    )
    prior_config_snapshot = read_prior_config_snapshot(
        shared_dir, bot_id, today=today,
    )
    today_turns = read_today_turns(bot_id, now=now_dt)
    # session_quality: trailing-window daily metrics (one per date dir).
    maintenance_window = int(thresholds["maintenance_ratio_window_days"])
    today_date = now_dt.date()
    daily_metrics: list[dict] = []
    for i in range(1, maintenance_window + 1):
        m = read_daily_metric(shared_dir, bot_id, today_date - timedelta(days=i))
        if m is not None:
            daily_metrics.append(m)

    detections: list[dict] = []
    detections += detect_daily_spend(
        bot_id,
        today_events,
        threshold_usd=float(thresholds["daily_spend_usd"]),
        today=today,
    )
    detections += detect_cost_spike(
        bot_id,
        cost_spike_cur_events,
        cost_spike_prior_events,
        multiplier=float(thresholds["cost_spike_multiplier"]),
        floor_usd=float(thresholds["cost_spike_floor_usd"]),
    )
    detections += detect_maintenance_ratio_high(
        bot_id,
        daily_metrics,
        threshold=float(thresholds["maintenance_ratio_threshold"]),
        window_days=maintenance_window,
    )
    detections += detect_automation_dominance(
        bot_id,
        window_events,
        ratio_threshold=float(thresholds["automation_ratio"]),
        min_turns=int(thresholds["automation_min_turns"]),
        window_days=int(thresholds["automation_window_days"]),
    )
    detections += detect_cron_wakes_agent(bot_id, crons)
    detections += detect_cron_overactive(
        bot_id,
        crons,
        factor=float(thresholds["cron_overactive_factor"]),
        window_hours=int(thresholds["cron_overactive_window_hours"]),
        config=config,
        now=now_dt,
    )
    detections += detect_context_bloat(bot_id, sizes, thresholds)
    detections += detect_config_drift(
        bot_id,
        config_snapshot_today,
        prior_config_snapshot,
        max_per_run=int(thresholds["config_drift_max_per_run"]),
    )
    detections += detect_workspace_growth_rate(
        bot_id,
        sizes,
        workspace_history,
        growth_kb_per_day_threshold=float(thresholds["workspace_growth_kb_per_day"]),
        min_window_days=int(thresholds["workspace_growth_min_window_days"]),
        min_current_kb=float(thresholds["workspace_growth_min_current_kb"]),
        max_per_run=int(thresholds["workspace_growth_max_per_run"]),
    )
    detections += detect_efficiency_drift(
        bot_id,
        eff_cur_events,
        eff_prior_events,
        multiplier=float(thresholds["efficiency_drift_multiplier"]),
        cur_window_days=eff_cur_days,
        prior_window_days=eff_prior_days,
        min_cur_calls=int(thresholds["efficiency_drift_min_cur_calls"]),
        min_prior_calls=int(thresholds["efficiency_drift_min_prior_calls"]),
        max_per_run=int(thresholds["efficiency_drift_max_per_run"]),
    )
    detections += detect_cache_write_volume(
        bot_id,
        eff_cur_events,
        eff_prior_events,
        multiplier=float(thresholds["cache_envelope_multiplier"]),
        cur_window_days=int(thresholds["cache_envelope_cur_window_days"]),
        prior_window_days=int(thresholds["cache_envelope_prior_window_days"]),
        min_cur_calls=int(thresholds["cache_envelope_min_cur_calls"]),
        min_prior_calls=int(thresholds["cache_envelope_min_prior_calls"]),
        min_cur_tokens_per_call=int(thresholds["cache_envelope_min_cur_tokens_per_call"]),
    )
    detections += detect_session_token_outlier(
        bot_id,
        outlier_events,
        factor=float(thresholds["outlier_factor"]),
        min_session_events=int(thresholds["outlier_min_session_events"]),
        min_cost_usd=float(thresholds["outlier_min_cost_usd"]),
        max_per_run=int(thresholds["outlier_max_per_run"]),
    )
    detections += detect_heartbeat_no_model_override(bot_id, oc_json)
    detections += detect_model_override_violated(
        bot_id,
        today_events,
        oc_json,
        min_cost_usd=float(thresholds["override_violation_min_cost_usd"]),
        max_per_run=int(thresholds["override_violation_max_per_run"]),
    )
    # llm_workload_redundant_with_script + heartbeat_cost_by_design are the
    # "structural" cost detectors: they catch a bot's design — HEARTBEAT.md
    # workload size and projected daily spend at the configured cadence —
    # rather than today's actuals. Same input streams (today_events, oc_json,
    # network cap) so reads are shared with the per-actuals detectors above.
    heartbeat_md_content = _read_heartbeat_md(bot_id, config)
    per_bot_cap = _resolve_per_bot_cap(bot_id, config, shared_dir)
    detections += detect_llm_workload_redundant_with_script(
        bot_id,
        heartbeat_md_content,
        oc_json,
        today_events,
        min_workload_chars=int(thresholds["llm_workload_min_chars"]),
        min_projected_daily_cost_usd=float(thresholds["llm_workload_min_daily_cost_usd"]),
        min_sessions_for_projection=int(thresholds["llm_workload_min_sessions"]),
    )
    detections += detect_heartbeat_cost_by_design(
        bot_id,
        oc_json,
        today_events,
        per_bot_cap,
        warn_fraction=float(thresholds["heartbeat_cost_warn_fraction"]),
        alert_fraction=float(thresholds["heartbeat_cost_alert_fraction"]),
        min_sessions_for_projection=int(thresholds["heartbeat_cost_min_sessions"]),
    )
    detections += detect_heartbeat_cadence_anomaly(
        bot_id,
        oc_json,
        today_events,
        factor=float(thresholds["heartbeat_cadence_anomaly_factor"]),
        alert_factor=float(thresholds["heartbeat_cadence_anomaly_alert_factor"]),
        min_extra_fires=int(thresholds["heartbeat_cadence_anomaly_min_extra_fires"]),
        now=now_dt,
    )
    detections += detect_heartbeat_session_bloat(
        bot_id,
        today_turns,
        warn_turns=int(thresholds["heartbeat_bloat_warn_turns"]),
        alert_turns=int(thresholds["heartbeat_bloat_alert_turns"]),
        critical_turns=int(thresholds["heartbeat_bloat_critical_turns"]),
        max_per_run=int(thresholds["heartbeat_bloat_max_per_run"]),
    )
    # model_fallback_exhaustion: tail the gateway log for
    # chain_exhausted+model_not_found events. Read-and-parse is bounded
    # by ``fallback_exhaustion_log_tail_bytes`` (default 512 KB) so a
    # blown-up log doesn't stall the watchdog.
    fb_window_mins = int(thresholds["fallback_exhaustion_window_minutes"])
    fb_log_text = read_openclaw_log_tail(
        bot_id,
        config,
        max_bytes=int(thresholds["fallback_exhaustion_log_tail_bytes"]),
    )
    if fb_log_text is not None:
        fb_since = now_dt - timedelta(minutes=fb_window_mins)
        fb_events = parse_fallback_exhaustion_events(fb_log_text, since=fb_since)
        detections += detect_model_fallback_exhaustion(
            bot_id,
            fb_events,
            window_minutes=fb_window_mins,
            threshold=int(thresholds["fallback_exhaustion_threshold"]),
        )
    return detections


def _dispatch_via_catalog(
    *,
    shared_dir: Path,
    network: dict,
    detection: dict,
    today: str,
) -> bool:
    """Fire a catalog Telegram alert for a detection that opts in.

    The detection dict can carry a ``details.catalog_event`` hint —
    when present, this function rounds-trips the dispatch through
    ``evolve_admin.alerts.dispatcher.send`` so the operator gets the
    same immediate notification surface as ``spend_alert``'s burst
    alerts. Detections without ``catalog_event`` are Signal-only.

    Dedup key embeds (producer, type, scope_key, day) so a repeated
    cost_watchdog tick on the same bloated session doesn't flood
    chat — the dispatcher's per-source cooldown layers on top.
    Returns True iff the dispatcher reported SENT.
    """
    details = detection.get("details") or {}
    catalog_event = details.get("catalog_event")
    if not catalog_event:
        return False
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(
            f"[cost_watchdog] dispatcher import failed; "
            f"{catalog_event} skipped: {exc}",
            flush=True,
        )
        return False

    # Match Telegram severity to the detection's tier. Catalog entries
    # used to encode tier in the key suffix (`*_critical`) but the
    # 2026-05-29 collapse moved tier into the payload — we read it from
    # ``details.tier`` here. Falls back to WARNING when absent.
    tier_hint = (details.get("tier") or "").lower()
    severity = (
        Severity.CRITICAL if tier_hint == "critical"
        else Severity.WARNING
    )

    bot_id = detection.get("bot_id") or details.get("bot_id") or "pod"
    session_id_full = str(details.get("session_id") or "") or "?"
    sig_short = session_id_full[:8]
    dedup_key = (
        f"cost_watchdog/{detection.get('type', 'unknown')}/"
        f"{bot_id}/{sig_short}/{today}"
    )

    # Pass the FULL session_id to the dispatch payload — the catalog
    # body templates render `{session_id:.8}` so the operator sees a
    # short prefix in Telegram while the Signal store still carries
    # the full UUID. That keeps the two surfaces aligned (clicking
    # "Cost → Bot detail" from Telegram and viewing the Signal both
    # let the operator pivot to the same session). Previously the
    # dispatch payload truncated, which made Telegram and Signal show
    # different identifiers for the same event.
    payload = {
        "bot_id": bot_id,
        "session_id": session_id_full,
        "turn_count": int(details.get("turn_count") or 0),
        "warn_threshold": int(details.get("warn_threshold") or 0),
        "cost": float(details.get("cost_usd") or 0.0),
        "models": ", ".join(details.get("models_seen") or []) or "unknown",
        # Producer-rendered tier markers — see detect_heartbeat_session_bloat
        # for the warn/critical split. Catalog template splices these.
        "level_emoji": details.get("level_emoji") or "💰",
        "trail": details.get("trail") or "",
    }

    try:
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="cost_watchdog",
            severity=severity,
            dedup_key=dedup_key,
            catalog_event=catalog_event,
            payload=payload,
        )
    except Exception as exc:
        print(
            f"[cost_watchdog] dispatcher.send raised; "
            f"{catalog_event} dropped: {exc}",
            flush=True,
        )
        return False
    return outcome.result == DispatchResult.SENT


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    today: str | None = None,
    now: datetime | None = None,
) -> tuple[set[str], int]:
    """Collect detections, write Signals, return (kept_signatures, count).

    Per spec §5.5 ("don't fight the breaker"): when a relevant breaker
    is tripped on this bot, suppressible signal types are skipped — we
    don't pile on with fresh cost/automation alerts while the operator
    already has the brake on. The signature is still added to ``kept``
    so the sweep-resolver doesn't mistakenly treat the condition as
    cleared during the trip's lifetime.
    """
    detections = collect_for_bot(
        bot_id, shared_dir, config, today=today, now=now
    )
    # Day rollover at pod-local midnight (see collect_for_bot). When caller
    # passes a `now`, we honor it for test reproducibility — otherwise read
    # the pod-local date.
    if today is not None:
        today_str = today
    elif now is not None:
        today_str = now.strftime("%Y-%m-%d")
    else:
        from pod_time import pod_today_str
        today_str = pod_today_str()
    # Lazy import — keeps the import dependency cost off the hot path of
    # modules that import cost_watchdog as a library.
    try:
        from breakers.suppression import find_suppressing_breaker
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cost_watchdog] suppression import failed; "
            f"proceeding without suppression: {exc}",
            flush=True,
        )
        find_suppressing_breaker = None  # type: ignore[assignment]

    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue

        # Suppression check — spec §5.5. Cost / automation signals are
        # squelched while a relevant breaker is tripped on this bot.
        # Fail-open: any error in the lookup proceeds with normal
        # observe().
        sup_rec = None
        if find_suppressing_breaker is not None:
            category = _SUPPRESSIBLE_TYPES_TO_CATEGORY.get(d.get("type", ""))
            if category is not None:
                try:
                    sup_rec = find_suppressing_breaker(
                        shared_dir, bot_id, category=category, now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[cost_watchdog] suppression check raised for "
                        f"{d['signature']}; proceeding: {exc}",
                        flush=True,
                    )
                    sup_rec = None
        if sup_rec is not None:
            print(
                f"[cost_watchdog] suppressed {d['signature']} "
                f"(breaker={sup_rec.type} scope={sup_rec.bot_id} "
                f"trip_id={sup_rec.trip_id[:8]})",
                flush=True,
            )
            continue

        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[cost_watchdog] observe failed for {d['signature']}: {exc}",
                flush=True,
            )
        # Opt-in Telegram dispatch via catalog. Only detections that
        # set details.catalog_event flow here; the dispatcher's
        # subscription gating + per-source cooldown govern delivery.
        if (d.get("details") or {}).get("catalog_event"):
            _dispatch_via_catalog(
                shared_dir=shared_dir,
                network=config,
                detection=d,
                today=today_str,
            )
    return kept, len(detections)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="cost_watchdog — emit Signals for cost antipatterns",
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

    # PR C — session-budget breaker signal emission. The TS plugin
    # writes a per-session breaker file when a runaway crosses its cap;
    # this pass converts those files into Signals on the watchlist.
    # Owns a distinct PRODUCER ("session_cost_monitor") so its kept-set
    # and sweep-resolve are independent of cost_watchdog's own.
    session_budget_kept: set[str] = set()
    session_budget_total = 0
    try:
        from signals import session_budget_emit
    except ImportError as exc:
        print(
            f"[cost_watchdog] session_budget_emit import failed; "
            f"skipping session-budget signals this run: {exc}",
            flush=True,
        )
        session_budget_emit = None  # type: ignore[assignment]
    if session_budget_emit is not None:
        for bot in all_bots:
            try:
                detections = session_budget_emit.collect_for_bot(bot, shared_dir)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[cost_watchdog] session_budget collect failed for {bot}: {exc}",
                    flush=True,
                )
                continue
            for d in detections:
                session_budget_kept.add(d["signature"])
                session_budget_total += 1
                if args.dry_run:
                    print(
                        json.dumps({"would_observe_session_budget": d}, default=str),
                        flush=True,
                    )
                    continue
                try:
                    signals_store.observe(shared_dir, **d)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[cost_watchdog] session_budget observe failed for "
                        f"{d['signature']}: {exc}",
                        flush=True,
                    )

    if args.dry_run:
        print(
            f"[cost_watchdog] dry-run: {len(all_bots)} bots, {total} would-fire, "
            f"session_budget would-fire={session_budget_total}",
            flush=True,
        )
        return

    try:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=all_kept,
            reason="auto-resolve: cost_watchdog condition cleared on next run",
        )
    except Exception as exc:
        resolved = []
        print(f"[cost_watchdog] sweep_resolve failed: {exc}", flush=True)

    # Separate sweep-resolve pass for session_budget signals so the
    # session_cost_monitor producer's resolved set is tracked
    # independently of cost_watchdog's own.
    if session_budget_emit is not None:
        try:
            session_budget_resolved = signals_store.sweep_resolve(
                shared_dir,
                producer="session_cost_monitor",
                kept_signatures=session_budget_kept,
                reason=(
                    "auto-resolve: session-budget breaker file cleared "
                    "(operator reset or session ended)"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            session_budget_resolved = []
            print(
                f"[cost_watchdog] session_budget sweep_resolve failed: {exc}",
                flush=True,
            )
        if session_budget_resolved or session_budget_total:
            print(
                f"[cost_watchdog] session_budget: {session_budget_total} firings, "
                f"{len(session_budget_resolved)} resolved",
                flush=True,
            )

    print(
        f"[cost_watchdog] {len(all_bots)} bots, {total} firings, "
        f"{len(resolved)} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
