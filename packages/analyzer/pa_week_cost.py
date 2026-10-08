"""pa_week_cost — the cost number for a PA week, per bot per day.

Brief ``pa-week-cost-is-measured`` (program-map-2026-09-27 §8). One question:
does a week at the Power default fit inside the subscription's programmatic
pool, and what does it cost at API rates? This module is the instrument; the
finding (``internal/finding-pa-week-cost-2026-10.md``) is the reading.

Model-free, read-only: a rollup over the turn rows the pod already writes.
Nothing here chooses which model answers a turn.

The daily row, per bot per pod-local day:

* ``total_usd`` — every priced turn, OC's per-call ``usage.cost`` where it
  exists (cost truth, #4492); ``cost_source`` says how much of it is OC's.
* ``overhead`` — Evolve's share (D-OH5), taken from
  :mod:`evolve_overhead` so there is one definition of the number.
* ``tiers`` — turns and dollars by the tier that answered (fast / standard /
  power; anything else is ``other``, a row with no tier is ``unknown`` — never
  guessed into a tier). Evolve's own calls and OC housekeeping are NOT tier
  rows: they are not what answered the person.
* ``housekeeping`` — flush / compaction / capture calls, apart.
* ``largest_turn`` — the day's single most expensive turn.
* ``standard_default_estimate_usd`` — what the day would have cost had every
  Power-tier answering turn run on the bot's standard rung instead, repriced
  from that turn's own token counts. An ESTIMATE, labelled, and it can only be
  computed when the standard rung's model is known and priceable
  (``estimate_unpriced_turns`` counts the turns it could not reprice; those
  are kept at their actual cost, so the estimate is an upper bound).

The operator's reading (:func:`set_reading`) is one optional record per day:
the programmatic-credit pool used / ceiling off his usage page. It is only
ever what he typed. A day without one reads ``None``; nothing is inferred,
carried forward, or back-filled.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

TIERS: tuple[str, ...] = ("fast", "standard", "power")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Fixed set so a day row always carries every key (a missing key reads as a
#: bug on the page, a zero reads as "none that day").
_TIER_KEYS: tuple[str, ...] = TIERS + ("other", "unknown")


def readings_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "pa-week-cost" / "operator-readings.json"


# ── Operator's reading ───────────────────────────────────────────────────────


def load_readings(shared_dir: Path) -> dict[str, dict[str, Any]]:
    """``{day: {used_usd, ceiling_usd, note, recorded_at}}``; ``{}`` if none."""
    try:
        data = json.loads(readings_path(shared_dir).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {d: r for d, r in data.items() if _DAY_RE.match(str(d)) and isinstance(r, dict)}


def _money(raw: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{name} must be a number")
    try:
        v = float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number")
    if v < 0 or (positive and v == 0):
        raise ValueError(f"{name} must be {'greater than' if positive else 'at least'} zero")
    return v


def set_reading(
    shared_dir: Path, day: str, *, used_usd: Any, ceiling_usd: Any = None,
    note: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Record the operator's reading for ``day``. Replaces that day's record.

    ``ceiling_usd`` is optional: the pool does not change day to day, and a
    day that omits it simply has no ceiling on record (the finding takes it
    from the days that do — it is never copied onto this one).
    """
    if not isinstance(day, str) or not _DAY_RE.match(day):
        raise ValueError("day must be YYYY-MM-DD")
    rec: dict[str, Any] = {
        "used_usd": _money(used_usd, "used"),
        "ceiling_usd": (
            None if ceiling_usd in (None, "") else _money(ceiling_usd, "ceiling", positive=True)
        ),
        "note": (note or "").strip()[:500] or None,
        "recorded_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
    }
    from evolve_util import atomic_write_json  # type: ignore[import]

    readings = load_readings(shared_dir)
    readings[day] = rec
    p = readings_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, readings, indent=2, mode=0o644)
    return rec


# ── Tier of a turn ───────────────────────────────────────────────────────────


def tier_of(turn: Mapping[str, Any], tier_of_model: Mapping[str, str] | None = None) -> str:
    """The tier that answered: the row's own ``model_role`` if it carries one,
    else the bot's role config looked up by the model that answered, else
    ``unknown``. A recorded role outside fast/standard/power is ``other``."""
    role = turn.get("model_role")
    if isinstance(role, str) and role.strip():
        r = role.strip().lower()
        return r if r in TIERS else "other"
    model = str(turn.get("model") or "").strip()
    if tier_of_model and model:
        for key in (model, model.split("/", 1)[-1]):
            hit = tier_of_model.get(key)
            if hit in TIERS:
                return hit
    return "unknown"


