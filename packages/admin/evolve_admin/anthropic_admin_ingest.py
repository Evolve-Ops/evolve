"""evolve_admin.anthropic_admin_ingest — daily Anthropic Admin snapshot.

Tier 2.2 Phase B. Runs once a day under launchd as the ``evolve`` user.
On each run:

  1. Resolves yesterday's UTC day window.
  2. Fetches Anthropic's org-level cost report for that day and writes
     a snapshot to ``{shared_dir}/anthropic_api/cost_report/<date>.json``
     so trend tracking has historical data without re-hitting the API.
  3. Fetches one page of audit-log events for that day and writes them
     to ``{shared_dir}/anthropic_api/audit_logs/<date>.jsonl`` for
     compliance review.
  4. Computes the local cost ledger's total for the same day across
     ``network.members`` and emits a ``cost_diverges_from_anthropic``
     Signal when the two totals disagree by more than
     ``divergence_threshold`` (default 10%).
  5. ``sweep_resolve``s any prior divergence signal that no longer
     fires — so when the ledger lines back up, the alert auto-archives.

No-ops cleanly when the admin key isn't configured (logs once, exits 0)
so the daemon doesn't spam errors on a fresh install. All API calls go
through the injected ``Transport`` to keep tests offline.

CLI: ``run_anthropic_admin_ingest.py --shared-dir ... --network ...``
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_admin.anthropic_admin import (
    AuditLogPage,
    CostReport,
    Transport,
    fetch_audit_logs,
    fetch_cost_report,
    load_admin_api_key,
)


log = logging.getLogger(__name__)


# Divergence threshold — fraction of the larger of (local, anthropic).
# 10% is a deliberate big-deal threshold; small day-boundary effects from
# clock skew or partial-day reporting shouldn't fire this.
DEFAULT_DIVERGENCE_THRESHOLD = 0.10

# Signal signature for the divergence alert — stable across days so the
# Alerts page collapses recurring divergence into one entry. Resolved via
# sweep_resolve when a future run lands inside the threshold.
DIVERGENCE_SIGNATURE = "cost_diverges_from_anthropic"
PRODUCER = "anthropic_admin_ingest"


@dataclass
class IngestResult:
    """Outcome of one ingest run — returned for tests and CLI logging."""

    date: str
    cost_report_written: bool
    audit_log_written: bool
    anthropic_total_usd: float | None
    local_total_usd: float | None
    divergence_fraction: float | None
    divergence_signal_fired: bool
    errors: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Date window
# ─────────────────────────────────────────────────────────────────────────────


def yesterday_window(now: datetime | None = None) -> tuple[str, str, str]:
    """Return (date_yyyymmdd, starting_at_iso, ending_at_iso) for yesterday UTC.

    The Anthropic cost-report endpoint accepts ISO-8601 with timezone;
    we hand it explicit ``...T00:00:00Z`` boundaries so partial-day
    rollovers don't double-count.
    """
    now = now or datetime.now(tz=timezone.utc)
    today_midnight = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yest_midnight = today_midnight - timedelta(days=1)
    date_str = yest_midnight.strftime("%Y-%m-%d")
    starting_at = yest_midnight.strftime("%Y-%m-%dT%H:%M:%SZ")
    ending_at = today_midnight.strftime("%Y-%m-%dT%H:%M:%SZ")
    return date_str, starting_at, ending_at


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot writers
# ─────────────────────────────────────────────────────────────────────────────


def cost_report_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "anthropic_api" / "cost_report"


def audit_log_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "anthropic_api" / "audit_logs"


def write_cost_report_snapshot(
    shared_dir: Path, date: str, report: CostReport
) -> Path:
    d = cost_report_dir(shared_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def write_audit_log_snapshot(
    shared_dir: Path, date: str, page: AuditLogPage
) -> Path:
    """Write the page's events as JSONL — one event per line."""
    d = audit_log_dir(shared_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.jsonl"
    tmp = p.with_suffix(".jsonl.tmp")
    lines = [json.dumps(ev, separators=(",", ":")) for ev in page.events()]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(p)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Local total (for divergence)
# ─────────────────────────────────────────────────────────────────────────────


def local_total_for_window(
    members: list[str],
    *,
    ending_at: datetime,
    shared_dir: Path,
) -> float:
    """Sum cost_event.cost_usd over the trailing 24h ending at ``ending_at``.

    Imports cost_ledger lazily — keeps this module importable in
    environments where evolve-analyzer isn't installed.
    """
    from cost_ledger import read_events  # type: ignore[import-not-found]

    total = 0.0
    for bot_id in members:
        for ev in read_events(bot_id, days=1, shared_dir=shared_dir, now=ending_at):
            v = ev.get("cost_usd")
            if isinstance(v, (int, float)):
                total += float(v)
    return total


def _audit_logs_enabled(network: dict) -> bool:
    """Read the opt-in toggle from ``network.anthropic_admin.audit_logs_enabled``.

    Defaults to ``False`` because the Compliance API path Anthropic
    uses for audit logs isn't on public docs — the daemon shouldn't
    burn a daily 404 on an endpoint the operator hasn't licensed yet.
    """
    cfg = network.get("anthropic_admin") if isinstance(network, dict) else None
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("audit_logs_enabled", False))


