#!/usr/bin/env python3
"""
substrate_audit_state.py — Local-state assembly for skill + provider audits.

Workstream B-skills (spec-audit-extensions §4.1 / §4.2). The runner runs
as the bot user with direct read access to:
  - ``~/.openclaw/openclaw.json`` (plugin entries, channel configs)
  - ``~/.openclaw/auth-profiles.json`` (OAuth tokens, scopes)
  - ``{workspace}/manifests/*.json`` (which apps depend on which skills)
  - ``{workspace}/evolve/skill_audits/<skill>/trail.jsonl``
  - ``{workspace}/evolve/provider_audits/<provider>/trail.jsonl``
  - ``{shared_dir}/signals/firing/*.json`` (recent failure context)

This module is the thin layer that reads those files and produces the
:class:`skill_audit.BotSkillState` / :class:`provider_audit.BotProviderState`
structs the audit modules consume. Kept independent so tests can exercise
state assembly without going through filesystem.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from skill_audit import BotSkillState  # noqa: E402
from provider_audit import BotProviderState  # noqa: E402


logger = logging.getLogger(__name__)


# ── Inventory ───────────────────────────────────────────────────────────────
#
# Canonical lists of shipping skills + providers, kept in sync with
# ``packages/admin/evolve_admin/skills/*_install.py`` and
# ``packages/admin/evolve_admin/oauth/providers/*_provider.py``. Used by
# the runner's ``--kind skill all`` / ``--kind provider all`` paths to
# know what to enumerate. Per-bot subset is computed by the runner from
# install-module presence on disk.

KNOWN_SKILLS = (
    "gmail", "calendar", "slack", "discord", "telegram", "imessage",
    "obsidian", "notion", "linear", "autocad", "home_assistant",
    "runway", "apple_local", "upstream_plugin_skills",
)

KNOWN_PROVIDERS = (
    "google_workspace", "gmail", "calendar", "slack", "discord",
    "telegram", "obsidian", "imessage",
)

# Expected scopes per skill (rough; the auditor reads install-module
# constants when present for the authoritative list). Empty list means
# the skill has no OAuth surface to scope-check.
_EXPECTED_SCOPES: dict[str, list[str]] = {
    "gmail":    ["gmail.readonly"],
    "calendar": ["calendar.readonly", "calendar.events.readonly"],
    "slack":    ["channels:read", "chat:write"],
    "notion":   [],
    "linear":   [],
}

# Skills that don't carry an OAuth profile — credential check is a no-op
# for these. We still audit config + code-vs-docstring drift.
_CREDENTIALLESS_SKILLS = frozenset({
    "imessage", "obsidian", "apple_local", "home_assistant",
    "autocad", "runway", "linear", "notion",
})

# Map skill_id → provider key in auth-profiles.json. Gmail + Calendar share
# google_workspace because Google issues a single token per app reg.
_SKILL_TO_PROVIDER: dict[str, str] = {
    "gmail":    "google_workspace",
    "calendar": "google_workspace",
    "slack":    "slack",
    "discord":  "discord",
    "telegram": "telegram",
}


# ── Bot file readers ────────────────────────────────────────────────────────


def _own_home() -> Path:
    """The RUNNING user's home — this module executes as the bot itself,
    so no bot_id→account resolution applies (don't confuse with the
    canonical evolve_config.bot_home, which maps another bot's id)."""
    return Path.home()


def read_openclaw_json(home: Path | None = None) -> dict:
    """Read the bot's openclaw.json. Returns {} on absence / parse failure."""
    home = home or _own_home()
    p = home / ".openclaw" / "openclaw.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def read_auth_profiles(home: Path | None = None) -> dict:
    """Read auth-profiles.json. Returns {} on absence / parse failure."""
    home = home or _own_home()
    p = home / ".openclaw" / "auth-profiles.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def read_skill_config(skill_id: str, home: Path | None = None) -> dict:
    """Read filesystem-skill config (notion, linear, runway, home_assistant).

    Returns {} when the file doesn't exist — the bot hasn't installed
    this skill via the filesystem path.
    """
    home = home or _own_home()
    p = home / ".openclaw" / "skills" / f"{skill_id}.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ── State assembly: skill ───────────────────────────────────────────────────


def assemble_skill_state(
    *,
    skill_id: str,
    bot_id: str,
    home: Path | None = None,
    manifests_dir: Path | None = None,
    recent_failures: list[dict[str, Any]] | None = None,
) -> BotSkillState:
    """Read local state for one skill on this bot.

    The runner calls this before dispatching the audit. Tests can construct
    :class:`BotSkillState` directly to bypass file I/O.
    """
    home = home or _own_home()
    oc = read_openclaw_json(home)
    auth = read_auth_profiles(home)

    plugin_entries = ((oc.get("plugins") or {}).get("entries") or {})
    plugin_entry: dict | None = None
    plugin_enabled: bool | None = None
    # Gmail + Calendar share the "google" plugin name. Everything else is
    # named after the skill.
    plugin_candidates = {
        "gmail":    ["google"],
        "calendar": ["google"],
    }.get(skill_id, [skill_id])
    for cand in plugin_candidates:
        if cand in plugin_entries:
            raw = plugin_entries[cand]
            if isinstance(raw, dict):
                plugin_entry = raw
                plugin_enabled = bool(raw.get("enabled", True))
                break
    if plugin_entry is None and skill_id in _CREDENTIALLESS_SKILLS:
        # Filesystem-shaped skills don't live in plugins.entries; they have
        # their own config file under ~/.openclaw/skills/<id>.json.
        fs_cfg = read_skill_config(skill_id, home)
        plugin_enabled = bool(fs_cfg)
        plugin_entry = fs_cfg or None

    # OAuth profile, only for skills with credential surface.
    has_oauth = skill_id not in _CREDENTIALLESS_SKILLS
    oauth_profile: dict | None = None
    oauth_profile_status: str | None = None
    scopes_present: list[str] = []
    credentials_age_days: int | None = None
    credentials_expire_days: int | None = None
    if has_oauth:
        provider_key = _SKILL_TO_PROVIDER.get(skill_id, skill_id)
        profiles = auth.get("profiles") or {}
        per_bot_key = f"{provider_key}:{bot_id}"
        oauth_profile = profiles.get(per_bot_key) or profiles.get(provider_key)
        if oauth_profile and isinstance(oauth_profile, dict):
            oauth_profile_status = str(oauth_profile.get("status", "")) or "active"
            scopes_present = list(oauth_profile.get("scopes") or [])
            credentials_age_days = _days_since(oauth_profile.get("issued_at"))
            credentials_expire_days = _days_until(oauth_profile.get("expires_at"))
        elif oauth_profile is None:
            oauth_profile_status = "missing"

    # Apps depending on this skill — search manifests for matching
    # requirements.integrations[].id. Useful blast-radius context for the
    # LLM's Stage 3a observations.
    apps_using: list[str] = []
    manifests_dir = manifests_dir or (home / ".openclaw" / "workspace" / "manifests")
    if manifests_dir.exists():
        try:
            for f in manifests_dir.iterdir():
                if f.suffix != ".json" or f.name.startswith("_"):
                    continue
                try:
                    m = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                reqs = (m.get("requirements") or {}).get("integrations") or []
                for r in reqs:
                    if isinstance(r, dict) and r.get("id") == skill_id:
                        apps_using.append(m.get("id") or f.stem)
                        break
        except OSError:
            pass

    return BotSkillState(
        bot_id=bot_id,
        skill_id=skill_id,
        plugin_enabled=plugin_enabled,
        plugin_entry=plugin_entry,
        oauth_profile=oauth_profile,
        oauth_profile_status=oauth_profile_status,
        credentials_age_days=credentials_age_days,
        credentials_expire_days=credentials_expire_days,
        scopes_present=scopes_present,
        scopes_expected=list(_EXPECTED_SCOPES.get(skill_id, [])),
        recent_failures=recent_failures or [],
        apps_using_skill=apps_using,
        has_oauth=has_oauth,
    )


# ── State assembly: provider ────────────────────────────────────────────────


def assemble_provider_state(
    *,
    provider_id: str,
    bot_id: str,
    home: Path | None = None,
    recent_failures: list[dict[str, Any]] | None = None,
) -> BotProviderState:
    """Read local state for one OAuth provider on this bot."""
    home = home or _own_home()
    auth = read_auth_profiles(home)
    profiles = auth.get("profiles") or {}

    per_bot_key = f"{provider_id}:{bot_id}"
    profile = profiles.get(per_bot_key) or profiles.get(provider_id) or {}
    profile_present = bool(profile)
    profile_status = (
        str(profile.get("status", "")) if profile_present else "missing"
    )
    scopes_present = list(profile.get("scopes") or [])
    token_age_days = _days_since(profile.get("issued_at")) if profile_present else None
    token_expire_days = (
        _days_until(profile.get("expires_at")) if profile_present else None
    )
    refresh_token_present = bool(profile.get("refresh_token"))
    last_refresh_age_days = _days_since(profile.get("last_refreshed_at"))

    # Build the scopes-needed-by-dependent-skills view. Each skill that
    # maps to this provider contributes its expected-scope list. This is
    # what lets the LLM call out scope drift like "skill calendar needs
    # calendar.events but the profile only has calendar.readonly".
    scopes_needed: dict[str, list[str]] = {}
    dependent_skills: list[str] = []
    for skill_id, p_id in _SKILL_TO_PROVIDER.items():
        if p_id == provider_id:
            dependent_skills.append(skill_id)
            expected = _EXPECTED_SCOPES.get(skill_id) or []
            if expected:
                scopes_needed[skill_id] = expected
    # If provider_id is itself a skill (e.g. "gmail"), surface that single
    # skill's expectations.
    if not dependent_skills and provider_id in _SKILL_TO_PROVIDER:
        dependent_skills = [provider_id]
        expected = _EXPECTED_SCOPES.get(provider_id) or []
        if expected:
            scopes_needed[provider_id] = expected

    return BotProviderState(
        bot_id=bot_id,
        provider_id=provider_id,
        profile_present=profile_present,
        profile_status=profile_status,
        token_age_days=token_age_days,
        token_expire_days=token_expire_days,
        scopes_present=scopes_present,
        scopes_needed_by_skills=scopes_needed,
        refresh_token_present=refresh_token_present,
        last_refresh_age_days=last_refresh_age_days,
        oauth_client_configured=True,  # Best-effort; runner can override.
        dependent_skills=dependent_skills,
        recent_failures=recent_failures or [],
    )


# ── Time helpers ────────────────────────────────────────────────────────────


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _days_since(timestamp: Any, *, now: datetime | None = None) -> int | None:
    """Days since the given ISO timestamp (None if unparseable)."""
    dt = _parse_iso(timestamp)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - dt).days)