def role_models_for_bot(network: dict, bot_id: str) -> dict[str, list[str]]:
    """``{role: [model, ...]}`` for fast / standard / power from the bot's own
    tiers file (pod defaults when it has none). Empty on any read failure —
    the rows then read ``unknown``, which is the honest answer."""
    try:
        import primary_bot  # type: ignore[import]

        doc = primary_bot.read_bot_tiers_doc(network, bot_id)
        out: dict[str, list[str]] = {}
        for tier_key, role in (("tier3", "fast"), ("tier2", "standard"), ("tier1", "power")):
            chain = primary_bot.resolve_tier_chain(doc, tier_key) if doc else []
            if chain:
                out[role] = [str(m) for m in chain]
        return out
    except Exception:  # noqa: BLE001 — a decoration; never sink the rollup
        return {}


def model_to_tier(role_models: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """Invert :func:`role_models_for_bot`. A model on two rungs is ambiguous
    and is left out (``unknown``) rather than assigned to one."""
    seen: dict[str, set[str]] = {}
    for role, models in role_models.items():
        for m in models:
            for key in (m, m.split("/", 1)[-1]):
                seen.setdefault(key, set()).add(role)
    return {m: next(iter(r)) for m, r in seen.items() if len(r) == 1}


# ── The rollup ───────────────────────────────────────────────────────────────


def _blank_tiers() -> dict[str, dict[str, Any]]:
    return {k: {"turns": 0, "usd": 0.0} for k in _TIER_KEYS}


def _reprice(turn: Mapping[str, Any], model: str, catalog: dict | None) -> float | None:
    """What ``turn`` would have cost on ``model``, from its own token counts."""
    from turn_cost import estimate_turn_cost  # type: ignore[import]

    probe = dict(turn)
    probe.update(model=model, cost=None, cost_source=None)
    if "/" in model:
        probe["provider"] = model.split("/", 1)[0]
    try:
        return estimate_turn_cost(probe, catalog=catalog)
    except Exception:  # noqa: BLE001
        return None


def build_daily_rows(
    turns: list[dict],
    *,
    days: int,
    now: datetime,
    tier_of_model: Mapping[str, str] | None = None,
    standard_model: str | None = None,
    fp_tokens: dict[str, int] | None = None,
    shared_dir: Path | None = None,
    role_of: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """One row per pod-local day, oldest first, ``days`` of them ending at the
    local day containing ``now``. A day with no turns is still a row."""
    import evolve_overhead as eo  # type: ignore[import]
    import live_spend  # type: ignore[import]
    from housekeeping_cost import is_housekeeping_turn  # type: ignore[import]
    from turn_cost import TRUTH_RESOLUTIONS, load_pricing_catalog, turn_cost_detail  # type: ignore[import]

    overhead = {
        r["day"]: r
        for r in eo.build_days(
            turns, days=days, now=now, fp_tokens=fp_tokens, shared_dir=shared_dir,
            role_of=role_of,
        )
    }
    tz = live_spend.pod_tz_or_local()
    catalog = load_pricing_catalog(shared_dir)
    rows: dict[str, dict[str, Any]] = {}
    for day, o in overhead.items():
        rows[day] = {
            "day": day,
            "total_usd": 0.0, "turns": 0, "unpriced_turns": 0,
            "oc_priced_turns": 0, "priced_turns": 0,
            "overhead": {
                "evolve_calls": o["evolve_calls"], "evolve_usd": o["evolve_usd"],
                "context_usd": o["context_usd"],
                "usd": o["evolve_usd"] + o["context_usd"],
                "share": ((o["evolve_usd"] + o["context_usd"]) / o["total_usd"]
                          if o["total_usd"] > 0 else None),
            },
            "tiers": _blank_tiers(),
            "housekeeping": {"turns": 0, "usd": 0.0},
            "largest_turn": None,
            "standard_default_estimate_usd": 0.0,
            "estimate_unpriced_turns": 0,
        }
    for t in turns:
        day = live_spend.local_day_iso(t.get("ts"), tz)
        row = rows.get(day) if day else None
        if row is None:
            continue
        cost, resolution = turn_cost_detail(t, catalog=catalog)
        row["turns"] += 1
        if cost is None:
            row["unpriced_turns"] += 1
            continue
        row["priced_turns"] += 1
        if resolution in TRUTH_RESOLUTIONS:
            row["oc_priced_turns"] += 1
        row["total_usd"] += cost
        best = row["largest_turn"]
        if best is None or cost > best["usd"]:
            row["largest_turn"] = {
                "usd": cost, "ts": t.get("ts"), "model": t.get("model"),
                "source": t.get("source"), "tier": tier_of(t, tier_of_model),
            }
        est = cost  # what this turn costs in the standard-default week
        if is_housekeeping_turn(t):
            row["housekeeping"]["turns"] += 1
            row["housekeeping"]["usd"] += cost
        elif eo.evolve_kind(t) is None:
            tier = tier_of(t, tier_of_model)
            row["tiers"][tier]["turns"] += 1
            row["tiers"][tier]["usd"] += cost
            if tier == "power" and standard_model:
                re_priced = _reprice(t, standard_model, catalog)
                if re_priced is None:
                    row["estimate_unpriced_turns"] += 1
                else:
                    est = re_priced
        row["standard_default_estimate_usd"] += est
    out = [rows[k] for k in sorted(rows)]
    for r in out:
        for k in ("total_usd", "standard_default_estimate_usd"):
            r[k] = round(r[k], 6)
        r["housekeeping"]["usd"] = round(r["housekeeping"]["usd"], 6)
        r["overhead"] = {k: (round(v, 6) if isinstance(v, float) else v)
                         for k, v in r["overhead"].items()}
        for b in r["tiers"].values():
            b["usd"] = round(b["usd"], 6)
        if r["largest_turn"]:
            r["largest_turn"]["usd"] = round(r["largest_turn"]["usd"], 6)
        r["cost_source"] = (
            None if r["priced_turns"] == 0
            else "oc" if r["oc_priced_turns"] == r["priced_turns"]
            else "estimate" if r["oc_priced_turns"] == 0 else "mixed"
        )
    return out


def week_summary(day_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fold day rows (one bot, or several bots' rows together) into a window."""
    rows = list(day_rows)
    tiers = _blank_tiers()
    out: dict[str, Any] = {
        "days": len({r["day"] for r in rows}), "turns": 0, "unpriced_turns": 0,
        "total_usd": 0.0, "overhead_usd": 0.0, "evolve_usd": 0.0, "context_usd": 0.0,
        "housekeeping_usd": 0.0, "standard_default_estimate_usd": 0.0,
        "estimate_unpriced_turns": 0, "largest_turn": None, "oc_priced_turns": 0,
        "priced_turns": 0,
    }
    for r in rows:
        out["turns"] += r["turns"]
        out["unpriced_turns"] += r["unpriced_turns"]
        out["total_usd"] += r["total_usd"]
        out["overhead_usd"] += r["overhead"]["usd"]
        out["evolve_usd"] += r["overhead"]["evolve_usd"]
        out["context_usd"] += r["overhead"]["context_usd"]
        out["housekeeping_usd"] += r["housekeeping"]["usd"]
        out["standard_default_estimate_usd"] += r["standard_default_estimate_usd"]
        out["estimate_unpriced_turns"] += r["estimate_unpriced_turns"]
        out["oc_priced_turns"] += r["oc_priced_turns"]
        out["priced_turns"] += r["priced_turns"]
        for k, b in r["tiers"].items():
            tiers[k]["turns"] += b["turns"]
            tiers[k]["usd"] += b["usd"]
        lt = r["largest_turn"]
        if lt and (out["largest_turn"] is None or lt["usd"] > out["largest_turn"]["usd"]):
            out["largest_turn"] = lt
    for k in ("total_usd", "overhead_usd", "evolve_usd", "context_usd",
              "housekeeping_usd", "standard_default_estimate_usd"):
        out[k] = round(out[k], 6)
    for b in tiers.values():
        b["usd"] = round(b["usd"], 6)
    out["tiers"] = tiers
    out["overhead_share"] = (
        round(out["overhead_usd"] / out["total_usd"], 6) if out["total_usd"] > 0 else None
    )
    out["measurable"] = out["unpriced_turns"] == 0
    return out


def build_report(
    shared_dir: Path,
    network: dict,
    *,
    bot_ids: Iterable[str],
    days: int = 7,
    now: datetime | None = None,
    turns_by_bot: dict[str, list[dict]] | None = None,
    role_models: dict[str, dict[str, list[str]]] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Per bot daily rows + week, the pod week, and the operator's readings.

    A bot whose turn files cannot be read is listed in ``unreadable_bots`` —
    "could not look", never a confident zero.
    """
    import evolve_overhead as eo  # type: ignore[import]

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bots: dict[str, Any] = {}
    unreadable: list[str] = []
    for bot in bot_ids:
        turns = (turns_by_bot or {}).get(bot)
        if turns is None:
            turns = eo.load_turns(bot, days=days, now=now, log=log)
        if turns is None:
            unreadable.append(bot)
            continue
        rm = (role_models or {}).get(bot)
        if rm is None:
            rm = role_models_for_bot(network, bot)
        std = (rm.get("standard") or [None])[0]
        rows = build_daily_rows(
            turns, days=days, now=now, tier_of_model=model_to_tier(rm),
            standard_model=std, fp_tokens=eo.footprint_tokens(shared_dir, bot),
            shared_dir=shared_dir,
        )
        bots[bot] = {
            "days": rows, "week": week_summary(rows),
            "standard_model": std, "power_models": rm.get("power") or [],
        }
    pod_rows = [r for b in bots.values() for r in b["days"]]
    pod_days: dict[str, list[dict]] = {}
    for r in pod_rows:
        pod_days.setdefault(r["day"], []).append(r)
    readings = load_readings(shared_dir)
    window = sorted(pod_days)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "window": {"from": window[0] if window else None, "to": window[-1] if window else None},
        "bots": bots,
        "unreadable_bots": unreadable,
        "pod": {"week": week_summary(pod_rows), "days": [
            {"day": d, **{k: round(sum(x[k] for x in pod_days[d]), 6)
                          for k in ("total_usd", "standard_default_estimate_usd")}}
            for d in window
        ]},
        # Only days inside the window, only what the operator typed.
        "operator_readings": {d: readings[d] for d in window if d in readings},
    }


# ── The one sentence ─────────────────────────────────────────────────────────


# The pool ceiling the operator types is a monthly (30-day) figure.
POOL_PERIOD_DAYS = 30


def fit_sentence(report: Mapping[str, Any]) -> str:
    """*A PA week at the Power default did / did not fit inside its share of the
    $N monthly pool, by this margin.* The pool is the monthly ceiling the
    operator recorded; if he never recorded one the sentence says it cannot be
    written, instead of assuming a plan size. The ceiling is a 30-day figure, so
    it is prorated to the window (``ceiling × days / 30``, the window's own day
    count) before it is compared with the window's API-rate cost — the pool is
    metered at API rates, so the two are the same unit AND the same period."""
    readings = report.get("operator_readings") or {}
    ceilings = [r["ceiling_usd"] for _, r in sorted(readings.items())
                if isinstance(r.get("ceiling_usd"), (int, float))]
    week = (report.get("pod") or {}).get("week") or {}
    total = week.get("total_usd")
    days = week.get("days")
    if not ceilings or total is None or not days:
        return ("No pool ceiling was recorded for this window, so the fit cannot be stated; "
                f"the week cost ${total:,.2f} at API rates." if total is not None
                else "The week could not be measured.")
    pool = ceilings[-1]
    share = pool * days / POOL_PERIOD_DAYS
    margin = share - total
    verb = "did" if margin >= 0 else "did not"
    side = "under" if margin >= 0 else "over"
    return (f"A {days}-day share of the ${pool:,.0f} monthly pool is ${share:,.2f}; "
            f"a PA week at the Power default {verb} fit inside it: "
            f"${total:,.2f} at API rates, ${abs(margin):,.2f} {side} "
            f"({total / share * 100:.0f}% of the share).")


# ── Rendering / CLI ──────────────────────────────────────────────────────────


def render_table(report: Mapping[str, Any]) -> str:
    """The finding's table: one row per bot, then the pod. Numbers only."""
    cols = ("bot", "API-rate week", "Evolve share", "fast", "standard", "power",
            "housekeeping", "standard-default (est.)")
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]

    def row(name: str, w: Mapping[str, Any]) -> str:
        sh = w.get("overhead_share")
        t = w["tiers"]
        cells = [name, f"${w['total_usd']:.2f}", "n/a" if sh is None else f"{sh * 100:.1f}%"]
        cells += [f"{t[k]['turns']} turns · ${t[k]['usd']:.2f}" for k in TIERS]
        cells += [f"${w['housekeeping_usd']:.2f}", f"~${w['standard_default_estimate_usd']:.2f}"]
        return "| " + " | ".join(cells) + " |"

    for bot, b in sorted(report["bots"].items()):
        lines.append(row(bot, b["week"]))
    lines.append(row("**pod**", report["pod"]["week"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="PA week cost: daily rows + the finding's table")
    ap.add_argument("--shared-dir", required=True)
    ap.add_argument("--network", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--table", action="store_true", help="print the markdown table + the sentence")
    args = ap.parse_args(argv)
    shared = Path(args.shared_dir)
    network = json.loads(Path(args.network or shared / "network.json").read_text())
    import evolve_overhead as eo  # type: ignore[import]

    report = build_report(shared, network, bot_ids=eo.bot_ids_of(network), days=args.days,
                          log=lambda m: print(m, file=sys.stderr))
    if args.table:
        print(render_table(report))
        print()
        print(fit_sentence(report))
    else:
        print(json.dumps({**report, "fit_sentence": fit_sentence(report)}, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
