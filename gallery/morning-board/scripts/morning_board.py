#!/usr/bin/env python3
"""
morning_board.py — Morning Board stocking + composer + delivery CLI

Commands:
    run      — stock the board, compose the numbered morning message + the
               board page's briefing panel, deliver, write the run file
    preview  — same composition, printed to stdout; nothing stocked, posted,
               sent, or written
    status   — last-run info from memory/board-runs/{date}.json

D-MB5 (internal/design-pa-mobile-board-2026-08-31.md): stocking and the
daily message are a gallery app under the delivery contract, registered
with the delivery monitor — this replaces the hand-planted `morning-board`
OC cron the PoC bot ran before (retired at install by `evolve_admin.board_store
migrate`, D-MB6).

D-BI6 (internal/design-pa-board-interface-v2-2026-09-04.md): calendar
stocking is on by default, email is off until the operator grants it
(``morning_board/config.json``'s ``sources.email``); the run makes exactly
ONE bounded model call, for the short greeting line only — never an agentic
loop (`internal/finding-cost-forensics-power-bot-2026-09-04.md`: a loop is a
$2 turn). The SAME composed text is delivered twice: the chat message, and
the board page's collapsible briefing panel (``POST /api/board-bot/briefing``).

The board itself is never touched from this workspace directly — D-MB1 (one
writer: the admin daemon). Every read/write of a card goes through the
admin daemon's bot-facing API (``/api/board-bot/...``) over its unix
socket; the kernel's peer-uid check on that socket IS the auth, so this
script sends no credential of its own.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Constants ────────────────────────────────────────────────────────────

_NETWORK_JSON = Path("/Users/Shared/evolve/network.json")
_ADMIN_SOCKET = Path("/Users/Shared/evolve/admin-daemon.sock")
_STATE_SUBDIR = "morning_board"

_OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)
_CHANNEL_PRIORITY = (
    "telegram", "whatsapp", "signal", "imessage", "slack",
    "discord", "sms", "matrix",
)

# D-BI6b: stocking sources are per-integration opt-in. Calendar on, email
# off, until the operator grants it in morning_board/config.json.
_DEFAULT_SOURCES = {"calendar": True, "email": False}

# Canonical cluster grouping order (board_store.CLUSTERS) — custom clusters
# a card carries but this tuple doesn't know about sort after it, alphabetically.
_CLUSTER_ORDER = (
    "health", "fitness", "travel", "work", "social",
    "hobbies", "family", "home", "admin",
)

_CLUSTER_KEYWORDS = {
    "health": ("doctor", "dentist", "clinic", "therapy", "checkup",
               "prescription", "physical"),
    "fitness": ("gym", "workout", "run", "yoga", "training"),
    "travel": ("flight", "trip", "hotel", "airport", "itinerary"),
    "social": ("dinner", "party", "birthday", "friends"),
    "family": ("kids", "school", "parent", "family"),
    "home": ("plumber", "repair", "contractor", "cleaning", "home"),
    "hobbies": ("class", "lesson", "hobby"),
}

# Approximate per-run cost, embedded locally so the run file needs no
# external pricing catalog — this is a raw, un-annotated API call outside
# the turn/annotation cost pipeline (packages/analyzer/turn_cost.py), so it
# must record its own usage. USD per 1M tokens, (input, output). Cached 2026-09;
# an unrecognized model records tokens with cost_usd left null rather than a
# silently wrong number.
_MODEL_PRICING_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
}
_DEFAULT_MODEL = "claude-haiku-4-5"
_MAX_FRAMING_TOKENS = 120  # ONE short greeting sentence, not the board itself

_FRAMING_SYSTEM_PROMPT = (
    "You write ONE short, warm morning-greeting sentence introducing a "
    "person's task board for the day. You will be given the person's first "
    "name (may be empty), today's local date, and a one-line summary of "
    "what is on the board. Return ONLY that one sentence — no markdown, no "
    "quotes, no signature, no board contents (those are appended "
    "separately). Use the name once if it is non-empty. Do not invent "
    "anything not in the summary."
)


# ── Bot identity + delivery (spec-gallery-delivery-convention-2026-06-11) ──


def _bot_id() -> str:
    """The logical bot id this script runs as (see calendar_summary.py for
    the same convention: account name, or the network.json bot whose `user`
    override matches this account)."""
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        bots = json.loads(_NETWORK_JSON.read_text()).get("bots", {})
    except (OSError, json.JSONDecodeError):
        return account
    if account in bots:
        return account
    for bot_id, cfg in bots.items():
        if isinstance(cfg, dict) and cfg.get("user") == account:
            return bot_id
    return account


def _find_openclaw() -> str:
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in _OPENCLAW_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("openclaw CLI not found")


def _enabled_channels():
    cfg = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw" / "openclaw.json"
    try:
        channels = json.loads(cfg.read_text()).get("channels", {})
    except (OSError, json.JSONDecodeError):
        return None
    enabled = {name for name, c in channels.items()
               if isinstance(c, dict) and c.get("enabled")}
    return enabled or None


def _resolve_route(bot_id: str) -> tuple:
    try:
        network = json.loads(_NETWORK_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"cannot read network.json ({_NETWORK_JSON}): {exc}")
        return None, None
    bot_cfg = network.get("bots", {}).get(bot_id) or {}
    ids = (bot_cfg.get("primary_user") or {}).get("external_ids") or {}
    enabled = _enabled_channels()
    for channel in _CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target and (enabled is None or channel in enabled):
            return channel, str(target)
    for channel in _CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* via `openclaw message send`. True = accepted, False =
    no route (graceful skip, no run file). Raises RuntimeError on failure."""
    channel, target = _resolve_route(bot_id)
    if channel is None:
        print(f"DELIVERY_SKIPPED: no delivery route for {bot_id} "
              "(network.json primary_user.external_ids empty)", file=sys.stderr)
        return False
    openclaw = _find_openclaw()
    cmd = [
        openclaw, "message", "send",
        f"--channel={channel}", f"--target={target}",
        f"--message={text}", "--json",
    ]
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(openclaw) + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=pwd.getpwuid(os.getuid()).pw_dir, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"openclaw message send failed: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:400]
        raise RuntimeError(f"openclaw message send failed (rc={r.returncode}): {err}")
    return True


