#!/usr/bin/env python3
"""
briefing.py — Morning Briefing CLI
Compose and deliver (or preview) the daily morning message.

Commands:
    send     — fetch data, compose, send to the user's channel, write memory log
    preview  — same as send but print to stdout instead of sending
    status   — print last-run info from today's (or yesterday's) memory file

Usage (cron / launchd via briefing-cron.sh):
    python3 scripts/briefing.py send \\
        --user-name "Alex" \\
        --time-zone "America/Los_Angeles" \\
        --detail standard \\
        --location "Seattle, WA" \\
        --news-sources "https://feeds.npr.org/1001/rss.xml,https://lobste.rs/rss"

All network calls use urllib (stdlib only — no extra packages required).
Graceful degradation: any section (weather, news, email, calendar) that
fails is omitted with a one-line note; the briefing still delivers.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

# Pod network config (read at runtime to resolve the delivery route —
# the primary user's channel + target).
_NETWORK_JSON = Path("/Users/Shared/evolve/network.json")

# Directory for daily memory files (relative to workspace root).
_MEMORY_SUBDIR = "memory"

# Timeout for all outbound HTTP requests (seconds).
_HTTP_TIMEOUT = 10

# Maximum number of news headlines to surface.
_MAX_NEWS_ITEMS = 3

# wttr.in base URL — zero-config weather, location by city name.
# ?format=j1 returns structured JSON; no API key required.
_WTTR_BASE = "https://wttr.in"


# ── Weather ───────────────────────────────────────────────────────────────────


def fetch_weather(location: str) -> Optional[str]:
    """Return a one-line weather summary for *location* via wttr.in.

    Uses the ``?format=j1`` JSON endpoint which gives current conditions
    plus today's forecast without requiring an API key or registration.

    Returns:
        A ready-to-embed string like
        ``"Sunny, 72F (feels 70F), high 76F — low chance of rain"``
        or None if the fetch fails (caller handles the omission).
    """
    if not location or not location.strip():
        return None

    # Percent-encode the location so characters like '?', '&', '#', '/'
    # don't inject query parameters or change the path.  Commas are kept
    # unencoded as they appear in city strings like "Seattle, WA".
    loc_enc = urllib.parse.quote(location.strip(), safe=",")
    url = f"{_WTTR_BASE}/{loc_enc}?format=j1"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "evolve-briefing/1.0 (morning-briefing-template)"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        _warn(f"weather fetch failed (network): {exc}")
        return None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        _warn(f"weather fetch failed (parse): {exc}")
        return None

    try:
        current = data["current_condition"][0]
        temp_f = current["temp_F"]
        feels_f = current["FeelsLikeF"]
        desc = current["weatherDesc"][0]["value"]

        # Today's forecast for high temp and precipitation.
        today_fc = data["weather"][0] if data.get("weather") else {}
        max_f = today_fc.get("maxtempF", "")
        # Hourly precipitation: average chance of rain across daylight hours.
        hourly = today_fc.get("hourly", [])
        rain_chances = [
            int(h.get("chanceofrain", 0))
            for h in hourly
            if isinstance(h, dict)
        ]
        avg_rain = sum(rain_chances) // len(rain_chances) if rain_chances else 0

        summary = f"{desc}, {temp_f}F (feels {feels_f}F)"
        if max_f:
            summary += f", high {max_f}F"
        if avg_rain >= 30:
            summary += f" — {avg_rain}% chance of rain"

        return summary
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _warn(f"weather parse failed: {exc}")
        return None


# ── News (RSS) ────────────────────────────────────────────────────────────────

# Well-known topic keywords → RSS URLs, for user convenience.
# Users can also pass raw URLs directly.
_TOPIC_FEEDS: dict[str, str] = {
    "hacker news": "https://news.ycombinator.com/rss",
    "hn":          "https://news.ycombinator.com/rss",
    "hn top":      "https://news.ycombinator.com/rss",
    "npr":         "https://feeds.npr.org/1001/rss.xml",
    "bbc":         "https://feeds.bbci.co.uk/news/rss.xml",
    "reuters":     "https://feeds.reuters.com/reuters/topNews",
    "lobsters":    "https://lobste.rs/rss",
    "lobste.rs":   "https://lobste.rs/rss",
}

_RSS_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# RFC1918 + loopback ranges that are never valid RSS sources.
# Blocks SSRF probes against internal services (e.g. http://localhost:9000/admin,
# http://192.168.1.1/secret, http://169.254.169.254/latest/meta-data/).
# Operator-supplied URLs are checked before fetching.
_RFC1918_RE = re.compile(
    r"^https?://"
    r"(?:"
    r"localhost\.?(?=[/:#?]|$)"               # loopback hostname (anchored to host-end; optional trailing dot for DNS form; avoids localhost.example.com false-positive)
    r"|127\.\d+\.\d+\.\d+"                   # 127.0.0.0/8
    r"|10\.\d+\.\d+\.\d+"                    # 10.0.0.0/8
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+" # 172.16.0.0/12
    r"|192\.168\.\d+\.\d+"                   # 192.168.0.0/16
    r"|0\.0\.0\.0"                            # any-local
    r"|\[?::1\]?"                             # IPv6 loopback — bare (::1) and URL bracket form ([::1])
    r"|169\.254\.\d+\.\d+"                   # link-local 169.254.0.0/16 (AWS/GCP/Azure cloud-metadata)
    r")",
    re.IGNORECASE,
)


def _is_private_url(url: str) -> bool:
    """Return True if *url* targets a loopback or RFC1918 (private) address."""
    return bool(_RFC1918_RE.match(url))


def _resolve_news_source(raw: str) -> str:
    """Map a user-supplied topic keyword or URL to an RSS URL."""
    stripped = raw.strip()
    if _RSS_URL_RE.match(stripped):
        return stripped
    return _TOPIC_FEEDS.get(stripped.lower(), stripped)


def _fetch_rss_headlines(url: str, max_items: int) -> list[str]:
    """Fetch and parse an RSS feed; return up to *max_items* titles."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "evolve-briefing/1.0 (morning-briefing-template)"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        _warn(f"news fetch failed ({url}): {exc}")
        return []
    except ET.ParseError as exc:
        _warn(f"news parse failed ({url}): {exc}")
        return []

    # Support both RSS 2.0 (<item><title>) and Atom (<entry><title>).
    titles: list[str] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            titles.append(title_el.text.strip())
        if len(titles) >= max_items:
            return titles
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title_el = entry.find("atom:title", ns)
        if title_el is None:
            title_el = entry.find("title")
        if title_el is not None and title_el.text:
            titles.append(title_el.text.strip())
        if len(titles) >= max_items:
            return titles
    return titles


