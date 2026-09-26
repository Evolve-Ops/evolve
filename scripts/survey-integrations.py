#!/usr/bin/env python3
"""Survey integration storage shapes across all bots in the pod.

For each bot, probes every known location where credentials for the
target integrations might live, then emits structured JSON. The output
is the input to a design pass on integration-discovery probes — see
internal/design/integration-discovery.md.

Read-only. Run as root (so we can read every bot user's home):

    sudo /usr/bin/python3 scripts/survey-integrations.py > /tmp/survey.json

Targets are configurable below — defaults to the six bots and six
services the user asked about (team_bot_a, team_bot_b, team_bot_c, admin_bot, security_bot, personal_bot
× git, dropbox, telegram, slack, discord, google).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

NETWORK_PATH = "/Users/Shared/evolve/network.json"
TARGET_BOTS = ["team_bot_a", "team_bot_b", "team_bot_c", "admin_bot", "security_bot", "personal_bot"]
TARGET_SERVICES = ["git", "dropbox", "telegram", "slack", "discord", "google"]


# ── helpers ──────────────────────────────────────────────────────────────────


def read_text(p: str) -> str | None:
    try:
        return Path(p).read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except Exception as e:
        return f"__read_error__: {type(e).__name__}: {e}"


def read_json(p: str) -> dict | list | None:
    text = read_text(p)
    if not text or text.startswith("__read_error__"):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def listdir(p: str, max_entries: int = 30) -> list[str] | None:
    try:
        entries = sorted(e.name for e in Path(p).iterdir())
        return entries[:max_entries]
    except Exception:
        return None


def find_files(root: str, name_globs: list[str], max_depth: int = 5, cap: int = 30) -> list[str]:
    """Wraps `/usr/bin/find` because pathlib's rglob doesn't honor maxdepth."""
    if not Path(root).exists():
        return []
    args = ["/usr/bin/find", root, "-maxdepth", str(max_depth), "("]
    for i, g in enumerate(name_globs):
        if i > 0:
            args.append("-o")
        args.extend(["-name", g])
    args.append(")")
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=30)
        if r.returncode != 0:
            return []
        out = [ln for ln in r.stdout.splitlines() if ln.strip()]
        return out[:cap]
    except Exception:
        return []


def deep_find_keys(obj, predicates: list[str]) -> list[str]:
    """Walk a JSON-like dict/list and return matching key paths."""
    pats = [re.compile(p, re.IGNORECASE) for p in predicates]
    hits: list[str] = []

    def walk(node, path: str):
        if isinstance(node, dict):
            for k, v in node.items():
                here = f"{path}.{k}" if path else k
                if any(p.search(k) for p in pats):
                    hits.append(here)
                walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return hits


# ── per-service probes ───────────────────────────────────────────────────────


def probe_git(home: str, oc: dict, profiles: dict) -> dict:
    out: dict = {}
    # SSH keys
    ssh_dir = f"{home}/.ssh"
    ssh_files = listdir(ssh_dir)
    if ssh_files:
        ssh_key_files = [f for f in ssh_files if f.startswith("id_") or f == "config" or f == "known_hosts"]
        out["ssh_dir_contents"] = ssh_key_files or ssh_files
    # gh CLI auth
    if Path(f"{home}/.config/gh/hosts.yml").exists():
        out["gh_cli_hosts_yml"] = "present"
    # auth-profiles entries (github)
    gh_profiles = [
        p for p, d in profiles.items()
        if "github" in p.lower()
        or (isinstance(d, dict) and "github" in str(d.get("provider", "")).lower())
    ]
    if gh_profiles:
        out["auth_profiles"] = {p: profiles[p] for p in gh_profiles}
    # gitconfig credential helpers / url overrides
    git_config = read_text(f"{home}/.gitconfig")
    if git_config:
        if "[credential" in git_config or "credentialhelper" in git_config:
            out["gitconfig_has_credential_section"] = True
        url_overrides = re.findall(r'\[url\s+"([^"]+)"\]\s*\n\s*insteadOf\s*=\s*"([^"]+)"', git_config)
        if url_overrides:
            out["gitconfig_url_overrides"] = url_overrides
    # macOS keychain credential helper presence (cannot list contents)
    if git_config and "osxkeychain" in git_config:
        out["macos_keychain_helper"] = True
    # Local repo count under workspace
    repos = find_files(home, [".git"], max_depth=4, cap=10)
    if repos:
        out["local_git_dirs"] = len(repos)
    return out


def probe_dropbox(home: str, oc: dict, profiles: dict) -> dict:
    out: dict = {}
    db_profiles = [
        p for p, d in profiles.items()
        if "dropbox" in p.lower()
        or (isinstance(d, dict) and "dropbox" in str(d.get("provider", "")).lower())
    ]
    if db_profiles:
        out["auth_profiles"] = {p: profiles[p] for p in db_profiles}
    # openclaw.json plugin / integration block
    oc_paths = deep_find_keys(oc, ["dropbox"])
    if oc_paths:
        out["openclaw_json_keys"] = oc_paths
    # Workspace credential files
    files = find_files(f"{home}/.openclaw/workspace", ["*dropbox*", "*Dropbox*"], max_depth=4)
    if files:
        out["workspace_files"] = files
    # Loose files anywhere in home matching dropbox
    loose = find_files(home, ["*dropbox*"], max_depth=3)
    if loose:
        out["loose_files"] = loose
    # MCP server config in openclaw.json
    mcp_servers = oc.get("mcpServers") or oc.get("mcp", {}).get("servers") or {}
    if isinstance(mcp_servers, dict):
        for name, cfg in mcp_servers.items():
            if "dropbox" in name.lower() or "dropbox" in json.dumps(cfg).lower():
                out.setdefault("mcp_servers", {})[name] = cfg
    return out


def probe_messaging(provider: str, home: str, oc: dict, profiles: dict) -> dict:
    """Shared probe for telegram/slack/discord — same shape across all three."""
    out: dict = {}
    matches = [
        p for p, d in profiles.items()
        if provider in p.lower()
        or (isinstance(d, dict) and provider in str(d.get("provider", "")).lower())
    ]
    if matches:
        out["auth_profiles"] = {p: profiles[p] for p in matches}
    # openclaw.json plugins.entries.<provider>
    plug_entry = (oc.get("plugins") or {}).get("entries", {}).get(provider)
    if plug_entry:
        # Don't dump tokens — just shape
        out["openclaw_plugins_entry"] = {
            k: ("<value>" if "token" in k.lower() or "secret" in k.lower() else v)
            for k, v in (plug_entry if isinstance(plug_entry, dict) else {}).items()
        }
    # Integrations block fallback (older shape)
    int_block = (oc.get("integrations") or {}).get(provider)
    if int_block:
        out["openclaw_integrations_block"] = "present"
    # Loose key search across openclaw.json
    keys = deep_find_keys(oc, [provider, f"{provider}_token", f"{provider}Token"])
    if keys:
        out["openclaw_json_key_paths"] = keys
    # Env-style files (some bots may use a .env-ish file)
    env_files = find_files(home, [".env", ".env.*"], max_depth=3)
    for ef in env_files:
        text = read_text(ef) or ""
        if re.search(rf"\b{provider}", text, re.IGNORECASE):
            out.setdefault("env_file_hits", []).append(ef)
    return out


def probe_google(home: str, oc: dict, profiles: dict) -> dict:
    out: dict = {}
    # 1. Wizard-managed auth-profiles entries (any key matching google/gmail)
    g_profiles = [
        p for p in profiles.keys()
        if "google" in p.lower() or "gmail" in p.lower()
    ]
    if g_profiles:
        out["auth_profiles_keys"] = g_profiles
        # Strip secrets from values for the survey
        out["auth_profiles_shapes"] = {
            p: sorted(profiles[p].keys()) if isinstance(profiles[p], dict) else type(profiles[p]).__name__
            for p in g_profiles
        }
    # 2. Legacy ~/.config/gws/ (oc gws --reauth flow)
    gws_dir = f"{home}/.config/gws"
    gws_listing = listdir(gws_dir)
    if gws_listing:
        out["config_gws_listing"] = gws_listing
    # 3. Workspace credentials dir (Team_bot_c / ranch pattern)
    ws_creds_dir = f"{home}/.openclaw/workspace/credentials"
    ws_creds = listdir(ws_creds_dir)
    if ws_creds:
        out["workspace_credentials_listing"] = ws_creds
        # Identify file types: client_secret, token, service-account JSON
        for fname in ws_creds:
            full = f"{ws_creds_dir}/{fname}"
            if not Path(full).is_file():
                continue
            sample = read_json(full)
            if isinstance(sample, dict):
                shape = sorted(sample.keys())
                if "private_key" in sample and "client_email" in sample:
                    out.setdefault("workspace_credentials_shapes", {})[fname] = "service_account_json"
                elif "installed" in sample or "web" in sample:
                    out.setdefault("workspace_credentials_shapes", {})[fname] = "oauth_client_secret_json"
                elif "refresh_token" in sample or "access_token" in sample:
                    out.setdefault("workspace_credentials_shapes", {})[fname] = "oauth_token_cache"
                else:
                    out.setdefault("workspace_credentials_shapes", {})[fname] = f"unknown:{shape[:5]}"
    # 4. openclaw.json plugin block
    g_plug = (oc.get("plugins") or {}).get("entries", {}).get("google")
    if g_plug:
        out["openclaw_plugin_google"] = g_plug if isinstance(g_plug, dict) else "present"
    # 5. Workspace integration manifests
    manifests_dir = f"{home}/.openclaw/workspace/manifests"
    if Path(manifests_dir).exists():
        manifests = listdir(manifests_dir)
        google_manifests = [m for m in (manifests or []) if "google" in m.lower() or "gmail" in m.lower() or "drive" in m.lower()]
        if google_manifests:
            out["workspace_manifests"] = google_manifests
    # 6. MCP servers referencing google
    mcp_servers = oc.get("mcpServers") or (oc.get("mcp") or {}).get("servers") or {}
    if isinstance(mcp_servers, dict):
        for name, cfg in mcp_servers.items():
            blob = json.dumps(cfg).lower()
            if "google" in name.lower() or "gmail" in blob or "googleapis" in blob:
                out.setdefault("mcp_servers", {})[name] = cfg
    # 7. Deep openclaw.json scan as a catch-all
    deep = deep_find_keys(oc, ["google", "gmail", "gws"])
    if deep:
        out["openclaw_deep_keys"] = deep[:30]
    return out


PROBES = {
    "git": probe_git,
    "dropbox": probe_dropbox,
    "telegram": lambda h, o, p: probe_messaging("telegram", h, o, p),
    "slack": lambda h, o, p: probe_messaging("slack", h, o, p),
    "discord": lambda h, o, p: probe_messaging("discord", h, o, p),
    "google": probe_google,
}


# ── per-bot driver ───────────────────────────────────────────────────────────


def survey_bot(bot_id: str, bot_cfg: dict) -> dict:
    user = bot_cfg.get("user") or bot_id
    home = f"/Users/{user}"
    findings: dict = {
        "bot_id": bot_id,
        "system_user": user,
        "home_exists": Path(home).exists(),
        "network_entry": bot_cfg,
    }
    if not findings["home_exists"]:
        return findings

    oc = read_json(f"{home}/.openclaw/openclaw.json") or {}
    if not isinstance(oc, dict):
        oc = {}
    ap = read_json(f"{home}/.openclaw/agents/main/agent/auth-profiles.json") or {}
    profiles = ap.get("profiles", {}) if isinstance(ap, dict) else {}

    findings["openclaw_top_level_keys"] = sorted(oc.keys())
    findings["auth_profiles_keys"] = sorted(profiles.keys())

    for service in TARGET_SERVICES:
        try:
            findings.setdefault("integrations", {})[service] = PROBES[service](home, oc, profiles) or {}
        except Exception as e:
            findings.setdefault("integrations", {})[service] = {"__probe_error__": f"{type(e).__name__}: {e}"}

    return findings


def main():
    net = read_json(NETWORK_PATH)
    if not isinstance(net, dict):
        print(f"ERROR: cannot read or parse {NETWORK_PATH}", file=sys.stderr)
        sys.exit(1)

    bots_cfg = net.get("bots", {})
    results: dict = {
        "_meta": {
            "network_path": NETWORK_PATH,
            "target_bots": TARGET_BOTS,
            "target_services": TARGET_SERVICES,
            "all_pod_bots": sorted(bots_cfg.keys()),
        },
        "bots": {},
    }

    for bot_id in TARGET_BOTS:
        if bot_id not in bots_cfg:
            results["bots"][bot_id] = {"__error__": "not in network.json"}
            continue
        results["bots"][bot_id] = survey_bot(bot_id, bots_cfg[bot_id])

    json.dump(results, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
