"""
spend_caps.py — Hard spend cap enforcement for Evolve pods.

Writes an enforcement flag when a bot's daily spend hits the configured hard cap.
The flag is read by the OpenClaw plugin (ModelRouter) to downgrade tier or by
other enforcement actions (pause crons, suspend bot).

Flag format: {sharedDir}/spend-caps/{bot_id}-{YYYY-MM-DD}.json
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VALID_ACTIONS = ("alert-only", "downgrade-tier", "pause-crons", "suspend-bot")


# ── Flag file management ──────────────────────────────────────────────────────

def _caps_dir(shared_dir: Path) -> Path:
    d = shared_dir / "spend-caps"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o755)
    except OSError:
        pass
    return d


def _flag_path(shared_dir: Path, bot_id: str, today: date) -> Path:
    return _caps_dir(shared_dir) / f"{bot_id}-{today}.json"


def get_active_enforcement(shared_dir: Path, bot_id: str, today: date | None = None) -> dict | None:
    """Return active enforcement flag for bot_id today, or None if not active."""
    today = today or date.today()
    fp = _flag_path(shared_dir, bot_id, today)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text())
        if data.get("cleared"):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def get_all_active_enforcement(shared_dir: Path, members: list[str]) -> dict[str, dict]:
    """Return {bot_id: flag_data} for all bots with active enforcement today."""
    today = date.today()
    result = {}
    for bot_id in members:
        flag = get_active_enforcement(shared_dir, bot_id, today)
        if flag:
            result[bot_id] = flag
    return result


def write_enforcement_flag(
    shared_dir: Path,
    bot_id: str,
    action: str,
    spend_at_trigger: float,
    cap: float,
    today: date | None = None,
) -> Path:
    """Write (or overwrite) the enforcement flag for bot_id today."""
    today = today or date.today()
    fp = _flag_path(shared_dir, bot_id, today)
    payload = {
        "bot_id": bot_id,
        "date": str(today),
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "spend_at_trigger": round(spend_at_trigger, 4),
        "cap": cap,
        "cleared": False,
        "cleared_at": None,
    }
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, fp)
    try:
        os.chmod(fp, 0o644)
    except OSError:
        pass
    return fp


def clear_enforcement(shared_dir: Path, bot_id: str, today: date | None = None) -> bool:
    """Mark the enforcement flag as cleared. Returns True if a flag was found and cleared."""
    today = today or date.today()
    fp = _flag_path(shared_dir, bot_id, today)
    if not fp.exists():
        return False
    try:
        data = json.loads(fp.read_text())
        data["cleared"] = True
        data["cleared_at"] = datetime.now(timezone.utc).isoformat()
        fp.write_text(json.dumps(data, indent=2))
        return True
    except (json.JSONDecodeError, OSError):
        return False


def enforcement_already_triggered(shared_dir: Path, bot_id: str, today: date | None = None) -> bool:
    """Return True if an enforcement flag (cleared or not) exists for today."""
    today = today or date.today()
    return _flag_path(shared_dir, bot_id, today).exists()


# ── Cap-status helpers (used by the Cost Measures chips) ──────────────────────


def get_today_spend(
    shared_dir: Path, bot_id: str, today: date | None = None
) -> float | None:
    """Return today's spend for a bot from the daily metrics file.

    Reads ``{shared_dir}/metrics/{date}/{bot_id}.json`` and pulls
    ``total_cost_estimated``. Returns None if the file is missing or
    unparseable — callers treat that as "no data yet for today" rather
    than $0 spend.
    """
    today = today or date.today()
    mf = shared_dir / "metrics" / str(today) / f"{bot_id}.json"
    if not mf.exists():
        return None
    try:
        data = json.loads(mf.read_text())
        return float(data.get("total_cost_estimated", 0.0))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def get_cap_warnings(
    shared_dir: Path,
    members: list[str],
    cap: float | None,
    *,
    threshold_pct: float = 0.80,
    today: date | None = None,
) -> dict[str, dict]:
    """Bots at ``threshold_pct`` of the daily cap, NOT already enforced.

    Bots with active enforcement today are excluded — those are surfaced
    by the cap_active chip instead. Returns ``{bot_id: {spend, cap, pct}}``
    for each bot that crossed the warning threshold but hasn't tripped
    the hard cap yet. Returns ``{}`` when no daily cap is configured
    (None or 0).
    """
    if not cap or cap <= 0:
        return {}
    today = today or date.today()
    result: dict[str, dict] = {}
    for bot_id in members:
        if get_active_enforcement(shared_dir, bot_id, today):
            continue
        spend = get_today_spend(shared_dir, bot_id, today)
        if spend is None:
            continue
        pct = spend / cap
        if pct >= threshold_pct:
            result[bot_id] = {
                "spend": round(spend, 4),
                "cap": cap,
                "pct": round(pct, 4),
            }
    return result


def get_recent_enforcement_history(
    shared_dir: Path,
    members: list[str],
    *,
    days: int = 7,
    today: date | None = None,
) -> dict[str, int]:
    """Per-bot count of enforcement trips in the last ``days`` days,
    excluding today's active flags (those are surfaced by cap_active).

    Counts the existence of a flag file regardless of cleared status —
    a flag was *triggered* even if the operator manually cleared it
    afterward. Useful for spotting recurring offenders.
    """
    today = today or date.today()
    result: dict[str, int] = {}
    for bot_id in members:
        # Skip if currently active today — already surfaced by cap_active.
        active_today = get_active_enforcement(shared_dir, bot_id, today)
        count = 0
        for i in range(days):
            d = today - timedelta(days=i)
            if i == 0 and active_today:
                continue
            if _flag_path(shared_dir, bot_id, d).exists():
                count += 1
        if count > 0:
            result[bot_id] = count
    return result


# ── Breaker state helpers ─────────────────────────────────────────────────────

def _breaker_expired(expires_at: Any) -> bool:
    """True iff ``expires_at`` is a parseable ISO timestamp in the past.

    Local copy of the same helper in cost_opt_tiles._breaker_expired so
    the spend-caps summary and the per-bot tile chip can't disagree on
    whether an L1 breaker has timed out. Fail-open on parse errors —
    better to surface a stale trip than to drop a chip on malformed JSON.
    """
    if not expires_at:
        return False
    raw = str(expires_at)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        exp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def get_active_breakers(
    shared_dir: Path, members: list[str]
) -> dict[str, dict]:
    """Return ``{bot_id: {tripped_at, reason, cleared_at?}}`` for every bot
    whose L1 cost breaker is currently in the tripped state.

    A breaker file at ``{shared}/breakers/<bot>/cost.json`` is considered
    active when it has ``tripped_at`` set AND either ``cleared_at`` is
    missing or is strictly older than ``tripped_at`` (re-trip after a
    clear is allowed) AND its ``expires_at`` hasn't passed (TTL trips
    auto-clear at the TTL even if the reaper hasn't deleted the file
    yet). Mirrors ``detect_breaker_tripped_chip`` in cost_opt_tiles.py
    so the tile chip and the summary band never disagree about whether
    a breaker is up.

    Pre-fix, the expires_at gate was missing — auto-resetting L1 trips
    (24h TTL on the daily-hard-cap rung) showed as "active" on the
    summary band and chips long after they'd expired, contradicting the
    per-bot tile chips which had the gate. Surfaced 2026-06-06 on the
    Cost Optimization page header chip + summary band.

    Distinct from ``get_all_active_enforcement`` — that reads the
    spend-cap enforcement flag (today's daily_cap_usd was crossed),
    whereas breakers can be tripped manually or by detectors other
    than the spend-cap action (e.g. the bloat detector, the 2026-05-23
    safety-net sprint). The Cost Optimization summary band needs both.
    """
    result: dict[str, dict] = {}
    for bot_id in members:
        breaker_path = shared_dir / "breakers" / bot_id / "cost.json"
        try:
            data = json.loads(breaker_path.read_text())
        except (OSError, json.JSONDecodeError, PermissionError):
            continue
        if not isinstance(data, dict):
            continue
        tripped_at = data.get("tripped_at")
        cleared_at = data.get("cleared_at")
        if not tripped_at:
            continue
        # ISO 8601 strings sort lexicographically, so a string compare
        # is the canonical "is the trip still in effect?" check.
        if cleared_at and str(cleared_at) >= str(tripped_at):
            continue
        # TTL trips auto-clear at expires_at; the file persists until the
        # reaper sweeps it. Treat past-expiry as cleared so the summary
        # drops the count without waiting on the reaper.
        if _breaker_expired(data.get("expires_at")):
            continue
        result[bot_id] = {
            "tripped_at": tripped_at,
            "reason": data.get("reason") or data.get("trigger") or "",
        }
    return result


# ── Cap config helpers ────────────────────────────────────────────────────────

def get_caps_config(network: dict, shared_dir: Path | None = None) -> dict:
    """Extract spend cap config — read from better-engine-config first
    (Phase 8 of the 2026-06 cost-cap normalization), fall back to the
    legacy network.json::thresholds path for pre-migration installs.

    ``dailySpendCapUsd`` is resolved through
    ``BetterEngineConfig.resolve_budget_ladder(None)`` — the same single reader
    ``spend_alert`` enforces on. This function is what ``GET /api/spend-caps``
    renders, so any divergence between it and the enforcer shows the operator a
    cap that isn't real; that is exactly the defect this routing closes. Keep
    them on one reader.

    spec: docs/spec-cost-caps-2026-06-05.md.
    """
    # ── Phase 8: BE config pod_defaults is the canonical source ──
    if shared_dir is not None:
        try:
            from better_engine_config import load as _load_be  # type: ignore
            be = _load_be(shared_dir)
            pod = be.pod_defaults.get("budget") or {}
            pod_ladder = be.resolve_budget_ladder(None)
            return {
                "dailySpendAlertUsd": float(pod.get("per_bot_daily_warn_usd") or 5.0),
                "dailySpendCapUsd": pod_ladder["l1_breaker"],
                "weeklySpendAlertUsd": float(pod.get("pod_weekly_warn_usd") or 20.0),
                "weeklySpendCapUsd": None,  # legacy field; not in new spec
                # spendCapAction is implied by which BE config field is set:
                # tier_downgrade_usd → "downgrade-tier"; l2_breaker_usd → "suspend-bot";
                # neither → "alert-only".
                "spendCapAction": _infer_action_from_be(pod),
            }
        except Exception:
            # Fall through to the legacy read.
            pass

    t = network.get("thresholds", {})
    return {
        "dailySpendAlertUsd": float(
            network.get("alerts", {}).get("spendThresholdUSD")
            or t.get("dailySpendAlertUsd", 5.0)
        ),
        "dailySpendCapUsd": t.get("dailySpendCapUsd"),   # None = no hard cap
        "weeklySpendAlertUsd": float(t.get("weeklySpendAlertUsd", 20.0)),
        "weeklySpendCapUsd": t.get("weeklySpendCapUsd"),
        "spendCapAction": t.get("spendCapAction", "downgrade-tier"),
    }


def _infer_action_from_be(pod_budget: dict) -> str:
    """Reverse-map BE config pod-defaults back to a legacy spendCapAction
    string for back-compat with the existing GET /api/spend-caps consumers.

    Phase 8 only — Phase 9+ UI moves entirely off this string.
    """
    if pod_budget.get("l2_breaker_usd"):
        return "suspend-bot"
    if pod_budget.get("tier_downgrade_usd"):
        return "downgrade-tier"
    return "alert-only"


def save_caps_config(network_path: Path, updates: dict) -> None:
    """Write spend cap fields back to network.json thresholds."""
    # Load inline to avoid circular import
    data = json.loads(network_path.read_text()) if network_path.exists() else {}
    t = data.setdefault("thresholds", {})
    allowed = {"dailySpendAlertUsd", "dailySpendCapUsd", "weeklySpendAlertUsd",
               "weeklySpendCapUsd", "spendCapAction"}
    for k, v in updates.items():
        if k in allowed:
            if v is None:
                t.pop(k, None)
            else:
                t[k] = v

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-network-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        try:
            import shutil
            shutil.copy2(tmp, network_path)
            os.chmod(network_path, 0o644)
        except PermissionError:
            import subprocess
            r = subprocess.run(["sudo", "/bin/cp", tmp, str(network_path)], capture_output=True)
            if r.returncode != 0:
                raise PermissionError(f"sudo /bin/cp failed: {r.stderr.decode().strip()}")
            subprocess.run(["sudo", "/bin/chmod", "644", str(network_path)], capture_output=True)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Enforcement actions ───────────────────────────────────────────────────────

def execute_enforcement_action(
    action: str,
    bot_id: str,
    shared_dir: Path,
    bot_cfg: dict,
    alerts_cfg: dict,
    spend: float,
    cap: float,
) -> str:
    """Execute the enforcement action and return a human-readable result string."""
    if action == "alert-only":
        return "Alert sent (no automated action)"

    if action == "downgrade-tier":
        # Flag is already written — ModelRouter will read it on next turn
        return f"Tier downgraded to tier 3 for remainder of day (${spend:.2f} hit cap ${cap:.2f})"

    if action == "pause-crons":
        return _pause_bot_crons(bot_id)

    if action == "suspend-bot":
        return _suspend_bot_gateway(bot_id, bot_cfg)

    return f"Unknown action: {action}"


def _pause_bot_crons(bot_id: str) -> str:
    """Unload all launchd jobs for this bot (via the Scheduler seam)."""
    paused = []
    failed = []
    try:
        from runtime.scheduler import get_launchd_scheduler
        sched = get_launchd_scheduler()
        for label in sched.list():
            if f".{bot_id}." in label or label.endswith(f".{bot_id}"):
                # raw() escape hatch (counted S2 debt): this is pause, not
                # uninstall — remove() would bootout AND delete the plist;
                # `unload` keeps the plist on disk so the jobs can resume.
                rc, _out, _err = sched.raw(
                    "unload", f"/Library/LaunchDaemons/{label}.plist"
                )
                (paused if rc == 0 else failed).append(label)
    except Exception as exc:
        return f"Error pausing crons: {exc}"
    return f"Paused {len(paused)} cron jobs" + (f" ({len(failed)} failed)" if failed else "")


def _suspend_bot_gateway(bot_id: str, bot_cfg: dict) -> str:
    """Kill the OpenClaw gateway process for the bot.

    Uses a regex covering every entry-point marker (heal._GATEWAY_PROC_MARKERS)
    so we match `node .../openclaw/dist/entry.js` (openclaw >= 2026.4.29) as
    well as the legacy `openclaw-gateway` process title. `pkill -f` matches
    against the full command line, and `.` is escaped to avoid false positives.
    """
    import re as _re
    from heal import _GATEWAY_PROC_MARKERS
    user = bot_cfg.get("user", bot_id)
    pattern = "(" + "|".join(_re.escape(m) for m in _GATEWAY_PROC_MARKERS) + ")"
    try:
        r = subprocess.run(
            ["sudo", "-u", user, "pkill", "-u", user, "-f", pattern],
            capture_output=True, timeout=10,
        )
        if r.returncode in (0, 1):  # 1 = no process found
            return f"Gateway suspended for {bot_id}"
        return f"pkill returned {r.returncode}"
    except Exception as exc:
        return f"Error suspending gateway: {exc}"
