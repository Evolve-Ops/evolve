#!/usr/bin/env python3
"""build — materialise a stranger's multi-bot pod as a real directory tree.

Why this exists
---------------
Every Evolve surface that matters to a new operator (the Apps page, the
Discovered queue, Usage, the Fleet rollups) reads *bot state* — files under
each bot's ``~/.openclaw/workspace``, plus pod state under ``{shared_dir}``.
Until this module there was no way to stand any of that up outside a real
multi-account host: ``tests/browser/fixtures/network.json`` describes a pod
with **zero** bots, and every populated Apps test stubs the HTTP reads.  That
made the one journey the alpha gate turns on — *install onto an existing
multi-bot OpenClaw pod and look at it* — impossible to rehearse anywhere but
the maintainer's live pod.

So: this builds a fixture pod.  Three bots with the shape a real OpenClaw
operator's pod has after months of use — workspaces with app-ish directories,
scripts, recurring memory logs, JSON data stores, OpenClaw cron jobs, session
turn files — plus the pod-side shared dir (network.json, signals, proposals,
annotations).  It writes only under the root it is given.

What is real and what is not
----------------------------
Real: every path, every file, and every reader.  The admin server, the
application scanner, and the analyzer all walk this tree with their own code.

Not real: the *location*.  Bot homes live under ``<root>/homes/<bot>`` rather
than ``/Users/<bot>``, because creating real accounts needs root.  The
redirect is one call to ``platform_profile.set_profile`` — the product's own
seam — performed by the sibling ``sitecustomize`` module.  Nothing else is
faked.

Also not real: OpenClaw itself.  There is no gateway process behind these
homes, so anything that shells out to ``openclaw`` (or to ``crontab -l``, or
to ``sudo``) gets the same empty answer it would get on a pod whose bots are
stopped.  That is a limitation of the fixture *and* a fact worth watching:
where a surface degrades to silence rather than saying so, the fixture shows
it.

Usage::

    python3 -m tests.fixtures.pod.build --root /tmp/fixture-pod
    python3 -m tests.fixtures.pod.build --root /tmp/fixture-pod --age-days 7

``--age-days`` shifts the pod's whole history back by N days, which is how the
"what does this look like after a week of accumulation" question gets asked
without waiting a week.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── The pod's shape ─────────────────────────────────────────────────────────
#
# Role placeholders per docs/PLACEHOLDER_NAMING.md — never a real bot name.

PRIMARY_BOT = "personal-bot"
TEAM_BOT = "team-bot-a"
OPS_BOT = "admin-bot"
BOTS = (PRIMARY_BOT, TEAM_BOT, OPS_BOT)

PORTS = {PRIMARY_BOT: 19000, TEAM_BOT: 19001, OPS_BOT: 19002}

#: How long each bot has supposedly been running before Evolve arrives.
#: A stranger's pod is not a fresh pod — that is the whole premise.
HISTORY_DAYS = {PRIMARY_BOT: 214, TEAM_BOT: 151, OPS_BOT: 96}


def fixture_bot_ids_are_safe() -> None:
    """Refuse to build if a fixture bot id is a real account on this host.

    ``evolve_config.bot_home`` resolves through ``pwd.getpwnam`` FIRST and
    only falls back to ``{user_home_root}/{user}`` on ``KeyError``.  If one of
    these ids happened to name a real account, the harness would quietly read
    and write that person's home instead of the fixture root.  Fail loudly
    instead.
    """
    import pwd

    for bot in BOTS:
        try:
            pwd.getpwnam(bot)
        except KeyError:
            continue
        raise SystemExit(
            f"refusing to build: '{bot}' is a real account on this host, so "
            "bot_home() would resolve to its real home instead of the "
            "fixture root. Rename the fixture bot before continuing."
        )


# ── Small writers ───────────────────────────────────────────────────────────


def _w(path: Path, text: str, *, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)
    return path


def _wj(path: Path, obj) -> Path:
    return _w(path, json.dumps(obj, indent=2) + "\n")


def _iso(day: date, hour: int = 9, minute: int = 0) -> str:
    return datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc
    ).isoformat().replace("+00:00", "Z")


# ── Bot workspaces ──────────────────────────────────────────────────────────


def _openclaw_json(bot: str, home: Path) -> dict:
    """A bot's openclaw.json, in the shape ``resolve_bot_paths`` reads."""
    return {
        "agents": {
            "defaults": {"workspace": str(home / ".openclaw" / "workspace")},
            "list": [
                {"id": "main", "agentDir": str(home / ".openclaw" / "agents" / "main" / "agent")}
            ],
        },
        "gateway": {"port": PORTS[bot]},
        "channels": {"telegram": {"enabled": True}},
    }


