#!/usr/bin/env python3
"""annotate — write the turn annotations Evolve's plugin writes after install.

The fixture's OpenClaw turn files are history from *before* Evolve arrived:
they carry usage but no attribution, because the plugin was not there to
stamp it.  This module writes the other half — ``{shared}/annotations/<bot>/
<YYYY-MM-DD>.jsonl`` ``turn_annotation`` rows in the schema-5 shape
``TurnObserver`` writes, plus the ``session_summary`` rows
``SessionSummarizer`` appends to the same files — for the N days since a
notional install, so the "what does the pod look like after a week"
question can be asked without waiting a week.

The summaries carry a ``recurring_request`` for the CHAT sessions only,
which is the shape ``recurringRequest.ts`` actually produces: its third
gate is a resolved human requester, so a cron-fired session gets no row
and its turns are honestly un-attributable to a person. That asymmetry
is what ``analyzer/usage_by_user.py`` has to read — most turns on a real
pod have no requester, and a fixture where every turn had one would
rehearse a pod that does not exist.

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

#: Who talks to each bot, as ``platform:senderId`` — the key
#: ``recurringRequest.ts`` stamps and ``usage_by_user.py`` stores verbatim.
#: Platforms match each bot's channel mix in ``build.TURN_CHANNELS``.
REQUESTERS = {
    "personal-bot": ["telegram:41880321"],
    "team-bot-a": ["slack:U04KPRIYA", "slack:U04KSAMIR", "slack:U04KDEVON"],
    "admin-bot": ["telegram:41880321", "telegram:77201544"],
}


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


def _summary_row(*, bot_id, session_id, day, requester, rng) -> dict:
    """A ``session_summary`` row, with the ``recurring_request`` when a human
    opened the session. Only the three fields design §7.1a names leave the
    bot: label, requester, hour."""
    hour = rng.randint(7, 20)
    row = {
        "type": "session_summary",
        "schema_version": 2,
        "session_id": session_id,
        "ts": datetime(
            day.year, day.month, day.day, hour, 55, tzinfo=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "bot_id": bot_id,
        "turn_count": rng.randint(1, 4),
        "session_class": "productive",
        "outcome": "answered the question",
        "complexity": "low",
        "applications_invoked": [],
        "promises_made": [],
        "correction_count": 0,
        "efficiency_flag": False,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    if requester:
        row["recurring_request"] = {
            "label": rng.choice(
                ["summarize inbox", "check calendar", "draft reply",
                 "morning summary", "status update"]
            ),
            "requester": requester,
            "hour": hour,
        }
    return row


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
            cron_sessions: set[str] = set()
            for i, (stem, app_id) in enumerate(sorted(ids.items())):
                sid = f"{bot_id}-{day.isoformat()}-cron-{stem}"
                cron_sessions.add(sid)
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
            chat_sessions: set[str] = set()
            app_ids = sorted(ids.values())
            for turn in range(rng.randint(4, 12)):
                sid = f"{bot_id}-{day.isoformat()}-chat-{turn // 4}"
                chat_sessions.add(sid)
                # Roughly a third of chat turns invoke an app on purpose —
                # the `explicit` grade, which on a real pod comes from
                # `recordExplicit(..., "script_middleware" | "expand_app")`.
                # These are the turns that make "who uses Morning Brief"
                # answerable at all: an app AND a person on the same turn.
                invoked = bool(app_ids) and rng.random() < 0.34
                rows.append(
                    _row(
                        bot_id=bot_id,
                        session_id=sid,
                        ts=datetime(
                            day.year, day.month, day.day,
                            9 + (turn % 12), (turn * 5) % 60, tzinfo=timezone.utc,
                        ).isoformat().replace("+00:00", "Z"),
                        app_id=rng.choice(app_ids) if invoked else None,
                        grade="explicit" if invoked else "none",
                        source="script_middleware" if invoked else None,
                        rng=rng,
                    )
                )
            # A summary per session — SessionSummarizer runs for every one.
            # Only the chat sessions carry a `recurring_request`: a cron
            # session has no human sender, so gate 3 omits the row. That is
            # the "summary exists and states no requester" case a per-user
            # reader has to report honestly rather than guess through.
            people = REQUESTERS.get(bot_id) or []
            for sid in sorted(chat_sessions):
                rows.append(
                    _summary_row(
                        bot_id=bot_id, session_id=sid, day=day,
                        requester=rng.choice(people) if people else None,
                        rng=rng,
                    )
                )
            for sid in sorted(cron_sessions):
                rows.append(
                    _summary_row(
                        bot_id=bot_id, session_id=sid, day=day,
                        requester=None, rng=rng,
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
    """Run the real rollups over the annotations, as their cron jobs do.

    Both of them: AL-1.3's per-app rollup (03:35) and D-S2 track 3's
    per-user one (03:37), each with its own writer.
    """
    import usage_by_app
    import usage_by_user

    net = json.loads((root / "shared" / "network.json").read_text())
    shared = Path(net["sharedDir"])
    out = {}
    for bot_id in usage_by_app.with_evolve_bot(list(net["members"])):
        payload = usage_by_app.rollup_bot(shared, bot_id)
        usage_by_app.write_usage_by_app(shared, bot_id, payload)
        user_payload = usage_by_user.rollup_bot(shared, bot_id)
        usage_by_user.write_usage_by_user(shared, bot_id, user_payload)
        out[bot_id] = {
            "by_app": payload.get("coverage", {}).get("d7", {}),
            "by_user": user_payload.get("coverage", {}).get("d7", {}),
        }
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
