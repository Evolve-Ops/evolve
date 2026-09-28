"""cost_truth — the daily check that Evolve's price arithmetic still matches the bill.

D-CS2 (``internal/assessment-cost-spikes-product-2026-09-13.md`` §3) made
OpenClaw's own per-call ``usage.cost`` the figure every cost control reads
(``oc_usage_cost``). Evolve's price table survives as the fallback for a turn
OC could not price and as the basis of every projection — so it still has to
be right, and three times it silently was not. This is the standing check:

* **estimate vs OC**, per bot, per model: Evolve's table applied to OC's own
  token counts for yesterday's calls, against OC's figure for the same calls.
  Like-for-like tokens, so a gap is a wrong RATE, never a missing turn.
* **OC vs the provider console**: from the most recent
  ``evolve-admin cost reconcile`` document, when one names OC's total.

Either diverging by more than :data:`DRIFT_TOLERANCE` raises a firing
``cost_truth_drift`` Signal that names the model and the rate dimension that
moved most (``cache_write_1h`` is what the 2026-09-12 case would have named).
The Signal auto-resolves the first day the check comes back in band — and
only a day the check could actually SEE: a bot whose transcript DB was
unreadable keeps its Signal firing, and the console kind resolves only on a
day a reconcile document was read. No data is never "in band".

No cap, threshold or routing changes here — this reads and reports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

#: |ratio - 1| above this is drift (the brief's 10 %).
DRIFT_TOLERANCE = 0.10

#: A model's day below this much OC spend is not judged: a $0.02 day moves
#: 10 % on one rounding and would page the operator about nothing.
MIN_JUDGED_USD = 0.10

SIGNAL_TYPE = "cost_truth_drift"
PRODUCER = "spend_alert"


@dataclass(frozen=True)
class DriftFinding:
    """One out-of-band comparison, named down to the rate that moved."""

    kind: str                 # "estimate_vs_oc" | "oc_vs_console"
    day: str
    model: str
    truth_usd: float          # OC (estimate_vs_oc) / console (oc_vs_console)
    other_usd: float          # the estimate / OC
    dimension: str            # the rate dimension with the largest gap
    dimension_truth_usd: float | None = None
    dimension_other_usd: float | None = None
    bot_id: str | None = None
    by_dimension: dict = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return round(self.other_usd / self.truth_usd, 4) if self.truth_usd else 0.0

    @property
    def signature(self) -> str:
        who = self.bot_id or "pod"
        return f"{PRODUCER}:{SIGNAL_TYPE}:{self.kind}:{who}:{self.model}"

    def title(self) -> str:
        if self.kind == "estimate_vs_oc":
            return (
                f"{self.bot_id}: Evolve's price for {self.model} is off OpenClaw's "
                f"by {abs(self.ratio - 1) * 100:.0f}% ({self.dimension})"
            )
        return (
            f"OpenClaw's cost is off the provider bill by "
            f"{abs(self.ratio - 1) * 100:.0f}% ({self.dimension})"
        )

    def body(self) -> str:
        dim = ""
        if self.dimension_truth_usd is not None and self.dimension_other_usd is not None:
            dim = (
                f" The largest gap is the {self.dimension} rate: "
                f"${self.dimension_other_usd:.2f} against ${self.dimension_truth_usd:.2f}."
            )
        if self.kind == "estimate_vs_oc":
            return (
                f"On {self.day} (UTC) {self.bot_id}'s {self.model} calls cost "
                f"${self.truth_usd:.2f} by OpenClaw's own accounting; Evolve's price "
                f"table priced the same tokens at ${self.other_usd:.2f} "
                f"(ratio {self.ratio:.2f}).{dim} The caps read OpenClaw's figure, "
                f"so today's spend is right — but projections, the fallback for "
                f"any call OpenClaw cannot price, and the console reconcile run "
                f"on the table, and it is wrong for this model."
            )
        return (
            f"For {self.day} OpenClaw's per-call total was ${self.other_usd:.2f} "
            f"and the provider console billed ${self.truth_usd:.2f} "
            f"(ratio {self.ratio:.2f}).{dim} Every cost control reads "
            f"OpenClaw's figure, so it is off the bill by that factor."
        )


def _worst_dimension(dims: Mapping[str, Mapping[str, float | None]], truth: str, other: str):
    best = ("total", None, None, -1.0)
    for dim, vals in dims.items():
        t, o = vals.get(truth), vals.get(other)
        if t is None or o is None:
            continue
        gap = abs(float(o) - float(t))
        if gap > best[3]:
            best = (dim, float(t), float(o), gap)
    return best[:3]


def _out_of_band(truth: float, other: float) -> bool:
    return truth > 0 and abs(other / truth - 1.0) > DRIFT_TOLERANCE


def estimate_vs_oc(
    bot_id: str, calls: Iterable, *, day: str, catalog: dict | None,
) -> list[DriftFinding]:
    """Per-model findings for one bot-day of OC calls."""
    from oc_usage_cost import DIMENSIONS, oc_dimension_costs
    from turn_cost import estimate_dimensions

    per_model: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: {d: {"oc": 0.0, "estimate": 0.0} for d in DIMENSIONS},
    )
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for c in calls:
        if not c.priced_by_oc:
            continue
        est = estimate_dimensions(c.as_turn(), catalog=catalog)
        if est is None:
            continue  # unpriceable by the table: nothing to compare
        model = c.model or "unknown"
        for d, usd in oc_dimension_costs(c).items():
            per_model[model][d]["oc"] += usd
        for d, usd in est.items():
            per_model[model][d]["estimate"] += usd
        totals[model][0] += float(c.cost or 0.0)
        totals[model][1] += sum(est.values())
    out: list[DriftFinding] = []
    for model, (oc_usd, est_usd) in sorted(totals.items()):
        if oc_usd < MIN_JUDGED_USD or not _out_of_band(oc_usd, est_usd):
            continue
        dims = per_model[model]
        dim, t, o = _worst_dimension(dims, "oc", "estimate")
        out.append(DriftFinding(
            kind="estimate_vs_oc", day=day, model=model, bot_id=bot_id,
            truth_usd=round(oc_usd, 6), other_usd=round(est_usd, 6),
            dimension=dim, dimension_truth_usd=t, dimension_other_usd=o,
            by_dimension={k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in dims.items()},
        ))
    return out


def oc_vs_console(doc: dict | None) -> list[DriftFinding]:
    """A finding when the latest reconcile puts OC's total off the bill."""
    if not doc:
        return []
    day_doc = (doc.get("days") or [{}])[0]
    oc = day_doc.get("oc_usd", doc.get("oc_usd"))
    console = day_doc.get("console_usd", doc.get("console_usd"))
    if oc is None or not console or not _out_of_band(float(console), float(oc)):
        return []
    dims = day_doc.get("dimensions") or {}
    dim, t, o = _worst_dimension(dims, "console", "oc")
    return [DriftFinding(
        kind="oc_vs_console", day=str(day_doc.get("date") or "?"),
        model=str(doc.get("provider") or "all"),
        bot_id=doc.get("bot_id"), truth_usd=float(console), other_usd=float(oc),
        dimension=dim, dimension_truth_usd=t, dimension_other_usd=o,
        by_dimension=dims,
    )]