def _identity_files(ws: Path, bot: str, blurb: str, user_blurb: str) -> None:
    """The OpenClaw identity corpus every real workspace carries."""
    _w(ws / "SOUL.md", f"# Soul\n\n{blurb}\n")
    _w(ws / "USER.md", f"# The person I work for\n\n{user_blurb}\n")
    _w(
        ws / "AGENTS.md",
        f"# Agent conventions — {bot}\n\n"
        "## Memory\nAppend dated entries; never rewrite history.\n\n"
        "## Tone\nShort. Concrete. No preamble.\n",
    )
    _w(
        ws / "MEMORY.md",
        "# Memory index\n\n"
        "- Standing preferences live in USER.md.\n"
        "- Long-running logs live under memory/.\n",
    )
    _w(
        ws / "HEARTBEAT.md",
        "# Heartbeat\n\nEvery 30 minutes: check for anything overdue and say so.\n",
    )


def _log_lines(start: date, days: int, fmt: str, *, every: int = 1) -> str:
    """A dated, recurring markdown log — the scanner's 'recurring file' signal."""
    out = []
    for i in range(0, days, every):
        d = start + timedelta(days=i)
        out.append(fmt.format(date=d.isoformat()))
    return "\n".join(out) + "\n"


def _build_personal_bot(home: Path, today: date) -> None:
    ws = home / ".openclaw" / "workspace"
    start = today - timedelta(days=HISTORY_DAYS[PRIMARY_BOT])
    _identity_files(
        ws,
        PRIMARY_BOT,
        "A household assistant. Runs the morning brief, keeps the journal, "
        "and nags about the things that get forgotten.",
        "Works from home. Reads the brief at 06:45 with coffee. Hates being "
        "asked to confirm things twice.",
    )

    # ── Morning brief: cron + script + config + recurring log ───────────────
    _w(
        ws / "morning-brief" / "brief.py",
        '#!/usr/bin/env python3\n'
        '"""Assemble the morning brief: calendar, inbox, weather, commitments."""\n'
        "import json, pathlib\n\n"
        "CONFIG = pathlib.Path(__file__).with_name('config.json')\n"
        "LOG = pathlib.Path.home() / '.openclaw/workspace/memory/morning-brief-log.md'\n\n\n"
        "def build_sections(cfg):\n"
        "    sections = []\n"
        "    if cfg.get('calendar'):\n"
        "        sections.append(fetch_calendar(cfg['calendar']))\n"
        "    if cfg.get('inbox'):\n"
        "        sections.append(fetch_inbox(cfg['inbox']))\n"
        "    if cfg.get('weather'):\n"
        "        sections.append(fetch_weather(cfg['weather']))\n"
        "    return sections\n\n\n"
        "def deliver(sections):\n"
        "    text = '\\n\\n'.join(s for s in sections if s)\n"
        "    send_telegram(text)\n"
        "    LOG.open('a').write(f'- delivered {len(sections)} sections\\n')\n",
        mode=0o755,
    )
    _wj(
        ws / "morning-brief" / "config.json",
        {
            "deliver_at": "06:45",
            "timezone": "America/Los_Angeles",
            "calendar": {"account": "primary", "lookahead_hours": 36},
            "inbox": {"account": "primary", "only_unread": True},
            "weather": {"location": "home"},
        },
    )
    _w(
        ws / "morning-brief" / "README.md",
        "# Morning brief\n\nDelivered at 06:45 local. Sections are skipped "
        "silently when their source is unavailable.\n",
    )
    _w(
        ws / "memory" / "morning-brief-log.md",
        "# Morning brief delivery log\n\n"
        + _log_lines(start, HISTORY_DAYS[PRIMARY_BOT], "- {date} delivered 3 sections"),
    )

    # ── Medication reminders ────────────────────────────────────────────────
    _w(
        ws / "meds" / "reminder.py",
        '#!/usr/bin/env python3\n'
        '"""Remind about the evening medication and record whether it was taken."""\n'
        "import datetime, pathlib\n\n"
        "LOG = pathlib.Path.home() / '.openclaw/workspace/memory/medications.md'\n\n\n"
        "def remind():\n"
        "    ask('Evening meds — taken?')\n\n\n"
        "def record(answer):\n"
        "    stamp = datetime.date.today().isoformat()\n"
        "    LOG.open('a').write(f'- {stamp} {answer}\\n')\n",
        mode=0o755,
    )
    _w(
        ws / "memory" / "medications.md",
        "# Medication log\n\n"
        + _log_lines(start, HISTORY_DAYS[PRIMARY_BOT], "- {date} taken"),
    )

    # ── Journal ─────────────────────────────────────────────────────────────
    _w(
        ws / "journal" / "prompt.py",
        '#!/usr/bin/env python3\n'
        '"""Ask the evening journal question and file the answer by month."""\n'
        "import datetime, pathlib\n\n"
        "ENTRIES = pathlib.Path(__file__).with_name('entries')\n\n\n"
        "def tonight():\n"
        "    return pick_prompt(datetime.date.today())\n\n\n"
        "def file_answer(text):\n"
        "    month = datetime.date.today().strftime('%Y-%m')\n"
        "    (ENTRIES / f'{month}.md').open('a').write(text + '\\n')\n",
        mode=0o755,
    )
    for i in range(6):
        month = (today.replace(day=1) - timedelta(days=31 * i)).strftime("%Y-%m")
        _w(
            ws / "journal" / "entries" / f"{month}.md",
            f"# {month}\n\n"
            + "\n".join(f"- day {d}: wrote a few lines." for d in range(1, 26))
            + "\n",
        )

    # ── Infrastructure that discovery is SUPPOSED to filter out ─────────────
    _w(
        ws / "bin" / "gateway-selfheal.sh",
        "#!/bin/sh\n# gateway self-heal — restart the openclaw gateway if the "
        "health probe fails.\ncurl -sf http://127.0.0.1:19000/health || "
        "openclaw gateway restart\n",
        mode=0o755,
    )
    _w(
        ws / "evolve" / "collect.py",
        "# Evolve platform file — not a user application.\n",
    )

    # ── OpenClaw's own cron store — where a real operator's schedules live ──
    _wj(
        home / ".openclaw" / "cron" / "jobs.json",
        {
            "jobs": [
                {
                    "id": "morning-brief",
                    "name": "morning-brief",
                    "schedule": "45 6 * * *",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "morning-brief" / "brief.py"),
                    },
                },
                {
                    "id": "evening-meds",
                    "name": "evening-meds",
                    "schedule": "0 20 * * *",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "meds" / "reminder.py"),
                    },
                },
                {
                    "id": "journal-prompt",
                    "name": "journal-prompt",
                    "schedule": "30 21 * * *",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "journal" / "prompt.py"),
                    },
                },
            ]
        },
    )


