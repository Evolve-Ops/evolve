"""board_import.py — D-TM9's parity gate and migration, plus the operator CLI.

Design: ``internal/design-pa-tasks-and-follow-through-2026-09-18.md`` D-TM9:
*parity first, then the store moves*. Two operator verbs, both run by hand —
nothing here is scheduled and nothing touches a live pod on its own:

    sudo evolve-admin project create-list --bot B --name N --prefix MN --members '[…]' --project '{…}'
    sudo evolve-admin project parity --bot B --list L [--days 7] [--history F] [--tasks-json F]
    sudo evolve-admin project import --bot B --list L --tasks-json F [--apply]
    sudo evolve-admin project tick --bot B --list L
    sudo evolve-admin project handoff --bot B --card C --list L --member M

``parity`` replays the last *days* of the list's Slack channel through the
Project Manager's ingest into a THROWAWAY store (a temp dir; the live store
is only read), renders the weekly report as of the old app's latest post,
and prints the unified diff against that post. Exit 0 = empty diff. The
optional ``--tasks-json`` seeds the throwaway store with the old app's open
items created before the window — the backlog a seven-day replay cannot see.

``import`` is :func:`from_task_manager`: a dry run by default (prints the
plan); ``--apply`` writes the cards and renames the bot's ``tasks.json`` to
``tasks.json.imported-<date>`` so the old script can no longer see a live
store. Idempotent by source id (``task-manager:<old id>``).

This module is a State service (it writes cards through ``board_store``, the
single writer); the Project Manager app it drives is ``board_project``.
"""
from __future__ import annotations

import difflib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from . import board_project as pm
from . import board_store as bs

#: Old status → the project card's working status. Closed ones are not imported.
_STATUS = {"open": "open", "in_progress": "in_progress", "blocked": "blocked",
           "needs_review": "in_progress"}
_CLOSED = ("complete", "cancelled")


def _norm_ts(value: Any) -> str | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assignee(lst: dict[str, Any], owner: Any, bot_id: str) -> str | None:
    owner = str(owner or "").strip().lower()
    if not owner:
        return None
    if owner in (bot_id.lower(), "bot"):
        return "bot"
    for m in lst.get("members") or []:
        if owner in (m["id"].lower(), m["display"].lower()):
            return m["id"]
    return None