def fetch_news(news_sources_str: str, max_items: int = _MAX_NEWS_ITEMS) -> list[str]:
    """Return up to *max_items* deduplicated headlines from *news_sources_str*.

    *news_sources_str* is comma-separated; each entry is either:
    - A full URL (``https://...``)
    - A topic keyword (``hacker news``, ``npr``, etc.)

    Returns an empty list on total failure (caller handles the omission).
    """
    if not news_sources_str or not news_sources_str.strip():
        return []

    sources_raw = [s.strip() for s in news_sources_str.split(",") if s.strip()]
    # Cap at 3 sources to keep network overhead bounded.
    sources_raw = sources_raw[:3]

    seen: set[str] = set()
    headlines: list[str] = []

    for raw in sources_raw:
        url = _resolve_news_source(raw)
        if not _RSS_URL_RE.match(url):
            _warn(f"news source {raw!r} is not a valid URL or known topic — skipping")
            continue
        if _is_private_url(url):
            _warn(f"news source {raw!r} targets a private/loopback address — skipping")
            continue
        for title in _fetch_rss_headlines(url, max_items * 2):
            # Simple dedup on lowercased title.
            key = title.lower()
            if key not in seen:
                seen.add(key)
                headlines.append(title)
            if len(headlines) >= max_items:
                return headlines

    return headlines[:max_items]


# ── Message composition ───────────────────────────────────────────────────────


