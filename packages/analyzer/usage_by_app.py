#!/usr/bin/env python3
"""
usage_by_app.py — Per-app usage rollup from turn annotations (AL-1.3).

Design: internal/design-app-attribution-2026-08-15.md §3 (the honesty
contract) + §8 (readers). Brief: internal/build-AL-1.3-usage-by-app.md.

For one bot, folds the trailing 30 days of ``turn_annotation`` records
into a per-``app_id`` rollup — turns, input/output tokens, and
``cost_estimated`` — **split by attribution grade** and windowed at
1 / 7 / 30 days.

The reader contract (brief §2 — the part that matters)
------------------------------------------------------
* Group by the annotation's ``app_id`` exactly as written. Nothing here
  re-derives an id from a filename, a manifest, or a session key.
* **Grades are additive columns, never merged.** Each window carries
  ``scheduled`` / ``explicit`` / ``inferred`` sub-buckets plus a
  ``total`` that is defined as ``scheduled + explicit`` — the
  deterministic grades. ``inferred`` rides *beside* the total, never
  inside it, until AL-1.9 calibration says otherwise. There is no
  key in the payload that silently sums an inferred turn into a
  deterministic one.
* **``none`` is shown, not hidden.** Turns with no attribution land in
  the top-level ``unattributed`` bucket, and ``coverage`` carries the
  ratio (``unattributed_turns_share`` / ``unattributed_cost_share``)
  that tells the operator how much to trust everything else. A reader
  that renders apps without rendering this number is lying by omission.
* Evolve's own subagent work (summarizer / classifier / task_extractor /
  fallback / forge — the trigger kinds the pod already bills as Evolve
  overhead) is pulled out into ``evolve_overhead`` so it never reads as
  "unknown app". See ``_session_trigger_kinds`` for the join.
* ``schema_version: 4`` annotations (pre-AL-1.1, no app fields at all)
  count as unattributed and are ALSO counted separately as
  ``legacy_schema_turns`` — on a pod mid-rollout most of the
  unattributed bucket is simply "written before attribution shipped",
  and conflating that with "attribution failed" would misdirect the
  operator.

Why turn annotations and not cost events
----------------------------------------
``cost_event`` records carry ``app_id`` too (AL-1.1 pass-through in
``cost_event_converter.py``), and they carry authoritative OC usage.
But the converter's PRIMARY source is OC's own turn-collector record
(``{bot_home}/.openclaw/workspace/memory/turns-<date>.jsonl``), which
has no app fields — the plugin never writes there. So a cost-event-
primary rollup would report near-total unattributed on any bot whose
OC turn-collector is running, which is a lie about capture, not a
measurement of it. The turn annotation is where the plugin stamps
attribution, so that is the source of record here.

Cost events are still read — but only to build a session → trigger_kind
map for the ``evolve_overhead`` split (they are the only place
``trigger_kind`` exists).

Readers
-------
  - admin ``/api/analytics/usage/by-app`` (Cost page "By App" card)
  - admin ``/api/analytics/applications`` (per-app tile: cost/turns 7d)
  - evo tool ``pod_state.app_usage``
  - ``load_usage_by_app()`` below is the single entry point for all three.

Output: ``{sharedDir}/{botId}/usage-by-app.json``, mode 0644 (coverage-file
perms lesson #3387), written atomically (tmp + rename in the destination
dir, mode pinned before the rename).

Privacy invariant: one bot per run. No cross-bot reads ever occur.

Schedule: daily at 03:35, right after usage_logger (03:30).

Usage:
    python3 usage_by_app.py --network /Users/Shared/evolve/network.json
    python3 usage_by_app.py --bot team_bot_a --shared-dir /Users/Shared/evolve
    python3 usage_by_app.py --bot team_bot_a --shared-dir /Users/Shared/evolve --report
    python3 usage_by_app.py --bot team_bot_a --shared-dir /Users/Shared/evolve --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from evolve_config import CANONICAL_SHARED_DIR
from evolve_util import now_iso_micro as _now_iso

# The annotation reader is exec_outcome_watchdog's — one reader for
# {shared}/annotations/<bot>/<date>.jsonl, not two (brief §0.4).
from exec_outcome_watchdog import read_turn_annotations

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
OUTPUT_FILENAME = "usage-by-app.json"
OUTPUT_MODE = 0o644

#: Trailing windows, in days. Key order is the payload's key order.
WINDOWS: dict[str, int] = {"d1": 1, "d7": 7, "d30": 30}
MAX_WINDOW_DAYS = max(WINDOWS.values())

#: The four grades the plugin may stamp (design §3). ``none`` is a value,
#: not a null — it means "we looked and there was no signal".
GRADES = ("scheduled", "explicit", "inferred", "none")

#: Grades whose turns make up an app's ``total``. ``inferred`` is
#: deliberately absent: a reader may never collapse inferred into
#: explicit (design §3). AL-1.9 calibration is what would change this.
DETERMINISTIC_GRADES = ("scheduled", "explicit")

#: trigger_kinds that are Evolve's own scaffolding rather than a user
#: app. Mirrors context_health's overhead bucket + the forge retag pass
#: in cost_event_converter. ``subagent`` is NOT here: an in-session
#: subagent spawn is the bot doing the user's work.
EVOLVE_OVERHEAD_TRIGGER_KINDS = frozenset({
    "summarizer", "classifier", "task_extractor", "fallback", "forge",
})

#: Annotations at this schema_version predate AL-1.1 and carry no app
#: fields at all. They are unattributed, and counted separately so the
#: coverage number can distinguish "before attribution shipped" from
#: "attribution ran and found nothing".
LEGACY_SCHEMA_VERSION_MAX = 4


# ── Accumulators ─────────────────────────────────────────────────────────────

def _empty_metrics() -> dict[str, Any]:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_estimated": 0.0,
    }


def _add_metrics(acc: dict[str, Any], rec: dict) -> None:
    """Fold one annotation's usage into an accumulator (defensive casts)."""
    acc["turns"] += 1
    acc["input_tokens"] += _int(rec.get("input_tokens"))
    acc["output_tokens"] += _int(rec.get("output_tokens"))
    acc["cost_estimated"] += _float(rec.get("cost_estimated"))


