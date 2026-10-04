"""app_contract.py — the app-facing contract v1, as data (D-AP1..3).

Design: ``internal/design-application-platform-2026-09-22.md`` §2 row 12, §4,
§5.1. The human-readable inventory is ``internal/design-app-contract-v1.md``;
this module holds the SAME rows as importable data, and
``tests/test_app_contract_inventory.py`` fails the moment the two disagree.
The plugin mirror is ``packages/plugin/src/apps/contract/rows.ts`` (same
names, kinds, versions, services, statuses — pinned by the same test).

WHAT THIS IS NOT. It is not a new API. Every row was written FROM the
personal assistant's shipped code and its queued briefs: a surface with no
caller is not a row. It is not an SDK either — there is no client shim here.

THREE THINGS LIVE HERE:

1. :data:`ROWS` — one :class:`ContractRow` per surface the PA calls (tool
   verbs, daemon endpoints, in-process store bindings, spec fields). Each
   carries a version, the platform service it belongs to (the twelve in the
   design's §2), an owner module, and a "works without Evolve?" line.
2. The **store bindings** — thin functions an in-daemon caller uses instead
   of importing another service's module directly. Each is one row, each is
   a pass-through (lazy import, no behaviour of its own), so a test that
   patches the owner module still patches what the binding calls.
3. :func:`require_route_rows` — the registration gate. A daemon endpoint the
   platform exposes to apps with no row here is REFUSED at registration:
   the call raises :class:`ContractRowMissing` naming the row it needs. The
   plugin's ``registerAppTool`` does the same for tool verbs.

The seam data the inventory test scans with (which service each PA module
belongs to, the known cross-service debt) is at the bottom — it is part of
the contract's honesty, not a separate registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The version every row below is written against. A breaking change to any
#: row's shape is a new version with its own rows, never an edit in place.
CONTRACT_VERSION = "v1"

#: The twelve platform services (design §2). Row ``service`` is a key here.
SERVICES: dict[int, str] = {
    1: "Identity & user management",
    2: "Cost",
    3: "Security & trust",
    4: "Usage reporting",
    5: "Intelligence views",
    6: "Lifecycle",
    7: "Execution",
    8: "State",
    9: "Delivery",
    10: "Signals",
    11: "Model selection",
    12: "The app-facing contract",
}

#: The five kinds of surface (brief item 1).
KINDS = ("tool_verb", "daemon_endpoint", "store_binding", "spec_field", "signal")

#: ``shipped`` = on main and callable; ``queued`` = a queued PA brief creates
#: it (the brief's id is ``introduced_by``). Nothing else is a row.
STATUSES = ("shipped", "queued")


@dataclass(frozen=True)
class ContractRow:
    name: str
    kind: str
    service: int
    owner: str
    works_without_evolve: str
    introduced_by: str
    used_by: tuple[str, ...] = ()
    status: str = "shipped"
    version: str = CONTRACT_VERSION


_BOARD_TOOL = "packages/plugin/src/tools/BoardTool.ts"
_BOARD_BOT_ROUTES = "packages/admin/evolve_admin/web/board_bot_routes.py"
_NO_TOOL = ("no — the `board` tool is registered only by the Evolve plugin; "
            "a bot without Evolve has no board to call")
_NO_DAEMON = ("no — the admin daemon is the store's only writer (D-MB1); "
              "the caller fails loudly without it (BOARD_FAILED, exit 1)")
_NO_INPROC = "no — an in-daemon binding; there is nothing to call without Evolve"
_BOARD_TOOL_CHIP = "board-tool-chat-parity-and-enrichment"
_BOARD_STORE = "packages/admin/evolve_admin/board_store.py"
_PM_CHIP = "project-manager-app-on-the-tracker-with-slack-parity"
_RECORDS_TOOL = "packages/plugin/src/tools/records.ts"
_RECORDS_ROUTES = "packages/admin/evolve_admin/web/records_bot_routes.py"
_RECORDS_CHIP = "app-store-and-ledger-verbs"
_NO_RECORDS_TOOL = ("no — the `records` tool is registered only by the Evolve plugin, "
                    "and the file it reaches is the daemon's; an app without Evolve has "
                    "no store to call")
_NO_RECORDS_DAEMON = ("no — the admin daemon is the store's only writer (D-AD2); "
                      "without it the caller gets a typed refusal and nothing is written")
_CAPTURE_CHIP = "capture-from-turns-is-gated-and-lands-as-a-proposal"

ROWS: tuple[ContractRow, ...] = (
    # ── tool verbs (the bot, in conversation) ────────────────────────────────
    ContractRow("board.list", "tool_verb", 8, _BOARD_TOOL, _NO_TOOL, _BOARD_TOOL_CHIP),
    ContractRow("board.add", "tool_verb", 8, _BOARD_TOOL, _NO_TOOL, _BOARD_TOOL_CHIP),
    ContractRow("board.move", "tool_verb", 8, _BOARD_TOOL, _NO_TOOL, _BOARD_TOOL_CHIP,
                used_by=("morning-board-gallery-app",)),
    ContractRow("board.assign", "tool_verb", 8, _BOARD_TOOL, _NO_TOOL, _BOARD_TOOL_CHIP,
                used_by=("morning-board-gallery-app",)),
    ContractRow("board.progress", "tool_verb", 8, _BOARD_TOOL, _NO_TOOL, _BOARD_TOOL_CHIP,
                used_by=("board-bot-lane-worker",)),
    # ── daemon endpoints (bot-facing; identity = socket peer uid) ────────────
    ContractRow("GET /api/board-bot/cards", "daemon_endpoint", 8, _BOARD_BOT_ROUTES,
                _NO_DAEMON, _BOARD_TOOL_CHIP,
                used_by=("board.list", "morning-board-gallery-app")),
    ContractRow("POST /api/board-bot/cards", "daemon_endpoint", 8, _BOARD_BOT_ROUTES,
                _NO_DAEMON, _BOARD_TOOL_CHIP,
                used_by=("board.add", "morning-board-gallery-app")),
    ContractRow("POST /api/board-bot/cards/<card_id>/move", "daemon_endpoint", 8,
                _BOARD_BOT_ROUTES, _NO_DAEMON, _BOARD_TOOL_CHIP, used_by=("board.move",)),
    ContractRow("POST /api/board-bot/cards/<card_id>/assign", "daemon_endpoint", 8,
                _BOARD_BOT_ROUTES, _NO_DAEMON, _BOARD_TOOL_CHIP, used_by=("board.assign",)),
    ContractRow("POST /api/board-bot/cards/<card_id>/progress", "daemon_endpoint", 8,
                _BOARD_BOT_ROUTES, _NO_DAEMON, _BOARD_TOOL_CHIP,
                used_by=("board.progress",)),
    ContractRow("POST /api/board-bot/briefing", "daemon_endpoint", 9, _BOARD_BOT_ROUTES,
                "degraded — best-effort second delivery; the chat message still "
                "lands, only the board page's briefing panel is missing",
                "morning-board-gallery-app"),
    # ── store bindings (in-daemon; the functions at the bottom of this file) ─
    ContractRow("identity.google_configured", "store_binding", 1,
                "packages/admin/evolve_admin/google_service.py", _NO_INPROC,
                "board-card-detail-and-actions", used_by=("board-bot-lane-worker",)),
    ContractRow("model.tier_chain", "store_binding", 11,
                "packages/analyzer/primary_bot.py", _NO_INPROC,
                "board-card-detail-and-actions", used_by=("board-bot-lane-worker",)),
    ContractRow("cost.model_price", "store_binding", 2,
                "packages/analyzer/model_pricing.py", _NO_INPROC,
                "board-card-detail-and-actions", used_by=("board-bot-lane-worker",)),
    ContractRow("delivery.send_to_owner", "store_binding", 9,
                "packages/admin/evolve_admin/alerts/dispatcher.py",
                "degraded — an app can still run `openclaw message send` itself "
                "(the gallery convention) but loses bot-scoped recipient "
                "resolution and the named-failure return (D-CS7)",
                "touch-scheduler-in-the-daemon-and-remind-is-zero-model"),
    # ── spec fields (the Morning Board manifest, p-aed7721c) ─────────────────
    ContractRow("scheduled_actions[]", "spec_field", 7,
                "packages/admin/evolve_admin/applications/app_spec.py",
                "degraded — the bundle's cron script runs under any scheduler; "
                "nothing materializes it or stamps EVOLVE_APP_ID",
                "morning-board-gallery-app"),
    ContractRow("scheduled_actions[].delivery_contract", "spec_field", 9,
                "packages/analyzer/delivery_monitor.py",
                "degraded — the app still delivers; nothing knows whether it arrived",
                "morning-board-gallery-app"),
    ContractRow("app_dependencies[]", "spec_field", 6,
                "packages/admin/evolve_admin/applications/forge_engine.py",
                "degraded — ignored; the operator installs Calendar Sync by hand",
                "morning-board-gallery-app"),
    ContractRow("requirements.messaging_channel[]", "spec_field", 9,
                "packages/admin/evolve_admin/applications/gallery.py",
                "degraded — unchecked at install; the first send fails instead",
                "morning-board-gallery-app"),
    ContractRow("interface_contract.data_files[]", "spec_field", 8,
                "packages/admin/evolve_admin/applications/forge_engine.py",
                "yes — plain workspace files; Evolve only reads the declaration",
                "morning-board-gallery-app"),
    # ── the Tracker's verbs + the group surface (Project Manager, D-TM11) ────
    *(ContractRow(f"tracker.{v}", "store_binding", 8, _BOARD_STORE, _NO_INPROC,
                  _PM_CHIP, used_by=("project-manager",))
      for v in ("read", "create_card", "update", "move", "touch", "link", "set_cursor")),
    ContractRow("delivery.send_to_channel", "store_binding", 9,
                "packages/admin/evolve_admin/alerts/dispatcher.py",
                "degraded — an app can run `openclaw message send` itself but loses "
                "the bot's gateway port resolution and the named-failure return",
                _PM_CHIP, used_by=("project-manager",)),
    ContractRow("channels.read_history", "store_binding", 9,
                "packages/analyzer/slack_signals.py",
                "degraded — an app can call Slack's conversations.history with its "
                "own token; Evolve adds only the per-bot token lookup",
                _PM_CHIP, used_by=("project-manager",)),
    # ── the records layer (D-AD2/D-AD3, service 8): six verbs, their routes,
    #    and the two spec fields that declare a store ─────────────────────────
    *(ContractRow(f"records.{v}", "tool_verb", 8, _RECORDS_TOOL, _NO_RECORDS_TOOL,
                  _RECORDS_CHIP) for v in ("list", "get", "put", "delete", "history")),
    ContractRow("ledger.append", "tool_verb", 8, _RECORDS_TOOL, _NO_RECORDS_TOOL,
                _RECORDS_CHIP),
    *(ContractRow(f"POST /api/records-bot/<app_id>/{v}", "daemon_endpoint", 8,
                  _RECORDS_ROUTES, _NO_RECORDS_DAEMON, _RECORDS_CHIP,
                  used_by=(u,))
      for v, u in (("list", "records.list"), ("get", "records.get"),
                   ("put", "records.put"), ("delete", "records.delete"),
                   ("history", "records.history"),
                   ("ledger/append", "ledger.append"))),
    ContractRow("store.tables{}", "spec_field", 8,
                "packages/admin/evolve_admin/app_spec_schema.py",
                "no — without Evolve nothing creates, migrates or serves the file; "
                "the declaration is inert", _RECORDS_CHIP),
    ContractRow("scope", "spec_field", 6,
                "packages/admin/evolve_admin/app_spec_schema.py",
                "degraded — ignored; without Evolve every copy of an app keeps its "
                "own state, which is what the default `bot` means anyway",
                _RECORDS_CHIP),
    # ── capture from turns (D-TM4, the Assistant) ────────────────────────────
    ContractRow("tracker.propose", "store_binding", 8,
                "packages/admin/evolve_admin/board_capture.py",
                "no — the plugin enqueues, the daemon is the only store writer; "
                "without Evolve nothing is captured", _CAPTURE_CHIP),
    ContractRow("POST /api/board-bot/capture", "daemon_endpoint", 8, _BOARD_BOT_ROUTES,
                "no — the plugin's gate posts here and writes nothing itself; "
                "daemon unreachable ⇒ the turn is simply not captured",
                _CAPTURE_CHIP, used_by=("tracker.propose",)),
)

ROWS_BY_NAME: dict[str, ContractRow] = {r.name: r for r in ROWS}


class ContractRowMissing(RuntimeError):
    """A surface was registered for apps with no contract row — refused."""


def _needed_row(name: str, kind: str) -> str:
    return (
        f"app contract {CONTRACT_VERSION}: {kind} {name!r} has no contract row. "
        f"Add ContractRow({name!r}, {kind!r}, <service 1-12>, <owner module>, "
        f"<works without Evolve?>, <introducing brief id>) to "
        f"packages/admin/evolve_admin/app_contract.py, its mirror "
        f"packages/plugin/src/apps/contract/rows.ts, and the table in "
        f"internal/design-app-contract-v1.md before registering it."
    )


def row(name: str, kind: str | None = None) -> ContractRow:
    """The row named *name* (of *kind*, when given) or :class:`ContractRowMissing`."""
    r = ROWS_BY_NAME.get(name)
    if r is None or (kind is not None and r.kind != kind):
        raise ContractRowMissing(_needed_row(name, kind or "surface"))
    return r


def route_names(app: Any, prefix: str) -> list[str]:
    """``"METHOD /rule"`` for every Flask route under *prefix* (HEAD/OPTIONS
    are Flask's own and never a contract surface)."""
    names = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith(prefix):
            continue
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            names.append(f"{method} {rule.rule}")
    return names


def require_route_rows(app: Any, prefix: str) -> None:
    """Fail closed: every app-facing route under *prefix* must carry a
    ``daemon_endpoint`` row. Called at the END of a route module's
    registration, so it sees the routes actually registered, not a list
    someone remembered to update."""
    missing = [n for n in route_names(app, prefix)
               if (ROWS_BY_NAME.get(n) is None
                   or ROWS_BY_NAME[n].kind != "daemon_endpoint")]
    if missing:
        raise ContractRowMissing(
            "\n".join(_needed_row(n, "daemon_endpoint") for n in missing))


# ── store bindings ──────────────────────────────────────────────────────────
# One function per ``store_binding`` row. Lazy imports on purpose: the owner
# modules live in two packages (admin + analyzer, the latter loaded by bare
# name) and a caller that never asks must not pay for loading them. A test
# that monkeypatches the owner module's attribute patches what these call.


def google_configured(bot_id: str, network: dict[str, Any]) -> bool:
    """``identity.google_configured`` — is a Google account connected for *bot_id*."""
    from .google_service import is_google_configured
    return is_google_configured(bot_id, network)


def model_tier_chain(network: dict[str, Any], bot_id: str, tier: str) -> list[str]:
    """``model.tier_chain`` — the bot's ``provider/model`` chain for *tier*."""
    from primary_bot import bot_tier_models
    return bot_tier_models(network, bot_id, tier)


def model_price(shared_dir: Path, provider: str, model: str) -> dict[str, Any] | None:
    """``cost.model_price`` — the pricing-catalog record for one model, or None."""
    from model_pricing import lookup_price, read_pricing_cache
    return lookup_price(read_pricing_cache(Path(shared_dir)), provider, model)


def send_to_owner(
    bot_id: str, network: dict[str, Any], message: str,
) -> tuple[bool, str | None]:
    """``delivery.send_to_owner`` — deliver *message* to *bot_id*'s own user,
    zero model calls; ``(ok, error)`` with the bot named on failure."""
    from .alerts import dispatcher
    return dispatcher.send_direct_to_bot(bot_id, network, message)


def send_to_channel(
    bot_id: str, network: dict[str, Any], channel: str, target: str, message: str,
) -> tuple[bool, str | None]:
    """``delivery.send_to_channel`` — post *message* as *bot_id* into a group
    channel/thread target, zero model calls; ``(ok, error)``."""
    from .alerts import dispatcher
    return dispatcher.send_to_bot_target(bot_id, network, channel, target, message)


def read_history(
    bot_id: str, network: dict[str, Any], channel: str, oldest: float,
) -> list[dict[str, Any]]:
    """``channels.read_history`` — a Slack channel's messages since *oldest*
    (epoch seconds), oldest first, with reactions; read-only. Raises
    ``RuntimeError`` naming the bot when no token resolves."""
    import slack_signals as ss
    try:
        token = ss._resolve_slack_token(bot_id, network)  # noqa: SLF001
    except SystemExit as exc:
        raise RuntimeError(f"no Slack token for bot {bot_id!r}") from exc
    msgs = ss._paginate("conversations.history",  # noqa: SLF001
                        {"channel": channel, "oldest": f"{oldest:.6f}"},
                        token, "messages", limit=2000)
    return sorted(msgs, key=lambda m: float(m.get("ts") or 0))


# Tracker verbs (State) — one pass-through per ``tracker.*`` row. Named
# here, not re-implemented: ``board_store`` is the single writer (D-MB1).

def tracker_read(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """``tracker.read`` — the bot's whole store (cards + lists)."""
    from . import board_store
    return board_store.load_board(Path(shared_dir), bot_id)


def tracker_propose(shared_dir: Path, bot_id: str, network: dict[str, Any],
                    request: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """``tracker.propose`` — one gated excerpt → at most one classifier call."""
    from . import board_capture
    return board_capture.propose(Path(shared_dir), bot_id, network, request, **kw)


def tracker_create_card(shared_dir: Path, bot_id: str, **kw: Any) -> dict[str, Any]:
    """``tracker.create_card``."""
    from . import board_store
    return board_store.create_card(Path(shared_dir), bot_id, **kw)


def tracker_update(shared_dir: Path, bot_id: str, card_id: str, **kw: Any) -> dict[str, Any]:
    """``tracker.update`` — a project card's assignee / working status."""
    from . import board_store
    return board_store.update_project_card(Path(shared_dir), bot_id, card_id, **kw)


def tracker_move(shared_dir: Path, bot_id: str, card_id: str, lane: str, **kw: Any) -> dict[str, Any]:
    """``tracker.move`` — lane change; settling a linked card settles both."""
    from . import board_store
    return board_store.move_card(Path(shared_dir), bot_id, card_id, lane, **kw)


def tracker_touch(shared_dir: Path, bot_id: str, card_id: str, **kw: Any) -> dict[str, Any]:
    """``tracker.touch`` — append a D-TM3 touch row."""
    from . import board_store
    return board_store.record_touch(Path(shared_dir), bot_id, card_id, **kw)


def tracker_link(shared_dir: Path, bot_id: str, a: str, b: str, *, actor: str) -> Any:
    """``tracker.link`` — D-TM13's two-way handoff link."""
    from . import board_store
    return board_store.link_cards(Path(shared_dir), bot_id, a, b, actor=actor)


def tracker_set_cursor(shared_dir: Path, bot_id: str, list_id: str, key: str, value: str) -> None:
    """``tracker.set_cursor`` — a list's run cursor, kept in the store."""
    from . import board_store
    board_store.set_list_cursor(Path(shared_dir), bot_id, list_id, key, value)


# ── seam data (read by tests/test_app_contract_inventory.py) ────────────────

#: The six PA briefs this inventory was written from (brief item 3b). Their
#: ``touches:`` front matter is the scan scope; for the three done briefs
#: that predate the ``touches:`` field, :data:`PA_TOUCHES_FROM_PRS` is the
#: source-file list of the PR that shipped them.
PA_BRIEFS = (
    "touch-scheduler-in-the-daemon-and-remind-is-zero-model",
    "capture-from-turns-is-gated-and-lands-as-a-proposal",
    "project-manager-app-on-the-tracker-with-slack-parity",
    "board-bot-lane-worker",
    "board-card-detail-and-actions",
    "morning-board-gallery-app",
)

PA_TOUCHES_FROM_PRS: dict[str, tuple[str, ...]] = {
    "board-bot-lane-worker": (  # PR #4279
        "packages/admin/evolve_admin/board_store.py",
        "packages/admin/evolve_admin/board_worker.py",
        "packages/admin/evolve_admin/board_worker_runner.py",
        "packages/admin/evolve_admin/web/routes_board.py",
    ),
    "board-card-detail-and-actions": (  # PR #4259
        "packages/admin/evolve_admin/board_actions.py",
        "packages/admin/evolve_admin/board_store.py",
        "packages/admin/evolve_admin/web/board_bot_routes.py",
        "packages/admin/evolve_admin/web/routes_board.py",
        "packages/plugin/src/tools/BoardTool.ts",
    ),
    "morning-board-gallery-app": (  # PR #4265
        "gallery/morning-board/scripts/morning_board.py",
        "packages/admin/evolve_admin/board_store.py",
        "packages/admin/evolve_admin/web/board_bot_routes.py",
        "packages/admin/evolve_admin/web/routes_board.py",
    ),
}

#: Every PA-touched source path (or directory prefix ending in ``/``) →
#: ``(role, service)``. Roles: ``app`` (the PA itself), ``service`` (a
#: platform service's own implementation, written by a PA chip),
#: ``transport`` (a contract surface — tool or endpoint module), ``host``
#: (a pre-existing module a PA chip edited or will edit; not PA code, not
#: scanned). A PA-touched source file with no entry fails the seam test.
PA_MODULES: dict[str, tuple[str, int | None]] = {
    "gallery/morning-board/scripts/morning_board.py": ("app", None),
    "packages/admin/evolve_admin/board_store.py": ("service", 8),
    "packages/admin/evolve_admin/board_actions.py": ("service", 8),
    "packages/admin/evolve_admin/board_stack.py": ("service", 8),
    "packages/admin/evolve_admin/web/routes_board.py": ("service", 8),
    "packages/admin/evolve_admin/board_touch.py": ("service", 7),
    "packages/admin/evolve_admin/board_worker.py": ("service", 7),
    "packages/admin/evolve_admin/board_worker_runner.py": ("service", 7),
    "packages/admin/evolve_admin/web/board_bot_routes.py": ("transport", 8),
    "packages/plugin/src/tools/BoardTool.ts": ("transport", 8),
    "packages/plugin/src/observer/": ("host", 4),
    "packages/plugin/dist/": ("host", None),
    "packages/admin/evolve_admin/applications/": ("host", 6),
    "gallery/task-manager/": ("host", None),
    "packages/admin/evolve_admin/board_project.py": ("app", None),
    "packages/admin/evolve_admin/board_import.py": ("service", 8),
    "packages/admin/evolve_admin/board_capture.py": ("service", 8),
}

#: Importable module → the platform service that owns it. An import from a
#: scanned PA module into a DIFFERENT service than its own must go through
#: this module (``evolve_admin.app_contract``) — or be listed in KNOWN_DEBT.
SERVICE_OWNERS: dict[str, int] = {
    "evolve_admin.board_store": 8,
    "evolve_admin.board_actions": 8,
    "evolve_admin.board_stack": 8,
    "evolve_admin.board_capture": 8,
    "evolve_admin.board_store_perms": 8,
    "evolve_admin.board_touch": 7,
    "evolve_admin.board_worker": 7,
    "evolve_admin.alerts.dispatcher": 9,
    "delivery_monitor": 9,
    "primary_bot": 11,
    "model_pricing": 2,
    "evolve_admin.google_service": 1,
    "evolve_admin.roster_resolver": 1,
    "evolve_admin.users_roster": 1,
    "evolve_admin.roster_identity": 1,
    "evolve_admin.external_ids": 1,
    "signals": 10,
    "evolve_admin.applications": 6,
}

#: Files allowed to WRITE persistent state, and why (forbidden-list item 1:
#: no PA-private state outside the Tracker/Board store). An ``app`` module
#: instead proves each write is a manifest-declared ``data_files[]`` entry.
STATE_WRITERS: dict[str, str] = {
    "packages/admin/evolve_admin/board_store.py":
        "the Board/Tracker store itself — the single writer (D-MB1)",
    "packages/admin/evolve_admin/board_worker.py":
        "Execution's own evidence: per-card run files, the delegation delivery "
        "ledger (delivery_monitor's vocabulary), its event cursor and busy marker",
    "packages/admin/evolve_admin/board_import.py":
        "the D-TM9 migration: renames the bot's old tasks.json to "
        "tasks.json.imported-<date> after its items are cards (operator-run)",
}

#: App modules that write, and the manifest whose ``interface_contract.
#: data_files[]`` declares what they write.
DECLARED_APP_DATA: dict[str, tuple[str, tuple[str, ...]]] = {
    "gallery/morning-board/scripts/morning_board.py": (
        "gallery/morning-board/p-aed7721c.json",
        ("memory/board-runs/YYYY-MM-DD.json", "morning_board/config.json"),
    ),
}

#: Forbidden-list item 2: these functions (and everything they call in the
#: same module) must never reach a model. ``(path, root function)``.
ZERO_MODEL_ROOTS: tuple[tuple[str, str], ...] = (
    ("packages/admin/evolve_admin/board_touch.py", "_fire_remind"),
    ("packages/admin/evolve_admin/board_touch.py", "compose_remind_message"),
    # D-TM9: "no model call in ingest, report, or nags" — the whole tick.
    ("packages/admin/evolve_admin/board_project.py", "tick"),
    ("packages/admin/evolve_admin/board_project.py", "render_weekly_report"),
)

#: The seam as it stands after this PR: every violation the scan still finds,
#: each with why it was not moved here and where it moves to. The test
#: requires the found set to EQUAL this set — a new reach fails, and so does
#: a fixed one still listed (delete the entry). ``(path, finding, why)``.
KNOWN_DEBT: tuple[tuple[str, str, str], ...] = (
    ("packages/admin/evolve_admin/board_touch.py", "import:evolve_admin.board_store",
     "Execution reads/writes cards through ~20 board_store functions (incl. "
     "_parse_ts, save_board). That is the Tracker verb set design §2 row 8 "
     "lists as 'verbs not yet' — a design, not a thin move."),
    ("packages/admin/evolve_admin/board_worker.py", "import:evolve_admin.board_store",
     "same as board_touch: the worker is Execution acting on State with no "
     "Tracker verbs to call; moves when the Tracker verbs exist."),
    ("packages/admin/evolve_admin/board_worker.py", "import:evolve_admin.board_actions",
     "the worker runs the card's named action from the action table (State); "
     "moves with the Tracker verbs."),
    ("packages/admin/evolve_admin/web/routes_board.py", "import:evolve_admin.board_worker",
     "the phone's approval tap resumes a delegation in-process "
     "(resume_delegation); becomes an event the worker subscribes to."),
    ("gallery/morning-board/scripts/morning_board.py", "roster_read_by_file",
     "the gallery delivery convention (spec-gallery-delivery-convention-"
     "2026-06-11) resolves its route from network.json's primary_user; moving "
     "it to delivery.send_to_owner changes route selection (primary_channel vs "
     "enabled-channel priority) and the sending uid — a behaviour change and a "
     "package bump, for the Delivery service to decide across every gallery app."),
    ("gallery/morning-board/scripts/morning_board.py", "pa_only_notification_route",
     "the same gallery convention: `openclaw message send` from the bot's own "
     "uid, shared by every gallery app (not PA-only) but outside the platform's "
     "Delivery binding; moves with the roster read above."),
)


__all__ = [
    "CONTRACT_VERSION", "SERVICES", "KINDS", "STATUSES", "ContractRow", "ROWS",
    "ROWS_BY_NAME", "ContractRowMissing", "row", "route_names",
    "require_route_rows", "google_configured", "model_tier_chain",
    "model_price", "send_to_owner", "send_to_channel", "read_history",
    "tracker_read", "tracker_create_card", "tracker_update",
    "tracker_move", "tracker_touch", "tracker_link", "tracker_set_cursor",
]
