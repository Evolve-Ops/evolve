#!/usr/bin/env python3
"""
cost.py — Cost and model management monitor.

Runs on PRIMARY. Aggregates cost signals across all bots, detects
anomalies, and generates proposals to optimize spend.

Key behaviors:
  1. Daily spend monitoring — All bots use pay-per-use API keys (MAX subscription
     ended April 4, 2026). API spend is expected every active session day.
     Alert only when daily spend exceeds the configured threshold.

  2. Model drift detection — bot's configured model doesn't match what's
     expected in the network config. Could mean a manual change was made
     outside Evolve, or a proposal changed it without tracking.

  3. Cost anomaly detection — bot spending significantly more than its
     7-day average. Not always bad (could be a big task day) but worth flagging.

  4. Shared key staleness — shared keys not synced to all bots recently.
     Generates a sync reminder.

Usage:
  python3 cost.py                  # check all bots once
  python3 cost.py --bot-id admin_bot   # check one bot
  python3 cost.py --dry-run        # report only, no proposals

Data sources (in priority order):
  1. /Users/{bot}/.openclaw/logs/   — session cost logs written by OpenClaw
  2. shared_dir/metrics/            — measure.py output (includes cost fields)
  3. check-daily-cost.py output     — legacy script output
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from evolve_config import (
    load_config, get_shared_dir, get_members, get_alerts,
    resolve_network_path, sign_proposal, bot_home as _bot_home,
)
from evolve_util import now_iso

COST_VERSION = "0.1.0"


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class BotCostReport:
    bot_id: str
    date: str
    api_spend_usd: float = 0.0
    max_tokens_used: int = 0
    api_tokens_used: int = 0
    model_configured: str = ""
    model_expected: str = ""
    sessions_today: int = 0
    avg_spend_7d: float = 0.0
    billing_mode: str = "api_key"  # "api_key" or "subscription" (legacy MAX)
    anomaly: bool = False
    anomaly_reason: str = ""
    source: str = "unknown"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve cost and model monitor")
    parser.add_argument("--network", default=str(resolve_network_path()))
    parser.add_argument("--bot-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    members = get_members(config)

    if args.bot_id:
        members = [args.bot_id]

    if not members:
        print("[evolve/cost] No members configured.")
        return

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")
    reports = []

    for bot_id in members:
        report = collect_cost_report(bot_id, target_date, shared_dir, config)
        reports.append(report)
        _print_report(report)

    if not args.dry_run:
        for report in reports:
            _check_and_propose(report, shared_dir, config)
        _check_key_sync_staleness(shared_dir, config)

    # Write aggregated cost report
    _write_cost_summary(reports, target_date, shared_dir)


# ── Data collection ───────────────────────────────────────────────────────────

def collect_cost_report(bot_id: str, date: str, shared_dir: Path, config: dict) -> BotCostReport:
    report = BotCostReport(bot_id=bot_id, date=date)

    # 1. Try metrics from measure.py output
    metrics_path = shared_dir / "metrics" / date / f"{bot_id}.json"
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text())
            # measure.py v2 schema uses total_cost_estimated (not api_spend_usd).
            # max_tokens_used, api_tokens_used, model_primary do not exist in v2;
            # session_count is correct; model is read from openclaw.json at step 3.
            report.api_spend_usd = float(metrics.get("total_cost_estimated", 0))
            report.sessions_today = int(metrics.get("session_count", 0))
            report.source = "metrics"
        except Exception:
            pass

    # 2. Fallback: parse OpenClaw session logs
    if report.source == "unknown":
        log_dir = _bot_home(bot_id, config) / ".openclaw" / "logs"
        if log_dir.exists():
            spend, tokens = _parse_oc_logs(log_dir, date)
            report.api_spend_usd = spend
            report.api_tokens_used = tokens
            report.source = "logs"

    # 3. Get configured model from openclaw.json
    if not report.model_configured:
        report.model_configured = _get_configured_model(bot_id, config)

    # 4. Get expected model from network config
    report.model_expected = config.get("bots", {}).get(bot_id, {}).get("expectedModel", "")

    # 5. Compute 7-day average for anomaly detection
    report.avg_spend_7d = _compute_7d_avg(bot_id, date, shared_dir)

    # 6. Detect billing mode from auth profile
    report.billing_mode = _get_billing_mode(bot_id, config)

    # 7. Anomaly check — API spend is normal; alert only when threshold exceeded
    daily_threshold = float(config.get("thresholds", {}).get("dailySpendAlertUsd", 5.0))
    if report.api_spend_usd > daily_threshold:
        report.anomaly = True
        report.anomaly_reason = f"Daily spend ${report.api_spend_usd:.2f} exceeds threshold ${daily_threshold:.2f}"
    elif report.avg_spend_7d > 0 and report.api_spend_usd > report.avg_spend_7d * 3:
        report.anomaly = True
        report.anomaly_reason = f"Spend ${report.api_spend_usd:.4f} is {report.api_spend_usd/report.avg_spend_7d:.1f}x 7-day average"
    elif report.model_expected and report.model_configured and report.model_configured != report.model_expected:
        report.anomaly = True
        report.anomaly_reason = f"Model drift: configured={report.model_configured!r}, expected={report.model_expected!r}"

    return report


def _parse_oc_logs(log_dir: Path, date: str) -> tuple[float, int]:
    """Parse OpenClaw session logs for cost data."""
    total_spend = 0.0
    total_tokens = 0

    for log_file in log_dir.glob("*.log"):
        try:
            content = log_file.read_text(errors="ignore")
            # Look for cost log lines like: [2026-04-04] cost: $0.0012 (1234 tokens)
            import re
            for line in content.splitlines():
                if date not in line:
                    continue
                cost_match = re.search(r'cost[:\s]+\$([0-9.]+)', line, re.IGNORECASE)
                token_match = re.search(r'(\d+)\s+tokens?', line, re.IGNORECASE)
                if cost_match:
                    total_spend += float(cost_match.group(1))
                if token_match:
                    total_tokens += int(token_match.group(1))
        except Exception:
            pass

    return total_spend, total_tokens


def _get_configured_model(bot_id: str, config: dict | None = None) -> str:
    """Read primary model from a bot's openclaw.json."""
    oc_path = _bot_home(bot_id, config) / ".openclaw" / "openclaw.json"
    if not oc_path.exists():
        return ""
    try:
        oc = json.loads(oc_path.read_text())
        models = oc.get("agents", {}).get("defaults", {}).get("models", [])
        if models:
            first = models[0]
            if isinstance(first, dict):
                return first.get("model", "")
            return str(first)
    except Exception:
        pass
    return ""