# ── Board client (D-MB1: the admin daemon is the one writer) ───────────────


class AdminDaemonUnavailable(RuntimeError):
    """The admin daemon's unix socket isn't reachable, or refused the call."""


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over an AF_UNIX stream socket (mirrors
    evolve_admin.evo.admin_client's client — this copy is standalone-stdlib,
    the load-bearing contract for a bot-workspace script: no deps beyond
    python3 + the openclaw CLI)."""

    def __init__(self, socket_path: str, timeout: Optional[float] = None):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.timeout is not None:
                sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            sock.close()
            raise AdminDaemonUnavailable(
                f"cannot connect to admin daemon socket {self._socket_path}: {exc}"
            ) from exc
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _board_request(method: str, path: str, body: Any = None,
                   *, timeout: float = 10.0) -> "tuple[int, Any]":
    conn = _UnixSocketHTTPConnection(str(_ADMIN_SOCKET), timeout=timeout)
    try:
        headers: dict = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if payload is not None:
            conn.request(method, path, body=payload, headers=headers)
        else:
            conn.request(method, path)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
    except (TimeoutError, socket.timeout) as exc:
        raise AdminDaemonUnavailable(f"timeout calling {method} {path}: {exc}") from exc
    except (ConnectionError, OSError) as exc:
        raise AdminDaemonUnavailable(f"network error calling {method} {path}: {exc}") from exc
    finally:
        conn.close()
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def board_list_active() -> "list[dict[str, Any]]":
    """Cards worth showing on the morning board — the lanes that are still
    open work. ``done``/``dropped`` are settled and belong to the receipt,
    not the morning message."""
    status, data = _board_request("GET", "/api/board-bot/cards?limit=200")
    if status != 200 or not isinstance(data, dict):
        raise AdminDaemonUnavailable(f"board.list failed (status={status})")
    cards = data.get("cards") or []
    return [c for c in cards if c.get("lane") in ("inbox", "today", "later")]


def board_add(*, title: str, cluster: str, source: str, note: str = "",
             source_id: Optional[str] = None,
             enrichment: Optional[dict] = None) -> dict:
    body: dict[str, Any] = {"title": title, "cluster": cluster, "source": source}
    if note:
        body["note"] = note
    if source_id:
        body["source_id"] = source_id
    if enrichment:
        body["enrichment"] = enrichment
    status, data = _board_request("POST", "/api/board-bot/cards", body)
    if status not in (200, 201) or not isinstance(data, dict):
        raise AdminDaemonUnavailable(
            f"board.add failed for {title!r} (status={status})")
    return data


def board_post_briefing(text: str) -> bool:
    """Best-effort: the panel is the same composition delivered a SECOND
    way (D-BI6d), not the load-bearing delivery — a failure here must never
    turn an accepted chat send into a failed run."""
    try:
        status, _data = _board_request(
            "POST", "/api/board-bot/briefing", {"text": text})
    except AdminDaemonUnavailable:
        return False
    return status == 200


# ── Stocking (D-BI6b: calendar on by default, email opt-in) ────────────────


def read_today_events(workspace: Path) -> Optional[list]:
    """Calendar Sync's ``memory/calendar-today.json``:
    ``{id, title, start_iso, end_iso, attendees, description, is_external,
    location}`` per event. None when unreadable — never crash on a source
    that hasn't produced anything yet."""
    path = workspace / "memory" / "calendar-today.json"
    if not path.exists():
        return None
    try:
        events = json.loads(path.read_text())
        return events if isinstance(events, list) else None
    except (json.JSONDecodeError, OSError):
        return None


