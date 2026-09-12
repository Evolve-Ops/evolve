#!/usr/bin/env python3
"""
email_triage.py — Email Triage CLI

Reads recent unread Gmail, classifies by urgency + category using a
quick LLM pass (haiku-tier), and delivers a short summary via the
bot's primary channel.

Commands:
    run      — fetch email, classify, send summary, write run log
    status   — print last-run info from today's (or yesterday's) run file

Usage:
    python3 scripts/email_triage.py run [--max-emails N] [--dry-run]
    python3 scripts/email_triage.py status

Graceful degradation:
  - If Gmail token is missing → TRIAGE_FAILED: gmail-not-configured
  - If LLM classifier fails → fall back to heuristics, emit TRIAGE_PARTIAL:
  - If fewer than 2 unread emails → skip with short notice or silent exit
  - If gateway delivery fails → TRIAGE_FAILED: delivery-error

All network calls use urllib (stdlib only — no pip packages required).
"""

from __future__ import annotations

import argparse
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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

_GMAIL_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_GMAIL_MSG_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_CLASSIFIER_MODEL = "claude-haiku-4-5"
_HTTP_TIMEOUT = 15
_MIN_EMAILS_TO_TRIAGE = 2
_WORKSPACE_SUBDIR = "email_triage"
_RUNS_SUBDIR = "runs"
_CONFIG_FILENAME = "config.json"

_DEFAULT_CONFIG: dict = {
    "schedule_times": ["07:30", "13:00"],
    "max_emails_per_run": 25,
    "archive_promotional": False,
    "urgent_senders": [],
    "category_overrides": {},
}

# ── Classifier prompt ─────────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = (
    "You are an email triage assistant. Given a list of emails (id, sender, "
    "subject, snippet), classify each one.\n\n"
    "For each email return a JSON array where each object has:\n"
    "  \"id\": the email id (exact string, unchanged)\n"
    "  \"urgency\": one of urgent | important | routine | promotional | spam-likely\n"
    "  \"category\": one of financial | legal | family | work-project | vendor | "
    "news | personal | other\n"
    "  \"action\": one of reply-now | reply-today | can-wait | can-ignore | archive\n\n"
    "Urgency rules:\n"
    "- urgent: needs a response today or involves time-sensitive action\n"
    "- important: worth reading soon but not time-critical\n"
    "- routine: normal correspondence, no time pressure\n"
    "- promotional: marketing, newsletters, offers\n"
    "- spam-likely: unsolicited, suspicious, or clearly junk\n\n"
    "Return ONLY the JSON array. No prose, no markdown fences."
)


# ── Utilities ─────────────────────────────────────────────────────────────────


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def _workspace_root(args: argparse.Namespace) -> Path:
    if getattr(args, "workspace", None):
        return Path(args.workspace)
    # scripts/ lives one level below workspace root
    return Path(__file__).parent.parent.resolve()


#: Pod network config — resolves the delivery route (primary user's channel).
_NETWORK_JSON = Path("/Users/Shared/evolve/network.json")


def _triage_dir(workspace: Path) -> Path:
    return workspace / _WORKSPACE_SUBDIR


def _runs_dir(workspace: Path) -> Path:
    return _triage_dir(workspace) / _RUNS_SUBDIR


def _openclaw_json_path(workspace: Path) -> Path:
    candidate = workspace.parent / "openclaw.json"
    if candidate.exists():
        return candidate
    return Path("~/.openclaw/openclaw.json").expanduser()


# ── Config ────────────────────────────────────────────────────────────────────


def load_config(workspace: Path) -> dict:
    """Load config.json; create with defaults if absent."""
    config_path = _triage_dir(workspace) / _CONFIG_FILENAME
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            # Merge any missing keys from defaults
            for k, v in _DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except (json.JSONDecodeError, OSError) as exc:
            _warn(f"config read failed ({exc}), using defaults")
    # First-run: write defaults
    _triage_dir(workspace).mkdir(parents=True, exist_ok=True)
    _atomic_write_json(config_path, _DEFAULT_CONFIG)
    return dict(_DEFAULT_CONFIG)


# ── Auth ──────────────────────────────────────────────────────────────────────


def _read_openclaw(openclaw_json: Path) -> dict:
    try:
        return json.loads(openclaw_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"cannot read openclaw.json: {exc}")
        return {}


