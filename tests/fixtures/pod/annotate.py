#!/usr/bin/env python3
"""annotate — write the turn annotations Evolve's plugin writes after install.

The fixture's OpenClaw turn files are history from *before* Evolve arrived:
they carry usage but no attribution, because the plugin was not there to
stamp it.  This module writes the other half — ``{shared}/annotations/<bot>/
<YYYY-MM-DD>.jsonl`` ``turn_annotation`` rows in the schema-5 shape
``TurnObserver`` writes — for the N days since a notional install, so the
"what does the pod look like after a week" question can be asked without
waiting a week.

**This is the BEST case, deliberately.**  Rows for a promoted app are written
with ``app_attribution: "scheduled"``, which on a real pod requires the plugin
to have joined the firing cron to an app through
``{shared}/{bot}/app-cron-map.json`` — a file only the app-INSTALL path
writes.  A pod whose apps were discovered rather than installed does not get
that join, and its rows land unattributed.  The audit reports that separately;
here the point is to see what the Usage and Apps surfaces do when the numbers
*are* there.

Usage::

    python3 -m tests.fixtures.pod.annotate --root /tmp/fixture-pod --days 7
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Which app each bot's scheduled turns serve, by manifest stem. Resolved to
#: the app's real ``pkg_id`` at write time — attribution is by app id, and a
#: stem would be a different (and wrong) key.
SCHEDULED_APPS = {
    "personal-bot": ["morning-brief", "medication-reminder", "evening-journal"],
    "team-bot-a": ["standup-digest", "oncall-rota", "morning-brief"],
    "admin-bot": ["release-notes", "repo-watch"],
}

MODELS = [
    ("anthropic", "claude-sonnet-4-5", 3.00, 15.00),
    ("anthropic", "claude-haiku-4-5", 0.80, 4.00),
]


def _pkg_ids(home: Path, stems: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    mdir = home / ".openclaw" / "workspace" / "manifests"
    for stem in stems:
        path = mdir / f"{stem}.json"
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        app_id = raw.get("pkg_id") or raw.get("spec_id") or raw.get("id")
        if isinstance(app_id, str) and app_id:
            out[stem] = app_id
    return out


def _row(*, bot_id, session_id, ts, app_id, grade, source, rng) -> dict:
    provider, model, in_price, out_price = rng.choice(MODELS)
    inp = rng.randint(1200, 9000)
    out = rng.randint(120, 900)
    return {
        "type": "turn_annotation",
        "schema_version": 5,
        "turn_id": f"t-{rng.getrandbits(32):08x}",
        "session_id": session_id,
        "ts": ts,
        "bot_id": bot_id,
        "session_class": "scheduled" if grade == "scheduled" else "human",
        "model_tier": "workhorse",
        "model_role": "chat",
        "model_selected": model,
        "provider": provider,
        "auth_mode": "unknown",
        "input_tokens": inp,
        "output_tokens": out,
        "cache_write_tokens": 0,
        "cache_read_tokens": rng.choice([0, rng.randint(3000, 30000)]),
        "cost_estimated": round(inp * in_price / 1e6 + out * out_price / 1e6, 6),
        "app_id": app_id,
        "app_attribution": grade,
        "app_confidence": 1.0 if grade == "scheduled" else None,
        "app_attribution_source": source,
    }


def annotate(root: Path, *, days: int = 7, seed: int = 20260823) -> dict:
    from evolve_config import bot_home

    rng = random.Random(seed)
    net = json.loads((root / "shared" / "network.json").read_text())
    shared = Path(net["sharedDir"])
    today = datetime.now(timezone.utc).date()
    counts: dict[str, int] = {}

    for bot_id in net["members"]:
        home = bot_home(bot_id, net)
        ids = _pkg_ids(home, SCHEDULED_APPS.get(bot_id, []))
        ann_dir = shared / "annotations" / bot_id
        ann_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for offset in range(days):
            day = today - timedelta(days=offset)
            rows = []
            # One scheduled session per app per day — the app fired.
            for i, (stem, app_id) in enumerate(sorted(ids.items())):
                sid = f"{bot_id}-{day.isoformat()}-cron-{stem}"
                for turn in range(rng.randint(1, 3)):
                    rows.append(
                        _row(
                            bot_id=bot_id,
                            session_id=sid,
                            ts=datetime(
                                day.year, day.month, day.day,
                                6 + i * 3, 45 + turn, tzinfo=timezone.utc,
                            ).isoformat().replace("+00:00", "Z"),
                            app_id=app_id,
                            grade="scheduled",
                            source="oc_cron_map",
                            rng=rng,
                        )
                    )
            # Plus ordinary chat, which belongs to no app and must show up in
            # the unattributed bucket rather than being quietly dropped.
            for turn in range(rng.randint(4, 12)):
                rows.append(
                    _row(
                        bot_id=bot_id,
                        session_id=f"{bot_id}-{day.isoformat()}-chat-{turn // 4}",
                        ts=datetime(
                            day.year, day.month, day.day,
                            9 + (turn % 12), (turn * 5) % 60, tzinfo=timezone.utc,
                        ).isoformat().replace("+00:00", "Z"),
                        app_id=None,
                        grade="none",
                        source=None,
                        rng=rng,
                    )
                )
            rows.sort(key=lambda r: r["ts"])
            (ann_dir / f"{day.isoformat()}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n"
            )
            written += len(rows)
        counts[bot_id] = written
    return counts


def rollup(root: Path) -> dict:
    """Run AL-1.3's real rollup over the annotations, as its cron job does."""
    import usage_by_app

    net = json.loads((root / "shared" / "network.json").read_text())
    shared = Path(net["sharedDir"])
    out = {}
    for bot_id in usage_by_app.with_evolve_bot(list(net["members"])):
        payload = usage_by_app.rollup_bot(shared, bot_id)
        usage_by_app.write_usage_by_app(shared, bot_id, payload)
        out[bot_id] = payload.get("coverage", {}).get("d7", {})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Write post-install turn annotations")
    ap.add_argument("--root", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-rollup", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    print(json.dumps({"annotations": annotate(root, days=args.days)}, indent=2))
    if not args.no_rollup:
        print(json.dumps({"rollup_coverage_d7": rollup(root)}, indent=2))


if __name__ == "__main__":
    main()