def _sum_metrics(parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out = _empty_metrics()
    for part in parts:
        out["turns"] += part["turns"]
        out["input_tokens"] += part["input_tokens"]
        out["output_tokens"] += part["output_tokens"]
        out["cost_estimated"] += part["cost_estimated"]
    return out


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _round_cost(metrics: dict[str, Any]) -> dict[str, Any]:
    """Round the float so JSON diffs stay readable (6dp ≈ $0.000001)."""
    metrics["cost_estimated"] = round(metrics["cost_estimated"], 6)
    return metrics


# ── Grade + overhead classification ──────────────────────────────────────────

def classify_grade(rec: dict) -> tuple[str | None, str]:
    """Return ``(app_id, grade)`` for one annotation record.

    Fails toward "no signal" in every ambiguous case, exactly as
    ``cost_event_converter.turn_to_cost_event`` does: an unrecognized
    grade, a grade with no id, or an id with no grade all resolve to
    ``(None, "none")``. Never invent an attribution the writer never
    made.
    """
    app_id = rec.get("app_id")
    if not (isinstance(app_id, str) and app_id.strip()):
        app_id = None
    grade = rec.get("app_attribution")
    if grade not in GRADES:
        grade = "none"
    if app_id is None or grade == "none":
        return None, "none"
    return app_id.strip(), grade


def is_legacy_schema(rec: dict) -> bool:
    """True for pre-AL-1.1 annotations (no app fields were written)."""
    version = rec.get("schema_version")
    if not isinstance(version, int):
        # Unversioned / malformed: older than anything we version-gate.
        return True
    return version <= LEGACY_SCHEMA_VERSION_MAX


def _session_trigger_kinds(shared_dir: Path, bot_id: str, day: date) -> dict[str, str]:
    """Map ``session_id -> trigger_kind`` from one day's cost events.

    ``trigger_kind`` exists only on cost events (derived by
    ``cost_event_converter`` from the turn record's source/channel), and
    a session is single-kind by construction — so a session-level join
    is enough to tell an Evolve-overhead turn (forge dispatch, one of
    the plugin-subagent lanes) from a real user/app turn.

    Best-effort: a bot with no converter output yet yields ``{}`` and
    every turn stays in its annotation-derived bucket. Missing overhead
    classification degrades toward ``none`` (honest) rather than toward
    a fabricated app.
    """
    try:
        from cost_rollup import iter_cost_events
    except ImportError:
        return {}
    out: dict[str, str] = {}
    try:
        for event in iter_cost_events(shared_dir, bot_id, day):
            session_id = event.get("session_id")
            kind = event.get("trigger_kind")
            if isinstance(session_id, str) and session_id and isinstance(kind, str):
                out.setdefault(session_id, kind)
    except Exception:
        # A cost-store hiccup must never break the app rollup — the
        # overhead split degrades, the app numbers stay intact.
        return out
    return out


# ── Rollup ───────────────────────────────────────────────────────────────────

def rollup_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Fold the trailing 30 days of annotations into the payload dict.

    Pure: reads only ``{shared}/annotations/<bot>/`` (+ the cost-event
    siblings for the overhead join) and returns the payload. The caller
    writes it.
    """
    shared_dir = Path(shared_dir)
    if today is None:
        today = datetime.now(timezone.utc).date()

    # app_id -> window_key -> grade -> metrics
    apps: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    # window_key -> metrics (plus legacy counter)
    unattributed: dict[str, dict[str, Any]] = {
        key: {**_empty_metrics(), "legacy_schema_turns": 0} for key in WINDOWS
    }
    overhead: dict[str, dict[str, Any]] = {key: _empty_metrics() for key in WINDOWS}
    seen_ts: dict[str, dict[str, str]] = {}   # app_id -> {first, last}

    for offset in range(MAX_WINDOW_DAYS):
        day = today - timedelta(days=offset)
        # days=1 per call so the FILE date (not a parsed ts) decides
        # window membership — one file is one UTC day by construction.
        records = read_turn_annotations(shared_dir, bot_id, days=1, today=day)
        if not records:
            continue
        windows_hit = [key for key, span in WINDOWS.items() if offset < span]
        trigger_kinds: dict[str, str] | None = None

        for rec in records:
            app_id, grade = classify_grade(rec)

            if app_id is None:
                # Unattributed — unless the pod already bills this turn
                # as Evolve's own overhead, in which case it is not an
                # "unknown app" and must not inflate the coverage gap.
                if trigger_kinds is None:
                    trigger_kinds = _session_trigger_kinds(shared_dir, bot_id, day)
                kind = trigger_kinds.get(str(rec.get("session_id") or ""))
                if kind in EVOLVE_OVERHEAD_TRIGGER_KINDS:
                    for key in windows_hit:
                        _add_metrics(overhead[key], rec)
                    continue
                legacy = is_legacy_schema(rec)
                for key in windows_hit:
                    _add_metrics(unattributed[key], rec)
                    if legacy:
                        unattributed[key]["legacy_schema_turns"] += 1
                continue

            app = apps.setdefault(
                app_id,
                {key: {g: _empty_metrics() for g in GRADES if g != "none"}
                 for key in WINDOWS},
            )
            for key in windows_hit:
                _add_metrics(app[key][grade], rec)

            # ISO-8601 UTC strings from one writer (TurnObserver's
            # toISOString) — lexicographic order is chronological order.
            ts = rec.get("ts")
            if isinstance(ts, str) and ts:
                bounds = seen_ts.setdefault(app_id, {"first": ts, "last": ts})
                if ts < bounds["first"]:
                    bounds["first"] = ts
                if ts > bounds["last"]:
                    bounds["last"] = ts

    return _assemble(bot_id, apps, unattributed, overhead, seen_ts, today)


def _assemble(
    bot_id: str,
    apps: dict[str, dict[str, dict[str, dict[str, Any]]]],
    unattributed: dict[str, dict[str, Any]],
    overhead: dict[str, dict[str, Any]],
    seen_ts: dict[str, dict[str, str]],
    today: date,
) -> dict[str, Any]:
    """Shape the accumulators into the on-disk payload."""
    apps_out: dict[str, Any] = {}
    for app_id, per_window in sorted(apps.items()):
        entry: dict[str, Any] = {
            "first_seen_ts": seen_ts.get(app_id, {}).get("first"),
            "last_seen_ts": seen_ts.get(app_id, {}).get("last"),
        }
        for key in WINDOWS:
            grades = per_window[key]
            entry[key] = {
                # `total` is scheduled + explicit ONLY (brief §2). There
                # is deliberately no key here that includes `inferred`.
                "total": _round_cost(
                    _sum_metrics(grades[g] for g in DETERMINISTIC_GRADES)
                ),
                **{g: _round_cost(dict(grades[g])) for g in GRADES if g != "none"},
            }
        apps_out[app_id] = entry

    coverage: dict[str, Any] = {}
    for key in WINDOWS:
        attributed = _sum_metrics(
            apps_out[a][key]["total"] for a in apps_out
        )
        inferred = _sum_metrics(apps_out[a][key]["inferred"] for a in apps_out)
        un = unattributed[key]
        # Denominator: attributed (deterministic) + inferred +
        # unattributed. Evolve overhead is deliberately OUT — it is not
        # an app and counting it would flatter or damn the coverage
        # number depending on how chatty Evolve's own subagents were.
        denom_turns = attributed["turns"] + inferred["turns"] + un["turns"]
        denom_cost = (
            attributed["cost_estimated"]
            + inferred["cost_estimated"]
            + un["cost_estimated"]
        )
        coverage[key] = {
            "attributed_turns": attributed["turns"],
            "inferred_turns": inferred["turns"],
            "unattributed_turns": un["turns"],
            "legacy_schema_turns": un["legacy_schema_turns"],
            "evolve_overhead_turns": overhead[key]["turns"],
            "app_turns_total": denom_turns,
            "unattributed_turns_share": (
                round(un["turns"] / denom_turns, 4) if denom_turns else None
            ),
            "unattributed_cost_share": (
                round(un["cost_estimated"] / denom_cost, 4) if denom_cost else None
            ),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "bot_id": bot_id,
        "as_of_date": today.isoformat(),
        "windows": {key: span for key, span in WINDOWS.items()},
        "apps": apps_out,
        "unattributed": {
            key: _round_cost(dict(unattributed[key])) for key in WINDOWS
        },
        "evolve_overhead": {
            key: _round_cost(dict(overhead[key])) for key in WINDOWS
        },
        "coverage": coverage,
    }


# ── Output ───────────────────────────────────────────────────────────────────

def usage_by_app_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / OUTPUT_FILENAME


def write_usage_by_app(
    shared_dir: Path,
    bot_id: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> Path:
    """Atomically write the payload to {shared}/{bot}/usage-by-app.json.

    tmp + rename inside the DESTINATION dir (a /tmp staging dir would be
    a cross-device rename), with the mode pinned to 0644 on the temp
    file BEFORE the rename — ``mkstemp`` creates 0600 and ``os.replace``
    carries that mode onto the destination, which would lock every
    reader but the writing user out of the file.
    """
    out_path = usage_by_app_path(shared_dir, bot_id)
    if dry_run:
        print(f"[usage-by-app] [dry-run] would write {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=".usage-by-app-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.chmod(tmp, OUTPUT_MODE)
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            # Already gone (interrupted rename) — nothing to clean up.
            print(f"[usage-by-app] {bot_id}: temp file vanished during cleanup",
                  file=sys.stderr)
        raise
    return out_path


def load_usage_by_app(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """Read one bot's rollup. Returns ``{}`` when absent or unreadable.

    The single reader for the admin routes and the evo tool — an empty
    dict means "no rollup yet", which callers render as "not measured",
    never as zero usage.
    """
    path = usage_by_app_path(shared_dir, bot_id)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def has_attributed_turns(payload: dict[str, Any], window: str = "d7") -> bool:
    """True when the rollup saw at least one attributed turn in ``window``.

    The fallback gate (brief §1): the Usage / Apps readers prefer this
    rollup, and fall back to ``usage_logger``'s mtime footprint only for
    bots with zero attributed turns — the audit's structural inference
    stays available, but as a fallback, never as the primary signal.
    """
    coverage = (payload.get("coverage") or {}).get(window) or {}
    return bool(
        _int(coverage.get("attributed_turns")) + _int(coverage.get("inferred_turns"))
    )


def with_evolve_bot(members: list[str]) -> list[str]:
    """Members plus the ``evolve`` bot, which also runs the plugin.

    ``network.members`` excludes it, but it writes turn annotations like
    any other bot and the admin readers list it (``_oc_members``) — skip
    it here and the Cost page's By App card would show evolve as
    permanently "not measured".
    """
    out = list(members)
    if "evolve" not in out:
        out.append("evolve")
    return out


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_usage_by_app(
    bot_id: str,
    shared_dir: Path,
    *,
    dry_run: bool = False,
    report: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Roll up one bot and (unless report/dry-run) write the file."""
    payload = rollup_bot(shared_dir, bot_id, today=today)

    if report:
        _print_report(payload)
        return payload

    write_usage_by_app(shared_dir, bot_id, payload, dry_run=dry_run)
    if not dry_run:
        cov = payload["coverage"]["d7"]
        share = cov["unattributed_turns_share"]
        share_str = "n/a" if share is None else f"{share:.1%}"
        print(
            f"[usage-by-app] {bot_id}: {len(payload['apps'])} apps, "
            f"{cov['attributed_turns']} attributed turns (7d), "
            f"unattributed {share_str}"
        )
    return payload


def _print_report(payload: dict[str, Any]) -> None:
    print(f"\n{payload['bot_id']} — per-app usage (7d window)")
    print(f"{'App':<32} {'Turns':>6} {'Sched':>6} {'Expl':>6} {'Infer':>6} "
          f"{'Cost':>9}  Last seen")
    print("-" * 88)
    rows = sorted(
        payload["apps"].items(),
        key=lambda kv: kv[1]["d7"]["total"]["cost_estimated"],
        reverse=True,
    )
    for app_id, entry in rows:
        window = entry["d7"]
        print(
            f"{app_id:<32} {window['total']['turns']:>6} "
            f"{window['scheduled']['turns']:>6} {window['explicit']['turns']:>6} "
            f"{window['inferred']['turns']:>6} "
            f"${window['total']['cost_estimated']:>8.4f}  "
            f"{(entry['last_seen_ts'] or '—')[:19]}"
        )
    cov = payload["coverage"]["d7"]
    un = payload["unattributed"]["d7"]
    share = cov["unattributed_turns_share"]
    share_str = "n/a" if share is None else f"{share:.1%}"
    print("-" * 88)
    print(
        f"{'unattributed':<32} {un['turns']:>6} {'':>6} {'':>6} {'':>6} "
        f"${un['cost_estimated']:>8.4f}  share {share_str} "
        f"({cov['legacy_schema_turns']} pre-AL-1.1)"
    )
    print(
        f"{'evolve_overhead (not an app)':<32} "
        f"{payload['evolve_overhead']['d7']['turns']:>6}"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-app usage rollup from turn annotations (AL-1.3)"
    )
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", help="Path to network.json (processes all bots)")
    parser.add_argument("--bot", dest="bot_id", help="Single bot to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute without writing")
    parser.add_argument("--report", action="store_true",
                        help="Print the 7d table, no file writes")
    args = parser.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bots: list[str] = []

    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        try:
            net = json.loads(Path(args.network).read_text())
            shared_dir = Path(net.get("sharedDir", str(shared_dir)))
            bots = with_evolve_bot(net.get("members", []))
        except Exception as exc:
            print(f"[usage-by-app] Failed to read network.json: {exc}", file=sys.stderr)
            return 1
    else:
        net_path = shared_dir / "network.json"
        if net_path.exists():
            try:
                bots = with_evolve_bot(
                    json.loads(net_path.read_text()).get("members", [])
                )
            except Exception as exc:
                print(f"[usage-by-app] Failed to read {net_path}: {exc}",
                      file=sys.stderr)

    if not bots:
        parser.error("Specify --bot BOT_ID, --network PATH, or ensure network.json exists")

    for bot in bots:
        try:
            run_usage_by_app(
                bot, shared_dir, dry_run=args.dry_run, report=args.report,
            )
        except Exception as exc:  # one bot's failure must not stop the sweep
            print(f"[usage-by-app] {bot}: rollup failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