def plan_import(
    board: dict[str, Any], bot_id: str, list_id: str, tasks: list[dict[str, Any]],
    *, created_before: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per task: ``{old_id, action, card?}`` where action is
    ``create`` or ``skip: <why>``. Pure — reads the board, writes nothing."""
    lst = next(x for x in board["lists"] if x["list_id"] == list_id)
    prefix = lst.get("id_prefix")
    seen = {c.get("source_id") for c in board["cards"]}
    plan = []
    for t in tasks:
        old = str(t.get("id") or "")
        created = _norm_ts(t.get("created_date"))
        row: dict[str, Any] = {"old_id": old}
        if t.get("status") in _CLOSED:
            row["action"] = f"skip: {t.get('status')}"
        elif not old.startswith(f"{prefix}-"):
            row["action"] = f"skip: prefix {old.split('-')[0] or '?'} belongs to another list"
        elif f"task-manager:{old}" in seen:
            row["action"] = "skip: already imported"
        elif created_before and created and created >= created_before.strftime("%Y-%m-%dT%H:%M:%SZ"):
            row["action"] = "skip: inside the replay window"
        else:
            sev = t.get("severity")
            owner = t.get("owner")
            who = _assignee(lst, owner, bot_id)
            note = (t.get("description") or "").strip()
            extras = [f"priority {t['priority']}" if t.get("priority") else "",
                      f"owner {owner} (not a list member)" if owner and who is None else ""]
            note = " · ".join(x for x in [note, *extras] if x)[:bs.MAX_NOTE_CHARS]
            due = t.get("due_date") or (str(t.get("expires") or "")[:10] or None)
            row["action"] = "create"
            row["card"] = {
                "title": str(t.get("title") or t.get("name") or old)[:bs.MAX_TITLE_CHARS],
                "note": note, "human_id": old, "source_id": f"task-manager:{old}",
                "severity": sev if sev in bs.SEVERITIES else None,
                "due": due if bs._parse_date(due) else None,  # noqa: SLF001
                "reporter": t.get("reporter") or t.get("created_by"),
                "area": t.get("area"), "created_at": created,
                "pm_status": _STATUS.get(t.get("status") or "open", "open"),
                "assignee": who,
            }
        plan.append(row)
    return plan


def from_task_manager(
    shared_dir: Path, bot_id: str, tasks_json: Path | dict[str, Any], *, list_id: str,
    apply: bool = False, rename: bool = True, created_before: datetime | None = None,
    actor: str = "operator", today: str | None = None,
) -> list[dict[str, Any]]:
    """D-TM9 migration: open Task Manager items → cards on *list_id*, ids
    preserved, idempotent by source id. Dry run unless *apply*; on apply
    with *rename*, ``tasks.json`` becomes ``tasks.json.imported-<date>``."""
    data = tasks_json if isinstance(tasks_json, dict) else json.loads(
        Path(tasks_json).read_text(encoding="utf-8"))
    raw = data.get("tasks")
    tasks: list[dict[str, Any]] = raw if isinstance(raw, list) else list((raw or {}).values())
    plan = plan_import(bs.load_board(shared_dir, bot_id), bot_id, list_id, tasks,
                       created_before=created_before)
    if not apply:
        return plan
    # Closed items are not imported, but their numbers were quoted once —
    # the list's counter moves past every id the old file ever minted.
    board = bs.load_board(shared_dir, bot_id)
    lst = next(x for x in board["lists"] if x["list_id"] == list_id)
    nums = [int(r["old_id"].split("-")[1]) for r in plan
            if r["old_id"].startswith(f"{lst.get('id_prefix')}-") and r["old_id"].split("-")[1].isdigit()]
    if nums and max(nums) >= int(lst.get("next_seq") or 1):
        lst["next_seq"] = max(nums) + 1
        bs.save_board(shared_dir, bot_id, board)
    for row in plan:
        if row["action"] != "create":
            continue
        c = dict(row["card"])
        to_bot = c.pop("assignee") == "bot"
        card = bs.create_card(shared_dir, bot_id, cluster="admin", source="task-manager",
                              actor=actor, list_id=list_id, **c,
                              assignee=None if to_bot else row["card"]["assignee"])
        if to_bot:
            pm.handoff_to_bot(shared_dir, bot_id, card["id"], actor=actor)
    if rename and not isinstance(tasks_json, dict):
        src = Path(tasks_json)
        stamp = today or f"{datetime.now():%Y-%m-%d}"
        src.rename(src.with_name(f"{src.name}.imported-{stamp}"))
    return plan


def parity_diff(
    live_board: dict[str, Any], bot_id: str, list_id: str, messages: list[dict[str, Any]],
    *, days: int = 7, tasks: dict[str, Any] | None = None, tz: Any = timezone.utc,
) -> tuple[str, str, str]:
    """``(old_report, new_report, diff)`` — the replay described in the
    module docstring, into a temp store. Raises ``ValueError`` when the
    messages hold no old report to compare against."""
    lst = next(x for x in live_board["lists"] if x["list_id"] == list_id)
    old = pm.find_old_report(messages, pm.project_config(lst))
    if old is None:
        raise ValueError("no weekly report from the old app in the replayed history")
    now = datetime.fromtimestamp(float(old["ts"]), tz=timezone.utc)
    since = now - timedelta(days=days)
    with tempfile.TemporaryDirectory(prefix="evolve-parity-") as tmp:
        shadow = Path(tmp)
        rec = bs.create_list(shadow, bot_id, name=lst["name"], shape="project",
                             members=lst.get("members"), id_prefix=lst.get("id_prefix"),
                             defaults=lst.get("defaults"), actor="parity")
        if tasks is not None:
            from_task_manager(shadow, bot_id, tasks, list_id=rec["list_id"], apply=True,
                              rename=False, created_before=since)
        window = [m for m in messages if since.timestamp() <= float(m["ts"]) < float(old["ts"])]
        pm.ingest(shadow, bot_id, rec["list_id"], window, now=now)
        new = pm.render_weekly_report(bs.load_board(shadow, bot_id), rec["list_id"],
                                      now=now, tz=tz)
    diff = "".join(difflib.unified_diff(
        (old["text"] + "\n").splitlines(keepends=True), (new + "\n").splitlines(keepends=True),
        "old-app", "new-list"))
    return old["text"], new, diff


# ── operator CLI (`evolve-admin project …`) ─────────────────────────────────


def _ctx(ctx: click.Context) -> tuple[Path, dict[str, Any], Any]:
    from zoneinfo import ZoneInfo

    from .config import CANONICAL_SHARED_DIR, DEFAULT_NETWORK_CONFIG, load_network, resolve_pod_timezone
    net = load_network((ctx.obj or {}).get("network_path") or DEFAULT_NETWORK_CONFIG)
    return Path(net.get("sharedDir", CANONICAL_SHARED_DIR)), net, ZoneInfo(resolve_pod_timezone(net))


@click.group("project")
def project_group() -> None:
    """Project Manager lists on the Tracker: parity, import, tick (D-TM9)."""


@project_group.command("create-list")
@click.option("--bot", "bot_id", required=True)
@click.option("--name", required=True)
@click.option("--prefix", required=True, help="Human id prefix, e.g. MN")
@click.option("--members", default="[]", help='JSON: [{"id","display","slack_user"}]')
@click.option("--project", "project", default="{}", help="JSON: defaults.project keys")
@click.option("--nag-days", type=int, default=pm.DEFAULT_NAG_DAYS)
@click.pass_context
def create_list_cmd(ctx, bot_id, name, prefix, members, project, nag_days) -> None:
    shared, _, _ = _ctx(ctx)
    rec = bs.create_list(shared, bot_id, name=name, shape="project", id_prefix=prefix,
                         members=json.loads(members), actor="operator",
                         defaults={"nag_days": nag_days, "project": json.loads(project)})
    click.echo(f"created project list {rec['list_id']} ({prefix}) on {bot_id}")


@project_group.command("parity")
@click.option("--bot", "bot_id", required=True)
@click.option("--list", "list_id", required=True)
@click.option("--days", type=int, default=7)
@click.option("--history", type=click.Path(exists=True, dir_okay=False),
              help="conversations.history JSON instead of reading Slack")
@click.option("--tasks-json", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def parity_cmd(ctx, bot_id, list_id, days, history, tasks_json) -> None:
    """Diff the new list's weekly report against the old app's last post."""
    from . import app_contract as ac
    shared, net, tz = _ctx(ctx)
    board = bs.load_board(shared, bot_id)
    lst = next(x for x in board["lists"] if x["list_id"] == list_id)
    if history:
        raw = json.loads(Path(history).read_text(encoding="utf-8"))
        msgs = raw.get("messages", raw) if isinstance(raw, dict) else raw
    else:
        oldest = (datetime.now(timezone.utc) - timedelta(days=days + 7)).timestamp()
        msgs = ac.read_history(bot_id, net, pm.project_config(lst)["channel"], oldest)
    tasks = json.loads(Path(tasks_json).read_text(encoding="utf-8")) if tasks_json else None
    _, _, diff = parity_diff(board, bot_id, list_id, msgs, days=days, tasks=tasks, tz=tz)
    click.echo(diff or "parity: no difference")
    sys.exit(1 if diff else 0)


@project_group.command("import")
@click.option("--bot", "bot_id", required=True)
@click.option("--list", "list_id", required=True)
@click.option("--tasks-json", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--apply", is_flag=True, help="write the cards (default: dry run)")
@click.pass_context
def import_cmd(ctx, bot_id, list_id, tasks_json, apply) -> None:
    """Import a bot's open Task Manager items (dry run unless --apply)."""
    shared, _, _ = _ctx(ctx)
    plan = from_task_manager(shared, bot_id, Path(tasks_json), list_id=list_id, apply=apply)
    for row in plan:
        c = row.get("card") or {}
        click.echo(f"{row['old_id']:<10} {row['action']:<40} {c.get('title', '')[:60]}")
    click.echo(f"{sum(r['action'] == 'create' for r in plan)} to create"
               + ("" if apply else " (dry run — rerun with --apply)"))


@project_group.command("tick")
@click.option("--bot", "bot_id", required=True)
@click.option("--list", "list_id", required=True)
@click.pass_context
def tick_cmd(ctx, bot_id, list_id) -> None:
    """One ingest + nags + (when due) weekly-report run for a list."""
    shared, net, tz = _ctx(ctx)
    click.echo(json.dumps(pm.tick(shared, bot_id, list_id, net, tz=tz)))


@project_group.command("handoff")
@click.option("--bot", "bot_id", required=True)
@click.option("--card", "card_id", required=True, help="assistant card id (or prefix)")
@click.option("--list", "list_id", required=True)
@click.option("--member", required=True, help="list member id")
@click.pass_context
def handoff_cmd(ctx, bot_id, card_id, list_id, member) -> None:
    """D-TM13: hand an assistant card to a project-list member (linked both ways)."""
    shared, _, _ = _ctx(ctx)
    card = bs.resolve_card(bs.load_board(shared, bot_id), card_id)
    item = pm.handoff_to_member(shared, bot_id, card["id"], list_id, member, actor="operator")
    click.echo(f"{item['human_id']} assigned to {member}, linked to {card['id'][:8]}")