def _build_team_bot(home: Path, today: date) -> None:
    ws = home / ".openclaw" / "workspace"
    start = today - timedelta(days=HISTORY_DAYS[TEAM_BOT])
    _identity_files(
        ws,
        TEAM_BOT,
        "The team's bot in Slack. Runs the standup digest, keeps the incident "
        "log, and answers 'who is on call'.",
        "A four-person team in one Slack channel. Standup is async at 09:30.",
    )

    _w(
        ws / "standup" / "digest.py",
        '#!/usr/bin/env python3\n'
        '"""Collect yesterday\'s standup replies and post one digest."""\n'
        "import json, pathlib\n\n"
        "ROSTER = pathlib.Path(__file__).with_name('roster.json')\n"
        "LOG = pathlib.Path.home() / '.openclaw/workspace/memory/standup-log.md'\n\n\n"
        "def collect(channel):\n"
        "    return [reply for reply in read_channel(channel) if is_standup(reply)]\n\n\n"
        "def post(digest, channel):\n"
        "    send_slack(channel, digest)\n"
        "    LOG.open('a').write('- posted\\n')\n",
        mode=0o755,
    )
    _wj(
        ws / "standup" / "roster.json",
        {
            "channel": "#team",
            "post_at": "09:45",
            "members": ["engineer-a", "engineer-b", "engineer-c", "designer-a"],
        },
    )
    _w(
        ws / "memory" / "standup-log.md",
        "# Standup digest log\n\n"
        + _log_lines(start, HISTORY_DAYS[TEAM_BOT], "- {date} posted digest (4 replies)"),
    )

    _w(
        ws / "incidents" / "log.md",
        "# Incident log\n\n"
        + _log_lines(start, HISTORY_DAYS[TEAM_BOT], "- {date} no incidents", every=7),
    )
    _w(
        ws / "incidents" / "triage.sh",
        "#!/bin/sh\n# Open an incident thread and page the on-call engineer.\n"
        "slack_post \"$1\" \"#incidents\"\npage_oncall \"$1\"\n",
        mode=0o755,
    )

    _w(
        ws / "oncall" / "rota.py",
        '#!/usr/bin/env python3\n'
        '"""Answer \'who is on call\' and roll the rota every Monday."""\n'
        "import json, pathlib\n\n"
        "ROTA = pathlib.Path(__file__).with_name('rota.json')\n\n\n"
        "def current():\n"
        "    return json.loads(ROTA.read_text())['current']\n\n\n"
        "def roll():\n"
        "    data = json.loads(ROTA.read_text())\n"
        "    order = data['order']\n"
        "    data['current'] = order[(order.index(data['current']) + 1) % len(order)]\n"
        "    ROTA.write_text(json.dumps(data, indent=2))\n",
        mode=0o755,
    )
    _wj(
        ws / "oncall" / "rota.json",
        {
            "current": "engineer-b",
            "order": ["engineer-a", "engineer-b", "engineer-c"],
            "rolls": "monday",
        },
    )

    _w(
        ws / "bin" / "sentry_ping.sh",
        "#!/bin/sh\n# liveness probe for the gateway watchdog\ncurl -sf "
        "http://127.0.0.1:19001/health\n",
        mode=0o755,
    )

    _wj(
        home / ".openclaw" / "cron" / "jobs.json",
        {
            "jobs": [
                {
                    "id": "standup-digest",
                    "name": "standup-digest",
                    "schedule": "45 9 * * 1-5",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "standup" / "digest.py"),
                    },
                },
                {
                    "id": "oncall-roll",
                    "name": "oncall-roll",
                    "schedule": "0 9 * * 1",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "oncall" / "rota.py"),
                    },
                },
            ]
        },
    )