def _compute_7d_avg(bot_id: str, date: str, shared_dir: Path) -> float:
    """Compute 7-day average API spend from metrics history."""
    try:
        from datetime import date as dateobj
        d = dateobj.fromisoformat(date)
        spends = []
        for i in range(1, 8):
            past = (d - timedelta(days=i)).isoformat()
            p = shared_dir / "metrics" / past / f"{bot_id}.json"
            if p.exists():
                m = json.loads(p.read_text())
                spends.append(float(m.get("total_cost_estimated", 0)))
        return sum(spends) / len(spends) if spends else 0.0
    except Exception:
        return 0.0


# ── Proposal generation ───────────────────────────────────────────────────────

def _check_and_propose(report: BotCostReport, shared_dir: Path, config: dict) -> None:
    if not report.anomaly:
        return

    # Spend threshold exceeded → handled exclusively by spend_alert.py (which
    # is the canonical daily-threshold pusher with its own flag-file dedup,
    # cooldown, and dispatcher routing under cost.daily_threshold). The
    # cost.py duplicate was retired in Phase C of the alert-subscriptions
    # spec — see docs/spec-alert-subscriptions-2026-05-10.md § 2.6.
    if "exceeds threshold" in report.anomaly_reason:
        pass  # spend_alert.py owns this event now

    # Model drift → config_change proposal
    elif "Model drift" in report.anomaly_reason and report.model_expected:
        _write_proposal(
            bot_id=report.bot_id,
            shared_dir=shared_dir,
            proposal_type="config_change",
            pattern_key=f"{report.bot_id}:model_drift",
            problem=report.anomaly_reason,
            root_cause="Model was changed outside of Evolve, or network.json expectation is stale.",
            risk="low",
            confidence=0.80,
            minimum_test=f"Confirm {report.bot_id} is using model {report.model_expected!r}",
            proposed_change={
                "path": "agents.defaults.models",
                "from": report.model_configured,
                "to": report.model_expected,
                "description": f"Restore expected model {report.model_expected!r}",
            },
        )