def compose_briefing(
    *,
    user_name: str,
    detail: str,
    calendar_events: list[dict],
    emails: list[dict],
    weather: Optional[str],
    news_headlines: list[str],
    location: str,
    vault_notes: Optional[list[dict]] = None,
) -> str:
    """Compose the morning briefing message.

    Args:
        user_name:       Operator name for greeting.
        detail:          ``"concise"``, ``"standard"`` (default), or
                         ``"detailed"``.
        calendar_events: List of dicts with keys: ``time``, ``name``,
                         ``duration``. Empty list = no events.
        emails:          List of dicts with keys: ``sender``, ``subject``,
                         ``snippet``. Empty list = no email to surface.
        weather:         One-line weather string or None.
        news_headlines:  List of headline strings.
        location:        City/location string (for weather label context).
        vault_notes:     List of dicts from ``fetch_vault_section`` — each has
                         ``title``, ``snippet``, ``modified``. None = section
                         omitted (vault not configured). Empty list = vault
                         configured but no recent notes.

    Returns:
        The composed message as a plain text string.
    """
    today = date.today()
    weekday = today.strftime("%A")
    date_str = today.strftime("%B %-d")

    lines: list[str] = []

    # ── Greeting ──────────────────────────────────────────────────────────────
    lines.append(f"Good morning, {user_name}.")
    lines.append("")

    # ── Date ─────────────────────────────────────────────────────────────────
    lines.append(f"Today — {weekday}, {date_str}")

    # ── Calendar ─────────────────────────────────────────────────────────────
    if calendar_events:
        cap = None if detail == "detailed" else (5 if detail == "standard" else 3)
        shown = calendar_events[:cap] if cap else calendar_events
        for ev in shown:
            t = ev.get("time", "")
            name = ev.get("name", "")
            dur = ev.get("duration", "")
            if detail == "concise":
                lines.append(f"• {t} {name}")
            else:
                dur_str = f" ({dur})" if dur else ""
                lines.append(f"• {t} {name}{dur_str}")
        if cap and len(calendar_events) > cap:
            lines.append(f"(+ {len(calendar_events) - cap} more)")
    else:
        lines.append("No events today.")
    lines.append("")

    # ── Email ─────────────────────────────────────────────────────────────────
    max_emails = 2 if detail == "concise" else 5
    if emails:
        lines.append(f"Email — {len(emails)} unread")
        for em in emails[:max_emails]:
            sender = em.get("sender", "")
            subject = em.get("subject", "")
            snippet = em.get("snippet", "")
            lines.append(f"• {sender} — {subject}")
            if detail != "concise" and snippet:
                lines.append(f"  {snippet}")
        if len(emails) > max_emails:
            lines.append(f"(+ {len(emails) - max_emails} more)")
    else:
        lines.append("No email needs attention.")
    lines.append("")

    # ── Weather ───────────────────────────────────────────────────────────────
    if weather:
        loc_label = f" — {location}" if location else ""
        lines.append(f"{weather}{loc_label}")
    else:
        lines.append("(weather unavailable)")
    lines.append("")

    # ── News ──────────────────────────────────────────────────────────────────
    if detail != "concise" and news_headlines:
        lines.append("News")
        n_items = 5 if detail == "detailed" else 3
        for h in news_headlines[:n_items]:
            lines.append(f"• {h}")
        lines.append("")
    elif detail != "concise" and not news_headlines:
        # Only mention missing news in standard/detailed mode.
        pass  # silently skip; news_sources may not be configured

    # ── Notes from your vault ─────────────────────────────────────────────────
    # Omitted entirely (no heading) if vault_notes is None (vault not configured).
    # If vault_notes is an empty list, vault is configured but no recent notes.
    if vault_notes is not None:
        lines.append("Notes from your vault")
        if vault_notes:
            for note in vault_notes:
                title = note.get("title", "")
                snippet = note.get("snippet", "")
                lines.append(f"• {title}")
                if detail != "concise" and snippet:
                    lines.append(f"  {snippet}")
        else:
            lines.append("No notes in the last few days.")
        lines.append("")

    return "\n".join(lines).rstrip()


# ── Memory log ────────────────────────────────────────────────────────────────


