"""
evolve-admin setup wizard

A guided, idempotent setup flow for a new Evolve network.
Run with sudo: sudo evolve-admin setup

Steps:
  1. Welcome + prerequisites check
  2. Discover existing OpenClaw instances on this machine
  3. Confirm/edit the list of bots to include
  4. Configure alerts (channel + chat/user ID)
  5. Choose a pod ID
  6. Set shared directory path
  7. Write network.json
  8. Create shared directory
  9. Deploy Evolve to each bot (copy scripts + launchd)
  10. Run first measurement to verify
  11. Summary + next steps
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .web.server import _apply_credential_to_oc_dict
from .config import (
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_SHARED_DIR,
    load_network,
    save_network,
    get_bot_workspace,
    user_home,
)
from .deploy import deploy_bot, deploy_shared_dir, DeployResult
from .installer import resolve_evolve_admin_bin
from .runtime.isolation import IsolationError, get_isolation
from platform_profile import get_profile


# ── Terminal helpers ──────────────────────────────────────────────────────────

def _c(text: str, code: str) -> str:
    """ANSI color wrapper."""
    codes = {"bold": "1", "dim": "2", "green": "32", "yellow": "33",
             "red": "31", "blue": "34", "cyan": "36", "reset": "0"}
    return f"\033[{codes.get(code, '0')}m{text}\033[0m"


def _header(title: str) -> None:
    width = 62
    print()
    print(_c("─" * width, "dim"))
    print(_c(f"  ⚡ {title}", "bold"))
    print(_c("─" * width, "dim"))


def _step(n: int, total: int, desc: str) -> None:
    print()
    print(_c(f"  Step {n}/{total} — {desc}", "bold"))


def _ok(msg: str) -> None:
    print(_c(f"  ✓ {msg}", "green"))


def _warn(msg: str) -> None:
    print(_c(f"  ⚠  {msg}", "yellow"))


def _err(msg: str) -> None:
    print(_c(f"  ✗ {msg}", "red"))


def _skip(msg: str) -> None:
    print(_c(f"  - {msg} (skipped)", "dim"))


def _info(msg: str) -> None:
    print(f"  {msg}")


def _ask(prompt: str, default: str = "") -> str:
    default_hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{default_hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _ask_secret(prompt: str) -> str:
    """Prompt for a secret (API key / token) WITHOUT echoing it.

    `input()` echoes the typed characters, so when an operator runs
    `setup --fresh` under `script`/`tee` the cleartext key lands in the
    session log (round-3 hygiene finding F: the real key was captured in
    /root/setup-session.log). `getpass` reads with terminal echo off, so
    nothing the operator types is captured. No TTY → getpass prints a
    warning and reads plainly; acceptable for the non-interactive path,
    which supplies keys from a file rather than this prompt."""
    import getpass
    try:
        return getpass.getpass(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} ({hint}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val.startswith("y")


def _pause() -> None:
    try:
        input(_c("  Press Enter to continue...", "dim"))
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _os_user_noun() -> str:
    """Operator-facing OS noun for account/user copy — "macOS" or "Linux".

    Single source for the platform-keyed account vocabulary so interactive
    strings don't print "macOS user ..." verbatim on a Linux pod (wizard-side
    sibling of the Pod Health fix, #3180). Mirrors the inline idiom already in
    ``_provision_account_and_gateway`` and ``setup_wizard._account_noun``.
    """
    return "macOS" if get_profile().name == "macos" else "Linux"


# ── OpenClaw discovery (advisory only) ────────────────────────────────────────
#
# Pod membership is decided by the user (wizard prompt, `evolve-admin add-bot`,
# or the UI's Add Bot flow) — never inferred from filesystem state. These types
# describe what was found on disk. The CHOICE of which candidates become pod
# bots — and what bot_id to claim them under — is the operator's, made
# explicitly through `add_bot()`.

LAUNCHD_DIR = Path("/Library/LaunchDaemons")


@dataclass
class OcCandidate:
    """An OpenClaw install discovered on this machine.

    Identified by `(user, config_path)` — a bot_id is the operator's choice
    at registration time, not a property of the install. Multiple candidates
    per macOS user, and multiple users per machine, are both naturally
    representable.

    Fields:
      user:                  macOS account owning the .openclaw directory
      config_path:           path to the install's openclaw.json
      port:                  gateway.port from openclaw.json (None if unreadable)
      plist_label:           full launchd label of a gateway plist whose
                             UserName matches `user`, e.g. "ai.openclaw.team_bot_b-gateway"
                             (None if no matching plist found)
      suggested_bot_id:      bot_id parsed from the plist label (e.g. "team_bot_b"),
                             offered as a default at the registration prompt;
                             `None` if no plist match
      looks_like_admin:      True if `user` matches SUDO_USER or is in the
                             macOS `admin` group — a hint to the wizard, not
                             an exclusion
      is_pod_member:         True if `user` or `port` already corresponds to a
                             registered bot in the supplied network config
    """
    user: str
    config_path: Path
    port: Optional[int]
    plist_label: Optional[str] = None
    suggested_bot_id: Optional[str] = None
    looks_like_admin: bool = False
    is_pod_member: bool = False


@dataclass
class DiscoveredBot:
    """A pod member as the user has chosen it during the wizard.

    Distinct from OcCandidate: this represents the operator's decision to
    register a candidate (or a brand-new bot) under a specific bot_id.
    Used as the wizard's working list before save_network + deploy_bot.
    """
    bot_id: str
    user: str
    home: Path
    oc_config: Path
    workspace: Optional[Path]
    port: Optional[int]
    is_known: bool = False


def _looks_like_admin_account(uname: str) -> bool:
    """Heuristic: True if `uname` is the human running sudo or in macOS admin.

    Data-driven (no hardcoded names). Used by find_oc_candidates to flag
    likely-admin accounts so the wizard can dim them in the candidate list,
    but never to filter — the operator decides.
    """
    sudo_user = os.environ.get("SUDO_USER", "")
    if sudo_user and uname == sudo_user:
        return True
    try:
        return uname in get_isolation().group_members("admin")
    except Exception:
        return False


def _read_oc_port(config_path: Path) -> Optional[int]:
    """Read gateway.port from an openclaw.json. Returns None on any failure."""
    text: Optional[str] = None
    try:
        text = config_path.read_text()
    except (OSError, PermissionError):
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(config_path)],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                text = r.stdout
        except Exception:
            return None
    if not text or not text.strip():
        return None
    try:
        port = json.loads(text).get("gateway", {}).get("port")
        return int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None
    except (json.JSONDecodeError, ValueError):
        return None


def _gateway_plists_by_user() -> dict[str, str]:
    """Index gateway plists by their UserName field. Returns {user: label}."""
    out: dict[str, str] = {}
    if not LAUNCHD_DIR.exists():
        return out
    for plist in sorted(LAUNCHD_DIR.glob("ai.openclaw.*-gateway.plist")):
        try:
            r = subprocess.run(
                ["sudo", "/usr/libexec/PlistBuddy", "-c", "Print :UserName", str(plist)],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                user = r.stdout.strip()
                if user:
                    # First plist wins on collision — multi-OC-per-user candidates
                    # will be distinguished by config_path, not the suggested_bot_id.
                    out.setdefault(user, plist.stem)
        except Exception:
            continue
    return out


def find_oc_candidates(network: Optional[dict] = None) -> list[OcCandidate]:
    """Scan the local machine for OpenClaw installs.

    Advisory only — does NOT mutate network.json. Pod membership is the
    operator's explicit decision, made through `add_bot()`. Discovery is
    only here to populate prompts and flag already-claimed installs in
    the UI.

    Pass `network` (a loaded network.json dict) to set `is_pod_member`
    on each candidate; otherwise the field is False for all.
    """
    network = network or {}
    pod_users: set[str] = set()
    pod_ports: set[int] = set()
    for bot_id, cfg in (network.get("bots") or {}).items():
        if not isinstance(cfg, dict):
            continue
        pod_users.add(cfg.get("user", bot_id))
        port = cfg.get("port")
        if isinstance(port, int):
            pod_ports.add(port)

    plists_by_user = _gateway_plists_by_user()

    candidates: list[OcCandidate] = []
    # Home-directory base for the OC-install scan: /Users on macOS, /home on
    # Linux. Routed through the profile so discovery finds real Linux installs
    # (and doesn't chase a stray /Users on a Linux box). Gateway-plist matching
    # (_gateway_plists_by_user) is launchd-only and returns {} on Linux —
    # candidates there simply carry no suggested_bot_id, which is fine.
    users_root = Path(get_profile().user_home_root)
    if not users_root.exists():
        return candidates

    for user_dir in sorted(users_root.iterdir()):
        uname = user_dir.name
        if uname.startswith("."):
            continue
        config_path = user_dir / ".openclaw" / "openclaw.json"
        if not config_path.exists():
            continue  # only OC installs are candidates — naturally skips Shared, Guest, etc.

        port = _read_oc_port(config_path)
        plist_label = plists_by_user.get(uname)
        suggested_bot_id: Optional[str] = None
        if plist_label:
            stem = plist_label.removeprefix("ai.openclaw.").removesuffix("-gateway")
            suggested_bot_id = stem or None

        is_pod_member = (uname in pod_users) or (port is not None and port in pod_ports)

        candidates.append(OcCandidate(
            user=uname,
            config_path=config_path,
            port=port,
            plist_label=plist_label,
            suggested_bot_id=suggested_bot_id,
            looks_like_admin=_looks_like_admin_account(uname),
            is_pod_member=is_pod_member,
        ))

    return candidates


# ── Bot creation helpers ──────────────────────────────────────────────────────

SOUL_TEMPLATE = """\
# SOUL.md — {name}

