"""cost_reconcile — check the pod's cost estimate against the provider's bill.

Every cost surface Evolve shows is an ESTIMATE: token counts multiplied by a
published rate. Nothing in the pod had ever compared that estimate to the
figure the provider actually billed, which is how the family-substring price
table overstated the power model ~3x for months without anyone noticing —
$32.09 estimated against $11.08 billed for UTC 2026-09-04 on the PoC bot
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §1). The price
rule is fixed in ``turn_cost`` / the plugin's ``ModelPricing``; this module is
the standing check that keeps it honest.

The operator surface is::

    sudo evolve-admin cost reconcile --console-csv <file> [--provider <id>] [--bot <id>]

``<file>`` is the daily cost export downloaded from the provider console's
Cost page — a CSV with a date column, a model column and a cost column. For
every date it names, the reconcile prints our estimate, the console figure,
the delta and the ratio, and writes ``{shared_dir}/cost/reconcile-<date>.json``
so the weekly receipt can cite it. A ratio outside
:data:`DRIFT_LOW`–:data:`DRIFT_HIGH` is drift the operator should hear about;
``spend_alert``'s weekly summary raises it as the ``cost_estimate_drift``
Signal and the ``cost.estimate_drift`` digest event.

Deliberately NOT here: any network call. The console export is a file the
operator downloads and hands us. A reconciliation that fetched its own truth
would need a billing credential on the pod, which is a much larger blast
radius than the number it would check.

One provider, deliberately
--------------------------
The export is ONE provider's bill, so the estimate side is scoped to the same
provider (``--provider``, inferred from the model column when the catalog
resolves it unambiguously). A pod that also ran xAI that week would otherwise
compare (anthropic + xai) against an anthropic-only bill, and the ratio — the
whole output of this module — would move with the model mix rather than with
the prices it exists to check (PR #4038 review, F1). Turns from other
providers are reported as an excluded count and dollar figure, never dropped
silently.

The UTC day, deliberately
-------------------------
Turn files are UTC-named and every record's ``ts`` is a ``Z`` instant; the
provider console bills by UTC day. So this module buckets on the turn's UTC
date — the one place in the cost layer where a ``ts[:10]`` prefix is the
RIGHT key rather than the pod-local bug ``live_spend`` warns about. Caps and
receipts stay pod-local; a reconciliation against a UTC bill does not.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from turn_cost import (
    GUESS_RESOLUTIONS, load_pricing_catalog, turn_cost_detail, turn_provider,
)

_log = logging.getLogger(__name__)

#: Ratio band (estimate / console) treated as agreement. Outside it, the
#: estimate is drifting from the bill and the operator is told.
DRIFT_LOW = 0.9
DRIFT_HIGH = 1.1

#: A catalog older than this is called out on the receipt: prices move, and a
#: stale mirror prices a new model at whatever it cost last quarter.
CATALOG_STALE_DAYS = 7

#: Column headers accepted from the console export, lowercased. Providers
#: spell these differently and rename them between exports; matching a small
#: set of spellings beats failing on a header nobody can control.
_DATE_HEADERS = ("date", "day", "usage_date", "invoice_date", "start_date")
_COST_HEADERS = ("cost", "cost_usd", "amount", "amount_usd", "usd", "total")
_MODEL_HEADERS = ("model", "model_id", "sku", "line_item", "description")


class ConsoleCsvError(ValueError):
    """The console export could not be read as a date/model/cost table."""


class ProviderScopeError(ValueError):
    """The export's provider is unknown and the caller named none.

    A console export is ONE provider's bill. Comparing it against an estimate
    that sums every provider's turns is not a measurement of pricing accuracy
    — on a bot that also ran xAI that week the ratio moves for a reason that
    has nothing to do with the rates being checked (PR #4038 review, F1). So
    the scope is required: inferred from the export's model column when the
    catalog resolves it unambiguously, and otherwise asked for.
    """


# ── The console export ────────────────────────────────────────────────────────


def _pick_header(fieldnames: Sequence[str], candidates: Sequence[str]) -> str | None:
    lowered = {(f or "").strip().lower(): f for f in fieldnames}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    # Fall back to a header that CONTAINS a candidate ("Cost (USD)").
    for cand in candidates:
        for low, original in lowered.items():
            if cand in low:
                return original
    return None


def _parse_date_cell(raw: str) -> str | None:
    """A console date cell → ``YYYY-MM-DD``, or ``None`` when it isn't one."""
    text = (raw or "").strip()
    if not text:
        return None
    # An ISO timestamp is common in these exports; the day is its prefix.
    head = text.split("T")[0].split(" ")[0]
    try:
        return date.fromisoformat(head).isoformat()
    except ValueError:
        return None


def _parse_cost_cell(raw: str) -> float | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_console_rows(path: Path | str) -> tuple[dict[str, float], list[str]]:
    """``({YYYY-MM-DD: usd}, [model cell, ...])`` from a console cost export.

    Both halves come out of ONE pass, because the model column is not
    decoration: it is what says whose bill this is, and reading the file twice
    to answer that would let the two answers come from different bytes.

    Rows are summed per date across every model line. Rows whose date or cost
    cell will not parse are skipped — an export routinely carries a totals
    row and a blank tail, and refusing the whole file over them would make
    the tool unusable. A file with no usable row at all raises
    :class:`ConsoleCsvError`: that is a wrong file, not a quiet zero.
    """
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as exc:
        raise ConsoleCsvError(f"cannot read {p}: {exc}") from exc
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ConsoleCsvError(f"{p} has no header row")
    date_col = _pick_header(reader.fieldnames, _DATE_HEADERS)
    cost_col = _pick_header(reader.fieldnames, _COST_HEADERS)
    model_col = _pick_header(reader.fieldnames, _MODEL_HEADERS)
    if date_col is None or cost_col is None:
        raise ConsoleCsvError(
            f"{p} needs a date column and a cost column; found "
            f"{list(reader.fieldnames)}"
        )
    out: dict[str, float] = {}
    models: list[str] = []
    for row in reader:
        day = _parse_date_cell(row.get(date_col) or "")
        usd = _parse_cost_cell(row.get(cost_col) or "")
        if day is None or usd is None:
            continue
        out[day] = round(out.get(day, 0.0) + usd, 6)
        cell = ((row.get(model_col) or "") if model_col else "").strip()
        if cell and cell not in models:
            models.append(cell)
    if not out:
        raise ConsoleCsvError(
            f"{p} parsed no date/cost rows — is it the console's cost export?"
        )
    return out, models


def parse_console_csv(path: Path | str) -> dict[str, float]:
    """``{YYYY-MM-DD: usd}`` from a provider console cost export."""
    return _read_console_rows(path)[0]


def console_models(path: Path | str) -> list[str]:
    """The distinct model cells the export names, in file order.

    Empty when the export has no model column at all — a real shape for some
    consoles, and the reason :func:`infer_provider` can return ``None`` rather
    than guessing from a column that is not there.
    """
    return _read_console_rows(path)[1]


def _catalog_providers_for(catalog: dict | None, model_cell: str) -> set[str]:
    """Providers the catalog knows a row for under ``model_cell``'s model id.

    The console spells its model column as its own id (``claude-opus-5``), so
    this is a REVERSE lookup — id → provider(s) — which ``model_pricing`` has
    no index for. A cell that names no model the catalog knows contributes
    nothing; two providers publishing the same id contribute both, and the
    caller then refuses to infer.
    """
    if not catalog:
        return set()
    bare = model_cell.strip().lower().rsplit("/", 1)[-1]
    if not bare:
        return set()
    found: set[str] = set()
    for rec in catalog.get("models") or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("model_id") or "").strip().lower() != bare:
            continue
        provider = str(rec.get("provider") or "").strip().lower()
        if provider:
            found.add(provider)
    return found