def _build_ops_bot(home: Path, today: date) -> None:
    ws = home / ".openclaw" / "workspace"
    start = today - timedelta(days=HISTORY_DAYS[OPS_BOT])
    _identity_files(
        ws,
        OPS_BOT,
        "The pod's own errand-runner. Watches the repo, drafts release notes, "
        "and keeps the backups honest.",
        "The person who owns the machine. Wants to be told, not asked.",
    )

    _w(
        ws / "release-notes" / "collect.py",
        '#!/usr/bin/env python3\n'
        '"""Draft release notes from the week\'s merged pull requests."""\n'
        "import pathlib, subprocess\n\n"
        "LOG = pathlib.Path.home() / '.openclaw/workspace/memory/release-notes.md'\n\n\n"
        "def merged_since(ref):\n"
        "    out = subprocess.run(['git', 'log', '--merges', f'{ref}..HEAD'],\n"
        "                         capture_output=True, text=True)\n"
        "    return out.stdout.splitlines()\n\n\n"
        "def draft(lines):\n"
        "    LOG.open('a').write('\\n'.join(lines) + '\\n')\n",
        mode=0o755,
    )
    _w(
        ws / "memory" / "release-notes.md",
        "# Release notes drafts\n\n"
        + _log_lines(start, HISTORY_DAYS[OPS_BOT], "- {date} drafted (7 merges)", every=7),
    )

    _w(
        ws / "repo-watch" / "watch.py",
        '#!/usr/bin/env python3\n'
        '"""Notice when a tracked repository gets a failing default branch."""\n'
        "import json, pathlib\n\n"
        "TRACKED = pathlib.Path(__file__).with_name('tracked.json')\n\n\n"
        "def check(repo):\n"
        "    return latest_run_conclusion(repo)\n\n\n"
        "def announce(repo, conclusion):\n"
        "    if conclusion != 'success':\n"
        "        notify(f'{repo} default branch is {conclusion}')\n",
        mode=0o755,
    )
    _wj(
        ws / "repo-watch" / "tracked.json",
        {"repos": ["example-org/service-a", "example-org/service-b"], "poll_minutes": 30},
    )

    _w(
        ws / "bin" / "backup.sh",
        "#!/bin/sh\n# nightly backup of the shared dir\ntar czf "
        "\"$HOME/backup-$(date +%F).tar.gz\" \"$HOME/.openclaw/workspace\"\n",
        mode=0o755,
    )

    _wj(
        home / ".openclaw" / "cron" / "jobs.json",
        {
            "jobs": [
                {
                    "id": "release-notes",
                    "name": "release-notes",
                    "schedule": "0 16 * * 5",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "release-notes" / "collect.py"),
                    },
                },
                {
                    "id": "repo-watch",
                    "name": "repo-watch",
                    "schedule": "*/30 * * * *",
                    "enabled": True,
                    "payload": {
                        "kind": "shell",
                        "command": str(ws / "repo-watch" / "watch.py"),
                    },
                },
            ]
        },
    )