{purpose}

## Core Rules
- Be genuinely helpful, not performatively helpful
- Read anything freely; send nothing externally without explicit approval
- Log important work and decisions as you go
- Be resourceful before asking

## Vibe
Concise, capable, trustworthy.
"""

AGENTS_MD_TEMPLATE = """\
# AGENTS.md — {name}

You are {name}, an OpenClaw AI assistant.

{purpose}

## Workspace
{workspace}/

## Style
Concise. Verify before claiming. Ask when genuinely unsure.
"""


def _find_existing_keys() -> list[dict]:
    """Scan existing bot home dirs for configured LLM API keys.
    Returns list of {profile_id, provider, type, source_bot} — NEVER the key value in display.
    Stores value internally for copying."""
    keys = []
    try:
        # Home-directory base is platform-keyed (/Users on macOS, /home on
        # Linux) — mirrors the OC-install scan in _scan_oc_candidates. Without
        # this, a Linux pod scanned a stray /Users tree and offered a stale
        # leftover key (the round-3 "key found (from darwin)" off /Users/darwin).
        for bot_dir in Path(get_profile().user_home_root).iterdir():
            if not bot_dir.is_dir():
                continue
            auth_path = bot_dir / ".openclaw/agents/main/agent/auth-profiles.json"
            if not auth_path.exists():
                continue
            try:
                r = subprocess.run(
                    ["sudo", "cat", str(auth_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    continue
                profiles = json.loads(r.stdout)
                for pid, p in profiles.get("profiles", {}).items():
                    val = p.get("key") or p.get("token") or p.get("apiKey")
                    if p.get("provider") and val:
                        keys.append({
                            "profile_id": pid,
                            "provider": p["provider"],
                            "type": p.get("type", "api_key"),
                            "source_bot": bot_dir.name,
                            "_value": val,  # internal only — never display
                        })
            except Exception:
                continue
    except Exception:
        pass
    return keys


def _wizard_floor_model(provider: str, role: str) -> str:
    """Resolve the starter ``primary`` model id for a new bot via the registry.

    Post-#1765-revert: workhorse-first walk (tier2 → tier3 → tier1) for
    ALL roles. Background work routes to tier3 via the trigger anchor
    (PR #1737/#1764) + routing.backgroundTier regardless of what
    `primary` is set to, so flipping primary to tier3 for member bots
    achieved no cost win on background turns AND silently degraded
    human chat (Slack/Telegram users got tier3 with no in-channel
    escalation path). See deploy.py module comment for the full history.

    ``role`` is currently ignored — kept on signature so the wizard
    pipeline + the forthcoming per-bot default-tier picker can drop
    in role/preference-aware selection here cleanly.

    Returns ``""`` when the registry import fails — the caller writes
    an empty primary field, which deploy will then fill in via its
    own tier-resolved path on the next deploy. No hardcoded model
    fallback (see follow-up "no-hardcoded-models" cleanup task).
    """
    del role  # see docstring + module-level history in deploy.py
    try:
        from model_registry import RECOMMENDED  # type: ignore
    except Exception:
        return ""
    rec = RECOMMENDED.get(provider, {})
    for tier_id in ("tier2", "tier3", "tier1"):
        entry = rec.get(tier_id)
        if entry and entry.get("model"):
            return entry["model"]
    return ""


def _new_bot_openclaw_config(
    name: str, provider: str, port: int,
    channel_token: str = "", chat_id: str = "",
    brave_key: str = "",
    role: str = "member",
) -> dict:
    # Floor-aware primary selection — mirrors deploy._detect_provider_model.
    # The wizard creates member bots; their primary defaults to the
    # provider's tier3 (Haiku for Anthropic, gpt-4o-mini for OpenAI).
    # Primary/admin bots get tier2 (Sonnet/gpt-4o). This is the same
    # architectural rule as PR #1736: don't hardcode model names in
    # admin code, route through the tier registry.
    primary_model = _wizard_floor_model(provider, role)
    config = {
        "agents": {
            "defaults": {
                "model": {"primary": primary_model, "fallbacks": []},
                "workspace": str(user_home(name) / ".openclaw" / "workspace"),
                "thinkingDefault": "off",
            }
        },
        "gateway": {"port": port, "mode": "local", "bind": "loopback", "trustedProxies": []},
        "plugins": {"entries": {}},
        "channels": {},
    }
    if channel_token:
        # Channel config defaults (non-credential). The bot token itself is
        # injected via the shared `_apply_credential_to_oc_dict` registry helper
        # so this builder stays in sync with the rotate endpoint's mirror paths.
        config["channels"]["telegram"] = {
            "enabled": True,
            "dmPolicy": "pairing",
            "groupPolicy": "allowlist",
            "streaming": {"mode": "off"},
        }
        config["plugins"]["entries"]["telegram"] = {"enabled": True}
        _apply_credential_to_oc_dict(config, "telegram", "bot_token", channel_token)
        # NOTE: chat_id is intentionally NOT written into channels.telegram here.
        # OC's config schema declares channels.telegram with
        # additionalProperties:false and no chatId key — writing it would be
        # rejected by safe_write_bot_config (and writing it raw would poison
        # every future validated config write). The operator's chat_id is
        # seeded post-deploy via seed_channel_identity (owner + DM approval +
        # the chat_id field on the telegram auth-profiles token_pair, which is
        # where the Credentials UI's canonical probe reads it). The param is
        # retained because the wizard return dict carries it onward (alerts
        # default + the post-deploy seed).
    if brave_key:
        config["plugins"]["entries"]["brave"] = {"config": {}}
        config.setdefault("tools", {}).setdefault("web", {}).setdefault("search", {})["provider"] = "brave"
        _apply_credential_to_oc_dict(config, "brave", "api_key", brave_key)
    return config


def _new_bot_auth_profiles(provider: str, key_type: str, key_value: str) -> dict:
    profile_id = f"{provider}:{'default' if key_type == 'token' else 'api'}"
    key_field = "key" if key_type == "api_key" else "token"
    return {
        "version": 1,
        "profiles": {
            profile_id: {
                "type": key_type,
                "provider": provider,
                key_field: key_value,
            }
        },
        "lastGood": {provider: profile_id},
    }


def _next_available_port(net_path: Optional[Path] = None, start: int = 19000) -> int:
    """Find the next available port starting at 19000, incrementing by 10."""
    used: set[int] = set()

    # From network.json
    if net_path and net_path.exists():
        try:
            from .config import load_network
            net = load_network(net_path)
            for bot_cfg in net.get("bots", {}).values():
                p = bot_cfg.get("port")
                if p:
                    used.add(int(p))
        except Exception:
            pass

    # From existing OC configs (platform-keyed home base — see _find_existing_keys)
    try:
        for user_dir in Path(get_profile().user_home_root).iterdir():
            if not user_dir.is_dir():
                continue
            oc_json = user_dir / ".openclaw" / "openclaw.json"
            if oc_json.exists():
                try:
                    cfg = json.loads(oc_json.read_text())
                    p = cfg.get("gateway", {}).get("port")
                    if p:
                        used.add(int(p))
                except Exception:
                    pass
    except Exception:
        pass

    port = start
    while port in used:
        port += 10
    return port


def _send_telegram_test(token: str, chat_id: str, name: str) -> tuple[bool, str]:
    """Send a test message via Telegram Bot API. Returns (success, error_detail)."""
    import urllib.request
    import urllib.error
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": f"Hello from {name}! Evolve setup test message.",
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return bool(result.get("ok")), ""
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return False, body.get("description", str(e))
        except Exception:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def _write_bot_files(
    name: str, oc_config: dict, auth_profiles: dict,
    soul_md: str, agents_md: str,
) -> list[str]:
    """Write bot config files via sudo. Returns list of error strings."""
    errors: list[str] = []
    # pwd-first home resolution (account == bot name here — the wizard just
    # created it). Platform-keyed: /Users/<name> on macOS, /home/<name> on
    # Linux. The hardcoded /Users literal wrote darwin's real key to
    # /Users/darwin on a Linux pod while the bot's agent looked under
    # /home/darwin (round-3 bug B). Never do f"{user_home_root}/{name}".
    home = user_home(name)
    oc_dir = home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"

    # Create directory tree
    for d in [oc_dir, agent_dir, workspace, oc_dir / "logs"]:
        r = subprocess.run(["sudo", "mkdir", "-p", str(d)], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"mkdir {d}: {r.stderr.strip()}")
            return errors

    # Write each file via tmp → sudo cp
    files = [
        (oc_dir / "openclaw.json",         json.dumps(oc_config, indent=2)),
        (agent_dir / "auth-profiles.json", json.dumps(auth_profiles, indent=2)),
        (workspace / "SOUL.md",            soul_md),
        (workspace / "AGENTS.md",          agents_md),
    ]
    for dst, content in files:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        r = subprocess.run(["sudo", "cp", tmp_path, str(dst)], capture_output=True, text=True)
        os.unlink(tmp_path)
        if r.returncode != 0:
            errors.append(f"Write {dst.name}: {r.stderr.strip()}")

    # Best-effort chown (fails gracefully if user doesn't exist yet)
    subprocess.run(
        ["sudo", "chown", "-R", f"{name}:staff", str(oc_dir)],
        capture_output=True,
    )
    return errors


def _provision_account_and_gateway(name: str) -> None:
    """Create the OS account for ``name``, fix ownership, and install its
    gateway — platform-keyed throughout (W10 #1/#4/#5).

    - The account-creation ritual lives in the isolation seam
      (``runtime.isolation.*Isolation.create_user``) — full sudo paths,
      dash-form verbs, the shapes the sudoers grants match. On macOS that's
      the dscl + createhomedir dance; on Linux it's useradd. This replaced an
      inline copy that used bare ``dscl``/``createhomedir`` names (which 404
      under sudo's secure_path on production pods).
    - Ownership: the chown BINARY routes through the profile
      (``/usr/sbin/chown`` on macOS, ``/usr/bin/chown`` on Linux); ``:staff``
      stays literal (gid 50 on Ubuntu) per deploy.py's W7 primary-group rule.
      The account home resolves via ``user_home()`` (pwd-first) — the wizard
      just created the account *named* ``name``, so account == bot name here;
      never do ``f"{user_home_root}/{name}"`` path math. Mirrors the
      already-fixed ``_setup_oc_for_bot`` idiom (#2979) + setup_wizard.py:3575.
    - On a fresh pod the deploy venv (which provides the ``evolve-admin``
      console script and is not on the system PATH) isn't built until the
      main 'Deploy Evolve' setup step, so a gateway install here would just
      hit "command not found". An absent venv is a legitimately-DEFERRED
      install (info note), distinct from a REAL failure (venv present but the
      deploy errored → loud warn).

    Caller side effects only (prints + sudo subprocesses); returns nothing.
    """
    profile = get_profile()
    os_noun = "macOS" if profile.name == "macos" else "Linux"
    _info(f"  Creating {os_noun} user '{name}'...")
    iso = get_isolation()
    if not iso.user_exists(name):
        uid = iso.next_free_uid()
        try:
            iso.create_user(name, uid)
            user_ok = True
        except IsolationError as exc:
            _warn(f"  Account creation step failed: {str(exc)[:120]}")
            user_ok = False
        if user_ok:
            subprocess.run(
                ["sudo", profile.chown, "-R", f"{name}:staff",
                 str(user_home(name) / ".openclaw")],
                capture_output=True,
            )
            _ok(f"  {os_noun} user '{name}' created (UID {uid})")
        else:
            _warn(f"  {os_noun} user creation incomplete — fix manually and re-run.")
    else:
        _skip(f"{os_noun} user '{name}' (already exists)")

    # Install gateway (deferred on a fresh pod — see docstring).
    if not Path(profile.venv_python).exists():
        _info("  Gateway install deferred — the evolve deploy venv isn't built yet.")
        _info("  It will be deployed during the main 'Deploy Evolve' setup step.")
        _info(f"  (Or run manually after setup: sudo evolve-admin deploy {name})")
    else:
        _info(f"  Installing gateway for '{name}'...")
        # Resolve evolve-admin to an absolute path — a uv-sync venv lives off
        # sudo's secure_path, so a bare "evolve-admin" here dies "command not
        # found" and the gateway (apply./test. daemons) never installs
        # (live evolve-vsp-pod, FIND-A).
        ea_bin = resolve_evolve_admin_bin()
        r_gw = subprocess.run(
            ["sudo", ea_bin, "deploy", name],
            capture_output=True, text=True, timeout=120,
        )
        if r_gw.returncode == 0:
            _ok(f"  Gateway installed for '{name}'")
        else:
            _warn(f"  Gateway install failed — run manually: sudo evolve-admin deploy {name}")
            _warn(f"  {r_gw.stderr.strip()[:120]}")

    # Check that the per-bot scheduled daemons were installed. They may be
    # skipped on first run if the evolve venv doesn't exist yet. The daemon
    # dir + unit-file suffix are platform-keyed: launchd writes
    # /Library/LaunchDaemons/<label>.plist, systemd writes
    # /etc/systemd/system/<label>.service.
    daemon_dir = Path(profile.daemon_dir)
    unit_suffix = ".plist" if profile.name == "macos" else ".service"
    jobs_noun = "launchd jobs" if profile.name == "macos" else "systemd jobs"
    missing_units = [
        daemon_dir / f"{label}{unit_suffix}"
        for label in [f"ai.openclaw.evolve.apply.{name}", f"ai.openclaw.evolve.test.{name}"]
        if not (daemon_dir / f"{label}{unit_suffix}").exists()
    ]
    if missing_units:
        _warn(f"  Some {jobs_noun} were not installed (venv may not be set up yet):")
        for unit_path in missing_units:
            _warn(f"    Missing: {unit_path}")
        _warn(f"  After completing evolve user setup, run: sudo evolve-admin deploy {name}")


def _pod_invariant_integrations(net_path: Optional[Path] = None) -> set[str]:
    """Return the pod's invariant-integration set (lower-cased).

    Single source of truth for which integrations the wizard SOLICITS. The
    Brave (and any future optional-integration) prompt is gated on membership
    here, so demoting an integration out of ``podInvariantIntegrations``
    automatically silences its wizard prompt — the prompt set can't drift away
    from the pod-invariant set the dashboard enforces. Falls back to the loader
    default (already brave-demoted + migrated) when the file is absent or the
    field is malformed.
    """
    net_path = net_path or DEFAULT_NETWORK_CONFIG
    try:
        net = load_network(net_path) if net_path.exists() else {}
    except Exception:
        net = {}
    raw = net.get("podInvariantIntegrations")
    if not isinstance(raw, list):
        from .config import DEFAULT_POD_INVARIANT_INTEGRATIONS
        raw = list(DEFAULT_POD_INVARIANT_INTEGRATIONS)
    return {str(x).strip().lower() for x in raw if str(x).strip()}


def _create_bot_flow(existing_keys: list[dict]) -> Optional[dict]:
    """
    Interactive CLI flow to configure a new bot.
    Returns bot config dict or None if user cancels.
    Executes privileged steps (account creation, gateway) directly.
    """
    _header("Add a New Bot")

    # ── 1. Bot name ────────────────────────────────────────────────────────
    while True:
        name = _ask("Bot name (e.g. assistant, helper, advisor)", "").strip().lower()
        if not name:
            _info("Cancelled.")
            return None
        if not re.match(r'^[a-z][a-z0-9_-]*$', name):
            _warn("Name must start with a letter and contain only letters, digits, hyphens, or underscores.")
            continue
        if get_isolation().user_exists(name):
            _warn(f"{_os_user_noun()} user '{name}' already exists.")
            if not _confirm("Use this name anyway?", default=False):
                continue
        break

    # ── 2. Purpose ─────────────────────────────────────────────────────────
    purpose = _ask(
        "Purpose (one sentence describing what this bot does)",
        "Personal assistant for daily tasks",
    ).strip() or "Personal assistant for daily tasks"

    # ── 3. LLM provider ────────────────────────────────────────────────────
    # No preselected default — Enter-through must not end with a provider
    # chosen for the user (provider-agnostic principle: recommending is
    # fine, presuming is not).
    print()
    _info("LLM Provider:")
    _info("  [1] Anthropic (recommended)")
    _info("  [2] OpenAI")
    _info("  [3] Other (enter manually)")
    while True:
        provider_choice = _ask("Provider (1-3)", "").strip()
        if provider_choice == "1":
            provider = "anthropic"
        elif provider_choice == "2":
            provider = "openai"
        elif provider_choice == "3":
            provider = _ask("Provider name (e.g. mistral, cohere)", "").strip().lower()
            if not provider:
                _warn("Provider name required.")
                continue
        else:
            _warn("Pick 1, 2, or 3 — Evolve doesn't choose a provider for you.")
            continue
        break

    # ── 4. API key ─────────────────────────────────────────────────────────
    api_key = ""
    key_type = "api_key"

    matching = [k for k in existing_keys if k["provider"] == provider]
    if matching:
        k = matching[0]
        print()
        if _confirm(
            f"{provider.capitalize()} key found (from {k['source_bot']}). Reuse it?",
            default=True,
        ):
            api_key = k["_value"]
            key_type = k["type"]
            _ok(f"Using {provider} key from {k['source_bot']}")

    if not api_key:
        print()
        if provider == "anthropic":
            _info("Enter Anthropic API key (sk-ant-...) or MAX token (sk-ant-oat...):")
        elif provider == "openai":
            _info("Enter OpenAI API key (sk-...):")
        else:
            _info(f"Enter {provider} API key:")
        # W10-G #6a: this prompt is getpass-masked (unlike the echoing token
        # prompts) — flag the hidden input so a blank line doesn't read as a
        # failed paste. Masking stays (correct for a key).
        api_key = _ask_secret("API key (input hidden — paste won't show)")
        if not api_key:
            _warn("No API key provided. Configure it later in auth-profiles.json.")
        else:
            key_type = "token" if api_key.startswith("sk-ant-oat") else "api_key"

    # ── 5. Messaging channel ───────────────────────────────────────────────
    print()
    _info("Messaging channel:")
    _info("  [1] Telegram (recommended — create bot at @BotFather)")
    _info("  [2] None (configure later)")
    channel_choice = _ask("Channel", "1").strip()

    channel_token = ""
    chat_id = ""

    if channel_choice == "1":
        channel_token = _ask("Telegram bot token (from @BotFather)", "").strip()
        if channel_token:
            chat_id = _ask("Your Telegram chat ID", "").strip()
            if chat_id:
                _info("Sending test message...")
                ok, err = _send_telegram_test(channel_token, chat_id, name)
                if ok:
                    _ok("Test message sent!")
                    if not _confirm("Received it?", default=True):
                        _warn("Test not confirmed — check your token and chat ID.")
                else:
                    # The overwhelmingly common cause of a failed first send is
                    # simply that the operator hasn't opened a chat with the bot
                    # yet: Telegram blocks bot→user DMs until the user sends
                    # /start. Lead with that as a normal next step rather than a
                    # ⚠ "you typed something wrong" error (the API's "chat not
                    # found" is just the symptom of the un-started chat).
                    _info("Almost there — Telegram won't let a bot message you until you")
                    _info("open Telegram, find your bot, and tap [bold]/start[/]. Do that, then alerts will flow.")
                    detail = f" [dim](Telegram said: {err})[/]" if err else ""
                    _info(f"  No test message went out yet.{detail}")

    # ── 5b. Brave Search ──────────────────────────────────────────────────
    # Solicited ONLY when brave is a pod invariant. Brave was demoted to an
    # optional integration (2026-06-24): web search is optional, especially for
    # the evo bot. Deriving the prompt from podInvariantIntegrations (rather
    # than a hardcoded question) means the prompt and the dashboard's
    # invariant set can never disagree — re-promoting brave by adding it back
    # to the list re-enables this automatically. An already-configured key the
    # operator wants reused is still offered even when brave is optional.
    brave_key = ""
    print()
    _solicit_integrations = _pod_invariant_integrations()
    existing_brave = [k for k in existing_keys if k["provider"] == "brave"]
    if existing_brave:
        k = existing_brave[0]
        if _confirm(
            f"Brave Search key found (from {k['source_bot']}). Use it?",
            default=True,
        ):
            brave_key = k["_value"]
            _ok(f"Using Brave key from {k['source_bot']}")
    if not brave_key and "brave" in _solicit_integrations:
        _info("Brave Search API key (optional — enables web search):")
        _info("  Get one free at https://api.search.brave.com/")
        brave_key = _ask("Brave API key (Enter to skip)", "").strip()

    # ── 5c. Backup repo ────────────────────────────────────────────────────
    print()
    _info("Workspace backup (optional — nightly git push to a private repo):")
    _info("  1. Create a private GitHub repo (e.g. github.com/you/{name}-workspace)")
    _info("  2. Paste its SSH URL here — the deploy key is generated automatically.")
    _info("  3. After setup, add the printed public key to the repo's Deploy Keys.")
    backup_repo_url = _ask(
        f"Backup repo SSH URL (Enter to skip)",
        "",
    ).strip()

    # ── 5d. Access model ───────────────────────────────────────────────────
    print()
    _info("Access model:")
    _info("  [1] Single-user  — only you will talk to this bot (default)")
    _info("  [2] Multi-user   — multiple people will share this bot")
    access_choice = _ask("Access", "1").strip()
    multi_user = access_choice == "2"

    # ── 6. Generate SOUL.md / AGENTS.md ───────────────────────────────────
    soul_md = SOUL_TEMPLATE.format(name=name, purpose=purpose)
    agents_md = AGENTS_MD_TEMPLATE.format(
        name=name, purpose=purpose,
        workspace=str(user_home(name) / ".openclaw" / "workspace"),
    )

    # ── 7. Determine port ──────────────────────────────────────────────────
    from .config import DEFAULT_NETWORK_CONFIG
    port = _next_available_port(DEFAULT_NETWORK_CONFIG)

    # ── 8. Build config dicts ──────────────────────────────────────────────
    oc_config = _new_bot_openclaw_config(name, provider, port, channel_token, chat_id, brave_key)
    if api_key:
        auth_profiles = _new_bot_auth_profiles(provider, key_type, api_key)
    else:
        auth_profiles = {"version": 1, "profiles": {}, "lastGood": {}}

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    _header(f"New Bot: {name}")
    _info(f"  Name:     {_c(name, 'bold')}")
    _info(f"  Purpose:  {purpose}")
    _info(f"  Provider: {_c(provider.capitalize(), 'bold')}" +
          (" (shared key)" if matching and api_key == matching[0]["_value"] else ""))
    _info(f"  Channel:  {_c('Telegram' if channel_token else 'None (configure later)', 'bold')}")
    _info(f"  Port:     {port}")
    print()

    if not _confirm("Create this bot?", default=True):
        _info("Cancelled.")
        return None

    # ── 8. Write config files ──────────────────────────────────────────────
    _info("Writing config files...")
    errors = _write_bot_files(name, oc_config, auth_profiles, soul_md, agents_md)
    if errors:
        for e in errors:
            _warn(f"  {e}")
        _warn("Some files could not be written yet (user doesn't exist — will be created next).")
    else:
        _ok("Config files written.")

    # ── 9. Create the OS account + install gateway directly ───────────────────
    _provision_account_and_gateway(name)

    # Defense-in-depth: scan the bot's workspace for hardcoded credentials
    # before declaring setup complete. The wizard itself doesn't write any
    # files containing tokens (only auth-profiles.json + openclaw.json,
    # both allowlisted), but humans and ad-hoc tooling commonly drop
    # *_SETUP.md notes, .env files, and watchdog scripts with tokens
    # embedded — see issues/open/2026-04-27-003 for the historical
    # cluster of team_bot_a/team_bot_c/security_bot leaks. Catching them at setup time means
    # they never escape into the 15-min audit's recurring critical alerts.
    _scan_workspace_for_secrets(name)

    return {
        "name": name,
        "purpose": purpose,
        "provider": provider,
        "port": port,
        "channel": "telegram" if channel_token else "none",
        # Surface the operator's personal chat ID so the wizard can reuse it
        # as the default for the Evolve-alerts chat (Step 11) — same person,
        # don't ask twice. Empty when no Telegram channel was configured.
        "chat_id": chat_id,
        "multi_user": multi_user,
        "backup_repo_url": backup_repo_url,
    }


def _selected_bot_channels(new_bot: dict) -> tuple[str, ...]:
    """The operator's channel pick off a `_create_bot_flow` result.

    Delegates to setup_wizard's registry-backed normalizer so both installer
    paths validate against the ONE channel vocabulary
    (`evolve_admin.channel_registry`) rather than growing a second table —
    tools/channel-literal-lint forbids the alternative. Imported locally
    because setup_wizard imports THIS module (function-locally); a top-level
    import in both directions would be a cycle.
    """
    from .setup_wizard import normalize_channels
    return normalize_channels(new_bot.get("channel"))


def _merge_bot_entry(
    existing_entry: object,
    *,
    bot_id: str,
    user: str,
    port: object,
    channels: tuple[str, ...],
) -> dict:
    """Build one `bots.<id>` entry, PRESERVING keys this loop doesn't manage.

    Copy-then-mutate, the same shape `deploy.deploy_bot` uses. The previous
    code assigned a fresh ``{"role", "port"[, "user"]}`` literal, so re-running
    ``evolve-admin setup`` over an existing pod silently dropped every other
    key on the entry — ``multiUser``, ``backupRepoUrl``, ``purpose``,
    ``comms_mode``, ``daily_cap_usd``. That clobber is also what would have
    made the channel write below un-do itself on the next setup run, so the
    two fixes are one change.

    Every field the old literal DID manage is set exactly as before (role is
    re-asserted to "member", port and user overwritten) — this widens what
    survives without changing what was already written.
    """
    entry: dict = dict(existing_entry) if isinstance(existing_entry, dict) else {}
    entry["role"] = "member"
    entry["port"] = port
    if user != bot_id:
        entry["user"] = user
    if channels:
        entry["channels"] = list(channels)
    return entry


def _scan_workspace_for_secrets(bot_id: str) -> None:
    """Run audit_workspace_secrets() against a freshly-provisioned bot.

    Warning-only by design — the bot already exists and is functional
    by the time we reach this point. We surface the findings so the
    operator can clean them up, but we don't fail the wizard. The
    operator can re-run `evolve-admin scan-secrets --bot <id>` after
    cleanup to confirm.
    """
    try:
        from audit import audit_workspace_secrets
    except ImportError:
        # Best-effort — if evolve-analyzer isn't installed, skip rather
        # than block setup.
        return

    shared_dir = Path(get_profile().shared_dir_default)
    findings = audit_workspace_secrets(bot_id, shared_dir)
    crits = [f for f in findings if f.level == "critical"]
    if not crits:
        _ok(f"  Workspace scan: clean (no credentials detected)")
        return

    _warn(f"  Workspace scan flagged {len(crits)} credential(s) in {bot_id}'s workspace:")
    for f in crits:
        _warn(f"    🔴 {f.message}")
    _warn(f"  Setup is complete, but rotate any real tokens and clean these files")
    _warn(f"  before adding more bots. Re-check with:")
    _warn(f"    sudo evolve-admin scan-secrets --bot {bot_id}")


# ── Prerequisite checks ───────────────────────────────────────────────────────

@dataclass
class PrereqResult:
    name: str
    ok: bool
    detail: str = ""


def check_prerequisites() -> list[PrereqResult]:
    results = []

    # Running as root/sudo
    is_root = os.geteuid() == 0
    results.append(PrereqResult(
        "Running as root/sudo", is_root,
        "OK" if is_root else "Run with: sudo evolve-admin setup"
    ))

    # Python 3.9+
    major, minor = sys.version_info[:2]
    py_ok = (major, minor) >= (3, 9)
    results.append(PrereqResult(
        f"Python {major}.{minor}", py_ok,
        "OK" if py_ok else "Python 3.9+ required"
    ))

    # evolve-admin itself accessible
    ea = shutil.which("evolve-admin")
    results.append(PrereqResult(
        "evolve-admin in PATH", ea is not None,
        ea or "Not found — install: pip3 install -e packages/admin"
    ))

    # Shared dir parent writable
    shared_parent = DEFAULT_SHARED_DIR.parent
    parent_ok = os.access(shared_parent, os.W_OK)
    results.append(PrereqResult(
        f"{shared_parent} writable", parent_ok,
        "OK" if parent_ok else f"Cannot write to {shared_parent}"
    ))

    # openclaw CLI available
    oc = shutil.which("openclaw")
    results.append(PrereqResult(
        "openclaw CLI", oc is not None,
        oc or "Not found — is OpenClaw installed?"
    ))

    # LaunchDaemons writable (for launchd jobs)
    ld_ok = Path("/Library/LaunchDaemons").exists() and os.access("/Library/LaunchDaemons", os.W_OK)
    results.append(PrereqResult(
        "/Library/LaunchDaemons writable", ld_ok,
        "OK" if ld_ok else "Need root for launchd job installation"
    ))

    return results


# ── Main wizard ───────────────────────────────────────────────────────────────

def run_wizard(network_path: Optional[Path] = None) -> None:
    total_steps = 8

    net_path = network_path or DEFAULT_NETWORK_CONFIG
    existing = load_network(net_path) if net_path.exists() else {}
    is_modify = bool(existing)

    _header("Evolve Pod Setup Wizard")
    if is_modify:
        _info(f"Existing network: {_c(existing.get('networkId','?'), 'bold')}")
        _info(f"Members: {', '.join(existing.get('members',[]))}")
        print()
        _info("What would you like to do?")
        print(f"  {_c('[m]', 'bold')} Modify network configuration (add/remove bots, change settings)")
        print(f"  {_c('[s]', 'bold')} Security configuration only")
        print(f"  {_c('[f]', 'bold')} Full re-run (reconfigure everything)")
        print(f"  {_c('[q]', 'bold')} Quit")
        print()
        mode_choice = _ask("Choice", "m").strip().lower()
        if mode_choice == "q":
            _info("Cancelled.")
            sys.exit(0)
        elif mode_choice == "s":
            _run_security_wizard_only(net_path, existing)
            return
        elif mode_choice == "m":
            _info("Running in modify mode — existing settings preserved unless changed.")
        # 'f' falls through to full wizard
        print()
    else:
        _info("This wizard will guide you through setting up the Evolve")
        _info("Better Engine across your OpenClaw pod.")
        _info("")
        _info("You can re-run this wizard at any time to modify configuration.")
        _info("Press Ctrl+C at any point to exit without making changes.")
        print()

    # ── Step 1: Prerequisites ─────────────────────────────────────────────────
    _step(1, total_steps, "Prerequisites")
    prereqs = check_prerequisites()
    all_ok = True
    for p in prereqs:
        if p.ok:
            _ok(p.name)
        else:
            _warn(f"{p.name} — {p.detail}")
            if p.name.startswith("Running"):
                all_ok = False  # hard requirement

    if not all_ok:
        print()
        _err("Must be run as root. Try: sudo evolve-admin setup")
        sys.exit(1)

    non_critical_fails = [p for p in prereqs if not p.ok and not p.name.startswith("Running")]
    if non_critical_fails:
        print()
        _warn("Some prerequisites are missing but setup can continue.")
        if not _confirm("Continue anyway?", default=True):
            sys.exit(0)

    # ── Step 2: Discover OpenClaw installs ─────────────────────────────────
    _step(2, total_steps, "Discover OpenClaw installs")
    _info(f"Scanning {get_profile().user_home_root}/ for OpenClaw installations...")
    candidates = find_oc_candidates(network=existing)

    if not candidates:
        _info("No OpenClaw installs found — you can create one in the next step.")
    else:
        _info("")
        _info(f"Found {len(candidates)} install(s):")
        for c in candidates:
            port_str = f"port {c.port}" if c.port else "port unknown"
            tags: list[str] = []
            if c.is_pod_member:
                tags.append("already in pod")
            if c.looks_like_admin:
                tags.append("admin account")
            tag_str = f"  [{', '.join(tags)}]" if tags else ""
            sugg = f" → suggested bot_id={c.suggested_bot_id}" if c.suggested_bot_id else ""
            _info(f"  • user={c.user:<12} {port_str}{sugg}{tag_str}")

    # ── Step 3: Choose bots to include ───────────────────────────────────────
    _step(3, total_steps, "Select bots to include in the pod")

    # Discovery is advisory. Each entry becomes a pod member only if the
    # operator picks it AND chooses a bot_id under which to register it.
    selected_bots: list[DiscoveredBot] = []
    addable = [c for c in candidates if not c.is_pod_member]

    def _ask_bot_id_for(c: OcCandidate) -> Optional[DiscoveredBot]:
        """Prompt the operator for a bot_id to claim this candidate under.
        Returns None if they decline (empty input)."""
        default_id = c.suggested_bot_id or c.user
        bot_id = _ask(
            f"Bot ID for user={c.user} port={c.port or '?'} (Enter to skip)",
            default_id,
        ).strip()
        if not bot_id:
            return None
        return DiscoveredBot(
            bot_id=bot_id,
            user=c.user,
            home=c.config_path.parent.parent,
            oc_config=c.config_path,
            workspace=get_bot_workspace(c.user),
            port=c.port,
            is_known=True,
        )

    if not addable:
        if candidates:
            _info("All discovered installs are already pod members. You can add a new bot next.")
        else:
            _info("No existing installs to claim. You'll create one next.")
    else:
        _info("Discovered installs available to claim:")
        for i, c in enumerate(addable, 1):
            port_str = f" port {c.port}" if c.port else ""
            sugg = f" (suggested bot_id={c.suggested_bot_id})" if c.suggested_bot_id else ""
            admin = " [admin account — confirm before claiming]" if c.looks_like_admin else ""
            _info(f"  [{i}] user={c.user}{port_str}{sugg}{admin}")
        _info("  [a] All of the above (you'll be asked to name each one)")
        _info("  [m] Skip — enter bot IDs manually")
        print()
        _info("To select specific candidates, enter numbers separated by spaces or commas")
        choice = _ask("Claim which installs?", "a").lower()

        chosen: list[OcCandidate] = []
        if choice == "a":
            chosen = list(addable)
        elif choice in ("m", ""):
            chosen = []
        else:
            indices = [
                int(x.strip()) - 1
                for x in choice.replace(",", " ").split()
                if x.strip().isdigit()
            ]
            chosen = [addable[i] for i in indices if 0 <= i < len(addable)]

        for c in chosen:
            spec = _ask_bot_id_for(c)
            if spec is not None:
                selected_bots.append(spec)

    # Manual entry (or supplement discovered)
    if not selected_bots:
        _info("Enter bot IDs, one per line. Empty line to finish.")
        while True:
            bot_id = _ask("Bot ID (or Enter to finish)", "")
            if not bot_id:
                break
            user_input = _ask(f"  {_os_user_noun()} user for {bot_id} (Enter for same as bot_id)", "")
            user = user_input or bot_id
            port = _ask(f"  Gateway port for {bot_id}", "")
            ws = get_bot_workspace(user)
            selected_bots.append(DiscoveredBot(
                bot_id=bot_id,
                user=user,
                home=user_home(user),
                oc_config=user_home(user) / ".openclaw" / "openclaw.json",
                workspace=ws,
                port=int(port) if port else None,
            ))

    # ── Bot creation ──────────────────────────────────────────────────────────
    new_bots: list[dict] = []

    if not candidates:
        # No existing installs — creation is mandatory
        print()
        _info("No OpenClaw instances found on this machine.")
        _info("Let's create your first bot.")
        existing_keys = _find_existing_keys()
        bot = _create_bot_flow(existing_keys)
        if bot:
            new_bots.append(bot)
            selected_bots.append(DiscoveredBot(
                bot_id=bot["name"],
                user=bot["name"],
                home=user_home(bot["name"]),
                oc_config=user_home(bot["name"]) / ".openclaw" / "openclaw.json",
                workspace=None,
                port=bot["port"],
            ))
        if not selected_bots:
            _err("No bots configured. Exiting.")
            sys.exit(1)
    else:
        if not selected_bots:
            _err("No bots selected. Exiting.")
            sys.exit(1)
        # Offer to add more bots
        print()
        while _confirm("Add a new bot to the pod?", default=False):
            existing_keys = _find_existing_keys()
            bot = _create_bot_flow(existing_keys)
            if bot:
                new_bots.append(bot)
                selected_bots.append(DiscoveredBot(
                    bot_id=bot["name"],
                    user=bot["name"],
                    home=user_home(bot["name"]),
                    oc_config=user_home(bot["name"]) / ".openclaw" / "openclaw.json",
                    workspace=None,
                    port=bot["port"],
                ))
            else:
                break

    print()
    _info("Selected bots:")
    for b in selected_bots:
        _ok(f"{b.bot_id} (port {b.port or '?'})")

    # ── Step 4: Pod identity ──────────────────────────────────────────────
    _step(4, total_steps, "Pod identity")
    _info("Give this pod a short identifier (no spaces).")
    _info("Used in reports and proposals. Example: home-network, mynetwork")
    network_id = _ask("Network ID", "my-network")
    network_id = network_id.replace(" ", "-").lower()

    # ── Step 5: Shared directory ──────────────────────────────────────────────
    _step(5, total_steps, "Shared directory")
    _info("All bots write metrics and proposals to a shared directory.")
    _info(f"Default: {DEFAULT_SHARED_DIR}")
    _info("This must be readable/writable by all bot users.")
    _info("Press Enter to use the default.")
    shared_dir_str = _ask("Path", str(DEFAULT_SHARED_DIR))
    shared_dir = Path(shared_dir_str)

    # ── Step 6: Alert channel ───────────────────────────────────────────────
    _step(6, total_steps, "Alert channel")
    _info("Evolve sends alerts when proposals are ready, gateways go down,")
    _info("or outcomes need your review.")
    _info("")

    # Auto-detect channel from first bot's openclaw.json
    detected_channel = ""
    detected_chat_id = ""
    first_bot = selected_bots[0]
    first_bot_id = first_bot.bot_id
    # Use the DiscoveredBot's resolved home path — bot_id may differ from the
    # macOS account name (e.g. team_bot_b/personal_bot_user). bot.home is already correct.
    primary_oc_json = first_bot.home / ".openclaw" / "openclaw.json"
    if primary_oc_json.exists():
        try:
            oc_cfg = json.loads(primary_oc_json.read_text())
            channels = oc_cfg.get("channels", {})
            # Check common channel keys
            for ch_name in ("telegram", "slack", "discord", "whatsapp", "signal"):
                ch = channels.get(ch_name, {})
                if ch and ch.get("enabled", True):
                    detected_channel = ch_name
                    # Try to extract chat/user id from channel config
                    detected_chat_id = str(
                        ch.get("chatId") or ch.get("userId") or
                        ch.get("defaultChannel") or ch.get("channelId") or ""
                    )
                    # Fallback: read from sessions.json (Telegram direct session key)
                    if not detected_chat_id and ch_name == "telegram":
                        sessions_file = first_bot.home / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
                        if sessions_file.exists():
                            try:
                                sdata = json.loads(sessions_file.read_text())
                                for skey in sdata:
                                    if "telegram:direct:" in skey:
                                        detected_chat_id = skey.split(":")[-1]
                                        break
                            except Exception:
                                pass
                    break
        except Exception:
            pass

    if detected_channel:
        _ok(f"Detected primary channel: {detected_channel}" +
            (f" (chat ID: {detected_chat_id})" if detected_chat_id else ""))
        _info("Press Enter to use detected values, or enter overrides.")
    else:
        _info("Could not auto-detect channel from bot config.")
        _info("Supported: telegram, slack, discord, whatsapp, signal")

    alert_channel = _ask("Alert channel", detected_channel or "telegram")
    chat_id = _ask("Chat / user ID (leave blank to skip alerts)", detected_chat_id)

    # ── Step 7: Security configuration ───────────────────────────────────────
    existing_sec = existing.get("security", {})
    _step(7, total_steps, "Security configuration")
    _info("Evolve screens every proposal through a security reviewer before")
    _info("it reaches you. Sensible defaults are applied automatically.")
    _info("")
    _info("Default: proposals rated 'high' or 'critical' risk are auto-rejected.")
    _info("You can tune this later with: evolve-admin config")
    _info("")
    _ok("Using default security configuration")

    security_cfg = {
        "mode": existing_sec.get("mode", "primary"),
        "botId": existing_sec.get("botId"),
        "autoRejectRisk": existing_sec.get("autoRejectRisk", ["high", "critical"]),
        # Fall back to the operator-chosen shared dir, not a macOS literal, so
        # a Linux pod (shared_dir=/var/lib/evolve) or a non-default macOS dir
        # gets a rulesFile that actually exists. Matches review.py's default.
        "rulesFile": existing_sec.get(
            "rulesFile", str(shared_dir / "security_rules.json")
        ),
    }
    security_bot_id: Optional[str] = security_cfg["botId"]

    # ── Step 8: Confirm and execute ───────────────────────────────────────────
    _step(8, total_steps, "Review and confirm")
    print()
    _info(f"  Network ID:    {_c(network_id, 'bold')}")
    _info(f"  Bots:          {_c(', '.join(b.bot_id for b in selected_bots), 'bold')}")
    _info(f"  Shared dir:    {_c(str(shared_dir), 'bold')}")
    _info(f"  Alerts:        {_c(alert_channel + ':' + chat_id, 'bold')}")
    print()

    if not _confirm("Proceed with setup?", default=True):
        _info("Cancelled. Nothing was changed.")
        sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────────────────
    print()
    _header("Running Setup")

    errors: list[str] = []

    # Build network config
    net_path = network_path or DEFAULT_NETWORK_CONFIG
    existing = load_network(net_path) if net_path.exists() else {}

    bots_cfg: dict = existing.get("bots", {})
    # `DiscoveredBot` carries no channel, so the operator's pick from
    # `_create_bot_flow` has to be re-joined by bot_id here — this is the
    # second of the two places the pick used to die on the floor (the other
    # was setup_wizard's BotSpec construction).
    new_bot_channels = {
        nb["name"]: _selected_bot_channels(nb)
        for nb in new_bots if nb.get("name")
    }
    members: list[str] = []
    for b in selected_bots:
        bots_cfg[b.bot_id] = _merge_bot_entry(
            bots_cfg.get(b.bot_id),
            user=b.user,
            bot_id=b.bot_id,
            port=b.port,
            channels=new_bot_channels.get(b.bot_id, ()),
        )
        members.append(b.bot_id)

    network_data = {
        **existing,
        "networkId": network_id,
        "members": members,
        "sharedDir": str(shared_dir),
        "thresholds": existing.get("thresholds", {}),
        "classifiers": existing.get("classifiers", {
            "tier": {"tier": "tier3", "fallback": "keyword"},
            "judge": {"tier": "tier0"},
        }),
        "alerts": {"channel": alert_channel, "chatId": chat_id},
        "security": security_cfg,
        "heal": existing.get("heal", {
            "failuresBeforeProposal": 3,
            "windowHours": 24,
            "slowThresholdMs": 3000,
            "restartCooldownMin": 10,
            "checkTimeoutSec": 5,
        }),
        "bots": bots_cfg,
    }

    net_path.parent.mkdir(parents=True, exist_ok=True)
    save_network(network_data, net_path)
    _ok(f"Network config written: {net_path}")

    # Create shared directory
    _info("")
    _info("Creating shared directory...")
    r = deploy_shared_dir(shared_dir, dry_run=False)
    for s in r.steps:
        _info(f"  {s}")
    if r.errors:
        for e in r.errors:
            _err(e)
        errors.extend(r.errors)
    else:
        _ok(f"Shared directory ready: {shared_dir}")

    # Deploy to each bot
    _info("")
    _info("For each bot, you can optionally configure a git remote for nightly backups.")
    _info("The SSH deploy key will be generated automatically — you add the public key to GitHub.")
    _info("Leave blank to skip (add 'bots.<bot_id>.backupRepoUrl' to network.json later).")
    _info("")
    new_bot_backup_urls = {b["name"]: b.get("backup_repo_url", "") for b in new_bots}
    for b in selected_bots:
        if b.bot_id in new_bot_backup_urls:
            backup_url = new_bot_backup_urls[b.bot_id]
            if backup_url:
                _info(f"  {b.bot_id}: using backup URL set during bot creation")
        else:
            backup_url = _ask(
                f"{b.bot_id} backup git remote URL (e.g. git@github.com:org/{b.bot_id}-workspace.git)",
                "",
            )
        _info(f"Deploying to {_c(b.bot_id, 'bold')}...")
        r = deploy_bot(b.bot_id, "member", b.port, net_path, dry_run=False, backup_repo_url=backup_url)
        for s in r.steps:
            _info(f"  {s}")
        if r.errors:
            for e in r.errors:
                _err(e)
            errors.extend(r.errors)
        else:
            _ok(f"{b.bot_id} deployed")
            # Seed the operator's own Telegram identity across all three
            # stores (owner + chat_id + DM approval) when they supplied their
            # chat id during bot creation. This is the highest-trust identity
            # signal in the flow — the operator's own id on their own pod — so
            # we record ownership AND auto-approve the DM (the "Require
            # approval" newcomer policy still gates everyone else). Without
            # this the bot comes up mute (DM(0)) and "Messaging channel
            # paired" stays Pending. Best-effort: a failure is logged, not
            # fatal — the bot is already deployed and the operator can repair
            # later via the Users page Set-owner action (same primitive).
            new_bot = next(
                (nb for nb in new_bots if nb["name"] == b.bot_id), None
            )
            seed_chat_id = (new_bot or {}).get("chat_id", "")
            if seed_chat_id and (new_bot or {}).get("channel") == "telegram":
                from .evo.seed_identity import seed_channel_identity
                seed_net = load_network(net_path)
                seed_res = seed_channel_identity(
                    seed_net, b.bot_id, "telegram", seed_chat_id,
                    network_path=net_path,
                )
                if seed_res.changed:
                    save_network(seed_net, net_path)
                if seed_res.errors:
                    for se in seed_res.errors:
                        _warn(f"  identity seed ({b.bot_id}): {se}")
                else:
                    _ok(f"{b.bot_id}: owner + DM approval seeded from your Telegram id")

    # Run first measurement
    _info("")
    _info("Running first measurement to verify setup...")
    first_ws = get_bot_workspace(selected_bots[0].bot_id)
    measure_script = first_ws / "evolve" / "measure.py" if first_ws else None
    if measure_script and measure_script.exists():
        proc = subprocess.run(
            [sys.executable, str(measure_script), "--network", str(net_path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            _ok("First measurement completed")
        else:
            _warn("Measurement ran but had warnings (this is normal on first run with no data yet)")
            if proc.stderr:
                _info(f"  {proc.stderr[:200]}")
    else:
        _warn("Measurement script not found yet — will run tonight at 1am")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    _header("Setup Complete")

    if errors:
        print()
        _warn(f"{len(errors)} error(s) occurred during setup:")
        for e in errors:
            _err(e)
        print()
        _info("Setup completed with errors. Check above for details.")
        _info("You can re-run 'evolve-admin setup' to retry failed steps.")
    else:
        _ok("All steps completed successfully!")

    # Ensure venv and symlink are world-executable so all users can run evolve-admin
    import shutil as _shutil, subprocess as _sp
    venv_path = _shutil.which("evolve-admin")
    if venv_path:
        # Walk up to find the venv bin dir
        from pathlib import Path as _P
        venv_bin = _P(venv_path).parent
        venv_root = venv_bin.parent
        if (venv_root / "pyvenv.cfg").exists():
            _sp.run(["chmod", "755", str(venv_root)], check=False)
            _sp.run(["chmod", "-R", "a+rX", str(venv_root)], check=False)
            _ok(f"Venv permissions set (world-readable): {venv_root}")
    # Also fix evolve-repo permissions
    import shutil as _sh2
    _prof = get_profile()
    repo_candidates = [_P(_prof.deploy_checkout_default), _P(_prof.venv_dir)]
    for _rp in repo_candidates:
        if _rp.exists():
            _sp.run(["chmod", "755", str(_rp)], check=False)
            _sp.run(["chmod", "-R", "a+rX", str(_rp)], check=False)
    # Grant evolve write ACL on the repo dirs the upgrade job needs to write:
    # .git/ (for git pull FETCH_HEAD) and packages/plugin/ (for npm install).
    # Run as the current admin user who owns the repo — no sudo needed.
    _repo_write_acl = (
        "evolve allow list,search,add_file,add_subdirectory,delete_child,"
        "readattr,writeattr,readextattr,writeextattr,readsecurity,delete,"
        "write,file_inherit,directory_inherit"
    )
    for _rp in [
        _P(_prof.deploy_checkout_default) / ".git",
        _P(_prof.deploy_checkout_default) / "packages" / "plugin",
    ]:
        if _rp.exists():
            _sp.run(["chmod", "+a", _repo_write_acl, str(_rp)], check=False)

    print()
    _info(_c("What happens next:", "bold"))
    _info("")
    _info("  Automatic (no action needed):")
    _info("   • Daily 1am    → metrics collected for each bot")
    _info("   • Sunday 2am   → analysis runs, proposals generated")
    _info("   • Daily 6:15am → health report sent if anything notable")
    _info("")
    _info("  To check status now:")
    _info(f"   evolve-admin status")
    _info(f"   evolve-admin serve --open   (admin UI at 127.0.0.1:19099)")
    _info("")
    _info("  To review proposals when they arrive:")
    _info("   ocadmin → [e] Evolve network → [p] Proposals")
    _info("   or: evolve-admin serve → Proposals tab")
    _info("")

    _info(f"  Network config: {net_path}")
    _info(f"  Shared data:    {shared_dir}")
    _info("  Admin UI:       evolve-admin serve --open")
    _info("                  (runs at http://127.0.0.1:19099 — run as any user)")

    # ── Admin-UI pairing (roadmap 2.6) ─────────────────────────────────────────
    # Auth is ON BY DEFAULT: the admin server enforces a paired device cookie.
    # Mint the key + print the pairing code now so first-run is forced pairing,
    # not lockout-discovery. The operator enters this code in the browser the
    # first time they open the admin UI.
    try:
        from .web import admin_auth
        if not admin_auth.is_optout(shared_dir):
            code = admin_auth.current_pairing_code(shared_dir)
            import subprocess as _sp3
            # The wizard runs as root, so this succeeds — full path per the
            # macOS-paths convention (CLAUDE.md). The daemon (evolve) reads the
            # key after the chown.
            _sp3.run(
                ["/usr/sbin/chown", "evolve:staff", str(admin_auth._key_path(shared_dir))],
                check=False, capture_output=True,
            )
            print()
            _info(_c("Admin UI access (auth is ON by default):", "bold"))
            _info(f"   Pairing code:  {_c(code, 'bold')}")
            _info("   Open the admin UI; it redirects to /pair — enter this code.")
            _info("   (valid a few minutes; `sudo evolve-admin pair` prints a fresh one.)")
            _info("   To run open instead: evolve-admin auth disable --accept-risk \"...\"")
    except Exception as _exc:
        _warn(f"Could not mint admin pairing code ({_exc}); run `evolve-admin pair`.")

    # Completed actions logged to /Users/Shared/evolve/logs/admin-actions.jsonl.

    print()


def _run_security_wizard_only(net_path: Path, existing: dict) -> None:
    """Quick flow: update security configuration only, then save and redeploy."""
    _header("Security Configuration")
    existing_sec = existing.get("security", {})

    print(f"  Current mode: {_c(existing_sec.get('mode', 'primary'), 'bold')}")
    if existing_sec.get("botId"):
        print(f"  Security bot: {_c(existing_sec['botId'], 'bold')}")
    print(f"  Auto-reject:  {_c(', '.join(existing_sec.get('autoRejectRisk', [])), 'bold')}")
    print()

    _info("  [p] Primary mode  — review.py runs on the primary bot")
    _info("  [d] Dedicated mode — review.py runs on a separate security bot")
    sec_input = _ask("Mode", "p" if existing_sec.get("mode", "primary") == "primary" else "d").strip().lower()
    security_mode = "dedicated" if sec_input == "d" else "primary"
    security_bot_id: Optional[str] = None

    if security_mode == "dedicated":
        current_reviewer = existing_sec.get("botId") or ""
        security_bot_id = _ask("Security reviewer bot ID", current_reviewer)

    default_reject = ",".join(existing_sec.get("autoRejectRisk", ["high", "critical"]))
    risk_input = _ask("Auto-reject risk levels (comma-separated)", default_reject)
    auto_reject_risk = [r.strip() for r in risk_input.split(",") if r.strip()]

    existing["security"] = {
        "mode": security_mode,
        "botId": security_bot_id,
        "autoRejectRisk": auto_reject_risk,
        # Reconfigure path: only `existing` is in scope, so derive the fallback
        # shared dir from the loaded network.json (sharedDir is always written
        # by setup). Avoids a macOS literal on Linux pods. See review.py default.
        "rulesFile": existing_sec.get(
            "rulesFile",
            str(Path(existing.get("sharedDir", str(DEFAULT_SHARED_DIR)))
                / "security_rules.json"),
        ),
    }

    if not _confirm("Save and redeploy?", default=True):
        _info("Cancelled.")
        return

    save_network(existing, net_path)
    _ok(f"Network config updated: {net_path}")

    # Redeploy to all members (and security bot if dedicated)
    targets = list(existing.get("members", []))
    if security_mode == "dedicated" and security_bot_id and security_bot_id not in targets:
        targets.append(security_bot_id)

    for bot_id in targets:
        if not bot_id:
            continue
        from .deploy import deploy_bot
        r = deploy_bot(bot_id=bot_id, role="member", port=None, network_path=net_path)
        if r.errors:
            for e in r.errors:
                _err(e)
        else:
            _ok(f"Redeployed to {bot_id}")