def read_email_digest(workspace: Path) -> Optional[list]:
    """Email Integration's ``memory/email-digest.json``. Only consulted
    when ``sources.email`` is explicitly true (off by default, D-BI6b) —
    email is untrusted input and an email-derived card can never
    auto-execute an action."""
    path = workspace / "memory" / "email-digest.json"
    if not path.exists():
        return None
    try:
        emails = json.loads(path.read_text())
        return emails if isinstance(emails, list) else None
    except (json.JSONDecodeError, OSError):
        return None


def guess_cluster(text: str, default: str) -> str:
    hay = text.lower()
    for cluster, words in _CLUSTER_KEYWORDS.items():
        if any(w in hay for w in words):
            return cluster
    return default


def stock_from_calendar(events: list) -> int:
    """Post each event as a stocking candidate; the daemon dedups against
    already-settled cards server-side (D-BI2) — this never checks first."""
    added = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        enrichment: dict[str, Any] = {}
        start_iso = ev.get("start_iso")
        if start_iso:
            enrichment["when"] = {"value": start_iso, "source": "calendar"}
        location = ev.get("location")
        if location:
            enrichment["location"] = {"text": str(location), "source": "calendar"}
        result = board_add(
            title=title, cluster=guess_cluster(title, "work"),
            source="calendar", source_id=str(ev.get("id") or ""),
            enrichment=enrichment or None,
        )
        if not result.get("skipped"):
            added += 1
    return added


def stock_from_email(emails: list) -> int:
    """Email-derived cards are marked ``source: email`` and carry a note
    saying so — an email card can never auto-execute an action (D-BI6b),
    only ever surface as something to look at or draft a reply to."""
    added = 0
    for em in emails:
        if not isinstance(em, dict):
            continue
        subject = str(em.get("subject") or "").strip()
        if not subject:
            continue
        sender = str(em.get("from_display") or em.get("from") or "").strip()
        title = f"{sender} — {subject}" if sender else subject
        result = board_add(
            title=title, cluster=guess_cluster(title, "admin"), source="email",
            source_id=str(em.get("id") or ""),
            note="from email — draft a reply, never auto-sent",
        )
        if not result.get("skipped"):
            added += 1
    return added


def stock_board(workspace: Path, sources: dict) -> dict:
    """Runs every enabled source. Returns per-source availability + counts
    for the run file — ``sources_available`` records what was READABLE,
    independent of whether it was enabled."""
    report = {"sources_available": {"calendar": False, "email": False},
             "cards_stocked": {"calendar": 0, "email": 0}}
    if sources.get("calendar", True):
        events = read_today_events(workspace)
        report["sources_available"]["calendar"] = events is not None
        if events:
            report["cards_stocked"]["calendar"] = stock_from_calendar(events)
    if sources.get("email", False):
        emails = read_email_digest(workspace)
        report["sources_available"]["email"] = emails is not None
        if emails:
            report["cards_stocked"]["email"] = stock_from_email(emails)
    return report


