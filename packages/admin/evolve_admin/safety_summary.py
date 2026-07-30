"""safety_summary — Plain-language per-bot safety summary.

Spec: docs/evolve-mvp-sprint.md §"Spec 2 — B2.a Plain-language safety summary".

Reads a bot's config (openclaw.json + exec-approvals.json) and recent
signals from the unified signal store, then renders them into the
"Plex test" friendly shape:

    {
      "bot_id": "team_bot_a",
      "can_do":    ["Read its project folder", "Send messages to Slack #ops", ...],
      "cannot_do": ["Read other bots' folders", "Access banking credentials", ...],
      "this_week": {"headline": "All quiet",  "items": [...]} ,
      "as_of":     "2026-05-12T18:00:00Z",
      "sources":   {...},   # diagnostic — which files we successfully read
      "read_errors": [...]  # advisory — surfaced as "we couldn't check X" in UI
    }

The summary is a strict read-only view; this module never writes
config files. The "advanced view" elsewhere on the security page
remains the source of truth for raw technical detail.

Voice rules (per [feedback_design_constraint_mildly_tech_capable]):
  * No undefined jargon (no "exec-approvals", no "ACL", no
    "workspaceOnly").
  * Lists are short — top 5-6 items each, not exhaustive.
  * Cannot-do list is concrete and protective ("Read your other
    bots' folders") rather than generic ("Things outside its
    permissions").
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso


# ─────────────────────────────────────────────────────────────────────
# Channel + plugin friendly labels (no jargon)
# ─────────────────────────────────────────────────────────────────────

_CHANNEL_LABEL: dict[str, str] = {
    "telegram":  "Telegram",
    "slack":     "Slack",
    "discord":   "Discord",
    "email":     "email",
    "sms":       "SMS",
    "whatsapp":  "WhatsApp",
    "signal":    "Signal",
    "webhook":   "a configured webhook",
}

# Plugins that translate to capabilities a non-technical user
# would recognise. "Skill" terminology mirrors the user-facing
# vocabulary (skills = means, applications = goals).
_PLUGIN_CAPABILITY: dict[str, str] = {
    "brave":    "Look up vendor websites and answer factual questions",
    "google":   "Look up vendor websites and answer factual questions",
    "search":   "Look up vendor websites and answer factual questions",
    "github":   "Read its own GitHub repo and open pull requests",
    "obsidian": "Read and update its Obsidian vault",
    "memory":   "Remember things you tell it across conversations",
    "gog":      "Read your Gmail and Google Calendar (only after you sign in)",
    "gmail":    "Read your Gmail (only after you sign in)",
    "calendar": "Read your Google Calendar (only after you sign in)",
    "weather":  "Look up the weather",
    "news":     "Look up the news",
    "linear":   "Read and update Linear tickets",
    "notion":   "Read and update Notion pages",
}

# Hard-coded protective statements every bot in the pod gets.
# These must be backed by a real architectural guarantee or measurement —
# never include a claim that can't be verified. The per-bot macOS user
# boundary genuinely prevents cross-bot file reads (enforced by the OS).
# "Run system commands without your approval" holds when exec-approvals
# is the gating mechanism, which is the architectural default.
#
# Removed (2026-05-13):
#   "Send messages to a channel it isn't connected to" — tautological
#   "Spend money beyond its cost cap" — budget_hawk vetoes NEW ARBITER
#     proposals only; the gateway keeps billing turns. The claim was a lie.
#   "Access your banking or financial credentials" — no scan, no detection;
#     held by a developer comment, not a measurement.
_UNIVERSAL_CANNOT_DO: tuple[str, ...] = (
    "Read your other bots' folders or conversations",
    "Run system commands without your approval",
)


# ─────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ThisWeekItem:
    """One notable security/sysadmin event from the last 7 days."""
    when: str           # ISO date, e.g. "2026-05-10"
    headline: str       # plain-language: "Tried to read another bot's folder — blocked"
    severity: str       # "info" | "warn" | "alert"
    producer: str       # "security_warden" | "sysadmin_watchdog"


@dataclass
class BotSafetySummary:
    bot_id: str
    bot_label: str               # display name; for now = bot_id
    can_do: list[str] = field(default_factory=list)
    cannot_do: list[str] = field(default_factory=list)
    this_week_headline: str = "All quiet"
    this_week_items: list[ThisWeekItem] = field(default_factory=list)
    as_of: str = ""
    sources: dict[str, Any] = field(default_factory=dict)
    read_errors: list[str] = field(default_factory=list)
    # V1.5-3: upstream OC version + exec-policy compliance. The UI
    # renders these as small chips on the card so the operator can see
    # at a glance which bots are eligible for the upstream
    # ``openclaw exec-policy`` adoption path. ``None`` for either means
    # "couldn't determine" — UI shows a neutral chip, not red/green.
    #
    # ``exec_policy_state`` (added 2026-05-25 for the chip pivot — see
    # docs/spec-app-derived-permissions-2026-05-24.md §7) carries the
    # explicit chip color: "green" | "yellow" | "red" | "grey" | "unknown".
    # The ``compliant`` bool is kept for back-compat with downstream JSON
    # consumers (compliant=True iff state=="green").
    upstream_version: str | None = None
    exec_policy_compliant: bool | None = None
    exec_policy_state: str | None = None
    exec_policy_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["this_week_items"] = [asdict(it) for it in self.this_week_items]
        return d


# ─────────────────────────────────────────────────────────────────────
# Helpers — config reads (direct + sudo /bin/cat fallback)
# ─────────────────────────────────────────────────────────────────────

def _read_json(path: Path, *, timeout: float = 5.0) -> tuple[dict | None, str | None]:
    """Read JSON from path. Returns (parsed_dict, error_str).

    Mirrors permissions.inventory._read_json_file: try direct read,
    fall back to sudo /bin/cat on PermissionError (per CLAUDE.md the
    admin server runs as `evolve` and has the right sudoers grant).
    Returns ``(None, "not_found")`` if the file does not exist.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        text = None
    except OSError as exc:
        return None, f"os_error: {exc}"

    if text is None:
        try:
            # Route the cat path through the platform-profile seam so the
            # invoked binary matches the /etc/sudoers.d/evolve grant (both
            # derive from profile.cat). `-n` is retained — the admin server has
            # no TTY, so an ungranted path fails fast instead of prompting.
            from platform_profile import get_profile
            r = subprocess.run(
                ["sudo", "-n", get_profile().cat, str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError as exc:
            return None, f"sudo_error: {exc}"
        if r.returncode != 0:
            return None, f"sudo_rc={r.returncode}"
        text = r.stdout

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


# ─────────────────────────────────────────────────────────────────────
# Translators — config → plain language
# ─────────────────────────────────────────────────────────────────────

def _channel_phrase(ch_id: str, ch_cfg: Any) -> str | None:
    """Render a channel config row as plain language, or None to skip."""
    if not isinstance(ch_cfg, dict) or not ch_cfg:
        return None
    enabled = ch_cfg.get("enabled", True)
    if not enabled:
        return None
    label = _CHANNEL_LABEL.get(ch_id, ch_id.title())
    # If the bot has a specific channel/room target, mention it (e.g.
    # "Send messages to Slack #ops"). Only surface obvious shapes; we
    # do not try to parse every channel's nested config.
    target = None
    if isinstance(ch_cfg.get("channel"), str):
        target = ch_cfg["channel"]
    elif isinstance(ch_cfg.get("defaultChannel"), str):
        target = ch_cfg["defaultChannel"]
    elif isinstance(ch_cfg.get("chatId"), (str, int)):
        target = str(ch_cfg["chatId"])
    if target:
        return f"Send messages on {label} ({target})"
    return f"Send messages on {label}"


def _plugin_phrase(plugin_id: str, plugin_cfg: Any) -> str | None:
    """Render a plugins.entries row as plain language, or None to skip.

    Only emits a phrase for plugins with a known capability description in
    _PLUGIN_CAPABILITY. Unknown plugins are silently skipped — we don't
    emit "Use the '<skill>' skill" because that wording belongs in
    Integrations, not Security, and the skill enumeration provides no
    security-relevant information to the operator.
    """
    if not isinstance(plugin_cfg, dict):
        return None
    if plugin_cfg.get("enabled") is False:
        return None
    key = plugin_id.lower()
    return _PLUGIN_CAPABILITY.get(key)  # None for unknown plugins → skipped


def _exec_approvals_phrase(ea: dict) -> str | None:
    """Render exec-approvals as 'Run N approved commands'."""
    agents = ea.get("agents") or {}
    total = 0
    if isinstance(agents, dict):
        for body in agents.values():
            approvals = (body or {}).get("approvals") if isinstance(body, dict) else None
            if isinstance(approvals, dict):
                total += len(approvals)
            elif isinstance(approvals, list):
                total += len(approvals)
    defaults = ea.get("defaults") or {}
    if isinstance(defaults, dict):
        total += len(defaults)
    elif isinstance(defaults, list):
        total += len(defaults)
    if total <= 0:
        return None
    if total == 1:
        return "Run 1 system command (only the one on its allow list)"
    return f"Run {total} pre-approved system commands"


def _file_access_phrases(oc: dict) -> tuple[list[str], list[str]]:
    """Return (can_do, cannot_do) phrases derived from openclaw.json fs config.

    Canonical default for ``tools.fs.workspaceOnly`` is **false**
    (unrestricted) — verified in three places:
      * OpenClaw schema docs/schemas/oc-config-schema.txt:18621:
        "Restrict filesystem tools … (default: false)."
      * packages/analyzer/permissions/posture.py:_axis_filesystem —
        treats anything other than ``is True`` as ``unrestricted``.
      * docs/spec-permission-posture-2026-05-10.md — only ``team_bot_c``
        has ``workspaceOnly: true`` in the pod; the other 6 bots
        have it absent/null.

    So: only ``workspace_only is True`` → workspace-restricted
    phrasing. Absent / null / explicit ``false`` → wider, "in its
    own user account" phrasing. The universal cannot-do
    "Read your other bots' folders or conversations" still
    carries the cross-bot boundary; we never claim a tighter
    bound than what the config actually enforces.
    """
    can: list[str] = []
    cannot: list[str] = []
    workspace_only = (
        ((oc.get("tools") or {}).get("fs") or {}).get("workspaceOnly")
    )
    if workspace_only is True:
        # Tighter posture: only ~/.openclaw/workspace is reachable.
        can.append("Read and write inside its own project folder")
        cannot.append("Read or change files outside its project folder")
    else:
        # Default + explicit-false: wider than workspace-only, but
        # still constrained by the macOS user boundary (one bot per
        # macOS user, no ACL across peers). Surface the wider
        # posture honestly rather than under-claiming the bound.
        can.append("Read and write files in its own user account")
    return can, cannot


def _web_phrases(oc: dict) -> list[str]:
    """Phrases for web.search + web.fetch capabilities."""
    out: list[str] = []
    web = (oc.get("tools") or {}).get("web") or {}
    if (web.get("search") or {}).get("enabled"):
        out.append("Search the web")
    if (web.get("fetch") or {}).get("enabled"):
        out.append("Open pages from the web")
    return out


# ─────────────────────────────────────────────────────────────────────
# Signal → plain-language event
# ─────────────────────────────────────────────────────────────────────

# Friendly headlines for known security_warden / sysadmin_watchdog
# signal types. Any type we don't recognise falls back to the
# signal's own title.
_FRIENDLY_HEADLINE: dict[str, str] = {
    # security_warden
    "credential_exposure":   "Sensitive credentials were nearly exposed — blocked",
    "tools_exec_security_warning":
                             "A risky command setting was flagged for review",
    "injection_pattern":     "Suspicious instructions were spotted in incoming content",
    "prompt_injection":      "Suspicious instructions were spotted in incoming content",
    "config_tampering":      "A config file changed in an unexpected way",
    "multi_user_no_primary_recorded":
                             "Heads up: no primary user is on file for this bot",
    "multi_user_no_pod_admins":
                             "Heads up: no pod admin is on file",
    "openclaw_version_below_floor":
                             "OpenClaw version is behind — upgrade recommended",
    # sysadmin_watchdog
    "gateway_down":          "The bot's gateway stopped responding briefly",
    "version_drift":         "OpenClaw drifted from the expected version",
    "config_validity":       "The bot's config has a validation problem",
    "plugin_missing":        "A required plugin is missing",
    "cron_failure":          "A scheduled job failed to run",
}


def _signal_to_item(sig: dict) -> ThisWeekItem:
    """Translate a Signal dict into a plain-language ThisWeekItem."""
    stype = (sig.get("type") or "").strip()
    headline = _FRIENDLY_HEADLINE.get(stype) or (sig.get("title") or stype or "Something noteworthy happened")
    # Trim to date-only so the UI shows "Mon May 12" instead of an
    # ISO timestamp.
    last = sig.get("last_observed_at") or sig.get("created_at") or ""
    when = last.split("T", 1)[0] if isinstance(last, str) else ""
    return ThisWeekItem(
        when=when,
        headline=headline,
        severity=str(sig.get("severity") or "info"),
        producer=str(sig.get("producer") or "monitor"),
    )


# ─────────────────────────────────────────────────────────────────────
# Public builder
# ─────────────────────────────────────────────────────────────────────

def build_summary(
    bot_id: str,
    *,
    bot_home: Path,
    signals: list[dict] | None = None,
    cost_cap_usd: float | None = None,
    bot_label: str | None = None,
) -> BotSafetySummary:
    """Build a plain-language safety summary for one bot.

    Pure function: takes a resolved bot_home path + an already-collected
    list of recent signals (filtered by bot + age + producer at the
    callsite). The Flask route wires this up with the live config files
    and signal store.

    Parameters
    ----------
    bot_id
        Logical bot name (key in network.json).
    bot_home
        Resolved macOS home for the bot's user — e.g. ``/Users/pod_admin_user``.
        We read ``<bot_home>/.openclaw/openclaw.json`` and
        ``<bot_home>/.openclaw/exec-approvals.json`` from here.
    signals
        Pre-filtered list of Signal dicts (already filtered to this bot
        + last N days + relevant producers). Passing an empty list
        produces the "All quiet" headline. ``None`` is treated as
        "couldn't read signals" and surfaces a read_error.
    cost_cap_usd
        Retained for call-site compatibility; no longer used. The
        cost-cap cannot_do claim was removed (2026-05-13) because
        budget_hawk's hard cap only vetoes new Arbiter proposals —
        it does not gate gateway billing. Bots CAN spend over cap.
    bot_label
        Display name; falls back to ``bot_id``.
    """
    s = BotSafetySummary(bot_id=bot_id, bot_label=bot_label or bot_id, as_of=_utc_now_iso())

    oc_path = bot_home / ".openclaw" / "openclaw.json"
    ea_path = bot_home / ".openclaw" / "exec-approvals.json"
    s.sources = {
        "openclaw_json": str(oc_path),
        "exec_approvals_json": str(ea_path),
    }

    # ── openclaw.json ────────────────────────────────────────────
    oc, oc_err = _read_json(oc_path)
    if oc is None:
        s.read_errors.append(f"Couldn't read this bot's main config ({oc_err}).")
        oc = {}

    # File access (per-bot file boundary)
    fa_can, fa_cannot = _file_access_phrases(oc)
    for p in fa_can:
        if p not in s.can_do:
            s.can_do.append(p)
    for p in fa_cannot:
        if p not in s.cannot_do:
            s.cannot_do.append(p)

    # Channels (messaging)
    channels = oc.get("channels") or {}
    if isinstance(channels, dict):
        for ch_id, ch_cfg in channels.items():
            phrase = _channel_phrase(ch_id, ch_cfg)
            if phrase and phrase not in s.can_do:
                s.can_do.append(phrase)

    # Plugins (skills)
    plugins_raw = oc.get("plugins") or {}
    entries = plugins_raw.get("entries") if isinstance(plugins_raw, dict) else None
    if isinstance(entries, dict):
        for plugin_id, plugin_cfg in entries.items():
            # The "evolve" plugin is the admin transport; not a
            # user-visible capability and would be misleading to list.
            if plugin_id.lower() in {"evolve", "oc_plugin"}:
                continue
            phrase = _plugin_phrase(plugin_id, plugin_cfg)
            if phrase and phrase not in s.can_do:
                s.can_do.append(phrase)

    # Web access
    for p in _web_phrases(oc):
        if p not in s.can_do:
            s.can_do.append(p)

    # ── exec-approvals.json ──────────────────────────────────────
    ea, ea_err = _read_json(ea_path)
    if ea is None:
        if ea_err != "not_found":  # absent is fine — many bots have none
            s.read_errors.append(f"Couldn't read the command allow list ({ea_err}).")
    else:
        ea_phrase = _exec_approvals_phrase(ea)
        if ea_phrase:
            s.can_do.append(ea_phrase)

    # ── V1.5-3: upstream OC version + exec-policy compliance ─────
    # Pulled from the same openclaw.json + exec-approvals.json reads
    # above (no extra I/O). Surfaced on the card as small chips.
    try:
        from . import upstream_version as _uv
        from .config import load_network as _load_network
        meta = (oc.get("meta") or {}) if isinstance(oc, dict) else {}
        raw_v = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
        if isinstance(raw_v, str) and raw_v.strip():
            s.upstream_version = _uv.canonical_version(raw_v.strip())

        # Resolve role + has-apps for the new state machine. Both are
        # best-effort: failure to read network.json or the manifests dir
        # degrades the chip to "unknown" / falls back to default greens
        # rather than crashing.
        bot_role = ""
        _primary_id: str | None = None
        has_apps: bool | None = None
        try:
            net = _load_network()
            bot_role = (
                (net.get("bots") or {}).get(bot_id, {}).get("role") or ""
            )
            # Resolve the primary id so exec-policy compliance classifies the
            # primary correctly even on a legacy pod that never set role= — and
            # without the old hardcoded "evolve" assumption (an "evo"-primary
            # pod would otherwise be misclassified as a member bot).
            from primary_bot import primary_bot_id as _pbid  # type: ignore
            _primary_id = _pbid(net)
        except Exception:
            pass
        manifests_dir = bot_home / ".openclaw" / "workspace" / "manifests"
        try:
            if manifests_dir.exists():
                has_apps = any(
                    f.suffix == ".json"
                    and not f.name.startswith(("_", "."))
                    for f in manifests_dir.iterdir()
                )
        except OSError:
            pass

        compliance = _uv.evaluate_exec_policy_compliance(
            bot_id,
            openclaw_config=oc if oc else None,
            exec_approvals=ea,
            role=bot_role,
            has_installed_apps=has_apps,
            primary_bot_id=_primary_id,
        )
        s.exec_policy_compliant = compliance.compliant
        s.exec_policy_state = compliance.state
        s.exec_policy_reason = compliance.reason
    except Exception as _upstream_exc:
        # Best-effort surfacing — never block the card render.
        # Log so a regression (bad import, parse error) surfaces in the
        # admin server's stderr rather than disappearing silently.
        print(
            f"[safety_summary] WARN: upstream-version chip rendering failed "
            f"for bot {bot_id!r}: {type(_upstream_exc).__name__}: {_upstream_exc}",
            file=sys.stderr,
        )

    # ── Universal cannot-do (architectural guarantees) ───────────
    for p in _UNIVERSAL_CANNOT_DO:
        if p not in s.cannot_do:
            s.cannot_do.append(p)

    # ── This week (signals) ──────────────────────────────────────
    if signals is None:
        s.read_errors.append("Couldn't read this week's monitor reports.")
        s.this_week_headline = "Couldn't check — see Advanced view"
    elif not signals:
        s.this_week_headline = "All quiet"
    else:
        items = [_signal_to_item(sig) for sig in signals]
        # Newest first (signals already sorted by store, but be safe).
        items.sort(key=lambda x: x.when, reverse=True)
        s.this_week_items = items[:5]
        # Pick the strongest severity for the headline.
        sevs = {it.severity for it in items}
        if "alert" in sevs:
            s.this_week_headline = "Notable: one or more alerts this week"
        elif "warn" in sevs:
            s.this_week_headline = "Notable: a few warnings this week"
        else:
            s.this_week_headline = "Minor notes this week"

    return s


def signals_for_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    days: int = 7,
    producers: tuple[str, ...] = ("security_warden", "sysadmin_watchdog"),
) -> list[dict] | None:
    """Collect recent Signals for a bot from the unified signal store.

    Returns a list of plain dicts. Returns ``None`` only if the signals
    directory doesn't exist (signal store not yet populated) — callers
    treat ``None`` as "couldn't check" and ``[]`` as "all quiet".

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): firing + snoozed + archived Signals come from
    ``signals.store.iter_signals``. A raw read of the firing/snoozed/
    archived subdirs is the fallback only when the analyzer signals
    package isn't importable.

    Filters: bot_id match (or scope=='pod' alerts that mention this bot
    in details.bot_id), producer in ``producers``, last_observed_at
    within ``days``.
    """
    root = Path(shared_dir) / "signals"
    if not root.exists():
        return None
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

    def _iter_signal_dicts():
        """Yield Signal dicts from firing+snoozed+archived (store-first)."""
        try:
            from signals import store as _signals_store  # type: ignore[import-not-found]
        except Exception:
            _signals_store = None  # type: ignore[assignment]
        if _signals_store is not None:
            # Materialize via the store API first so a mid-iteration error
            # doesn't half-yield before the raw-read fallback runs.
            try:
                via_store = [
                    sig.to_dict()
                    for sig in _signals_store.iter_signals(
                        shared_dir, subdirs=("firing", "snoozed", "archived")
                    )
                ]
            except Exception:
                via_store = None
            if via_store is not None:
                yield from via_store
                return
        # Fallback: store unavailable / unreadable — read subdirs directly.
        for subdir in ("firing", "snoozed", "archived"):
            d = root / subdir  # store-access-lint: analyzer-unavailable fallback
            if not d.exists():
                continue
            try:
                paths = sorted(d.glob("*.json"))
            except OSError:
                continue
            for p in paths:
                try:
                    raw = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(raw, dict):
                    yield raw

    out: list[dict] = []
    for sig in _iter_signal_dicts():
        if sig.get("producer") not in producers:
            continue
        # bot match: direct bot_id or details.bot_id
        sig_bot = sig.get("bot_id")
        if not sig_bot:
            details = sig.get("details") or {}
            sig_bot = details.get("bot_id") if isinstance(details, dict) else None
        if sig_bot != bot_id:
            continue
        last = sig.get("last_observed_at") or sig.get("created_at")
        if not last:
            continue
        try:
            last_dt = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if last_dt < cutoff:
            continue
        out.append(sig)
    # Newest first.
    out.sort(key=lambda s: s.get("last_observed_at") or s.get("created_at") or "", reverse=True)
    return out