def write_memory(
    workspace: Path,
    *,
    sent_at: str,
    event_count: int,
    email_count: int,
    weather_summary: Optional[str],
    news_count: int,
) -> None:
    """Append today's briefing metadata to memory/YYYY-MM-DD.md."""
    today = date.today().isoformat()
    memory_dir = workspace / _MEMORY_SUBDIR
    memory_dir.mkdir(parents=True, exist_ok=True)
    mem_file = memory_dir / f"{today}.md"

    weather_line = weather_summary or "not configured"
    news_line = f"{news_count} headlines" if news_count else "not configured"

    content = (
        f"# {today}\n\n"
        f"## Briefing\n"
        f"- Sent at {sent_at}\n"
        f"- Events: {event_count}\n"
        f"- Emails: {email_count} surfaced\n"
        f"- Weather: {weather_line}\n"
        f"- News: {news_line}\n"
    )
    # Append if file already exists (cron is idempotent; --preview doesn't call
    # write_memory so this only fires on genuine re-sends of the same day).
    if mem_file.exists():
        content = mem_file.read_text() + "\n" + content

    # Atomic write: write to a temp file in the same directory then rename.
    # Prevents a cron + manual run racing on the same YYYY-MM-DD.md file.
    fd, tmp_path = tempfile.mkstemp(dir=memory_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_path, mem_file)
    except Exception:
        # Clean up the temp file if rename fails; don't crash the briefing.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def write_run_record(
    workspace: Path,
    *,
    sent_at_iso: str,
    event_count: int,
    email_count: int,
    partial: bool,
) -> None:
    """Write the per-day JSON run record at memory/briefing-runs/YYYY-MM-DD.json.

    The recommended delivery-proof contract (spec-proactive-delivery-
    monitor-2026-06-10.md §5.4): written atomically, ONLY after gateway
    delivery succeeded — the file's existence is the claim "the user got
    it", with sent_at inside as the timeliness witness. The delivery
    monitor reads this; the markdown memory file above stays as the
    human-readable log.
    """
    runs_dir = workspace / _MEMORY_SUBDIR / "briefing-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "date": date.today().isoformat(),
        "sent_at": sent_at_iso,
        "events_count": event_count,
        "emails_surfaced": email_count,
        "partial": partial,
    }
    fd, tmp_path = tempfile.mkstemp(dir=runs_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp_path, runs_dir / f"{record['date']}.json")
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_last_status(workspace: Path) -> str:
    """Return a human-readable status string from today's (or yesterday's) memory."""
    today = date.today()
    for candidate in [today.isoformat(), _yesterday()]:
        mem_file = workspace / _MEMORY_SUBDIR / f"{candidate}.md"
        if mem_file.exists():
            text = mem_file.read_text()
            sent_match = re.search(r"Sent at (.+)", text)
            if sent_match:
                sent = sent_match.group(1).strip()
                return f"Last briefing: {candidate} at {sent}"
    return "No briefing logged today yet."


def _yesterday() -> str:
    from datetime import timedelta
    return (date.today() - timedelta(days=1)).isoformat()


# ── Message delivery ──────────────────────────────────────────────────────────

_OPENCLAW_BIN_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",   # macOS arm64
    "/usr/local/bin/openclaw",      # macOS x86_64
    "/usr/bin/openclaw",            # Linux
)

# Channel preference when a user is reachable on several. Product default
# (most personal first), not a technical constraint.
_CHANNEL_PRIORITY = (
    "telegram", "whatsapp", "signal", "imessage", "slack",
    "discord", "sms", "matrix",
)


def _bot_id() -> str:
    """The logical bot id this script runs as.

    Scheduled jobs run as the bot's macOS account, which usually equals
    the network.json bot id — but a bot may run on a different account
    (the per-bot ``user`` override), so map account → bot id when one
    matches. Falls back to the account name.
    """
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
    """Channels this bot can actually send on (its own openclaw.json,
    channels.<name>.enabled). None when unreadable — trust external_ids
    alone rather than refusing to send."""
    cfg = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw" / "openclaw.json"
    try:
        channels = json.loads(cfg.read_text()).get("channels", {})
    except (OSError, json.JSONDecodeError):
        return None
    enabled = {
        name for name, c in channels.items()
        if isinstance(c, dict) and c.get("enabled")
    }
    return enabled or None


def _resolve_route(bot_id: str) -> tuple:
    """(channel, target) for the bot's primary user, from network.json.

    `bots.<id>.primary_user.external_ids` is the single source of truth
    for where this bot's person lives. (None, None) when the bot has no
    recorded delivery route — callers degrade gracefully and must NOT
    write a run file.
    """
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
    # The user is only recorded on channels this bot doesn't have —
    # return the best recorded id anyway so the send fails LOUDLY (a
    # miss the monitor reports) instead of silently skipping.
    for channel in _CHANNEL_PRIORITY:
        target = ids.get(channel)
        if target:
            return channel, str(target)
    return None, None