# ── Composition (D-BI6a: one bounded model call, never a loop) ─────────────


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    rates = _MODEL_PRICING_PER_MTOK.get(model)
    if not rates:
        return None
    rate_in, rate_out = rates
    return round(input_tokens / 1_000_000 * rate_in
                + output_tokens / 1_000_000 * rate_out, 6)


def _read_api_key() -> Optional[str]:
    """Env var (set by the launchd plist at install time) first, then the
    bot's own openclaw.json auth profile — never prompts, never invents."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cfg = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw" / "openclaw.json"
    try:
        profiles = json.loads(cfg.read_text()).get("auth", {}).get("profiles", [])
    except (OSError, json.JSONDecodeError):
        return None
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and profile.get("type") == "anthropic_api_key":
            key = profile.get("key")
            if key:
                return str(key)
    return None


def call_anthropic(api_key: str, model: str, user_payload: dict,
                   timeout: float = 20) -> "tuple[str, int, int]":
    """One bounded call. Returns ``(text, input_tokens, output_tokens)``."""
    body = json.dumps({
        "model": model,
        "max_tokens": _MAX_FRAMING_TOKENS,
        "system": _FRAMING_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(user_payload)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    text = ""
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    usage = data.get("usage") or {}
    return text, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def compose_framing_template(user_name: str) -> str:
    return f"Good morning{', ' + user_name if user_name else ''}."


def number_and_group(cards: "list[dict]") -> "tuple[list[str], int]":
    """Group by the canonical cluster order, number sequentially across the
    WHOLE board (not restarted per cluster) — the reply triage names one
    global number, so numbering must be globally unique."""
    by_cluster: dict[str, list[dict]] = {}
    for c in cards:
        by_cluster.setdefault(str(c.get("cluster") or "admin"), []).append(c)
    order = list(_CLUSTER_ORDER) + sorted(
        k for k in by_cluster if k not in _CLUSTER_ORDER)
    lines: list[str] = []
    n = 0
    for cluster in order:
        rows = by_cluster.get(cluster)
        if not rows:
            continue
        lines.append(cluster.capitalize())
        for card in rows:
            n += 1
            lines.append(f"{n}. {card.get('title')}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines, n


def compose_board_message(cards: "list[dict]", *, user_name: str,
                          model: str, api_key: Optional[str]) -> dict:
    """Returns ``{text, composer, model, input_tokens, output_tokens,
    cost_usd, cards_shown}`` — never raises; an LLM failure falls through to
    the deterministic template line."""
    body_lines, shown = number_and_group(cards)
    board_text = ("\n".join(body_lines) if body_lines
                 else "Nothing waiting this morning.")
    input_tokens = output_tokens = 0
    composer = "template"
    framing = compose_framing_template(user_name)
    if model and api_key:
        try:
            summary = (f"{shown} item(s) on the board" if shown
                      else "nothing waiting")
            text, itoks, otoks = call_anthropic(api_key, model, {
                "user_name": user_name,
                "date_local": date.today().isoformat(),
                "board_summary": summary,
            })
            if text and "```" not in text:
                framing = text
                composer = "llm"
                input_tokens, output_tokens = itoks, otoks
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
               KeyError):
            pass  # fall through to the template framing — never crash a run
    if shown:
        legend = ("Reply with a number and today / bot / later — "
                 "e.g. \"2 bot\" hands it to me.")
        message = f"{framing}\n\n{board_text}\n\n{legend}"
    else:
        message = f"{framing}\n\n{board_text}"
    return {
        "text": message, "composer": composer, "model": model if composer == "llm" else "",
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cost_usd": (_estimate_cost(model, input_tokens, output_tokens)
                    if composer == "llm" else 0.0),
        "cards_shown": shown,
    }


# ── Config + run-file bookkeeping ───────────────────────────────────────────


def _config_path(workspace: Path) -> Path:
    return workspace / _STATE_SUBDIR / "config.json"


def load_config(workspace: Path) -> dict:
    defaults = {
        "user_name": "", "time_zone": "America/Los_Angeles",
        "delivery_time": "07:00", "model": _DEFAULT_MODEL,
        "sources": dict(_DEFAULT_SOURCES),
    }
    path = _config_path(workspace)
    try:
        on_disk = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        on_disk = {}
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(defaults, indent=2) + "\n")
    merged = dict(defaults)
    merged.update({k: v for k, v in on_disk.items() if k != "sources"})
    sources = dict(_DEFAULT_SOURCES)
    sources.update(on_disk.get("sources") or {})
    merged["sources"] = sources
    return merged


def _run_dir(workspace: Path) -> Path:
    return workspace / "memory" / "board-runs"


def _run_path(workspace: Path, day: str) -> Path:
    return _run_dir(workspace) / f"{day}.json"


def already_ran_today(workspace: Path) -> bool:
    return _run_path(workspace, date.today().isoformat()).exists()


def write_run(workspace: Path, record: dict) -> None:
    d = _run_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = _run_path(workspace, record["date"])
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_status_string(workspace: Path) -> str:
    today = date.today().isoformat()
    path = _run_path(workspace, today)
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "No board sent today yet."
    return (f"Last board: today at {record.get('sent_at', '?')} — "
           f"{record.get('cards_shown', 0)} item(s), "
           f"composer={record.get('composer', '?')}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def _workspace_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace)
    return Path(__file__).parent.parent.resolve()


# ── Commands ─────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    workspace = _workspace_from_args(args)
    if not args.force and already_ran_today(workspace):
        print(f"BOARD_SKIPPED: already sent today ({_run_path(workspace, date.today().isoformat())})")
        return 0
    bot_id = _bot_id()
    config = load_config(workspace)
    try:
        stocking = stock_board(workspace, config["sources"])
    except AdminDaemonUnavailable as exc:
        print(f"BOARD_FAILED: admin daemon unreachable while stocking: {exc}",
              file=sys.stderr)
        return 1
    try:
        cards = board_list_active()
    except AdminDaemonUnavailable as exc:
        print(f"BOARD_FAILED: admin daemon unreachable while reading the "
              f"board: {exc}", file=sys.stderr)
        return 1
    composed = compose_board_message(
        cards, user_name=config["user_name"], model=config.get("model") or "",
        api_key=_read_api_key())
    try:
        accepted = send_message(bot_id, composed["text"])
    except RuntimeError as exc:
        print(f"BOARD_FAILED: {exc}", file=sys.stderr)
        return 1
    if not accepted:
        return 0  # DELIVERY_SKIPPED already printed by send_message; no run file
    panel_posted = board_post_briefing(composed["text"])
    if not panel_posted:
        _warn("board briefing panel was not updated (daemon call failed); "
              "the chat message still delivered")
    record = {
        "date": date.today().isoformat(),
        "sent_at": _utcnow_iso(),
        "cards_shown": composed["cards_shown"],
        "cards_stocked": stocking["cards_stocked"],
        "sources_available": stocking["sources_available"],
        "sources_enabled": config["sources"],
        "composer": composed["composer"],
        "model": composed["model"],
        "input_tokens": composed["input_tokens"],
        "output_tokens": composed["output_tokens"],
        "cost_usd": composed["cost_usd"],
        "message": composed["text"],
    }
    write_run(workspace, record)
    print(f"BOARD_SENT: {composed['cards_shown']} item(s), "
         f"composer={composed['composer']}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    workspace = _workspace_from_args(args)
    config = load_config(workspace)
    try:
        cards = board_list_active()
    except AdminDaemonUnavailable as exc:
        print(f"BOARD_PREVIEW_FAILED: {exc}", file=sys.stderr)
        return 1
    composed = compose_board_message(
        cards, user_name=config["user_name"], model=config.get("model") or "",
        api_key=_read_api_key())
    print(composed["text"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(read_status_string(_workspace_from_args(args)))
    return 0


# ── Argument parsing ─────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Morning Board — stock, compose, and deliver the daily board message",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--workspace", help="Path to the bot's workspace directory")
    sub = p.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Stock, compose, and deliver today's board")
    run_p.add_argument("--force", action="store_true",
                       help="Ignore today's idempotency record and re-send")
    sub.add_parser("preview", help="Compose and print, without stocking or sending")
    sub.add_parser("status", help="Show last-run info")
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "preview":
        sys.exit(cmd_preview(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