@dataclass(frozen=True)
class DayCheck:
    """One day's findings plus what the check could actually read.

    ``read_bots`` are the members whose loader returned a list (possibly
    empty: a readable DB with no calls is a real "nothing to judge");
    ``console_read`` is whether a non-empty reconcile document was present.
    :func:`emit` resolves Signals only within this coverage, so an
    unreadable DB or a missing reconcile never clears a firing alarm.
    """

    findings: list[DriftFinding]
    read_bots: frozenset[str] = frozenset()
    console_read: bool = False


def check_day(
    shared_dir: Path,
    members: Iterable[str],
    day: date,
    *,
    load_calls: Callable[..., list | None] | None = None,
    reconcile_doc: dict | None = None,
) -> DayCheck:
    """Every finding for UTC ``day`` across ``members`` plus the console."""
    from oc_usage_cost import load_oc_calls
    from turn_cost import load_pricing_catalog

    loader = load_calls or load_oc_calls
    catalog = load_pricing_catalog(shared_dir)
    start = datetime.combine(day, dt_time(0, 0), tzinfo=timezone.utc)
    findings: list[DriftFinding] = []
    read_bots: set[str] = set()
    for bot_id in members:
        try:
            calls = loader(bot_id, start, start + timedelta(days=1))
        except Exception:  # noqa: BLE001 — one unreadable bot never sinks the check
            calls = None
        if calls is None:
            continue  # unreadable: not "in band" — emit leaves its Signals alone
        read_bots.add(bot_id)
        if calls:
            findings.extend(estimate_vs_oc(bot_id, calls, day=day.isoformat(), catalog=catalog))
    if reconcile_doc is None:
        try:
            from cost_reconcile import latest_reconcile
            reconcile_doc = latest_reconcile(shared_dir)
        except Exception:  # noqa: BLE001
            reconcile_doc = None
    findings.extend(oc_vs_console(reconcile_doc))
    return DayCheck(
        findings=findings,
        read_bots=frozenset(read_bots),
        console_read=bool(reconcile_doc),
    )