def _write_proposal(
    bot_id: str, shared_dir: Path, proposal_type: str, pattern_key: str,
    problem: str, root_cause: str, risk: str, confidence: float,
    minimum_test: str, proposed_change: dict | None = None,
) -> None:
    pending_dir = shared_dir / "proposals" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Dedup
    for stage in ("pending", "reviewed", "approved"):
        stage_dir = shared_dir / "proposals" / stage
        if not stage_dir.exists():
            continue
        for f in stage_dir.glob("*.json"):
            try:
                p = json.loads(f.read_text())
                if p.get("pattern_key") == pattern_key:
                    return
            except Exception:
                pass

    proposal_id = f"cost-{bot_id}-{int(time.time())}"
    proposal = {
        "id": proposal_id,
        "generated": now_iso(),
        "status": "pending",
        "schema_version": 1,
        "pattern_key": pattern_key,
        "target_bot": bot_id,
        "type": proposal_type,
        "category": "alert",
        "problem": problem,
        "root_cause": root_cause,
        "alternatives_considered": [],
        "confidence": confidence,
        "proposed_change": proposed_change or {},
        "minimum_test": minimum_test,
        "risk": risk,
        "forge_required": proposal_type == "config_change",
        "source": "cost.py",
    }
    sig = sign_proposal(proposal)
    if sig:
        proposal["evolve_sig"] = sig
    out = pending_dir / f"{proposal_id}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(proposal, indent=2))
    tmp.rename(out)
    print(f"[evolve/cost] Generated proposal: {proposal_id}")


def _check_key_sync_staleness(shared_dir: Path, config: dict) -> None:
    """Alert if shared keys haven't been synced to all bots recently."""
    keystore = Keystore(shared_dir)
    sync_log = keystore.load_sync_log()
    if not sync_log:
        return

    members = get_members(config)
    stale_threshold_days = 7
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_threshold_days)

    stale_bots = []
    for bot_id in members:
        last_sync = sync_log.get(bot_id)
        if last_sync:
            try:
                ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                if ts < cutoff:
                    stale_bots.append(bot_id)
            except Exception:
                stale_bots.append(bot_id)
        else:
            stale_bots.append(bot_id)

    if not stale_bots:
        return

    # Phase F: pass structured payload — catalog body_template + cli
    # action render the operator-facing message. The stale-bots list
    # is hashed into the dedup_key so a *change* in the set (more bots
    # stale, or some recovered) produces a fresh push.
    import hashlib
    fingerprint = hashlib.sha256(
        ",".join(sorted(stale_bots)).encode("utf-8")
    ).hexdigest()[:16]

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[evolve/cost] dispatcher import failed; "
              f"key-sync alert dropped: {exc}", file=sys.stderr)
        return

    try:
        _dispatch_send(
            shared_dir=shared_dir,
            network=config,
            source="cost",
            severity=Severity.WARNING,
            dedup_key=f"cost/key_rotation/{fingerprint}",
            catalog_event="security.key_rotation_overdue",
            payload={
                "stale_bots": ", ".join(stale_bots),
                "threshold_days": stale_threshold_days,
            },
        )
    except Exception as exc:
        print(f"[evolve/cost] dispatcher.send raised; "
              f"key-sync alert dropped: {exc}", file=sys.stderr)


def _write_cost_summary(reports: list[BotCostReport], date: str, shared_dir: Path) -> None:
    summary = {
        "date": date,
        "generated": now_iso(),
        "total_api_spend_usd": sum(r.api_spend_usd for r in reports),
        "anomalies": [
            {"bot_id": r.bot_id, "reason": r.anomaly_reason}
            for r in reports if r.anomaly
        ],
        "bots": [
            {
                "bot_id": r.bot_id,
                "api_spend_usd": r.api_spend_usd,
                "billing_mode": r.billing_mode,
                "model": r.model_configured,
                "sessions": r.sessions_today,
                "anomaly": r.anomaly,
            }
            for r in reports
        ],
    }
    out_dir = shared_dir / "cost"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{date}.json").write_text(json.dumps(summary, indent=2))


def _print_report(report: BotCostReport) -> None:
    flag = " ⚠️" if report.anomaly else " ✓"
    print(f"[evolve/cost] {report.bot_id}{flag}: "
          f"api=${report.api_spend_usd:.4f} model={report.model_configured or '?'} "
          f"src={report.source}")
    if report.anomaly:
        print(f"  → {report.anomaly_reason}")


