#!/usr/bin/env python3
"""
OpenClaw Admin Tool
Run as: sudo python3 /Users/Shared/openclaw-admin.py

Manage models, API keys, and gateways for all bots.
Changes save automatically after each action.
Enter a bot name or 'all' to manage all bots at once.
"""

import csv
import json
import os
import re
import stat
import sys
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOTS = {
    "admin_bot":  {"user": "admin_bot",   "config": "/Users/admin_bot/.openclaw/openclaw.json",   "auth": "/Users/admin_bot/.openclaw/agents/main/agent/auth-profiles.json",   "exec_approvals": "/Users/admin_bot/.openclaw/exec-approvals.json",   "service": "ai.openclaw.admin_bot-gateway"},
    "team_bot_a":    {"user": "team_bot_a",     "config": "/Users/team_bot_a/.openclaw/openclaw.json",     "auth": "/Users/team_bot_a/.openclaw/agents/main/agent/auth-profiles.json",     "exec_approvals": "/Users/team_bot_a/.openclaw/exec-approvals.json",     "service": "ai.openclaw.team_bot_a-gateway"},
    "security_bot": {"user": "security_bot",  "config": "/Users/security_bot/.openclaw/openclaw.json",  "auth": "/Users/security_bot/.openclaw/agents/main/agent/auth-profiles.json",  "exec_approvals": "/Users/security_bot/.openclaw/exec-approvals.json",  "service": "ai.openclaw.security_bot-gateway"},
    "team_bot_b":   {"user": "personal_bot_user", "config": "/Users/personal_bot_user/.openclaw/openclaw.json", "auth": "/Users/personal_bot_user/.openclaw/agents/main/agent/auth-profiles.json", "exec_approvals": "/Users/personal_bot_user/.openclaw/exec-approvals.json", "service": "ai.openclaw.team_bot_b-gateway"},
    "team_bot_c":  {"user": "team_bot_c",   "config": "/Users/team_bot_c/.openclaw/openclaw.json",   "auth": "/Users/team_bot_c/.openclaw/agents/main/agent/auth-profiles.json",   "exec_approvals": "/Users/team_bot_c/.openclaw/exec-approvals.json",   "service": "ai.openclaw.team_bot_c-gateway"},
}

# Provider metadata: key hints and whether they support a MAX/token auth mode
PROVIDER_META = {
    "anthropic": {"hint": "sk-ant-api03-...", "token_hint": "sk-ant-oat01-...", "has_token": True},
    "openai":    {"hint": "sk-...",           "has_token": False},
    "google":    {"hint": "AIza...",          "has_token": False},
    "xai":       {"hint": "xai-...",          "has_token": False},
    "mistral":   {"hint": "mistral-...",      "has_token": False},
    "cohere":    {"hint": "...",              "has_token": False},
    "groq":      {"hint": "gsk_...",          "has_token": False},
    "perplexity":{"hint": "pplx-...",         "has_token": False},
    "together":  {"hint": "...",              "has_token": False},
    "deepseek":  {"hint": "sk-...",           "has_token": False},
    "runway":    {"hint": "key_...",          "has_token": False},
    "suno":      {"hint": "...",              "has_token": False},
}

def providers_from_catalog(catalog):
    """Derive the set of providers from model names in the catalog + any in auth profiles."""
    providers = set()
    for model in catalog:
        if "/" in model:
            providers.add(model.split("/")[0])
    return providers

def build_provider_list(catalog, profiles):
    """
    Build the list of (provider, mode, description) to prompt for during key rotation.
    - Derives providers from catalog models
    - Adds any providers already in auth profiles (even if no model in catalog)
    - Anthropic gets two entries: api_key + token
    - Unknown providers get a generic entry
    """
    providers = providers_from_catalog(catalog)
    # Also include providers already configured in auth
    for p in profiles.values():
        prov = p.get("provider", "")
        if prov:
            providers.add(prov)

    result = []
    seen = set()
    for provider in sorted(providers):
        meta = PROVIDER_META.get(provider, {"hint": "...", "has_token": False})
        # api_key entry
        key = (provider, "api_key")
        if key not in seen:
            result.append((provider, "api_key", f"{provider.capitalize()} API Key ({meta['hint']})"))
            seen.add(key)
        # token entry for providers that support it (e.g. Anthropic MAX)
        if meta.get("has_token"):
            key2 = (provider, "token")
            if key2 not in seen:
                result.append((provider, "token", f"{provider.capitalize()} MAX Token ({meta.get('token_hint', '...')})"))
                seen.add(key2)
    return result


# ── Version & upgrade ─────────────────────────────────────────────────────────

OPENCLAW_PACKAGE_JSON = Path("/opt/homebrew/lib/node_modules/openclaw/package.json")
OPENCLAW_NPM_REGISTRY = "https://registry.npmjs.org/openclaw/latest"


def _installed_version() -> str:
    try:
        return json.loads(OPENCLAW_PACKAGE_JSON.read_text()).get("version", "unknown")
    except Exception as e:
        return f"(error: {e})"