def _signature_kind(signature: str) -> str | None:
    prefix = f"{PRODUCER}:{SIGNAL_TYPE}:"
    if not signature.startswith(prefix):
        return None
    return signature[len(prefix):].split(":", 1)[0]


def emit(shared_dir: Path, check: DayCheck) -> set[str]:
    """Observe one firing Signal per finding; resolve the ones that cleared.

    Resolution is bounded by what ``check`` read: ``estimate_vs_oc`` Signals
    are swept only for ``check.read_bots``, ``oc_vs_console`` Signals only
    when ``check.console_read``. Everything outside that stays as it was.
    """
    from signals import store as signals_store  # type: ignore[import]

    kept: set[str] = set()
    for f in check.findings:
        signals_store.observe(
            shared_dir,
            signature=f.signature,
            producer=PRODUCER,
            type=SIGNAL_TYPE,
            flavor="activity",
            severity="warn",
            scope="bot" if f.bot_id else "pod",
            bot_id=f.bot_id,
            title=f.title(),
            body=f.body(),
            details={
                "kind": f.kind,
                "day": f.day,
                "model": f.model,
                "dimension": f.dimension,
                "truth_usd": f.truth_usd,
                "other_usd": f.other_usd,
                "ratio": f.ratio,
                "by_dimension": f.by_dimension,
                "vector": "cost",
                "fix_steps": (
                    "1. Check {shared_dir}/model-pricing.json has a row for this "
                    "model with every rate (including cache_write_1h_cost_per_token).\n"
                    "2. Reconcile against the console export:\n"
                    "   sudo evolve-admin cost reconcile --console-csv costs.csv "
                    "--provider <provider>"
                ),
            },
        )
        kept.add(f.signature)
    # Active drift Signals by kind, so each sweep can shield the other kind
    # (sweep_resolve filters by type and bot, not by the kind we encode).
    active_by_kind: dict[str | None, set[str]] = defaultdict(set)
    for sig in signals_store.iter_signals(shared_dir):
        if sig.producer == PRODUCER and sig.type == SIGNAL_TYPE:
            active_by_kind[_signature_kind(sig.signature)].add(sig.signature)
    shield_all_but = lambda kind: set().union(  # noqa: E731
        *(sigs for k, sigs in active_by_kind.items() if k != kind),
    )
    if check.read_bots:
        signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            types={SIGNAL_TYPE},
            bot_ids=set(check.read_bots),
            kept_signatures=kept | shield_all_but("estimate_vs_oc"),
            reason="auto-resolve: Evolve's estimate back within 10% of OpenClaw's cost",
        )
    if check.console_read:
        signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            types={SIGNAL_TYPE},
            bot_ids=None,
            kept_signatures=kept | shield_all_but("oc_vs_console"),
            reason="auto-resolve: OpenClaw's cost back within 10% of the provider bill",
        )
    return kept


def maybe_run_daily(
    shared_dir: Path,
    members: Iterable[str],
    now: datetime | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> list[DriftFinding] | None:
    """Run :func:`check_day` for yesterday (UTC), once per day. Never raises.

    ``None`` = already ran today, or the check itself failed (logged).
    The day's flag is written only when at least one member's transcript DB
    was readable; otherwise the check retries on a later tick.
    """
    emit_log = log or (lambda _m: None)
    now = now or datetime.now(timezone.utc)
    day = (now.astimezone(timezone.utc) - timedelta(days=1)).date()
    flag = Path(shared_dir) / "alerts" / f"cost-truth-{day.isoformat()}.flag"
    if flag.exists():
        return None
    try:
        check = check_day(shared_dir, list(members), day)
        emit(shared_dir, check)
    except Exception as exc:  # noqa: BLE001 — the spend daemon must keep ticking
        emit_log(f"[cost_truth] drift check for {day} failed: {exc}")
        return None
    findings = check.findings
    if not check.read_bots:
        # Log once per day, not once per tick: the retry marker is only a
        # log throttle — the check itself reruns every tick until a DB reads.
        retry = flag.with_suffix(".retry")
        if not retry.exists():
            emit_log(f"[cost_truth] no member's OpenClaw DB readable for {day}; will retry")
            try:
                retry.parent.mkdir(parents=True, exist_ok=True)
                retry.write_text("")
            except OSError as exc:
                emit_log(f"[cost_truth] could not write {retry}: {exc}")
        return findings
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"{len(findings)}\n")
    except OSError as exc:
        emit_log(f"[cost_truth] could not write {flag}: {exc}")
    for f in findings:
        emit_log(f"[cost_truth] drift: {f.title()}")
    return findings