def infer_provider(models: Iterable[str], catalog: dict | None) -> str | None:
    """The one provider an export's model column names, or ``None``.

    ``None`` for both failure shapes — nothing in the column resolved, or the
    column names models from more than one provider — because they lead to the
    same place: the caller must be told the scope rather than pick one. A
    catalog that cannot resolve a model id is the common case on a pod whose
    discovery sweep has not run, which is exactly when an inferred scope would
    be least trustworthy.
    """
    providers: set[str] = set()
    for cell in models:
        providers |= _catalog_providers_for(catalog, cell)
        if len(providers) > 1:
            return None
    return next(iter(providers)) if len(providers) == 1 else None


# ── Our side of the comparison ────────────────────────────────────────────────


@dataclass(frozen=True)
class DayReconcile:
    """One UTC day, both readings of it."""

    date_iso: str
    estimate_usd: float
    console_usd: float
    priced_turns: int = 0
    unpriced_turns: int = 0
    repriced_turns: int = 0
    #: Turns dropped because they ran on a DIFFERENT provider than the export
    #: bills. Counted and costed rather than silently omitted: the operator
    #: has to be able to see that the estimate side was narrowed, and by how
    #: much, or a scoped comparison is just an unexplained smaller number.
    excluded_turns: int = 0
    excluded_usd: float = 0.0
    #: Turns inside the scope priced at ``turn_cost``'s provider-level rung —
    #: a rate for some other model of this provider. They ARE in
    #: ``estimate_usd``; the count says how much of it is a guess.
    provider_guess_turns: int = 0

    @property
    def delta_usd(self) -> float:
        return round(self.estimate_usd - self.console_usd, 6)

    @property
    def turns_seen(self) -> int:
        return self.priced_turns + self.unpriced_turns

    @property
    def ratio(self) -> float | None:
        """estimate / console, or ``None`` when there is nothing to compare.

        ``None`` is not 0 and not 1, and the difference matters twice over:

          * the console billed nothing that day — it cannot confirm or refute
            an estimate;
          * we read no turns for that day at all — a $0.00 estimate then means
            "did not measure", and a ratio of 0.00 would present that as an
            infinite under-estimate and raise drift on it. That is exactly the
            silent zero ``docs/principle-tri-state-status.md`` forbids.
        """
        if not self.console_usd or self.turns_seen == 0:
            return None
        return round(self.estimate_usd / self.console_usd, 4)

    @property
    def measurable(self) -> bool:
        """True when every turn in the day could be priced. When False the
        estimate is a floor, so a low ratio may be missing turns rather than
        a wrong rate."""
        return self.unpriced_turns == 0

    def to_dict(self) -> dict:
        return {
            "date": self.date_iso,
            "estimate_usd": round(self.estimate_usd, 6),
            "console_usd": round(self.console_usd, 6),
            "delta_usd": self.delta_usd,
            "ratio": self.ratio,
            "priced_turns": self.priced_turns,
            "unpriced_turns": self.unpriced_turns,
            "repriced_turns": self.repriced_turns,
            "excluded_turns": self.excluded_turns,
            "excluded_usd": round(self.excluded_usd, 6),
            "provider_guess_turns": self.provider_guess_turns,
            "measurable": self.measurable,
            "turns_seen": self.turns_seen,
        }