def _latest_version() -> str:
    try:
        import urllib.request
        req = urllib.request.Request(
            OPENCLAW_NPM_REGISTRY,
            headers={"Accept": "application/json", "User-Agent": "openclaw-admin/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("version", "unknown")
    except Exception as e:
        return f"(error: {e})"


def show_version():
    installed = _installed_version()
    print(f"\n  ── OpenClaw Version ──────────────────────────────────")
    print(f"  Installed:  {installed}")
    print(f"  Checking npm registry...", end="", flush=True)
    latest = _latest_version()
    print(f"\r  Latest:     {latest}      ")

    if latest.startswith("(error"):
        print(f"  ⚠️  Could not reach npm registry.")
        return installed, latest, False

    up_to_date = installed == latest
    if up_to_date:
        print(f"  ✅ Up to date.")
    else:
        print(f"  🔼 Update available: {installed} → {latest}")

    # Show per-bot gateway versions from running processes
    print(f"\n  ── Gateway processes ──────────────────────────────────")
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    found = False
    for bot, info in BOTS.items():
        user = info["user"]
        procs = [l for l in result.stdout.splitlines()
                 if "openclaw-gateway" in l and l.split()[0] == user]
        if procs:
            pid = procs[0].split()[1]
            status = "✅" if len(procs) == 1 else f"⚠️  {len(procs)} procs"
            print(f"  {bot:8s}  PID {pid}  {status}")
            found = True
        else:
            print(f"  {bot:8s}  ❌ not running")
    if not found:
        print("  (no gateway processes found)")

    # Updater state
    state_path = Path("/Users/Shared/openclaw-updater-state.json")
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            last_check = state.get("last_check", "never")
            last_ver   = state.get("last_applied_version", "unknown")
            print(f"\n  Auto-updater last check:   {last_check[:19] if last_check else 'never'}")
            print(f"  Auto-updater last applied: {last_ver}")
        except Exception:
            pass

    return installed, latest, not up_to_date


def do_upgrade():
    print("\n  ── Upgrade OpenClaw ───────────────────────────────────")
    installed = _installed_version()
    latest    = _latest_version()

    if latest.startswith("(error"):
        print(f"  ❌ Cannot reach npm registry: {latest}")
        return

    if installed == latest:
        print(f"  ✅ Already on latest ({installed}). Nothing to do.")
        force = input("  Force reinstall anyway? (y/N): ").strip().lower()
        if force != "y":
            return

    print(f"  Upgrading {installed} → {latest}")
    confirm = input("  Proceed? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    print("  Running: npm install -g openclaw@latest")
    r = subprocess.run(["npm", "install", "-g", "openclaw@latest"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ❌ npm install failed:\n{r.stderr[:500]}")
        return

    new_ver = _installed_version()
    print(f"  ✅ Installed: {new_ver}")

    # Restart all running gateways
    print("\n  Restarting all gateways...")
    for bot in BOTS:
        restart_gateway(bot)

    print("\n  ✅ Upgrade complete.")


def version_menu():
    while True:
        installed, latest, update_available = show_version()
        print()
        if update_available:
            print("  [u] Upgrade to latest   [r] Restart all gateways   [Enter] Back")
        else:
            print("  [r] Restart all gateways   [Enter] Back")
        print()
        sub = input("  Choice: ").strip().lower()

        if sub == "u":
            do_upgrade()
        elif sub == "r":
            confirm = input("  Restart ALL gateways? (y/N): ").strip().lower()
            if confirm == "y":
                for bot in BOTS:
                    restart_gateway(bot)
        else:
            break


# ── User ID resolution ────────────────────────────────────────────────────────

# Known Slack user IDs → display names
SLACK_USER_MAP = {
    "U0PLKKXV0":  "Pod_admin (pod_admin)",
    "U9ZL3JYR3":  "Elizabeth",
    "U4T907NV6":  "Dan",
    "U0518A544N5": "Peter",
    "U4VBB85PY":  "Brent",
    "UCX7M5PV3":  "Justin",
    "U5ETJ3HFC":  "Q",
    "U055QV2URHP": "Robert",
    "U087LN8U4J0": "Ping",
    "U56HP2BJN":  "Terran",
    "U02BKCTMB5G": "Team_bot_a (bot)",
    "U099KA9MCFR": "Pod_admin (pod_admin_user)",
}

# Known Telegram chat IDs → display names
TELEGRAM_USER_MAP = {
    "123456789": "Pod_admin",
}

def _resolve_user(channel: str, user_id: str) -> str:
    """Resolve a user_id to a display name if known."""
    if channel == "slack":
        return SLACK_USER_MAP.get(user_id, user_id)
    if channel == "telegram":
        return TELEGRAM_USER_MAP.get(user_id, user_id)
    return user_id


# ── Usage reporting ───────────────────────────────────────────────────────────

TURN_DIRS = {
    "team_bot_a":    "/Users/team_bot_a/.openclaw/workspace/memory",
    "team_bot_b":   "/Users/personal_bot_user/.openclaw/workspace/memory",
    "admin_bot":  "/Users/admin_bot/.openclaw/workspace/memory",
    "team_bot_c":  "/Users/team_bot_c/.openclaw/workspace/memory",
    "security_bot": "/Users/security_bot/.openclaw/workspace/memory",
}
BOT_LOGS = {
    "team_bot_a":    "/Users/team_bot_a/.openclaw/logs/gateway.log",
    "team_bot_b":   "/Users/personal_bot_user/.openclaw/logs/gateway.log",
    "admin_bot":  "/Users/admin_bot/.openclaw/logs/gateway.log",
    "team_bot_c":  "/Users/team_bot_c/.openclaw/logs/gateway.log",
    "security_bot": "/Users/security_bot/.openclaw/logs/gateway.log",
}
COLLECTOR = "/Users/Shared/openclaw-usage/turn-collector.py"


def _load_turns(days=1, end_date=None, instance_filter=None):
    if end_date is None:
        end_date = datetime.now()
    dates = [(end_date - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(days - 1, -1, -1)]
    instances = TURN_DIRS if not instance_filter else {instance_filter: TURN_DIRS[instance_filter]}
    turns = []
    for date_str in dates:
        for inst, mem_dir in instances.items():
            path = Path(mem_dir) / f"turns-{date_str}.jsonl"
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                rec["instance"] = inst
                                turns.append(rec)
                            except Exception:
                                pass
            except Exception:
                pass
    return turns


# ANSI color codes — per specific model, with provider fallbacks
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

# Per-model colors (full model name keys)
# Anthropic family: cyan/teal tones — Haiku=dim, Sonnet=regular, Opus=bold
# OpenAI family:    green tones
# Google family:    yellow tones
# xAI family:       magenta tones
# Mistral:          blue
_MODEL_COLORS_SPECIFIC = {
    # ── Anthropic ──
    # Haiku = green (small/fast/cheap)
    "anthropic/claude-haiku-4-6":        "\033[32m",    # green
    "anthropic/claude-haiku-4-5":        "\033[32m",    # green
    "anthropic/claude-haiku-3-5":        "\033[32m",    # green
    "anthropic/claude-3-haiku":          "\033[32m",    # green
    # Sonnet = cyan (mid-tier)
    "anthropic/claude-sonnet-4-6":       "\033[36m",    # cyan
    "anthropic/claude-sonnet-4-5":       "\033[36m",    # cyan
    "anthropic/claude-sonnet-4-20250514":"\033[36m",    # cyan
    "anthropic/claude-sonnet-4-20250219":"\033[36m",    # cyan
    "anthropic/claude-3-5-sonnet":       "\033[36m",    # cyan
    "anthropic/claude-3-sonnet":         "\033[36m",    # cyan
    # Opus = blue (flagship)
    "anthropic/claude-opus-4-6":         "\033[34m",    # blue
    "anthropic/claude-opus-4-5":         "\033[34m",    # blue
    "anthropic/claude-3-opus":           "\033[34m",    # blue
    # Anthropic API key fallback (metered — show differently)
    "anthropic:api_key":                 "\033[91m",    # bright red  (⚠ metered!)
    # ── OpenAI (white/bright family) ──
    "openai/gpt-4o":                     "\033[97m",    # bright white
    "openai/gpt-4o-mini":                "\033[37m",    # white/light grey
    "openai/gpt-4.1":                    "\033[97m",    # bright white
    "openai/gpt-4.1-mini":               "\033[37m",    # light grey
    "openai/gpt-5.1-codex":              "\033[1;97m",  # bold white
    "openai/o3":                         "\033[1;97m",  # bold white
    "openai/o4-mini":                    "\033[37m",    # light grey
    # ── Google (yellow family) ──
    "google/gemini-3.1-pro-preview":     "\033[33m",    # yellow
    "google/gemini-2.5-pro-preview":     "\033[33m",    # yellow
    "google/gemini-1.5-pro":             "\033[93m",    # bright yellow
    "google/gemini-2.0-flash":           "\033[2;33m",  # dim yellow
    "google/gemini-2.0-flash-lite":      "\033[2;33m",  # dim yellow
    # ── xAI (magenta family) ──
    "xai/grok-4-1-fast":                 "\033[35m",    # magenta
    "xai/grok-3":                        "\033[95m",    # bright magenta
    "xai/grok-3-mini":                   "\033[2;35m",  # dim magenta
    # ── Provider fallbacks (when no exact model match) ──
    "anthropic":                         "\033[36m",    # cyan (Sonnet is most common)
    "openai":                            "\033[97m",    # bright white
    "google":                            "\033[33m",    # yellow
    "xai":                               "\033[35m",    # magenta
    "mistral":                           "\033[34m",    # blue
    "runway":                            "\033[95m",    # bright magenta
    "unknown":                           "\033[37m",    # grey
}

def _turn_key(turn: dict) -> str:
    """Return a display key for a turn — full model name, with Anthropic auth split."""
    model = turn.get("model", "unknown")
    provider = model.split("/")[0] if "/" in model else "unknown"
    if provider == "anthropic":
        auth_mode = turn.get("auth_mode", "token")
        if auth_mode == "api_key":
            return f"{model}:api_key"
    return model

def _model_color(key: str) -> str:
    # Try exact match first, then strip :api_key suffix for provider lookup,
    # then try provider prefix, then unknown
    if key in _MODEL_COLORS_SPECIFIC:
        return _MODEL_COLORS_SPECIFIC[key]
    # e.g. "anthropic/claude-sonnet-4-6:api_key" → try base model
    base = key.replace(":api_key", "")
    if base in _MODEL_COLORS_SPECIFIC:
        return _MODEL_COLORS_SPECIFIC.get("anthropic:api_key", "\033[96m")
    # Try provider prefix
    provider = key.split("/")[0] if "/" in key else key.split(":")[0]
    return _MODEL_COLORS_SPECIFIC.get(provider, _MODEL_COLORS_SPECIFIC["unknown"])

def _model_family(model: str) -> str:
    return model.split("/")[0] if "/" in model else "unknown"

def _model_label(key: str) -> str:
    """Human label for legend."""
    if key.endswith(":api_key"):
        base = key.replace(":api_key", "")
        return f"{base}  [API key/metered]"
    return key


def _print_usage_summary(turns, days, instance_filter=None):
    if not turns:
        print("  No turn data found. Run [c] to collect first.")
        return

    by_date          = defaultdict(int)
    by_date_key      = defaultdict(lambda: defaultdict(int))  # date -> display_key -> count
    by_instance      = defaultdict(int)
    by_channel       = defaultdict(int)
    by_model         = defaultdict(int)
    by_user          = defaultdict(int)

    for t in turns:
        date    = t.get("ts", "")[:10]
        model   = t.get("model", "?")
        channel = t.get("channel", "?")
        user_id = t.get("user_id", "?")
        key     = _turn_key(t)
        by_date[date] += 1
        by_date_key[date][key] += 1
        by_instance[t.get("instance", "?")] += 1
        by_channel[channel] += 1
        by_model[model] += 1
        resolved = _resolve_user(channel, user_id)
        by_user[f"{channel}:{resolved}"] += 1

    scope = instance_filter or "all bots"
    print()
    print(f"  {'─'*56}")
    print(f"  Usage — {scope} — last {days} day{'s' if days > 1 else ''} — {len(turns)} turns total")
    print(f"  {'─'*56}")

    # Build color legend from models seen
    seen_families = {_model_family(m) for m in by_model}
    BAR_WIDTH = 35
    # Collect all keys seen across all days for legend
    seen_keys = set()
    for day_keys in by_date_key.values():
        seen_keys.update(day_keys.keys())

    print("\n  By date (stacked by model + auth):")
    for d in sorted(by_date):
        count = by_date[d]
        day_keys = by_date_key[d]
        total = sum(day_keys.values())
        bar = ""
        allocated = 0
        keys_sorted = sorted(day_keys.items(), key=lambda x: -x[1])
        for i, (key, cnt) in enumerate(keys_sorted):
            color = _model_color(key)
            if i == len(keys_sorted) - 1:
                blocks = BAR_WIDTH - allocated
            else:
                blocks = round(cnt / total * BAR_WIDTH)
            allocated += blocks
            bar += f"{color}{'█' * blocks}{_RESET}"
        print(f"    {d}  {count:4d}  {bar}")

    # Color legend
    print("\n  Legend:")
    for key in sorted(seen_keys):
        color = _model_color(key)
        label = _model_label(key)
        print(f"    {color}██{_RESET}  {color}{label}{_RESET}")

    if not instance_filter:
        print("\n  By instance:")
        for k, v in sorted(by_instance.items(), key=lambda x: -x[1]):
            print(f"    {k:10s}  {v:4d}")

    print("\n  By channel:")
    for k, v in sorted(by_channel.items(), key=lambda x: -x[1]):
        print(f"    {k:12s}  {v:4d}")

    print("\n  By model:")
    for k, v in sorted(by_model.items(), key=lambda x: -x[1]):
        color = _model_color(k)
        print(f"    {color}██{_RESET}  {color}{k:43s}{_RESET}  {v:4d}")

    # Billing breakdown — use cost data if available, fall back to turn counts
    has_cost = any(t.get('cost', 0) for t in turns)

    token_cost     = sum(t.get('cost',0) or 0 for t in turns if t.get('model','').startswith('anthropic/') and t.get('auth_mode') == 'token')
    api_key_cost   = sum(t.get('cost',0) or 0 for t in turns if t.get('model','').startswith('anthropic/') and t.get('auth_mode') != 'token')
    non_anth_cost  = sum(t.get('cost',0) or 0 for t in turns if not t.get('model','').startswith('anthropic/'))
    total_cost     = sum(t.get('cost',0) or 0 for t in turns)
    human_cost     = sum(t.get('cost',0) or 0 for t in turns if t.get('source') == 'human')
    cron_cost      = sum(t.get('cost',0) or 0 for t in turns if t.get('source') == 'cron')
    subagent_cost  = sum(t.get('cost',0) or 0 for t in turns if t.get('source') == 'subagent')

    anthropic_token   = sum(1 for t in turns if t.get('model','').startswith('anthropic/') and t.get('auth_mode') == 'token')
    anthropic_api_key = sum(1 for t in turns if t.get('model','').startswith('anthropic/') and t.get('auth_mode') != 'token')
    non_anthropic     = sum(1 for t in turns if not t.get('model','').startswith('anthropic/'))
    total_flat        = anthropic_token
    total_metered     = anthropic_api_key + non_anthropic

    print("\n  Billing summary:")
    if has_cost:
        print(f"    {'Total tracked cost':42s}  ${total_cost:.4f}")
        print(f"    {'  MAX subscription (flat — token)':42s}  ${token_cost:.4f}  ({anthropic_token} calls)")
        print(f"    {'  API key metered — Anthropic fallback':42s}  ${api_key_cost:.4f}  ({anthropic_api_key} calls)")
        print(f"    {'  API key metered — Non-Anthropic':42s}  ${non_anth_cost:.4f}  ({non_anthropic} calls)")
        if total_cost > 0:
            pct = round(100 * token_cost / total_cost, 1)
            print(f"    {'  Anthropic MAX coverage (by cost)':42s}  {pct}%")
        print(f"    By source:")
        print(f"      {'human conversations':40s}  ${human_cost:.4f}")
        print(f"      {'cron / automated':40s}  ${cron_cost:.4f}")
        print(f"      {'sub-agents / spawned':40s}  ${subagent_cost:.4f}")
    else:
        print(f"    {'MAX subscription (flat — Anthropic token)':42s}  {total_flat:4d} calls")
        print(f"    {'API key / metered (total)':42s}  {total_metered:4d} calls")
        print(f"      {'└ Anthropic API key fallback':40s}  {anthropic_api_key:4d}")
        print(f"      {'└ Non-Anthropic (OpenAI/Google/xAI/etc)':40s}  {non_anthropic:4d}")
        if total_flat + total_metered > 0:
            pct = round(100 * total_flat / (total_flat + total_metered))
            print(f"    {'Anthropic MAX coverage':42s}  {pct}%")
        print(f"    (Re-run [c] collect to get cost data from session files)")

    print("\n  By user (top 10):")
    for k, v in sorted(by_user.items(), key=lambda x: -x[1])[:10]:
        print(f"    {k:45s}  {v:4d}")
    print()


def _usage_menu(bot="all"):
    """Usage reports scoped to the currently selected bot (or 'all')."""
    inst_filter = None if bot == "all" else bot

    while True:
        scope = "all bots" if bot == "all" else bot
        print()
        print(f"  ── Usage Reports ({scope}) ──────────────────────────")
        print("    [1]  Today")
        print("    [2]  Last 7 days")
        print("    [3]  Last 30 days")
        print("    [4]  Custom date range")
        print("    [5]  Filter by channel")
        print("    [c]  Collect turns now (re-parse gateway log)")
        print("    [x]  Export CSV")
        print("    [b]  Back")
        print()
        choice = input("  Choice: ").strip().lower()

        if choice == "b":
            break

        elif choice == "1":
            turns = _load_turns(days=1, instance_filter=inst_filter)
            _print_usage_summary(turns, 1, instance_filter=inst_filter)

        elif choice == "2":
            turns = _load_turns(days=7, instance_filter=inst_filter)
            _print_usage_summary(turns, 7, instance_filter=inst_filter)

        elif choice == "3":
            turns = _load_turns(days=30, instance_filter=inst_filter)
            _print_usage_summary(turns, 30, instance_filter=inst_filter)

        elif choice == "4":
            raw = input("  Start date (YYYY-MM-DD): ").strip()
            try:
                start = datetime.strptime(raw, "%Y-%m-%d")
                end_raw = input("  End date (YYYY-MM-DD, Enter for today): ").strip()
                end = datetime.strptime(end_raw, "%Y-%m-%d") if end_raw else datetime.now()
                days = (end - start).days + 1
                turns = _load_turns(days=days, end_date=end, instance_filter=inst_filter)
                _print_usage_summary(turns, days, instance_filter=inst_filter)
            except ValueError:
                print("  Invalid date format.")

        elif choice == "5":
            chan = input("  Channel (telegram/slack/discord/...): ").strip().lower()
            days_raw = input("  Days (default 7): ").strip()
            days = int(days_raw) if days_raw.isdigit() else 7
            turns = _load_turns(days=days, instance_filter=inst_filter)
            turns = [t for t in turns if t.get("channel") == chan]
            _print_usage_summary(turns, days, instance_filter=inst_filter)

        elif choice == "c":
            print()
            collect_bots = list(BOT_LOGS.keys()) if bot == "all" else [bot]
            print(f"  Collecting turns for: {', '.join(collect_bots)}...")
            if not os.path.exists(COLLECTOR):
                print(f"  ❌ Collector not found: {COLLECTOR}")
                continue
            for inst in collect_bots:
                log_path = BOT_LOGS.get(inst)
                mem_dir  = TURN_DIRS.get(inst)
                if not log_path or not os.path.exists(log_path):
                    print(f"  ⚠️  {inst}: log not found")
                    continue
                r = subprocess.run(
                    ["python3", COLLECTOR,
                     "--instance", inst,
                     "--log", log_path,
                     "--out", mem_dir],
                    capture_output=True, text=True
                )
                for line in (r.stdout + r.stderr).strip().splitlines():
                    print(f"  {line}")
            print("  Done.")

        elif choice == "x":
            days_raw = input("  Days to export (default 30): ").strip()
            days = int(days_raw) if days_raw.isdigit() else 30
            out_path = input("  Output file (default: /tmp/turns-export.csv): ").strip()
            if not out_path:
                out_path = "/tmp/turns-export.csv"
            turns = _load_turns(days=days, instance_filter=inst_filter)
            turns.sort(key=lambda t: t.get("ts", ""))
            fields = ["ts", "instance", "channel", "user_id", "model", "source", "msg_id"]
            with open(out_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(turns)
            print(f"  ✅ Exported {len(turns)} turns to {out_path}")

        else:
            print("  Unknown option.")


# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_json(path):
    with open(path, "r") as f:
        content = f.read()
    content = re.sub(r',(\s*[}\]])', r'\1', content)
    return json.loads(content)

def _preserve_write(data, path):
    """Write JSON preserving the file's existing ownership and permissions."""
    path = str(path)
    # Capture existing stat before writing
    try:
        st = os.stat(path)
        existing_uid = st.st_uid
        existing_gid = st.st_gid
        existing_mode = stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        existing_uid = existing_gid = existing_mode = None

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    if existing_uid is not None:
        try:
            os.chown(path, existing_uid, existing_gid)
            os.chmod(path, existing_mode)
        except OSError as e:
            print(f"  ⚠️  Could not restore permissions on {path}: {e}")

def save_json(data, path, user):
    _preserve_write(data, path)

def save_auth(data, path, user):
    _preserve_write(data, path)


# ── Model helpers ─────────────────────────────────────────────────────────────

def get_model_config(data):
    return data.get("agents", {}).get("defaults", {}).get("model", {})

def set_model_config(data, mc):
    data.setdefault("agents", {}).setdefault("defaults", {})["model"] = mc

def get_catalog(data):
    return data.get("agents", {}).get("defaults", {}).get("models", {})

def set_catalog(data, catalog):
    data.setdefault("agents", {}).setdefault("defaults", {})["models"] = catalog

def display_models(model_config, catalog, indent="  "):
    primary   = model_config.get("primary", "(none)")
    fallbacks = model_config.get("fallbacks", [])

    print(f"{indent}Primary:   {primary}")
    for i, f in enumerate(fallbacks, 1):
        print(f"{indent}Fallback {i}: {f}")

    active = [primary] + [f for f in fallbacks if f != primary]
    all_models = list(active)
    for m in catalog:
        if m not in all_models:
            all_models.append(m)

    print(f"{indent}Catalog:")
    for i, m in enumerate(all_models, 1):
        alias = catalog.get(m, {}).get("alias", "")
        alias_str = f"  [{alias}]" if alias else ""
        if m == primary:
            tag = "  ← PRIMARY"
        elif m in fallbacks:
            tag = f"  ← FALLBACK {fallbacks.index(m)+1}"
        else:
            tag = ""
        print(f"{indent}  {i:2}. {m}{alias_str}{tag}")

    return all_models


# ── Auth helpers ──────────────────────────────────────────────────────────────

def mask(key):
    if not key or len(key) < 12:
        return key or "(empty)"
    return key[:8] + "..." + key[-4:]

def find_profile(profiles, provider, mode):
    for name, p in profiles.items():
        if p.get("provider") == provider:
            pmode = p.get("type", p.get("mode", ""))
            if pmode == mode:
                field = "token" if mode == "token" else "key"
                if field in p:
                    return name, p, field
    return None, None, None


# ── Gateway ───────────────────────────────────────────────────────────────────

def restart_gateway(bot):
    svc = BOTS[bot]["service"]
    r = subprocess.run(["launchctl", "kickstart", "-k", f"system/{svc}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  ✅ {svc} restarted")
    else:
        print(f"  ⚠️  {r.stderr.strip() or 'unknown error'}")


# ── Bot context loader ────────────────────────────────────────────────────────

def load_bot(bot):
    """Load config and auth for a single bot. Returns (info, config, auth) or raises."""
    info = BOTS[bot]
    config = load_json(info["config"])
    try:
        auth = load_json(info["auth"]) if os.path.exists(info["auth"]) else {"profiles": {}}
    except Exception:
        auth = {"profiles": {}}
    return info, config, auth

def load_all_bots():
    """Load all bots. Returns dict of bot -> (info, config, auth)."""
    result = {}
    for bot in BOTS:
        try:
            info, config, auth = load_bot(bot)
            result[bot] = (info, config, auth)
        except Exception as e:
            print(f"  ⚠️  Could not load {bot}: {e}")
    return result


# ── Exec approvals ────────────────────────────────────────────────────────────

def show_exec_approvals(bot: str, config: dict):
    ea = config.get("execApprovals", {})
    print(f"\n  ── {bot.upper()} Exec Approvals ──────────────────────────────")
    if not ea:
        print("  ❌ Not configured — agent cannot run shell commands.")
        print("  Add approvers to enable approval-gated exec.")
        return
    approvers = ea.get("approvers", [])
    channels  = ea.get("channels", [])
    print(f"  Approvers: {approvers if approvers else '(none)'}")
    print(f"  Channels:  {channels if channels else '(none)'}")
    if approvers and channels:
        print(f"  ✅ Active — agent will request approval before running commands.")
    else:
        print(f"  ⚠️  Incomplete — need both approvers and channels.")


def manage_exec_approvals(bot: str, config: dict, save_fn):
    while True:
        show_exec_approvals(bot, config)
        ea = config.setdefault("execApprovals", {})
        print()
        print("  [a] Add approver ID   [r] Remove approver")
        print("  [c] Set channels      [x] Clear all (disable)")
        print("  [Enter] Back")
        sub = input("  Choice: ").strip().lower()

        if sub == "a":
            uid = input("  Slack/Telegram user ID to add: ").strip()
            if not uid:
                continue
            approvers = ea.setdefault("approvers", [])
            if uid in approvers:
                print(f"  Already in list.")
            else:
                approvers.append(uid)
                save_fn()
                print(f"  ✅ Added {uid}")

        elif sub == "r":
            approvers = ea.get("approvers", [])
            if not approvers:
                print("  No approvers to remove.")
                continue
            for i, a in enumerate(approvers, 1):
                print(f"    {i}. {a}")
            num = input("  Number to remove: ").strip()
            try:
                idx = int(num) - 1
                removed = approvers.pop(idx)
                save_fn()
                print(f"  ✅ Removed {removed}")
            except (ValueError, IndexError):
                print("  Invalid number.")

        elif sub == "c":
            print("  Available: slack, telegram, discord")
            print(f"  Current: {ea.get('channels', [])}")
            raw = input("  Channels (comma-separated): ").strip()
            channels = [x.strip() for x in raw.split(",") if x.strip()]
            ea["channels"] = channels
            save_fn()
            print(f"  ✅ Channels set to: {channels}")

        elif sub == "x":
            confirm = input("  Clear exec approvals (disables exec)? (y/N): ").strip().lower()
            if confirm == "y":
                config.pop("execApprovals", None)
                save_fn()
                print("  ✅ Exec approvals cleared.")
                break

        else:
            break


# ── Help text ─────────────────────────────────────────────────────────────────

HELP_TEXT = """
  ══════════════════════════════════════════════════════════
  OpenClaw Admin Tool — Help
  ══════════════════════════════════════════════════════════

  OVERVIEW
  ────────
  Manages all OpenClaw bots on this Mac mini. Each bot has:
  - openclaw.json      — main config (channels, plugins, models)
  - auth-profiles.json — API keys & tokens (sensitive, 600 perms)

  All saves preserve existing file ownership & permissions.

  MENU OPTIONS
  ────────────
  [a] Add model      Add a model to the catalog (e.g. anthropic/claude-sonnet-4-6)
  [r] Remove model   Remove a model (must not be primary/fallback)
  [o] Set order      Set primary model + fallback order by number
  [k] Rotate keys    Update API keys: Anthropic, OpenAI, Google, Brave, Slack, Telegram
  [g] Restart        Restart the bot's gateway via launchctl kickstart
  [i] Info           Show model config, auth profile status, gateway process
  [l] Logs           Tail gateway.log and gateway.err.log
  [x] Processes      Show/kill gateway processes
  [p] Plugins        Manage plugin allow/deny lists and enable/disable entries
                     ⚠️  Setting a non-empty allow list EXCLUDES all other plugins!
  [d] Doctor         Run `openclaw doctor` for the bot
  [b] Switch bot     Switch to another bot without quitting
  [w] Google WS      Check GWS token status and run OAuth reauth flow
  [c] Calendars      Manage secret iCal/feed URLs (stored in ops/config/calendars.json)
                     NOT in auth-profiles — gateway never touches this file
  [x] Exec approvals Configure who can approve shell command execution for this bot
                     Required for bots to run exec/shell commands safely
  [e] Evolve network Manage the Evolve RSI network — status, proposals, deploy
  [u] Usage reports  Turn-level usage stats by date/channel/model; CSV export
  [v] Version        Check installed vs latest OpenClaw; upgrade if available
  [h] Help           This screen
  [q] Quit

  KEY CONCEPTS
  ────────────
  plugins.allow      Allowlist — if non-empty, ONLY listed plugins load.
                     Empty = all plugins load. Must remove key entirely, not set [].
  plugins.deny       Denylist — listed plugins are skipped. Safe to leave empty.
  auth-profiles      Holds AI provider keys. Gateway reads on startup and writes
                     back on profile updates — do not manually edit while running.
  execApprovals      Controls exec gating. Without this, bots cannot run commands.
                     Add your Slack/Telegram user ID as an approver.
  lastGood           Tracks which auth profile last succeeded — gateway uses this
                     to prefer working profiles automatically.

  COMMON WORKFLOWS
  ────────────────
  Rotate a key:      [k] → enter new key → gateway auto-reloads for most changes
  Bot not responding: [i] to check profile status, [l] for recent errors, [g] restart
  Plugin not loading: [p] → check allow list isn't excluding it
  Slack disconnected: Check allow list first (happened Mar 2026 — unity was the only
                      allowed plugin, silently blocking Slack)
  Duplicate process:  [x] → kill older PID (PID lock now prevents recurrence)
  ══════════════════════════════════════════════════════════
"""

def show_help():
    print(HELP_TEXT)
    input("  Press Enter to continue...")


# ── Exec approvals (exec-approvals.json) ─────────────────────────────────────

SECURITY_LEVELS = ["deny", "allowlist", "full"]
ASK_MODES       = ["off", "on-miss", "always"]
ASK_FALLBACKS   = ["deny", "allowlist", "full"]

COMMON_ALLOWLIST_PATTERNS = [
    ("/opt/homebrew/bin/python3*",       "Homebrew Python 3"),
    ("/usr/bin/python3*",                "System Python 3"),
    ("/opt/homebrew/bin/node",           "Homebrew Node.js"),
    ("/opt/homebrew/bin/npx",            "Homebrew npx"),
    ("/opt/homebrew/bin/git",            "Homebrew git"),
    ("/usr/bin/git",                     "System git"),
    ("/opt/homebrew/bin/bash",           "Homebrew bash"),
    ("/bin/bash",                        "System bash"),
    ("/bin/sh",                          "System sh"),
    ("/opt/homebrew/bin/zsh",            "Homebrew zsh"),
    ("/bin/zsh",                         "System zsh"),
    ("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python", "Xcode Python 3"),
]


def _load_exec_approvals(info: dict) -> dict:
    path = info.get("exec_approvals", "")
    if path and os.path.exists(path):
        try:
            return json.loads(open(path).read())
        except Exception:
            pass
    return {"version": 1, "defaults": {}, "agents": {}}


def _save_exec_approvals(info: dict, data: dict):
    path = info.get("exec_approvals", "")
    if not path:
        print("  ❌ No exec-approvals path configured for this bot.")
        return False
    if os.path.exists(path):
        st = os.stat(path)
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    else:
        uid = gid = mode = None
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    if uid is not None:
        try:
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        except OSError:
            pass
    else:
        try:
            import pwd
            uid = pwd.getpwnam(info["user"]).pw_uid
            os.chown(path, uid, -1)
            os.chmod(path, 0o600)
        except Exception:
            pass
    print(f"  💾 Saved: {path}")
    return True


def show_exec_approvals(bot: str, info: dict):
    data = _load_exec_approvals(info)
    path = info.get("exec_approvals", "")
    exists = os.path.exists(path) if path else False

    print(f"\n  ── {bot.upper()} Exec Approvals ──────────────────────────────")
    print(f"  File: {path}  {'✅ exists' if exists else '❌ not found'}")

    defaults = data.get("defaults", {})
    sec  = defaults.get("security", "(not set — uses openclaw default)")
    ask  = defaults.get("ask", "(not set)")
    fallback = defaults.get("askFallback", "(not set)")
    auto = defaults.get("autoAllowSkills", False)
    print(f"\n  Defaults:")
    print(f"    security:       {sec}")
    print(f"    ask:            {ask}")
    print(f"    askFallback:    {fallback}")
    print(f"    autoAllowSkills:{auto}")

    agents = data.get("agents", {})
    main = agents.get("main", {})
    if main:
        msec = main.get("security", "(inherited)")
        mask = main.get("ask", "(inherited)")
        mfb  = main.get("askFallback", "(inherited)")
        mauto = main.get("autoAllowSkills", False)
        allowlist = main.get("allowlist", [])
        print(f"\n  Agent 'main':")
        print(f"    security:       {msec}")
        print(f"    ask:            {mask}")
        print(f"    askFallback:    {mfb}")
        print(f"    autoAllowSkills:{mauto}")
        if allowlist:
            print(f"    Allowlist ({len(allowlist)} entries):")
            for i, entry in enumerate(allowlist, 1):
                pattern = entry.get("pattern", entry) if isinstance(entry, dict) else entry
                comment = entry.get("comment", "") if isinstance(entry, dict) else ""
                last = entry.get("lastUsedCommand", "") if isinstance(entry, dict) else ""
                comment_str = f"  # {comment}" if comment else ""
                last_str    = f"  [last: {last[:40]}]" if last else ""
                print(f"      {i:2}. {pattern}{comment_str}{last_str}")
        else:
            print(f"    Allowlist: (empty)")
    else:
        print(f"\n  Agent 'main': (not configured)")


def manage_exec_approvals(bot: str, info: dict):
    while True:
        data = _load_exec_approvals(info)
        show_exec_approvals(bot, info)
        print()
        print("  [s] Set security/ask defaults   [a] Add allowlist pattern")
        print("  [r] Remove allowlist pattern     [q] Add common patterns (quick setup)")
        print("  [t] Toggle autoAllowSkills       [Enter] Back")
        sub = input("  Choice: ").strip().lower()

        if sub == "s":
            print()
            print(f"  Security levels: {', '.join(SECURITY_LEVELS)}")
            sec = input(f"  security (deny/allowlist/full): ").strip().lower()
            if sec not in SECURITY_LEVELS:
                print(f"  ❌ Invalid. Choose from: {SECURITY_LEVELS}")
                continue
            print(f"  Ask modes: {', '.join(ASK_MODES)}")
            ask = input(f"  ask (off/on-miss/always): ").strip().lower()
            if ask not in ASK_MODES:
                print(f"  ❌ Invalid.")
                continue
            print(f"  Ask fallback: {', '.join(ASK_FALLBACKS)}")
            fb = input(f"  askFallback (deny/allowlist/full): ").strip().lower()
            if fb not in ASK_FALLBACKS:
                print(f"  ❌ Invalid.")
                continue
            # Set both defaults and agent main
            for scope in [data.setdefault("defaults", {}),
                          data.setdefault("agents", {}).setdefault("main", {})]:
                scope["security"]    = sec
                scope["ask"]         = ask
                scope["askFallback"] = fb
            _save_exec_approvals(info, data)
            print(f"  ✅ Set security={sec} ask={ask} askFallback={fb}")

        elif sub == "a":
            print()
            print("  Enter ONE absolute path pattern.")
            print("  ⚠️  Do NOT use slash-separated lists (curl/grep/...) — each tool needs its own entry.")
            print("  Use [q] Quick setup for common tools instead.")
            pattern = input("  Pattern (e.g. /opt/homebrew/bin/python3*): ").strip()
            if not pattern:
                continue
            # Must be an absolute path
            if not pattern.startswith("/"):
                print(f"  ❌ Pattern must be an absolute path starting with /")
                print(f"     Use [q] Quick setup or add tools one at a time.")
                continue
            # Detect slash-separated tool list disguised as a path (e.g. /curl/grep/shasum)
            parts = [p for p in pattern.split("/") if p]
            if len(parts) > 1 and all(len(p) < 20 and "." not in p and "*" not in p for p in parts):
                print(f"  ⚠️  This looks like a slash-separated tool list, not a valid absolute path.")
                print(f"     Valid example: /opt/homebrew/bin/python3*")
                print(f"     Use [q] Quick setup to add common tools as individual entries.")
                confirm = input("  Add anyway? (y/N): ").strip().lower()
                if confirm != "y":
                    continue
            comment = input("  Comment (optional): ").strip()
            entry = {"pattern": pattern}
            if comment:
                entry["comment"] = comment
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = [e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist]
            if pattern in existing:
                print(f"  Already in allowlist.")
                continue
            allowlist.append(entry)
            _save_exec_approvals(info, data)
            print(f"  ✅ Added: {pattern}")

        elif sub == "r":
            allowlist = data.get("agents", {}).get("main", {}).get("allowlist", [])
            if not allowlist:
                print("  Nothing to remove.")
                continue
            for i, entry in enumerate(allowlist, 1):
                pattern = entry.get("pattern", entry) if isinstance(entry, dict) else entry
                comment = entry.get("comment", "") if isinstance(entry, dict) else ""
                print(f"    {i:2}. {pattern}  {('# ' + comment) if comment else ''}")
            num = input("  Number to remove: ").strip()
            try:
                idx = int(num) - 1
                removed = allowlist.pop(idx)
                pattern = removed.get("pattern", removed) if isinstance(removed, dict) else removed
                _save_exec_approvals(info, data)
                print(f"  ✅ Removed: {pattern}")
            except (ValueError, IndexError):
                print("  Invalid number.")

        elif sub == "q":
            print()
            print("  Common patterns:")
            for i, (pattern, label) in enumerate(COMMON_ALLOWLIST_PATTERNS, 1):
                print(f"    {i:2}. {label:40s} {pattern}")
            print()
            raw = input("  Numbers to add (space-separated, or 'all'): ").strip()
            if raw.lower() == "all":
                indices = list(range(len(COMMON_ALLOWLIST_PATTERNS)))
            else:
                try:
                    indices = [int(x) - 1 for x in raw.split()]
                except ValueError:
                    print("  Invalid input.")
                    continue
            allowlist = data.setdefault("agents", {}).setdefault("main", {}).setdefault("allowlist", [])
            existing = {e.get("pattern", e) if isinstance(e, dict) else e for e in allowlist}
            added = []
            for idx in indices:
                try:
                    pattern, label = COMMON_ALLOWLIST_PATTERNS[idx]
                    if pattern not in existing:
                        allowlist.append({"pattern": pattern, "comment": label})
                        existing.add(pattern)
                        added.append(label)
                except IndexError:
                    pass
            if added:
                _save_exec_approvals(info, data)
                print(f"  ✅ Added: {', '.join(added)}")
            else:
                print("  Nothing new to add.")

        elif sub == "t":
            main = data.setdefault("agents", {}).setdefault("main", {})
            current = main.get("autoAllowSkills", False)
            main["autoAllowSkills"] = not current
            _save_exec_approvals(info, data)
            print(f"  ✅ autoAllowSkills → {not current}")

        else:
            break


# ── Calendar secret URL helpers ──────────────────────────────────────────────

def _cal_config_path(user: str) -> Path:
    """Per-bot calendars config — in workspace, never touched by the gateway."""
    return Path(f"/Users/{user}/.openclaw/workspace/ops/config/calendars.json")


def _load_calendars(user: str) -> dict:
    path = _cal_config_path(user)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"calendars": []}


def _save_calendars(user: str, data: dict):
    path = _cal_config_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve permissions if file exists
    if path.exists():
        st = os.stat(str(path))
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    else:
        uid = gid = mode = None
    path.write_text(json.dumps(data, indent=2))
    if uid is not None:
        try:
            os.chown(str(path), uid, gid)
            os.chmod(str(path), mode)
        except OSError:
            pass
    else:
        # New file: own it by the bot user
        try:
            import pwd
            uid = pwd.getpwnam(user).pw_uid
            os.chown(str(path), uid, -1)
            os.chmod(str(path), 0o600)
        except Exception:
            pass
    print(f"  💾 Saved: {path}")


def show_calendars(bot: str, info: dict):
    user = info["user"]
    data = _load_calendars(user)
    calendars = data.get("calendars", [])
    print(f"\n  ── {bot.upper()} Calendar URLs ──────────────────────────────────")
    print(f"  File: {_cal_config_path(user)}")
    if not calendars:
        print("  (none configured)")
        return
    for entry in calendars:
        url = entry.get("url", "")
        masked = url[:40] + "..." if len(url) > 40 else url
        print(f"  [{entry.get('id','')}] {entry.get('name','')}  ({entry.get('type','')})")
        print(f"    url: {masked}")


def manage_calendars(bot: str, info: dict, auth: dict, save_fn):
    user = info["user"]

    while True:
        data = _load_calendars(user)
        calendars = data.setdefault("calendars", [])
        show_calendars(bot, info)
        print()
        print("  [a] Add calendar URL   [r] Remove   [Enter] Back")
        sub = input("  Choice: ").strip().lower()

        if sub == "a":
            print()
            name = input("  Name (e.g. 'Example Corp Gmail', 'Xola — The Attraction'): ").strip()
            if not name:
                print("  Cancelled.")
                continue
            # Derive id from name: lowercase, spaces/special → hyphens
            cal_id = re.sub(r'[^a-z0-9\-]', '-', name.lower()).strip('-')
            cal_id = re.sub(r'-+', '-', cal_id)
            cal_id = input(f"  ID (default: {cal_id}): ").strip() or cal_id
            # Check for duplicate id
            if any(e.get("id") == cal_id for e in calendars):
                overwrite = input(f"  '{cal_id}' already exists. Overwrite? (y/N): ").strip().lower()
                if overwrite != "y":
                    print("  Cancelled.")
                    continue
                calendars[:] = [e for e in calendars if e.get("id") != cal_id]
            cal_type = input("  Type [ical_url/google_api/xola] (default: ical_url): ").strip() or "ical_url"
            print("  ⚠️  Secret URLs are like passwords — don't share them.")
            url = input("  URL: ").strip()
            if not url:
                print("  Cancelled.")
                continue
            calendars.append({
                "id": cal_id,
                "name": name,
                "type": cal_type,
                "url": url,
            })
            _save_calendars(user, data)
            print(f"  ✅ Saved '{name}' (id: {cal_id})")

        elif sub == "r":
            if not calendars:
                print("  Nothing to remove.")
                continue
            for i, e in enumerate(calendars, 1):
                print(f"    {i}. [{e.get('id','')}] {e.get('name','')}")
            num = input("  Number to remove: ").strip()
            try:
                idx = int(num) - 1
                entry = calendars[idx]
                confirm = input(f"  Remove '{entry.get('name','')}' ({entry.get('id','')})?  (y/N): ").strip().lower()
                if confirm == "y":
                    calendars.pop(idx)
                    _save_calendars(user, data)
                    print(f"  ✅ Removed")
            except (ValueError, IndexError):
                print("  Invalid number.")

        else:
            break


# ── Google Workspace helpers ──────────────────────────────────────────────────

# Map bot name → Google account email
GWS_ACCOUNTS = {
    "admin_bot": "admin_bot@example.com",
    "team_bot_a":   "team_bot_a@example.com",
}

GWS_CONFIG_DIR = ".config/gws"
GWS_NPX        = "/opt/homebrew/bin/npx"
GWS_TOKEN_MAX_AGE_DAYS = 14  # warn if token_cache.json older than this


def _gws_config_dir(user: str) -> Path:
    return Path(f"/Users/{user}") / GWS_CONFIG_DIR


def _gws_token_status(user: str) -> dict:
    """Return a dict with keys: configured, token_age_days, token_fresh, missing_files."""
    cfg = _gws_config_dir(user)
    result = {
        "configured": False,
        "token_age_days": None,
        "token_fresh": False,
        "missing_files": [],
    }
    for fname in ("client_secret.json", "credentials.enc", "token_cache.json"):
        if not (cfg / fname).exists():
            result["missing_files"].append(fname)

    if result["missing_files"]:
        return result

    result["configured"] = True
    token_cache = cfg / "token_cache.json"
    age_days = (datetime.now().timestamp() - token_cache.stat().st_mtime) / 86400
    result["token_age_days"] = round(age_days, 1)
    result["token_fresh"] = age_days < GWS_TOKEN_MAX_AGE_DAYS
    return result


def show_gws_status(bot: str, info: dict):
    user = info["user"]
    account = GWS_ACCOUNTS.get(bot)
    cfg = _gws_config_dir(user)
    print(f"\n  ── {bot.upper()} Google Workspace ──────────────────────────")

    if not account:
        print(f"  (no Google account configured for {bot})")
        return

    print(f"  Account: {account}")
    status = _gws_token_status(user)

    if status["missing_files"]:
        print(f"  ❌ Missing: {', '.join(status['missing_files'])}")
        print(f"  Run reauth to set up Google credentials.")
        return

    age = status["token_age_days"]
    fresh = status["token_fresh"]
    age_str = f"{age}d ago" if age is not None else "unknown"
    icon = "✅" if fresh else "⚠️ "
    print(f"  Token cache: {icon} last refreshed {age_str}"
          + ("" if fresh else f"  (>{GWS_TOKEN_MAX_AGE_DAYS}d — likely expired)"))

    # Try a quick token validity check via token_cache.json
    try:
        import json as _json
        tc = _json.loads((cfg / "token_cache.json").read_text())
        expiry = tc.get("expiry") or tc.get("token", {}).get("expiry")
        if expiry:
            from datetime import timezone as _tz
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            now_dt = datetime.now(_tz.utc)
            if exp_dt < now_dt:
                print(f"  ❌ Token expired at {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}")
            else:
                mins = int((exp_dt - now_dt).total_seconds() / 60)
                print(f"  ✅ Token valid for ~{mins}m")
    except Exception:
        pass

    print(f"\n  Config dir: {cfg}")
    print(f"  Reauth cmd: sudo -u {user} bash -c \"cd /Users/{user} && {GWS_NPX} @googleworkspace/cli auth login --account {account}\"")


def run_gws_reauth(bot: str, info: dict):
    user = info["user"]
    account = GWS_ACCOUNTS.get(bot)
    if not account:
        print(f"  ❌ No Google account configured for {bot}.")
        return

    print(f"\n  ── Reauth {bot.upper()} ({account}) ──────────────────────────")
    print(f"  This will open a browser OAuth flow.")
    print(f"  ⚠️  You must be at the machine (or have a display available).")
    confirm = input(f"  Proceed? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    cmd = ["sudo", "-u", user, "bash", "-c",
           f"cd /Users/{user} && {GWS_NPX} @googleworkspace/cli auth login --account {account}"]
    print(f"\n  Running: {' '.join(cmd[3:])}")
    print()
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print(f"\n  ✅ Reauth complete. Verify with [w] → status.")
    else:
        print(f"\n  ❌ Reauth failed (rc={result.returncode}). Try running manually:")
        print(f"     sudo -u {user} bash -c \"cd /Users/{user} && {GWS_NPX} @googleworkspace/cli auth login --account {account}\"")


# ── Info display ──────────────────────────────────────────────────────────────

def show_info(bot, info, config, auth):
    from datetime import datetime
    user = info["user"]
    print(f"\n  ── {bot.upper()} ({user}) ────────────────────────────")

    mc = get_model_config(config)
    catalog = get_catalog(config)
    print(f"  Primary: {mc.get('primary', '(none)')}")
    for i, f in enumerate(mc.get('fallbacks', []), 1):
        print(f"  Fallback {i}: {f}")

    port = config.get("gateway", {}).get("port", "?")
    print(f"  Port: {port}")

    last_good = auth.get("lastGood", {})
    stats = auth.get("usageStats", {})
    now_ms = int(datetime.now().timestamp() * 1000)
    for pname, pdata in auth.get("profiles", {}).items():
        pstats    = stats.get(pname, {})
        last_used = pstats.get("lastUsed", 0)
        cooldown  = pstats.get("cooldownUntil", 0)
        errors    = pstats.get("errorCount", 0)
        failures  = pstats.get("failureCounts", {})
        is_good   = any(v == pname for v in last_good.values())
        is_cool   = cooldown > now_ms
        cool_secs = max(0, (cooldown - now_ms) // 1000)
        cool_min  = cool_secs // 60
        atype = pdata.get("type", pdata.get("mode", "?"))
        type_label = {"token": "MAX/token", "api_key": "API key"}.get(atype, atype)
        provider = pdata.get("provider", "?")
        if is_cool:
            fail_reason = ", ".join(f"{k}" for k in failures)
            status = f"🔴 rate limited ({cool_min}m {cool_secs%60}s)"
        elif errors > 0:
            status = f"⚠️  {errors} error(s)"
        elif is_good:
            status = "✅ ok"
        else:
            status = ""
        lu_str = datetime.fromtimestamp(last_used/1000).strftime("%H:%M") if last_used else "never"
        print(f"    {pname:20s} [{provider}/{type_label}] used:{lu_str} {status}")

    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = [l for l in result.stdout.splitlines()
             if "openclaw-gateway" in l and l.split()[0] == user]
    if procs:
        proc_status = f"✅ running ({len(procs)} proc)" if len(procs) == 1 else f"⚠️  {len(procs)} processes"
    else:
        proc_status = "❌ NOT RUNNING"
    print(f"  Gateway: {proc_status}")

def show_logs(bot, info, n=30):
    user = info["user"]
    print(f"\n  ── {bot.upper()} logs ──────────────────────────────")
    for path, label in [
        (f"/Users/{user}/.openclaw/logs/gateway.log", "gateway.log"),
        (f"/Users/{user}/.openclaw/logs/gateway.err.log", "gateway.err.log"),
    ]:
        if os.path.exists(path):
            r = subprocess.run(["tail", f"-{n}", path], capture_output=True, text=True)
            print(f"  {label}:")
            print(r.stdout or "  (empty)")
        else:
            print(f"  {label}: not found")

def show_process(bot, info):
    user = info["user"]
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    procs = [l for l in result.stdout.splitlines()
             if "openclaw-gateway" in l and l.split()[0] == user]
    print(f"\n  ── {bot.upper()} ({user}) ──")
    if not procs:
        print(f"  ❌ NOT RUNNING")
    else:
        pids = []
        for line in procs:
            parts = line.split()
            pid  = parts[1]
            stat = parts[7] if len(parts) > 7 else "?"
            time = parts[9] if len(parts) > 9 else "?"
            pids.append(pid)
            print(f"  PID {pid}  stat={stat}  started={time}")
    return procs


# ── Cost settings menu ────────────────────────────────────────────────────────

def manage_cost_settings(bot, info, config, save_cfg):
    """View and edit cost-related settings for a bot."""
    import json as _json

    def get_nested(d, *keys, default=None):
        for k in keys:
            if not isinstance(d, dict): return default
            d = d.get(k, {})
        return d if d != {} else default

    def set_nested(d, keys, value):
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    while True:
        defaults   = config.get("agents", {}).get("defaults", {})
        compaction = defaults.get("compaction", {})
        session    = config.get("session", {})
        reset      = session.get("reset", {})

        idle_min    = reset.get("idleMinutes", "(not set)")
        # softThresholdTokens lives in compaction.memoryFlush, NOT on compaction directly
        soft_thresh = compaction.get("memoryFlush", {}).get("softThresholdTokens", "(not set)")
        bootstrap   = defaults.get("bootstrapTotalMaxChars", "(not set)")

        # lastGood from auth file
        try:
            auth_data = _json.load(open(info["auth"]))
            last_good = auth_data.get("lastGood", {})
            last_good_str = ", ".join(f"{k}={v}" for k, v in last_good.items()) or "(empty — good)"
        except Exception:
            last_good_str = "(unreadable)"

        print(f"\n  ── Cost Settings: {bot} ──")
        print(f"  [1] Idle session reset:       {idle_min} minutes")
        print(f"      (fresh session after N min idle; 0=disable)")
        print(f"  [2] Memory flush threshold:   {soft_thresh} tokens")
        print(f"      (pre-compaction housekeeping; ~150000 recommended)")
        print(f"  [3] Bootstrap max chars:      {bootstrap}")
        print(f"      (total injected workspace files; 60000 recommended)")
        print(f"  [4] Clear lastGood:           {last_good_str}")
        print(f"      (force MAX token on next call; run after key rotation)")
        print(f"  [q] Back")
        print()
        sub = input("  Choice: ").strip().lower()

        if sub == "1":
            val = input(f"  Idle minutes (current={idle_min}, 0=disable, Enter to skip): ").strip()
            if val:
                try:
                    n = int(val)
                    if n == 0:
                        config.setdefault("session", {}).pop("reset", None)
                    else:
                        set_nested(config, ["session", "reset", "idleMinutes"], n)
                    save_cfg()
                    print(f"  \u2705 idleMinutes set to {n} (restart gateway to apply)")
                except ValueError:
                    print("  \u274c Must be an integer.")

        elif sub == "2":
            val = input(f"  Soft threshold tokens (current={soft_thresh}, Enter to skip): ").strip()
            if val:
                try:
                    n = int(val)
                    set_nested(config, ["agents", "defaults", "compaction", "memoryFlush", "softThresholdTokens"], n)
                    save_cfg()
                    print(f"  \u2705 softThresholdTokens set to {n:,} inside compaction.memoryFlush (restart gateway to apply)")
                except ValueError:
                    print("  \u274c Must be an integer.")

        elif sub == "3":
            val = input(f"  Bootstrap max chars (current={bootstrap}, Enter to skip): ").strip()
            if val:
                try:
                    n = int(val)
                    set_nested(config, ["agents", "defaults", "bootstrapTotalMaxChars"], n)
                    save_cfg()
                    print(f"  \u2705 bootstrapTotalMaxChars set to {n:,} (restart gateway to apply)")
                except ValueError:
                    print("  \u274c Must be an integer.")

        elif sub == "4":
            confirm = input("  Clear lastGood for this bot? (y/N): ").strip().lower()
            if confirm == "y":
                try:
                    auth_data = _json.load(open(info["auth"]))
                    old = auth_data.get("lastGood", {})
                    auth_data["lastGood"] = {}
                    open(info["auth"], "w").write(_json.dumps(auth_data, indent=2))
                    print(f"  \u2705 Cleared lastGood (was: {old}). Restart gateway to apply.")
                except Exception as e:
                    print(f"  \u274c Could not write auth file: {e}")

        elif sub in ("q", ""):
            break

        else:
            print("  Unknown option.")


# ── Single-bot loop ───────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# Evolve Network Management
# ═══════════════════════════════════════════════════════════════════════════════

EVOLVE_SHARED_DIR = Path("/Users/Shared/evolve")
EVOLVE_ADMIN_SCRIPT = None  # discovered at runtime

def _find_evolve_admin():
    """Locate the evolve-admin CLI."""
    import shutil
    candidates = [
        "/usr/local/bin/evolve-admin",
        "/opt/homebrew/bin/evolve-admin",
        "/Users/Shared/evolve-venv/bin/evolve-admin",  # direct venv path
        str(Path.home() / ".local/bin/evolve-admin"),
        str(Path.home() / "bin/evolve-admin"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    found = shutil.which("evolve-admin")
    return found


def _evolve_network_path():
    """Return path to network.json."""
    candidates = [
        EVOLVE_SHARED_DIR / "network.json",
        Path("/Users/admin_bot/.openclaw/workspace/evolve/network.json"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return EVOLVE_SHARED_DIR / "network.json"


def _load_evolve_network():
    """Load Evolve network.json or return empty dict."""
    p = _evolve_network_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _load_evolve_proposals(status="pending"):
    """Load proposals from shared dir by status folder."""
    d = EVOLVE_SHARED_DIR / "proposals" / status
    if not d.exists():
        return []
    proposals = []
    for f in sorted(d.iterdir()):
        if f.suffix == ".json":
            try:
                proposals.append(json.loads(f.read_text()))
            except Exception:
                pass
    return proposals


def _load_forge_results():
    """Load forge-results keyed by proposal_id."""
    d = EVOLVE_SHARED_DIR / "proposals" / "forge-results"
    results = {}
    if not d.exists():
        return results
    for f in d.iterdir():
        if f.suffix == ".json":
            try:
                data = json.loads(f.read_text())
                results[data.get("proposal_id", f.stem)] = data
            except Exception:
                pass
    return results


def _evolve_run(args, network_flag=True):
    """Run evolve-admin CLI with optional --network flag."""
    ea = _find_evolve_admin()
    if not ea:
        print("  ❌ evolve-admin not found. Install: cd evolve/packages/admin && pip3 install -e .")
        return False
    cmd = [ea]
    if network_flag:
        net = _evolve_network_path()
        if net.exists():
            cmd += ["--network", str(net)]
    cmd += args
    proc = subprocess.run(cmd)
    return proc.returncode == 0


def _atomic_json_write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def _promote_proposal(proposal_id):
    """Move a forge-validated proposal from approved/ to deployed/."""
    approved_path = EVOLVE_SHARED_DIR / "proposals" / "approved" / f"{proposal_id}.json"
    deployed_dir = EVOLVE_SHARED_DIR / "proposals" / "deployed"
    if not approved_path.exists():
        print(f"  ❌ Not found in approved/: {proposal_id}")
        return False
    deployed_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(approved_path.read_text())
    data["status"] = "deployed"
    data["deployed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_json_write(deployed_dir / f"{proposal_id}.json", data)
    approved_path.unlink()
    print(f"  ✅ Promoted {proposal_id} → deployed/")
    return True


def _reject_proposal(proposal_id, reason="other", note=""):
    """Remove a proposal from approved/ or pending/ and log rejection."""
    for status in ("approved", "pending"):
        p = EVOLVE_SHARED_DIR / "proposals" / status / f"{proposal_id}.json"
        if p.exists():
            data = json.loads(p.read_text())
            p.unlink()
            # Log to rejections.jsonl
            rej_path = EVOLVE_SHARED_DIR / "feedback" / "rejections.jsonl"
            rej_path.parent.mkdir(parents=True, exist_ok=True)
            with rej_path.open("a") as f:
                f.write(json.dumps({
                    "proposal_id": proposal_id,
                    "pattern_key": data.get("pattern_key"),
                    "target_bot": data.get("target_bot"),
                    "type": data.get("type"),
                    "reason": reason,
                    "note": note,
                    "rejected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }) + "\n")
            print(f"  ✅ Rejected {proposal_id} (reason: {reason})")
            return True
    print(f"  ❌ Not found: {proposal_id}")
    return False


def show_evolve_status():
    """Print a quick network health table."""
    network = _load_evolve_network()
    if not network:
        print("  ⚠️  No network.json found. Run setup first.")
        return

    members = network.get("members", [])
    primary = network.get("primary", "?")
    pending = _load_evolve_proposals("pending")
    approved = _load_evolve_proposals("approved")
    forge_results = _load_forge_results()

    print()
    print(f"  Network: {network.get('networkId','?')}  Primary: {primary}")
    print(f"  Pending proposals: {len(pending)}  Awaiting Forge: {len(approved)}  Forge results: {len(forge_results)}")
    print()
    print(f"  {'Bot':<10} {'Role':<8} {'Last metric':<14} {'Score':<7} {'Maint%':<8}")
    print(f"  {'-'*10} {'-'*8} {'-'*14} {'-'*7} {'-'*8}")

    metrics_dir = EVOLVE_SHARED_DIR / "metrics"
    for bot_id in members:
        role = network.get("bots", {}).get(bot_id, {}).get("role", "member")
        last_date, score, maint = "-", "-", "-"
        if metrics_dir.exists():
            bot_files = sorted(
                [f for f in metrics_dir.iterdir() if f.name.startswith(f"{bot_id}-") and f.name.endswith(".json")],
                reverse=True,
            )
            if bot_files:
                last_date = bot_files[0].stem.replace(f"{bot_id}-", "")
                try:
                    m = json.loads(bot_files[0].read_text())
                    score = str(m.get("score", "-"))
                    mr = m.get("maintenance_ratio_7d") or m.get("maintenance_ratio")
                    maint = f"{mr*100:.0f}%" if mr is not None else "-"
                except Exception:
                    pass
        flag = " ← primary" if bot_id == primary else ""
        print(f"  {bot_id:<10} {role:<8} {last_date:<14} {score:<7} {maint:<8}{flag}")
    print()


def manage_evolve_proposals():
    """Interactive proposal review — pending and forge-validated."""
    while True:
        pending = _load_evolve_proposals("pending")
        approved = _load_evolve_proposals("approved")
        forge_results = _load_forge_results()

        # Which approved proposals have forge results?
        forge_ready = [p for p in approved if p.get("id") in forge_results]
        forge_waiting = [p for p in approved if p.get("id") not in forge_results]

        print()
        print("  ── Proposals ───────────────────────────────────────────────")
        if not pending and not forge_ready and not forge_waiting:
            print("  ✅ No proposals pending.")
        else:
            if pending:
                print(f"  📋 Pending ({len(pending)}) — awaiting your review:")
                for i, p in enumerate(pending, 1):
                    conf = f"{p.get('confidence',0)*100:.0f}%"
                    print(f"    [{i}] {p.get('target_bot','?')} · {p.get('type','?')} · {conf} · {p.get('problem','')[:60]}")
            if forge_ready:
                print()
                print(f"  🔬 Forge validated ({len(forge_ready)}) — ready to promote or reject:")
                for i, p in enumerate(forge_ready, 1):
                    pid = p.get("id","?")
                    r = forge_results.get(pid, {})
                    icon = "✅" if r.get("result") == "pass" else "⚠️ "
                    print(f"    [f{i}] {icon} {p.get('target_bot','?')} · {r.get('result','?')} → {r.get('recommendation','?')} · {p.get('problem','')[:50]}")
            if forge_waiting:
                print()
                print(f"  ⏳ Awaiting Forge ({len(forge_waiting)}):")
                for p in forge_waiting:
                    print(f"    · {p.get('target_bot','?')} · {p.get('id','?')[:24]}")

        print()
        print("  [1-N] Review pending proposal   [fN] Promote/reject Forge result")
        print("  [Enter] Back")
        choice = input("  Choice: ").strip().lower()

        if not choice:
            return

        # Forge-result action
        if choice.startswith("f") and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < len(forge_ready):
                _review_forge_result(forge_ready[idx], forge_results.get(forge_ready[idx].get("id",""), {}))
            else:
                print("  ❌ Invalid selection")
            continue

        # Pending proposal review
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(pending):
                _review_pending_proposal(pending[idx])
            else:
                print("  ❌ Invalid selection")
            continue

        print("  ❌ Unknown choice")


def _review_pending_proposal(proposal):
    """Show full detail for a pending proposal and act on it."""
    p = proposal
    print()
    print(f"  ── {p.get('id','?')} ─────────────────────────────────────────")
    print(f"  Bot:        {p.get('target_bot','?')}")
    print(f"  Type:       {p.get('type','?')}   Risk: {p.get('risk','?')}   Confidence: {p.get('confidence',0)*100:.0f}%")
    print(f"  Problem:    {p.get('problem','')}")
    print(f"  Root cause: {p.get('root_cause','')}")
    if p.get("proposed_change",{}).get("description"):
        print(f"  Proposed:   {p['proposed_change']['description']}")
    elif p.get("proposed_change"):
        pc = p["proposed_change"]
        print(f"  Proposed:   Set {pc.get('path','')} = {pc.get('to','?')} (was {pc.get('from','?')})")
    if p.get("minimum_test"):
        print(f"  Min test:   {p.get('minimum_test')}")
    print()
    print("  [a] Approve → Forge   [r] Reject   [Enter] Back")
    choice = input("  Choice: ").strip().lower()

    if choice == "a":
        # Move pending → approved
        pid = p.get("id","")
        src = EVOLVE_SHARED_DIR / "proposals" / "pending" / f"{pid}.json"
        dst_dir = EVOLVE_SHARED_DIR / "proposals" / "approved"
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            data = json.loads(src.read_text())
            data["status"] = "approved"
            data["approved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _atomic_json_write(dst_dir / f"{pid}.json", data)
            src.unlink()
            print(f"  ✅ Approved. Forge watcher will pick it up within 5 minutes.")
        else:
            print("  ❌ File not found")

    elif choice == "r":
        _reject_with_reason(p.get("id",""))


def _review_forge_result(proposal, result):
    """Show Forge validation result and offer promote/reject."""
    p = proposal
    r = result
    print()
    print(f"  ── Forge Result: {p.get('id','?')} ──────────────────────────")
    print(f"  Bot:            {p.get('target_bot','?')}")
    print(f"  Problem:        {p.get('problem','')}")
    print()
    icon = {"pass":"✅","fail":"❌","needs-human":"👤","error":"🔴"}.get(r.get("result",""), "❓")
    print(f"  Forge result:   {icon} {r.get('result','?').upper()}")
    print(f"  Recommendation: {r.get('recommendation','?')}")
    if r.get("forge_notes"):
        print(f"  Notes:          {r.get('forge_notes','')}")
    print()

    # Tests summary
    tests = r.get("tests_run", [])
    if tests:
        passed = sum(1 for t in tests if t.get("passed"))
        print(f"  Tests: {passed}/{len(tests)} passed")
        failed = [t for t in tests if not t.get("passed")]
        for t in failed:
            print(f"    ✗ {t.get('name','')} — {t.get('detail','')}")

    print()
    rec = r.get("recommendation","")
    if rec == "promote":
        print("  [p] Promote to deployed   [r] Reject anyway   [Enter] Back")
    else:
        print("  [p] Promote anyway   [r] Reject   [Enter] Back")
    choice = input("  Choice: ").strip().lower()

    if choice == "p":
        _promote_proposal(p.get("id",""))
    elif choice == "r":
        _reject_with_reason(p.get("id",""))


def _reject_with_reason(proposal_id):
    """Prompt for rejection reason then reject."""
    print()
    print("  Rejection reason:")
    reasons = [
        ("1", "too-aggressive",    "Too aggressive — change larger than needed"),
        ("2", "wrong-layer",       "Wrong layer — treating symptom not root cause"),
        ("3", "already-fixed",     "Already fixed manually"),
        ("4", "low-priority",      "Low priority right now"),
        ("5", "wrong-root-cause",  "Wrong root cause — diagnosis incorrect"),
        ("6", "other",             "Other"),
    ]
    for key, _, label in reasons:
        print(f"    [{key}] {label}")
    choice = input("  Reason [1-6]: ").strip()
    reason_map = {k: v for k, v, _ in reasons}
    reason = reason_map.get(choice, "other")
    note = input("  Optional note: ").strip()
    _reject_proposal(proposal_id, reason=reason, note=note)


def manage_evolve_deploy():
    """Deploy or update Evolve on a bot."""
    ea = _find_evolve_admin()
    if not ea:
        print("  ❌ evolve-admin not installed.")
        print("  Install: cd /path/to/evolve/packages/admin && sudo pip3 install -e .")
        return

    network = _load_evolve_network()
    members = network.get("members", [])

    print()
    print(f"  Registered bots: {', '.join(members) if members else '(none)'}")
    print(f"  All known bots:  {', '.join(BOTS)}")
    print()
    print("  [a] Deploy to all   [b] Deploy specific bot   [f] Setup Forge   [s] Setup shared dir")
    print("  [Enter] Back")
    choice = input("  Choice: ").strip().lower()

    if choice == "a":
        dry = input("  Dry run? (y/N): ").strip().lower() == "y"
        args = ["deploy", "--all"]
        if dry:
            args.append("--dry-run")
        _evolve_run(args)

    elif choice == "b":
        bot_id = input("  Bot ID: ").strip().lower()
        role = input("  Role [member/primary]: ").strip().lower() or "member"
        dry = input("  Dry run? (y/N): ").strip().lower() == "y"
        args = ["deploy", "--bot", bot_id, "--role", role]
        if dry:
            args.append("--dry-run")
        _evolve_run(args)

    elif choice == "f":
        dry = input("  Dry run? (y/N): ").strip().lower() == "y"
        args = ["setup-forge"]
        if dry:
            args.append("--dry-run")
        _evolve_run(args)

    elif choice == "s":
        _evolve_run(["setup-shared"])


def manage_evolve():
    """Main Evolve management menu."""
    while True:
        network = _load_evolve_network()
        net_id = network.get("networkId", "(not configured)")
        pending_count = len(_load_evolve_proposals("pending"))
        approved_count = len(_load_evolve_proposals("approved"))
        forge_result_count = len(_load_forge_results())
        forge_ready = sum(
            1 for p in _load_evolve_proposals("approved")
            if p.get("id") in _load_forge_results()
        )

        print()
        print("=" * 62)
        print(f"  ⚡ Evolve Network — {net_id}")
        print("=" * 62)
        print(f"  Pending: {pending_count}  |  Awaiting Forge: {approved_count}  |  Forge ready: {forge_ready}")
        print()
        print("  [s] Status          [p] Proposals (review/approve/reject)")
        print("  [d] Deploy/update   [A] Start admin UI (127.0.0.1:19099)")
        print("  [c] Config          [w] Setup wizard")
        print("  [C] Applications    [$] Cost report")
        print("  [k] Keys            [m] Model tiers")
        print("  [Enter] Back")
        print()
        choice = input("  Choice: ").strip().lower()

        if not choice:
            return

        elif choice == "s":
            show_evolve_status()

        elif choice == "p":
            manage_evolve_proposals()

        elif choice == "d":
            manage_evolve_deploy()

        elif choice == "a":
            print()
            ea = _find_evolve_admin()
            if not ea:
                print("  ❌ evolve-admin not found")
            else:
                print("  Starting admin UI at http://127.0.0.1:19099/ ...")
                print("  Ctrl+C to stop")
                try:
                    subprocess.run([ea, "serve", "--open"])
                except KeyboardInterrupt:
                    print("\n  Stopped.")

        elif choice == "w":
            ea = _find_evolve_admin()
            if not ea:
                print("  ❌ evolve-admin not found")
            else:
                subprocess.run([ea, "setup"])

        elif choice == "C":
            ea = _find_evolve_admin()
            if not ea:
                print("  ❌ evolve-admin not found")
                continue
            print()
            print(f"  Bot: {bot}")
            print("  [s] Scan workspace   [l] List manifests   [n] New application")
            sub = input("  Choice: ").strip().lower()
            net = str(_evolve_network_path())
            if sub == "s":
                subprocess.run([ea, "--network", net, "application", "scan", bot])
            elif sub == "l":
                subprocess.run([ea, "--network", net, "application", "list", bot])
            elif sub == "n":
                subprocess.run([ea, "--network", net, "application", "new", bot])

        elif choice == "$":
            import sys
            net = str(_evolve_network_path())
            cost_script = "/Users/Shared/evolve-repo/packages/analyzer/cost.py"
            subprocess.run([sys.executable, cost_script, "--network", net])

        elif choice == "k":
            ea = _find_evolve_admin()
            if not ea:
                print("  \u2717 evolve-admin not found")
                continue
            net = str(_evolve_network_path())
            print()
            print("  [l] List keys    [a] Add key    [s] Sync to all bots")
            print("  [r] Rotate key   [Enter] Back")
            sub = input("  Choice: ").strip().lower()
            if sub == "l":
                subprocess.run([ea, "--network", net, "keys", "list"])
            elif sub == "a":
                name = input("  Key name: ").strip()
                provider = input("  Provider: ").strip()
                subprocess.run([ea, "--network", net, "keys", "add", name, "--provider", provider])
            elif sub == "s":
                subprocess.run([ea, "--network", net, "keys", "sync", "--all"])
            elif sub == "r":
                name = input("  Key name to rotate: ").strip()
                subprocess.run([ea, "--network", net, "keys", "rotate", name])

        elif choice == "m":
            ea = _find_evolve_admin()
            if not ea:
                print("  \u2717 evolve-admin not found")
                continue
            net = str(_evolve_network_path())
            print()
            print("  [l] List tiers   [s] Show tier detail   [e] Edit tier")
            print("  [u] Usage today  [Enter] Back")
            sub = input("  Choice: ").strip().lower()
            if sub == "l":
                subprocess.run([ea, "--network", net, "models", "list"])
            elif sub == "s":
                tier = input("  Tier (tier0/tier1/tier2/tier3): ").strip()
                subprocess.run([ea, "--network", net, "models", "show", tier])
            elif sub == "e":
                tier = input("  Tier to edit: ").strip()
                model = input("  New primary model: ").strip()
                fb = input("  Fallbacks (comma-separated, or Enter to skip): ").strip()
                cmd = [ea, "--network", net, "models", "set", tier, model]
                for f in (fb.split(",") if fb else []):
                    cmd += ["--fallback", f.strip()]
                subprocess.run(cmd)
            elif sub == "u":
                subprocess.run([ea, "--network", net, "models", "usage"])

        elif choice == "c":
            net_path = _evolve_network_path()
            if net_path.exists():
                print()
                print(f"  Config: {net_path}")
                print(json.dumps(_load_evolve_network(), indent=4))
                print()
                edit = input("  Edit with $EDITOR? (y/N): ").strip().lower()
                if edit == "y":
                    editor = os.environ.get("EDITOR", "nano")
                    subprocess.run([editor, str(net_path)])
            else:
                print(f"  ❌ No network.json at {net_path}")
                init = input("  Initialize with defaults? (y/N): ").strip().lower()
                if init == "y":
                    _evolve_run(["config", "show"])

        else:
            print("  ❌ Unknown choice")


def single_bot_loop(bot):
    info, config, auth = load_bot(bot)
    user        = info["user"]
    config_path = info["config"]
    auth_path   = info["auth"]

    def save_cfg():
        save_json(config, config_path, user)
        print(f"  💾 Saved: {config_path}")

    def save_ath():
        save_auth(auth, auth_path, user)
        print(f"  💾 Saved: {auth_path}")

    while True:
        model_config = get_model_config(config)
        catalog      = get_catalog(config)
        profiles     = auth.get("profiles", {})

        print()
        print(f"━━━ {bot.upper()} ({user}) ━━━")
        all_models = display_models(model_config, catalog)

        print()
        print("  [a] Add model   [r] Remove model   [o] Set order")
        print("  [k] Rotate keys [g] Restart gateway")
        print("  [i] Info        [l] Logs            [x] Processes")
        print("  [p] Plugins     [d] Doctor          [b] Switch bot")
        print("  [w] Google Workspace (token status + reauth)")
        print("  [c] Calendars   (secret iCal/feed URLs)")
        print("  [u] Usage reports          [v] Version & upgrade")
        print("  [x] Exec approvals         [$] Cost settings")
        print("  [e] Evolve network")
        print("  [h] Help")
        print("  [q] Quit")
        print()
        choice = input("  Choice: ").strip().lower()

        # Handle "b <botname>" inline switch shortcut
        if choice.startswith("b "):
            target = choice[2:].strip()
            if target in BOTS:
                single_bot_loop(target)
                return
            else:
                print(f"  \u274c Unknown bot: {target}")
                continue

        if choice == "i":
            show_info(bot, info, config, auth)

        elif choice == "l":
            lines_str = input("  Lines? (default 30): ").strip()
            n = int(lines_str) if lines_str.isdigit() else 30
            show_logs(bot, info, n)

        elif choice == "x":
            procs = show_process(bot, info)
            if procs:
                pids = [l.split()[1] for l in procs]
                kill = input(f"\n  Kill process(es)? (y/N): ").strip().lower()
                if kill == "y":
                    for pid in pids:
                        subprocess.run(["kill", "-9", pid])
                    print(f"  ✅ Killed {', '.join(pids)} — launchd will restart")

        elif choice == "p":
            print()
            plugins_cfg = config.get("plugins", {})
            entries = plugins_cfg.get("entries", {})
            allow = plugins_cfg.get("allow", [])
            deny = plugins_cfg.get("deny", [])

            print(f"  ── {bot.upper()} Plugins ──────────────────────────")
            print(f"  allow list: {allow if allow else '(empty — all allowed)'}")
            print(f"  deny list:  {deny if deny else '(empty)'}")
            print()
            if entries:
                print("  Plugin entries:")
                for pname, pdata in entries.items():
                    enabled = pdata.get("enabled", "?") if isinstance(pdata, dict) else "?"
                    status = "✅" if enabled is True else "❌" if enabled is False else "?"
                    print(f"    {status} {pname}")
            else:
                print("  No plugin entries configured.")

            print()
            print("  Options:")
            print("    [1] Edit allow list")
            print("    [2] Edit deny list")
            print("    [3] Toggle plugin enabled/disabled")
            print("    [Enter] Back")
            print()
            sub = input("  Choice: ").strip()

            if sub == "1":
                print(f"  Current allow: {allow}")
                print("  ⚠️  WARNING: Setting a non-empty allow list will EXCLUDE all other plugins!")
                print("  Known plugins: slack, telegram, brave, unity, gmail-watcher")
                print("  Enter comma-separated plugin names (empty = allow all, removes allow key):")
                raw = input("  allow: ").strip()
                new_allow = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
                # Warn about unrecognized plugin names
                known_plugins = {"slack", "telegram", "brave", "unity", "gmail-watcher"}
                unknown = [x for x in new_allow if x not in known_plugins]
                if unknown:
                    print(f"  ⚠️  Unrecognized plugin name(s): {unknown}")
                    confirm = input("  Continue anyway? (y/N): ").strip().lower()
                    if confirm != "y":
                        print("  Cancelled.")
                        continue
                # Check for conflict with deny
                conflicts = [x for x in new_allow if x in deny]
                if conflicts:
                    print(f"  ⚠️  Conflict: {conflicts} are in deny list. Removing from deny.")
                    deny = [x for x in deny if x not in conflicts]
                    plugins_cfg["deny"] = deny
                # Remove the allow key entirely when empty (OpenClaw rejects allow: [])
                if new_allow:
                    plugins_cfg["allow"] = new_allow
                else:
                    plugins_cfg.pop("allow", None)
                config["plugins"] = plugins_cfg
                save_cfg()
                print(f"  ✅ allow = {new_allow if new_allow else '(key removed — all plugins allowed)'}")

            elif sub == "2":
                print(f"  Current deny: {deny}")
                print("  Enter comma-separated plugin names (empty = deny none):")
                raw = input("  deny: ").strip()
                new_deny = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
                conflicts = [x for x in new_deny if x in allow]
                if conflicts:
                    print(f"  ⚠️  Conflict: {conflicts} are in allow list. Removing from allow.")
                    allow = [x for x in allow if x not in conflicts]
                    if allow:
                        plugins_cfg["allow"] = allow
                    else:
                        plugins_cfg.pop("allow", None)
                if new_deny:
                    plugins_cfg["deny"] = new_deny
                else:
                    plugins_cfg.pop("deny", None)
                config["plugins"] = plugins_cfg
                save_cfg()
                print(f"  ✅ deny = {new_deny if new_deny else '(key removed — nothing denied)'}")

            elif sub == "3":
                if not entries:
                    print("  No plugin entries to toggle.")
                else:
                    plist = list(entries.keys())
                    for i, pname in enumerate(plist, 1):
                        enabled = entries[pname].get("enabled", "?") if isinstance(entries[pname], dict) else "?"
                        status = "✅" if enabled is True else "❌"
                        print(f"    {i}. {status} {pname}")
                    num = input("  Toggle number: ").strip()
                    try:
                        idx = int(num) - 1
                        pname = plist[idx]
                        current = entries[pname].get("enabled", True) if isinstance(entries[pname], dict) else True
                        entries[pname]["enabled"] = not current
                        plugins_cfg["entries"] = entries
                        config["plugins"] = plugins_cfg
                        save_cfg()
                        new_status = "✅ enabled" if not current else "❌ disabled"
                        print(f"  ✅ {pname} → {new_status}")
                    except (ValueError, IndexError):
                        print("  Invalid number.")

        elif choice == "d":
            print()
            print(f"  Running doctor for {bot.upper()} ({user})...")
            print()
            subprocess.run(
                ["sudo", "-u", user, "bash", "-c", f"cd /Users/{user} && /opt/homebrew/bin/openclaw doctor"],
            )
            print()

        elif choice == "a":
            print()
            new_model = input("  Model name (e.g. anthropic/claude-sonnet-4-6): ").strip()
            if not new_model:
                continue
            alias = input("  Alias (Enter to skip): ").strip()
            catalog[new_model] = {"alias": alias} if alias else {}
            set_catalog(config, catalog)
            save_cfg()

        elif choice == "r":
            print()
            num = input("  Number to remove: ").strip()
            try:
                idx   = int(num) - 1
                model = all_models[idx]
                primary   = model_config.get("primary", "")
                fallbacks = model_config.get("fallbacks", [])
                if model == primary or model in fallbacks:
                    print(f"  ⚠️  {model} is active. Update order [o] first.")
                    continue
                catalog.pop(model, None)
                set_catalog(config, catalog)
                save_cfg()
                print(f"  ✅ Removed {model}")
            except (ValueError, IndexError):
                print("  Invalid number.")

        elif choice == "o":
            print()
            print("  Numbers space-separated. First = primary, rest = fallbacks.")
            raw = input("  Order: ").strip()
            try:
                nums     = [int(x) for x in raw.split()]
                selected = [all_models[n - 1] for n in nums]
                model_config["primary"]   = selected[0]
                model_config["fallbacks"] = selected[1:]
                set_model_config(config, model_config)
                for m in selected:
                    if m not in catalog:
                        catalog[m] = {}
                set_catalog(config, catalog)
                save_cfg()
                print(f"  ✅ Primary: {selected[0]}")
                for i, f in enumerate(selected[1:], 1):
                    print(f"  ✅ Fallback {i}: {f}")
            except (ValueError, IndexError) as e:
                print(f"  Invalid: {e}")

        elif choice == "k":
            print()
            print(f"  Rotating keys for {bot.upper()}. Enter to keep current.")
            auth_changed = False

            # ── AI model keys (auth-profiles.json) ──
            for provider, mode, description in build_provider_list(catalog, profiles):
                name, profile, field = find_profile(profiles, provider, mode)
                current = profile.get(field, "") if profile else ""
                print(f"\n  {description}")
                print(f"    Current: {mask(current) if profile else '(not configured)'}")
                new_key = input("    New key (Enter to skip): ").strip()
                if new_key:
                    if profile is None:
                        profile_name = f"{provider}:default"
                        if profile_name in profiles:
                            profile_name = f"{provider}:api"
                        profiles[profile_name] = {"provider": provider, "type": mode}
                        profile = profiles[profile_name]
                        field = "token" if mode == "token" else "key"
                    profile[field] = new_key
                    auth_changed = True
                    print("    ✅ Updated")
            if auth_changed:
                save_ath()

            # ── Brave search key (openclaw.json) ──
            print(f"\n  Brave Search API Key")
            canonical_key = (config.get("plugins", {}).get("entries", {})
                             .get("brave", {}).get("config", {})
                             .get("webSearch", {}).get("apiKey", ""))
            legacy_key    = (config.get("tools", {}).get("web", {})
                             .get("search", {}).get("apiKey", ""))

            if canonical_key:
                print(f"    Current (canonical): {mask(canonical_key)}")
            elif legacy_key:
                print(f"    Current (⚠️  legacy path): {mask(legacy_key)}")
                print(f"    ⚠️  Key is at tools.web.search.apiKey (legacy).")
                print(f"       Canonical: plugins.entries.brave.config.webSearch.apiKey")
                migrate = input("    Migrate to canonical path now? (Y/n): ").strip().lower()
                if migrate != "n":
                    (config.setdefault("plugins", {})
                           .setdefault("entries", {})
                           .setdefault("brave", {})
                           .setdefault("config", {})
                           .setdefault("webSearch", {}))["apiKey"] = legacy_key
                    search_cfg = config.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})
                    search_cfg.pop("apiKey", None)
                    search_cfg["provider"] = "brave"
                    save_cfg()
                    canonical_key = legacy_key
                    print("    ✅ Migrated to canonical path")
            else:
                print(f"    Current: (not configured)")

            new_key = input("    New Brave key (Enter to skip): ").strip()
            if new_key:
                (config.setdefault("plugins", {})
                       .setdefault("entries", {})
                       .setdefault("brave", {})
                       .setdefault("config", {})
                       .setdefault("webSearch", {}))["apiKey"] = new_key
                search_cfg = config.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})
                search_cfg.pop("apiKey", None)
                search_cfg["provider"] = "brave"
                save_cfg()
                print("    ✅ Brave key updated")

            # ── Channel tokens (openclaw.json channels section) ──
            channels_cfg = config.get("channels", {})

            # Telegram bot token (show regardless of enabled status)
            tg = channels_cfg.get("telegram", {})
            if isinstance(tg, dict):
                current = tg.get("botToken", "")
                enabled_str = "" if tg.get("enabled") else "  [disabled]"
                print(f"\n  Telegram Bot Token{enabled_str}")
                print(f"    Current: {mask(current) if current else '(not configured)'}")
                new_key = input("    New token (Enter to skip): ").strip()
                if new_key:
                    tg["botToken"] = new_key
                    config.setdefault("channels", {})["telegram"] = tg
                    save_cfg()
                    print("    ✅ Telegram token updated (restart gateway to apply)")

            # Slack tokens (show regardless of enabled status)
            slack = channels_cfg.get("slack", {})
            if isinstance(slack, dict):
                enabled_str = "" if slack.get("enabled") else "  [disabled]"
                for field, label in [("botToken", "Slack Bot Token (xoxb-...)"),
                                      ("appToken", "Slack App Token (xapp-...)")]:
                    current = slack.get(field, "")
                    print(f"\n  {label}{enabled_str}")
                    print(f"    Current: {mask(current) if current else '(not configured)'}")
                    new_key = input("    New token (Enter to skip): ").strip()
                    if new_key:
                        slack[field] = new_key
                        config.setdefault("channels", {})["slack"] = slack
                        save_cfg()
                        print(f"    ✅ {label.split()[1]} updated (restart gateway to apply)")

            # Discord token (show regardless of enabled status)
            discord = channels_cfg.get("discord", {})
            if isinstance(discord, dict):
                enabled_str = "" if discord.get("enabled") else "  [disabled]"
                current = discord.get("token", "")
                print(f"\n  Discord Bot Token{enabled_str}")
                print(f"    Current: {mask(current) if current else '(not configured)'}")
                new_key = input("    New token (Enter to skip): ").strip()
                if new_key:
                    discord["token"] = new_key
                    config.setdefault("channels", {})["discord"] = discord
                    save_cfg()
                    print("    ✅ Discord token updated (restart gateway to apply)")

            if not auth_changed:
                print("\n  Done.")

        elif choice == "w":
            show_gws_status(bot, info)
            print()
            sub = input("  [r] Reauth  [Enter] Back: ").strip().lower()
            if sub == "r":
                run_gws_reauth(bot, info)

        elif choice == "c":
            manage_calendars(bot, info, auth, lambda: None)

        elif choice == "u":
            _usage_menu(bot=bot)

        elif choice == "v":
            version_menu()

        elif choice == "x":
            manage_exec_approvals(bot, info)

        elif choice == "$":
            manage_cost_settings(bot, info, config, lambda: save_json(config, info["config"], info["user"]))

        elif choice == "e":
            manage_evolve()

        elif choice == "h":
            show_help()

        elif choice == "g":
            confirm = input(f"\n  Restart {bot} gateway? (y/N): ").strip().lower()
            if confirm == "y":
                restart_gateway(bot)

        elif choice == "b":
            print()
            print(f"  Bots: {', '.join(BOTS)} | all")
            new_bot = input("  Switch to: ").strip().lower()
            if new_bot == "all":
                all_bots_loop()
                return
            elif new_bot in BOTS:
                single_bot_loop(new_bot)
                return
            else:
                print(f"  ❌ Unknown: {new_bot}")

        elif choice == "q":
            print("Bye.")
            break

        else:
            print("  Unknown option.")


# ── All-bots loop ─────────────────────────────────────────────────────────────

def all_bots_loop():
    print()
    print("━━━ ALL BOTS ━━━")
    bots = load_all_bots()

    while True:
        print()
        print("  [a] Add model to all       [r] Remove model from all")
        print("  [o] Set order for all      [k] Rotate key for all")
        print("  [g] Restart all gateways")
        print("  [i] Info for all           [l] Logs for all")
        print("  [x] Processes for all      [w] Google Workspace status (all)")
        print("  [u] Usage reports (all)    [v] Version & upgrade")
        print("  [h] Help                   [b] Switch to single bot")
        print("  [q] Quit")
        print()
        choice = input("  Choice: ").strip().lower()

        # Handle "b <botname>" inline switch shortcut
        if choice.startswith("b "):
            target = choice[2:].strip()
            if target in BOTS:
                single_bot_loop(target)
                return
            else:
                print(f"  \u274c Unknown bot: {target}")
                continue

        if choice == "i":
            for bot, (info, config, auth) in bots.items():
                show_info(bot, info, config, auth)

        elif choice == "l":
            lines_str = input("  Lines per bot? (default 10): ").strip()
            n = int(lines_str) if lines_str.isdigit() else 10
            for bot, (info, config, auth) in bots.items():
                show_logs(bot, info, n)

        elif choice == "x":
            all_procs = {}
            for bot, (info, config, auth) in bots.items():
                procs = show_process(bot, info)
                if procs:
                    all_procs[bot] = procs
            if all_procs:
                print()
                kill = input("  Kill a gateway? Enter bot name or Enter to skip: ").strip().lower()
                if kill in all_procs:
                    pids = [l.split()[1] for l in all_procs[kill]]
                    for pid in pids:
                        subprocess.run(["kill", "-9", pid])
                    print(f"  ✅ Killed {kill} ({', '.join(pids)}) — launchd will restart")

        elif choice == "a":
            print()
            new_model = input("  Model name (e.g. anthropic/claude-sonnet-4-6): ").strip()
            if not new_model:
                continue
            alias = input("  Alias (Enter to skip): ").strip()
            for bot, (info, config, auth) in bots.items():
                catalog = get_catalog(config)
                catalog[new_model] = {"alias": alias} if alias else {}
                set_catalog(config, catalog)
                save_json(config, info["config"], info["user"])
                print(f"  ✅ {bot}: added {new_model}")

        elif choice == "r":
            print()
            model = input("  Model name to remove: ").strip()
            if not model:
                continue
            for bot, (info, config, auth) in bots.items():
                catalog      = get_catalog(config)
                model_config = get_model_config(config)
                primary      = model_config.get("primary", "")
                fallbacks    = model_config.get("fallbacks", [])
                if model == primary or model in fallbacks:
                    print(f"  ⚠️  {bot}: {model} is active — skipping. Update order first.")
                    continue
                if model in catalog:
                    catalog.pop(model)
                    set_catalog(config, catalog)
                    save_json(config, info["config"], info["user"])
                    print(f"  ✅ {bot}: removed {model}")
                else:
                    print(f"  ─  {bot}: not in catalog, skipping")

        elif choice == "o":
            print()
            # Build unified model list: union of all bots' catalogs
            unified = {}  # model -> alias (prefer first alias found)
            for bot, (info, config, auth) in bots.items():
                catalog = get_catalog(config)
                mc = get_model_config(config)
                # Include active models even if not in catalog
                active = [mc.get("primary", "")] + mc.get("fallbacks", [])
                for m in active:
                    if m and m not in unified:
                        unified[m] = catalog.get(m, {}).get("alias", "")
                for m, meta in catalog.items():
                    if m not in unified:
                        unified[m] = meta.get("alias", "")
            all_unified = list(unified.keys())

            print("  Available models (union of all bot catalogs):")
            for i, m in enumerate(all_unified, 1):
                alias = unified[m]
                alias_str = f"  [{alias}]" if alias else ""
                print(f"    {i:2}. {m}{alias_str}")
            print()
            print("  Enter numbers space-separated. First = primary, rest = fallbacks.")
            raw = input("  Order: ").strip()
            if not raw:
                continue
            try:
                nums     = [int(x) for x in raw.split()]
                selected = [all_unified[n - 1] for n in nums]
            except (ValueError, IndexError) as e:
                print(f"  Invalid: {e}")
                continue

            for bot, (info, config, auth) in bots.items():
                catalog      = get_catalog(config)
                model_config = get_model_config(config)
                model_config["primary"]   = selected[0]
                model_config["fallbacks"] = selected[1:]
                set_model_config(config, model_config)
                added = []
                for m in selected:
                    if m not in catalog:
                        catalog[m] = {}
                        added.append(m)
                set_catalog(config, catalog)
                save_json(config, info["config"], info["user"])
                added_str = f"  (auto-added: {', '.join(added)})" if added else ""
                print(f"  ✅ {bot}: primary={selected[0]}, fallbacks={selected[1:]}{added_str}")

        elif choice == "k":
            print()
            print("  Rotating keys for ALL bots. Enter to keep current.")
            # Build unified provider list across all bots
            all_catalogs = {}
            all_profiles = {}
            for b, (i, c, a) in bots.items():
                all_catalogs.update(get_catalog(c))
                all_profiles.update(a.get("profiles", {}))
            new_ai_keys = {}
            for provider, mode, description in build_provider_list(all_catalogs, all_profiles):
                print(f"\n  {description}")
                new_key = input("    New key (Enter to skip): ").strip()
                if new_key:
                    new_ai_keys[(provider, mode)] = new_key

            print(f"\n  Brave Search API Key")
            new_brave_key = input("    New key (Enter to skip): ").strip()

            print(f"\n  Telegram Bot Token (leave blank to skip)")
            new_tg_token = input("    New token (Enter to skip): ").strip()

            print(f"\n  Slack Bot Token (xoxb-...) (leave blank to skip)")
            new_slack_bot = input("    New token (Enter to skip): ").strip()

            print(f"\n  Slack App Token (xapp-...) (leave blank to skip)")
            new_slack_app = input("    New token (Enter to skip): ").strip()

            print(f"\n  Discord Bot Token (leave blank to skip)")
            new_discord_token = input("    New token (Enter to skip): ").strip()

            if not new_ai_keys and not new_brave_key and not new_tg_token and not new_slack_bot and not new_slack_app and not new_discord_token:
                print("  No keys entered.")
                continue

            for bot, (info, config, auth) in bots.items():
                # AI model keys
                if new_ai_keys:
                    profiles = auth.get("profiles", {})
                    auth_changed = False
                    for (provider, mode), new_key in new_ai_keys.items():
                        field = "token" if mode == "token" else "key"
                        name, profile, fld = find_profile(profiles, provider, mode)
                        if profile is not None:
                            profile[fld] = new_key
                            auth_changed = True
                            print(f"  ✅ {bot}: updated {provider}/{mode}")
                        else:
                            profile_name = f"{provider}:default"
                            if profile_name in profiles:
                                profile_name = f"{provider}:api"
                            profiles[profile_name] = {"provider": provider, "type": mode, field: new_key}
                            auth_changed = True
                            print(f"  ✅ {bot}: created {provider}/{mode} profile")
                    if auth_changed:
                        save_auth(auth, info["auth"], info["user"])

                # Brave key — canonical path, auto-migrate legacy
                if new_brave_key:
                    legacy_key = (config.get("tools", {}).get("web", {})
                                  .get("search", {}).get("apiKey", ""))
                    if legacy_key:
                        print(f"  ⚠️  {bot}: migrating Brave key from legacy path")
                        search_cfg = config.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})
                        search_cfg.pop("apiKey", None)
                        search_cfg["provider"] = "brave"
                    (config.setdefault("plugins", {})
                           .setdefault("entries", {})
                           .setdefault("brave", {})
                           .setdefault("config", {})
                           .setdefault("webSearch", {}))["apiKey"] = new_brave_key
                    search_cfg = config.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})
                    search_cfg.pop("apiKey", None)
                    search_cfg["provider"] = "brave"
                    save_json(config, info["config"], info["user"])
                    print(f"  ✅ {bot}: Brave key updated (canonical path)")

                # Telegram token
                if new_tg_token:
                    tg = config.get("channels", {}).get("telegram", {})
                    if isinstance(tg, dict) and tg.get("enabled"):
                        tg["botToken"] = new_tg_token
                        config.setdefault("channels", {})["telegram"] = tg
                        save_json(config, info["config"], info["user"])
                        print(f"  ✅ {bot}: Telegram token updated")
                    else:
                        print(f"  ─  {bot}: Telegram not enabled, skipping")

                # Slack tokens
                if new_slack_bot or new_slack_app:
                    slack = config.get("channels", {}).get("slack", {})
                    if isinstance(slack, dict) and slack.get("enabled"):
                        if new_slack_bot:
                            slack["botToken"] = new_slack_bot
                        if new_slack_app:
                            slack["appToken"] = new_slack_app
                        config.setdefault("channels", {})["slack"] = slack
                        save_json(config, info["config"], info["user"])
                        print(f"  ✅ {bot}: Slack token(s) updated")
                    else:
                        print(f"  ─  {bot}: Slack not enabled, skipping")

                # Discord token (field: channels.discord.token)
                if new_discord_token:
                    discord = config.get("channels", {}).get("discord", {})
                    if isinstance(discord, dict) and discord.get("enabled"):
                        discord["token"] = new_discord_token
                        config.setdefault("channels", {})["discord"] = discord
                        save_json(config, info["config"], info["user"])
                        print(f"  ✅ {bot}: Discord token updated")
                    else:
                        print(f"  ─  {bot}: Discord not enabled, skipping")

        elif choice == "w":
            for bot, (info, config, auth) in bots.items():
                if bot in GWS_ACCOUNTS:
                    show_gws_status(bot, info)
            print()
            sub = input("  [r] Reauth a bot  [Enter] Back: ").strip().lower()
            if sub == "r":
                gws_bots = [b for b in bots if b in GWS_ACCOUNTS]
                print(f"  Bots with Google accounts: {', '.join(gws_bots)}")
                target = input("  Which bot? ").strip().lower()
                if target in bots and target in GWS_ACCOUNTS:
                    run_gws_reauth(target, bots[target][0])
                else:
                    print(f"  ❌ Unknown or no Google account: {target}")

        elif choice == "u":
            _usage_menu(bot="all")

        elif choice == "v":
            version_menu()

        elif choice == "h":
            show_help()

        elif choice == "g":
            confirm = input("\n  Restart ALL gateways? (y/N): ").strip().lower()
            if confirm == "y":
                for bot in bots:
                    restart_gateway(bot)

        elif choice == "b":
            print()
            print(f"  Bots: {', '.join(BOTS)}")
            new_bot = input("  Switch to: ").strip().lower()
            if new_bot in BOTS:
                single_bot_loop(new_bot)
                return
            else:
                print(f"  ❌ Unknown: {new_bot}")

        elif choice == "q":
            print("Bye.")
            break

        else:
            print("  Unknown option.")


# ── Main ──────────────────────────────────────────────────────────────────────

# ── JSON non-interactive mode ──────────────────────────────────────────────────

def json_status(bot_filter):
    bots_to_check = [bot_filter] if bot_filter != "all" else list(BOTS.keys())
    result = {}
    for bot in bots_to_check:
        if bot not in BOTS:
            result[bot] = {"error": f"unknown bot: {bot}"}
            continue
        info = BOTS[bot]
        config_readable = False
        model_primary = None
        model_catalog = []
        providers_configured = []
        try:
            _, config, auth = load_bot(bot)
            config_readable = True
            mc = get_model_config(config)
            model_primary = mc.get("primary")
            catalog_dict = get_catalog(config)
            model_catalog = list(catalog_dict.keys()) if isinstance(catalog_dict, dict) else []
            providers = set()
            for model in model_catalog:
                if "/" in model:
                    providers.add(model.split("/")[0])
            for p in auth.get("profiles", {}).values():
                prov = p.get("provider", "")
                if prov:
                    providers.add(prov)
            providers_configured = sorted(providers)
        except Exception:
            pass
        svc = info["service"]
        try:
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
            gateway_running = svc in r.stdout
        except Exception:
            gateway_running = False
        result[bot] = {
            "user": info["user"],
            "config_path": info["config"],
            "gateway_service": svc,
            "gateway_running": gateway_running,
            "model_primary": model_primary,
            "model_catalog": model_catalog,
            "providers_configured": providers_configured,
            "config_readable": config_readable,
        }
    return {"bots": result}


def json_models(bot):
    if bot not in BOTS:
        raise ValueError(f"unknown bot: {bot}")
    _, config, _ = load_bot(bot)
    mc = get_model_config(config)
    catalog_dict = get_catalog(config)
    primary = mc.get("primary")
    fallbacks = mc.get("fallbacks", [])
    catalog = list(catalog_dict.keys()) if isinstance(catalog_dict, dict) else []
    return {
        "bot": bot,
        "primary": primary,
        "catalog": catalog,
        "fallback_order": fallbacks,
    }


def json_models_set(bot, model_list_str):
    if bot not in BOTS:
        return {"error": "bot not found", "bot": bot}
    models = model_list_str.strip().split()
    if not models:
        return {"error": "no models provided"}
    _, config, _ = load_bot(bot)
    primary = models[0]
    fallbacks = models[1:]
    mc = get_model_config(config)
    mc["primary"] = primary
    mc["fallbacks"] = fallbacks
    set_model_config(config, mc)
    catalog_dict = get_catalog(config)
    if not isinstance(catalog_dict, dict):
        catalog_dict = {}
    for model in models:
        if model not in catalog_dict:
            catalog_dict[model] = {}
    set_catalog(config, catalog_dict)
    _preserve_write(config, BOTS[bot]["config"])
    try:
        svc = BOTS[bot]["service"]
        subprocess.run(["launchctl", "kickstart", "-k", f"system/{svc}"],
                       capture_output=True, text=True)
    except Exception:
        pass
    return {"ok": True, "bot": bot, "primary": primary, "catalog": models}


def json_keys(bot):
    if bot not in BOTS:
        raise ValueError(f"unknown bot: {bot}")
    _, _, auth = load_bot(bot)
    profiles = auth.get("profiles", {})
    keys = {}
    for provider, meta in PROVIDER_META.items():
        entry = {}
        _, profile, field = find_profile(profiles, provider, "api_key")
        if profile is not None:
            val = profile.get(field, "")
            entry["api_key"] = bool(val and val.strip())
        else:
            entry["api_key"] = False
        if meta.get("has_token"):
            _, profile_t, field_t = find_profile(profiles, provider, "token")
            if profile_t is not None:
                val_t = profile_t.get(field_t, "")
                entry["token"] = bool(val_t and val_t.strip())
            else:
                entry["token"] = False
        keys[provider] = entry
    return {"bot": bot, "keys": keys}


def json_keys_set(bot, provider, mode):
    if bot not in BOTS:
        return {"error": f"unknown bot: {bot}"}
    key_value = sys.stdin.readline().strip()
    if not key_value:
        return {"error": "no key value provided"}
    _, _, auth = load_bot(bot)
    profiles = auth.get("profiles", {})
    name, profile, field = find_profile(profiles, provider, mode)
    if profile is None:
        field = "token" if mode == "token" else "key"
        profile_name = f"{provider}_{mode}"
        profiles[profile_name] = {
            "provider": provider,
            "type": mode,
            field: key_value,
        }
    else:
        profile[field] = key_value
    auth["profiles"] = profiles
    _preserve_write(auth, BOTS[bot]["auth"])
    return {"ok": True}


def json_usage(bot, days):
    inst_filter = None if bot == "all" else bot
    turns = _load_turns(days=days, instance_filter=inst_filter)
    total = len(turns)
    by_day = defaultdict(int)
    by_day_cost = defaultdict(float)
    by_model = defaultdict(int)
    by_source = defaultdict(int)
    for t in turns:
        date = t.get("ts", "")[:10]
        if date:
            by_day[date] += 1
            by_day_cost[date] += t.get("cost", 0) or 0
        by_model[t.get("model", "unknown")] += 1
        by_source[t.get("source", "unknown")] += 1
    total_cost = sum(t.get("cost", 0) or 0 for t in turns)
    all_max = bool(turns) and all(t.get("auth_mode") == "token" for t in turns)
    by_day_list = [
        {"date": d, "turns": by_day[d], "cost": 0.0 if all_max else by_day_cost.get(d, 0.0)}
        for d in sorted(by_day.keys())
    ]
    by_model_list = [
        {"model": m, "turns": c, "pct": round(100 * c / total) if total else 0}
        for m, c in sorted(by_model.items(), key=lambda x: -x[1])
    ]
    productive = by_source.get("human", 0)
    maintenance = sum(v for k, v in by_source.items() if k != "human")
    by_session_type = []
    if productive:
        by_session_type.append({"type": "productive", "turns": productive, "pct": round(100 * productive / total) if total else 0})
    if maintenance:
        by_session_type.append({"type": "maintenance", "turns": maintenance, "pct": round(100 * maintenance / total) if total else 0})
    result = {
        "bot": bot,
        "days": days,
        "turns_total": total,
        "cost_total_usd": total_cost,
        "by_day": by_day_list,
        "by_model": by_model_list,
        "by_session_type": by_session_type,
    }
    if all_max:
        result["cost_note"] = "MAX subscription — API cost is $0"
    return result


def json_gateway_restart(bot):
    if bot not in BOTS:
        return {"error": f"unknown bot: {bot}"}
    svc = BOTS[bot]["service"]
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"system/{svc}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return {"error": "gateway restart failed", "stderr": r.stderr.strip()}
    import time
    time.sleep(2)
    check = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    running = svc in check.stdout
    return {"ok": running, "bot": bot, "service": svc}


def main_json(args):
    if not args:
        print(json.dumps({"error": "subcommand required"}))
        sys.exit(1)
    sub = args[0]
    rest = args[1:]
    try:
        if sub == "status":
            bot = rest[0] if rest else "all"
            print(json.dumps(json_status(bot)))
        elif sub == "models":
            if rest and rest[0] == "set":
                print(json.dumps(json_models_set(rest[1], " ".join(rest[2:]))))
            else:
                print(json.dumps(json_models(rest[0] if rest else "all")))
        elif sub == "keys":
            if rest and rest[0] == "set":
                print(json.dumps(json_keys_set(rest[1], rest[2], rest[3] if len(rest) > 3 else "api_key")))
            else:
                print(json.dumps(json_keys(rest[0] if rest else "all")))
        elif sub == "usage":
            bot = rest[0] if rest else "all"
            days = 7
            if "--days" in rest:
                days = int(rest[rest.index("--days") + 1])
            print(json.dumps(json_usage(bot, days)))
        elif sub == "gateway":
            if rest and rest[0] == "restart":
                print(json.dumps(json_gateway_restart(rest[1])))
            else:
                print(json.dumps({"error": "gateway requires: restart <bot>"}))
                sys.exit(1)
        else:
            print(json.dumps({"error": f"unknown subcommand: {sub}"}))
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        main_json(sys.argv[2:])
        return  # never reached, main_json calls sys.exit
    if os.geteuid() != 0:
        print("❌ Run with sudo: sudo python3 /Users/Shared/openclaw-admin.py")
        sys.exit(1)

    print("=" * 62)
    print("  OpenClaw Admin Tool")
    print("  Changes save automatically. Enter bot name or 'all'.")
    print("=" * 62)
    print()
    print(f"  Bots: {', '.join(BOTS)} | all")
    target = input("  Which bot? ").strip().lower()

    if target == "all":
        all_bots_loop()
    elif target in BOTS:
        single_bot_loop(target)
    else:
        print(f"❌ Unknown: {target}")
        sys.exit(1)

if __name__ == "__main__":
    main()