def _days_until(timestamp: Any, *, now: datetime | None = None) -> int | None:
    """Days remaining until the given ISO timestamp (negative if past)."""
    dt = _parse_iso(timestamp)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (dt - now).days


# ── Recent-failure aggregation ──────────────────────────────────────────────


def gather_recent_skill_failures(
    *, skill_id: str, bot_id: str, shared_dir: Path,
    window_days: int = 7,
) -> list[dict[str, Any]]:
    """Read recent Signal-store entries that name this skill / provider.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): iterates firing Signals via ``signals.store.iter_active``
    (bot_id filtered server-side) rather than globbing
    ``{shared_dir}/signals/firing/`` directly. The original recency
    pre-filter keyed on each file's mtime; the store equivalent is the
    Signal's own ``last_observed_at`` / ``created_at`` timestamp (the
    canonical Signal-schema fields — the pre-Phase-B raw reader looked
    for ``first_seen`` / ``last_seen``, which the Signal model does not
    emit, so the original recency filter was effectively a no-op on
    store-written records). Now the window is enforced honestly over the
    last ``window_days`` days.

    Best-effort. Returns [] when the Signal store is unreadable or empty
    (typical early-in-pod-life condition). The audit modules treat the
    result as informational context — empty is fine.
    """
    out: list[dict[str, Any]] = []
    try:
        from signals import store as signals_store  # type: ignore[import-not-found]
    except Exception:
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    try:
        candidates = list(
            signals_store.iter_active(shared_dir, bot_id=bot_id, state="firing")
        )
    except Exception:
        return out
    for sig in candidates:
        ts = getattr(sig, "last_observed_at", None) or getattr(sig, "created_at", None)
        recent = True
        if isinstance(ts, str) and ts:
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                recent = parsed >= cutoff
            except ValueError:
                recent = True
        if not recent:
            continue
        details = dict(getattr(sig, "details", None) or {})
        # We accept both shapes: details.skill_id (skill audit) and
        # details.provider_id (provider audit) — gather_recent_skill_failures
        # is shared by both call sites for simplicity.
        if details.get("skill_id") == skill_id or details.get("provider_id") == skill_id:
            out.append({
                "signal_id": getattr(sig, "id", None),
                "type": getattr(sig, "type", None),
                "severity": getattr(sig, "severity", None),
                "title": getattr(sig, "title", None),
                "ts": getattr(sig, "created_at", None)
                or getattr(sig, "last_observed_at", None),
            })
    return out[:20]