BUILDERS = {
    PRIMARY_BOT: _build_personal_bot,
    TEAM_BOT: _build_team_bot,
    OPS_BOT: _build_ops_bot,
}


# ── Sessions / turn history ─────────────────────────────────────────────────
#
# OpenClaw's own turn collector writes memory/turns-<date>.jsonl.  A pod that
# has been running for months has hundreds of these lines and NO Evolve
# attribution on any of them — Evolve was not installed when they were
# written.  That asymmetry is the point: it is what "before Evolve" looks
# like, and it is what the first Usage screen has to read.

#: Channels each bot actually talks on, so the mix is not uniform noise.
TURN_CHANNELS = {
    PRIMARY_BOT: ("telegram", "telegram", "telegram", "cron"),
    TEAM_BOT: ("slack", "slack", "slack", "cron"),
    OPS_BOT: ("telegram", "cron", "cron"),
}


def _write_oc_turns(ws: Path, bot: str, today: date, days: int, rng: random.Random) -> None:
    channels = TURN_CHANNELS[bot]
    for offset in range(days):
        day = today - timedelta(days=offset)
        lines = []
        for turn in range(rng.randint(3, 14)):
            channel = rng.choice(channels)
            lines.append(
                json.dumps(
                    {
                        "ts": _iso(day, hour=7 + (turn % 14), minute=(turn * 7) % 60),
                        "session_id": f"{bot}-{day.isoformat()}-{turn // 4}",
                        # The collector writes provider separately from the
                        # bare model id; the cost estimator needs BOTH to
                        # price a turn (a bare id with no provider prices at
                        # zero, which is how a busy bot reads as free).
                        "provider": "anthropic",
                        "model": rng.choice(
                            ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"]
                        ),
                        "input_tokens": rng.randint(900, 12000),
                        "output_tokens": rng.randint(80, 1400),
                        "cache_read_tokens": rng.choice([0, 0, rng.randint(2000, 40000)]),
                        "channel": channel,
                        # OC's own collector says "human"; the Evolve-side
                        # fallback writer says "user". Both appear in the
                        # wild — use the collector's word here, since this
                        # is history from BEFORE Evolve was installed.
                        "source": "cron" if channel == "cron" else "human",
                    }
                )
            )
        _w(ws / "memory" / f"turns-{day.isoformat()}.jsonl", "\n".join(lines) + "\n")