@dataclass(frozen=True)
class ReconcileResult:
    """A whole console export, reconciled."""

    days: tuple[DayReconcile, ...] = ()
    bot_id: str | None = None
    provider: str = ""
    generated_at: str = ""
    console_csv: str = ""
    catalog_refreshed_at: str | None = None

    @property
    def estimate_usd(self) -> float:
        return round(sum(d.estimate_usd for d in self.days), 6)

    @property
    def console_usd(self) -> float:
        return round(sum(d.console_usd for d in self.days), 6)

    @property
    def turns_seen(self) -> int:
        return sum(d.turns_seen for d in self.days)

    @property
    def excluded_turns(self) -> int:
        return sum(d.excluded_turns for d in self.days)

    @property
    def excluded_usd(self) -> float:
        return round(sum(d.excluded_usd for d in self.days), 6)

    @property
    def provider_guess_turns(self) -> int:
        return sum(d.provider_guess_turns for d in self.days)

    @property
    def ratio(self) -> float | None:
        """See :attr:`DayReconcile.ratio` — ``None`` when there is nothing to
        compare, never a 0.00 standing in for "did not measure"."""
        if not self.console_usd or self.turns_seen == 0:
            return None
        return round(self.estimate_usd / self.console_usd, 4)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "bot_id": self.bot_id,
            "provider": self.provider,
            "console_csv": self.console_csv,
            "catalog_refreshed_at": self.catalog_refreshed_at,
            "estimate_usd": self.estimate_usd,
            "console_usd": self.console_usd,
            "ratio": self.ratio,
            "excluded_turns": self.excluded_turns,
            "excluded_usd": self.excluded_usd,
            "provider_guess_turns": self.provider_guess_turns,
            "days": [d.to_dict() for d in self.days],
        }