def resolve_gmail_token(openclaw_json: Path) -> Optional[str]:
    """Return an access_token for Gmail, or None if not configured.

    Tries two paths:
    1. openclaw.json → integrations.gmail.token.access_token  (post-V2.1-3 gmail skill)
    2. openclaw.json → auth.profiles[google_workspace:*].token.access_token  (GOG/legacy)
    """
    oc = _read_openclaw(openclaw_json)

    # Path 1: new gmail skill
    gmail_cfg = (oc.get("integrations") or {}).get("gmail") or {}
    token_dict = gmail_cfg.get("token") or {}
    access_token = token_dict.get("access_token", "")
    if access_token:
        return access_token

    # Path 2: GOG auth profile (legacy)
    profiles = (oc.get("auth") or {}).get("profiles") or {}
    for k, v in profiles.items():
        if k.startswith("google_workspace:") and isinstance(v, dict):
            inner = v.get("token") or {}
            tok = inner.get("access_token", "")
            if tok:
                return tok

    return None


def resolve_anthropic_key(openclaw_json: Path) -> Optional[str]:
    """Return the Anthropic API key, or None.

    Tries: env ANTHROPIC_API_KEY first, then openclaw.json auth profiles.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key.strip():
        return env_key.strip()

    oc = _read_openclaw(openclaw_json)
    profiles = (oc.get("auth") or {}).get("profiles") or {}
    for k, v in profiles.items():
        if "anthropic" in k.lower() and isinstance(v, dict):
            key = v.get("api_key") or v.get("token", {}).get("api_key", "")
            if key:
                return key

    # Also check plugins.entries.anthropic for API key in env
    plugins = (oc.get("plugins") or {}).get("entries") or {}
    ant = plugins.get("anthropic") or {}
    key = ant.get("api_key", "")
    if key:
        return key

    return None


# ── Gmail fetching ────────────────────────────────────────────────────────────


def _gmail_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gmail API HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Gmail API network error: {exc}") from exc


def fetch_unread_emails(token: str, max_emails: int) -> list[dict]:
    """Fetch recent unread emails from INBOX.

    Returns a list of dicts: {id, from_addr, subject, snippet}.
    Raises RuntimeError on API failure.
    """
    params = urllib.parse.urlencode({
        "q": "is:unread in:inbox",
        "maxResults": max_emails,
    })
    list_data = _gmail_get(f"{_GMAIL_LIST_URL}?{params}", token)
    messages = list_data.get("messages") or []

    emails: list[dict] = []
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        # Fetch metadata only (much cheaper than full message)
        params_msg = urllib.parse.urlencode({
            "format": "metadata",
            "metadataHeaders": ["From", "Subject"],
        }, doseq=True)
        try:
            msg = _gmail_get(f"{_GMAIL_MSG_URL.format(id=msg_id)}?{params_msg}", token)
        except RuntimeError as exc:
            _warn(f"skipping message {msg_id}: {exc}")
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in (msg.get("payload") or {}).get("headers") or []
        }
        snippet = (msg.get("snippet") or "")[:200]
        emails.append({
            "id": msg_id,
            "from_addr": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "snippet": snippet,
        })

    return emails


# ── Heuristic classifier (fallback) ──────────────────────────────────────────

_PROMO_KEYWORDS = re.compile(
    r"unsubscribe|newsletter|\b\d+%\s*off\b|deal|sale|offer|promotion|coupon",
    re.IGNORECASE,
)
_SPAM_PATTERN = re.compile(
    r"<?(noreply|no-reply|donotreply|do-not-reply)@",
    re.IGNORECASE,
)
_URGENT_KEYWORDS = re.compile(
    r"urgent|action required|deadline|invoice|payment due|overdue",
    re.IGNORECASE,
)
_FINANCIAL_KEYWORDS = re.compile(
    r"bank|invoice|payment|receipt|statement|tax|billing|charge|refund",
    re.IGNORECASE,
)
_LEGAL_KEYWORDS = re.compile(
    r"contract|agreement|legal|attorney|counsel|lawsuit|compliance|gdpr",
    re.IGNORECASE,
)


def _heuristic_classify(email: dict, config: dict) -> dict:
    from_addr = email.get("from_addr", "").lower()
    subject = email.get("subject", "").lower()
    combined = f"{from_addr} {subject}"

    # Urgency
    urgency = "routine"
    action = "can-wait"

    if any(
        addr.lower() in from_addr
        for addr in (config.get("urgent_senders") or [])
    ):
        urgency = "urgent"
        action = "reply-now"
    elif _SPAM_PATTERN.search(from_addr):
        urgency = "spam-likely"
        action = "can-ignore"
    elif _PROMO_KEYWORDS.search(combined):
        urgency = "promotional"
        action = "archive"
    elif _URGENT_KEYWORDS.search(subject):
        urgency = "important"
        action = "reply-today"

    # Category
    category = "other"
    if _FINANCIAL_KEYWORDS.search(combined):
        category = "financial"
    elif _LEGAL_KEYWORDS.search(combined):
        category = "legal"
    elif urgency == "spam-likely" or urgency == "promotional":
        category = "vendor"

    return {
        "id": email["id"],
        "urgency": urgency,
        "category": category,
        "action": action,
    }


def classify_heuristic(emails: list[dict], config: dict) -> list[dict]:
    return [_heuristic_classify(e, config) for e in emails]


# ── LLM classifier ────────────────────────────────────────────────────────────


def classify_with_llm(
    emails: list[dict],
    api_key: str,
    config: dict,
) -> tuple[list[dict], str]:
    """Classify emails using the Anthropic API.

    Returns (results, classifier) where classifier is 'llm' or 'heuristic'.
    Results is a list of {id, urgency, category, action}.
    """
    # Truncate input for API call — send only the fields the classifier needs
    payload_emails = [
        {
            "id": e["id"],
            "from_addr": e["from_addr"],
            "subject": e["subject"],
            "snippet": e["snippet"],
        }
        for e in emails
    ]

    request_body = {
        "model": _CLASSIFIER_MODEL,
        "max_tokens": 1024,
        "system": _CLASSIFIER_SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": json.dumps(payload_emails, ensure_ascii=False),
            }
        ],
    }

    req = urllib.request.Request(
        _ANTHROPIC_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read())
        raw_text = resp_data["content"][0]["text"].strip()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError) as exc:
        _warn(f"LLM classifier failed ({exc}), falling back to heuristics")
        return classify_heuristic(emails, config), "heuristic"

    # Parse the JSON array from the response
    # Strip any accidental markdown fences
    text = re.sub(r"^```[a-z]*\n?", "", raw_text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    try:
        results = json.loads(text.strip())
        if not isinstance(results, list):
            raise ValueError("not a list")
        # Validate structure
        valid = []
        id_to_email = {e["id"]: e for e in emails}
        for item in results:
            if not isinstance(item, dict) or "id" not in item:
                continue
            if item["id"] not in id_to_email:
                continue
            valid.append({
                "id": item["id"],
                "urgency": item.get("urgency", "routine"),
                "category": item.get("category", "other"),
                "action": item.get("action", "can-wait"),
            })
        # Fill in any missing emails with heuristics
        classified_ids = {v["id"] for v in valid}
        missing = [e for e in emails if e["id"] not in classified_ids]
        if missing:
            valid.extend(classify_heuristic(missing, config))
        return valid, "llm"
    except (json.JSONDecodeError, ValueError) as exc:
        _warn(f"LLM response parse failed ({exc}), falling back to heuristics")
        return classify_heuristic(emails, config), "heuristic"


# ── Config overrides ──────────────────────────────────────────────────────────


def apply_config_overrides(classified: list[dict], emails: list[dict], config: dict) -> list[dict]:
    """Apply urgent_senders and category_overrides from config."""
    id_to_email = {e["id"]: e for e in emails}
    urgent_senders = [s.lower() for s in (config.get("urgent_senders") or [])]
    category_overrides = {
        k.lower(): v
        for k, v in (config.get("category_overrides") or {}).items()
    }

    for item in classified:
        email = id_to_email.get(item["id"]) or {}
        from_lower = email.get("from_addr", "").lower()

        # urgent_senders override
        if any(s in from_lower for s in urgent_senders):
            item["urgency"] = "urgent"
            item["action"] = "reply-now"

        # category_overrides: key is a sender-pattern (substring match)
        for pattern, cat in category_overrides.items():
            if pattern in from_lower:
                item["category"] = cat
                break

    return classified


# ── Message composition ───────────────────────────────────────────────────────


def _sender_display_name(from_addr: str) -> str:
    """Extract display name from 'Name <email>' or return domain from email."""
    m = re.match(r'^"?([^"<]+)"?\s*<', from_addr)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    # Fall back to email domain
    m2 = re.search(r"@([\w.-]+)", from_addr)
    if m2:
        return m2.group(1)
    return from_addr[:40] if from_addr else "Unknown"


def compose_summary(
    emails: list[dict],
    classified: list[dict],
    run_at: datetime,
) -> str:
    """Compose the triage summary message."""
    total = len(emails)
    if total < _MIN_EMAILS_TO_TRIAGE:
        return f"Email triage — {run_at.strftime('%b %-d %-I:%M %p')}\n\nNo unread emails to triage."

    id_to_email = {e["id"]: e for e in emails}

    urgency_buckets: dict[str, list[dict]] = {
        "urgent": [],
        "important": [],
        "routine": [],
        "promotional": [],
        "spam-likely": [],
    }
    for item in classified:
        bucket = urgency_buckets.get(item["urgency"], urgency_buckets["routine"])
        bucket.append(item)

    urgent_count = len(urgency_buckets["urgent"])
    important_count = len(urgency_buckets["important"])
    routine_count = len(urgency_buckets["routine"])
    promo_count = len(urgency_buckets["promotional"])

    lines: list[str] = []
    lines.append(f"Email triage — {run_at.strftime('%b %-d %-I:%M %p')}")
    lines.append("")

    # Summary line
    parts = []
    if urgent_count:
        parts.append(f"{urgent_count} urgent")
    if important_count:
        parts.append(f"{important_count} important")
    if routine_count:
        parts.append(f"{routine_count} routine")
    if promo_count:
        parts.append(f"{promo_count} promotional")
    lines.append(f"{total} emails | {', '.join(parts) if parts else 'all routine'}")
    lines.append("")

    # Urgent section
    if urgency_buckets["urgent"]:
        lines.append("Urgent")
        for item in urgency_buckets["urgent"]:
            em = id_to_email.get(item["id"]) or {}
            sender = _sender_display_name(em.get("from_addr", ""))
            subject = em.get("subject", "(no subject)")
            lines.append(f"• {sender} — {subject}")
        lines.append("")
    else:
        lines.append("Nothing urgent.")
        lines.append("")

    # Important section
    if urgency_buckets["important"]:
        lines.append("Important")
        for item in urgency_buckets["important"]:
            em = id_to_email.get(item["id"]) or {}
            sender = _sender_display_name(em.get("from_addr", ""))
            subject = em.get("subject", "(no subject)")
            lines.append(f"• {sender} — {subject}")
        lines.append("")

    return "\n".join(lines).rstrip()


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
    /api/message — internal/spec-gallery-delivery-convention-2026-06-11.md).

    True = the channel accepted the send. False = no delivery route
    configured (graceful skip — caller does NOT write a run record).
    Raises RuntimeError on a failed send.
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


# ── Run result storage ────────────────────────────────────────────────────────


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_run_result(
    workspace: Path,
    *,
    run_at: datetime,
    emails: list[dict],
    classified: list[dict],
    classifier: str,
) -> None:
    """Write the per-run result to workspace/email_triage/runs/YYYY-MM-DD-HHMM.json."""
    stamp = run_at.strftime("%Y-%m-%d-%H%M")
    path = _runs_dir(workspace) / f"{stamp}.json"

    urgency_counts: dict[str, int] = {
        "urgent": 0, "important": 0, "routine": 0, "promotional": 0, "spam-likely": 0,
    }
    for item in classified:
        urg = item.get("urgency", "routine")
        urgency_counts[urg] = urgency_counts.get(urg, 0) + 1

    id_to_email = {e["id"]: e for e in emails}
    combined_emails = []
    for item in classified:
        em = id_to_email.get(item["id"]) or {}
        combined_emails.append({
            "id": item["id"],
            "from_addr": em.get("from_addr", ""),
            "subject": em.get("subject", ""),
            "urgency": item.get("urgency", "routine"),
            "category": item.get("category", "other"),
            "action": item.get("action", "can-wait"),
        })

    result = {
        "run_at": run_at.isoformat(timespec="seconds"),
        "email_count": len(emails),
        "urgent_count": urgency_counts.get("urgent", 0),
        "important_count": urgency_counts.get("important", 0),
        "routine_count": urgency_counts.get("routine", 0),
        "promotional_count": urgency_counts.get("promotional", 0),
        "classifier": classifier,
        "emails": combined_emails,
    }
    _atomic_write_json(path, result)


def read_last_run_status(workspace: Path) -> str:
    """Return a human-readable status line from today's (or yesterday's) run."""
    runs = _runs_dir(workspace)
    if not runs.exists():
        return "No triage run logged today yet."

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    for prefix in [today, yesterday]:
        candidates = sorted(runs.glob(f"{prefix}-*.json"), reverse=True)
        if candidates:
            try:
                data = json.loads(candidates[0].read_text())
                run_at = data.get("run_at", "")
                ec = data.get("email_count", 0)
                uc = data.get("urgent_count", 0)
                return (
                    f"Last run: {run_at} — "
                    f"{ec} emails, {uc} urgent"
                )
            except (json.JSONDecodeError, OSError):
                pass

    return "No triage run logged today yet."


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace, dry_run: bool = False) -> int:
    """Fetch, classify, send, and log one triage run.

    Returns 0 on success, 1 on hard failure.
    TRIAGE_PARTIAL: emitted when classifier falls back to heuristics.
    """
    workspace = _workspace_root(args)
    openclaw_json = _openclaw_json_path(workspace)
    config = load_config(workspace)
    run_at = datetime.now()
    today = date.today().isoformat()
    partial = False

    # ── Max emails from args or config ────────────────────────────────────────
    max_emails = getattr(args, "max_emails", None) or config.get("max_emails_per_run", 25)

    # ── Resolve Gmail token ───────────────────────────────────────────────────
    gmail_token = resolve_gmail_token(openclaw_json)
    if not gmail_token:
        print(f"TRIAGE_FAILED: {today} gmail-not-configured", flush=True)
        return 1

    # ── Fetch unread emails ───────────────────────────────────────────────────
    try:
        emails = fetch_unread_emails(gmail_token, max_emails)
    except RuntimeError as exc:
        _warn(f"Gmail fetch failed: {exc}")
        print(f"TRIAGE_FAILED: {today} gmail-fetch-error", flush=True)
        return 1

    if len(emails) < _MIN_EMAILS_TO_TRIAGE:
        if dry_run:
            print(f"No triage needed — only {len(emails)} unread email(s).")
        else:
            # Nothing to triage — don't bother the operator
            print(f"TRIAGE_SENT: {today} 0urgent 0important {len(emails)}total (below threshold)", flush=True)
        return 0

    # ── Classify ──────────────────────────────────────────────────────────────
    api_key = resolve_anthropic_key(openclaw_json)
    if api_key:
        classified, classifier = classify_with_llm(emails, api_key, config)
        if classifier == "heuristic":
            partial = True
    else:
        _warn("No Anthropic API key found — using heuristic classification")
        classified = classify_heuristic(emails, config)
        classifier = "heuristic"
        partial = True

    # Apply config overrides (urgent_senders, category_overrides)
    classified = apply_config_overrides(classified, emails, config)

    # ── Compose message ───────────────────────────────────────────────────────
    message = compose_summary(emails, classified, run_at)

    # ── Deliver ───────────────────────────────────────────────────────────────
    if dry_run:
        print(message)
    else:
        try:
            accepted = send_message(_bot_id(), message)
        except RuntimeError as exc:
            _warn(f"delivery failed: {exc}")
            print(f"TRIAGE_FAILED: {today} delivery-error", flush=True)
            return 1
        if not accepted:
            # No route to a person — nothing was delivered; skip the run
            # record so status/history don't read this as a sent triage.
            print(f"TRIAGE_SKIPPED: {today} no-delivery-route", flush=True)
            return 0

    # ── Write run result ──────────────────────────────────────────────────────
    try:
        write_run_result(
            workspace,
            run_at=run_at,
            emails=emails,
            classified=classified,
            classifier=classifier,
        )
    except OSError as exc:
        _warn(f"run result write failed: {exc}")
        # Non-fatal — summary was already sent

    # ── Emit signal ───────────────────────────────────────────────────────────
    urgent_count = sum(1 for c in classified if c.get("urgency") == "urgent")
    important_count = sum(1 for c in classified if c.get("urgency") == "important")
    signal = "TRIAGE_PARTIAL" if partial else "TRIAGE_SENT"
    print(
        f"{signal}: {today} {urgent_count}urgent {important_count}important {len(emails)}total",
        flush=True,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args)
    print(read_last_run_status(workspace))
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Email Triage — classify unread email and deliver a summary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--workspace",
        help="Path to the bot's workspace directory (default: auto-detected)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Fetch email, classify, and send summary")
    run_p.add_argument(
        "--max-emails",
        type=int,
        default=None,
        metavar="N",
        help="Max unread emails to classify (default: from config, fallback 25)",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary to stdout instead of sending",
    )

    # status
    sub.add_parser("status", help="Show last-run info from today's run file")

    return p


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        dry = getattr(args, "dry_run", False)
        sys.exit(cmd_run(args, dry_run=dry))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