# _alert_spend_threshold was removed in Phase C of the alert-subscriptions
# spec — it duplicated spend_alert.py's daily-threshold push, producing
# two operator-facing messages per spend overage. spend_alert.py owns the
# cost.daily_threshold event now (Phase 3b migration + Phase D
# catalog_event annotation).


def _get_billing_mode(bot_id: str, config: dict | None = None) -> str:
    """Detect billing mode from the bot's active auth profile."""
    auth_path = _bot_home(bot_id, config) / ".openclaw" / "auth-profiles.json"
    try:
        data = json.loads(auth_path.read_text())
        last_good = data.get("lastGood", "")
        profiles = data.get("profiles", {})
        if last_good and last_good in profiles:
            if profiles[last_good].get("type", "") in ("max", "subscription"):
                return "subscription"
        return "api_key"
    except Exception:
        return "api_key"  # Default post April 4, 2026


def get_daily_spend(bot_id: str, shared_dir: str | Path) -> float:
    """Return today's estimated API spend for bot_id (used by test_runner.py budget gate)."""
    today = datetime.now().strftime("%Y-%m-%d")
    metrics_file = Path(shared_dir) / "metrics" / today / f"{bot_id}.json"
    if metrics_file.exists():
        try:
            data = json.loads(metrics_file.read_text())
            return float(data.get("total_cost_estimated", 0.0))
        except Exception:
            pass
    return 0.0


def get_daily_budget(config: dict) -> float:
    """Return the daily budget limit from config (used by test_runner.py budget gate)."""
    return float(config.get("thresholds", {}).get("dailyBudgetUsd", 0.0))


# ── Keystore class (shared with keystore.py CLI) ──────────────────────────────

class Keystore:
    """
    Manages shared and per-bot API key records.

    The keystore tracks WHICH keys exist and their metadata (provider, scope,
    last-synced). The actual key values are read from/written to each bot's
    auth-profiles.json — the keystore is a coordination layer, not a vault.

    For shared keys, the canonical value lives in the keystore's encrypted
    store. For per-bot keys, the keystore only tracks metadata.

    Structure:
      shared_dir/keystore/
        keys.json          — key registry (names, providers, scopes, sync timestamps)
        shared/            — canonical values for shared keys (encrypted at rest)
          {provider}.enc   — encrypted key values
        sync-log.json      — {bot_id: last_synced_at} per bot
    """

    def __init__(self, shared_dir: Path) -> None:
        self.base = shared_dir / "keystore"
        self.registry_path = self.base / "keys.json"
        self.sync_log_path = self.base / "sync-log.json"
        self.shared_dir_path = self.base / "shared"

    def load_registry(self) -> dict:
        if self.registry_path.exists():
            try:
                return json.loads(self.registry_path.read_text())
            except Exception:
                pass
        return {"keys": {}, "schema_version": 1}

    def save_registry(self, registry: dict) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(registry, indent=2))
        tmp.rename(self.registry_path)

    def load_sync_log(self) -> dict:
        if self.sync_log_path.exists():
            try:
                return json.loads(self.sync_log_path.read_text())
            except Exception:
                pass
        return {}

    def save_sync_log(self, log: dict) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.sync_log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=2))
        tmp.rename(self.sync_log_path)

    def get_key_entry(self, key_name: str) -> dict | None:
        registry = self.load_registry()
        return registry.get("keys", {}).get(key_name)

    def list_keys(self) -> list[dict]:
        registry = self.load_registry()
        return [
            {"name": k, **v}
            for k, v in registry.get("keys", {}).items()
        ]

    def register_key(
        self,
        name: str,
        provider: str,
        scope: str,           # "shared" | "per-bot"
        description: str = "",
        bots: list[str] | None = None,  # None = all bots
    ) -> None:
        registry = self.load_registry()
        registry.setdefault("keys", {})[name] = {
            "provider": provider,
            "scope": scope,
            "description": description,
            "bots": bots,          # None = shared with all
            "registered_at": now_iso(),
            "last_rotated": None,
        }
        self.save_registry(registry)

    def record_sync(self, bot_id: str) -> None:
        log = self.load_sync_log()
        log[bot_id] = now_iso()
        self.save_sync_log(log)


if __name__ == "__main__":
    main()