def _turn_utc_day(turn: dict) -> str | None:
    ts = (turn.get("ts") or "").strip()
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _default_load_turns(
    bot_id: str | None, *, days: int, end: datetime,
    network_path: Path | str | None = None,
) -> list[dict]:
    from usage_analytics import load_turns  # type: ignore[import]
    return load_turns(
        bot_id, days=days, end_date=end,
        network_path=str(network_path) if network_path else None,
    )


@dataclass
class DayEstimate:
    """Our estimate for one UTC day, with the tri-state counts beside it.

    ``unpriced`` turns are counted, never summed as zero — the same contract
    ``TurnCostTotal`` keeps, so a day with unpriced turns reads as a floor
    rather than as a low ratio the operator would chase as drift.

    ``excluded`` turns are a different absence: they were priced fine, they
    just belong to another provider's bill. They are kept with their dollars
    so the reconcile can show what the scope left out.
    """

    usd: float = 0.0
    priced: int = 0
    unpriced: int = 0
    repriced: int = 0
    excluded: int = 0
    excluded_usd: float = 0.0
    provider_guesses: int = 0


def estimate_by_utc_day(
    days_wanted: Iterable[str],
    *,
    provider: str,
    bot_id: str | None = None,
    shared_dir: Path | str | None = None,
    network_path: Path | str | None = None,
    load_turns: Callable[..., list[dict]] | None = None,
    turns: Iterable[dict] | None = None,
) -> dict[str, DayEstimate]:
    """Our own estimate for each UTC day in ``days_wanted``, ONE provider only.

    ``provider`` is required and is compared against ``turn_cost.turn_provider``
    for every turn: a console export is one provider's bill, so summing every
    provider's turns against it measures the pod's model mix, not its pricing
    accuracy (PR #4038 review, F1). Turns from other providers are counted and
    costed into ``excluded`` / ``excluded_usd`` rather than dropped in silence.

    ``turns`` short-circuits the disk read (tests, and a caller that already
    holds the records). Otherwise the window is loaded from the turn JSONL,
    widened by one file at each end so a day at the edge of the export is not
    read from a file that was still being appended to.
    """
    wanted = sorted({d for d in days_wanted if d})
    buckets: dict[str, DayEstimate] = {d: DayEstimate() for d in wanted}
    if not wanted:
        return buckets

    if turns is None:
        first = date.fromisoformat(wanted[0])
        last = date.fromisoformat(wanted[-1])
        end = datetime.combine(
            last + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
        )
        span = (last - first).days + 2
        loader = load_turns or _default_load_turns
        turns = loader(bot_id, days=span, end=end, network_path=network_path)

    want_provider = (provider or "").strip().lower()
    catalog = load_pricing_catalog(shared_dir)
    for turn in turns:
        bucket = buckets.get(_turn_utc_day(turn) or "")
        if bucket is None:
            continue
        cost, resolution = turn_cost_detail(turn, catalog=catalog)
        if turn_provider(turn).strip().lower() != want_provider:
            # Another provider's turn. It is not part of THIS bill, but the
            # operator still needs the size of what the scope removed.
            bucket.excluded += 1
            if cost is not None:
                bucket.excluded_usd = round(bucket.excluded_usd + cost, 6)
            continue
        if cost is None:
            bucket.unpriced += 1
            continue
        bucket.usd = round(bucket.usd + cost, 6)
        bucket.priced += 1
        if resolution in GUESS_RESOLUTIONS:
            bucket.provider_guesses += 1
        # A pre-fix record the catalog corrected on read: worth reporting,
        # because the file on disk and this total deliberately disagree.
        try:
            recorded = float(turn.get("cost") or 0)
        except (TypeError, ValueError):
            recorded = 0.0
        if resolution == "catalog" and recorded and abs(recorded - cost) > 1e-9:
            bucket.repriced += 1
    return buckets


