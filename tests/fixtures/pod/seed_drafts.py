#!/usr/bin/env python3
"""seed_drafts — mint the drafts a SUCCESSFUL discovery scan would have written.

Why this is a separate step, and what it is honest about
-------------------------------------------------------
``scan_workspace_pipeline``'s only producer of app candidates is its Phase-2
LLM pass (plus Phase 3b's conversation-recurrence detector).  With no
credentialed provider the pipeline degrades to a structural scan and mints
**nothing** — which is a finding about the product, not about the fixture, and
it is recorded as one.

But the surfaces downstream of discovery — the Discovered queue, the drawer,
App detail's bots × facts table, promotion — cannot be looked at at all on an
empty pod.  So this module supplies the *detections* the LLM would have made,
from the fixture's own on-disk evidence, and then hands them to the scanner's
own ``_stub_manifest`` writer and the real ``migrate_manifest`` normaliser.
Everything from Phase 3 onward is the product's code on the product's schema;
only the Phase-2 output is substituted.

Anything read from a manifest seeded here is therefore trustworthy as *shape*
and as *presentation*, and says nothing about whether a real scan would have
found these apps.  Timing claims must come from a real scan, never from here.

Usage::

    python3 -m tests.fixtures.pod.seed_drafts --root /tmp/fixture-pod
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # package import (python -m tests.fixtures.pod.seed_drafts)
    from .build import OPS_BOT, PRIMARY_BOT, TEAM_BOT
except ImportError:  # direct script run — `tests` is shadowed by
    # packages/admin/tests/, so the package form is not always available.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build import OPS_BOT, PRIMARY_BOT, TEAM_BOT  # type: ignore


def _detection(**kw):
    from evolve_admin.applications.scanner import DetectedApplication

    kw.setdefault("suggested_goals", [])
    kw.setdefault("suggested_tests", [])
    kw.setdefault("suggested_privacy", [])
    kw.setdefault("source", "discovered")
    return DetectedApplication(**kw)


def _drafts_for(bot_id: str, ws: Path) -> list:
    """The detections a working Phase 2 would have returned for this bot."""
    if bot_id == PRIMARY_BOT:
        return [
            _detection(
                id="morning-brief",
                name="Morning Brief",
                confidence=0.94,
                description="Assembles calendar, inbox and weather into one "
                            "message delivered before the day starts",
                evidence_files=[
                    "morning-brief/brief.py",
                    "morning-brief/config.json",
                    "morning-brief/README.md",
                    "memory/morning-brief-log.md",
                ],
                evidence_summary="A scheduled script with its own config and a "
                                 "delivery log going back 214 days.",
                suggested_goals=[
                    "Deliver one message before 07:00 local",
                    "Skip a section rather than fail when a source is down",
                ],
                suggested_tests=["No delivery recorded for a weekday"],
                suggested_privacy=["Reads mail and calendar; never forwards them"],
            ),
            _detection(
                id="medication-reminder",
                name="Medication Reminder",
                confidence=0.88,
                description="Asks each evening whether the medication was taken "
                            "and keeps the answer",
                evidence_files=["meds/reminder.py", "memory/medications.md"],
                evidence_summary="A nightly prompt script and a 214-day log of "
                                 "yes/no answers.",
                suggested_goals=["Ask once each evening", "Record the answer"],
                suggested_tests=["A day with no recorded answer"],
                suggested_privacy=["Health information — stays on this machine"],
            ),
            _detection(
                id="evening-journal",
                name="Evening Journal",
                confidence=0.71,
                description="Puts one reflective question each evening and files "
                            "the answer by month",
                evidence_files=["journal/prompt.py", "journal/entries"],
                evidence_summary="A prompt script and six months of monthly "
                                 "entry files.",
                suggested_goals=["One prompt per evening"],
                suggested_tests=["An empty month file"],
                suggested_privacy=["Personal writing — never leaves the pod"],
            ),
        ]
    if bot_id == TEAM_BOT:
        return [
            _detection(
                id="standup-digest",
                name="Standup Digest",
                confidence=0.91,
                description="Collects the team's async standup replies and posts "
                            "one digest",
                evidence_files=[
                    "standup/digest.py",
                    "standup/roster.json",
                    "memory/standup-log.md",
                ],
                evidence_summary="A weekday-scheduled script, a roster, and a "
                                 "151-day posting log.",
                suggested_goals=["Post one digest per weekday morning"],
                suggested_tests=["A weekday with no digest posted"],
                suggested_privacy=["Reads one channel only"],
            ),
            _detection(
                id="oncall-rota",
                name="On-call Rota",
                confidence=0.83,
                description="Answers who is on call and rolls the rota every "
                            "Monday",
                evidence_files=["oncall/rota.py", "oncall/rota.json"],
                evidence_summary="A rota file whose current holder changes "
                                 "weekly, plus the script that rolls it.",
                suggested_goals=["Always able to name the current on-call"],
                suggested_tests=["Rota unchanged across two Mondays"],
                suggested_privacy=["Names of team members only"],
            ),
            _detection(
                id="incident-log",
                name="Incident Log",
                confidence=0.62,
                description="Opens an incident thread, pages the on-call, and "
                            "keeps the running log",
                evidence_files=["incidents/log.md", "incidents/triage.sh"],
                evidence_summary="A weekly-appended log and a triage script; no "
                                 "schedule — it runs when something breaks.",
                suggested_goals=["Every incident has a log line"],
                suggested_tests=["A page with no matching log line"],
                suggested_privacy=[],
            ),
            # The same app the personal bot has. This is the pod-plane moment:
            # one app, two bots, from one row.
            _detection(
                id="morning-brief",
                name="Morning Brief",
                confidence=0.77,
                description="Assembles the team's calendar and channel highlights "
                            "into one message before the day starts",
                evidence_files=["standup/roster.json", "memory/standup-log.md"],
                evidence_summary="Same delivery shape as the personal bot's, "
                                 "pointed at the team channel.",
                suggested_goals=["Deliver one message before 09:00 local"],
                suggested_tests=["No delivery recorded for a weekday"],
                suggested_privacy=["Reads one channel only"],
            ),
        ]
    return [
        _detection(
            id="release-notes",
            name="Release Notes",
            confidence=0.79,
            description="Drafts the week's release notes from merged pull "
                        "requests",
            evidence_files=["release-notes/collect.py", "memory/release-notes.md"],
            evidence_summary="A Friday-scheduled script and 96 days of drafts.",
            suggested_goals=["A draft every Friday"],
            suggested_tests=["A Friday with no draft"],
            suggested_privacy=[],
        ),
        _detection(
            id="repo-watch",
            name="Repo Watch",
            confidence=0.68,
            description="Notices when a tracked repository's default branch "
                        "starts failing",
            evidence_files=["repo-watch/watch.py", "repo-watch/tracked.json"],
            evidence_summary="A half-hourly script over two tracked repos.",
            suggested_goals=["Announce a red default branch within an hour"],
            suggested_tests=["A red branch that went unannounced"],
            suggested_privacy=[],
        ),
    ]


#: Schedules the OpenClaw cron store already carries for these apps, restated
#: on the manifest because that is where the app surfaces read them.
SCHEDULES = {
    ("morning-brief", PRIMARY_BOT): ("45 6 * * *", "morning-brief/brief.py"),
    ("medication-reminder", PRIMARY_BOT): ("0 20 * * *", "meds/reminder.py"),
    ("evening-journal", PRIMARY_BOT): ("30 21 * * *", "journal/prompt.py"),
    ("standup-digest", TEAM_BOT): ("45 9 * * 1-5", "standup/digest.py"),
    ("oncall-rota", TEAM_BOT): ("0 9 * * 1", "oncall/rota.py"),
    ("morning-brief", TEAM_BOT): ("45 8 * * 1-5", "standup/digest.py"),
    ("release-notes", OPS_BOT): ("0 16 * * 5", "release-notes/collect.py"),
    ("repo-watch", OPS_BOT): ("*/30 * * * *", "repo-watch/watch.py"),
}


def seed(root: Path) -> dict:
    from evolve_admin.applications import scanner
    from evolve_admin.applications.layer_classifier import (
        apply_to_manifest as classify_layers,
    )
    from evolve_admin.applications.manifest import migrate_manifest
    from evolve_config import bot_home

    net = json.loads((root / "shared" / "network.json").read_text())
    written: dict[str, list[str]] = {}
    now = datetime.now(timezone.utc)

    for bot_id in net["members"]:
        home = bot_home(bot_id, net)
        ws = home / ".openclaw" / "workspace"
        out_dir = ws / "manifests"
        out_dir.mkdir(parents=True, exist_ok=True)
        stems: list[str] = []
        for i, det in enumerate(_drafts_for(bot_id, ws)):
            data = scanner._stub_manifest(det, bot_id)
            # A scanner discovery is born un-vouched-for; that is what puts it
            # on Discovered rather than Apps.
            data["definition_status"] = "discovered"
            data["source"] = "discovered"
            data["source_detail"] = now.isoformat().replace("+00:00", "Z")
            # Stagger creation so the queue is not one timestamp.
            data["created_at"] = (
                (now - timedelta(hours=i * 3 + 1)).isoformat().replace("+00:00", "Z")
            )
            schedule = SCHEDULES.get((det.id, bot_id))
            if schedule:
                when, script = schedule
                data["crons"] = [{"schedule": when, "script": script, "label": det.id}]
            path = out_dir / f"{det.id}.json"
            path.write_text(json.dumps(data, indent=2) + "\n")
            # Phase 5 for real: resolve the evidence to on-disk paths, assign
            # pkg_id/file_ids, infer each file's layer, and embed the
            # provenance markers. Skipping it would leave ``files[]`` empty,
            # which is what the readiness scorer reads — the queue would score
            # every draft lower than a real scan would.
            scanner._stamp_discovered_files(
                json.loads(path.read_text()), ws, path
            )
            migrate_manifest(path)
            # Phase 5.5: re-classify each file against the v20 layer enum.
            # The Phase-5 stamper infers ``script``/``data`` from the
            # extension; only this pass turns ``script`` into ``code``, which
            # is the value the readiness scorer's implementation rung reads.
            data_on_disk = json.loads(path.read_text())
            if classify_layers(data_on_disk):
                path.write_text(json.dumps(data_on_disk, indent=2) + "\n")
            stems.append(path.stem)
        written[bot_id] = stems
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed discovered drafts into a fixture pod")
    ap.add_argument("--root", required=True)
    args = ap.parse_args()
    print(json.dumps(seed(Path(args.root)), indent=2))


if __name__ == "__main__":
    main()