def send_message(bot_id: str, text: str) -> bool:
    """Deliver *text* to the bot's primary user via `openclaw message send`
    (the supported plain-send surface since OC 2026.6 removed POST
    /api/message — docs/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip — caller emits BRIEFING_SKIPPED and does
    NOT write the run file). Raises RuntimeError on a failed send.
    """
    channel, target = _resolve_route(bot_id)
    if channel is None:
        print(
            f"DELIVERY_SKIPPED: no delivery route for {bot_id} "
            "(network.json primary_user.external_ids empty)",
            file=sys.stderr,
        )
        return False
    openclaw = _find_openclaw()
    cmd = [
        openclaw, "message", "send",
        f"--channel={channel}", f"--target={target}",
        f"--message={text}", "--json",
    ]
    # launchd/systemd jobs get a minimal PATH; the openclaw entrypoint is
    # `#!/usr/bin/env node`, so node must be resolvable — prepend the CLI's
    # own bin dir (where node also lives) to the child PATH.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(openclaw) + os.pathsep + env.get("PATH", "")
    # cwd must be bot-readable and env-independent: node resolves its CWD
    # at startup, and under sudo-to-the-bot-account without -H, $HOME (and so
    # Path.home()) still points at the invoking user's untraversable home.
    # The 60s timeout outlives delivery so the CLI is never killed mid-send.
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=pwd.getpwuid(os.getuid()).pw_dir, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"openclaw message send failed: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:400]
        raise RuntimeError(
            f"openclaw message send failed (rc={r.returncode}): {err}"
        )
    return True


# ── Obsidian vault ────────────────────────────────────────────────────────────


def fetch_vault_section(
    vault_path: str,
    *,
    days: int = 3,
    max_notes: int = 3,
    budget_seconds: float = 10.0,
) -> Optional[list[dict]]:
    """Return up to *max_notes* recently modified notes from the Obsidian vault.

    Used by the Morning Briefing's "Notes from your vault" section (opt-in via
    the ``{vault_path}`` template var). Returns None if ``vault_path`` is empty
    or the vault is not accessible — the caller handles graceful omission.

    Args:
        vault_path: Absolute path to the Obsidian vault directory.
                    Empty string or None → returns None (section omitted).
        days: Look-back window for "recent" notes (default 3 days).
        max_notes: Maximum notes to surface (default 3, per spec).
        budget_seconds: Wall-clock budget for the entire vault scan (default 10s).
                        If exceeded, returns None so the morning briefing is
                        delivered on time even when the vault is on a slow
                        network drive or flaky external mount.

    Returns:
        List of dicts: {title, snippet, modified} — or None if not configured
        or the vault is unavailable. "snippet" is the first non-empty,
        non-heading content line (up to 200 characters).

    The function is stdlib-only (no imports from evolve_admin) so briefing.py
    remains a standalone script deployable to any bot workspace.
    """
    import time as _time

    if not vault_path or not vault_path.strip():
        return None

    try:
        vault = Path(vault_path.strip()).expanduser().resolve()
    except (TypeError, ValueError):
        _warn(f"vault_section: invalid vault path {vault_path!r}")
        return None

    if not vault.is_dir():
        _warn(f"vault_section: vault not found at {vault_path!r}")
        return None

    _budget_start = _time.monotonic()

    def _over_budget() -> bool:
        return _time.monotonic() - _budget_start > budget_seconds

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()

    notes: list[dict] = []
    try:
        md_files = list(vault.rglob("*.md"))
    except (PermissionError, OSError) as exc:
        _warn(f"vault_section: cannot read vault: {exc}")
        return None

    if _over_budget():
        _warn(
            f"[briefing] WARNING: vault scan exceeded {budget_seconds:.0f}s budget; "
            f"skipping vault section"
        )
        return None

    for fpath in md_files:
        # Budget check on each iteration — cheap, ensures we stop promptly.
        if _over_budget():
            _warn(
                f"[briefing] WARNING: vault scan exceeded {budget_seconds:.0f}s budget; "
                f"skipping vault section"
            )
            return None

        # Path-traversal guard: file must be inside the vault.
        try:
            fpath.resolve().relative_to(vault)
        except ValueError:
            continue

        try:
            mtime = fpath.stat().st_mtime
        except OSError:
            continue

        if mtime < cutoff_ts:
            continue

        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue

        title = _vault_note_title(fpath, text)
        snippet = _vault_first_content_line(text)
        modified = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        notes.append({"title": title, "snippet": snippet, "modified": modified, "_mtime": mtime})

    if not notes:
        return []

    notes.sort(key=lambda n: n["_mtime"], reverse=True)
    result = notes[:max_notes]
    for n in result:
        del n["_mtime"]
    return result