def resolve_provider(
    models: Iterable[str],
    *,
    provider: str | None = None,
    shared_dir: Path | str | None = None,
    catalog: dict | None = None,
) -> str:
    """The provider to scope this reconcile to, or raise.

    An explicit ``provider`` always wins — the operator can see the export we
    cannot parse. Otherwise the model column is put to the catalog, and a
    single unambiguous answer is used. Anything else raises
    :class:`ProviderScopeError` with the models it could not place: guessing
    here would silently pick which bill we are checking against.
    """
    named = (provider or "").strip().lower()
    if named:
        return named
    cat = catalog if catalog is not None else load_pricing_catalog(shared_dir)
    seen = list(models)
    inferred = infer_provider(seen, cat)
    if inferred:
        return inferred
    shown = ", ".join(seen[:5]) if seen else "(the export has no model column)"
    raise ProviderScopeError(
        "cannot tell which provider this export bills — pass --provider. "
        f"The catalog placed none of its models unambiguously: {shown}. "
        "A console export is one provider's bill; comparing it against every "
        "provider's turns would move the ratio for reasons unrelated to price."
    )


def reconcile(
    *,
    console_csv: Path | str,
    shared_dir: Path | str,
    provider: str | None = None,
    bot_id: str | None = None,
    network_path: Path | str | None = None,
    load_turns: Callable[..., list[dict]] | None = None,
    turns: Iterable[dict] | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Compare our estimate to the console export, day by day, ONE provider.

    ``provider`` scopes the estimate side to the same bill the export is. When
    omitted it is inferred from the export's model column via the pod's pricing
    catalog; when that is ambiguous the call raises
    :class:`ProviderScopeError` rather than reconciling against a mixed total.
    """
    console, models = _read_console_rows(console_csv)
    scope = resolve_provider(models, provider=provider, shared_dir=shared_dir)
    buckets = estimate_by_utc_day(
        console.keys(), provider=scope, bot_id=bot_id, shared_dir=shared_dir,
        network_path=network_path, load_turns=load_turns, turns=turns,
    )
    days = tuple(
        DayReconcile(
            date_iso=day,
            estimate_usd=round(buckets[day].usd, 6),
            console_usd=round(console[day], 6),
            priced_turns=buckets[day].priced,
            unpriced_turns=buckets[day].unpriced,
            repriced_turns=buckets[day].repriced,
            excluded_turns=buckets[day].excluded,
            excluded_usd=round(buckets[day].excluded_usd, 6),
            provider_guess_turns=buckets[day].provider_guesses,
        )
        for day in sorted(console)
    )
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    catalog = load_pricing_catalog(shared_dir) or {}
    refreshed = catalog.get("refreshed_at")
    return ReconcileResult(
        days=days,
        bot_id=bot_id,
        provider=scope,
        generated_at=stamp.isoformat(),
        console_csv=str(console_csv),
        catalog_refreshed_at=refreshed if isinstance(refreshed, str) else None,
    )


# ── Persistence ───────────────────────────────────────────────────────────────


def reconcile_dir(shared_dir: Path | str) -> Path:
    return Path(shared_dir) / "cost"


def reconcile_path(shared_dir: Path | str, date_iso: str) -> Path:
    return reconcile_dir(shared_dir) / f"reconcile-{date_iso}.json"


def _inherit_owner(path: Path, source: Path) -> None:
    """Give ``path`` the owner of ``source``.

    ``evolve-admin cost reconcile`` runs under sudo, so every file it creates
    is root's by default — and a root-owned file inside ``{shared_dir}`` is
    the shape that has locked the evolve daemon out of a shared directory
    before now. Cheap insurance, and a no-op when we are not root.

    Failure is logged, not raised: the reconcile document is already written
    and useful, and an unwritable owner bit is a permissions question for the
    operator rather than a reason to lose the comparison.
    """
    try:
        st = source.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except (OSError, AttributeError) as exc:
        _log.debug("cost_reconcile: could not chown %s to %s's owner: %s",
                   path, source, exc)


def write_reconcile(shared_dir: Path | str, result: ReconcileResult) -> list[Path]:
    """Persist one JSON file per reconciled day. Returns the paths written."""
    base = Path(shared_dir)
    out_dir = reconcile_dir(base)
    created = not out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    if created:
        _inherit_owner(out_dir, base)
    written: list[Path] = []
    for day in result.days:
        doc = result.to_dict()
        doc["days"] = [day.to_dict()]
        doc["estimate_usd"] = day.estimate_usd
        doc["console_usd"] = day.console_usd
        doc["ratio"] = day.ratio
        path = reconcile_path(base, day.date_iso)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
        os.replace(tmp, path)
        _inherit_owner(path, base)
        written.append(path)
    return written


def read_reconcile(shared_dir: Path | str, date_iso: str) -> dict | None:
    path = reconcile_path(shared_dir, date_iso)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _generated_on(doc: dict, fallback_day: str) -> date | None:
    """The date a reconcile document was PRODUCED, not the day it reconciled.

    Falls back to the reconciled day for a document with no usable
    ``generated_at`` — an older file, or one an operator hand-edited. That is
    the pre-2026-09-05 behaviour, which is conservative in the right
    direction: it can only make a document look older than it is.
    """
    raw = doc.get("generated_at")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc).date()
    try:
        return date.fromisoformat(fallback_day)
    except ValueError:
        return None


def iter_reconciles(shared_dir: Path | str) -> list[tuple[str, dict]]:
    """``[(reconciled day, document), ...]`` for every reconcile on disk."""
    out: list[tuple[str, dict]] = []
    try:
        paths = sorted(reconcile_dir(shared_dir).glob("reconcile-*.json"))
    except OSError:
        return out
    for path in paths:
        day = path.stem[len("reconcile-"):]
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict):
            out.append((day, doc))
    return out


def latest_reconcile(
    shared_dir: Path | str, *, within_days: int = 7, today: date | None = None,
) -> dict | None:
    """The most recently PRODUCED reconcile inside the window, or ``None``.

    The window is on ``generated_at`` — when the operator ran the reconcile —
    not on the day it reconciled. Walking the filenames instead (the original
    shape) meant a Wednesday reconcile of last month's export did not count as
    "reconciled this week", so the receipt said nothing had been checked on
    the very week it was, and the drift warning waited for a reconcile of a
    recent DAY rather than a recent RUN (PR #4038 review, F4).

    ``None`` means "not reconciled recently" — a state the receipt reports as
    such. It is never rendered as agreement.
    """
    end = today or datetime.now(timezone.utc).date()
    oldest = end - timedelta(days=max(within_days, 1) - 1)
    best: tuple[date, str, dict] | None = None
    for day, doc in iter_reconciles(shared_dir):
        produced = _generated_on(doc, day)
        # Only the LOWER bound is enforced. A stamp ahead of ``end`` is clock
        # skew (the operator's laptop, a pod whose time drifted), and dropping
        # it would make a reconcile that was genuinely run disappear from the
        # receipt — the "checked and fine" silence this module exists to end.
        if produced is None or produced < oldest:
            continue
        # Newest run wins; a run that reconciled several days is one document
        # per day, so the reconciled day breaks the tie deterministically.
        if best is None or (produced, day) > (best[0], best[1]):
            best = (produced, day, doc)
    return None if best is None else best[2]


# ── Drift + the receipt lines ─────────────────────────────────────────────────


def ratio_out_of_band(ratio: float | None) -> bool:
    """True when a ratio is far enough from 1.0 to be worth telling the
    operator. ``None`` (nothing billed) is not drift — it is no measurement."""
    if ratio is None:
        return False
    return ratio < DRIFT_LOW or ratio > DRIFT_HIGH


def catalog_age_days(
    shared_dir: Path | str, *, now: datetime | None = None,
) -> float | None:
    """Age of the mirrored pricing catalog in days, or ``None`` when the pod
    has no catalog (or one with no ``refreshed_at``)."""
    catalog = load_pricing_catalog(shared_dir)
    raw = (catalog or {}).get("refreshed_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        refreshed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return round((stamp - refreshed).total_seconds() / 86400.0, 2)


def receipt_lines(
    shared_dir: Path | str, *, now: datetime | None = None, within_days: int = 7,
) -> list[str]:
    """The lines the weekly receipt appends about pricing accuracy.

    Always at least one: a receipt that silently omits the comparison reads
    as "checked and fine", which is exactly the confusion the reconcile
    exists to end.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    doc = latest_reconcile(shared_dir, within_days=within_days, today=stamp.date())
    lines: list[str] = []
    if not doc:
        lines.append("estimate vs provider bill: not reconciled this week")
    else:
        ratio = doc.get("ratio")
        day = (doc.get("days") or [{}])[0].get("date") or "?"
        if ratio is None:
            reason = (
                "no turns read for that day"
                if not (doc.get("days") or [{}])[0].get("turns_seen")
                else "the console billed nothing that day"
            )
            lines.append(
                f"estimate vs provider bill: nothing to compare for {day} — "
                f"{reason}"
            )
        else:
            scope = str(doc.get("provider") or "").strip()
            tail = f"{day}, {scope}" if scope else day
            lines.append(
                f"estimate vs provider bill: "
                f"${float(doc.get('estimate_usd') or 0):.2f} vs "
                f"${float(doc.get('console_usd') or 0):.2f} (ratio {ratio:.2f}, {tail})"
            )
        lines.extend(_scope_lines(doc))
    age = catalog_age_days(shared_dir, now=stamp)
    if age is None:
        lines.append("pricing catalog: none mirrored on this pod")
    elif age > CATALOG_STALE_DAYS:
        # Whole days, floored: "35 days old" for an age of 35.5 rather than
        # a rounded-up 36 the operator cannot reconcile with the file's date.
        lines.append(
            f"pricing catalog: {int(age)} days old — prices may be stale"
        )
    return lines


def _scope_lines(doc: dict) -> list[str]:
    """What the scoped comparison left out, and how much of it is a guess.

    Both are silence-shaped failures otherwise: a smaller estimate with no
    reason given reads as the pod having spent less, and a provider-level
    guess folded into the total reads as a published price.
    """
    out: list[str] = []
    day = (doc.get("days") or [{}])[0]
    excluded = int(day.get("excluded_turns") or doc.get("excluded_turns") or 0)
    if excluded:
        usd = float(day.get("excluded_usd") or doc.get("excluded_usd") or 0)
        turn_word = "turn" if excluded == 1 else "turns"
        out.append(
            f"  excluded from the comparison: {excluded} {turn_word} on other "
            f"providers (~${usd:.2f}) — a different bill"
        )
    guesses = int(
        day.get("provider_guess_turns") or doc.get("provider_guess_turns") or 0
    )
    if guesses:
        from turn_cost import provider_guess_note  # type: ignore[import]
        note = provider_guess_note(guesses, [str(doc.get("provider") or "")])
        if note:
            out.append(f"  {note}")
    return out


def drift_signal_body(doc: dict) -> tuple[str, str]:
    """``(title, body)`` for the drift Signal, from a reconcile document."""
    ratio = doc.get("ratio")
    est = float(doc.get("estimate_usd") or 0)
    console = float(doc.get("console_usd") or 0)
    day = (doc.get("days") or [{}])[0].get("date") or "?"
    scope = str(doc.get("provider") or "").strip()
    whose = f"{scope} " if scope else ""
    direction = "over" if (ratio or 0) > 1 else "under"
    title = f"Cost estimate is {direction} the provider bill (ratio {ratio})"
    body = (
        f"For {day} Evolve estimated ${est:.2f} for {whose}turns and the "
        f"provider console billed ${console:.2f} — a ratio of {ratio}, outside the "
        f"{DRIFT_LOW}–{DRIFT_HIGH} band. Every cost surface (the daily cap, "
        f"the 80% warning, the checkpoint message, this receipt) runs on the "
        f"estimate, so it is {direction}stating spend by roughly that factor "
        f"until the prices behind it are corrected."
    )
    return title, body