# ── Shared (pod) state ──────────────────────────────────────────────────────


def _network_json(root: Path) -> dict:
    return {
        "networkId": "fixture-pod",
        "primary": PRIMARY_BOT,
        "members": list(BOTS),
        "sharedDir": str(root / "shared"),
        "timezone": "America/Los_Angeles",
        "thresholds": {
            "dailySpendAlertUsd": 5.0,
            "weeklySpendAlertUsd": 20.0,
            "spendCapAction": "alert-only",
            "maxSessionContextTokens": 100000,
        },
        "alerts": {"channel": "telegram", "chatId": "0"},
        "bots": {
            PRIMARY_BOT: {"role": "primary", "port": PORTS[PRIMARY_BOT]},
            TEAM_BOT: {"role": "member", "port": PORTS[TEAM_BOT]},
            OPS_BOT: {"role": "member", "port": PORTS[OPS_BOT]},
        },
    }


SHARED_SUBDIRS = (
    "signals/firing",
    "signals/snoozed",
    "signals/archived",
    "signals/log",
    "proposals/pending",
    "proposals/snoozed",
    "proposals/applied",
    "proposals/archived",
    "generators",
    "profiles",
    "observations",
    "watchdog",
    "metrics",
    "annotations",
    "applications",
    "plists",
)


def _build_shared(root: Path, today: date) -> Path:
    shared = root / "shared"
    for sub in SHARED_SUBDIRS:
        (shared / sub).mkdir(parents=True, exist_ok=True)
    _wj(shared / "network.json", _network_json(root))
    for bot in BOTS:
        (shared / "annotations" / bot).mkdir(parents=True, exist_ok=True)
        (shared / bot / "turns").mkdir(parents=True, exist_ok=True)
    return shared


def build(root: Path, *, age_days: int = 0, seed: int = 20260823) -> dict:
    """Build the whole fixture pod under *root*. Returns a summary dict."""
    fixture_bot_ids_are_safe()
    rng = random.Random(seed)
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "scratch").mkdir()
    (root / "repo").mkdir()

    today = datetime.now(timezone.utc).date() - timedelta(days=age_days)
    shared = _build_shared(root, today)

    for bot in BOTS:
        home = root / "homes" / bot
        (home / ".openclaw" / "agents" / "main" / "agent").mkdir(parents=True, exist_ok=True)
        (home / ".openclaw" / "logs").mkdir(parents=True, exist_ok=True)
        _wj(home / ".openclaw" / "openclaw.json", _openclaw_json(bot, home))
        BUILDERS[bot](home, today)
        _write_oc_turns(
            home / ".openclaw" / "workspace", bot, today, min(HISTORY_DAYS[bot], 40), rng
        )
        # Manifests dir exists but is EMPTY — Evolve has never scanned here.
        (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True, exist_ok=True)

    return {
        "root": str(root),
        "shared": str(shared),
        "homes": str(root / "homes"),
        "network": str(shared / "network.json"),
        "bots": list(BOTS),
        "today": today.isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a fixture multi-bot OpenClaw pod")
    ap.add_argument("--root", required=True, help="Directory to build the pod under")
    ap.add_argument(
        "--age-days",
        type=int,
        default=0,
        help="Shift the pod's whole history back N days (simulate elapsed time)",
    )
    args = ap.parse_args()
    summary = build(Path(args.root), age_days=args.age_days)
    print(json.dumps(summary, indent=2))
    print(
        "\nRun the admin server against it with:\n"
        f"  EVOLVE_FIXTURE_POD_ROOT={summary['root']} \\\n"
        f"  PYTHONPATH={Path(__file__).parent}:$PYTHONPATH \\\n"
        f"  python -m evolve_admin.web.run --network {summary['network']}\n",
        file=os.sys.stderr,
    )


if __name__ == "__main__":
    main()