def compute_divergence_fraction(local_usd: float, anthropic_usd: float) -> float:
    """Return |local - anthropic| / max(local, anthropic), or 0 if both ~0.

    Symmetric and bounded in [0, 1]. Two values of 0 are considered
    fully aligned — there's nothing to disagree about.
    """
    denom = max(abs(local_usd), abs(anthropic_usd))
    if denom < 1e-9:
        return 0.0
    return abs(local_usd - anthropic_usd) / denom


# ─────────────────────────────────────────────────────────────────────────────
# Ingest entry point
# ─────────────────────────────────────────────────────────────────────────────


def ingest_yesterday(
    shared_dir: Path,
    network: dict,
    *,
    transport: Transport | None = None,
    now: datetime | None = None,
    divergence_threshold: float = DEFAULT_DIVERGENCE_THRESHOLD,
    api_key: str | None = None,
) -> IngestResult:
    """Run one ingest cycle for yesterday's UTC day. Never raises.

    Returns an ``IngestResult`` with per-step success flags and any
    errors encountered. The daemon wrapper logs these but exits 0 so a
    transient API failure doesn't generate launchd error events.
    """
    date_str, starting_at, ending_at = yesterday_window(now=now)
    ending_dt = datetime.strptime(ending_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    errors: list[str] = []

    key = api_key if api_key is not None else load_admin_api_key(Path(shared_dir))
    if not key:
        log.info("anthropic admin key not configured; ingest skipped")
        return IngestResult(
            date=date_str,
            cost_report_written=False,
            audit_log_written=False,
            anthropic_total_usd=None,
            local_total_usd=None,
            divergence_fraction=None,
            divergence_signal_fired=False,
            errors=["admin key not configured"],
        )

    # ── Cost report ─────────────────────────────────────────────────
    report, err = fetch_cost_report(
        key,
        starting_at=starting_at,
        ending_at=ending_at,
        bucket_width="1d",
        transport=transport,
    )
    cost_written = False
    anth_total: float | None = None
    if report is not None:
        try:
            write_cost_report_snapshot(Path(shared_dir), date_str, report)
            cost_written = True
            anth_total = report.total_cost_usd
        except OSError as exc:
            errors.append(f"cost_report snapshot write: {exc}")
    else:
        errors.append(
            f"cost_report fetch: {err.status if err else 0} {err.message if err else ''}".strip()
        )

    # ── Audit logs ──────────────────────────────────────────────────
    # Off by default: the audit-log endpoint is part of Anthropic's
    # Compliance API, which an operator's Primary Owner has to request
    # via their account team. The exact URL is not on public docs
    # (it ships in a gated PDF) and our initial guess at the path
    # 404s. Opt-in via ``network.anthropic_admin.audit_logs_enabled``
    # once you have the correct path and a Compliance API license —
    # keep ``fetch_audit_logs`` and the on-demand endpoint usable
    # for manual testing during that rollout.
    audit_written = False
    if _audit_logs_enabled(network):
        page, err = fetch_audit_logs(
            key,
            starting_at=starting_at,
            ending_at=ending_at,
            limit=100,
            transport=transport,
        )
        if page is not None:
            try:
                write_audit_log_snapshot(Path(shared_dir), date_str, page)
                audit_written = True
            except OSError as exc:
                errors.append(f"audit_log snapshot write: {exc}")
        else:
            errors.append(
                f"audit_log fetch: {err.status if err else 0} {err.message if err else ''}".strip()
            )

    # ── Divergence ──────────────────────────────────────────────────
    # Only meaningful when we have an Anthropic total to compare against.
    members: list[str] = list(network.get("members") or [])
    local_total: float | None = None
    divergence: float | None = None
    fired = False
    if anth_total is not None:
        try:
            local_total = local_total_for_window(
                members, ending_at=ending_dt, shared_dir=Path(shared_dir)
            )
            divergence = compute_divergence_fraction(local_total, anth_total)
            fired = _emit_or_resolve_divergence(
                shared_dir=Path(shared_dir),
                date=date_str,
                local_total=local_total,
                anthropic_total=anth_total,
                divergence=divergence,
                threshold=divergence_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            # Don't let a signal-store hiccup hide the snapshot write.
            errors.append(f"divergence: {type(exc).__name__}: {exc}")

    return IngestResult(
        date=date_str,
        cost_report_written=cost_written,
        audit_log_written=audit_written,
        anthropic_total_usd=anth_total,
        local_total_usd=local_total,
        divergence_fraction=divergence,
        divergence_signal_fired=fired,
        errors=errors,
    )


def _emit_or_resolve_divergence(
    *,
    shared_dir: Path,
    date: str,
    local_total: float,
    anthropic_total: float,
    divergence: float,
    threshold: float,
) -> bool:
    """Emit the divergence signal when above threshold; sweep otherwise."""
    from signals import store as signals_store  # type: ignore

    if divergence > threshold:
        body = (
            f"Anthropic reports ${anthropic_total:.2f} for {date}; "
            f"local cost ledger reports ${local_total:.2f}. "
            f"Divergence: {divergence * 100:.1f}% (threshold "
            f"{threshold * 100:.0f}%)."
        )
        signals_store.observe(
            shared_dir,
            signature=DIVERGENCE_SIGNATURE,
            producer=PRODUCER,
            type="cost_diverges_from_anthropic",
            flavor="maintenance",
            severity="warn",
            scope="pod",
            title="Cost ledger diverges from Anthropic admin total",
            body=body,
            details={
                "date": date,
                "anthropic_total_usd": round(anthropic_total, 4),
                "local_total_usd": round(local_total, 4),
                "divergence_fraction": round(divergence, 4),
                "threshold": threshold,
            },
        )
        signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures={DIVERGENCE_SIGNATURE},
        )
        return True

    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=set(),
    )
    return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anthropic_admin_ingest")
    parser.add_argument("--shared-dir", default="/Users/Shared/evolve")
    parser.add_argument(
        "--network", default="/Users/Shared/evolve/network.json"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    shared = Path(args.shared_dir)
    try:
        network: dict[str, Any] = json.loads(Path(args.network).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read network config %s: %s", args.network, exc)
        return 0  # don't generate a launchd error event

    result = ingest_yesterday(shared, network)
    log.info(
        "anthropic_admin_ingest %s: cost=%s audit=%s anth=%s local=%s div=%s fired=%s errs=%s",
        result.date,
        result.cost_report_written,
        result.audit_log_written,
        result.anthropic_total_usd,
        result.local_total_usd,
        result.divergence_fraction,
        result.divergence_signal_fired,
        result.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