def _vault_note_title(path: Path, text: str) -> str:
    """Return the note title: first H1 heading, or the filename stem."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ")


def _vault_first_content_line(text: str) -> str:
    """Return the first non-empty, non-heading, non-frontmatter line (up to 200 chars)."""
    in_frontmatter = False
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        return stripped[:200]
    return ""


# ── Google data fetching stubs ────────────────────────────────────────────────
#
# Calendar and email are fetched via the calendar and gmail skills (V2.1-3
# split the legacy combined GOG skill into separate gmail and calendar skills
# that share one google_workspace:<bot_id> OAuth profile). The bot's agent
# calls the calendar and gmail tool functions directly; briefing.py does
# not need to re-implement the OAuth flow. These stubs produce empty data
# so the script remains self-contained for unit testing; in production the
# provisioner wires the real helpers at deploy time.
#
# V1.1-1's provisioner will substitute the real implementations here when
# it materializes briefing.py onto a bot. Until then, the stubs ensure
# a graceful preview with placeholder data.


def fetch_calendar_events(time_zone: str) -> tuple[list[dict], Optional[str]]:
    """Return (events, error_note). Events are dicts: time, name, duration."""
    # Production: use the calendar skill (calendar.readonly) to list today's events.
    # Stub for testing: return empty list with a note.
    return [], None  # No events — provisioner will replace this stub at deploy time.


def fetch_emails() -> tuple[list[dict], Optional[str]]:
    """Return (emails, error_note). Emails are dicts: sender, subject, snippet."""
    # Production: use the gmail skill (gmail.readonly) to fetch recent unread.
    # Stub for testing: return empty list with a note.
    return [], None  # No emails — provisioner will replace this stub at deploy time.


# ── Utilities ─────────────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    """Print a warning to stderr (non-fatal)."""
    print(f"WARNING: {msg}", file=sys.stderr)


def _workspace_from_args(args: argparse.Namespace) -> Path:
    """Resolve the workspace root directory."""
    if getattr(args, "workspace", None):
        return Path(args.workspace)
    # Fallback: infer from the script's own location (scripts/ → parent).
    return Path(__file__).parent.parent.resolve()


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_send(args: argparse.Namespace, dry_run: bool = False) -> int:
    """Compose and send (or preview) the morning briefing.

    Returns 0 on success, 1 on hard failure (Gmail+Calendar both down).
    BRIEFING_PARTIAL: is emitted when some but not all data sources work.
    """
    workspace = _workspace_from_args(args)
    today = date.today().isoformat()
    partial = False

    # ── Fetch calendar ────────────────────────────────────────────────────────
    events, cal_err = fetch_calendar_events(args.time_zone)
    if cal_err:
        _warn(f"calendar: {cal_err}")
        partial = True

    # ── Fetch email ───────────────────────────────────────────────────────────
    emails, email_err = fetch_emails()
    if email_err:
        _warn(f"email: {email_err}")
        partial = True

    # ── Fetch weather (direct HTTP — no plugin required) ──────────────────────
    weather: Optional[str] = None
    if args.location and args.location.strip():
        weather = fetch_weather(args.location)
        if weather is None:
            _warn("weather fetch failed — skipping weather section")
            partial = True

    # ── Fetch news (direct HTTP — no plugin required) ─────────────────────────
    news_headlines: list[str] = []
    if args.news_sources and args.news_sources.strip():
        news_headlines = fetch_news(args.news_sources)
        if not news_headlines:
            _warn("news fetch returned no headlines — skipping news section")

    # ── Fetch vault notes (Obsidian, opt-in via --vault-path) ─────────────────
    vault_path_arg: str = getattr(args, "vault_path", "") or ""
    vault_notes: Optional[list[dict]] = fetch_vault_section(vault_path_arg) if vault_path_arg.strip() else None

    # ── Both primary sources (calendar + email) failed ───────────────────────
    if cal_err and email_err:
        msg = (
            f"Morning briefing couldn't run on {today} — "
            f"Gmail/Calendar access needs a refresh. "
            f"You can fix this from the admin dashboard."
        )
        if dry_run:
            print(msg)
        else:
            try:
                send_message(_bot_id(), msg)
            except RuntimeError as exc:
                _warn(str(exc))
        print(f"BRIEFING_FAILED: {today} gmail+calendar-unavailable", flush=True)
        return 1

    # ── Compose ───────────────────────────────────────────────────────────────
    message = compose_briefing(
        user_name=args.user_name,
        detail=args.detail,
        calendar_events=events,
        emails=emails,
        weather=weather,
        news_headlines=news_headlines,
        location=args.location or "",
        vault_notes=vault_notes,
    )

    # ── Deliver ───────────────────────────────────────────────────────────────
    if dry_run:
        print(message)
    else:
        try:
            accepted = send_message(_bot_id(), message)
        except RuntimeError as exc:
            _warn(f"delivery failed: {exc}")
            print(f"BRIEFING_FAILED: {today} delivery-error", flush=True)
            return 1
        if not accepted:
            # No route to a person — nothing was delivered, so no memory
            # log and no run file (the delivery monitor must not read this
            # as a delivered briefing).
            print(f"BRIEFING_SKIPPED: {today} no-delivery-route", flush=True)
            return 0

    # ── Memory log + run record ───────────────────────────────────────────────
    if not dry_run:
        now_local = datetime.now().astimezone()
        write_memory(
            workspace,
            sent_at=now_local.strftime("%H:%M"),
            event_count=len(events),
            email_count=len(emails),
            weather_summary=weather,
            news_count=len(news_headlines),
        )
        try:
            write_run_record(
                workspace,
                sent_at_iso=now_local.isoformat(timespec="seconds"),
                event_count=len(events),
                email_count=len(emails),
                partial=partial,
            )
        except OSError as exc:
            # The briefing was delivered; a failed run record must not turn
            # a successful send into a reported failure. The delivery
            # monitor will report the window unmeasurable, which is honest.
            _warn(f"run-record write failed: {exc}")

    # ── Signal ───────────────────────────────────────────────────────────────
    signal = "BRIEFING_PARTIAL" if partial else "BRIEFING_SENT"
    print(
        f"{signal}: {today} {len(events)}ev {len(emails)}em",
        flush=True,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = _workspace_from_args(args)
    print(read_last_status(workspace))
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Morning Briefing — compose and deliver the daily message",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--workspace",
        help="Path to the bot's workspace directory (default: auto-detected)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # send
    send_p = sub.add_parser("send", help="Compose and send the morning briefing")
    _add_briefing_flags(send_p)

    # preview (same flags, dry-run)
    prev_p = sub.add_parser(
        "preview", help="Preview the briefing (print to stdout, don't send)"
    )
    _add_briefing_flags(prev_p)

    # status
    sub.add_parser("status", help="Show last-run info from memory log")

    return p


def _add_briefing_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user-name", default="there", help="Operator's name (for greeting)")
    p.add_argument(
        "--time-zone",
        default="America/Los_Angeles",
        help="Operator's timezone (IANA format, e.g. America/New_York)",
    )
    p.add_argument(
        "--detail",
        default="standard",
        choices=["concise", "standard", "detailed"],
        help="Briefing detail level",
    )
    p.add_argument(
        "--location",
        default="",
        help="City or ZIP for weather (e.g. 'Seattle, WA'). Leave blank to skip.",
    )
    p.add_argument(
        "--news-sources",
        default="",
        help=(
            "Comma-separated RSS URLs or topic keywords "
            "(e.g. 'hacker news,https://feeds.npr.org/1001/rss.xml'). "
            "Leave blank to skip."
        ),
    )
    p.add_argument(
        "--vault-path",
        default="",
        help=(
            "Absolute path to your Obsidian vault directory. "
            "If set, the briefing includes a 'Notes from your vault' section "
            "with your 3 most recently modified notes. Leave blank to skip."
        ),
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "send":
        sys.exit(cmd_send(args, dry_run=False))
    elif args.command == "preview":
        sys.exit(cmd_send(args, dry_run=True))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
