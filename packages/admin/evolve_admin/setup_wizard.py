"""
setup_wizard.py — Full "fresh dedicated Mac to running pod" wizard.

Handles everything needed to go from a bare machine to a running Evolve pod:
  1.  Prerequisites check (macOS, Node.js, npm, Python, OpenClaw)
  2.  Pod identity + bot roster
  3.  OpenClaw global install via npm (if missing)
  4.  macOS user account creation per bot (idempotent)
  5.  Per-user OC directory structure + openclaw.json
  6.  Telegram channel configuration for the primary bot
  7.  Shared directory setup (/Users/Shared/evolve/)
  8.  network.json written
  9.  Evolve deployed to all bots (plugin, scripts, launchd)
  10. Gateways started, endpoints verified

Entry point:
    from .setup_wizard import run_fresh_wizard
    run_fresh_wizard(non_interactive=False)

Run with sudo: sudo evolve-admin setup --fresh
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import DEFAULT_NETWORK_CONFIG, DEFAULT_SHARED_DIR, load_network, save_network, get_bot_workspace, user_home
from .deploy import EVOLVE_SERVICE_USER, PLUGIN_SRC_DIR, PLUGIN_INSTALL_DIR, set_evolve_read_acl, per_bot_gateway_plist_label
from .secret_config_perms import BOT_PRIVATE_SECRET_RELPATHS, chmod_secret_config
from .sudo_dest import redirect_refusal  # D-2 gate: dir-shaped dest of a root mkdir/chmod/chown
from .installer import setup_shared
from .runtime import JobSpec, get_launchd_scheduler, render_launchd_plist
from .web.server import _apply_credential_to_oc_dict

console = Console()

# Per-bot workspace identity/setup docs the content scanner reads
# (packages/analyzer/content_scan). The scanner reads each of these from every
# bot's ~/.openclaw/workspace/ and fires content_scan_file_disappeared (alert)
# when one is missing or unreadable. The direct evolve-user read is primary
# (via set_evolve_read_acl), but on Linux a transient ACL-mask clamp can make
# that read raise PermissionError, so scanner._read_text falls back to
# `sudo /bin/cat <abs_path>` — which needs a per-file NOPASSWD grant or it dies
# with sudo_rc=1 and the alert can NEVER clear during the clamp window.
#
# These are non-secret identity/setup docs: SOUL/AGENTS/MEMORY/README from
# bot_doc_seeding.EVOLVE_SEEDED_DOCS + the catalog identity set
# USER/IDENTITY/HEARTBEAT/TOOLS. This tuple MUST equal the catalog's per-bot
# scanned set (content_scan.default_patterns scope.scanned_files_per_bot) —
# the lockstep is pinned by test_sudoers_workspace_doc_cat_grants.py so a new
# scanned doc forces a conscious grant + golden-fixture update. Pod-wide docs
# (POD_CONDUCT.md, RUNTIME_NOTES.md) live under the evolve-owned shared_dir and
# are read directly, so they need NO grant here. Kept sorted for a
# deterministic render; enumerated per file (never a workspace/* wildcard) so
# the grant stays auditable and can't read cred/token files under workspace/.
CONTENT_SCAN_WORKSPACE_DOCS: tuple[str, ...] = (
    "AGENTS.md", "HEARTBEAT.md", "IDENTITY.md", "MEMORY.md",
    "README.md", "SOUL.md", "TOOLS.md", "USER.md",
)

# ── Data types ────────────────────────────────────────────────────────────────

def provisionable_channel_ids() -> tuple[str, ...]:
    """Channel ids a bot's gateway can actually be provisioned on.

    The registry (``evolve_admin.channel_registry``) is the ONE channel
    vocabulary — invariant 7, docs/spec-users-meta-2026-06-15.md §5. We select
    on the ``install`` capability rather than taking ``known_ids()`` wholesale,
    because ``email`` and ``webhook`` are delivery sinks Evolve *labels* but
    never provisions: a bot does not "run on" email, and admitting those two
    into a BotSpec would let the wizard record a channel the gateway can never
    serve. Narrow consumer, narrow predicate — the registry's central rule.
    """
    from .channel_registry import ids_where
    return ids_where(lambda c: c.install is not None)


def _channel_tokens(raw: object) -> set[str]:
    """Lowercased, non-empty channel tokens out of whatever was supplied.

    Accepts a single id (``"telegram"``), a sequence/set of ids, or a MAPPING
    keyed by channel id — the last because OC's own ``openclaw.json::channels``
    is exactly that shape, so it is the form an operator is most likely to copy
    into a manifest. Silently returning ``()`` for it would be data loss in the
    one case we can most easily predict.
    """
    if raw is None:
        return set()
    if isinstance(raw, str):
        items: list = [raw]
    elif isinstance(raw, dict):
        items = list(raw.keys())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        items = []
    return {c.strip().lower() for c in items if isinstance(c, str) and c.strip()}


def normalize_channels(raw: object) -> tuple[str, ...]:
    """Coerce wizard input into a validated, registry-keyed tuple.

    LENIENT by design — this is the *interactive* boundary. The wizard encodes
    "operator picked no channel" as the string ``"none"``, which is the absence
    of a channel and deliberately not a registry row, so unrecognized tokens
    drop out rather than raising. The manifest loader is strict instead (see
    ``_manifest_channels``): a machine-authored file with a typo should say so
    loudly, an interactive sentinel should not.

    Result is deduped and ordered by the registry's canonical display order, so
    the on-disk value is stable regardless of the order the operator supplied.
    """
    seen = _channel_tokens(raw)
    return tuple(cid for cid in provisionable_channel_ids() if cid in seen)


def _manifest_channels(raw: object, bot_id: str, index: int) -> tuple[str, ...]:
    """STRICT channel parse for ``--bots-manifest`` entries.

    Every other malformed field in a manifest raises ManifestError with a
    pointer at the offending entry; an unknown channel id gets the same
    treatment. A silently-dropped typo here would leave the operator with an
    install that looks fine and a channel that was never recorded — the exact
    "written and never read" failure this whole field exists to end.
    """
    tokens = _channel_tokens(raw)
    valid = set(provisionable_channel_ids())
    unknown = sorted(tokens - valid)
    if unknown:
        raise ManifestError(
            f"bots[{index}] ({bot_id}) has unknown channel(s) "
            f"{', '.join(repr(u) for u in unknown)}; valid ids are "
            f"{', '.join(provisionable_channel_ids())} "
            f"(omit the key entirely for no channel)"
        )
    return tuple(cid for cid in provisionable_channel_ids() if cid in tokens)


@dataclass
class BotSpec:
    """A pod member as the operator has specified it.

    `name` is the logical bot_id (what shows up in network.json). `user` is
    the macOS account hosting it — same as `name` by default. They differ
    when one bot lives on a personal/shared account (e.g. bot_id=team_bot_b runs
    on a personal/shared account); without `user` the wizard would silently treat
    the macOS account as a separate bot.

    `channels` is the operator's messaging-channel pick, carried from
    `wizard._create_bot_flow()` through to `bots.<id>.channels` in
    network.json. It is PLURAL by construction — see `normalize_channels`
    and the cardinality note in the class body below.
    """
    name: str
    port: int
    role: str = "member"
    multi_user: bool = False
    user: str = ""  # empty → defaults to `name` at consumption time
    # Registry-keyed, plural. Plural because the thing being recorded is
    # already plural everywhere downstream: OC's own `openclaw.json::channels`
    # is a MAP keyed by channel id, a bot may run Telegram and Slack at once,
    # and the Channels UI adds a second one without touching the wizard. A
    # scalar would have to be migrated the first time that happens. Same
    # reasoning that took person external_ids plural in #3500.
    channels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Normalize at the boundary so every construction site — wizard
        # caller, manifest loader, tests — lands on the same validated shape.
        self.channels = normalize_channels(self.channels)

    @property
    def effective_user(self) -> str:
        return self.user or self.name


class ManifestError(ValueError):
    """Raised when --bots-manifest can't be parsed or is missing required fields."""


def load_bots_manifest(path: Path) -> list[BotSpec]:
    """Parse a --bots-manifest JSON file into a list of BotSpec.

    Schema:
      {
        "bots": [
          {"bot_id": "admin_bot", "port": 19000, "user": "admin_bot",
           "role": "member", "multi_user": false},
          ...
        ]
      }

    Required per entry: `bot_id` (str) and `port` (int).
    Optional: `user`, `role`, `multi_user`, `channels`. `name` is accepted as a
    synonym for `bot_id` since BotSpec stores it under `name` internally, and
    `channel` (singular) as a synonym for `channels` so a hand-written manifest
    can carry one id without a list. An unknown channel id RAISES here (unlike
    the lenient interactive path) — see `_manifest_channels`.

    Raises ManifestError on any structural problem with a clear message
    pointing at the offending entry.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest {path} is not valid JSON: {e}") from e

    raw_bots = data.get("bots") if isinstance(data, dict) else None
    if not isinstance(raw_bots, list) or not raw_bots:
        raise ManifestError(
            f"manifest {path} must contain a non-empty top-level `bots` list"
        )

    out: list[BotSpec] = []
    for i, raw in enumerate(raw_bots):
        if not isinstance(raw, dict):
            raise ManifestError(
                f"bots[{i}] must be an object, got {type(raw).__name__}"
            )
        bot_id = raw.get("bot_id") or raw.get("name")
        port = raw.get("port")
        if not isinstance(bot_id, str) or not bot_id:
            raise ManifestError(
                f"bots[{i}] missing required string `bot_id` (got {bot_id!r})"
            )
        if not isinstance(port, int) or port <= 0:
            raise ManifestError(
                f"bots[{i}] ({bot_id}) requires positive integer `port` "
                f"(got {port!r})"
            )
        out.append(BotSpec(
            name=bot_id,
            user=raw.get("user", "") or "",
            port=port,
            role=raw.get("role") or "member",
            multi_user=bool(raw.get("multi_user", False)),
            channels=_manifest_channels(
                raw.get("channels") if raw.get("channels") is not None
                else raw.get("channel"),
                bot_id, i,
            ),
        ))
    return out


def _botspec_from_wizard_bot(bot: dict, *, role: str = "member") -> BotSpec:
    """Convert `wizard._create_bot_flow()`'s return dict into a BotSpec.

    THE boundary this whole field exists to close. `_create_bot_flow` has
    always returned the operator's channel pick (`bot["channel"]`, pinned by
    test_setup_wizard_chat_id_carry.py), but the installer used to rebuild a
    BotSpec from name/port/multi_user only, so the pick died right here and
    never reached network.json. META:users deleted the matching *reader*
    (`_primary_channel_hint`, PR #3492) because nothing had ever written the
    key; this is the writer that makes such a reader truthful.

    `_create_bot_flow` encodes "no channel" as the string `"none"`, which is
    not a registry id — `normalize_channels` drops it to an empty tuple.
    """
    return BotSpec(
        name=bot["name"],
        port=bot["port"],
        role=role,
        multi_user=bot.get("multi_user", False),
        channels=normalize_channels(bot.get("channel")),
    )


def _bot_network_entry(bot: BotSpec) -> dict:
    """Build one `bots.<id>` block of network.json from a BotSpec.

    `channels` is written only when non-empty, matching how `user` and
    `backupRepoUrl` are handled on the same entry: an absent key means "this
    pod has nothing to say", which is exactly right both for a pod installed
    before the field existed and for an operator who picked "configure later".
    Writing `[]` would assert a positive "no channels" that we cannot
    distinguish from the former.
    """
    entry: dict = {
        "role": bot.role,
        "port": bot.port,
        "multiUser": bot.multi_user,
    }
    # Record macOS user only when it differs from the bot_id (the
    # bot-id-vs-host-user split case). Without this, downstream lookups treat the
    # bot_id as both the logical name and the Unix user, which fails
    # the moment one bot lives on a personal/shared account.
    if bot.user and bot.user != bot.name:
        entry["user"] = bot.user
    if bot.channels:
        entry["channels"] = list(bot.channels)
    return entry


# ── Output helpers ────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    console.print(f"  [green]✅[/] {msg}")


def _skip(msg: str) -> None:
    console.print(f"  [dim]✅ {msg} (already done)[/]")


def _warn(msg: str) -> None:
    console.print(f"  [yellow]⚠️  {msg}[/]")


def _err(msg: str) -> None:
    console.print(f"  [red]❌ {msg}[/]")


def _info(msg: str) -> None:
    console.print(f"  {msg}")


def _step(n: int, total: int, title: str) -> None:
    console.print(f"\n[bold]Step {n}/{total}: {title}[/]")


def _load_existing_config(net_path: Optional[Path] = None) -> dict:
    """
    Read existing install.json + network.json and return a dict of known values
    to pre-fill wizard prompts.  Missing keys are returned as empty strings.
    """
    result: dict = {}
    try:
        net_file = net_path or (DEFAULT_SHARED_DIR / "network.json")
        if net_file.exists():
            net = json.loads(net_file.read_text())
            result["pod_name"] = net.get("networkId", "")
            result["admin_user"] = net.get("admin_user", "")
            result["chat_id"] = (net.get("alerts") or {}).get("chatId", "")
            result["shared_dir"] = net.get("sharedDir", "")
            # Empty string when the field is absent — wizard treats that
            # as "missing, offer to set" rather than "user cleared it".
            result["timezone"] = net.get("timezone", "")
            result["_timezone_present"] = "timezone" in net
            # Resolve the primary's comms mode via primary_bot_id (it lives on
            # the primary's bots[] entry now); the legacy top-level `evolve`
            # block is honoured as a fallback for pre-S1 pods. Empty string
            # when unset — wizard treats that as "missing, offer to set".
            try:
                from primary_bot import primary_bot_comms_mode  # type: ignore
                result["comms_mode"] = primary_bot_comms_mode(net, "")
            except Exception:
                result["comms_mode"] = (net.get("evolve") or {}).get("comms_mode", "")
            result["security"] = net.get("security") or {}
            result["backup"] = net.get("backup") or {}
            result["mcp_bridge"] = net.get("mcp_bridge") or {}
            result["host"] = net.get("host") or {}
            result["bots"] = net.get("bots") or {}
    except Exception:
        pass

    try:
        install_file = DEFAULT_SHARED_DIR / "install.json"
        if install_file.exists():
            inst = json.loads(install_file.read_text())
            if not result.get("admin_user"):
                result["admin_user"] = inst.get("admin_user", "")
    except Exception:
        pass

    # Try to find Telegram token from the evolve OC config
    if not result.get("telegram_token"):
        try:
            oc_file = user_home("evolve") / ".openclaw" / "openclaw.json"
            if oc_file.exists():
                oc = json.loads(oc_file.read_text())
                tok = (oc.get("channels") or {}).get("telegram", {}).get("botToken", "")
                if tok:
                    result["telegram_token"] = tok
        except Exception:
            pass

    # A previously-configured *dedicated* security-alert channel lives in the
    # keystore (the wizard no longer prompts for it — it's an opt-in Settings
    # control). Surface it here so a re-run preserves the dedicated channel
    # rather than clobbering it with the main bot's token below.
    try:
        sd = Path(result.get("shared_dir") or str(DEFAULT_SHARED_DIR))
        sec_tok_file = sd / "keystore" / "security-alert-token"
        sec_chat_file = sd / "keystore" / "security-alert-chat-id"
        if sec_tok_file.exists() and sec_chat_file.exists():
            result["security_alert_token"] = sec_tok_file.read_text().strip()
            result["security_alert_chat_id"] = sec_chat_file.read_text().strip()
    except Exception:
        # Best-effort pre-fill: a partial read (e.g. token readable but
        # chat-id not) must not surface a half-configured dedicated channel,
        # or the wizard would write a token with no chat-id to the keystore.
        result.pop("security_alert_token", None)
        result.pop("security_alert_chat_id", None)

    return result


def _ask(prompt: str, default: str = "", non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        sys.exit(0)
    return val if val else default


def _evolve_alert_chat_default(existing_chat_id: str, bot_alert_chat_id: str) -> str:
    """Resolve the default for Step 11's "Your Telegram chat ID" prompt.

    Precedence, highest first:
      1. An existing-config chat ID (re-run / reconfigure case).
      2. The chat ID the operator already entered while creating a bot in
         Step 2 — same person, so don't make them type it twice.
      3. "" (no default; first install with no bot chat configured).

    Kept as a tiny pure helper so the carry-through is unit-testable without
    driving the whole interactive installer.
    """
    return (existing_chat_id or "") or (bot_alert_chat_id or "") or ""


def _ask_secret(prompt: str, non_interactive: bool = False) -> str:
    """Prompt for a secret (API key / token) WITHOUT echoing it.

    `input()` echoes, so a `setup --fresh` run captured under `script`/`tee`
    leaks the cleartext key into the session log (round-3 hygiene finding F:
    the key was found in /root/setup-session.log). `getpass` reads with echo
    off. Non-interactive keeps `_ask`'s contract (returns "" — keys arrive
    from the bots file, not this prompt)."""
    if non_interactive:
        return ""
    import getpass
    try:
        return getpass.getpass(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        sys.exit(0)


def _sanitize_mcp_hostname(raw: str) -> str:
    """Normalize a pasted Tailscale hostname to a bare host.

    W10-G #6b: operators paste a value that already carries a scheme, a
    ``:port``, and/or a path (e.g. ``http://mini.tail1234.ts.net:5051/sse``).
    The MCP client URL then appends ``:5051/sse`` again, producing a doubled
    port (``…ts.net:5051:5051/sse``). Strip scheme, path, and a trailing
    numeric ``:port`` so the composed URL has exactly one port and one
    ``/sse``. A bare hostname passes through unchanged; a colon followed by
    non-digits (a malformed paste, not a port) is left intact.
    """
    host = raw.strip()
    if not host:
        return ""
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]  # drop any path (e.g. trailing /sse)
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        host = head  # drop a pasted :PORT
    return host


def _confirm(prompt: str, default: bool = True, non_interactive: bool = False) -> bool:
    if non_interactive:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} ({hint}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        sys.exit(0)
    return val.startswith("y") if val else default


_DEFAULT_POD_TZ = "America/Los_Angeles"


def _ask_timezone(default: str, non_interactive: bool) -> str:
    """Prompt for a pod timezone (IANA name) until valid; default on empty.

    Validated via zoneinfo.ZoneInfo(name); re-prompts on error so a typo
    can be corrected without restarting the wizard. Non-interactive mode
    accepts the default without validation (manifest-driven flows).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    base = default or _DEFAULT_POD_TZ
    if non_interactive:
        return base
    while True:
        val = _ask("Pod timezone (IANA, e.g. America/Los_Angeles)", base, non_interactive).strip()
        try:
            ZoneInfo(val)
            return val
        except ZoneInfoNotFoundError:
            _warn(f"Unknown timezone '{val}' — try an IANA name like America/New_York or Europe/London.")


# ── Audit logging ─────────────────────────────────────────────────────────────

def _log_admin_action(action: str, result: str, bot: str = "", initiated_by: str = "wizard") -> None:
    """Append a completed wizard action to admin-actions.jsonl (audit trail only)."""
    entry = {
        # Timezone-aware UTC (datetime.utcnow() is deprecated in 3.12+); keep
        # the trailing "Z" exactly as before — isoformat() on an aware value
        # renders "+00:00", so swap it back to "Z" for byte-identical output.
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": action,
        "bot": bot,
        "initiated_by": initiated_by,
        "result": result,
    }
    log_path = DEFAULT_SHARED_DIR / "logs" / "admin-actions.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # audit log failure is non-fatal


# ── Evolve OC provisioning ────────────────────────────────────────────────────

_EVOLVE_SOUL_MD = """\
# SOUL.md — Evolve Infrastructure Bot

You are Evolve, the infrastructure manager for this OpenClaw pod.

## Purpose
- Monitor pod health and surface issues
- Generate and present improvement proposals for operator approval
- Execute approved configuration changes safely
- Run the RSI feedback loop (measure → analyze → propose → apply)

## Constraints
- No personal assistant capabilities — no scheduling, email, calendar
- NEVER modify bot configs without explicit human approval
- READ configs and logs; WRITE only to own workspace and approved targets
- Report facts; do not make autonomous decisions

## Tone
Concise, technical, factual. This is infrastructure tooling.
"""

_EVOLVE_PORT = 19030


def _select_api_keys_for_evolve(non_interactive: bool) -> dict:
    """
    Scan all existing bot auth-profiles.json files and let the operator choose
    which keys to copy into the evolve user. Falls back to prompting for a
    fresh Anthropic API key if none are found. Returns an auth-profiles dict.
    """
    from .wizard import _find_existing_keys, _new_bot_auth_profiles

    existing = _find_existing_keys()
    if not existing:
        _warn("No existing API keys found to copy.")
        if non_interactive:
            _warn("Configure evolve's auth-profiles.json manually.")
            return {"version": 1, "profiles": {}, "lastGood": {}}
        console.print()
        console.print("  [bold]Enter an API key for the Evolve bot:[/]")
        console.print("  [dim](Press Enter to skip and configure manually later)[/]")
        # Provider choice mirrors the add-bot wizard (wizard.py): no
        # preselected provider — provider-agnostic principle
        # (docs/principle-llm-provider-agnostic.md: recommending is fine,
        # presuming is not). Enter skips, keeping cold-start skippable.
        console.print("  LLM provider:")
        console.print("    [1] Anthropic (recommended)")
        console.print("    [2] OpenAI")
        console.print("    [3] Other (enter manually)")
        provider = ""
        while not provider:
            choice = _ask("Provider (1-3, or Enter to skip)", "", non_interactive).strip()
            if not choice:
                return {"version": 1, "profiles": {}, "lastGood": {}}
            if choice == "1":
                provider = "anthropic"
            elif choice == "2":
                provider = "openai"
            elif choice == "3":
                provider = _ask(
                    "Provider name (e.g. mistral, cohere)", "", non_interactive
                ).strip().lower()
                if not provider:
                    _warn("Provider name required.")
            else:
                _warn("Pick 1, 2, or 3 — Evolve doesn't choose a provider for you.")
        # W10-G #6a: unlike the other (echoing) secret prompts, this one is
        # getpass-masked — operators read the blank line as a failed paste.
        # Signal the masking inline; keep masking (correct for a key).
        api_key = _ask_secret(
            f"{provider.capitalize()} API key (input hidden — paste won't show)",
            non_interactive)
        if api_key:
            # sk-ant-oat… is an Anthropic OAuth/MAX token; the token key
            # type applies only to anthropic (same inference as wizard.py).
            key_type = (
                "token"
                if provider == "anthropic" and api_key.startswith("sk-ant-oat")
                else "api_key"
            )
            return _new_bot_auth_profiles(provider, key_type, api_key)
        return {"version": 1, "profiles": {}, "lastGood": {}}

    if non_interactive:
        # Auto-select first key per provider
        seen: dict[str, dict] = {}
        for k in existing:
            if k["provider"] not in seen:
                seen[k["provider"]] = k
        selected = list(seen.values())
    else:
        # Group by provider for display
        by_provider: dict[str, list] = {}
        for k in existing:
            by_provider.setdefault(k["provider"], []).append(k)

        console.print()
        console.print("  [bold]Which API keys should the Evolve bot use?[/]")
        console.print()

        indexed: list[dict] = []
        for provider, keys in sorted(by_provider.items()):
            console.print(f"  [bold]{provider.capitalize()}:[/]")
            for k in keys:
                idx = len(indexed) + 1
                hint = "MAX token" if k["type"] == "token" else "api_key"
                suffix = k["_value"][-6:] if k["_value"] else "???"
                console.print(f"    [{idx}] {hint} from {k['source_bot']} (...{suffix})")
                indexed.append(k)
            console.print()

        console.print("  Enter numbers to include (e.g. [bold]1 3[/]), or [bold]a[/] for all:")
        choice = _ask("Keys to copy", "a", non_interactive).strip().lower()

        if choice == "a" or not choice:
            selected = indexed
        else:
            selected = []
            for tok in choice.replace(",", " ").split():
                if tok.isdigit():
                    i = int(tok) - 1
                    if 0 <= i < len(indexed):
                        selected.append(indexed[i])

    # Build auth-profiles structure from selected keys
    profiles: dict = {}
    last_good: dict = {}
    for k in selected:
        pid = f"{k['provider']}:{k['type']}"
        key_field = "token" if k["type"] == "token" else "key"
        profiles[pid] = {
            "type": k["type"],
            "provider": k["provider"],
            key_field: k["_value"],
        }
        last_good[k["provider"]] = pid

    return {"version": 1, "profiles": profiles, "lastGood": last_good}


def _embedding_chain_for_credentials(provider_ids: set[str]) -> list[str]:
    """
    Compute a default embedding chain from the set of providers the operator
    just provisioned credentials for. Delegates to embeddings.chain_from_credentials
    so the install-time default tracks the runtime resolver.

    Returns at minimum ["local"] so memory_search always has a usable terminal.
    """
    try:
        from embeddings import chain_from_credentials  # type: ignore
    except Exception:
        return ["local"]
    return chain_from_credentials(provider_ids)


# Default models to add to the catalog per provider when keys are provisioned.
# A provider's models are added ONLY when that provider is credentialed —
# no provider (Anthropic included) is presumed present
# (docs/principle-llm-provider-agnostic.md).
_PROVIDER_CATALOG_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-opus-4-6",
    ],
    "openai": [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1",
    ],
    "google": [
        "google/gemini-2.0-flash",
        "google/gemini-2.5-pro",
    ],
    "xai": [
        "xai/grok-4",
        "xai/grok-3",
        "xai/grok-3-mini",
        "xai/grok-2",
    ],
    "moonshot": [
        "moonshot/kimi-k3",
        "moonshot/kimi-k2.6",
    ],
}


def _derive_seed_models(active_providers: set[str]) -> tuple[str, str]:
    """(primary, classifier) seed models from the credentialed providers.

    Thin wrapper over ``models.derive_seed_models`` (analyzer package,
    top-level module). Lazy import with an empty fallback so setup_wizard
    stays importable when the analyzer package is absent — an unseeded
    model field is loud (wizard_verify fails "primary not set"), whereas
    presuming a provider here would be silent and possibly dead.
    """
    try:
        from models import derive_seed_models  # type: ignore
    except Exception:
        return "", ""
    return derive_seed_models(active_providers)


def _evolve_openclaw_config(
    network_id: str,
    shared_dir: str,
    telegram_token: str = "",
    auth_profiles_flat: dict | None = None,
    gateway_token: str = "",
    bot_id: str = "evo",
    gateway_account: str = "evolve",
) -> dict:
    """Build a security-hardened openclaw.json for the primary bot.

    ``bot_id`` is the logical name in network.json — defaults to ``"evo"``,
    the new dedicated-primary-bot name. ``gateway_account`` is the Unix
    account the gateway runs as (and whose home holds the workspace):
    ``"evolve"`` on macOS (the bot starts on the service account, then the
    E.2.b cutover moves it to ``evo``), ``"evo"`` on a fresh Linux pod
    (day-one provisioning straight onto ``evo``, no cutover). Account name ≠
    bot id is first-class.
    """
    gw_token = gateway_token or secrets.token_hex(32)

    # Build auth.order per provider: token-mode profiles first, then api_key
    auth_order: dict = {}
    active_providers: set[str] = set()
    if auth_profiles_flat:
        providers: dict[str, list] = {}
        for pid, p in auth_profiles_flat.items():
            provider = p.get("provider") or pid.split(":")[0]
            providers.setdefault(provider, []).append((pid, p.get("type", "")))
            active_providers.add(provider)
        for provider, entries in providers.items():
            sorted_pids = [
                pid for pid, _ in sorted(entries, key=lambda x: 0 if x[1] == "token" else 1)
            ]
            auth_order[provider] = sorted_pids

    # Build model catalog from whichever providers have keys — credentialed
    # providers only; Anthropic is not presumed present.
    catalog_models: dict[str, dict] = {}
    for provider in sorted(active_providers):
        for m in _PROVIDER_CATALOG_MODELS.get(provider, []):
            catalog_models[m] = {}

    # Derive evo's primary + classifier models from the credentialed
    # providers (models.derive_seed_models: primary = derived tier2 pick,
    # classifier = derived tier3 pick). Both come back "" when no
    # credentialed provider has a known pick (e.g. only a free-text
    # provider like "mistral", or no credentials at all) — in that case
    # the fields are left unseeded below so wizard_verify's existing
    # "primary not set" check fires loudly, rather than silently seeding
    # a dead Anthropic model on a pod that can't call it.
    seed_primary, seed_classifier = _derive_seed_models(active_providers)

    # Build memorySearch block from the providers the operator just supplied
    # keys for. OpenClaw supports {provider, fallback}; the chain's first two
    # entries become the primary and fallback. local is always the safety net.
    embedding_chain = _embedding_chain_for_credentials(active_providers)
    memory_search_cfg: dict = {"provider": embedding_chain[0]}
    if len(embedding_chain) > 1:
        memory_search_cfg["fallback"] = embedding_chain[1]

    cfg: dict = {
        "gateway": {
            "port": _EVOLVE_PORT,
            "mode": "local",
            "bind": "loopback",
            "trustedProxies": [],
            "auth": {"mode": "token", "token": gw_token},
        },
        "agents": {
            "defaults": {
                "model": {"primary": seed_primary, "fallbacks": []},
                "models": catalog_models,
                "memorySearch": memory_search_cfg,
                "workspace": str(user_home(gateway_account) / ".openclaw" / "workspace"),
                "compaction": {"mode": "safeguard"},
            }
        },
        "tools": {
            "exec": {"security": "full", "ask": "on-miss"},
            "web": {"search": {"enabled": False}, "fetch": {"enabled": False}},
        },
        "plugins": {
            "entries": {
                "evolve": {
                    "enabled": True,
                    "config": {
                        "botId": bot_id,
                        "role": "primary",
                        "networkId": network_id,
                        "sharedDir": shared_dir,
                        "classifierModel": seed_classifier,
                        "tierClassification": "session",
                        "dashboardEnabled": True,
                    },
                }
            }
        },
    }
    # Unseedable fields (no known pick for any credentialed provider) are
    # DROPPED, not written as "": wizard_verify then reports "primary not
    # set" (missing key) and the plugin's own config.ts default covers the
    # classifier as a last resort. An empty-string value would instead risk
    # tripping OC's config validator / the materializer's drift promotion.
    if not seed_primary:
        del cfg["agents"]["defaults"]["model"]
    if not seed_classifier:
        del cfg["plugins"]["entries"]["evolve"]["config"]["classifierModel"]
    if auth_order:
        cfg["auth"] = {"order": auth_order}
    if telegram_token:
        # Channel config defaults (non-credential). Credential goes through the
        # shared `_apply_credential_to_oc_dict` registry helper below so this
        # builder stays in sync with the rotate endpoint.
        cfg["channels"] = {
            "telegram": {
                "enabled": True,
                "dmPolicy": "pairing",
                "groupPolicy": "allowlist",
                "streaming": {"mode": "off"},
            }
        }
        # Merge the telegram entry alongside the evolve plugin built above —
        # a bare `cfg["plugins"] = {...}` here wiped the evolve entry (with its
        # required botId), leaving the primary's openclaw.json with no evolve
        # plugin config at all (W10-E: contributed to the empty-config /
        # missing-botId failure on the evo gateway).
        cfg["plugins"]["entries"]["telegram"] = {"enabled": True}
        _apply_credential_to_oc_dict(cfg, "telegram", "bot_token", telegram_token)
    return cfg


def _evolve_gateway_jobspec(
    label: str,
    node_bin: str,
    oc_index: str,
    gw_port: str,
    gw_token: str,
    oc_version: str,
    account: str = "evolve",
) -> JobSpec:
    """Build the evolve-gateway JobSpec (pure — no disk writes).

    ``account`` is the Unix account the gateway runs as: ``"evolve"`` on
    macOS (byte-identical to the legacy shape), ``"evo"`` on a fresh Linux
    pod (day-one provisioning). Home/log paths resolve through the blessed
    ``user_home`` helper. ``PATH``/``NODE_EXTRA_CA_CERTS`` are platform-keyed
    (Homebrew layout on macOS, FHS on Linux); node/oc-entry resolution is
    the caller's job (``_provision_evo_oc``, profile-keyed there).

    The Scheduler seam renders this to a launchd plist (macOS) or a systemd
    unit (Linux) — the wizard never hand-formats either.
    """
    from platform_profile import get_profile

    profile = get_profile()
    home = user_home(account)
    logs = home / ".openclaw" / "logs"
    return JobSpec(
        label=label,
        comment="OpenClaw Gateway for Evolve (headless, survives GUI logout)",
        program_args=[node_bin, oc_index, "gateway", "--port", gw_port],
        user=account,
        group_name="staff",
        umask=63,
        run_at_load=True,
        keep_alive=True,
        throttle_interval=5,
        stdout_path=str(logs / "gateway.log"),
        stderr_path=str(logs / "gateway.err.log"),
        env={
            "HOME": str(home),
            "TMPDIR": "/tmp",
            # Platform-keyed exec dirs (Homebrew prefixes on macOS, FHS on
            # Linux; NodeSource node lands in /usr/bin) — sourced from the
            # profile, the SAME PATH app crons inject via
            # install_helpers._ensure_launchd_openclaw_path, so the infra-daemon
            # and app-cron PATHs can't drift (W10-F single-source contract).
            "PATH": ":".join(profile.exec_path_dirs),
            # macOS ships the unified cert.pem; Debian/Ubuntu's ca-certificates
            # package writes the bundle here. Single source = the profile field
            # (W10-F) so this and deploy.install_bot_gateway_plist can't drift.
            "NODE_EXTRA_CA_CERTS": profile.ca_bundle,
            "OPENCLAW_GATEWAY_PORT": gw_port,
            "OPENCLAW_GATEWAY_TOKEN": gw_token,
            "OPENCLAW_LAUNCHD_LABEL": label,
            "OPENCLAW_SERVICE_MARKER": "openclaw",
            "OPENCLAW_SERVICE_KIND": "gateway",
            "OPENCLAW_SERVICE_VERSION": oc_version,
        },
    )


def _evolve_gateway_plist_content(
    label: str,
    node_bin: str,
    oc_index: str,
    gw_port: str,
    gw_token: str,
    oc_version: str,
    account: str = "evolve",
) -> str:
    """Render the evolve-gateway LaunchDaemon plist XML (macOS install path).

    Thin wrapper over :func:`_evolve_gateway_jobspec` + ``render_launchd_plist``;
    kept for the macOS raw-bootstrap ritual and the golden parity test. The
    Linux install path uses the JobSpec directly via the Scheduler seam.

    An empty ``gw_token`` still emits the OPENCLAW_GATEWAY_TOKEN env key
    (as an empty string) — same shape the original hand-rolled emitter
    produced via its ``_token_xml`` branch.
    """
    return render_launchd_plist(
        _evolve_gateway_jobspec(
            label, node_bin, oc_index, gw_port, gw_token, oc_version, account,
        )
    )


def _provision_evo_oc(
    pod_name: str,
    shared_dir: Path,
    admin_user: str,
    bots: list,
    non_interactive: bool,
    telegram_token: str = "",
    bot_id: str = "evo",
    gateway_account: str = "evolve",
) -> bool:
    """
    Provision the service account and the primary bot's OpenClaw gateway.

    The ``evolve`` service account (admin daemon, infra jobs) is always
    created. ``gateway_account`` is the Unix account the *primary bot's
    gateway* runs as:
      - ``"evolve"`` (macOS default): the bot starts on the service account,
        then run_setup's E.2.b cutover moves it to ``evo``. Byte-identical
        to the legacy shape.
      - ``"evo"`` (fresh Linux): day-one provisioning straight onto ``evo``
        (created here via Step A.2), no cutover dance — there is no
        pre-separation legacy on a fresh pod (census §12 Q9 / W5).

    ``bot_id`` controls the logical name in network.json. Returns True on
    success.
    """
    console.print()
    console.print(f"  [bold]Provisioning primary bot OC instance '{bot_id}' (port 19030)...[/]")

    # Step A: Create the evolve service account (always — admin daemon +
    # infra jobs run as evolve regardless of which account the gateway uses).
    with console.status("  Creating evolve account..."):
        ok = _create_bot_account("evolve")
    if not ok:
        _err("Failed to create evolve account.")
        return False
    _log_admin_action("create_user", "ok", bot="evolve")

    # W10-F #10: provision the evolve service account's OWN .openclaw tree,
    # owned by evolve. On macOS the gateway account IS evolve, so the Step B/C
    # block below already mkdir+chowns this tree (guard Linux-only to keep the
    # macOS path byte-identical). On Linux the gateway account is `evo`, so
    # Step B/C provisions /home/evo/.openclaw — never evolve's. Later root-
    # context writers then `sudo mkdir` /home/evolve/.openclaw/{logs,cron}
    # (the SystemdScheduler log-parent step, the app-install cron merge),
    # leaving them root:root and unwritable by the evolve daemons. Creating
    # them here, owned by evolve, makes those writers just work; mkdir -p is a
    # no-op on re-run and chown is idempotent.
    from platform_profile import get_profile as _gp_w10f
    if _gp_w10f().name != "macos":
        _evolve_home = user_home("evolve")
        for _sub in (".openclaw", ".openclaw/logs", ".openclaw/cron",
                     ".openclaw/workspace"):
            subprocess.run(["sudo", "/bin/mkdir", "-p", str(_evolve_home / _sub)],
                           capture_output=True)
        subprocess.run(
            ["sudo", _gp_w10f().chown, "-R", "evolve:staff",
             str(_evolve_home / ".openclaw")],
            capture_output=True,
        )

    # Step A.2 (Phase E.2.a): Provision the `evo` macOS account empty.
    # Today the primary bot's gateway runs as the `evolve` user (same as
    # the admin daemon). Phase E moves it to its own unprivileged `evo`
    # user. E.2.a is the first half: the account exists but nothing runs
    # there yet — E.2.b's cutover (still ahead) flips the plist's
    # UserName. Provisioning here on every new install means the cutover
    # PR doesn't need to touch dscl.
    # Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.a".
    with console.status("  Provisioning evo macOS account (Phase E.2.a)..."):
        evo_ok = _provision_evo_account()
    if not evo_ok:
        # Non-fatal: the primary bot still works on the evolve account.
        # E.2.b's cutover will retry the provisioning if needed.
        _warn(
            "evo account provisioning failed — install will continue but "
            "Phase E.2.b cutover will need to re-run this step."
        )
    else:
        _log_admin_action("create_user", "ok", bot="evo")

    # Step B/C: Write the gateway account's OC directories. On macOS this is
    # the evolve service account (Step A); on Linux it is the `evo` account
    # provisioned by Step A.2. user_home resolves it via pwd — /Users/<acct>
    # on macOS, /home/<acct> on Linux.
    from platform_profile import get_profile
    profile = get_profile()
    home = user_home(gateway_account)
    dirs = [
        home / ".openclaw",
        home / ".openclaw" / "workspace",
        home / ".openclaw" / "workspace" / "evolve",
        home / ".openclaw" / "logs",
        home / ".openclaw" / "agents" / "main" / "agent",
    ]
    for d in dirs:
        subprocess.run(["sudo", "/bin/mkdir", "-p", str(d)], capture_output=True)

    # Step D/E: API key selection
    _info("  Selecting API keys for Evolve bot...")
    auth_profiles = _select_api_keys_for_evolve(non_interactive)

    # Embedding-provider validation: surface a loud warning if no remote
    # embedding-capable provider got a credential. memory_search will still
    # work via the local GGUF fallback, but quality + speed degrade and the
    # operator should know now (not after a session goes sideways).
    _provider_ids = {
        p.get("provider")
        for p in (auth_profiles.get("profiles") or {}).values()
        if p.get("provider")
    }
    _embedding_chain = _embedding_chain_for_credentials(_provider_ids)
    _remote_chain = [p for p in _embedding_chain if p not in {"local", "ollama"}]
    if not _remote_chain:
        # Informational, not a failure: memory_search works out of the box on
        # the bundled local GGUF model. A cloud embedding key is a quality/speed
        # upgrade the operator can add anytime — style it as a neutral note so a
        # healthy install doesn't look like it's missing something required.
        _info(
            "[dim]ℹ[/] memory_search will use the bundled local embedding model "
            "(no cloud key configured). This works fine out of the box."
        )
        _info(
            "  Optional upgrade: add a Gemini (multimodal-capable), OpenAI, or "
            "Voyage key after install via Integrations & Keys for higher-quality, "
            "faster search."
        )
    else:
        _ok(
            f"Embedding chain: {' → '.join(_embedding_chain)} "
            f"(memory_search will fail over in this order)"
        )

    # Step F: Telegram token for evolve (reuse token from Step 11 if already provided)
    if telegram_token:
        evolve_tg = telegram_token
    else:
        console.print()
        _info("  Evolve bot Telegram token (for admin alerts and infrastructure notifications):")
        evolve_tg = _ask(
            "Evolve Telegram bot token (or Enter to skip)", "", non_interactive
        ).strip()
        if evolve_tg:
            with console.status("  Testing Evolve Telegram connection..."):
                username = _test_telegram_token(evolve_tg)
            if username:
                _ok(f"  Evolve Telegram: @{username}")
            else:
                _warn("  Token not verified — writing anyway.")

    # Step C/D: Write hardened openclaw.json
    oc_cfg = _evolve_openclaw_config(
        pod_name, str(shared_dir), evolve_tg,
        auth_profiles_flat=auth_profiles.get("profiles", {}),
        bot_id=bot_id,
        gateway_account=gateway_account,
    )
    oc_path = home / ".openclaw" / "openclaw.json"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(oc_cfg, tmp, indent=2)
        tmp_path = tmp.name
    r = subprocess.run(["sudo", "/bin/cp", tmp_path, str(oc_path)], capture_output=True, text=True)
    os.unlink(tmp_path)
    if r.returncode != 0:
        _err(f"Failed to write evolve openclaw.json: {r.stderr.strip()}")
        return False
    # Enforce 0600 — openclaw.json carries the gateway auth token. A bare `cp`
    # (no `-p`) to a *fresh* dest lands 0600, but a wizard re-run over an existing
    # 0644 dest would cp-PRESERVE 0644 (world-readable on a multi-user box).
    # chmod_secret_config is the secret-config-perms single source of truth that
    # deploy.py's writers use; chmod preserves the evolve read ACL.
    chmod_secret_config(oc_path)

    # Write auth-profiles.json
    auth_path = home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(auth_profiles, tmp, indent=2)
        tmp_path = tmp.name
    auth_cp = subprocess.run(["sudo", "/bin/cp", tmp_path, str(auth_path)], capture_output=True)
    os.unlink(tmp_path)
    # Enforce 0600 — auth-profiles.json carries provider API keys (same fresh-dest
    # vs. re-run cp-preserve exposure as openclaw.json above).
    if auth_cp.returncode == 0:
        chmod_secret_config(auth_path)

    # Step G: SOUL.md
    soul_path = home / ".openclaw" / "workspace" / "SOUL.md"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write(_EVOLVE_SOUL_MD)
        tmp_path = tmp.name
    subprocess.run(["sudo", "/bin/cp", tmp_path, str(soul_path)], capture_output=True)
    os.unlink(tmp_path)

    # Fix ownership on everything — owned by the gateway account so its
    # gateway can read/write its own config. chown BINARY routes through the
    # profile (/usr/sbin macOS, /usr/bin Linux); the `:staff` primary group is
    # left literal — it exists on Ubuntu (gid 50), matching deploy.py's W7 rule.
    subprocess.run(
        ["sudo", profile.chown, "-R", f"{gateway_account}:staff", str(home / ".openclaw")],
        capture_output=True,
    )
    _ok("  evolve OC config written (port 19030, hardened)")
    _log_admin_action("write_oc_config", "ok", bot="evolve")

    # OpenClaw 2026.6+ imports auth-profiles.json into the per-agent SQLite
    # store (openclaw-agent.sqlite) on agent-CLI init, NOT on gateway start.
    # Trigger that import now so the gateway bootstrapped in Step H below reads
    # the provider key instead of failing every dispatch with `No API key
    # found` (caught live on a fresh evo-primary Linux pod, evolve-vps). The
    # chown above already made gateway_account the owner of the agent dir, so
    # the import (run as that account) can write the store; Step H then bounces
    # the gateway, which loads the now-populated store. Best-effort. WRITE-side
    # counterpart to the oc_store read adapter (#3103/#3105 fixed reads only).
    try:
        from .oc_auth_provision import ensure_agent_auth_store_imported
        _imp_ok, _imp_msg = ensure_agent_auth_store_imported(
            bot_id, gateway_account, home)
        if _imp_ok:
            _ok("  Auth store imported (gateway will load the provider key)")
        else:
            _warn(f"  Auth-store import deferred: {_imp_msg} "
                  f"(gateway picks it up on the next agent CLI run)")
    except Exception as exc:
        _warn(f"  Auth-store import step skipped (non-fatal): "
              f"{type(exc).__name__}: {exc}")

    # Sync openclaw's config integrity metadata after writing the file externally.
    # Without this, operations that validate the config see a meta anomaly.
    try:
        subprocess.run(
            ["sudo", "-u", gateway_account, "env", f"HOME={home}",
             "openclaw", "doctor", "--fix"],
            cwd=str(home), capture_output=True, timeout=15,
        )
    except Exception:
        pass  # best-effort — gateway bootstrap will catch any remaining issues

    # Step H: Register gateway — write the LaunchDaemon plist directly.
    # Must run node directly (NOT 'openclaw gateway start') because the start
    # command tries to kickstart the service in the gui/{uid} domain, which
    # is inaccessible from a system LaunchDaemon context (error 125).
    _info("  Installing evolve gateway service...")
    # Resolve node binary — use the symlink path (e.g. /opt/homebrew/bin/node),
    # NOT realpath, so the service stays stable across upgrades. Homebrew
    # prefixes are macOS-only; NodeSource node lands in /usr/bin on Linux.
    if profile.name == "macos":
        _node_search = "/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/opt/node/bin"
        _node_fallback = "/opt/homebrew/bin/node"
    else:
        _node_search = "/usr/bin:/usr/local/bin"
        _node_fallback = "/usr/bin/node"
    node_bin = shutil.which("node", path=_node_search) or _node_fallback
    # Resolve openclaw's JS entry point — check all known locations/names.
    # macOS: Homebrew/usr-local node_modules; Linux: NodeSource global prefix.
    if profile.name == "macos":
        _oc_prefixes = ["/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules"]
    else:
        _oc_prefixes = ["/usr/lib/node_modules", "/usr/local/lib/node_modules"]
    _oc_names = ["dist/index.js", "dist/entry.js", "openclaw.mjs"]
    _oc_candidates = [f"{p}/openclaw/{n}" for p in _oc_prefixes for n in _oc_names]
    oc_index = next((p for p in _oc_candidates if Path(p).exists()), _oc_candidates[0])

    # Read port and token from evolve's openclaw.json
    _oc_cfg: dict = {}
    try:
        import json as _json
        _raw = subprocess.run(
            ["sudo", profile.cat, str(home / ".openclaw" / "openclaw.json")],
            capture_output=True, text=True,
        ).stdout
        _oc_cfg = _json.loads(_raw) if _raw.strip() else {}
    except Exception:
        pass
    gw_cfg = _oc_cfg.get("gateway", {})
    gw_port = str(gw_cfg.get("port", 19030))
    gw_token = gw_cfg.get("auth", {}).get("token", "")
    if not gw_cfg:
        _warn("  No 'gateway' section found in evolve's openclaw.json — "
              "using default port 19030 and empty auth token. "
              "Re-run setup after OpenClaw initializes evolve's config.")
    elif not gw_token:
        _warn("  Gateway token is empty in evolve's openclaw.json — "
              "the gateway will start unauthenticated. "
              "Re-run setup after OpenClaw sets the token.")

    # EVO-SEP-GW-CRED: on the evo-primary path the admin daemon (`evolve`) and
    # this gateway run as DIFFERENT accounts, so the admin UI's home-chat
    # dispatch (`openclaw agent` inheriting HOME=/home/evolve) needs its OWN
    # client openclaw.json carrying THIS gateway's token — a fresh evolve home
    # has none, so the dispatch fails GatewayCredentialsRequiredError. Write the
    # matching client credential (evolve-owned, 0600). No-op when
    # gateway_account == "evolve" (legacy / macOS-pre-cutover: admin IS the
    # gateway account, one shared openclaw.json), and skipped when the token
    # couldn't be read back (the warns above already fired) — the deploy-time
    # self-heal (deploy.ensure_pod_perms → check_evolve_gateway_client) converges
    # it then.
    if gateway_account != "evolve" and gw_token:
        from .evo_gateway_client import provision_evolve_gateway_client
        try:
            _gw_port_i = int(gw_port) if str(gw_port).isdigit() else _EVOLVE_PORT
            if provision_evolve_gateway_client(_gw_port_i, gw_token, gateway_account):
                _ok("  evolve admin→gateway client credential written (0600)")
            else:
                _warn("  Could not write the evolve admin→gateway client "
                      "credential — the home-chat dispatch may fail until the "
                      "deploy self-heal runs.")
        except Exception as _cred_exc:  # noqa: BLE001 — best-effort; self-heal converges
            _warn(f"  evolve admin→gateway client credential: {_cred_exc}")

    # Determine installed openclaw version for the env var — read from the
    # npm package.json directly rather than running `openclaw --version`,
    # which fails because evolve has no sudoers grant for that command.
    # Falls back to "unknown" if openclaw is installed at a non-standard prefix
    # (e.g. via fnm/nvm); this is cosmetic — the gateway does not use this value
    # for anything functional.
    oc_version = "unknown"
    for _pkg_path in [f"{p}/openclaw/package.json" for p in _oc_prefixes]:
        try:
            import json as _json
            _pkg = _json.loads(Path(_pkg_path).read_text())
            oc_version = _pkg.get("version", "unknown")
            break
        except Exception:
            pass
    if oc_version == "unknown":
        _warn("  Could not read openclaw version from package.json — "
              "OPENCLAW_SERVICE_VERSION will be 'unknown' in the plist.")

    # The primary gateway's label is the canonical per-bot gateway label for
    # the PRIMARY bot id — ``ai.openclaw.evo-gateway`` on an evo-primary pod,
    # ``ai.openclaw.evolve-gateway`` on a legacy evolve-primary pod (bot_id
    # "evolve"). It must NOT be hardcoded to ``evolve-gateway``: on an evo-
    # primary install the canonical per-bot machinery (deploy_bot / recovery /
    # health) provisions and heals ``ai.openclaw.evo-gateway``, so a hardcoded
    # ``evolve-gateway`` here is a SECOND daemon on the same gateway port —
    # the loser crash-loops (EVO-LINUX-PHANTOM-GATEWAY). ``bot_id`` is the
    # resolved primary id (the caller passes ``primary_bot_id_choice``).
    label = per_bot_gateway_plist_label(bot_id)
    # One JobSpec; the Scheduler seam renders it to a launchd plist (macOS)
    # or a systemd unit (Linux). The gateway runs as `gateway_account`.
    jobspec = _evolve_gateway_jobspec(
        label, node_bin, oc_index, gw_port, gw_token, oc_version, gateway_account,
    )
    log_dir = home / ".openclaw" / "logs"

    if profile.name == "macos":
        # macOS: stage the rendered plist and bootstrap via raw launchctl
        # verbs — byte-identical to the legacy ritual. install() can't be used
        # here: the log-dir fixup is staged between cp and bootstrap, and
        # install()'s byte-identical skip would leave an UNLOADED daemon
        # unloaded on wizard re-runs, where this path must re-bootstrap it.
        plist_path = Path(f"/Library/LaunchDaemons/{label}.plist")
        plist_content = render_launchd_plist(jobspec)
        _fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".plist")
        with os.fdopen(_fd, "w") as _f:
            _f.write(plist_content)
        try:
            subprocess.run(["sudo", "/bin/cp", tmp_path, str(plist_path)], check=True, capture_output=True)
            subprocess.run(["sudo", "/usr/sbin/chown", "root:wheel", str(plist_path)], check=True, capture_output=True)
            subprocess.run(["sudo", "/bin/chmod", "644", str(plist_path)], check=True, capture_output=True)
            # Ensure log directory exists and is owned by the gateway account —
            # launchd error 5 (I/O error) almost always means a path in the
            # plist doesn't exist at bootstrap time. Re-create here in case
            # openclaw doctor --fix removed it.
            subprocess.run(["sudo", "/bin/mkdir", "-p", str(log_dir)], capture_output=True)
            subprocess.run(["sudo", "/usr/sbin/chown", f"{gateway_account}:staff", str(log_dir)], capture_output=True)
            # Unload existing service — bootout is async, so poll until launchd
            # confirms the label is gone before bootstrapping.  Skipping this wait
            # causes error 5 (I/O error) on re-installs.
            sched = get_launchd_scheduler()
            _was_loaded = sched.status(label)["managed"]
            sched.raw("bootout", f"system/{label}")  # bare bootout: keep the staged plist
            if _was_loaded:
                for _ in range(20):  # up to 10 s
                    import time as _time
                    _time.sleep(0.5)
                    if not sched.status(label)["managed"]:
                        break
            _br_rc, _br_out, _br_err = sched.raw("bootstrap", "system", str(plist_path))
            if _br_rc == 0:
                _ok("  evolve gateway LaunchDaemon bootstrapped")
                _log_admin_action("install_gateway", "ok", bot="evolve")
            else:
                # Bootstrap failed — try kickstart as a fallback (sometimes the
                # plist registers but fails to start; kickstart forces it).
                _kick_ok, _ = sched.restart(label)
                if _kick_ok:
                    _ok("  evolve gateway started via kickstart")
                    _log_admin_action("install_gateway", "ok", bot="evolve")
                else:
                    # Last resort: check if the gateway is already responding on
                    # the port (started in a prior run).  If so, the pod works —
                    # just warn about the launchd state.
                    import urllib.request as _ur
                    _gateway_live = False
                    try:
                        with _ur.urlopen(f"http://127.0.0.1:{_EVOLVE_PORT}/health", timeout=3):
                            _gateway_live = True
                    except Exception:
                        pass
                    if _gateway_live:
                        _warn(
                            f"  evolve gateway LaunchDaemon could not be loaded "
                            f"(error: {_br_err.strip()[:200]}), but the gateway is "
                            f"already responding on port {_EVOLVE_PORT}. "
                            f"It will not auto-restart on reboot — to fix:\n"
                            f"  sudo launchctl bootstrap system {plist_path}"
                        )
                    else:
                        import pwd as _pwd
                        try:
                            log_owner = _pwd.getpwuid(log_dir.stat().st_uid).pw_name if log_dir.exists() else "missing"
                        except Exception:
                            log_owner = "unknown"
                        _warn(
                            f"  Gateway bootstrap failed: {_br_err.strip()[:300]}\n"
                            f"  Log dir: {log_dir} exists={log_dir.exists()} owner={log_owner}\n"
                            f"  Retry: sudo launchctl bootstrap system {plist_path}"
                        )
        except Exception as e:
            _warn(f"  Gateway plist install failed: {e}")
        finally:
            os.unlink(tmp_path)
    else:
        # Linux: install the systemd unit through the Scheduler seam — it
        # renders systemd from the same JobSpec and owns enable+start.
        # _install_job_ensuring_restart preserves the legacy always-bounce
        # for daemon-shaped specs like this one (keep_alive=True) — a config
        # change needs a restarted gateway to take effect.
        subprocess.run(["sudo", "/bin/mkdir", "-p", str(log_dir)], capture_output=True)
        subprocess.run(["sudo", profile.chown, f"{gateway_account}:staff", str(log_dir)], capture_output=True)
        from .deploy import _install_job_ensuring_restart
        try:
            _ok_install, _msg = _install_job_ensuring_restart(jobspec)
            if _ok_install:
                _ok("  evolve gateway systemd unit installed")
                _log_admin_action("install_gateway", "ok", bot="evolve")
            else:
                _warn(f"  Gateway unit install failed: {_msg}")
        except Exception as e:
            _warn(f"  Gateway unit install failed: {e}")

    return True


# ── Phase E.2.b cutover ───────────────────────────────────────────────────────
#
# Migrate evo's gateway from the privileged `evolve` macOS account to the
# unprivileged `evo` account. Spec: docs/spec-evo-account-separation-
# 2026-05-25.md §"Phase E.2.b". Pre-cutover, evo runs as `evolve` and
# enjoys ACL access to every bot. Post-cutover, evo runs as `evo` with
# no special grants; cross-bot work routes through the admin daemon's
# unix-socket API (the Phase E.3 endpoints).
#
# The helper is split from the wizard so the CLI command
# ``evolve-admin migrate-evo-account-cutover`` can call it on an
# already-installed pod. Both dry-run (default) and apply modes share
# the same step list — only the apply branch actually mutates state.
#
# Idempotency: if ``bots.<primary>.user`` is already ``"evo"`` and
# /Users/evo/.openclaw/openclaw.json already exists, the helper bails
# out cleanly ("nothing to do"). Re-running the cutover after success
# is a no-op.

EVO_CUTOVER_LABEL = "ai.openclaw.evolve-gateway"
"""Legacy/default LaunchDaemon label for the primary bot's OC gateway.

This is the byte-identical legacy literal, retained as the back-compat
default for the evolve-primary case. The cutover does NOT use it directly:
``_evo_cutover_preconditions`` resolves the actual label from the pod's
RESOLVED primary id via ``per_bot_gateway_plist_label`` and stashes it in
``ctx["label"]`` (``ai.openclaw.evo-gateway`` on an evo-primary pod,
``ai.openclaw.evolve-gateway`` on a legacy evolve-primary pod). The label
is stable across the cutover — flipping the plist's UserName to ``evo``
never renames the label, which would otherwise have to be chased through
``expected_plist_labels`` and operator runbooks.
"""

# Subpaths under /Users/evolve/.openclaw/ that the cutover should NOT
# carry over to /Users/evo/.openclaw/. Everything else under .openclaw/
# is per-bot state and travels (verified on the production mini
# 2026-05-25: agents/, workspace/, credentials/, memory/, identity/,
# telegram/, plugins/, tasks/, flows/, devices/, browser/, canvas/,
# completions/, cron/, delivery-queue/, extensions/, qqbot/,
# service-env/, npm/, tmp/, openclaw.json, evolve-tiers.json,
# exec-approvals.json, update-check.json, and a few more — adding to
# the list as new OC features arrive isn't realistic, so deny-list is
# the right shape).
#
# Excluded:
#   - logs/ — log continuity isn't worth the chown churn; the new
#     gateway will start fresh log files at /Users/evo/.openclaw/logs/.
#   - openclaw.json.clobbered.*  — accumulated repair backups from OC's
#     config self-repair flow; large, stale, no value at the new path.
#   - openclaw.json.bak / openclaw.json.last-good — kept on the source
#     side as historical bakeups; the migration writes a fresh
#     openclaw.json at the dest and any future bak files will be
#     produced by OC itself at the new path.
EVO_CUTOVER_EXCLUDE_NAMES: tuple[str, ...] = (
    "logs",
)
EVO_CUTOVER_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "openclaw.json.clobbered.",
    "openclaw.json.bak",
    "openclaw.json.last-good",
)

# Source/target SSH dirs for the backup-key migration sub-step. Under
# the unified shared-key model (docs/spec-backup-key-distribution-
# unification-2026-06-08.md) the canonical pod-wide keypair lives at
# /Users/evolve/.ssh/evolve-backup-shared; each backup-configured bot
# gets a byte-identical copy at /Users/<bot_user>/.ssh/evolve-backup-<bot>
# via evolve_admin.backup_keys.ensure_bot_in_sync. On legacy pre-
# unification pods, per-bot keys at /Users/evolve/.ssh/evolve-backup-<bot>
# may still exist; the cutover function below handles either shape.
# Post-cutover the primary bot's nightly backup daemon runs as `evo`
# and resolves ~/.ssh/evolve-backup-<primary> → /Users/evo/.ssh/…,
# which the next deploy populates via ensure_bot_in_sync.
EVOLVE_SSH_DIR = Path("/Users/evolve/.ssh")
EVO_SSH_DIR = Path("/Users/evo/.ssh")


def _evo_cutover_should_copy(name: str) -> bool:
    """Return False if the given .openclaw/ child should be skipped
    during the cutover (logs, clobbered backups, etc.)."""
    if name in EVO_CUTOVER_EXCLUDE_NAMES:
        return False
    for prefix in EVO_CUTOVER_EXCLUDE_PREFIXES:
        if name.startswith(prefix):
            return False
    return True


def _evo_cutover_preconditions(
    network_path: Path,
) -> tuple[bool, str | None, dict]:
    """Check that the cutover can proceed. Returns (ok, error_msg, ctx).

    ``ctx`` carries values the apply path needs:
      - ``primary_id``: the primary bot's logical name (from network.json)
      - ``current_user``: the macOS user currently in ``bots.<primary>.user``
      - ``label``: the gateway LaunchDaemon label, resolved from the primary
        id (``ai.openclaw.evo-gateway`` on an evo-primary pod,
        ``ai.openclaw.evolve-gateway`` on a legacy evolve-primary pod)
      - ``plist_path``: the LaunchDaemons path to rewrite (derived from ``label``)
      - ``already_done``: True iff network.json already says ``user == "evo"``
        AND /Users/evo/.openclaw/openclaw.json already exists. Caller
        treats this as a clean no-op (idempotent re-run).
    """
    import pwd as _pwd

    # Default to the legacy evolve-primary label until network.json resolves
    # the real primary id below; the pre-resolution early returns are all
    # failures whose plist_path is never consumed.
    ctx: dict = {
        "primary_id": None,
        "current_user": None,
        "label": EVO_CUTOVER_LABEL,
        "plist_path": Path(f"/Library/LaunchDaemons/{EVO_CUTOVER_LABEL}.plist"),
        "already_done": False,
    }

    # 1. `evo` user exists on the system. E.2.a should have created it.
    try:
        _pwd.getpwnam("evo")
    except KeyError:
        return (
            False,
            "macOS user 'evo' does not exist — run 'evolve-admin "
            "provision-evo-account' (Phase E.2.a) first",
            ctx,
        )

    # 2. /Users/evo/.openclaw/ exists (E.2.a created the empty tree).
    evo_oc_dir = Path("/Users/evo/.openclaw")
    if not evo_oc_dir.exists():
        return (
            False,
            f"{evo_oc_dir} does not exist — run 'evolve-admin "
            "provision-evo-account' (Phase E.2.a) first",
            ctx,
        )

    # 3. network.json must be readable and identify the primary bot.
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read network.json at {network_path}: {exc}", ctx

    primary_id = net.get("primary")
    if not primary_id or not isinstance(primary_id, str):
        return (
            False,
            "network.json is missing a top-level 'primary' field — the "
            "cutover doesn't know which bot to flip",
            ctx,
        )
    ctx["primary_id"] = primary_id

    # Resolve the gateway label from the primary id — the cutover operates on
    # whatever label the wizard installed for THIS primary, which is the
    # canonical per-bot gateway label (evo-gateway on an evo-primary pod,
    # evolve-gateway on a legacy evolve-primary pod). Hardcoding evolve-gateway
    # here would make the precondition "gateway plist missing" check fail on
    # every evo-primary pod (EVO-LINUX-PHANTOM-GATEWAY).
    ctx["label"] = per_bot_gateway_plist_label(primary_id)
    ctx["plist_path"] = Path(f"/Library/LaunchDaemons/{ctx['label']}.plist")

    primary_entry = (net.get("bots") or {}).get(primary_id) or {}
    current_user = primary_entry.get("user") or primary_id
    ctx["current_user"] = current_user

    # 4. Idempotency: if we've already cut over, bail cleanly.
    evo_oc_json = evo_oc_dir / "openclaw.json"
    if current_user == "evo" and evo_oc_json.exists():
        ctx["already_done"] = True
        return True, None, ctx

    # 5. Source state must exist on the `evolve` side. Reading happens
    # via sudo /bin/cat downstream so the evolve-user mode bit isn't
    # the gate here; the path-existence check uses .exists() which the
    # admin user can run.
    evolve_oc_json = Path("/Users/evolve/.openclaw/openclaw.json")
    if not evolve_oc_json.exists():
        return (
            False,
            f"{evolve_oc_json} does not exist — evo doesn't appear to be "
            "running on the 'evolve' account, so there's nothing to cut "
            "over. Check the pod state manually.",
            ctx,
        )

    # 6. The gateway plist must exist (so we can rewrite it). If it's
    # missing we can't migrate — the operator's install is in a state
    # this code can't reason about.
    if not ctx["plist_path"].exists():
        return (
            False,
            f"{ctx['plist_path']} does not exist — evo's gateway plist is "
            "missing. The cutover doesn't know what to rewrite.",
            ctx,
        )

    return True, None, ctx


def _evo_cutover_describe(ctx: dict) -> list[str]:
    """Human-readable plan for the cutover. Used by both dry-run and apply
    to print what's about to happen / what just happened.
    """
    primary_id = ctx["primary_id"]
    current_user = ctx["current_user"]
    plist_path = ctx["plist_path"]
    label = ctx["label"]
    exclude_summary = (
        "logs/, openclaw.json.clobbered.*, openclaw.json.bak, "
        "openclaw.json.last-good"
    )
    lines = [
        f"  Primary bot:        {primary_id}",
        f"  Current macOS user: {current_user}",
        f"  Target macOS user:  evo",
        f"  Gateway plist:      {plist_path}",
        f"  State source:       /Users/{current_user}/.openclaw/",
        f"  State destination:  /Users/evo/.openclaw/",
        f"  Excluded from copy: {exclude_summary}",
        "  Steps:",
        f"    1. launchctl bootout system/{label}",
        f"    2. sudo cp -a each non-excluded child of "
        f"/Users/{current_user}/.openclaw/ → /Users/evo/.openclaw/",
        "    3. patch /Users/evo/.openclaw/openclaw.json "
        "agents.defaults.workspace → /Users/evo/.openclaw/workspace",
        "    4. sudo chown -R evo:staff /Users/evo/.openclaw/",
        "    5. ACL grants: set_evolve_read_acl('evo') + evo write ACL "
        "on {sharedDir}/{proposals,signals}/",
        f"    5b. migrate backup SSH key(s) /Users/evolve/.ssh/"
        f"evolve-backup-{{{primary_id},shared}}* → /Users/evo/.ssh/ "
        f"(so ai.evolve.{primary_id}.backup can push under the new user)",
        f"    6. network.json: bots.{primary_id}.user = 'evo' "
        f"(was '{current_user}')",
        "    7. rewrite gateway plist with UserName=evo + /Users/evo/ "
        "paths",
        "    8. launchctl bootstrap system <plist>",
        "    9. verify gateway responds on its port",
    ]
    return lines


def _evo_cutover_bootout(label: str) -> bool:
    """Bootout the gateway and wait for the label to disappear (≤ 10s).

    Returns True if the label is gone after the wait (or wasn't loaded
    in the first place). False only if the label is still present after
    polling — that's a state we don't want to copy files on top of.
    """
    sched = get_launchd_scheduler()
    was_loaded = sched.status(label)["managed"]
    if not was_loaded:
        return True

    # Bare bootout via raw() — NOT Scheduler.remove(), which would also
    # delete the plist. The old plist must stay on disk until step 7
    # rewrites it, so a cutover halted here remains recoverable.
    sched.raw("bootout", f"system/{label}")
    for _ in range(20):  # up to 10 s
        time.sleep(0.5)
        if not sched.status(label)["managed"]:
            return True
    return False


def _evo_cutover_copy_state(current_user: str) -> tuple[bool, str | None]:
    """Copy every per-bot subtree from /Users/{current_user}/.openclaw/
    to /Users/evo/.openclaw/. Uses ``cp -a`` so ownership/mode/ACL/xattr
    survive the move (downstream chown rewrites ownership anyway).

    Uses a deny-list, not an allowlist: the production .openclaw/ tree
    has too many OC-managed subdirs (memory/, identity/, telegram/,
    plugins/, devices/, flows/, …) for a static allowlist to be safe.
    Anything under .openclaw/ that isn't logs/ or a clobbered-backup
    file is per-bot state and travels. See ``EVO_CUTOVER_EXCLUDE_*``.

    Returns (True, None) on success, (False, reason) on the first
    failure (halts mid-migration rather than pressing through a partial
    state).
    """
    src_root = Path(f"/Users/{current_user}/.openclaw")
    dst_root = Path("/Users/evo/.openclaw")

    # Enumerate src_root via sudo /bin/ls — Path.iterdir() fails when the
    # admin user lacks search on the bot home, which is the common case
    # (mode 700). The set_evolve_read_acl grant covers the `evolve` user
    # specifically, not whoever runs this CLI command (typically root via
    # sudo, which can list anyway — but use /bin/ls -1 so the code works
    # under either user).
    listing = subprocess.run(
        ["sudo", "/bin/ls", "-1A", str(src_root)],
        capture_output=True, text=True,
    )
    if listing.returncode != 0:
        return False, (
            f"failed to list {src_root}: {listing.stderr.strip()[:200]}"
        )
    children = [c for c in listing.stdout.splitlines() if c.strip()]
    if not children:
        return False, f"{src_root} appears empty — nothing to migrate"

    copied: list[str] = []
    skipped: list[str] = []
    for name in children:
        if not _evo_cutover_should_copy(name):
            skipped.append(name)
            continue
        src = src_root / name
        dst = dst_root / name
        # If the dest already exists (E.2.a placeholder tree, or a
        # partial earlier attempt), remove first so cp -a lands cleanly.
        # Done as -rf because everything under /Users/evo/.openclaw was
        # created by E.2.a or by a prior cutover attempt; nothing
        # user-owned to protect.
        rm_res = subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(dst)],
            capture_output=True, text=True,
        )
        if rm_res.returncode != 0:
            return False, (
                f"failed to clear destination {dst}: "
                f"{rm_res.stderr.strip()[:200]}"
            )
        r = subprocess.run(
            ["sudo", "/bin/cp", "-a", str(src), str(dst)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"failed to copy {src} → {dst}: {r.stderr.strip()[:200]}"
            )
        copied.append(name)
    # Persist a one-line summary in the result via the standard log
    # channel — operators can scan stderr/Console.app afterwards. The
    # outer driver also _ok-prints a high-level "copied state" line.
    if skipped:
        _info(
            f"    skipped (excluded by cutover policy): "
            f"{', '.join(sorted(skipped)[:10])}"
            + (f" + {len(skipped) - 10} more" if len(skipped) > 10 else "")
        )
    _info(f"    copied {len(copied)} entries under {src_root}")
    return True, None


def _evo_cutover_patch_openclaw_json(current_user: str) -> tuple[bool, str | None]:
    """Update workspace path inside the migrated openclaw.json so the OC
    runtime, started under the `evo` user, doesn't try to chdir into
    /Users/{current_user}/.openclaw/workspace (which evo can't access).

    Reads via sudo /bin/cat (the file is owned by the source user at
    this point in the sequence; ownership rewrite happens after). Writes
    via /tmp staging + sudo cp.

    Idempotent: if the workspace path is already pointed at /Users/evo/,
    leaves the file alone.
    """
    dst_oc_json = Path("/Users/evo/.openclaw/openclaw.json")
    r = subprocess.run(
        ["sudo", "/bin/cat", str(dst_oc_json)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, (
            f"could not read {dst_oc_json}: {r.stderr.strip()[:200]}"
        )
    try:
        cfg = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return False, f"{dst_oc_json} is not valid JSON: {exc}"

    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    current_ws = defaults.get("workspace") or ""
    target_ws = "/Users/evo/.openclaw/workspace"

    # Rewrite if it points at the old user's path. If the operator has
    # customized to an absolute path that doesn't reference either
    # user's home, leave it — they'll handle it.
    if current_ws.startswith(f"/Users/{current_user}/.openclaw/"):
        defaults["workspace"] = current_ws.replace(
            f"/Users/{current_user}/.openclaw/",
            "/Users/evo/.openclaw/",
            1,
        )
    elif not current_ws:
        defaults["workspace"] = target_ws

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evo-cutover-oc-", suffix=".json")
    with os.fdopen(fd, "w") as fp:
        json.dump(cfg, fp, indent=2)
    r2 = subprocess.run(
        ["sudo", "/bin/cp", tmp, str(dst_oc_json)],
        capture_output=True, text=True,
    )
    os.unlink(tmp)
    if r2.returncode != 0:
        return False, (
            f"failed to write patched {dst_oc_json}: "
            f"{r2.stderr.strip()[:200]}"
        )
    return True, None


def _evo_cutover_chown_to_evo() -> tuple[bool, str | None]:
    """Recursive chown of /Users/evo/.openclaw to evo:staff."""
    r = subprocess.run(
        ["sudo", "/usr/sbin/chown", "-R", "evo:staff", "/Users/evo/.openclaw"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, (
            f"chown -R evo:staff failed: {r.stderr.strip()[:200]}"
        )
    return True, None


def _evo_cutover_ensure_evo_ssh_dir() -> tuple[bool, str | None]:
    """Ensure /Users/evo/.ssh/ exists with mode 700 owned by evo:staff.

    Idempotent — stats first and only fires mkdir/chmod/chown when the
    actual state differs from the target. Returns (True, None) on success
    or no-op; (False, reason) if any shell step fails.

    Symlink gate (D-2): all three legs below run as ROOT against a path inside
    ``/Users/evo``, and ``evo`` OWNS its own home — so it can replace ``.ssh``
    with a symlink. None of ``mkdir -p`` / ``chmod 700`` / ``chown evo:staff``
    is passed a no-follow flag, so a plant would chmod an arbitrary directory to
    0700 and hand ``evo`` ownership of it. The probes above the legs make it
    worse, not safer: ``target.exists()`` and ``target.stat()`` both FOLLOW, so a
    link aimed at any 0700 evo-owned dir reads as "already correct" while a link
    aimed anywhere else reads as exactly the drift the repair exists to fix.
    Gated once, before the probes, since every leg shares the one dest.
    """
    import stat as _stat
    import pwd as _pwd

    target = EVO_SSH_DIR
    if why := redirect_refusal(target):
        return False, f"refusing to repair {target}: {why}"
    needs_create = not target.exists()
    needs_fix_mode = False
    needs_fix_owner = False

    if not needs_create:
        try:
            st = target.stat()
            if _stat.S_IMODE(st.st_mode) != 0o700:
                needs_fix_mode = True
            try:
                evo_uid = _pwd.getpwnam("evo").pw_uid
                if st.st_uid != evo_uid:
                    needs_fix_owner = True
            except KeyError:
                # `evo` user missing here would have failed preconditions
                # already; treat as ownership-needs-fix so the chown below
                # will surface a clear error.
                needs_fix_owner = True
        except OSError as exc:
            return False, f"could not stat {target}: {exc}"

    if needs_create:
        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", str(target)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"mkdir -p {target} failed: {r.stderr.strip()[:200]}"
            )
    if needs_create or needs_fix_mode:
        r = subprocess.run(
            ["sudo", "/bin/chmod", "700", str(target)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"chmod 700 {target} failed: {r.stderr.strip()[:200]}"
            )
    if needs_create or needs_fix_owner:
        r = subprocess.run(
            ["sudo", "/usr/sbin/chown", "evo:staff", str(target)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"chown evo:staff {target} failed: {r.stderr.strip()[:200]}"
            )
    return True, None


def _evo_cutover_copy_ssh_keypair(name: str) -> tuple[bool, str | None]:
    """Copy /Users/evolve/.ssh/{name}{,.pub} → /Users/evo/.ssh/{name}{,.pub}.

    - If the source private key is missing, returns (True, None) — nothing
      to migrate, not an error (the operator may simply not have generated
      this key, e.g. the shared deploy key is opt-in).
    - If the target private key is missing, sudo cp from source.
    - Always re-asserts ownership (evo:staff) and mode (priv=600, pub=644)
      on each target file that exists, so re-running after a partial
      earlier attempt converges on the correct shape.

    Does NOT touch the source files — leaves /Users/evolve/.ssh/ intact in
    case other daemons still resolve to it during some boot orderings.
    """
    src_priv = EVOLVE_SSH_DIR / name
    src_pub = EVOLVE_SSH_DIR / f"{name}.pub"
    dst_priv = EVO_SSH_DIR / name
    dst_pub = EVO_SSH_DIR / f"{name}.pub"

    if not src_priv.exists():
        return True, None

    for src, dst, mode in (
        (src_priv, dst_priv, "600"),
        (src_pub, dst_pub, "644"),
    ):
        if not src.exists():
            continue
        if not dst.exists():
            r = subprocess.run(
                ["sudo", "/bin/cp", str(src), str(dst)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                return False, (
                    f"cp {src} → {dst} failed: {r.stderr.strip()[:200]}"
                )
        r = subprocess.run(
            ["sudo", "/usr/sbin/chown", "evo:staff", str(dst)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"chown evo:staff {dst} failed: {r.stderr.strip()[:200]}"
            )
        r = subprocess.run(
            ["sudo", "/bin/chmod", mode, str(dst)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"chmod {mode} {dst} failed: {r.stderr.strip()[:200]}"
            )
    return True, None


def _evo_cutover_migrate_backup_ssh_key(
    primary_id: str,
) -> tuple[bool, str | None]:
    """Migrate the primary bot's backup SSH deploy key(s) into /Users/evo/.ssh/.

    Post-cutover, ``ai.evolve.<primary>.backup`` runs as the `evo` user;
    ``backup.ssh_key_path(bot_id)`` resolves to ``Path.home() / ".ssh" /
    f"evolve-backup-{bot_id}"`` → ``/Users/evo/.ssh/evolve-backup-<primary>``.
    On legacy pre-unification pods the per-bot key may still live at
    ``/Users/evolve/.ssh/evolve-backup-<primary>``; under the unified
    shared-key model (docs/spec-backup-key-distribution-unification-
    2026-06-08.md) only ``evolve-backup-shared`` exists pod-wide and
    ``evolve_admin.backup_keys.ensure_bot_in_sync`` is the path that
    populates the per-bot file under ``/Users/evo/.ssh/`` on the next
    deploy. Without this step the daemon hits ``Host key verification
    failed`` because ``backup._ssh_env`` returns an empty dict when the
    key isn't where it expects, dropping ``-i`` and
    ``-o StrictHostKeyChecking=accept-new`` from the git push.

    Covers both legacy naming conventions in play:
      - ``evolve-backup-<primary_id>`` — pre-unification per-bot key.
        Absent on fresh post-unification installs.
      - ``evolve-backup-shared`` — the canonical pod-wide source under
        the unified model. Not directly read by the daemon (which reads
        the per-bot file), but copying it keeps the regen path working
        from the new home.

    Source files are left in /Users/evolve/.ssh/ — the source-of-truth
    question for post-Phase-E deploy keys is resolved by the
    unification spec referenced above.

    Best-effort and idempotent: collects errors across substeps and
    returns ok=False if any failed, but the caller treats the step as
    non-fatal (logs a warning and continues).
    """
    candidates = [
        f"evolve-backup-{primary_id}",
        "evolve-backup-shared",
    ]
    present = [n for n in candidates if (EVOLVE_SSH_DIR / n).exists()]
    if not present:
        return True, None

    ok, err = _evo_cutover_ensure_evo_ssh_dir()
    if not ok:
        return False, err

    errors: list[str] = []
    for name in present:
        ok, err = _evo_cutover_copy_ssh_keypair(name)
        if not ok and err:
            errors.append(err)
    if errors:
        return False, "; ".join(errors)
    return True, None


def _evo_cutover_apply_acl() -> tuple[bool, str | None]:
    """Re-apply ACL grants so the admin daemon (evolve user) can still
    read /Users/evo/.openclaw/, and so the freshly-on-its-own-account
    evo user can still write to the proposal + signal stores.

    Two ACL grants:
      1. set_evolve_read_acl('evo') — evolve reads /Users/evo/.openclaw/.
         Without it, every Phase-E.3 endpoint that proxies "tell me what
         evo's config looks like" loses access.
      2. _ensure_evo_write_acl on {sharedDir}/{proposals,signals}/ —
         evo writes state-transition files into evolve-owned dirs.
         Without it, dismiss/snooze/resolve from the UI fails with
         EACCES on the os.replace unlink (the 2026-05-25 symptom that
         motivated this fix landing).

    Both are idempotent + best-effort; failure here is reported but the
    cutover doesn't abort. ensure_pod_perms on the next deploy re-asserts
    either grant if it didn't take here.
    """
    errors: list[str] = []
    try:
        from .deploy import set_evolve_read_acl
        set_evolve_read_acl("evo")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"set_evolve_read_acl('evo') failed: {exc}")
    try:
        from .deploy import (
            EVO_WRITE_SHARED_SUBDIRS,
            _ensure_evo_write_acl,
        )
        shared_dir = Path(DEFAULT_SHARED_DIR)
        for subdir in EVO_WRITE_SHARED_SUBDIRS:
            p = shared_dir / subdir
            if p.exists():
                _ensure_evo_write_acl(p)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"evo write-ACL grant failed: {exc}")
    if errors:
        return False, "; ".join(errors)
    return True, None


def _evo_cutover_update_network(
    network_path: Path, primary_id: str,
) -> tuple[bool, str | None]:
    """Flip ``bots.<primary>.user`` to ``"evo"`` in network.json.

    Uses ``save_network`` (atomic temp-file + rename); the file lives
    under {shared_dir} which the admin daemon owns, so no sudo needed.
    """
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not re-read network.json: {exc}"

    bots = net.setdefault("bots", {})
    entry = bots.setdefault(primary_id, {})
    entry["user"] = "evo"

    try:
        save_network(net, network_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not write network.json: {exc}"
    return True, None


def _evo_cutover_rewrite_plist(plist_path: Path) -> tuple[bool, str | None]:
    """Rewrite the gateway plist: UserName, log paths, HOME env all
    flip from `evolve` to `evo`. Read the current plist verbatim, sub
    `/Users/evolve/` → `/Users/evo/` and `<string>evolve</string>` →
    `<string>evo</string>` for the UserName tag specifically. The swaps
    are surgical — they never touch the launchd Label, which is stable
    across the cutover (a naive s/evolve/evo/g would corrupt a legacy
    ``ai.openclaw.evolve-gateway`` label; see EVO_CUTOVER_LABEL).
    """
    r = subprocess.run(
        ["sudo", "/bin/cat", str(plist_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"could not read plist {plist_path}: {r.stderr.strip()[:200]}"
    content = r.stdout

    # UserName: replace the value under the <key>UserName</key> tag.
    # The setup wizard renders this as:
    #   <key>UserName</key>
    #   <string>evolve</string>
    # so we look for that exact pair to avoid swapping the wrong string.
    new_content = content.replace(
        "<key>UserName</key>\n    <string>evolve</string>",
        "<key>UserName</key>\n    <string>evo</string>",
        1,
    )
    if new_content == content:
        # Fall back to a less-strict match — the indent may vary by
        # whoever wrote the plist (setup wizard vs. manual edit).
        import re as _re
        new_content = _re.sub(
            r"(<key>UserName</key>\s*<string>)evolve(</string>)",
            r"\1evo\2",
            content,
            count=1,
        )
    if new_content == content:
        return False, (
            f"could not find <key>UserName</key><string>evolve</string> "
            f"in {plist_path}; nothing to rewrite"
        )

    # Path swaps: every /Users/evolve/ reference in the plist (log
    # paths, HOME env var, etc.) flips to /Users/evo/. The launchd
    # label string (evo-gateway / legacy evolve-gateway) is intentionally
    # left alone — see EVO_CUTOVER_LABEL.
    new_content = new_content.replace("/Users/evolve/", "/Users/evo/")

    # HOME env var: `<string>/Users/evolve</string>` (no trailing slash
    # — the path-prefix swap above leaves this one in place). Match
    # both `<key>HOME</key>` siblings and the bare evolve home string.
    new_content = new_content.replace(
        "<string>/Users/evolve</string>",
        "<string>/Users/evo</string>",
    )

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evo-cutover-plist-", suffix=".plist")
    with os.fdopen(fd, "w") as fp:
        fp.write(new_content)
    try:
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(plist_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, (
                f"failed to write rewritten plist: {r.stderr.strip()[:200]}"
            )
        subprocess.run(
            ["sudo", "/usr/sbin/chown", "root:wheel", str(plist_path)],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(plist_path)],
            capture_output=True,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # Make sure /Users/evo/.openclaw/logs/ exists — launchd error 5
    # (I/O error) on bootstrap almost always traces back to a
    # StandardOutPath/StandardErrorPath whose directory is missing.
    log_dir = Path("/Users/evo/.openclaw/logs")
    subprocess.run(["sudo", "/bin/mkdir", "-p", str(log_dir)], capture_output=True)
    subprocess.run(
        ["sudo", "/usr/sbin/chown", "evo:staff", str(log_dir)],
        capture_output=True,
    )
    return True, None


def _evo_cutover_bootstrap(plist_path: Path) -> tuple[bool, str | None]:
    """Bootstrap the rewritten plist. Falls back to kickstart if
    bootstrap fails (mirrors the setup wizard's gateway-install
    fallback).
    """
    # raw() rather than Scheduler.install(): the rewritten plist was staged
    # by _evo_cutover_rewrite_plist, and install()'s byte-identical skip
    # would refuse to re-register the (booted-out) gateway. raw() also keeps
    # stderr separate so the diagnostic below stays byte-equivalent.
    sched = get_launchd_scheduler()
    rc, _out, err = sched.raw("bootstrap", "system", str(plist_path))
    if rc == 0:
        return True, None
    # bootstrap failed — try kickstart as a fallback. Derive the label from
    # the plist filename so this stays correct for the primary-id-resolved
    # label (evo-gateway / evolve-gateway) without re-reading network.json.
    label = plist_path.stem
    krc, _kout, kerr = sched.raw("kickstart", "-k", f"system/{label}")
    if krc == 0:
        return True, None
    return False, (
        f"both bootstrap and kickstart failed. bootstrap: "
        f"{err.strip()[:200]}; kickstart: {kerr.strip()[:200]}"
    )


def _evo_cutover_verify(port: int = _EVOLVE_PORT, timeout_sec: int = 30) -> bool:
    """Poll the gateway's /health endpoint for up to ``timeout_sec``."""
    import urllib.request as _ur
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
                return True
        except Exception:
            time.sleep(2)
    return False


def _perform_evo_cutover(
    network_path: Path,
    dry_run: bool = True,
) -> bool:
    """Migrate evo's gateway from `evolve` → `evo` macOS user.

    Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.b".

    Preconditions: E.2.a has run (the `evo` user exists with an empty
    .openclaw/ tree) AND the Phase-E.3 admin-daemon endpoints are
    deployed (so post-cutover evo can still do cross-bot work via
    the unix socket).

    Modes:
      - ``dry_run=True`` (default): print the plan, run no mutations.
      - ``dry_run=False``: execute every step. Halts on first error
        with a diagnostic. State left at whatever step failed —
        operator's call to recover.

    Idempotent: re-running after a successful cutover is a no-op
    (precondition check sees ``bots.<primary>.user == "evo"`` plus a
    populated /Users/evo/.openclaw/ and returns True without touching
    anything).

    Returns True on success / no-op; False on any failure during
    apply. Dry-run always returns True (planning never fails — the
    precondition check returns False before we get here for
    unsatisfied preconditions).
    """
    console.print()
    console.print(
        f"  [bold]Phase E.2.b cutover — evo's gateway to the `evo` user "
        f"(dry-run={dry_run})[/]"
    )

    ok, err, ctx = _evo_cutover_preconditions(network_path)
    if not ok:
        _err(err or "preconditions failed")
        return False

    if ctx["already_done"]:
        _ok("Already cut over — bots.<primary>.user is 'evo' and "
            "/Users/evo/.openclaw/openclaw.json exists. Nothing to do.")
        return True

    for line in _evo_cutover_describe(ctx):
        console.print(line)

    if dry_run:
        console.print()
        _info("Dry-run — no changes applied. Re-run with --confirm to "
              "execute.")
        return True

    primary_id = ctx["primary_id"]
    current_user = ctx["current_user"]
    plist_path = ctx["plist_path"]
    label = ctx["label"]

    console.print()
    _info("Applying cutover steps...")

    # Step 1: bootout the gateway
    if not _evo_cutover_bootout(label):
        _err(f"could not bootout {label} (label still loaded "
             "after 10s). Aborting to avoid copying state under a "
             "running process.")
        return False
    _ok(f"bootout {label}")
    _log_admin_action("evo_cutover_bootout", "ok", bot=primary_id)

    # Step 2: copy state
    ok, err = _evo_cutover_copy_state(current_user)
    if not ok:
        _err(err or "state copy failed")
        return False
    _ok(f"copied per-bot state /Users/{current_user}/.openclaw/ → "
        "/Users/evo/.openclaw/")
    _log_admin_action("evo_cutover_copy_state", "ok", bot=primary_id)

    # Step 3: patch openclaw.json's workspace path
    ok, err = _evo_cutover_patch_openclaw_json(current_user)
    if not ok:
        _err(err or "openclaw.json patch failed")
        return False
    _ok("patched openclaw.json workspace path")

    # Step 4: chown
    ok, err = _evo_cutover_chown_to_evo()
    if not ok:
        _err(err or "chown failed")
        return False
    _ok("chown -R evo:staff /Users/evo/.openclaw")

    # Step 5: ACL re-grant (non-fatal — best effort)
    ok, err = _evo_cutover_apply_acl()
    if not ok:
        _warn(err or "ACL grant failed (continuing — re-run after cutover "
              "completes)")
    else:
        _ok("set_evolve_read_acl('evo') + evo write ACL on proposals/, signals/")

    # Step 5b: migrate primary-bot backup SSH key (non-fatal — best effort).
    # Without this, ai.evolve.<primary>.backup hits "Host key verification
    # failed" because backup.ssh_key_path resolves to /Users/evo/.ssh/…
    # under the new account. See _evo_cutover_migrate_backup_ssh_key docstring.
    ok, err = _evo_cutover_migrate_backup_ssh_key(primary_id)
    if not ok:
        _warn(err or
              "backup SSH key migration failed (continuing — re-run after "
              "cutover completes, or copy manually with sudo cp "
              f"/Users/evolve/.ssh/evolve-backup-{primary_id}* "
              "/Users/evo/.ssh/)")
    else:
        _ok(f"migrated backup SSH key(s) to /Users/evo/.ssh/ "
            f"(for ai.evolve.{primary_id}.backup)")

    # Step 6: update network.json
    ok, err = _evo_cutover_update_network(network_path, primary_id)
    if not ok:
        _err(err or "network.json update failed")
        return False
    _ok(f"network.json: bots.{primary_id}.user = 'evo'")
    _log_admin_action(
        "evo_cutover_network_update", "ok", bot=primary_id,
    )

    # Step 7: rewrite plist
    ok, err = _evo_cutover_rewrite_plist(plist_path)
    if not ok:
        _err(err or "plist rewrite failed")
        return False
    _ok(f"rewrote {plist_path} (UserName=evo, /Users/evo/ paths)")

    # Step 8: bootstrap
    ok, err = _evo_cutover_bootstrap(plist_path)
    if not ok:
        _err(err or "bootstrap failed")
        return False
    _ok("bootstrap system <plist>")
    _log_admin_action(
        "evo_cutover_bootstrap", "ok", bot=primary_id,
    )

    # Step 9: verify
    if _evo_cutover_verify():
        _ok(f"gateway responding on port {_EVOLVE_PORT}")
        _log_admin_action(
            "evo_cutover_verify", "ok", bot=primary_id,
        )
    else:
        _warn(
            f"gateway did not respond on port {_EVOLVE_PORT} within 30s. "
            "Check logs at /Users/evo/.openclaw/logs/ and consider "
            "running 'sudo launchctl kickstart -k "
            f"system/{label}'."
        )
        # Don't return False — the cutover state changes have all
        # landed; only the post-restart health probe failed. Operator
        # can investigate without re-running the migration.

    console.print()
    _ok("Phase E.2.b cutover complete.")
    _info(
        "Next: observe evo for one or more sessions, then ship Phase E.4 "
        "to remove the primary-bot exec-deny carve-out."
    )
    return True


def _render_admin_sudoers(admin_user: str, bots: list) -> str:
    """Render the /etc/sudoers.d/evolve-admin content (pure — no disk).

    Same ONE-writer-TWO-command-tables discipline as
    ``_render_evolve_sudoers`` (design-linux-port §5): every binary path
    and the home root come from ``platform_profile.get_profile()``. The
    macOS render is byte-identical to the pre-parameterization output
    (pinned by ``tests/test_admin_sudoers_platform_profile.py``).
    """
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands
    macos = profile.name == "macos"
    cat = c["cat"]
    svc = c["service_manager"]  # launchctl (macOS) / systemctl (Linux)
    home = profile.user_home_root

    lines = [
        "# Evolve admin access — allows reading bot config files without password",
        "# Written by evolve-admin setup wizard",
        "",
    ]

    for bot in bots:
        name = bot.name if hasattr(bot, "name") else str(bot)
        lines += [
            f"# Allow {admin_user} to read {name} config files",
            f"{admin_user} ALL=({name}) NOPASSWD: {cat} {home}/{name}/.openclaw/openclaw.json",
            f"{admin_user} ALL=({name}) NOPASSWD: {cat} {home}/{name}/.openclaw/agents/main/agent/auth-profiles.json",
            f"{admin_user} ALL=({name}) NOPASSWD: /usr/bin/crontab -l",
            "",
        ]

    if macos:
        lines += [
            "# Allow gateway restart operations",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} stop ai.openclaw.*-gateway",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} start ai.openclaw.*-gateway",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} kickstart -k system/ai.openclaw.*-gateway",
            "",
        ]
    else:
        # systemd has no kickstart; restart is the equivalent verb. Same
        # ai.openclaw.* label family the SystemdScheduler manages (the
        # gateway unit name carries the -gateway suffix inside the label).
        lines += [
            "# Allow gateway restart operations (systemd verbs)",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} stop ai.openclaw.*",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} start ai.openclaw.*",
            f"{admin_user} ALL=(ALL) NOPASSWD: {svc} restart ai.openclaw.*",
            "",
        ]

    return "\n".join(lines) + "\n"


def _write_sudoers(admin_user: str, bots: list) -> bool:
    """
    Write /etc/sudoers.d/evolve-admin with passwordless grants for admin_user.
    Derived dynamically from the bot list. Returns True on success.
    """
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands
    content = _render_admin_sudoers(admin_user, bots)

    # Validate with visudo -c before installing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    # The four sudoers writes below are intentionally UNGRANTED to the evolve
    # service user (Option B, PR #2759): the service user must never be able
    # to rewrite its own sudoers. These run as the operator/root via the setup
    # wizard; when reached non-interactively the refresh fails by design and
    # raises sudoers-refresh-failed immediately so the operator re-runs it.
    # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    r = subprocess.run(
        ["sudo", c["visudo"], "-c", "-f", tmp_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        os.unlink(tmp_path)
        _err(f"sudoers validation failed: {r.stderr.strip()}")
        return False

    dst = Path("/etc/sudoers.d/evolve-admin")
    # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    r2 = subprocess.run(
        ["sudo", c["cp"], tmp_path, str(dst)],
        capture_output=True, text=True,
    )
    os.unlink(tmp_path)
    if r2.returncode != 0:
        _err(f"Failed to install sudoers file: {r2.stderr.strip()}")
        return False

    # wheel is the macOS root group; root is its own group on Linux.
    root_group = "root:wheel" if profile.name == "macos" else "root:root"
    subprocess.run(["sudo", c["chmod"], "440", str(dst)], capture_output=True)  # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    subprocess.run(["sudo", c["chown"], root_group, str(dst)], capture_output=True)  # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    _ok(f"  /etc/sudoers.d/evolve-admin written for admin user '{admin_user}'")
    _log_admin_action("write_sudoers", "ok", bot="", initiated_by="wizard")
    return True


def _find_openclaw_path() -> str | None:
    """Discover the openclaw CLI binary path on this machine.

    Delegates to ``platform_profile.find_openclaw_cli()`` — the single shared
    resolver also used by deploy._openclaw_bin() / safe_upgrade and the
    analyzer dispatcher. The path resolved here gets baked into the sudoers
    `evolve ALL=(ALL) NOPASSWD: ...` grant, so it must match what the runtime
    callers will actually invoke. Keep returning None when nothing is found —
    the sudoers render must fail loudly rather than bake a bare name.
    """
    from platform_profile import find_openclaw_cli
    return find_openclaw_cli()


def _render_evolve_sudoers() -> str | None:
    """Render the /etc/sudoers.d/evolve file content.

    Pure function — does not write to disk. Used by _write_evolve_sudoers
    (which adds visudo-check + install-with-sudo) and by the
    `evolve-admin refresh-sudoers --dry-run` command (which just prints
    the rendered content).

    ONE writer, TWO command tables (design-linux-port §5): every binary
    path and platform root comes from ``platform_profile.get_profile()``
    — the same table the runtime adapters invoke — so "what evolve may
    sudo" and "what the code runs" cannot drift apart. Both renders use
    the strictest-common-denominator sudoers syntax (no trailing ``/*``,
    no escaped dots, no bare ``:`` in argument patterns — macOS visudo
    rejects all three; Linux visudo accepts the common subset). The
    macOS render is pinned byte-for-byte by
    tests/test_sudoers_platform_profile.py — exact-match grants mean a
    single drifted byte is a silently dead grant.

    Returns None if openclaw path can't be discovered (rare — only
    when openclaw isn't installed). Callers should surface that error.
    """
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands
    macos = profile.name == "macos"

    # Binary paths — single source: the profile's command table.
    cat = c["cat"]
    cp = c["cp"]
    chmod = c["chmod"]
    chown = c["chown"]
    mkdir = c["mkdir"]
    rm = c["rm"]
    ls = c["ls"]
    truncate = c["truncate"]
    kill = c["kill"]
    visudo = c["visudo"]
    sqlite3 = c["sqlite3"]
    lsof = c["lsof"]
    sshd = c["sshd"]
    git = c["git"]
    crontab = c["crontab"]
    python3 = c["python3"]
    venv_python = c["venv_python"]  # deploy-venv python (has packaged evolve_admin)
    svc = c["service_manager"]  # launchctl (macOS) / systemctl (Linux)
    # Linux-only ACL tooling (absent from the macOS table — macOS ACLs are
    # chmod +a/-N). Referenced only inside the linux branches below.
    # (getfacl is deliberately NOT granted: LinuxPerms' probes run
    # unprivileged — see the 9b3 removal note below.)
    setfacl = c.get("setfacl")

    # Platform roots — same profile, same single source.
    home = profile.user_home_root            # /Users vs /home
    shared = profile.shared_dir_default      # /Users/Shared/evolve vs /var/lib/evolve
    repo = profile.deploy_checkout_default   # deploy checkout
    daemons = profile.daemon_dir             # /Library/LaunchDaemons vs /etc/systemd/system
    plugin_dir = f"{shared}-plugin"          # /Users/Shared/evolve-plugin (macOS)

    # sudo-spawned PATH: Homebrew prefixes are a macOS-only concern; on
    # Linux the NodeSource node lands in /usr/bin (design §6 runtime row).
    # The Linux branch also prepends the deploy venv's bin ({venv_dir}/bin,
    # /var/lib/evolve-venv/bin) so bare `sudo evolve-admin …` resolves: the
    # `evolve-admin` console script lives there, not on the system PATH, and
    # without it on secure_path every `sudo evolve-admin deploy <bot>` fails
    # with "command not found" (W10 #1). macOS keeps its pre-W10 string
    # byte-identical — the macOS golden must not move; the dev mini's
    # interactive PATH happens to mask the same latent gap there (out of
    # scope for this Linux-only wave).
    secure_path = (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" if macos
        else f"{profile.venv_dir}/bin:/usr/local/bin:/usr/bin:/bin"
    )

    # Audit-kick interpreter: audit_dispatch._kick_runner invokes the
    # Homebrew python3 verbatim on macOS; the Linux grant renders the
    # system python (the dispatch code itself is ported in a later wave).
    audit_python = "/opt/homebrew/bin/python3" if macos else python3

    oc_path = _find_openclaw_path()
    analyzer_dir = f"{repo}/packages/analyzer"

    content = (
        "# /etc/sudoers.d/evolve\n"
        "# Evolve infrastructure bot — narrow sudo grants\n"
        "# Managed by: docs/archive/specs/spec-sudoers.md\n"
        "# Written by: evolve-admin setup wizard\n"
        "\n"
        "Defaults:evolve !requiretty\n"
        "\n"
        "# secure_path provides PATH to every sudo-spawned process. Without\n"
        "# this, sudo clears PATH and Node.js binaries (`openclaw` has a\n"
        "# `#!/usr/bin/env node` shebang) fail to find `node`. Previously\n"
        "# deploy.py worked around this with `sudo -u <bot> env HOME=... PATH=...\n"
        "# openclaw ...` wrappers, but those invocations don't match the\n"
        "# `evolve ALL=(ALL) NOPASSWD: <oc_path>` grant below (sudo sees the\n"
        "# command as `env`, not `openclaw`), so the fallback was a password\n"
        "# prompt and guaranteed deploy failure. Putting PATH in secure_path\n"
        "# lets the python code drop the env wrapper entirely.\n"
        "#\n"
        "# Two scopes are needed because `evolve-admin deploy --all` is invoked\n"
        "# under outer `sudo` (admin → root). The inner `sudo -u <bot> openclaw\n"
        "# config validate ...` from ensure_plugin_config has root as the\n"
        "# invoking user, so `Defaults:evolve secure_path` doesn't apply and\n"
        "# the openclaw shebang's `env node` lookup fails. The unqualified\n"
        "# `Defaults secure_path` covers root (and any other invoker) so the\n"
        "# deploy path works end-to-end. The narrow `Defaults:evolve` line is\n"
        "# kept as belt-and-suspenders for the admin-ui daemon's runtime path.\n"
        f'Defaults secure_path = "{secure_path}"\n'
        f'Defaults:evolve secure_path = "{secure_path}"\n'
        "\n"
        "# ── 1. Read bot openclaw.json files ──────────────────────────────────────────\n"
        "# Belt-and-suspenders fallback; ACL inheritance (set_evolve_read_acl) is primary.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/openclaw.json\n"
        "\n"
        "# ── 2. Read bot auth-profiles.json files ─────────────────────────────────────\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/agents/main/agent/auth-profiles.json\n"
        "\n"
        "# ── 2a. Read the per-agent OpenClaw auth store (sqlite) ──────────────────────\n"
        "# OpenClaw 2026.6 migrated auth-profiles.json into a per-agent sqlite store\n"
        "# (openclaw-agent.sqlite). oc_store.read_auth_store opens it read-only as the\n"
        "# evolve user via the read ACL; this grant is the pre-ACL fallback. The\n"
        "# `-readonly` flag forces a read-only open at the binary level, so no SQL\n"
        "# argument can mutate the DB — the trailing `*` only carries the fixed\n"
        "# SELECT statement. Belt-and-suspenders, like the §2 cat grant above.\n"
        f"evolve ALL=(root) NOPASSWD: {sqlite3} -readonly {home}/*/.openclaw/agents/main/agent/openclaw-agent.sqlite *\n"
        "\n"
        "# ── 3. Read bot gateway logs ─────────────────────────────────────────────────\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/logs/gateway.log\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/logs/gateway.err.log\n"
        "\n"
        + (
            "# ── 3.1. Truncate launchd-captured gateway logs (ai.evolve.evolve.oc-log-rotate) ─\n"
            "# OC's own logger writes to logging.file (bounded via maxFileBytes); these\n"
            "# two files are populated by the gateway plist's StandardOut/ErrorPath\n"
            "# capture and launchd has no rotation for them. The daily oc-log-rotate\n"
            "# cron truncates each to zero when it crosses 10MB. Pinned to the exact\n"
            "# filenames so the grant can't be used to clobber other bot files.\n"
            if macos else
            "# ── 3.1. Truncate unit-captured gateway logs (ai.evolve.evolve.oc-log-rotate) ─\n"
            "# OC's own logger writes to logging.file (bounded via maxFileBytes); these\n"
            "# two files are populated by the gateway unit's StandardOutput/Error\n"
            "# append: capture, which systemd never rotates. The daily oc-log-rotate\n"
            "# job truncates each to zero when it crosses 10MB. Pinned to the exact\n"
            "# filenames so the grant can't be used to clobber other bot files.\n"
        )
        + f"evolve ALL=(root) NOPASSWD: {truncate} -s 0 {home}/*/.openclaw/logs/gateway.log\n"
        f"evolve ALL=(root) NOPASSWD: {truncate} -s 0 {home}/*/.openclaw/logs/gateway.err.log\n"
        "\n"
        "# ── 3.2. One-click disk reclaim (disk_reclaim_apply, POST /api/host-health/reclaim) ─\n"
        "# The 'Reclaim space' remediation on the disk_low alert frees the\n"
        "# regenerable npm caches disk_reclaim.scan_reclaimable reports (rm the\n"
        "# whole _cacache/_npx dir). Logs are intentionally NOT one-click\n"
        "# reclaimable: they are the smaller win and already bounded at source\n"
        "# (mcp-bridge rotation + the daily log_cap job), and a truncate grant\n"
        "# follows symlinks in BOTH intermediate and final components — a root\n"
        "# arbitrary-file-zero primitive — so it is omitted entirely (PR2 audit,\n"
        "# Option B).\n"
        "#\n"
        "# These grants are the policy FLOOR, not the safety boundary. `rm -rf`\n"
        "# re-resolves its operand at root's exec time and follows a symlink in\n"
        "# any INTERMEDIATE component (e.g. a `.npm` swapped to a symlink mid-\n"
        "# window) — so an ABSOLUTE path grant could not be made race-safe. The\n"
        "# operand is therefore a BARE LEAF NAME (no path): disk_reclaim_apply\n"
        "# walks bot-home → .npm with openat/O_NOFOLLOW, rejecting any symlink\n"
        "# component, then runs rm with the child's cwd pinned (via fchdir on the\n"
        "# verified .npm dirfd) so `rm -rf -- _cacache` resolves the leaf inside\n"
        "# that exact verified inode — never a re-resolvable absolute string. A\n"
        "# rename of `.npm` to a symlink after the walk cannot redirect the held\n"
        "# fd. The grant pins binary + flags + the exact leaf name as a second\n"
        "# floor; because the operand carries no directory, these grants cannot\n"
        "# name a path at all.\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf -- _cacache\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf -- _npx\n"
        "\n"
        "# ── 3a. Read legacy oc-gws credentials (.config/gws/) ──────────────────────\n"
        "# Bots Google-connected via the pre-wizard `oc gws --reauth` CLI store\n"
        "# tokens here; admin keys API probes these to surface a legacy 'oc_only'\n"
        "# row so the operator can migrate via the dashboard wizard.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.config/gws/client_secret.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.config/gws/credentials.enc\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.config/gws/token_cache.json\n"
        "\n"
        + (
            "# ── 3b. Read Dropbox desktop client metadata (~/.dropbox/info.json) ────────\n"
            "# Dropbox in this pod is the macOS desktop sync app, not an OAuth/API\n"
            "# integration. The Dropbox app on the bot's user account writes a tiny\n"
            "# JSON file with the sync-folder path and subscription type; the admin\n"
            "# keys API reads it to surface the row as connected.\n"
            f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.dropbox/info.json\n"
            "\n"
            if macos else ""  # Dropbox desktop sync app is macOS-only on pods
        )
        + "# ── 3c. Phase 2 integration discovery: workspace credentials ───────────────\n"
        "# Plugin-managed credentials (storage shape S3): team_bot_c's ranch plugin and\n"
        "# similar custom integrations bundle OAuth client secrets, OAuth token\n"
        "# caches, and service-account JSONs under the bot's workspace. The\n"
        "# admin keys API enumerates the directory and classifies each file by\n"
        "# content shape so plugin-managed Workspace integrations show as\n"
        "# connected on the dashboard. Without these grants the v2 probes\n"
        "# silently return NO_EVIDENCE in production.\n"
        f"evolve ALL=(root) NOPASSWD: {ls} {home}/*/.openclaw/workspace/credentials\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/credentials/*.json\n"
        "\n"
        "# ── 3d. Phase 2 integration discovery: workspace dotenv ────────────────────\n"
        "# Storage shape S5: team_bot_a-style pattern where Slack/Telegram tokens live\n"
        "# only in a workspace .env file (no auth-profiles entry). The probe\n"
        "# greps for provider-specific env-var names with non-empty values; the\n"
        "# values themselves never leave the helper (privacy — .env can hold\n"
        "# unrelated secrets like database passwords).\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/.env\n"
        "# Dotenv rotation (storage=\"dotenv\" on /api/admin/keys/<bot>/<provider>/rotate):\n"
        "# rewrites the matching <NAME>=<value> line via /tmp staging; the rewriter\n"
        "# preserves every other line (including unrelated secrets).\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.env {home}/*/.openclaw/workspace/.env\n"
        "\n"
        "# ── 3e. Phase 2 integration discovery: workspace manifests ─────────────────\n"
        "# Storage shape S4: bot-authored declarative descriptions of integrations\n"
        "# the bot uses at runtime (google_integration.json, gmail_fetcher.json).\n"
        "# Manifests don't carry credentials but evidence the bot's runtime\n"
        "# expects the integration to work — surfaced as a chip on rows that\n"
        "# already MATCH some other probe.\n"
        f"evolve ALL=(root) NOPASSWD: {ls} {home}/*/.openclaw/workspace/manifests\n"
        "\n"
        "# ── 3f. Phase 2 integration discovery: system-level GitHub auth ────────────\n"
        "# Storage shape S8: gh CLI hosts.yml (OAuth tokens stored in keychain,\n"
        "# referenced from this file) and user SSH private keys. Both are\n"
        "# evidence-only — the dashboard surfaces them as chips alongside the\n"
        "# integration_token row, never as the primary status driver.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.config/gh/hosts.yml\n"
        f"evolve ALL=(root) NOPASSWD: {ls} {home}/*/.ssh\n"
        "\n"
        + (
            "# ── 3g. Quarantine DB copy (home_artifacts_monitor) ────────────────────────\n"
            "# detect_quarantine_downloads snapshots each bot's LaunchServices DB\n"
            "# (mode 0644 under mode-0700 ~/Library/Preferences/, so unreadable by\n"
            "# the evolve user directly) to a mkstemp'd /tmp path, reads the copy,\n"
            "# then unlinks it. sudo arg wildcards can cross '/'; what keeps this\n"
            "# narrow is the literal anchors — the source must end in the exact\n"
            "# LaunchServices filename (no bare-* source, roadmap 2.10) and the\n"
            "# destination in /tmp/evolve-quarantine-…sqlite, so it can't stage\n"
            "# arbitrary content the way a wildcard-source cp could. Without this\n"
            "# grant the hourly check fails sudo -n and fires a\n"
            "# quarantine_check_failed Signal per bot.\n"
            f"evolve ALL=(root) NOPASSWD: {cp} {home}/*/Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2 /tmp/evolve-quarantine-*.sqlite\n"
            "\n"
            if macos else ""  # LaunchServices quarantine DB is macOS-only
        )
        + "# ── 3h. Read content-scan workspace identity/setup docs ────────────────────\n"
        "# The content scanner (packages/analyzer/content_scan) reads these per-bot\n"
        "# identity/setup docs from ~/.openclaw/workspace and fires\n"
        "# content_scan_file_disappeared (alert severity) when one is missing or\n"
        "# unreadable. The direct read via the evolve read ACL is primary; on Linux a\n"
        "# transient ACL-mask clamp makes that read raise PermissionError, so\n"
        "# scanner._read_text falls back to `sudo cat <abs>`. Without these grants the\n"
        "# fallback hits 'sudo: a password is required' (recorded as read_error\n"
        "# sudo_rc=1) and the alert can NEVER clear during the clamp window — the\n"
        "# 2026-06-29 evo-vps flurry (70 archived content_scan_file_disappeared, all\n"
        "# sudo_rc=1) while the docs existed the whole time. Non-secret docs,\n"
        "# enumerated per file (NOT a workspace/* wildcard) so cred/token files under\n"
        "# workspace/ (credentials/*, .env) stay unreadable. The set is kept in\n"
        "# lockstep with the catalog's scanned_files_per_bot by\n"
        "# test_sudoers_workspace_doc_cat_grants.py. (AGENTS.md also has the §12 heal\n"
        "# read-fallback grant; the duplicate is harmless and keeps this block a\n"
        "# complete, self-contained mirror of the scanned set.)\n"
        + "".join(
            f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/{doc}\n"
            for doc in CONTENT_SCAN_WORKSPACE_DOCS
        )
        + "\n"
        + "# ── 3i. Producer sudo-cat fallbacks: tiers / cron jobs / OC app log ─────────\n"
        "# Three more direct-read-first producers whose `sudo /bin/cat` fallback had\n"
        "# NO grant on either platform (2026-07-29 VPS denial audit — same class as\n"
        "# the §3h content-scan gap): audit_tier_drift (evolve-tiers.json),\n"
        "# audit_cron_health (cron/jobs.json), and the cost-watchdog /\n"
        "# embedding-monitor OC log-tail readers (logs/openclaw.log). On macOS the\n"
        "# ACL read always works so the fallback never fired; on Linux the OC\n"
        "# gateway mints these 0600 (the create-mode group bits become the POSIX-ACL\n"
        "# mask, capping evolve's inherited read ACE), so the fallback is load-\n"
        "# bearing until the hourly mask reassert catches up. Non-secret files:\n"
        "# tiers is model-routing config (0644 by contract), jobs.json is cron\n"
        "# schedule metadata, openclaw.log is the gateway's own app log.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/evolve-tiers.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/cron/jobs.json\n"
        "# jobs-quarantine.json: OpenClaw ≥2026.7 silently parks invalid cron jobs\n"
        "# here during the SQLite import — audit_cron_health reads it to surface\n"
        "# quarantined (i.e. never-running) jobs. Gateway-minted, so same 0600-on-\n"
        "# Linux story as jobs.json above.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/cron/jobs-quarantine.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/logs/openclaw.log\n"
        "\n"
        + "# ── 4. Write bot openclaw.json files (via /tmp staging only) ─────────────────\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.json {home}/*/.openclaw/openclaw.json\n"
        "# openclaw.json carries the gateway token + every messaging-channel bot\n"
        "# token, so it must be 0600. A bare `cp` (no -p) creates the dest at root's\n"
        "# umask → 0644 (world-readable); the config-write paths (write_oc_config,\n"
        "# safe_write_bot_config, the deploy repairs) chmod it back to 0600. Without\n"
        "# this grant that chmod is silently denied and every freshly channel-\n"
        "# connected bot's token sits world-readable until a later deploy. chmod\n"
        "# preserves the evolve read ACL, so the admin read path is unaffected.\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/openclaw.json\n"
        "\n"
        + (
            "# ── 4a. Plant Spotlight-exclusion marker (.metadata_never_index) ─────────────\n"
            "# deploy._plant_never_index_marker drops an empty .metadata_never_index in\n"
            "# each bot's .openclaw/ so macOS Spotlight (mds/mdworker) stops INDEXING the\n"
            "# churny state trees (sessions.json, the per-agent sqlite, logs) — indexing\n"
            "# them is pure waste and every recursive deploy perm-pass triggers an mds\n"
            "# reindex storm (the 2026-06-24 starved-mini incident: mds_stores at 582 MB).\n"
            "# The .openclaw/ root is bot-owned and only r-x to evolve (the #3198 read\n"
            "# clamp), so the marker is planted via sudo touch. Empty + non-secret +\n"
            "# additive: it does NOT disturb the read ACL or the group/other clamp.\n"
            "# macOS-only — Spotlight doesn't exist on Linux, so the code never touches\n"
            "# there and the grant is rendered only on the macOS profile.\n"
            f"evolve ALL=(root) NOPASSWD: /usr/bin/touch {home}/*/.openclaw/.metadata_never_index\n"
            "\n"
            if macos else ""  # Spotlight (.metadata_never_index) is macOS-only
        )
        + "# ── 4b. Write bot evolve-tiers.json (via /tmp staging only) ──────────────────\n"
        "# The AI-Optimization page's reconcile / tiers-save / cascade / user-tier-\n"
        "# override writes all land in the bot-owned ~/.openclaw/evolve-tiers.json.\n"
        "# oc_model._save_tiers_file / migrate_model_roles._write_bot_owned_json stage\n"
        "# in /tmp and copy with sudo when the admin server (evolve user) can't write\n"
        "# the bot's home directly — the only sanctioned path (evolve has no\n"
        "# `sudo -u <bot>` grant). Without the cp line, 'Reconcile catalog' dies with\n"
        "# [Errno 13] Permission denied on the bot's evolve-tiers.json.\n"
        "#\n"
        "# The chown + chmod-644 grants are load-bearing. A bare `cp` (no -p) to a\n"
        "# *fresh* dest creates it root:wheel 0600 (cp runs as root). The BOT user —\n"
        "# which runs oc_model.py to read/rewrite its OWN tier config — then can't\n"
        "# read its own file, so every tier read/write 500s with [Errno 13] on\n"
        "# evolve-tiers.json until repaired (the 2026-06-16 fleet-wide repo-puller\n"
        "# heal failure: all 10 bots looping the EACCES every deploy). The writers and\n"
        "# the ensure_pod_perms self-heal (secret_config_perms.check_bot_tiers_owner-\n"
        "# ship) chown it back to the bot and chmod 644 — it is model-routing config,\n"
        "# NOT a secret, so 0644 (world-readable) is correct, unlike openclaw.json/\n"
        "# auth-profiles.json which stay 0600. Distinct dest from the openclaw.json\n"
        "# grant above.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-tiers-*.json {home}/*/.openclaw/evolve-tiers.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/evolve-tiers.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/evolve-tiers.json\n"
        "\n"
        "# ── 5. Write bot auth-profiles.json (via /tmp staging only) ──────────────────\n"
        "# Five sudo steps; the chown of the parent dir is load-bearing.\n"
        "# Without it, OC's token-refresh atomic write fails EACCES because the\n"
        "# bot user can't create tmp files in a root-owned parent dir. Symptom:\n"
        "# every Telegram message dies with 'Embedded agent failed: EACCES\n"
        "# auth-profiles.json.<uuid>.tmp' and the operator sees 'Something went\n"
        "# wrong while processing your request' in their DM. mode 600 (not 644)\n"
        "# because auth-profiles.json carries API keys; the file is bot-owned\n"
        "# after the chown so the bot still has read access.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/agents/main/agent\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/agents/main/agent\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.json {home}/*/.openclaw/agents/main/agent/auth-profiles.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/agents/main/agent/auth-profiles.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/agents/main/agent/auth-profiles.json\n"
        "\n"
        "# ── 5a. Per-bot pairing-credentials writes (Users page) ──────────────────────\n"
        "# Spec: docs/spec-per-bot-users-management-2026-05-29.md. The admin UI's\n"
        "# Users page reads OC's per-bot pairing state from\n"
        f"# {home}/<bot>/.openclaw/credentials/<provider>-{{pairing,default-allowFrom}}.json\n"
        "# and writes them on approve/revoke/reject (routes_bot_users.py). Reads\n"
        "# use the ACL granted by set_evolve_read_acl; writes need /tmp staging\n"
        "# + sudo /bin/cp because the files are bot-owned (mode 600 to match OC).\n"
        "# Fallback /bin/cat read covers pre-ACL bot deploys.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/credentials/*-pairing.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/credentials/*-default-allowFrom.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-pairing-*.json {home}/*/.openclaw/credentials/*-pairing.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-pairing-*.json {home}/*/.openclaw/credentials/*-default-allowFrom.json\n"
        # 2026-06-08 — chown grants for the credentials files. Without
        # these the _write_bot_json sudo-fallback path failed on
        # "Approve" with "sudo: a password is required" after the cp
        # succeeded (because cp leaves the file root-owned, then chown
        # back to the bot user is required before chmod 600 so the OC
        # gateway can still read its own credentials). Symptom: any
        # Approve from the Seen Recently / Pending sections on a bot
        # whose credentials dir didn't already have direct evolve-user
        # write access via ACL.
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/credentials/*-pairing.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/credentials/*-default-allowFrom.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/credentials/*-pairing.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/credentials/*-default-allowFrom.json\n"
        "\n"
        "# ── 5b. Google Workspace MCP credentials writes (skill install) ─────────────\n"
        "# Spec: docs/spec-google-workspace-suite-2026-06-04.md §8.\n"
        "# The taylorwilsdon/google_workspace_mcp server reads OAuth credentials\n"
        "# from <email>.json files under WORKSPACE_MCP_CREDENTIALS_DIR. The\n"
        "# Evolve token shim (skills/google_workspace_token_shim.py) translates\n"
        "# auth-profiles.json into that shape at install time and on each deploy.\n"
        "# Files are bot-owned (mode 600) so /tmp staging + sudo /bin/cp is needed;\n"
        "# the dir tree lives under .openclaw/ so a single grant covers the whole\n"
        "# tree without intersecting other skills' sudoers rules.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/google_workspace_mcp/credentials\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/google_workspace_mcp\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {home}/*/.openclaw/google_workspace_mcp\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-gws-*.json {home}/*/.openclaw/google_workspace_mcp/credentials/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/google_workspace_mcp/credentials/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/google_workspace_mcp/credentials/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/google_workspace_mcp/credentials/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {rm} {home}/*/.openclaw/google_workspace_mcp/credentials/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {home}/*/.openclaw/google_workspace_mcp\n"
        "\n"
        "# ── 5c. CLI device-scope invariant (oc_cli_device.py) ───────────────────────\n"
        "# The OC 2026.6 upgrade narrowed every bot's own CLI device to\n"
        "# operator.read in ~/.openclaw/devices/paired.json — `openclaw message\n"
        "# send` and defer fires died pod-wide with 'scope upgrade pending\n"
        "# approval', and the approval flow can't self-serve (each CLI attempt\n"
        "# supersedes the previous request). ensure_pod_perms widens the scope\n"
        "# lists back (and pre-seeds a day-1 entry from/with the CLI identity);\n"
        "# the files are bot-owned mode 600, so the evolve-user daemon path needs\n"
        "# cat fallback reads + the /tmp staging write ritual. Staging prefix is\n"
        "# pinned to evolve-device-*.json; dirs get 700 to match what the OC CLI\n"
        "# itself creates.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/devices/paired.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/identity/device.json\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/devices\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/identity\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/devices\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/identity\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.openclaw/devices\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.openclaw/identity\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-device-*.json {home}/*/.openclaw/devices/paired.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-device-*.json {home}/*/.openclaw/identity/device.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/devices/paired.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/identity/device.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/devices/paired.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/identity/device.json\n"
        "\n"
        "# ── 6. Run openclaw CLI as bot users (cron, status, gateway queries) ────────\n"
        "# Needed for: openclaw cron list, openclaw status, openclaw models list,\n"
        "# deploy.py's plugins install / doctor --fix / config validate / gateway\n"
        "# restart subprocess calls, etc. The CLI must run as the bot user to\n"
        "# connect to the bot's gateway socket.\n"
        "#\n"
        "# SETENV tag allows the caller to pass OPENCLAW_CONFIG_PATH via\n"
        "# `sudo --preserve-env=OPENCLAW_CONFIG_PATH`. deploy.py uses this when\n"
        "# validating a STAGED temp config before touching the live one\n"
        "# (_write_bot_config_safely). Without SETENV, sudo strips the env var\n"
        "# and validation targets the live config — which would defeat the\n"
        "# pre-write safety check.\n"
        + (f"evolve ALL=(ALL) NOPASSWD: SETENV: {oc_path}\n" if oc_path else
           "# openclaw not found at setup time — add manually after installing openclaw\n"
           + ("# evolve ALL=(ALL) NOPASSWD: SETENV: /opt/homebrew/lib/node_modules/openclaw/bin/openclaw\n"
              if macos else
              "# evolve ALL=(ALL) NOPASSWD: SETENV: /usr/lib/node_modules/openclaw/bin/openclaw\n"))
        + "\n"
        "# ── 7. Run oc_model.py and oc_keys.py as bot users ────────────────────────\n"
        "# Needed for: model catalog reads/writes, API key presence checks.\n"
        f"evolve ALL=(ALL) NOPASSWD: {python3} {analyzer_dir}/oc_model.py *\n"
        f"evolve ALL=(ALL) NOPASSWD: {python3} {analyzer_dir}/oc_keys.py *\n"
        "\n"
        "# ── 7b. Refresh the heal drift baseline as the bot user ────────────────────\n"
        "# Admin UI 'Accept as baseline' (api_security_accept_drift) shells out to\n"
        "# backup.py --commit-baseline-local under sudo -H -u <bot_user>. The bot's\n"
        "# workspace .git/ dir is bot-owned with only a read ACL for evolve — the\n"
        "# previous in-process call from the admin daemon failed with EACCES on\n"
        "# .git/index.lock. Same CLI as deploy.py's _baseline_refresh_as_bot_user\n"
        "# (which runs as root and doesn't need this grant).\n"
        f"evolve ALL=(ALL) NOPASSWD: {python3} {analyzer_dir}/backup.py --commit-baseline-local *\n"
        "\n"
        "# ── 7a. Run application_scanner.py as bot users ────────────────────────────\n"
        "# Needed for: workspace scan triggered by admin UI (Files & Resources tab).\n"
        "# The scanner must run as the bot user to read its workspace and write manifests.\n"
        "# The plugin API path (delegating back to this server) creates a deadlock;\n"
        "# admin server always uses this subprocess grant directly.\n"
        "# Interpreter is the VENV python, not {python3}: the scanner imports the\n"
        "# packaged evolve_admin, absent from macOS system python 3.9. web/server.py\n"
        "# renders the same path via config.scanner_python() — the argv must match\n"
        "# this grant exactly or sudo demands a password and every scan dies.\n"
        f"evolve ALL=(ALL) NOPASSWD: SETENV: {venv_python} {analyzer_dir}/application_scanner.py *\n"
        "\n"
        "# ── 7c. Apply an OpenClaw upgrade from the admin UI ─────────────────────────\n"
        "# Spec: docs/spec-oc-upgrade-from-ui-2026-07-28.md §4. The banner's 'Run\n"
        "# upgrade now' button posts to /api/oc/upgrade/apply, which shells out to\n"
        "# this helper as root because the global node_modules dir is owned by the\n"
        "# host's admin user and `evolve` (staff/wheel, not admin) genuinely cannot\n"
        "# write it.\n"
        "#\n"
        "# The obvious grant — `<npm> install -g --prefix=<prefix> openclaw@*` — is\n"
        "# NOT what this is, deliberately. sudo's `*` is fnmatch-style: it doesn't\n"
        "# cross a '/', which rules out `openclaw@file:///…`, but it DOES match npm's\n"
        "# alias syntax. `openclaw@npm:some-other-package` satisfies that grant and\n"
        "# runs an arbitrary package's postinstall AS ROOT — the version field is a\n"
        "# package-spec field, so the grant's apparent narrowness is an illusion.\n"
        "# Same failure class as the 2026-04-24 outage the preflight was built to\n"
        "# prevent, elevated to root and reachable from an HTTP endpoint.\n"
        "#\n"
        "# Instead: one interpreter running one script (the §11h marker_embed_helper\n"
        "# shape). The helper takes ONLY a report id, re-validates its shape before\n"
        "# touching the filesystem, re-runs the gates, and resolves the version to\n"
        "# install FROM the persisted report — so the web layer never names a package\n"
        "# spec and there is nothing to inject. The npm temp-dir cleanup and the\n"
        "# macOS user-LaunchAgent sweep run inside the helper (already root, already\n"
        "# knows the npm prefix) rather than as two more `rm` grants.\n"
        "# Interpreter is the VENV python (it imports evolve_admin), matching §7a;\n"
        "# oc_upgrade_apply.helper_script_path() renders the identical path from the\n"
        "# same platform_profile deploy checkout, so the argv cannot drift.\n"
        f"evolve ALL=(root) NOPASSWD: {venv_python} {repo}/packages/admin/evolve_admin/oc_upgrade_apply.py --report-id *\n"
        "\n"
        + (
            "# ── 8. Check and restart bot gateways ────────────────────────────────────────\n"
            f"evolve ALL=(root) NOPASSWD: {svc} list *\n"
            f"evolve ALL=(root) NOPASSWD: {svc} kickstart -k system/ai.openclaw.*\n"
            # After a bot gateway is bootouted (manual stop, crash-loop reset, or
            # a launchd transient unload), the next kickstart fails with "Could
            # not find service" until the plist is re-bootstrapped. The
            # /api/admin/gateway/<bot>/restart endpoint detects this and
            # bootstraps from the discovered plist before retrying. Without these
            # grants the recovery path falls through to a sudo password prompt.
            f"evolve ALL=(root) NOPASSWD: {svc} bootstrap system {daemons}/ai.openclaw.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootout system/ai.openclaw.*\n"
            # User-domain LaunchAgents (legacy installs where the gateway runs
            # in the bot user's GUI session). oc_gateway_restart falls back to
            # gui/<uid>/<svc> after system kickstart fails.
            f"evolve ALL=(root) NOPASSWD: {svc} kickstart -k gui/*/ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootstrap gui/* /Library/LaunchAgents/ai.openclaw.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootstrap gui/* {home}/*/Library/LaunchAgents/ai.openclaw.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootout gui/*/ai.openclaw.*\n"
            # Repo-puller restarts dependent ai.evolve.* daemons after a pull
            # whose diff touched code those daemons load (admin-ui,
            # heal/audit/verify). Without this, fixes shipped via PR sit dormant
            # in the running daemons until the next deploy (PR #867 was the
            # canonical case: admin-ui ran pre-pull code for 14min after the
            # fix landed). The ai.evolve.* wildcard also covers the puller's
            # own label, which it explicitly declines to restart (racy) — the
            # grant is broader than the policy, with the policy enforced in code.
            f"evolve ALL=(root) NOPASSWD: {svc} kickstart -k system/ai.evolve.*\n"
            "\n"
            if macos else
            # Linux analogue of macOS sections 8 + 9 + 11: SystemdScheduler
            # (packages/analyzer/runtime/scheduler.py) owns every unit-file
            # write (/tmp staging ritual) and every systemctl verb below —
            # the grants mirror that adapter's argv exactly, one source of
            # truth (design-linux-port §5). systemd has no gui/ domain and
            # no bootstrap/bootout split, so this set is shorter than the
            # launchd one. ai.openclaw.evolve.* labels match ai.openclaw.*.
            "# ── 8. Check and restart bot gateways + evolve's own jobs (systemd) ──────────\n"
            "# Grants mirror runtime/scheduler.py::SystemdScheduler argv exactly.\n"
            f"evolve ALL=(root) NOPASSWD: {svc} daemon-reload\n"
            f"evolve ALL=(root) NOPASSWD: {svc} enable ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} restart ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} disable --now ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} show ai.openclaw.* -p *\n"
            f"evolve ALL=(root) NOPASSWD: {svc} enable ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} restart ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} disable --now ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} show ai.evolve.* -p *\n"
            # remove() ends with reset-failed. Deleting a unit file does NOT
            # retract systemd's recorded failure — without this grant the call
            # fails under `sudo -n`, remove() ignores the rc (it is bookkeeping,
            # not the operation), and a retired unit that was failing keeps
            # showing up in `list-units --state=failed` as `not-found / failed`
            # forever. Like `disable --now`, it is passed the whole unit set at
            # once; sudoers arg matching is a join-and-fnmatch, so one trailing
            # `*` covers `<label>.service <label>.timer`.
            f"evolve ALL=(root) NOPASSWD: {svc} reset-failed ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} reset-failed ai.evolve.*\n"
            # running() probes liveness; kill() signals a unit's processes
            # (the orphan-sweep / crash-loop reset path). Both label
            # families, same as the verbs above.
            f"evolve ALL=(root) NOPASSWD: {svc} is-active ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} is-active ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} kill -s * ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} kill -s * ai.evolve.*\n"
            # list() calls list-units/list-timers with and without a label
            # pattern — sudoers arg matching is a join-and-fnmatch, so the
            # bare form needs its own line (a trailing `*` does not match
            # the empty argument list).
            f"evolve ALL=(root) NOPASSWD: {svc} list-units --all --plain --no-legend\n"
            f"evolve ALL=(root) NOPASSWD: {svc} list-units --all --plain --no-legend *\n"
            f"evolve ALL=(root) NOPASSWD: {svc} list-timers --all --plain --no-legend\n"
            f"evolve ALL=(root) NOPASSWD: {svc} list-timers --all --plain --no-legend *\n"
            "# Unit-file writes — the SystemdScheduler staging ritual\n"
            "# (mkstemp /tmp/*.unit + cp + chown root:root + chmod 644; rm on removal).\n"
            f"evolve ALL=(root) NOPASSWD: {cp} /tmp/*.unit {daemons}/ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {cp} /tmp/*.unit {daemons}/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {daemons}/ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {daemons}/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 644 {daemons}/ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 644 {daemons}/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {rm} {daemons}/ai.openclaw.*\n"
            f"evolve ALL=(root) NOPASSWD: {rm} {daemons}/ai.evolve.*\n"
            "\n"
        )
        + "# ── 8b. Bot provisioning pipeline (wizard /api/wizard/provision endpoint\n"
        "# + the conversational `evo add-bot` chain) ─────────────────────────────────\n"
        "# The provision pipeline (packages/admin/evolve_admin/provisioning.py)\n"
        "# executes inside the admin-ui daemon as the `evolve` user — both for\n"
        "# the wizard endpoint and for the conversational `evo add-bot` chain.\n"
        "# The CLI path (`sudo evolve-admin provision-bot ...`) runs under the\n"
        "# operator's unrestricted sudo, so these commands work silently there —\n"
        "# but the daemon path hits the `evolve` sudoers, which without these\n"
        "# grants dies at stage 2 (create_macos_user) with `sudo: a terminal is\n"
        "# required to read the password`. Surfaced 2026-05-31 mid-onboarding\n"
        "# for the test pod's 9th bot; grants shipped in #1892, were dropped by\n"
        "# an unrelated renderer edit in #1956 (the pin tests in\n"
        "# test_provision_pipeline_sudoers.py were quarantined instead of\n"
        "# treated as the regression they flagged), restored for add-bot M2.\n"
        "#\n"
        + (
            "# Stage 2 (create_macos_user) — dscl + createhomedir + rollback.\n"
            f"evolve ALL=(root) NOPASSWD: {c['dscl']} . -create {home}/*\n"
            f"evolve ALL=(root) NOPASSWD: {c['createhomedir']} -c -u *\n"
            f"evolve ALL=(root) NOPASSWD: {c['dscl']} . -delete {home}/*\n"
            # /bin/rm -rf /Users/* is broad; the rollback's delete_user already
            # guards with str(home).startswith('/Users/') + an existence check,
            # so the grant is the policy floor, not the safety boundary.
            f"evolve ALL=(root) NOPASSWD: {rm} -rf {home}/*\n"
            if macos else
            # Linux stage 2: LinuxUserIsolation's verb map (runtime/
            # isolation.py) — groupadd/useradd/userdel replace dscl/
            # createhomedir. userdel -r owns home removal, so there is NO
            # `rm -rf {home}/*` grant on Linux (strictly narrower).
            "# Stage 2 (create_user) — groupadd + useradd + rollback\n"
            "# (LinuxUserIsolation in runtime/isolation.py; grants match its\n"
            "# argv exactly — -m/-M home toggle, pinned shell and inventory\n"
            "# group; userdel -r owns home removal, no separate rm -rf).\n"
            f"evolve ALL=(root) NOPASSWD: {c['groupadd']} -f evolve-bots\n"
            f"evolve ALL=(root) NOPASSWD: {c['useradd']} -m -u * -s /bin/bash -c * -G evolve-bots *\n"
            f"evolve ALL=(root) NOPASSWD: {c['useradd']} -M -u * -s /bin/bash -c * -G evolve-bots *\n"
            f"evolve ALL=(root) NOPASSWD: {c['userdel']} -r *\n"
            f"evolve ALL=(root) NOPASSWD: {c['userdel']} *\n"
            # W10-F #11: create_user chowns the account's HOME dir to itself
            # (LinuxUserIsolation.create_user). `useradd -m` only owns a home it
            # CREATES; a pre-existing root-owned /home/<user> (made by an earlier
            # `mkdir -p /home/<user>/.openclaw`) stays root:root and the account
            # can't write its own $HOME — npm cache, dotfiles, the brave gap-fill
            # install all EACCES. Non-recursive (the home dir itself only, never
            # the .openclaw subtree). No macOS analog — createhomedir owns the
            # home — so this grant is Linux-only (the macOS golden must not move).
            f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*\n"
        )
        + "#\n"
        "# Stage 3 (create_openclaw_dir) — parent .openclaw/ dir creation,\n"
        "# ownership, and permissions (plus the wrong-owner repair path's -R\n"
        "# form and the rollback rm). The two chown forms also serve the\n"
        "# wizard verify gauntlet's Check-1 Fix button (ownership repair of\n"
        "# a flagged path back to bot:staff — wizard_verify.repair_ownership;\n"
        "# spec docs/spec-wizard-verification-gauntlet-2026-05-30.md §5).\n"
        "# Pinned by test_wizard_verify_sudoers.py — formerly a standalone\n"
        "# §5c block; merged here when #2664 and the M2 restoration of this\n"
        "# section each re-added the same lines after #1956 dropped them.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {home}/*/.openclaw\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.openclaw\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {home}/*/.openclaw\n"
        "#\n"
        "# Stage 4 (openclaw onboard) is covered by the existing\n"
        "# `evolve ALL=(ALL) NOPASSWD: SETENV: <oc_path>` grant below; the\n"
        "# pipeline invokes `sudo -u <bot> -H <oc_path> ...` so sudo matches\n"
        "# that grant directly. No new grant needed here.\n"
        "#\n"
        + (
            "# Stage 6 (deploy_bot → install_bot_gateway_plist) — write the\n"
            "# per-bot OpenClaw gateway LaunchDaemon. The launchctl\n"
            "# bootstrap/bootout verbs are already granted in section 8; these\n"
            "# cover the log-dir creation and the Scheduler seam's plist write\n"
            "# ritual (/tmp staging + cp + chown root:wheel + chmod 644). The\n"
            "# /tmp/*.plist source pattern matches the seam's unprefixed\n"
            "# tempfile staging — same shape as the ai.evolve.* plist grants\n"
            "# in section 12. chown grants use bare `*` for the user:group arg:\n"
            "# macOS visudo rejects `:` inside the literal argument pattern\n"
            "# (PR #1906 shipped `root:wheel` and bricked refresh-sudoers).\n"
            f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/logs\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/logs\n"
            f"evolve ALL=(root) NOPASSWD: {cp} /tmp/*.plist {daemons}/ai.openclaw.*-gateway.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {daemons}/ai.openclaw.*-gateway.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 644 {daemons}/ai.openclaw.*-gateway.plist\n"
            "#\n"
            "# Stage 6 (deploy_bot → set_evolve_read_acl) — the ACL ritual that\n"
            "# makes every later evolve-side read of the bot's .openclaw/ work\n"
            "# without per-path sudo (CLAUDE.md §File Access Pattern). All of\n"
            "# these run check=False in deploy.py, so without the grants they\n"
            "# fail SILENTLY and the bot comes up unreadable to the admin\n"
            "# daemon — the add-bot M2 pod proof surfaced exactly that (smoke\n"
            "# check reported openclaw.json 'not found' on a healthy bot).\n"
            "# Same multi-word-ACE wildcard shape as the workspace/manifests\n"
            "# grants above.\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {home}/*/.openclaw\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {home}/*/.openclaw\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -N {home}/*/.openclaw/credentials\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.openclaw/credentials\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -N {home}/*/.openclaw/profiles/*.md\n"
            # Bot-private app token files (#3452): file-shaped carve-out —
            # strip any ACE the recursive grant swept up + restore 0600 so
            # strict app-side mode gates keep passing.
            + "".join(
                f"evolve ALL=(root) NOPASSWD: {chmod} -N {home}/*/.openclaw/{rel}\n"
                f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/{rel}\n"
                for rel in BOT_PRIVATE_SECRET_RELPATHS
            )
            + f"evolve ALL=(root) NOPASSWD: {chmod} +a * {home}/*/.openclaw/workspace/evolve\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {home}/*/.openclaw/workspace/evolve\n"
            if macos else
            # Linux: gateway unit writes ride the generic /tmp/*.unit grants
            # in section 8; the macOS `chmod +a` ACL ritual has no Linux
            # equivalent — POSIX-ACL (setfacl) grants land with the Linux
            # perms adapter (port wave W4a), not in this writer yet.
            "# Stage 6 (deploy_bot) — gateway unit writes are covered by the\n"
            "# generic /tmp/*.unit grants in section 8. Log-dir creation:\n"
            f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/logs\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/logs\n"
            "# W10-E: the SystemdScheduler seam mkdir+chowns every unit's\n"
            "# StandardOutput/StandardError parent before starting it (systemd,\n"
            "# unlike launchd, won't auto-create it). The daemon fleet logs under\n"
            "# three roots; every one the evolve-invoked install path may create:\n"
            "#   - {home}/*/.openclaw/logs  (gateways + evolve infra; covered above)\n"
            "#   - {shared}/logs            (admin-ui stderr, imessage, better_engine, puller)\n"
            "#   - {home}/*/.evolve/logs    (the mcp-bridge daemon)\n"
            f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/logs\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/logs\n"
            f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.evolve/logs\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.evolve/logs\n"
            "#\n"
            "# Stage 6 (set_evolve_read_acl) — the POSIX-ACL ritual via the\n"
            "# LinuxPerms adapter (runtime/perms.py); grants match its argv\n"
            "# shapes exactly: recursive grants are the `-R -m` (access) +\n"
            "# `-R -d -m` (default-ACL inheritance) pair, single-shot grant()\n"
            "# is `-m` (+ `-d -m` on dirs whose verb string carries inherit\n"
            "# flags), clear_acl is `-b` (+ `-k` on dirs). The bare `*` after\n"
            "# -m stands in for the u:<user>:<bits> entry spec — visudo\n"
            "# rejects unescaped colons in argument patterns, the same reason\n"
            "# the macOS render wildcards its multi-word ACE strings. All of\n"
            "# these run check=False in deploy.py, so a missing grant fails\n"
            "# SILENTLY in the admin-daemon context (the add-bot M2 lesson).\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {home}/*/.openclaw\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m * {home}/*/.openclaw\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {home}/*/.openclaw\n"
            # W10-F: traverse-only (--x) on the bot HOME dir itself. Ubuntu's
            # `useradd -m` honours HOME_MODE=0750, so /home/<bot> is
            # drwxr-x--- <bot>:<bot> and evolve can't reach .openclaw at all
            # (the rX ACL above is unreachable without x on every ancestor).
            # Matches LinuxPerms.grant_traverse's argv exactly; the literal
            # u:evolve:--x (escaped colons, like the m::rwX mask grant below)
            # keeps this strictly execute-only — no read/list of the home's
            # other dotfiles.
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:--x {home}/*\n"
            "# credentials/ + profile-.md carve-outs (threat model §3.1 —\n"
            "# evolve must NOT read bot API keys / private user profiles):\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -b {home}/*/.openclaw/credentials\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -k {home}/*/.openclaw/credentials\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.openclaw/credentials\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -b {home}/*/.openclaw/profiles/*.md\n"
            "# Bot-private app token files (#3452, file-shaped carve-out): the\n"
            "# recursive .openclaw grant sweeps these up, and a planted ACE\n"
            "# makes stat display the ACL mask (600 reads as 640) — strict\n"
            "# app-side mode gates (pm-inbox tokens) then refuse. Strip the\n"
            "# ACE + restore 0600 after every recursive grant/heal.\n"
            + "".join(
                f"evolve ALL=(root) NOPASSWD: {setfacl} -b {home}/*/.openclaw/{rel}\n"
                f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/{rel}\n"
                for rel in BOT_PRIVATE_SECRET_RELPATHS
            )
            + "# Secret-config read-ACE re-grant (W10-G #2): chmod_secret_config\n"
            "# does `chmod 600` on these token files, which on Linux zeroes the\n"
            "# ACL mask and caps evolve's inherited read ACE. It re-adds\n"
            "# `u:evolve:rX` (setfacl recomputes the mask) so the admin read path\n"
            "# survives. Single-file `-m` grants — the recursive .openclaw grant\n"
            "# above does not match a file argv.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw/openclaw.json\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw/openclaw.json.bak\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw/agents/main/agent/auth-profiles.json\n"
            "# workspace/evolve write contract (manifests, scan status):\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw/workspace/evolve\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {home}/*/.openclaw/workspace/evolve\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m * {home}/*/.openclaw/workspace/evolve\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {home}/*/.openclaw/workspace/evolve\n"
            "# set_evolve_read_acl's REMAINING Linux argv shapes (2026-07-29 VPS\n"
            "# denial audit — every one of these was firing on each deploy/heal and\n"
            "# dying 'command not allowed', silently, because the grants below were\n"
            "# never added when their callers were; check=False hid it):\n"
            "#   • workspace/ ROOT write grant + its -d default-ACL pair (grant()\n"
            "#     with file_inherit on a dir emits both) — evolve writes AGENTS.md\n"
            "#     and the other identity docs directly in workspace/.\n"
            "#   • per-file backfill on pre-existing files directly in workspace/\n"
            "#     (AGENTS.md, USER.md, openclaw-workspace-state.json, …). The\n"
            "#     path wildcard spans '/', so the ENTRY SPEC is pinned literally\n"
            "#     (u:evolve:rwX — matches LinuxPerms._linux_entry_bits for the\n"
            "#     write verb sets) instead of the bare `*` the trailing-anchored\n"
            "#     grants use: evolve can grant only ITSELF rwX through this line.\n"
            "#   • workspace/evolve-backup — grant_write_recursive pair (same\n"
            "#     share_group_other_read shape as workspace/evolve above).\n"
            "#   • ~/.claude/projects — the Auto-Memory inventory read grant\n"
            "#     (grant_read_recursive pair).\n"
            "#   • ~/.zshrc — the tamper-detection single-file read grant.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.openclaw/workspace\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m * {home}/*/.openclaw/workspace\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rwX {home}/*/.openclaw/workspace/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {home}/*/.openclaw/workspace/evolve-backup\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {home}/*/.openclaw/workspace/evolve-backup\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {home}/*/.claude/projects\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {home}/*/.claude/projects\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m * {home}/*/.zshrc\n"
            "# ACL-mask repair under .openclaw (2026-07-29 evolve-vps incident):\n"
            "# the OC gateway re-hardens agents/main/agent to 0700 on auth\n"
            "# writes, clamping that DIR's mask so evolve's inherited rX caps to\n"
            "# --- and auth-profiles.json is unreachable while the file's own\n"
            "# ACL looks healthy. reassert_mask / heal_evolve_access re-widen the\n"
            "# secret relpaths' parent dirs (and the Tier-1 recursive workspace\n"
            "# pass re-widens arbitrary masked children) with exactly\n"
            "# `setfacl -m m::rwX <path>`. Sudoers matches args as ONE fnmatch\n"
            "# string, so the trailing-anchored `-m * {home}/*/.openclaw` grant\n"
            "# above never matches deeper paths. The literal m::rwX entry spec\n"
            "# keeps this grant mask-widen-only (it can mint no new ACE); the\n"
            "# path wildcard spans `/`, covering every dir under .openclaw —\n"
            "# same shape as the shared-store m::rwX grants in 9b2.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m m\\:\\:rwX {home}/*/.openclaw/*\n"
        )
        + "\n"
        "# ── 8c. retire-bot archive sudo-cp fallback ─────────────────────────────────\n"
        "# When the API path (admin-ui as evolve) runs `retire_bot`, the archive\n"
        f"# step shutil.copytree's the bot's {home}/<bot>/.openclaw/ tree into the\n"
        "# shared archive dir. Plugin runtimes (e.g. browser-automation) sometimes\n"
        "# create subdirs under .openclaw/ with restrictive modes that don't\n"
        "# inherit the evolve-user read ACL — shutil hits EACCES on the subdir\n"
        "# and the archive fails. retire._sudo_cp_tree_fallback retries the copy\n"
        "# as root + chowns the result to evolve so the post-copy audit walks\n"
        "# the tree cleanly. These grants are what makes that fallback work in\n"
        "# the API path; the CLI deploy/retire path runs as root and bypasses\n"
        "# the fallback entirely. Surfaced 2026-06-01 when a wizard-Delete\n"
        "# failed in the API path but the same retire-bot CLI invocation\n"
        "# succeeded.\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {shared}/archived-bots/*\n"
        f"evolve ALL=(root) NOPASSWD: {cp} -R {home}/*/.openclaw {shared}/archived-bots/*/openclaw\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/archived-bots/*\n"
        "\n"
        + (
            "# ── 9. Manage evolve's own launchd jobs ──────────────────────────────────────\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootout system/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootstrap system {daemons}/ai.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootout system/ai.openclaw.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} bootstrap system {daemons}/ai.openclaw.evolve.*.plist\n"
            # Persistent pause/resume of scheduled app jobs (Scheduler.disable/
            # enable — app pause/archive ⇄ unpause/restore). A bare bootout is
            # not enough: /Library/LaunchDaemons plists auto-load at the next
            # boot, so an archived app would resume firing. `launchctl disable`
            # writes the override DB (reboot-surviving); `enable` clears it
            # before the §9 bootstrap grant above reloads the job. The bootout
            # (disable) and bootstrap (enable) verbs are already granted above.
            f"evolve ALL=(root) NOPASSWD: {svc} disable system/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} enable system/ai.evolve.*\n"
            # remove_orphaned_plists() deletes stale LaunchDaemon plists that are no
            # longer in expected_plist_labels(). Bootout grants above cover unloading;
            # these cover the subsequent rm.
            f"evolve ALL=(root) NOPASSWD: {rm} {daemons}/ai.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {rm} {daemons}/ai.openclaw.evolve.*.plist\n"
            "\n"
            if macos else
            # Linux: evolve's own jobs are systemd units — every verb and the
            # unit-file rm are already granted by the section-8 systemd set
            # (ai.evolve.* / ai.openclaw.* patterns cover both job families).
            "# ── 9. Manage evolve's own jobs — covered by the section-8 systemd set ──────\n"
            "\n"
        )
        # mkdir for {shared}/* is intentionally omitted:
        # evolve owns the shared dir after setup, so it can create
        # subdirectories there directly without sudo.
        # Sticky-bit and world-writable repair grants for shared dirs:
        # deploy_shared_dir re-asserts these after chmod -R a+rX; the Fix button
        # in the health UI also needs them.  Trailing /* is rejected by macOS
        # visudo; mid-path wildcards (/*/turns) are accepted.
        + "# ── 9a. Repair permissions on shared dirs after a+rX pass ──────────────────\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 1777 {shared}\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 1777 {shared}/*/turns\n"
        # deploy_shared_dir chmod 777's these dirs when evolve doesn't own them.
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/proposals/pending\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/proposals/approved\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/proposals/rejected\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/proposals/deployed\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/proposals/validation-results\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/feedback\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 777 {shared}/tests\n"
        "# 2026-07-29 VPS denial-audit wave 2 — shared-store heal shapes that had\n"
        "# NO grant on either platform; each fired per deploy/heal and died\n"
        "# 'command not allowed', silently (check=False).\n"
        "# (a) Proposal lifecycle subdir ownership normalization. Non-recursive\n"
        "# chown — the §9b `chown -R` grants don't match a plain chown argv.\n"
        "# The caller stat-gates on current ownership now, so this fires only\n"
        "# on genuine drift.\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/proposals/pending\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/proposals/snoozed\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/proposals/applied\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/proposals/archived\n"
        "# (b) Per-bot shared-store subdirs (deploy._create_bot_subdir): the\n"
        "# mkdir/chmod/chown trio falls to sudo when the bot's gateway won the\n"
        "# creation race and the dir landed bot-owned. One wildcard component\n"
        "# per shape — visudo globs don't cross '/', so each pattern pins\n"
        "# exactly the per-bot level it names. (chmod 1777 */turns is already\n"
        "# granted above.)\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/metrics/*\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/annotations/*\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/*/turns\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/*/spans\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/*/cascade\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/*/recommendations\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 1777 {shared}/metrics/*\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 1777 {shared}/*/recommendations\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 755 {shared}/annotations/*\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 755 {shared}/*/spans\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 755 {shared}/*/cascade\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/metrics/*\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/annotations/*\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/*/turns\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/*/spans\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/*/cascade\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/*/recommendations\n"
        "\n"
        + (
            "# ── 9b. Grant evo user write ACL on proposal / signal stores ───────────────\n"
            "# Phase E.2.b of docs/spec-evo-account-separation-2026-05-25.md split evo\n"
            "# off the privileged `evolve` account onto its own `evo` macOS user. Evo's\n"
            "# MCP tools still need to rename/delete files in {sharedDir}/proposals/ and\n"
            "# /signals/ (state transitions via arbiter.store / signals.store). The dirs\n"
            "# stay owned by evolve:wheel; an inherited ACL grants evo the write/delete\n"
            "# it needs. ensure_pod_perms applies + re-asserts these on every pass; the\n"
            "# sudoers grants here are the admin-daemon code path (no operator at the\n"
            "# keyboard). Wildcards stand in for the multi-word `user:evo allow …` ACL\n"
            "# string (visudo rejects bare colons) and for the `evolve:wheel` owner spec\n"
            "# (same colon-rejection reason — matches the §12 chown grants pattern).\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/signals\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {shared}/signals\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/signals\n"
            "# keystore/ + config_intents/ joined EVO_WRITE_SHARED_SUBDIRS after\n"
            "# these grants were first written (2026-06-06 config_intents PR) —\n"
            "# _ensure_evo_write_acl fired for them on every pass and died\n"
            "# silently (2026-07-29 VPS denial-audit wave 2).\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/config_intents\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {shared}/config_intents\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/config_intents\n"
            "# Per-bot shared-store subdir read ACEs (_create_bot_subdir's\n"
            "# acl_perms leg — pairs with the §9a mkdir/chmod/chown trio) and\n"
            "# the deploy-checkout packages/ read ACL (cost_ledger imports).\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/metrics/*\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/annotations/*\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/*/turns\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/*/spans\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {shared}/*/cascade\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {repo}/packages\n"
            "\n"
            "# ── 9c. Delivery-monitor heal: probe + one-shot restart of app jobs ─────────\n"
            "# Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §8 (U2.2 heal).\n"
            "# The delivery monitor heals a missed user-facing scheduled action (plist\n"
            "# labels ai.evolve.<bot>.<app>) with a one-shot kickstart — deliberately\n"
            "# WITHOUT -k: these are run-once calendar jobs, and -k would also kill a\n"
            "# possibly-in-flight run. (sudoers matches the full argv, so the §8\n"
            "# `kickstart -k system/ai.evolve.*` grant does not cover this form.)\n"
            "# `print` is the heal-path load probe and doubles as the sudo -n grant\n"
            "# probe — a denial is reported as \"couldn't attempt the restart\", never\n"
            "# silence. The plist-exists-not-loaded case re-bootstraps via the §9\n"
            "# grant above (bootstrap system /Library/LaunchDaemons/ai.evolve.*.plist)\n"
            "# after a plutil -lint gate (plutil needs no grant — LaunchDaemon plists\n"
            "# are world-readable).\n"
            f"evolve ALL=(root) NOPASSWD: {svc} print system/ai.evolve.*\n"
            f"evolve ALL=(root) NOPASSWD: {svc} kickstart system/ai.evolve.*\n"
            "\n"
            if macos else
            # Linux 9b: the evo write-access ritual through the LinuxPerms
            # adapter — grant_write_recursive is the `-R -m` + `-R -d -m`
            # pair (see the Stage 6 comment for the shape vocabulary). The
            # chown -R precursor matches deploy._ensure_evo_write_acl's
            # ownership-normalization sweep, same as the macOS branch.
            # 9c's one-shot heal verb stays a deliberate omission until the
            # delivery-monitor heal is ported (a `systemctl start` grant is
            # the likely shape; decide with that port, don't pre-grant).
            "# ── 9b. Grant evo user write ACL on proposal / signal stores ───────────────\n"
            "# Same contract as the macOS branch (spec-evo-account-separation):\n"
            "# stores stay evolve-owned; an inherited POSIX ACL grants evo the\n"
            "# write/delete its MCP tools need for state transitions.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {shared}/signals\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {shared}/signals\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/proposals\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/signals\n"
            "# keystore/ + config_intents/ joined EVO_WRITE_SHARED_SUBDIRS after\n"
            "# these grants were first written (2026-06-06 config_intents PR) —\n"
            "# _ensure_evo_write_acl fired for them on every pass and died\n"
            "# silently (2026-07-29 VPS denial-audit wave 2: 16 denials/day per\n"
            "# shape on evolve-vps-pod).\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {shared}/config_intents\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {shared}/config_intents\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/keystore\n"
            f"evolve ALL=(root) NOPASSWD: {chown} -R * {shared}/config_intents\n"
            "# Per-bot shared-store subdir read ACEs (_create_bot_subdir's\n"
            "# acl_perms leg — pairs with the §9a mkdir/chmod/chown trio).\n"
            "# The path patterns carry a wildcard tail, so the ENTRY SPEC is\n"
            "# pinned literally (u:evolve:rX — LinuxPerms' read verb bits):\n"
            "# evolve can grant only ITSELF read through these lines, unlike\n"
            "# the bare `-m *` spec the trailing-anchored grants above use.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rX {shared}/metrics/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m u\\:evolve\\:rX {shared}/metrics/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rX {shared}/annotations/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m u\\:evolve\\:rX {shared}/annotations/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rX {shared}/*/turns\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m u\\:evolve\\:rX {shared}/*/turns\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rX {shared}/*/spans\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m u\\:evolve\\:rX {shared}/*/spans\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m u\\:evolve\\:rX {shared}/*/cascade\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -d -m u\\:evolve\\:rX {shared}/*/cascade\n"
            "# Deploy-checkout packages/ read ACL (grant_read_recursive pair —\n"
            "# the admin daemon imports analyzer modules like cost_ledger).\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {repo}/packages\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {repo}/packages\n"
            "\n"
            "# ── 9b2. ACL-mask repair (Linux only — macOS has no POSIX mask) ─────────────\n"
            "# A chmod that touches group bits silently BECOMES the ACL mask and\n"
            "# caps every named ACE (design-linux-port §4). LinuxPerms.reassert_mask\n"
            "# re-widens it with exactly `setfacl -m m::rwX <path>` — on shared-store\n"
            "# subdirs after mode normalization, and per-masked-path under the\n"
            "# plugin install dir after its 755 sweep. Colons are escaped because\n"
            "# visudo rejects bare `:` in argument patterns (the lsof grant\n"
            "# precedent). The mask entry only re-widens what named ACEs already\n"
            "# grant — it cannot add a new principal or new bits by itself.\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m m\\:\\:rwX {shared}\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m m\\:\\:rwX {shared}/*\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m m\\:\\:rwX {plugin_dir}\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -m m\\:\\:rwX {plugin_dir}/*\n"
            "\n"
            # 9b3 (sudo getfacl probe grants) was removed 2026-06-11: no
            # code path ever invoked `sudo getfacl` — LinuxPerms' probes
            # run unprivileged by design (runtime/perms.py), and the
            # planned privileged-probe escalation was never built. A grant
            # without a consumer is pure attack surface; if the escalation
            # path lands later, its grant returns in the SAME PR as the
            # consumer.
        )
        + "# ── 9d. Retire-bot orphan sweep: per-bot {sharedDir}/<bot>/ dir ─────────────\n"
        "# retire_orphans.sweep_orphan_state deletes {sharedDir}/<bot>/ on retire.\n"
        "# Its leaf files (e.g. <bot>/cascade/tier1_active.json) are written by the\n"
        "# bot's own gateway process and owned by the bot's macOS UID at mode 600,\n"
        "# so the evolve admin can't unlink them and shutil.rmtree hits EACCES; it\n"
        "# retries via `sudo /bin/rm -rf`. Under `sudo evolve-admin retire-bot` the\n"
        "# CLI already runs as root, but the admin-daemon (web UI) retire path runs\n"
        "# as evolve and needs this grant. macOS visudo globs don't cross '/', so\n"
        "# the wildcard matches exactly one path component — a direct child of the\n"
        "# shared dir (the <bot> dir) — and can neither match the shared dir itself\n"
        "# nor escape it. evolve already owns + manages this tree, so this adds no\n"
        "# destructive reach it doesn't already have on its own children.\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {shared}/*\n"
        "\n"
        "# ── 10. Write network.json (via /tmp staging only) ──────────────────────────\n"
        "# network.json may be root-owned if setup wizard ran as root.\n"
        "# Admin UI routes write it via this grant when direct write fails.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-network-*.json {shared}/network.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {shared}/network.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/network.json\n"
        "\n"
        "# ── 10a. Write install.json (via /tmp staging only) ─────────────────────────\n"
        "# deploy.write_install_json / record_bot_deploy write the version record after\n"
        "# every upgrade. The fast path is a direct write_text (evolve owns {shared} on\n"
        "# macOS), but at bootstrap install.json can be root-owned (the CLI/root install\n"
        "# path created it) — then the daemon (evolve, e.g. the web Upgrade route) falls\n"
        "# back to /tmp staging + sudo cp. Without these grants that fallback prompts for\n"
        "# a password and the per-bot deploy aborts. The chown lands it evolve-owned so\n"
        "# the fast path wins thereafter (parity with macOS). Source prefix = _secure_stage\n"
        "# (`/tmp/evolve-stage-*.json`). Mirrors the network.json trio above.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-stage-*.json {shared}/install.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {shared}/install.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {shared}/install.json\n"
        "\n"
        + (
            "# ── 11. Write LaunchDaemon plists (via /tmp staging only) ───────────────────\n"
            f"evolve ALL=(root) NOPASSWD: {cp} /tmp/*.plist {daemons}/ai.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {cp} /tmp/*.plist {daemons}/ai.openclaw.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {daemons}/ai.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chown} * {daemons}/ai.openclaw.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 644 {daemons}/ai.evolve.*.plist\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} 644 {daemons}/ai.openclaw.evolve.*.plist\n"
            "\n"
            if macos else
            # Linux: unit-file writes for ai.evolve.* / ai.openclaw.* are in
            # the section-8 systemd set (one staging ritual serves all jobs).
            "# ── 11. Write unit files — covered by the section-8 systemd set ─────────────\n"
            "\n"
        )
        + "# ── 11b. Write per-bot app manifests ────────────────────────────────────────\n"
        f"# Manifests live with the bot at {home}/<bot>/.openclaw/workspace/manifests/.\n"
        "# Scanner grants evolve a write ACL on this dir when it runs as the bot;\n"
        "# these sudoers grants are the bootstrap path for the first save (before any\n"
        "# scan has run) and the fallback for cases where the ACL gets stripped.\n"
        "#\n"
        "# The mkdir+chown+chmod set mirrors what set_evolve_read_acl(bot_id) in\n"
        "# deploy.py runs on every deploy. Without these grants, the dir-creation\n"
        "# block in that function silently no-ops when invoked from the evolve-user\n"
        "# admin daemon (e.g. UI 'Deploy' button) and the bot ends up without\n"
        "# workspace/manifests/. The first forge install onto that bot then fails\n"
        "# at Step 10 with 'manifest not found' because the seed save can't create\n"
        "# the dir. See 2026-06-01 task-manager install regression.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-manifest-*.json {home}/*/.openclaw/workspace/manifests/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/workspace/manifests\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/manifests\n"
        # The ACL half of the ritual is platform-shaped: chmod +a on macOS,
        # the LinuxPerms setfacl access/default pair on Linux (the +a form
        # rendered on Linux pre-W5A was a dead grant — no Linux chmod has +a).
        + (
            f"evolve ALL=(root) NOPASSWD: {chmod} +a * {home}/*/.openclaw/workspace/manifests\n"
            f"evolve ALL=(root) NOPASSWD: {chmod} -R +a * {home}/*/.openclaw/workspace/manifests\n"
            if macos else
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -m * {home}/*/.openclaw/workspace/manifests\n"
            f"evolve ALL=(root) NOPASSWD: {setfacl} -R -d -m * {home}/*/.openclaw/workspace/manifests\n"
        )
        + "\n"
        "# ── 11c. Write per-bot INSTALLED_APPS.md ────────────────────────────────────\n"
        "# Generated from manifest state by app_registry.regenerate_installed_apps_md.\n"
        "# Direct write usually succeeds (workspace root has the evolve ACL); this\n"
        "# grant is the /tmp staging fallback for fresh bots before set_evolve_read_acl\n"
        "# has run.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/INSTALLED_APPS.md\n"
        "\n"
        "# ── 11d. Write per-bot audit pod_config.json ────────────────────────────────\n"
        "# Synced slice of network.json the audit_runner reads at each tick.\n"
        "# See packages/admin/evolve_admin/applications/audit_pod_config.py.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-pod-config-*.json {home}/*/.openclaw/workspace/evolve/pod_config.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/workspace/evolve/pod_config.json\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/workspace/evolve\n"
        "\n"
        "# ── 11f. Write per-bot workspace .git/config (backup remote management) ──────\n"
        "# Both the github onboarding flow (_ensure_github_remote) and the github\n"
        "# token rotation flow (api_admin_rotate_github_integration_token) write the\n"
        "# bot's workspace .git/config via /tmp staging. The file is bot-owned and\n"
        "# the `workspace` root has no inherited write ACL for evolve (only\n"
        "# workspace/evolve/ does), so sudo cp is the only path. Missing this grant\n"
        "# was the root cause of the 'sudo: a password is required' failure seen\n"
        "# from the Backup → Cloud wizard's Set Up button.\n"
        "# chmod 600, not 644: under https_pat auth the remote URL embeds a GitHub\n"
        "# PAT, making this a token-bearing file (BOT_SECRET_CONFIG_RELPATHS) — 644\n"
        "# left the token world-readable on a multi-user box. Bot keeps owner rw;\n"
        "# evolve reads via the inherited ACL (+ the sudo cat above).\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/.git/config\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.gitconfig {home}/*/.openclaw/workspace/.git/config\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/.git/config\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/workspace/.git/config\n"
        "\n"
        "# ── 11e. Kick the per-bot audit_runner for on-demand requests ────────────────\n"
        "# When UI / CLI / evo audit triggers an audit, the admin writes an inbox file\n"
        "# then invokes the runner via this sudo grant so the bot acts immediately\n"
        "# rather than waiting for its next hourly tick. See spec §7.3.\n"
        "#\n"
        "# Note: the runas spec MUST be (ALL), not (*). macOS sudo's parser accepts\n"
        "# (*) syntactically and `sudo -l` displays it, but at match time it doesn't\n"
        "# match any actual user — every kick silently failed with \"user evolve is\n"
        "# not allowed to execute ... as <bot>\". The Popen(stderr=DEVNULL) in\n"
        "# audit_dispatch._kick_runner swallowed the error and the dispatch returned\n"
        "# ok=true, so the failure was invisible until 2026-05-26 diagnosis.\n"
        f"evolve ALL=(ALL) NOPASSWD: {audit_python} {repo}/packages/analyzer/app_audit_runner.py *\n"
        "\n"
        "# ── 11g. First-party app install (install_evolve_app) — shared manifest ──────\n"
        "# install_evolve_infra_jobs loops FIRST_PARTY_EVOLVE_APPS → install_evolve_app,\n"
        "# which stages each app's manifest.json to /tmp and sudo-cp's it into the shared\n"
        "# applications dir. The manual `install-infra-jobs` CLI runs as root so the cp\n"
        "# lands; the repo-puller LaunchDaemon re-runs the same function as the `evolve`\n"
        "# user on every infra-touching pull (repo_puller.py), where only granted commands\n"
        "# land. Without these two grants the manifest step password-prompts and returns\n"
        "# early (install_evolve_app `if not result.success: return result`) — flipping\n"
        "# DeployResult.success=False on every post-pull app refresh (#2922 follow-up,\n"
        "# same root cause as the doc-seeding grants in §12). On post-evo-separation pods\n"
        f"# {shared}/applications/evolve is owned by the `evo` user, so evolve needs root\n"
        "# here. Source pinned to install_evolve_app's /tmp/evolve-app-* prefix; dst pinned\n"
        "# to the evolve applications dir (the `*` is the app_id — sudoers globs never\n"
        "# cross a '/') so a compromised evolve account can only overwrite a first-party\n"
        "# manifest with a /tmp-staged file — the same blast-radius limit as the §11b\n"
        "# per-bot manifest grant.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/applications/evolve\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-app-*.json {shared}/applications/evolve/*.json\n"
        "\n"
        "# ── 11h. Stamp provenance markers on bot-owned workspace files (Sync 'Fix') ──\n"
        "# The Sync Applications modal's 'Fix' on a missing-marker row stamps a\n"
        "# provenance marker onto a bot-owned script (workspace/scripts/*.py,\n"
        "# *-cron.sh, …). evolve has ACL READ but not write on those paths, so the\n"
        "# admin server computes the marked content as evolve (read is enough) and\n"
        "# this single fixed-path helper, run as root, applies it. The helper is the\n"
        "# security boundary — NOT a broad cp wildcard: it re-validates that the\n"
        "# destination is an existing regular file (never a symlink), resolves under\n"
        "# {home}/<bot>/.openclaw/workspace/, and is can_app_own-eligible (no secret /\n"
        "# telemetry / OpenClaw-standard path), then writes preserving the prior mode\n"
        "# and chowning to the bot. A per-path cp grant can't express the arbitrary\n"
        "# depth + extensions of workspace scripts (a sudoers '*' never crosses '/'),\n"
        "# which is exactly why a fixed-path helper that validates its own args is the\n"
        "# right shape. Interpreter is the VENV python (it imports evolve_admin's\n"
        "# ownership policy), matching §7a. Mirrors marker_embed_helper.helper_script_path().\n"
        f"evolve ALL=(root) NOPASSWD: {venv_python} {repo}/packages/admin/evolve_admin/applications/marker_embed_helper.py *\n"
        "\n"
        "# ── 12. Write bot AGENTS.md and workspace files ─────────────────────────────\n"
        # deploy_bot step 4 calls _sudo_chown(workspace/evolve, bot_user) to fix ownership
        # when the dir was created by root (e.g. initial deploy ran as root).
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {home}/*/.openclaw/workspace/evolve\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/evolve/*.md\n"
        "# workspace/{AGENTS,SOUL,MEMORY,README}.md — the primary bot's identity docs,\n"
        "# fallback when the ACL write isn't yet set by set_evolve_read_acl.\n"
        "# bot_doc_seeding.write_doc stages each to /tmp and sudo-cp's it into the\n"
        "# bot-owned workspace (deploy.install_bot_docs / install_evolve_bot_docs,\n"
        "# role=\"primary\"). The manual `install-infra-jobs` path runs as root so every\n"
        "# cp lands; the repo-puller LaunchDaemon re-runs install_evolve_infra_jobs as\n"
        "# the `evolve` user after an infra-touching pull, where only granted filenames\n"
        "# land — without the SOUL/MEMORY/README cp + the chmod 644 below, those docs\n"
        "# don't fully land and write_doc's final chmod password-prompts, flipping\n"
        "# DeployResult.success=False (a spurious infra-install failure; PR #2921\n"
        "# reviewer follow-up). dst is pinned per-filename (not a `*.md` wildcard) so\n"
        "# the /tmp-staged source can only overwrite these four Evolve-owned identity\n"
        "# docs — the same blast-radius limit as the long-standing AGENTS.md grant.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/AGENTS.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/SOUL.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/MEMORY.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/README.md\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/AGENTS.md\n"
        "# write_doc runs `mkdir -p <workspace>` before each cp; the §11b grant covers\n"
        "# only the /manifests child, not the workspace root. Almost always a no-op,\n"
        "# but sudo needs the grant or it password-prompts on the evolve-user path.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/workspace\n"
        # v17 install_helpers.install_heartbeat_instruction writes managed
        # sections to HEARTBEAT.md via direct ACL write + os.replace, which
        # leaves the file owned by evolve. The chown grant below restores
        # bot ownership so install_integrity_monitor doesn't flag drift and
        # OC's session writer can later overwrite the file without EACCES.
        # Wildcarded to *.md so future install_helpers callers (e.g. apps
        # that target INSTRUCTIONS.md / NOTES.md) inherit the fix; the chmod 644
        # companion below is write_doc's final step (identity docs are 0644, not
        # secrets) and was the call that errored on the evolve-user repo-puller path.
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/*.md\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/workspace/*.md\n"
        "# workspace/procedures/ — install_evolve_bot_docs creates this dir for the\n"
        "# primary bot and chowns it to the bot user (mkdir + chown the DIR). Same\n"
        "# evolve-user repo-puller rationale as the docs above.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/workspace/procedures\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/procedures\n"
        "# Per-app procedure FILES — install_evolve_app (the §11g manifest's sibling step)\n"
        "# stages each app's procedure.md to /tmp and sudo-cp's it to\n"
        "# <bot>/.openclaw/workspace/procedures/<app>.md, then chowns it to the bot user.\n"
        "# The dir grants above cover only the DIR; a sudoers `*` never crosses a '/', so\n"
        "# the per-file cp + chown need their own grants or the evolve-user repo-puller\n"
        "# path password-prompts on every app refresh. Source pinned to the evolve-proc\n"
        "# /tmp prefix; dst is the per-app *.md under any bot's procedures dir.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proc-*.md {home}/*/.openclaw/workspace/procedures/*.md\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/procedures/*.md\n"
        "# Primary-bot reference library (B6 AGENTS.md split, #3541) —\n"
        "# bot_doc_seeding.install_primary_reference_docs seeds\n"
        "# evolve/reference/{PAGE_CONTEXT,GLOSSARY,PLAYBOOKS,COMMANDS}.md via\n"
        "# write_doc (/tmp staging + cp + chown + chmod 644) and chowns the dir\n"
        "# to the bot user (the bot's own APPS_GUIDE.md writer needs a\n"
        "# bot-owned dir). Without these grants the evolve-user deploy paths\n"
        "# (repo-puller redeploy, admin daemon) password-prompt on every\n"
        "# primary doc install — the same failure shape the identity-doc\n"
        "# grants above were added for (#2921). dst pinned per filename, the\n"
        "# same blast-radius limit as those grants: the existing workspace\n"
        "# *.md / procedures grants end in literal tails that can never match\n"
        "# paths under evolve/reference/.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/workspace/evolve/reference\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/evolve/reference\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/evolve/reference/PAGE_CONTEXT.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/evolve/reference/GLOSSARY.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/evolve/reference/PLAYBOOKS.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/workspace/evolve/reference/COMMANDS.md\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/evolve/reference/*.md\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/workspace/evolve/reference/*.md\n"
        "# Read fallback for AGENTS.md — heal needs to read existing content before\n"
        "# rewriting it (otherwise the rewrite would destroy any non-POD_CONDUCT.md\n"
        "# content). Direct read via ACL is primary; this grants the sudo cat fallback.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/AGENTS.md\n"
        "# POD_CONDUCT.md propagation: heal copies the pod-wide conduct file from\n"
        "# the shared dir into each bot's workspace, then chowns it to the bot user.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} {shared}/POD_CONDUCT.md {home}/*/.openclaw/workspace/POD_CONDUCT.md\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/workspace/POD_CONDUCT.md\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.md {home}/*/.openclaw/agents/main/AGENTS.md\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/agents/main/AGENTS.md\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/agents/main/AGENTS.md\n"
        # POD_CONDUCT.md lives at workspace root (not under workspace/evolve/)
        # matching the AGENTS.md placement pattern — pod-wide rules readable by
        # any agent, not just the evolve plugin. Both writers
        # (deploy.inject_pod_conduct, heal.check_pod_conduct_injection) cp from
        # exactly /Users/Shared/evolve/POD_CONDUCT.md, so the exact-source grant
        # above covers them. A wildcard-SOURCE variant (`/bin/cp *  <dst>`) that
        # historically lived here was removed 2026-06-10 (roadmap 2.10): a bare
        # `*` source lets a compromised evolve account copy ANY readable file —
        # e.g. a crafted /tmp payload — over a bot-owned workspace file as root.
        "\n"
        "# ── 13. Fix file ownership on bot config files ───────────────────────────────\n"
        "# openclaw.json + its .bak hold the gateway + channel tokens, so they are\n"
        "# 0600, not 0644. The live openclaw.json chmod-600 grant is in §4; the .bak\n"
        "# (a copy of the same secret, written by safe_write_bot_config) gets its own\n"
        "# chmod-600 grant here, consumed by the secret_config_perms self-heal.\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/openclaw.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/openclaw.json.bak\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.openclaw/openclaw.json.bak\n"
        "\n"
        "# ── 14. Write/delete proposals and feedback (via /tmp staging) ───────────────\n"
        "# Needed when proposals/ and feedback/ dirs are root-owned (setup ran as root).\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/proposals/pending\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/proposals/approved\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/proposals/rejected\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/proposals/deployed\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/proposals/validation-results\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/feedback\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {shared}/tests\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/proposals/pending/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/proposals/approved/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/proposals/rejected/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/proposals/deployed/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/proposals/validation-results/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-proposal-* {shared}/feedback/rejections.jsonl\n"
        f"evolve ALL=(root) NOPASSWD: {rm} {shared}/proposals/pending/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {rm} {shared}/proposals/approved/*.json\n"
        "\n"
        "# ── 15. Git pull and plugin build in evolve-repo ─────────────────────────────\n"
        "# Upgrade job chowns .git and packages/plugin to evolve, runs git pull + npm,\n"
        "# then restores original owner.  The -R flag must be literal in the rule.\n"
        "# The whole-repo rule normalizes ownership after a fresh clone (which\n"
        "# typically lands as the human admin user); deploy_shared_dir invokes it.\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {repo}\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {repo}/.git\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {repo}/packages/plugin\n"
        "# chmod -R g+rwX runs alongside the chown above to make the deploy\n"
        "# checkout group-writable for both the evolve daemon and the human\n"
        "# admin (both in the staff group). See deploy_shared_dir().\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} -R g+rwX {repo}\n"
        "# Same g+rwX grant on packages/plugin/dist specifically: build_plugin()'s\n"
        "# `git checkout -- dist` step (run under sudo) writes root-owned files at\n"
        "# mode 644 in mode-755 dirs. The cleanup chowns back to evolve:staff but\n"
        "# also needs to add group-write so the next git pull from a different\n"
        "# staff-group user (the human admin) can unlink the old files.\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} -R g+rwX {repo}/packages/plugin/dist\n"
        "\n"
        "# ── 16. Plugin install directory (evolve-plugin) ──────────────────────────────\n"
        "# build_plugin() and fix_plugin_permissions() install compiled artifacts here\n"
        "# as root:wheel so openclaw's security scanner accepts them.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {plugin_dir}\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {plugin_dir}/dist\n"
        f"evolve ALL=(root) NOPASSWD: {rm} -rf {plugin_dir}/node_modules\n"
        # Sources are pinned to the deploy checkout (same hardcoded path as the
        # §15 git-pull grants) — build_plugin() copies from
        # _REPO_ROOT/packages/plugin, which IS the deploy checkout whenever the
        # caller is the evolve daemon (the only invocation these grants serve;
        # operator-run setup executes as root and bypasses sudoers). Wildcard
        # SOURCES here previously let the evolve account install arbitrary
        # readable content as root-owned plugin code — removed 2026-06-10
        # (roadmap 2.10).
        f"evolve ALL=(root) NOPASSWD: {cp} -R {repo}/packages/plugin/dist {plugin_dir}/dist\n"
        f"evolve ALL=(root) NOPASSWD: {cp} -R {repo}/packages/plugin/node_modules {plugin_dir}/node_modules\n"
        f"evolve ALL=(root) NOPASSWD: {cp} {repo}/packages/plugin/package.json {plugin_dir}/package.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} {repo}/packages/plugin/package-lock.json {plugin_dir}/package-lock.json\n"
        f"evolve ALL=(root) NOPASSWD: {cp} {repo}/packages/plugin/openclaw.plugin.json {plugin_dir}/openclaw.plugin.json\n"
        # plugin_signature.stamp_manifest_in_place() re-writes the DEPLOYED
        # manifest (dist digest stamp, called from build_plugin) via mkstemp
        # staging — its evolve-manifest-* prefix is the constrained source
        # glob, same shape as the §11b workspace-manifest grant.
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-manifest-*.json {plugin_dir}/openclaw.plugin.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} -R * {plugin_dir}\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} -R 755 {plugin_dir}\n"
        "\n"
        "# ── 17. Probe TCP listener ownership for upgrade-safety preflight ────────────\n"
        "# safe_upgrade.gate_port_owners runs lsof to confirm each gateway port has a\n"
        "# listener owned by the expected bot user. macOS lsof from a non-root user\n"
        "# only sees the calling user's sockets — bot gateways run under per-bot users,\n"
        "# so an unprivileged probe falsely reports every gateway as 'not running'.\n"
        "# Colons must be backslash-escaped — macOS visudo rejects bare ':' in args.\n"
        f"evolve ALL=(root) NOPASSWD: {lsof} -nP -iTCP\\:* -sTCP\\:LISTEN -Fpcun\n"
        "\n"
        "# ── 17a. Kill orphaned openclaw background processes (security, message) ───────\n"
        "# `openclaw security audit` and `openclaw message send` spawn background children\n"
        "# (openclaw-security, openclaw-message) that run as the bot user. oc_cli.py's\n"
        "# _kill_pg() tries os.killpg() first; when that raises PermissionError (process\n"
        "# group owned by the bot user, not evolve), it falls back to this grant.\n"
        "# The -9 flag is SIGKILL; the argument -<pgid> targets the whole process group.\n"
        f"evolve ALL=(root) NOPASSWD: {kill} -9 -*\n"
        "\n"
        "# ── 18. Write per-bot filesystem-skill config files ────────────────────────────\n"
        "# Filesystem skills (Obsidian as obsidian_vault, plus future ones) store their\n"
        f"# config in {home}/<bot>/.openclaw/skills/<skill_id>.json. The file and directory\n"
        "# are owned by the bot user; the evolve user writes via /tmp staging + sudo cp.\n"
        "# mkdir grant creates the skills/ dir on first install if it doesn't exist.\n"
        "# Grants are wildcarded over *.json in the skills dir so new filesystem skills\n"
        "# work without re-rolling sudoers.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/skills\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.json {home}/*/.openclaw/skills/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/skills\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/skills/*.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/skills/*.json\n"
        "\n"
        "# ── 19. Write bot cron/jobs.json (UpsertCronJob / RemoveCronJob appliers) ─────\n"
        f"# permissions.writer.write_cron_jobs targets {home}/<bot>/.openclaw/cron/jobs.json\n"
        "# via /tmp staging + sudo cp + chown + chmod. sudoers grants are per-destination\n"
        "# (macOS visudo globs don't cross '/'), so the openclaw.json grants in §4/§13\n"
        "# don't cover this path. mkdir is belt-and-suspenders for fresh bots whose cron/\n"
        "# dir doesn't exist yet. See docs/diagnosis-cron-caps-applier-sudo-wall-2026-05-20.md.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.openclaw/cron\n"
        "# Dir chown after the mkdir: the gateway (running as the bot) writes its own\n"
        "# store files in cron/ — OpenClaw ≥2026.7 stages its import-once migration\n"
        "# temp file there and fails ('Failed writing migrated cron store') when a\n"
        "# fresh sudo-mkdir left the dir root-owned.\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/cron\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.json {home}/*/.openclaw/cron/jobs.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/cron/jobs.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/cron/jobs.json\n"
        "\n"
        "# ── 20. Write bot exec-approvals.json (UpdateExecApproval applier) ───────────\n"
        f"# permissions.writer.write_exec_approvals targets {home}/<bot>/.openclaw/exec-approvals.json.\n"
        "# Latent gap until exec_approval mutation proposals start auto-promoting; granted\n"
        "# alongside cron/jobs.json so the next sibling applier doesn't hit the same wall.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-*.json {home}/*/.openclaw/exec-approvals.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/exec-approvals.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/exec-approvals.json\n"
        "\n"
        "# ── 20a. Write bot exec-approvals.preview.json (app-derived permissions) ─────\n"
        "# evolve_admin.app_permissions.reconciler writes the manifest-derived would-be\n"
        "# allowlist here on every deploy. Tracking-only in Phase A — the file is the\n"
        "# seed for the operator opt-in toggle (Phase C). Sits as a sibling next to\n"
        "# exec-approvals.json. See docs/spec-app-derived-permissions-2026-05-24.md.\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-preview-*.json {home}/*/.openclaw/exec-approvals.preview.json\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.openclaw/exec-approvals.preview.json\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.openclaw/exec-approvals.preview.json\n"
        "\n"
        "# ── 21. Distribute the shared backup SSH key into each bot's ~/.ssh ──────────\n"
        "# /api/backup/cloud/keys/distribute (alias: /api/security/backup-distribute-key)\n"
        "# stages the private + matching .pub into\n"
        "# /tmp/evolve-backup-<bot>-{priv,pub}-XXXXXX, then sudo cp's both into\n"
        f"# {home}/<bot>/.ssh/evolve-backup-<bot>{{,.pub}} owned by the bot user (600/644)\n"
        "# so the per-bot backup daemon (running as the bot user) can use it for the\n"
        "# `ssh -i ~/.ssh/evolve-backup-<bot>` git push. SSH's identity_sign step refuses\n"
        "# to load a private key whose paired .pub on disk doesn't match — that's why we\n"
        "# distribute the .pub alongside instead of only the private half.\n"
        f"evolve ALL=(root) NOPASSWD: {mkdir} -p {home}/*/.ssh\n"
        f"evolve ALL=(root) NOPASSWD: {cp} /tmp/evolve-backup-* {home}/*/.ssh/evolve-backup-*\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.ssh\n"
        f"evolve ALL=(root) NOPASSWD: {chown} * {home}/*/.ssh/evolve-backup-*\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 700 {home}/*/.ssh\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 600 {home}/*/.ssh/evolve-backup-*\n"
        f"evolve ALL=(root) NOPASSWD: {chmod} 644 {home}/*/.ssh/evolve-backup-*\n"
        "\n"
        "# ── 22. Verify evolve sudoers syntax from the infra audit ────────────────────\n"
        "# applications.infra_audit._visudo_check runs `sudo -n /usr/sbin/visudo -c -f`\n"
        "# against the two evolve sudoers files to detect syntax breakage. Without these\n"
        "# narrow grants the audit can't escalate non-interactively; sudo's stderr\n"
        "# (\"a terminal is required to read the password\") would be misread as a\n"
        "# visudo parse error, producing critical-severity false positives. (Bug\n"
        "# surfaced 2026-05-25: two `sudoers_invalid_syntax` proposals fired at 0.85\n"
        "# confidence on files that validated fine under interactive sudo.) The grants\n"
        "# are read-only (-c is a syntax check; visudo never writes) and constrained to\n"
        "# the two evolve-owned files, so they can't be repurposed to mutate sudoers.\n"
        f"evolve ALL=(root) NOPASSWD: {visudo} -c -f /etc/sudoers.d/evolve\n"
        f"evolve ALL=(root) NOPASSWD: {visudo} -c -f /etc/sudoers.d/evolve-admin\n"
        "\n"
        "# ── 23. Read evolve sudoers files for the infra audit content check ───────────\n"
        "# applications.infra_audit._read_sudoers_contents runs `sudo -n <cat>` to read\n"
        "# the two evolve-owned drop-ins and verify the load-bearing grants are present\n"
        "# (the required-grant check). The files are mode 0440 root and live under\n"
        "# /etc/sudoers.d (0750 root on Linux), so the evolve user can't read them\n"
        "# directly — without this grant the content check always returned None and the\n"
        "# required-grant verification was effectively dead. Pinned to the two exact\n"
        "# files (no wildcard) so the grant can't be repurposed to read other root-only\n"
        "# files; read-only (cat) — the audit never writes sudoers. The {cat} path is\n"
        "# the same profile command the audit invokes, so grant and argv can't drift.\n"
        f"evolve ALL=(root) NOPASSWD: {cat} /etc/sudoers.d/evolve\n"
        f"evolve ALL=(root) NOPASSWD: {cat} /etc/sudoers.d/evolve-admin\n"
        "\n"
        "# ── 24. Machine + workspace audit probes (2026-07-29 VPS denial census) ──────\n"
        "# Four read-only probe families that had NO grant on either platform, so\n"
        "# their checks were dark fleet-wide (each degraded to a 'skipped'/'not\n"
        "# configured' finding or an empty scan, silently):\n"
        "#   • crontab -l — the application scanner reads each bot's crontab via\n"
        "#     `sudo -u <bot> crontab -l`. Bots are dynamic (added without a\n"
        "#     sudoers refresh), so the runas list is ALL; the command is pinned\n"
        "#     to the binary + the read-only -l flag.\n"
        "#   • sshd -T / lsof — audit.py's machine-hygiene checks (_check_ssh_config,\n"
        "#     _check_listening_ports). Binaries from the profile table; colons\n"
        "#     escaped (visudo rejects bare ':' in args — §17 precedent).\n"
        "#   • git rev-parse/show on the bot workspace repo — audit_identity's\n"
        "#     baseline check. `-c safe.directory=*` is required: git 2.35.2+\n"
        "#     refuses bot-owned repos even as root (dubious ownership). Both\n"
        "#     verbs are read-only; the -C path is pinned under the bot homes.\n"
        "#   • cat fallbacks for two evolve-workspace channels OUTSIDE the §3h\n"
        "#     doc set: pod_config.json (audit_pod_config's compare-before-write\n"
        "#     read) and audit_outbox/* (audit_poller drains bot-written recs,\n"
        "#     which the OC gateway umask mints 0600 — the ACL-mask birth clamp\n"
        "#     makes this fallback load-bearing on Linux until the hourly mask\n"
        "#     reassert catches up; same class as the §3i grants).\n"
        f"evolve ALL=(ALL) NOPASSWD: {crontab} -l\n"
        f"evolve ALL=(root) NOPASSWD: {sshd} -T\n"
        f"evolve ALL=(root) NOPASSWD: {lsof} -iTCP -sTCP\\:LISTEN -n -P\n"
        f"evolve ALL=(root) NOPASSWD: {git} -c safe.directory=* -C {home}/*/.openclaw/workspace rev-parse HEAD\n"
        f"evolve ALL=(root) NOPASSWD: {git} -c safe.directory=* -C {home}/*/.openclaw/workspace show HEAD\\:*\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/evolve/pod_config.json\n"
        f"evolve ALL=(root) NOPASSWD: {cat} {home}/*/.openclaw/workspace/evolve/audit_outbox/*\n"
        "\n"
        "# ── 25. Bot-user host-privilege probe (/etc/sudoers.d scan) ─────────────────\n"
        "# audit._check_bot_user_privilege fires CRITICAL when a BOT account holds\n"
        "# host admin/sudo — the inverse of the admin-user-gateway invariant, found\n"
        "# live on the mini 2026-08-02 (two legacy bot accounts in the macOS `admin`\n"
        "# group, hence stock %admin sudo + SSH reachability).\n"
        "# Group membership itself needs NO grant: the check probes it with an\n"
        "# unprivileged `id -Gn <user>` plus the platform group-record read. That\n"
        "# is deliberate — a grant-dependent probe is how `sshd -T` stayed dark\n"
        "# fleet-wide for months (#3462). Only the drop-in scan needs root:\n"
        "#   • ls -1 /etc/sudoers.d — the directory happens to be world-listable\n"
        "#     on macOS and 0750 root:root on Debian/Ubuntu; the check takes the\n"
        "#     granted path on both so the platforms behave identically.\n"
        "#   • cat /etc/sudoers.d/* — drop-ins are 0440 root on both platforms, so\n"
        "#     the fallback is the normal path. This WIDENS the §23 pair (which\n"
        "#     pinned evolve/evolve-admin by name) to the whole directory: the check\n"
        "#     must read files it cannot know the names of, since an operator-\n"
        "#     created drop-in granting a bot sudo is exactly what it is looking\n"
        "#     for. Still read-only and still confined to /etc/sudoers.d — nothing\n"
        "#     outside the sudoers policy dir becomes readable. The §23 grants stay\n"
        "#     as the narrower, self-documenting pins for the required-grant check.\n"
        f"evolve ALL=(root) NOPASSWD: {ls} -1 /etc/sudoers.d\n"
        f"evolve ALL=(root) NOPASSWD: {cat} /etc/sudoers.d/*\n"
    )

    return content


def _write_evolve_sudoers(initiated_by: str = "wizard") -> bool:
    """
    Write /etc/sudoers.d/evolve with narrow sudo grants for the evolve service user.

    The evolve user (web server / background jobs) needs passwordless sudo to:
      - Read bot openclaw.json and auth-profiles.json (via /bin/cat as root)
      - Write bot openclaw.json via /tmp staging (atomic config apply)
      - Run openclaw CLI as bot users (cron, status queries)
      - Run oc_model.py/oc_keys.py as bot users (model/key management)
      - Restart bot gateways via launchctl kickstart
      - Manage evolve's own launchd jobs (bootstrap/bootout)

    ``initiated_by`` tags the admin-actions.jsonl entry so wizard / cli /
    repo-puller invocations are distinguishable post-hoc. The repo-puller's
    auto-refresh wrapper passes ``initiated_by="repo-puller"``; PR #1909
    landed that wrapper but forgot to widen this signature, so every
    auto-refresh crashed with TypeError until the f294255e investigation
    surfaced it (silently — the puller caught the TypeError into its WARN
    log and any new grant added by a subsequent PR sat dormant on disk).

    See docs/archive/specs/spec-sudoers.md for full rationale and attack-surface analysis.
    Returns True on success.
    """
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands

    content = _render_evolve_sudoers()
    if content is None:
        _err("openclaw CLI not found — install openclaw first, then re-run refresh-sudoers")
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    # Intentionally UNGRANTED to the evolve service user (Option B, PR #2759):
    # the puller reaches this in evolve context (repo_puller → this function),
    # but the service user must never be able to rewrite its own sudoers. The
    # refresh fails by design there and fires sudoers-refresh-failed at once so
    # the operator re-runs it as root. See [[feedback_sudo_subprocess_interpreter_must_be_venv]].
    # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    r = subprocess.run(
        ["sudo", c["visudo"], "-c", "-f", tmp_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        os.unlink(tmp_path)
        _err(f"evolve sudoers validation failed: {r.stderr.strip()}")
        return False

    dst = Path("/etc/sudoers.d/evolve")
    # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    r2 = subprocess.run(
        ["sudo", c["cp"], tmp_path, str(dst)],
        capture_output=True, text=True,
    )
    os.unlink(tmp_path)
    if r2.returncode != 0:
        _err(f"Failed to install /etc/sudoers.d/evolve: {r2.stderr.strip()}")
        return False

    # wheel is the macOS root group; root is its own group on Linux.
    root_group = "root:wheel" if profile.name == "macos" else "root:root"
    subprocess.run(["sudo", c["chmod"], "440", str(dst)], capture_output=True)  # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    subprocess.run(["sudo", c["chown"], root_group, str(dst)], capture_output=True)  # sudo-grant: ungranted-by-design: Option B PR#2759 — evolve must not rewrite its own sudoers
    _record_installed_sudoers_marker(content)
    _ok("  /etc/sudoers.d/evolve written for evolve service user")
    _log_admin_action("write_evolve_sudoers", "ok", bot="evolve", initiated_by=initiated_by)
    return True


def _record_installed_sudoers_marker(content: str) -> None:
    """Record a hash of the just-installed sudoers under ``{shared_dir}/state``.

    The repo-puller runs as the ``evolve`` service user, which (a) can't read
    root-owned ``/etc/sudoers.d/evolve`` and (b) is deliberately barred from
    installing it (Option B, #2759). So the puller can't tell whether the live
    sudoers matches the rendered template — it relies on THIS marker, written
    only by a *successful* install (which runs as root), as the in-sync oracle.
    With it, the puller's drift check fires the ``sudoers_refresh_failed``
    Signal when grants are dormant and AUTO-RESOLVES it once the operator runs
    ``refresh-sudoers`` — closing the cry-wolf where the old Signal never
    cleared after a manual fix and got ignored (sudoers sat stale Jun 12–16).

    Best-effort: a marker-write failure never fails the install — worst case
    the puller reports drift until the next successful refresh writes it.
    """
    try:
        import hashlib

        from evolve_config import get_shared_dir, load_config, resolve_network_path
        shared_dir = Path(get_shared_dir(load_config(str(resolve_network_path()))))
        state_dir = shared_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        marker = state_dir / "sudoers-installed.sha256"
        marker.write_text(hashlib.sha256(content.encode("utf-8")).hexdigest() + "\n")
        os.chmod(marker, 0o644)  # evolve (puller) must be able to read it
    except Exception as e:  # noqa: BLE001 — never fail the install on a marker hiccup
        _warn(f"  (could not record sudoers marker for drift detection: {e})")


def _setup_bot_shared_dirs(bots: list, shared_dir: Path) -> None:
    """Create per-bot shared analytics directories (turns, summaries, metrics)."""
    for bot in bots:
        name = bot.name if hasattr(bot, "name") else str(bot)
        bot_dir = shared_dir / name
        for subdir in ("turns", "summaries", "metrics"):
            d = bot_dir / subdir
            subprocess.run(["sudo", "/bin/mkdir", "-p", str(d)], capture_output=True)
            subprocess.run(
                ["sudo", "/usr/sbin/chown", "-R", f"{name}:staff", str(d)],
                capture_output=True,
            )
            # turns must be 1777 (sticky world-writable) so the bot user can
            # write its own turn files; summaries/metrics use 755
            mode = "1777" if subdir == "turns" else "755"
            subprocess.run(["sudo", "/bin/chmod", mode, str(d)], capture_output=True)
    _ok(f"  Per-bot shared dirs created for {len(bots)} bot(s)")


# ── macOS account helpers ─────────────────────────────────────────────────────

def _next_free_uid(start: "int | None" = None) -> int:
    """Find the next free UID on this system, starting at the active
    isolation adapter's platform convention (502 on macOS, 1000 on Linux)
    unless ``start`` is given explicitly."""
    from .runtime.isolation import get_isolation
    iso = get_isolation()
    if start is None:
        return iso.next_free_uid()
    return iso.next_free_uid(start=start)


def _user_exists(username: str) -> bool:
    from .runtime.isolation import get_isolation
    return get_isolation().user_exists(username)


def _create_bot_account(bot_id: str) -> bool:
    """
    Create a macOS user account for bot_id.
    Idempotent: returns True immediately if user already exists.
    Returns True on success, False on failure.

    bot_id IS the account name here — this is the account-creation path.
    The wizard always creates macOS accounts named after the logical bot_id.
    Do NOT route through bot_home()/get_bot_user() — they look up existing
    accounts; this call defines the new one.

    The dscl + createhomedir ritual (full sudo paths, dash-form verbs —
    the shapes the sudoers grants and sudo's secure_path require) lives in
    ``runtime.isolation.MacOSIsolation.create_user``.
    """
    from platform_profile import get_profile

    from .runtime.isolation import IsolationError, get_isolation

    noun = "macOS account" if get_profile().name == "macos" else "Linux account"
    iso = get_isolation()
    if _user_exists(bot_id):
        _skip(f"{noun} '{bot_id}'")
        return True

    uid = _next_free_uid()
    try:
        iso.create_user(bot_id, uid)
    except IsolationError as exc:
        _err(f"Account creation failed: {exc}")
        return False

    _ok(f"{noun} '{bot_id}' created (UID {uid})")
    return True


# ── Phase E.2.a: provision the `evo` account (empty) ─────────────────────────
#
# Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.a".
#
# Today the `evo` bot runs on the privileged `evolve` macOS user (the admin
# daemon's user). The structural fix (Phase E) splits them: evo runs on its
# own non-privileged `evo` user. Phase E.2.a is the first step of that split
# — provision the `evo` account empty, with the admin-daemon ACL grant, but
# DO NOT cut over anything yet. The cutover happens in E.2.b after E.3 lands
# the API endpoints + auth layer that lets evo's tools route through the
# admin daemon.
#
# Idempotent. Safe to re-run on existing installs. After this function:
# `/Users/evo/.openclaw/` exists, owned by `evo:staff`, with the inherited
# ACL grant for `evolve` user read. No openclaw.json (E.2.b populates that).
# No plist changes (evo still runs as the `evolve` user gateway).


def _provision_evo_account() -> bool:
    """Phase E.2.a — create the `evo` macOS account and empty .openclaw/ tree.

    Returns True on success, False on failure. Idempotent: re-running on
    a system where the account already exists is a no-op (or refresh-only,
    for the chown/ACL steps which are idempotent themselves).

    What this function does:
      1. Creates the `evo` macOS user via dscl (reuses _create_bot_account).
      2. Creates the empty `/Users/evo/.openclaw/` directory tree with the
         standard subdirs (workspace, logs, agents, credentials).
      3. Chowns the tree to `evo:staff` so the future evo gateway can write.
      4. Grants the `evolve` admin daemon user ACL read access via
         deploy.set_evolve_read_acl (matches every other bot's pattern).

    What this function does NOT do (deferred to E.2.b):
      - Write openclaw.json.
      - Write auth-profiles.json.
      - Change any LaunchDaemon plist's UserName.
      - Move evo's runtime state from /Users/evolve to /Users/evo.

    The account simply exists, ready for E.2.b's cutover.
    """
    from platform_profile import get_profile

    profile = get_profile()
    c = profile.commands

    # Step 1: create the account. Idempotent. (The isolation seam owns the
    # platform ritual — dscl/createhomedir on macOS, useradd on Linux.)
    if not _create_bot_account("evo"):
        _err("Failed to create 'evo' account.")
        return False

    # Step 2: empty .openclaw/ tree. Mirrors _setup_oc_for_bot's dirs but
    # stops short of writing openclaw.json + auth-profiles.json (those are
    # E.2.b's job — for now the tree is a placeholder).
    #
    # CONSTRUCTION-time path math on user_home_root is correct here: the
    # account was just created by this function (platform_profile module
    # docstring, "CONSTRUCTION vs RESOLUTION").
    home = Path(profile.user_home_root) / "evo"
    dirs = [
        home / ".openclaw",
        home / ".openclaw" / "workspace",
        home / ".openclaw" / "workspace" / "evolve",
        home / ".openclaw" / "logs",
        home / ".openclaw" / "agents" / "main" / "agent",
        home / ".openclaw" / "credentials",
    ]
    for d in dirs:
        proc = subprocess.run(
            ["sudo", c["mkdir"], "-p", str(d)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            _err(f"mkdir failed for {d}: {proc.stderr.strip()}")
            return False

    # Step 3: chown the tree to evo. Done in a single recursive sweep.
    # Idempotent — running again on already-owned tree is a no-op.
    # Group: staff is the macOS regular-user group; on Linux useradd gave
    # evo a same-named login group, so the bare user spec keeps it.
    owner = "evo:staff" if profile.name == "macos" else "evo"
    proc = subprocess.run(
        ["sudo", c["chown"], "-R", owner, str(home / ".openclaw")],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        _err(f"chown failed on {home / '.openclaw'}: {proc.stderr.strip()}")
        return False

    # Credentials dir gets mode 700 (no ACL) — matches the post-deploy
    # invariant set_evolve_read_acl enforces. Doing it here too so the
    # tree is in its final shape even before deploy.py has run on this bot.
    proc = subprocess.run(
        ["sudo", c["chmod"], "700", str(home / ".openclaw" / "credentials")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Not fatal — the deploy-time set_evolve_read_acl will re-enforce.
        _warn(
            f"chmod 700 on credentials dir failed (non-fatal): "
            f"{proc.stderr.strip()}"
        )

    # Step 4: grant the admin daemon (`evolve` user) ACL read access.
    # Uses the same helper deploy.py applies to every other bot. Idempotent.
    #
    # Note: set_evolve_read_acl looks up the bot's macOS user via network.json
    # via _bot_user_for; for a fresh "evo" not-yet-in-network.json that
    # falls back to "evo" — exactly what we want.
    try:
        from .deploy import set_evolve_read_acl
        set_evolve_read_acl("evo")
    except Exception as exc:
        # ACL setup is recoverable — the bot can be deployed without it
        # and set_evolve_read_acl is called again as part of deploy_bot.
        # Warn but don't fail the provisioning.
        _warn(
            f"set_evolve_read_acl('evo') failed (non-fatal — deploy will retry): "
            f"{type(exc).__name__}: {exc}"
        )

    _ok("Evo account provisioned (Phase E.2.a — empty, ready for E.2.b cutover)")
    return True


# ── OC per-user setup ─────────────────────────────────────────────────────────

def _oc_config_exists(bot_id: str) -> bool:
    # Setup wizard context: bot_id == macOS account name (account was just
    # created by _create_bot_account). For post-setup lookups of existing bots
    # use bot_home(bot_id) from evolve_admin.config instead.
    # bot_id IS the account name here — this is the account-creation path.
    return (user_home(bot_id) / ".openclaw" / "openclaw.json").exists()


def _setup_oc_for_bot(bot_id: str, port: int) -> bool:
    """
    Create the ~/.openclaw directory structure, a minimal openclaw.json, and
    a minimal auth-profiles.json (if not already present).
    Idempotent: skips the openclaw.json write if it already exists, but always
    ensures the agents/main/agent/ dir and auth-profiles.json are present.
    Returns True on success.
    """
    # bot_id IS the account name here — this is the account-creation path.
    # _create_bot_account() just created the account; user_home resolves it
    # via pwd (/Users/<bot> on macOS, /home/<bot> on Linux), falling back to
    # profile-keyed construction. Do not replace with bot_home() — the
    # account is being initialized, not looked up from an established
    # network.json (bot_id == OS account name on this path).
    from platform_profile import get_profile
    profile = get_profile()
    home = user_home(bot_id)
    dirs = [
        home / ".openclaw",
        home / ".openclaw" / "workspace",
        home / ".openclaw" / "workspace" / "evolve",
        home / ".openclaw" / "logs",
        home / ".openclaw" / "agents" / "main" / "agent",
        home / ".openclaw" / "credentials",
    ]
    for d in dirs:
        proc = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", str(d)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            _err(f"mkdir failed for {d}: {proc.stderr.strip()}")
            return False

    # Write openclaw.json — skip if already present
    dst = home / ".openclaw" / "openclaw.json"
    if dst.exists():
        _skip(f"OC config for '{bot_id}'")
    else:
        # Initial openclaw.json carries the permission-baseline fields so a
        # bot is monitor-clean from the moment the gateway first starts.
        # ensure_plugin_config (deploy.py) gap-fills any missing field on
        # every deploy, but the wizard runs the gateway before the first
        # deploy_bot — without these, the first audit pass between
        # gateway-start and deploy_bot fires perm_config_drift alerts that
        # noise the operator's first impression of the new pod.
        # Values match DEFAULT_BASELINE.pod_default.permission_config in
        # packages/analyzer/permissions/baseline.py.
        oc_json = {
            "agents": {
                "defaults": {
                    "workspace": str(home / ".openclaw" / "workspace"),
                }
            },
            "gateway": {
                "port": port,
            },
            "plugins": {
                "entries": {}
            },
            "tools": {
                # exec.security defaults to "full" for member bots (pivoted
                # 2026-05-25; see docs/spec-app-derived-permissions-2026-05-24.md).
                # A member bot runs as its own macOS user; "full" treats it
                # like a trusted agent rather than a hostile process. Bots
                # that operators want to harden land at "allowlist" via the
                # opt-in toggle (Phase C of the spec) seeded by the
                # manifest-derived preview the reconciler writes on deploy.
                # Setup wizard mirrors the deploy-time default so the very
                # first openclaw.json on disk doesn't need a corrective
                # rewrite on first deploy.
                "exec": {"security": "full", "ask": "on-miss"},
                "web": {
                    "search": {"enabled": True},
                    "fetch": {"enabled": True},
                },
            },
            "commands": {
                "native": "auto",
                "nativeSkills": "auto",
            },
            # Sandbox intentionally omitted — OC's config schema has no
            # top-level `sandbox` key (the valid path is
            # `agents.defaults.sandbox.mode`). Writing it at top level fails
            # gateway-startup config validation. See
            # packages/analyzer/permissions/baseline.py for the full rationale.
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(oc_json, tmp, indent=2)
            tmp_path = tmp.name
        proc = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(dst)],
            capture_output=True, text=True,
        )
        os.unlink(tmp_path)
        if proc.returncode != 0:
            _err(f"Failed to write openclaw.json for {bot_id}: {proc.stderr.strip()}")
            return False
        _ok(f"OC config for '{bot_id}' (port {port})")

    # Write auth-profiles.json — skip if already present and non-empty
    auth_dst = home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    auth_exists = auth_dst.exists()
    if not auth_exists:
        # Check via sudo in case the file is owned by the bot user
        probe = subprocess.run(
            ["sudo", "/bin/cat", str(auth_dst)],
            capture_output=True, text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            auth_exists = True

    if auth_exists:
        _skip(f"auth-profiles for '{bot_id}'")
    else:
        minimal_auth = {"version": 1, "profiles": {}, "lastGood": {}}
        with tempfile.NamedTemporaryFile(
            mode="w", dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json", delete=False
        ) as tmp:
            json.dump(minimal_auth, tmp, indent=2)
            tmp_path = tmp.name
        proc = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(auth_dst)],
            capture_output=True, text=True,
        )
        os.unlink(tmp_path)
        if proc.returncode != 0:
            _warn(f"Could not write auth-profiles.json for {bot_id}: {proc.stderr.strip()}")
        else:
            # 0600: auth-profiles.json carries API keys (matches the §5 grant —
            # there is no chmod-644 grant for it, so 644 here was a denied no-op).
            subprocess.run(
                ["sudo", "/bin/chmod", "600", str(auth_dst)],
                capture_output=True,
            )

    # Fix ownership. chown BINARY routes through the profile; `:staff` stays
    # literal (gid 50 on Ubuntu) per deploy.py's W7 primary-group rule.
    proc = subprocess.run(
        ["sudo", profile.chown, "-R", f"{bot_id}:staff", str(home / ".openclaw")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _warn(f"Could not fix ownership for {bot_id}: {proc.stderr.strip()}")

    # Credentials dir must be 700 — set before evolve ACL so set_evolve_read_acl()
    # can then strip the inherited ACL entry cleanly.
    subprocess.run(
        ["sudo", "/bin/chmod", "700", str(home / ".openclaw" / "credentials")],
        capture_output=True,
    )

    # Grant evolve ACL read access so the admin server can read all .openclaw/ files.
    # set_evolve_read_acl() also strips the evolve ACL from credentials/ specifically.
    try:
        set_evolve_read_acl(bot_id)
    except Exception as e:
        _warn(f"Could not set evolve read ACL for {bot_id}: {e}")

    return True


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _test_telegram_token(token: str) -> Optional[str]:
    """
    Test a Telegram bot token via the Bot API.
    Returns the bot username on success, None on failure.
    """
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return data["result"].get("username")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        pass
    return None


def _write_telegram_to_oc_config(bot_id: str, token: str) -> bool:
    """
    Merge the Telegram token into the bot's existing openclaw.json.
    Uses the shared `_apply_credential_to_oc_dict` registry so the path
    matches `_RUNTIME_MIRROR_PATH` (channels.telegram.botToken). Returns
    True on success.
    """
    # bot_id IS the account name here — this is the account-creation path.
    # The wizard creates accounts named after bot_id, so this path is correct
    # in setup_wizard context. Post-setup code must use bot_home(bot_id).
    oc_path = user_home(bot_id) / ".openclaw" / "openclaw.json"
    if not oc_path.exists():
        _err(f"openclaw.json not found for {bot_id}")
        return False

    # Read current config as root (we have sudo)
    proc = subprocess.run(
        ["sudo", "cat", str(oc_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _err(f"Cannot read openclaw.json for {bot_id}")
        return False

    try:
        cfg = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _err(f"Invalid JSON in openclaw.json for {bot_id}")
        return False

    channels = cfg.setdefault("channels", {})
    tg = channels.setdefault("telegram", {})
    tg["enabled"] = True
    # Drop the legacy "token" field if present (see _validate_bot_config).
    tg.pop("token", None)
    _apply_credential_to_oc_dict(cfg, "telegram", "bot_token", token)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(cfg, tmp, indent=2)
        tmp_path = tmp.name

    proc = subprocess.run(
        ["sudo", "/bin/cp", tmp_path, str(oc_path)],
        capture_output=True, text=True,
    )
    os.unlink(tmp_path)
    if proc.returncode != 0:
        _err(f"Failed to write openclaw.json for {bot_id}: {proc.stderr.strip()}")
        return False

    from platform_profile import get_profile
    subprocess.run(
        ["sudo", get_profile().chown, f"{bot_id}:staff", str(oc_path)],
        capture_output=True,
    )
    return True


# ── Idempotency helpers ───────────────────────────────────────────────────────

def _validate_bot_config(bot_name: str) -> list[str]:
    """Check bot's openclaw.json for known issues. Returns list of warnings."""
    issues = []
    try:
        # bot_name IS the account name here — this is the account-creation path.
        # Wizard creates accounts named bot_name; post-setup code must use bot_home().
        r = subprocess.run(
            ["sudo", "cat", str(user_home(bot_name) / ".openclaw" / "openclaw.json")],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return [f"Cannot read config for {bot_name}"]
        cfg = json.loads(r.stdout)
        tg = cfg.get("channels", {}).get("telegram", {})
        if "token" in tg:
            issues.append(
                f"{bot_name}: channels.telegram config has an outdated key format — Evolve will auto-fix this"
            )
    except Exception as e:
        issues.append(f"{bot_name}: config read error: {e}")
    return issues


def _plugin_already_installed(bot_name: str, port: int) -> bool:
    """Check if Evolve plugin is already live on this bot."""
    try:
        url = f"http://localhost:{port}/evolve/status"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
            return data.get("bot_id") == bot_name
    except Exception:
        return False


def _gateway_running(bot_name: str, port: int) -> bool:
    """Check if the bot's gateway is already responding."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2):
            return True
    except Exception:
        return False



# ── Platform gate (Linux port 8.3, design-linux-port-2026-06-10.md §9) ────────
#
# The wizard's ONE platform-detection site. Everything downstream selects
# behavior through the seams (platform_profile / get_isolation /
# get_scheduler / get_perms) — "gated" means the wizard refuses here, not
# that modules branch on sys.platform.

_LINUX_DESIGN_DOC = "docs/design-linux-port-2026-06-10.md"


def _activate_linux_platform() -> None:
    """Pin the LINUX profile and swap in the Linux seam adapters.

    Called exactly once, by :func:`_resolve_platform_gate`, once the host
    is detected as Linux. The perms backend is already
    profile-keyed (``get_perms``), so pinning the profile selects it;
    isolation and scheduler default to the macOS adapters and are swapped
    explicitly here.
    """
    from platform_profile import LINUX, set_profile

    from .runtime.isolation import LinuxUserIsolation, set_isolation
    from .runtime.scheduler import SystemdScheduler, set_scheduler

    set_profile(LINUX)
    set_isolation(LinuxUserIsolation())
    set_scheduler(SystemdScheduler())


def _resolve_platform_gate(
    platform_flag: "str | None" = None,
    *,
    host: "str | None" = None,
    env: "dict[str, str] | None" = None,
) -> str:
    """Decide whether the wizard may run on this host (design §9: detection
    stays, opt-in proceeds). Returns the profile name to run under
    (``"macos"`` / ``"linux"``); raises ``SystemExit(1)`` on refusal.

    - **darwin** proceeds exactly as today — no profile pinning, no adapter
      swap, zero behavior change. ``--platform linux`` on a Mac is refused
      loudly rather than silently ignored: the operator asked for a platform
      this host cannot be.
    - **linux** is GA — it auto-detects and proceeds with no opt-in (the W10
      port reached parity and a fresh install is proven clean end-to-end on a
      real VPS), pinning the LINUX profile and activating the Linux adapters
      (LinuxUserIsolation, SystemdScheduler). ``--platform linux`` /
      ``EVOLVE_PLATFORM=linux`` stay accepted as no-ops; ``--platform macos``
      on a Linux host is refused.
    - **anything else** (win32, …) hard-fails.

    ``host`` / ``env`` are test injection points only — production callers
    pass neither, so the gate reads the real ``sys.platform``.
    """
    actual_host = host if host is not None else sys.platform
    flag = (platform_flag or "").strip().lower()

    if actual_host == "darwin":
        if flag in ("", "auto", "macos", "darwin"):
            return "macos"
        _err(f"--platform {flag} is not valid on a macOS host.")
        _info("This machine is a Mac — run the wizard without --platform.")
        raise SystemExit(1)

    if actual_host.startswith("linux"):
        # Linux is a GA platform: the W10 port reached parity and a fresh
        # install is proven clean end-to-end on a real VPS. It auto-detects
        # like macOS — no EVOLVE_PLATFORM / --platform opt-in required. The
        # explicit forms (``--platform linux`` / ``EVOLVE_PLATFORM=linux``)
        # remain accepted as no-ops on a host that already is Linux; only an
        # explicit ``--platform macos`` is refused (this host cannot be a Mac).
        if flag in ("", "auto", "linux"):
            _activate_linux_platform()
            return "linux"
        _err(f"--platform {flag} is not valid on a Linux host.")
        _info("This machine runs Linux — run the wizard without --platform (or --platform linux).")
        raise SystemExit(1)

    _err(f"Unsupported platform '{actual_host}' — Evolve pods run on macOS "
         f"and Linux (see {_LINUX_DESIGN_DOC}).")
    raise SystemExit(1)


def _preflight_repo_root_traversable() -> None:
    """Linux preflight: refuse a repo source root the service user can't read.

    The source location is baked into two long-lived places: the venv editable
    ``.pth`` (so ``import analyzer`` resolves) and ~50 daemon ``ExecStart=`` /
    ``ANALYZER_DIR`` paths (deploy.py ``_REPO_ROOT``). If the source sits under
    a directory the ``evolve`` service user can't traverse — the live W10-E
    failure staged it under ``/root`` (mode 0o710, no o+x) — every daemon dies
    with ModuleNotFoundError / EACCES and the admin UI crash-loops, but only
    AFTER a long, half-finished install. Catch it up front instead.

    macOS is a no-op (``/Users/Shared/evolve-repo`` is always operator-readable).
    On Linux: when the ``evolve`` account already exists (re-run / repair) do the
    faithful test — can it actually read a deep source file (which requires
    traversing every ancestor); otherwise (true fresh install, account created
    later) fall back to a world-traverse (o+x) scan of every ancestor, which
    deterministically catches the ``/root`` case. Fail-closed with the canonical
    staging remediation. See docs/design-linux-port-2026-06-10.md §8.2.
    """
    from platform_profile import get_profile as _get_profile
    prof = _get_profile()
    if prof.name != "linux":
        return

    from .deploy import _REPO_ROOT
    repo_root = Path(_REPO_ROOT).resolve()
    canonical = prof.deploy_checkout_default
    remediation = (
        f"Stage the repo where the 'evolve' service user can read it — the "
        f"canonical location is {canonical} (NOT /root, whose 0o710 mode blocks "
        f"non-root traversal). Move the checkout there and re-run setup. See "
        f"docs/runbook-vps-pod-provision.md and design-linux-port-2026-06-10.md §8.2."
    )

    service_user = "evolve"
    if _user_exists(service_user):
        # Faithful: read a file deep in the tree as evolve. Success proves both
        # full ancestor traversal and read access — exactly what the .pth and
        # daemon ExecStart paths need.
        sentinel = repo_root / "packages" / "analyzer" / "platform_profile.py"
        r = subprocess.run(
            ["sudo", "-n", "-u", service_user, "/usr/bin/test", "-r", str(sentinel)],
            capture_output=True,
        )
        if r.returncode != 0:
            _err(f"Repo source root is not readable by the '{service_user}' user: {repo_root}")
            _info(f"  {remediation}")
            raise SystemExit(1)
        return

    # Fresh install: evolve doesn't exist yet. Require o+x on every ancestor so
    # the soon-to-be-created service + bot users can traverse into the source.
    blocking: list[str] = []
    p = repo_root
    while True:
        try:
            mode = p.stat().st_mode
        except OSError:
            break
        if not (mode & 0o001):  # other-execute (traverse) bit
            blocking.append(f"{p} (mode {oct(mode & 0o777)})")
        if p == p.parent:
            break
        p = p.parent
    if blocking:
        _err(f"Repo source root is not traversable by non-root users: {repo_root}")
        _info("  These ancestor directories block traversal (no o+x bit):")
        for b in blocking:
            _info(f"    {b}")
        _info(f"  {remediation}")
        raise SystemExit(1)


def _preflight_no_stale_users_tree() -> None:
    """Linux preflight: refuse a stale ``/Users`` tree on a Linux pod.

    ``/Users`` is the macOS home root; on Linux it is ALWAYS cruft (homes
    live under ``/home``). A leftover ``/Users/<bot>`` from an earlier round
    is not harmless: the wizard's key scan walks the home root, so a stale
    ``/Users/<bot>/.openclaw/.../auth-profiles.json`` gets offered as a "key
    found" source (round-3 bug B fed off exactly this — teardown removed
    ``/home/*`` but never ``/Users``). The home-root scans are now
    platform-keyed (they read ``/home`` on Linux), but a ``/Users`` tree
    still indicates an incompletely-torn-down box, so fail closed up front
    rather than install onto dirty state. macOS is a no-op (``/Users`` is
    the real home root there). See docs/runbook-vps-pod-provision.md.
    """
    from platform_profile import MACOS, get_profile as _get_profile
    if _get_profile().name != "linux":
        return
    # The macOS home root (/Users) sourced from the profile, not a literal —
    # its presence on a Linux box is the cruft signal.
    macos_root = MACOS.user_home_root  # "/Users" — from the profile, not a literal
    stale = Path(macos_root)
    if not stale.exists():
        return
    _err(f"A {macos_root} tree exists on this Linux host — always cruft from an "
         "incomplete teardown (macOS home root; Linux homes live under /home).")
    try:
        leftovers = sorted(p.name for p in stale.iterdir())
    except OSError:
        leftovers = []
    if leftovers:
        _info(f"  Contains: {', '.join(leftovers[:12])}"
              + (" …" if len(leftovers) > 12 else ""))
    _info(f"  A stale {macos_root}/<bot>/.openclaw can feed the key scan a "
          "leftover API key (round-3 bug B). Remove it and re-run:")
    _info(f"    sudo rm -rf {macos_root}")
    _info("  Full teardown set: docs/runbook-vps-pod-provision.md.")
    raise SystemExit(1)


# ── Prerequisites ─────────────────────────────────────────────────────────────

@dataclass
class Prereq:
    name: str
    ok: bool
    detail: str = ""
    hard: bool = False  # if True, abort on failure


# NodeSource is the blessed Node channel on Linux (design-linux-port §6:
# the node binary lands in /usr/bin, no Homebrew prefixes). Install needs
# judgment (piping a vendor script into bash), so the wizard
# detects-and-instructs — the same posture as Homebrew on macOS.
_LINUX_NODE_MIN = 24
_LINUX_NODE_HINT = (
    f"Install Node.js {_LINUX_NODE_MIN} via NodeSource: "
    f"curl -fsSL https://deb.nodesource.com/setup_{_LINUX_NODE_MIN}.x | sudo -E bash - "
    f"&& sudo apt-get install -y nodejs"
)


def _linux_release_prereq(os_release: "Path | None" = None) -> Prereq:
    """Soft host-OS row for the Linux path (the gate already opted in).

    Ubuntu 24.04 LTS is the tested target; Debian-family likely works but
    is unsupported (design-linux-port §10) — so this row warns, never
    aborts. Pure file read of /etc/os-release (no subprocess).
    """
    path = os_release if os_release is not None else Path("/etc/os-release")
    fields: dict = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip().strip('"')
    except OSError:
        return Prereq(
            "Linux (unknown distribution)", False,
            "Could not read /etc/os-release — Ubuntu 24.04 LTS is the tested target",
        )
    pretty = fields.get("PRETTY_NAME") or fields.get("NAME") or "Linux"
    dist_id = (fields.get("ID") or "").lower()
    version_id = fields.get("VERSION_ID") or ""
    try:
        ver_tuple = tuple(int(x) for x in version_id.split("."))
    except ValueError:
        ver_tuple = ()
    ubuntu_ok = dist_id == "ubuntu" and ver_tuple >= (24, 4)
    return Prereq(
        pretty, ubuntu_ok,
        "" if ubuntu_ok else "Ubuntu 24.04 LTS is the tested target — other "
        "distributions are best-effort (design-linux-port §10)",
    )


def _check_prerequisites(runner: "Optional[Callable[..., subprocess.CompletedProcess]]" = None) -> list[Prereq]:
    """Probe the host against the active platform profile's prereq set.

    Platform divergence comes from ``platform_profile.get_profile()`` (set
    by the gate) — never from ``sys.platform`` here. ``runner`` is the
    subprocess injection seam (a callable with ``subprocess.run``'s
    signature, same shape as the adapter runners); tests inject a fake so
    prereq probing never spawns a real process.
    """
    from platform_profile import get_profile

    profile = get_profile()
    macos = profile.name == "macos"
    run = runner or subprocess.run
    # Discovery PATH for binaries, profile-keyed: Homebrew prefixes are a
    # macOS-only concern; NodeSource node lands in /usr/bin on Linux.
    bin_path = (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin" if macos
        else "/usr/local/bin:/usr/bin:/bin"
    )
    results: list[Prereq] = []

    # Running as root
    is_root = os.geteuid() == 0
    results.append(Prereq(
        "Root/sudo privileges", is_root,
        "" if is_root else "Re-run with: sudo evolve-admin setup --fresh",
        hard=True,
    ))

    # Host OS
    if macos:
        ver_str = platform.mac_ver()[0]  # e.g. "14.4.1"
        try:
            major = int(ver_str.split(".")[0])
            mac_ok = major >= 14
        except (ValueError, IndexError):
            mac_ok = False
        results.append(Prereq(
            f"macOS {ver_str}", mac_ok,
            "" if mac_ok else "macOS 14 (Sonoma) or later recommended",
        ))
    else:
        results.append(_linux_release_prereq())

    # CPU architecture — Apple Silicon recommended, Intel best-effort.
    # Soft (ok=False renders the yellow ⚠ row, never aborts): macOS 26
    # (Tahoe) is the final Intel macOS release, so Intel hosts work today
    # but get no new platform effort. (No Linux analogue: x86_64 and ARM64
    # Ubuntu are both fine per design §10.)
    if macos:
        from . import host_power
        arch = platform.machine()
        if host_power.is_apple_silicon():
            results.append(Prereq(f"Apple Silicon ({arch})", True))
        else:
            results.append(Prereq(
                f"CPU architecture ({arch})", False,
                "Intel Mac — supported best-effort (macOS 26 is the final "
                "Intel release); Apple Silicon recommended",
            ))

    # Python 3.10+ (matches requires-python in both packages' pyproject.toml;
    # macOS system /usr/bin/python3 is 3.9.6, so a Homebrew python is required)
    major, minor = sys.version_info[:2]
    py_ok = (major, minor) >= (3, 10)
    results.append(Prereq(
        f"Python {major}.{minor}", py_ok,
        "" if py_ok else (
            "Python 3.10+ required — on macOS: brew install python@3.12"
            if macos else "Python 3.10+ required"
        ),
        hard=True,
    ))

    # Node.js — 20+ on macOS (Homebrew), 24+ on Linux (NodeSource).
    node_min = 20 if macos else _LINUX_NODE_MIN
    node_hint = (
        "Install Node.js 20+: brew install node" if macos else _LINUX_NODE_HINT
    )
    node = shutil.which("node") or shutil.which("node", path=bin_path)
    node_ok = False
    node_ver = "not found"
    if node:
        proc = run([node, "--version"], capture_output=True, text=True)
        node_ver = proc.stdout.strip()  # e.g. "v20.11.0"
        try:
            node_major = int(node_ver.lstrip("v").split(".")[0])
            node_ok = node_major >= node_min
        except (ValueError, IndexError):
            node_ok = False
    results.append(Prereq(
        f"Node.js {node_ver}", node_ok,
        node if node_ok else node_hint,
    ))

    # npm
    npm = shutil.which("npm") or shutil.which("npm", path=bin_path)
    results.append(Prereq(
        "npm", npm is not None,
        npm or "Install npm (comes with Node.js)",
    ))

    if macos:
        # Homebrew (optional)
        brew = shutil.which("brew") or shutil.which("brew", path="/opt/homebrew/bin:/usr/local/bin")
        results.append(Prereq(
            "Homebrew", brew is not None,
            brew or "Optional: https://brew.sh",
        ))
    else:
        # POSIX ACL tools — the perms adapter (runtime/perms.py) is
        # setfacl/getfacl on Linux; without them every ACL contract
        # (.openclaw/ reads, workspace writes, evo store access) is dead.
        # Detect-and-instruct, same posture as Homebrew above.
        c = profile.commands
        acl_ok = Path(c["setfacl"]).exists() and Path(c["getfacl"]).exists()
        results.append(Prereq(
            "POSIX ACL tools (setfacl/getfacl)", acl_ok,
            "" if acl_ok else "Install ACL tools: sudo apt-get install -y acl",
        ))
        # systemd — the scheduler adapter is systemctl; a non-systemd host
        # cannot run any Evolve job (design-linux-port §3).
        results.append(Prereq(
            "systemd (systemctl)", Path(profile.service_manager).exists(),
            "" if Path(profile.service_manager).exists()
            else f"{profile.service_manager} not found — systemd is required",
            hard=True,
        ))

    # OpenClaw CLI
    oc = shutil.which("openclaw") or shutil.which("openclaw", path=bin_path)
    results.append(Prereq(
        "OpenClaw CLI", oc is not None,
        oc or "Not installed — will install via npm",
    ))

    # Service-definition dir writable (need root) —
    # /Library/LaunchDaemons vs /etc/systemd/system.
    daemon_dir = profile.daemon_dir
    ld_ok = Path(daemon_dir).exists() and os.access(daemon_dir, os.W_OK)
    results.append(Prereq(
        f"{daemon_dir} writable", ld_ok,
        "" if ld_ok else "Need root — run with sudo",
        hard=True,
    ))

    # Venv Python + required packages (soft — wizard will install what's missing)
    from .deploy import VENV_PYTHON
    from .health import _VENV_REQUIRED_PACKAGES, _VENV_PIP
    # VENV_PYTHON is the macOS interpreter contract; the Linux venv follows
    # the same "<shared_dir> sibling" convention pre-parity (the deploy
    # wave owns the final location).
    venv_python = (
        VENV_PYTHON if macos
        else f"{profile.shared_dir_default}-venv/bin/python3"
    )
    venv_py = Path(venv_python)
    # Resolve to actual versioned binary if the generic symlink doesn't exist yet
    if not venv_py.exists():
        candidates = sorted(venv_py.parent.glob("python3*")) if venv_py.parent.exists() else []
        venv_py = candidates[0] if candidates else venv_py
    if not venv_py.exists():
        results.append(Prereq(
            "Evolve venv", False,
            f"Venv Python not found at {venv_python} — venv will be created during setup",
        ))
    else:
        results.append(Prereq("Evolve venv", True, str(venv_py)))
        for import_name, pip_name in _VENV_REQUIRED_PACKAGES:
            r = run(
                [str(venv_py), "-c", f"import {import_name}"],
                capture_output=True, text=True,
            )
            pkg_ok = r.returncode == 0
            fix_hint = f"sudo {_VENV_PIP} install {pip_name}" if not pkg_ok else ""
            results.append(Prereq(
                f"venv: {pip_name}", pkg_ok,
                fix_hint,
            ))

    return results


# ── Host power posture + dedicated-host acknowledgment (Phase 8.2) ────────────
#
# Evolve supports any *dedicated, always-on* Mac — the chassis is irrelevant
# (a retired MacBook is exactly as isolated as a mini); "dedicated" is a usage
# property and the informed operator's choice. These two steps explain and
# ask. They never hard-block (docs/research-platform-expansion-2026-06-10.md
# §4a).


def _run_power_posture_step(non_interactive: bool) -> dict:
    """Detect battery/portable hardware and require never-sleep-on-AC.

    launchd StartInterval jobs don't fire during sleep (they coalesce on
    wake) and KeepAlive gateways are suspended — a sleeping host silently
    takes every daemon and bot dark. Offers `pmset -c sleep 0 displaysleep 0`
    when the AC profile allows system sleep; warns (does not block) when
    the operator declines. Returns the posture dict recorded under
    network.json `host`.

    On a host that cannot sleep (a headless Linux VPS), the
    :class:`~evolve_admin.host_power.HostPower` adapter reports
    ``manages_sleep() is False`` — there are no sleep targets to manage and
    no pmset analogue — so the step records the always-on posture and skips
    the offer entirely (Linux port L3 / W1, design §1).
    """
    from . import host_power

    hp = host_power.get_host_power()
    if not hp.manages_sleep():
        posture = hp.power_posture()
        _skip("Always-on host — no sleep management needed")
        return posture

    posture = host_power.power_posture()
    model = posture["hardware_model"] or "unknown model"
    if posture["portable"]:
        _info(f"Hardware: [bold]{model}[/] — portable (battery present)")
    else:
        _info(f"Hardware: [bold]{model}[/]")

    _info("Evolve's daemons assume an always-on host: scheduled jobs don't fire")
    _info("during sleep, and bot gateways are suspended — a sleeping Mac takes")
    _info("the whole pod dark, silently.")
    console.print()

    ac_sleep = posture["ac_sleep"]
    if posture["sleep_disabled"]:
        _ok("Sleep is disabled system-wide (pmset disablesleep 1)")
    elif ac_sleep == 0:
        _ok("System sleep on AC power: never — daemons keep running")
    elif ac_sleep is None:
        _warn("Could not read the power profile (pmset) — verify manually:")
        _info("  sudo pmset -c sleep 0 displaysleep 0")
    else:
        _warn(f"This Mac is set to sleep after {ac_sleep} min on AC power.")
        if _confirm(
            "Set never-sleep on AC power now (pmset -c sleep 0 displaysleep 0)?",
            default=True, non_interactive=non_interactive,
        ):
            ok, err = host_power.set_never_sleep_on_ac()
            if ok:
                refreshed = host_power.ac_power_settings()
                posture["ac_sleep"] = refreshed.get("sleep")
                posture["ac_displaysleep"] = refreshed.get("displaysleep")
                _ok("AC power profile updated: system sleep off, display sleep off")
            else:
                _warn(f"pmset failed: {err}")
                _info("Set it manually: sudo pmset -c sleep 0 displaysleep 0")
        else:
            _warn("Leaving sleep enabled — scheduled jobs and bots WILL go dark")
            _warn("whenever this Mac sleeps. Set it later with:")
            _info("  sudo pmset -c sleep 0 displaysleep 0")

    if posture["portable"]:
        console.print()
        _info("Portable-host notes:")
        _info("  • Keep it on AC power — on battery, the battery profile still")
        _info("    applies and the machine will sleep normally.")
        _info("  • Closing the lid sleeps the Mac unless it's on AC power with an")
        _info("    external display attached, or sleep is disabled outright:")
        _info("    sudo pmset disablesleep 1   (also prevents sleep on battery —")
        _info("    the lid-closed-on-a-shelf configuration)")
        if not posture["on_ac_power"]:
            _warn("Currently running on battery power — plug this Mac in.")

    return posture


def _run_dedication_ack_step(
    non_interactive: bool,
    existing_host: dict,
    admin_user: str,
) -> dict:
    """Informed-operator dedication acknowledgment. Records, never blocks.

    The single-tenant assumption (threat-model.md §2) is load-bearing for
    four controls; this step makes the operator's "this host is dedicated"
    choice explicit and durable instead of implicit in the chassis name.
    A previously recorded acknowledgment is reused so repair re-runs don't
    re-ask.

    The threat-model assumption is platform-neutral; only the operator copy
    differs (Linux port L3 / W1). On macOS the framing is the dedicated-Mac
    chassis ("a retired MacBook is as isolated as a mini"); on a Linux VPS
    the operator is remote, so the framing is the SSH-operator model from
    design §1 — the admin UI stays bound to 127.0.0.1 and "anyone who can
    SSH in is the operator", which is the same single-tenant assumption
    restated. The drift failure mode shifts accordingly: personal-use creep
    on macOS, SSH-key sprawl on a VPS.
    """
    prior = (existing_host or {}).get("dedication_ack") or {}
    if prior.get("acknowledged"):
        who = prior.get("acknowledged_by", "operator")
        when = (prior.get("acknowledged_at") or "")[:10]
        _skip(f"Dedicated-host acknowledgment already recorded ({who}, {when})")
        return prior

    from platform_profile import get_profile
    if get_profile().name == "macos":
        panel_body = (
            "Evolve assumes this Mac is [bold]dedicated[/] to the pod — it is a\n"
            "single-tenant system. Four security controls depend on no other\n"
            "untrusted local users or day-to-day workloads existing here:\n\n"
            "  • sudoers grants    — wildcard copy rules reachable from /tmp\n"
            "  • /tmp staging      — bot configs stage through world-writable /tmp\n"
            "  • keystore perms    — signing keys protected by file modes, not encryption\n"
            "  • loopback as authz — anyone reaching 127.0.0.1:5050 is 'the operator'\n\n"
            "The [bold]chassis doesn't matter[/] — a retired MacBook on a shelf is\n"
            "exactly as isolated as a Mac mini. 'Dedicated' is how the machine is\n"
            "[italic]used[/], and that's your call to make and keep.\n\n"
            "The common failure mode is [bold]drift[/]: a 'retired' laptop quietly\n"
            "returning to daily personal use months later. If this Mac ever picks\n"
            "up other users or workloads, re-home the pod first.\n\n"
            "[dim]Full detail: docs/threat-model.md §2[/]"
        )
        confirm_q = (
            "Will this Mac stay dedicated to Evolve "
            "(no other day-to-day users or workloads)?"
        )
    else:
        panel_body = (
            "Evolve assumes this host is [bold]dedicated[/] to the pod — it is a\n"
            "single-tenant system. Four security controls depend on no other\n"
            "untrusted local users or day-to-day workloads existing here:\n\n"
            "  • sudoers grants    — wildcard copy rules reachable from /tmp\n"
            "  • /tmp staging      — bot configs stage through world-writable /tmp\n"
            "  • keystore perms    — signing keys protected by file modes, not encryption\n"
            "  • loopback as authz — anyone reaching 127.0.0.1:5050 is 'the operator'\n\n"
            "On a VPS the operator is [bold]remote[/]: the admin UI stays bound to\n"
            "127.0.0.1 and you reach it over an SSH tunnel\n"
            "([italic]ssh -L 5050:127.0.0.1:5050 <host>[/]). So [bold]anyone who can\n"
            "SSH in is the operator[/] — that is the single-tenant assumption\n"
            "restated, not a new one. Keep SSH access to this box as tight as\n"
            "the operator role itself.\n\n"
            "The common failure mode is [bold]drift[/]: SSH-key sprawl — a second\n"
            "person gaining shell access to a box quietly treated as shared. If\n"
            "this host ever picks up other users or operators, re-home the pod\n"
            "first.\n\n"
            "[dim]Full detail: docs/threat-model.md §2 · "
            "docs/design-linux-port-2026-06-10.md §1[/]"
        )
        confirm_q = (
            "Will this host stay dedicated to Evolve "
            "(no other day-to-day users or SSH operators)?"
        )

    console.print(Panel.fit(panel_body, title="Dedicated host"))

    acknowledged = _confirm(
        confirm_q,
        default=True, non_interactive=non_interactive,
    )
    if acknowledged:
        _ok("Dedicated-host acknowledgment recorded")
    else:
        _warn("Continuing anyway — on a shared machine the four controls above")
        _warn("are weakened and the deployment is outside the supported threat")
        _warn("model. See docs/threat-model.md §2.")

    from datetime import datetime, timezone
    return {
        "acknowledged": acknowledged,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "acknowledged_by": admin_user,
        "mode": "non-interactive" if non_interactive else "interactive",
    }


# ── OpenClaw global install ───────────────────────────────────────────────────

def _install_openclaw_npm(runner: "Optional[Callable[..., subprocess.CompletedProcess]]" = None) -> bool:
    """
    Install OpenClaw globally via npm. Returns True on success.

    Profile-keyed (never ``sys.platform``): the ``--prefix`` override is a
    Homebrew-layout concern that must NOT render on Linux — NodeSource npm's
    default global prefix (/usr) is exactly where the Linux sudoers grants
    and unit files resolve openclaw from. ``runner`` is the subprocess
    injection seam for tests.
    """
    from platform_profile import get_profile

    profile = get_profile()
    macos = profile.name == "macos"
    run = runner or subprocess.run
    bin_path = (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin" if macos
        else "/usr/local/bin:/usr/bin:/bin"
    )

    npm = shutil.which("npm") or shutil.which("npm", path=bin_path)
    if not npm:
        _err("npm not found — cannot install OpenClaw automatically")
        _info("Install Node.js first: "
              + ("brew install node" if macos else _LINUX_NODE_HINT))
        return False

    if macos:
        # Force --prefix to the brew-canonical prefix so the install lands at
        # the path the gateway plists and sudoers grants resolve from. npm's
        # default prefix is inside <brew>/Cellar/node/<ver>, which would
        # silently orphan the install. The prefix is /opt/homebrew on Apple
        # Silicon and /usr/local on Intel — hardcoding the former broke fresh
        # Intel installs (Phase 8.2 fix).
        from .host_power import homebrew_prefix
        brew_prefix = homebrew_prefix()
        argv = [npm, "install", "-g", f"--prefix={brew_prefix}", "openclaw"]
        manual_hint = f"sudo npm install -g --prefix={brew_prefix} openclaw"
    else:
        # NodeSource npm installs to its default global prefix — no
        # --prefix flag, no Homebrew paths (design-linux-port §6).
        argv = [npm, "install", "-g", "openclaw"]
        manual_hint = "sudo npm install -g openclaw"

    # The macOS bin_path lacks /bin (Homebrew-first discovery order), so the
    # spawned PATH appends it; the Linux bin_path already ends in /bin.
    env_path = (f"{bin_path}:/bin:" if macos else f"{bin_path}:") + os.environ.get("PATH", "")
    with console.status("  Installing OpenClaw via npm..."):
        proc = run(
            argv,
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": env_path},
        )

    if proc.returncode != 0:
        _err(f"npm install failed:\n{proc.stderr.strip()[:400]}")
        _info(f"Try manually: {manual_hint}")
        return False

    # Verify it's now in PATH
    oc = shutil.which("openclaw") or shutil.which("openclaw", path=bin_path)
    if not oc:
        _warn("openclaw installed but not found in PATH — you may need to restart your shell")
    else:
        _ok(f"OpenClaw installed at {oc}")
    return True


# ── Evolve deploy pipeline ────────────────────────────────────────────────────

def _fix_venv_ownership() -> None:
    """Ensure the shared venv is owned by root:wheel.

    The venv may have been created as the admin user before initial setup,
    or a subprocess may have touched files as that user.  Enforce root
    ownership here so the health check doesn't flag it every run.
    """
    from platform_profile import get_profile
    profile = get_profile()
    venv = Path(profile.venv_dir)
    if venv.exists():
        # sudo-grant: root-only — a setup-wizard step ("Fixing venv ownership");
        # the venv is created and re-owned during interactive setup, run as root.
        # chown binary + admin group route through the profile: root:wheel on
        # macOS, root:root on Linux (`wheel` is not gid 0 on Ubuntu).
        subprocess.run(
            ["sudo", profile.chown, "-R", f"root:{profile.admin_group}", str(venv)],
            capture_output=True, check=False,
        )


def _run_deploy_step(label: str, fn, *args, **kwargs) -> bool:
    """Run a deploy step function with spinner output. Returns True on success."""
    with console.status(f"  {label}..."):
        try:
            fn(*args, **kwargs)
            return True
        except Exception as e:
            _err(f"{label}: {e}")
            return False


def _deploy_evolve(bots: list[BotSpec], network: dict, net_path: Path) -> list[str]:
    """
    Run the full Evolve deploy pipeline across all bots.
    Returns list of error strings (empty = success).
    """
    from .deploy import (
        reinstall_evolve_admin, build_plugin, fix_plugin_permissions,
        ensure_plugin_config, install_oc_plugin, fix_shared_dir_permissions,
        deploy_bot, restart_gateway, verify_plugin_live,
        deploy_shared_dir,
    )
    from .ocadmin import _remove_conflicting_user_agents

    errors: list[str] = []
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))

    # One-time steps (build once for all bots)
    console.print("  [dim]Building plugin + fixing permissions...[/]")
    for label, fn, args in [
        ("Reinstalling evolve-admin", reinstall_evolve_admin, ()),
        ("Building plugin", build_plugin, ()),
        ("Fixing plugin permissions", fix_plugin_permissions, ()),
        ("Fixing venv ownership", _fix_venv_ownership, ()),
    ]:
        with console.status(f"  {label}..."):
            try:
                fn(*args)
            except Exception as e:
                errors.append(f"{label}: {e}")
                _err(f"{label}: {e}")
                return errors  # plugin build failure is fatal

    # Shared directory
    with console.status("  Setting up shared directory..."):
        try:
            r = deploy_shared_dir(shared_dir, dry_run=False)
            for s in r.steps:
                pass  # swallow verbose steps
            if r.errors:
                for e in r.errors:
                    errors.append(e)
        except Exception as e:
            errors.append(f"Shared directory: {e}")

    # Pre-loop: remove any conflicting user-level LaunchAgents BEFORE touching
    # any gateways.  npm's post-install drops ai.openclaw.gateway.plist into
    # ~/Library/LaunchAgents/ on install/upgrade; if it's loaded when we
    # kickstart the system daemon both processes race for the port.
    console.print("\n  Checking for conflicting user-level LaunchAgents…")
    try:
        _remove_conflicting_user_agents(network)
    except Exception as e:
        _warn(f"Pre-deploy conflicting agent cleanup: {e}")

    # Per-bot steps
    for bot in bots:
        console.print(f"\n  [bold]→ {bot.name}[/] (role={bot.role})")

        # Idempotent plugin install
        _install_ok = False
        if _plugin_already_installed(bot.name, bot.port):
            _skip(f"{bot.name} plugin already installed")
        else:
            # openclaw plugins install communicates with a running gateway — ensure it's up first.
            if not _gateway_running(bot.name, bot.port):
                with console.status(f"  Starting gateway for {bot.name} before plugin install..."):
                    try:
                        restart_gateway(bot.name, bot_user=bot.effective_user)
                        for _ in range(10):
                            time.sleep(2)
                            if _gateway_running(bot.name, bot.port):
                                break
                        else:
                            _warn(f"Gateway for {bot.name} did not respond after start — plugin install may fail")
                    except Exception as e:
                        _warn(f"Could not start gateway for {bot.name}: {e}")
            try:
                with console.status(f"  Installing plugin on {bot.name}..."):
                    ensure_plugin_config(bot.name, network)
                    install_oc_plugin(bot.name, port=bot.port, network=network)
                _ok(f"Plugin installed on {bot.name}")
                _install_ok = True
            except Exception as first_err:
                _warn(f"Plugin install on {bot.name} hit an issue — attempting auto-fix...")
                err_text = str(first_err)
                if "Config invalid" in err_text or "Unrecognized key" in err_text:
                    _info(f"   Issue: {bot.name} config has an incompatible setting.")
                else:
                    _info(f"   Issue: {err_text[:400]}")
                _info(f"   Running config repair on {bot.name}...")
                try:
                    _bot_home = user_home(bot.name)
                    subprocess.run(
                        ["sudo", "-u", bot.name, "env", f"HOME={_bot_home}",
                         "openclaw", "doctor", "--fix"],
                        cwd=str(_bot_home), capture_output=True, timeout=15,
                    )
                    # Re-validate to confirm fix actually worked
                    remaining_issues = _validate_bot_config(bot.name)
                    if remaining_issues:
                        for issue in remaining_issues:
                            _warn(f"   Still present after repair: {issue}")
                    else:
                        _ok(f"Config repaired on {bot.name}")
                    with console.status(f"  Retrying plugin install on {bot.name}..."):
                        ensure_plugin_config(bot.name, network)
                        install_oc_plugin(bot.name, port=bot.port, network=network)
                    _ok(f"Plugin installed on {bot.name}")
                    _install_ok = True
                except Exception as retry_err:
                    _err(f"Plugin install on {bot.name} could not be completed automatically.")
                    _info(f"   Retry error: {str(retry_err)[:400]}")
                    _info("")
                    _hint_home = user_home(bot.name)
                    _info(f"   To fix manually, run these commands in your terminal:")
                    _info(f"   [bold]sudo -u {bot.name} env HOME={_hint_home} sh -c 'cd {_hint_home} && openclaw doctor --fix'[/]")
                    _info(f"   [bold]sudo -u {bot.name} env HOME={_hint_home} sh -c 'cd {_hint_home} && openclaw plugins install -l {PLUGIN_INSTALL_DIR}'[/]")
                    _info("")
                    _info(f"   Then re-run: [bold]sudo evolve-admin setup --fresh[/] to complete setup.")
                    _info(f"   Continuing with remaining bots...")
                    errors.append(f"Plugin install on {bot.name}: manual fix required")
                    continue

        with console.status(f"  Fixing shared dir perms for {bot.name}..."):
            try:
                fix_shared_dir_permissions(bot.name, shared_dir)
            except Exception as e:
                _warn(f"Shared dir perms for {bot.name}: {e}")

        # Install brave ONLY when it's a pod invariant. Brave was demoted to an
        # optional integration (2026-06-24): web search is optional, especially
        # for the evo bot, and force-installing it on every bring-up paired
        # badly with the dashboard showing a perpetual "Setup required" row.
        # The solicited/installed set now derives from
        # ``podInvariantIntegrations`` (default ["github"]) so the wizard and
        # the dashboard invariant set can't drift — re-add brave to that list
        # to restore the auto-install. Externalized npm package, per-bot. Run
        # after the evolve plugin so the install record exists before
        # deploy_bot's ensure_plugin_config writes the plugins.entries.brave
        # entry. Best-effort: an install failure surfaces as a plugin_missing
        # alert on the next monitor pass, not a setup-failing error. (deploy.py
        # still runs an idempotent brave gap-fill for bots whose openclaw.json
        # already declares the entry, so existing brave users keep working.)
        bot_account = bot.effective_user
        _invariants = {
            str(x).strip().lower()
            for x in (network.get("podInvariantIntegrations") or [])
            if str(x).strip()
        }
        if "brave" in _invariants:
            with console.status(f"  Installing brave plugin on {bot.name}..."):
                try:
                    from .oc_neutralize import install_externalized_plugin
                    ok_brave, err_brave = install_externalized_plugin(
                        bot_account, "@openclaw/brave-plugin", force=True,
                    )
                    if ok_brave:
                        _ok(f"brave plugin installed on {bot.name}")
                    else:
                        _warn(f"brave plugin install on {bot.name}: {err_brave}")
                except Exception as e:
                    _warn(f"brave plugin install on {bot.name}: {e}")

        with console.status(f"  Registering {bot.name} with Evolve..."):
            try:
                bot_entry = (network.get("bots") or {}).get(bot.name) or {}
                bot_role = (bot_entry.get("role") if isinstance(bot_entry, dict) else None) or "member"
                r = deploy_bot(bot.name, bot_role, bot.port, net_path, dry_run=False)
                if r.errors:
                    for e in r.errors:
                        _warn(f"  {e}")
                _ok(f"{bot.name} registered in Evolve network")
            except Exception as e:
                msg = f"Cron jobs for {bot.name}: {e}"
                errors.append(msg)
                _err(msg)

        # Start or restart the gateway.  Always restart when the plugin was
        # just installed — the running process needs to reload the new plugin.
        if _install_ok:
            with console.status(f"  Restarting gateway for {bot.name} (plugin updated)..."):
                try:
                    restart_gateway(bot.name, bot_user=bot.effective_user)
                    _ok(f"Gateway restarted for {bot.name}")
                except Exception as e:
                    _warn(f"Gateway restart for {bot.name}: {e} (may start via launchd)")
        elif _gateway_running(bot.name, bot.port):
            _skip(f"{bot.name} gateway already running")
        else:
            with console.status(f"  Starting gateway for {bot.name}..."):
                try:
                    restart_gateway(bot.name, bot_user=bot.effective_user)
                    _ok(f"Gateway started for {bot.name}")
                except Exception as e:
                    _warn(f"Gateway start for {bot.name}: {e} (may start via launchd)")

    # Post-install safety net: plugin install (above) runs openclaw CLI as the
    # bot user, which can re-trigger npm's post-install and drop a fresh
    # ai.openclaw.gateway.plist.  Run cleanup again to catch anything new.
    console.print("\n  Post-install check for conflicting user-level LaunchAgents…")
    try:
        _remove_conflicting_user_agents(network)
    except Exception as e:
        _warn(f"Post-install conflicting agent cleanup: {e}")

    return errors


# ── Security config step ──────────────────────────────────────────────────────

def _run_security_config_step(
    non_interactive: bool,
    existing: dict,
) -> str:
    """
    Security Configuration step.

    Asks for evolve's workspace git remote URL so nightly backups have a
    remote to push to. Per-bot backup remotes are set at deploy time.

    The HMAC signing key is generated automatically (no user input needed).
    SSH deploy keys per bot are generated during bot provisioning (deploy step).

    Returns evolve's backup repo URL (may be empty string if skipped).
    """
    console.print()
    console.print(
        "  [bold]Security Configuration[/]\n"
        "  Evolve uses Security Protocol v2 — HMAC proposal signing, nightly git\n"
        "  backup, and 15-minute audit checks, all running as the evolve user.\n"
    )
    _info("  Each bot backs up its workspace to its own git remote (set at deploy time).")
    _info("  Here you can configure the git remote for the [bold]evolve[/] user's own workspace.\n")
    _info("  Example: git@github.com:yourorg/evolve-workspace.git")
    _info("  Leave blank to skip — add 'bots.evolve.backupRepoUrl' to network.json later.\n")

    # Look up the existing primary bot's backup URL. We don't know which bot
    # the operator will pick this run, but if a prior install set one we can
    # surface it as the default. Resolve via primary_bot_id; fall back to
    # "evolve" / "evo" for the dedicated-default case.
    existing_bots = existing.get("bots") or {}
    primary_candidates = [existing.get("primary"), "evo", "evolve"]
    existing_primary_cfg: dict = {}
    for cand in primary_candidates:
        if isinstance(cand, str) and cand and cand in existing_bots:
            existing_primary_cfg = existing_bots.get(cand) or {}
            if existing_primary_cfg:
                break
    existing_url = existing_primary_cfg.get("backupRepoUrl", "")

    # If network.json doesn't have it, try to discover from the live git config
    if not existing_url:
        try:
            r = subprocess.run(  # sudo-grant: root-only — interactive setup wizard runs as operator root, dropping TO evolve
                ["sudo", "-u", "evolve", "git", "-C",
                 "/Users/evolve/.openclaw/workspace/evolve",
                 "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                existing_url = r.stdout.strip()
        except Exception:
            pass

    if non_interactive:
        evolve_backup_url = existing_url
    else:
        evolve_backup_url = _ask(
            "evolve workspace git remote URL (leave blank to skip)",
            existing_url,
            non_interactive,
        ).strip()

    if evolve_backup_url:
        _ok(f"evolve backup remote: {evolve_backup_url}")
    else:
        _warn("evolve backup remote skipped — add 'bots.evolve.backupRepoUrl' to network.json later")

    return evolve_backup_url


# ── HTTPS / Tailscale-serve setup step ────────────────────────────────────────

def _run_https_phase(net_path: Path, non_interactive: bool = False) -> None:
    """Wizard step: try to flip the pod to Tailscale-served HTTPS.

    Sub-spec: ``docs/spec-pwa-phase0-https-2026-05-18.md`` §3.4 + §5.4.
    Wraps :func:`evolve_admin.https_setup.enable_https_if_possible`,
    which never raises — every outcome maps to a single operator-facing
    line per the brief's table:

    * Skipped (no Tailscale / not signed in / version too old) → friendly
      "skipped, run enable-https later" note.
    * Skipped (admin-console HTTPS-cert toggle off) → print the one-time
      tailnet setup instructions, then a "re-run after flipping" note.
    * Succeeded → "HTTPS enabled at <url>".
    * Failed mid-flow → reason + retry hint (network.json was already
      rolled back inside enable_https).

    Non-interactive mode runs the same flow — the underlying helpers
    only consult Tailscale state, not the operator.
    """
    from .https_setup import enable_https_if_possible, PreflightResult

    try:
        attempt = enable_https_if_possible(network_path=net_path)
    except Exception as exc:
        # enable_https_if_possible() is documented as non-raising, but
        # any escape (importer error, OSError on save) still leaves the
        # pod on HTTP — degrade gracefully instead of aborting setup.
        _warn(
            f"HTTPS setup hit an unexpected error: {exc}. "
            "Pod remains on HTTP; you can retry with 'sudo evolve-admin enable-https'."
        )
        return

    pf = attempt.preflight
    if pf is PreflightResult.READY and attempt.result is not None:
        # ── Succeeded ─────────────────────────────────────────────────
        result = attempt.result
        # Surface anything the underlying helper wanted to say
        # (e.g. App-Store PATH symlink hint); these are operator-facing.
        for msg in result.messages:
            if msg.startswith("Note:"):
                _info(msg)
        if result.changed:
            _ok(f"HTTPS enabled at {result.url}")
        else:
            _skip(f"HTTPS enabled at {result.url}")
        return

    if pf is PreflightResult.NEED_TOGGLE:
        # ── Deferred — admin-console HTTPS-cert toggle is off ─────────
        _info(
            "[bold]One-time Tailscale setup needed before HTTPS will work:[/]"
        )
        _info("  1. Open https://login.tailscale.com/admin/dns")
        _info('  2. Scroll to "HTTPS Certificates" and click "Enable HTTPS"')
        _info("  3. After that, re-run: [bold]sudo evolve-admin enable-https[/]")
        _warn(
            "HTTPS skipped — enable the Tailscale admin-console toggle "
            "(see above), then re-run."
        )
        return

    if pf is PreflightResult.READY:
        # ── Preflight passed but apply failed mid-flow ────────────────
        # network.json is back to HTTP; serve is cleared. Surface the
        # reason so the operator can either diagnose or retry.
        reason = attempt.error or "unknown error"
        _warn(
            f"HTTPS setup failed: {reason}. "
            "Pod remains on HTTP; you can retry with "
            "'sudo evolve-admin enable-https'."
        )
        return

    # ── NEED_INSTALL / NEED_LOGIN / NEED_UPGRADE ─────────────────────
    # All three lead to the same operator action: get Tailscale into a
    # usable state, then re-run enable-https. The line is per-cause so
    # the operator knows what to fix.
    cause_line = {
        PreflightResult.NEED_INSTALL: "Tailscale not installed",
        PreflightResult.NEED_LOGIN: "Tailscale not signed in",
        PreflightResult.NEED_UPGRADE: "Tailscale too old (need v1.44+)",
    }.get(pf, "Tailscale not ready")
    _info(
        "We recommend running Evolve over Tailscale for HTTPS — "
        "required to install the admin UI on your phone."
    )
    _warn(
        f"HTTPS skipped — {cause_line}. "
        "Run 'sudo evolve-admin enable-https' later."
    )


def _build_security_section() -> dict:
    """network.json's top-level ``security`` block for a fresh pod.

    Down to one live field. ``mode`` / ``autoRejectRisk`` / ``rulesFile``
    were review.py's configuration surface and were dropped 2026-08-14 when
    the last of that reviewer's advertising surfaces went (#3641 deleted the
    reviewer itself; its deny mandate is code now, in
    ``arbiter/security_screen.py``, deliberately not config).

    ``botId`` stays because three live callers read it: the default target
    for ``evolve-admin repair-security_bot`` (``deploy.py``), the pod-readable
    user list (``deploy._pod_readable_users``), and this wizard's own
    primary-context-bot exclusion. It names the pod's designated
    security/audit bot; ``None`` means there isn't one.
    """
    return {"botId": None}


# ── Repo access (auto-update credential) ──────────────────────────────────────
#
# A durable pod stays current only if the repo-puller daemon can `git pull`
# the deploy checkout from origin. That requires (a) the checkout being a real
# clone with an `origin` remote and (b) the evolve user holding a deploy key
# GitHub accepts. The classic freeze (`evolve-vsp-pod`, 2026-06): the box was
# bootstrapped by TARBALL transfer — no `.git`, no remote, no credential — so
# the puller could never advance and the pod silently froze on install-day
# code. The only signal was a quiet "repo-puller stale" hint nobody saw.
#
# This step makes the gap LOUD at install time and records `pod.repo_url` so
# the puller (and every operator-facing deploy-key link) resolves origin
# without a git probe. `ensure_deploy_key` / `resolve_repo_url` /
# `ensure_deploy_key_registered` are reused as-is — this is wiring + a verify,
# not new ssh/crypto machinery. Platform-aware via `DEFAULT_REPO`: on macOS the
# checkout is always a real clone, so the step records the URL, re-confirms the
# key (idempotent — the puller install re-confirms it too), and stays quiet.


@dataclass
class _RepoRemote:
    """Discovered state of the deploy checkout's git origin."""
    is_git_repo: bool
    web_url: str  # normalized https form (https://github.com/owner/repo), or ""
    # The RAW `git remote get-url origin` value ("" when unknown). web_url
    # normalizes git@… to https://…, so transport decisions (can this origin
    # be read anonymously?) must gate on the raw form — an SSH origin pulls
    # over SSH regardless of what the normalized URL says.
    raw_url: str = ""


def _discover_deploy_repo(network: dict) -> _RepoRemote:
    """Probe the deploy checkout for its git origin remote.

    Seam-friendly default for :func:`_run_repo_access_step`. Reuses
    ``config.resolve_repo_url`` for the remote read + git@→https normalization;
    on a fresh install ``pod.repo_url`` is unset so it falls through to
    ``git remote get-url origin`` on ``repo_puller.DEFAULT_REPO``. The
    ``.git`` probe distinguishes a tarball-staged tree (no VCS) from a clone
    with no remote — both block auto-update, but the operator message differs.
    """
    from . import repo_puller as _rp
    from .config import resolve_repo_url

    repo = Path(_rp.DEFAULT_REPO)
    is_git = (repo / ".git").exists()
    url = resolve_repo_url(network if isinstance(network, dict) else {})
    raw = ""
    if is_git:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                raw = r.stdout.strip()
        except Exception:
            raw = ""
    return _RepoRemote(is_git_repo=is_git, web_url=url or "", raw_url=raw)


def _default_repo_deploy_key():
    """Establish + auth-test the evolve user's repo-puller deploy key."""
    from . import repo_puller as _rp
    return _rp.ensure_deploy_key(test_auth=True)


def _default_repo_load_pat(shared_dir: Path):
    """The pod-wide GitHub PAT (for the optional zero-click register), or None."""
    from . import keystore as _ks
    return _ks.load_github_pat(shared_dir)


def _default_repo_register_key(token, owner, repo, pubkey, bot_id, *, read_only, title):
    """Register the puller pubkey as a READ-ONLY deploy key on the repo."""
    from . import backup_keys as _bk
    return _bk.ensure_deploy_key_registered(
        token, owner, repo, pubkey, bot_id, read_only=read_only, title=title,
    )


def _default_repo_anon_probe(url: str) -> bool:
    """True if the origin can be read WITHOUT credentials (public HTTPS repo).

    A pod cloned over HTTPS from the public repo pulls anonymously — no deploy
    key involved — so the deploy-key walkthrough is noise there. A real
    ``ls-remote`` (not a URL heuristic) proves it; GIT_TERMINAL_PROMPT=0 keeps
    a private origin from hanging on a credential prompt.
    """
    import os as _os
    env = dict(_os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(
            ["git", "ls-remote", "--heads", url],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception:
        return False
    return r.returncode == 0


def _run_repo_access_step(
    net_path: Path,
    network: dict,
    shared_dir: Path,
    *,
    non_interactive: bool = False,
    discover_fn: "Optional[Callable[[dict], _RepoRemote]]" = None,
    deploy_key_fn: "Optional[Callable[[], Any]]" = None,
    load_pat_fn: "Optional[Callable[[Path], Optional[str]]]" = None,
    register_fn: "Optional[Callable[..., Any]]" = None,
    anon_probe_fn: "Optional[Callable[[str], bool]]" = None,
) -> dict:
    """Establish + verify the repo-puller's GitHub deploy credential.

    Flow (discover → record → verify → optional auto-register → loud-on-failure):

      1. Discover the deploy checkout's origin and record it into
         ``network.json::pod.repo_url`` (so the puller resolves origin without
         a git probe, and a tarball-staged box still has a remote of record).
      2. If the checkout cannot EVER self-update (not a git repo, or no origin
         remote) → LOUD warning + how-to, and stop. This is the tarball-freeze
         class.
      3. Establish + auth-test the evolve deploy key. On a clone-as-evolve
         durable pod the key is already accepted → SILENT success, no prompt.
      4. Key present but not yet registered: try the zero-click auto-register
         path when a GitHub PAT is on hand (read-only key); otherwise print the
         registration walkthrough — framed LOUD, because the pod will not stay
         current until the key is registered.

    Returns a small summary dict for the caller/tests. The subprocess/ssh/API
    calls are injected via the ``*_fn`` seams (defaults wire the real helpers).
    """
    discover_fn = discover_fn or _discover_deploy_repo
    summary: dict = {
        "repo_url": "", "is_git_repo": False,
        "auth_ok": False, "registered": False, "loud": False,
    }

    remote = discover_fn(network)
    summary["is_git_repo"] = remote.is_git_repo

    # 1. Record pod.repo_url from the clone remote (idempotent; preserves any
    #    sibling pod.* fields). repo-pull's --repo-url already auto-resolves
    #    from this field, so the puller follows the right origin afterwards.
    if remote.web_url:
        summary["repo_url"] = remote.web_url
        pod = network.setdefault("pod", {})
        if isinstance(pod, dict) and pod.get("repo_url") != remote.web_url:
            pod["repo_url"] = remote.web_url
            try:
                save_network(network, net_path)
                _ok(f"Deploy repo recorded: {remote.web_url}")
            except Exception as e:
                _warn(f"Could not record repo_url in network.json: {e}")

    # 2. LOUD: a checkout that can never self-update.
    if not remote.is_git_repo or not remote.web_url:
        summary["loud"] = True
        reason = (
            "the deploy checkout is not a git repository"
            if not remote.is_git_repo
            else "the deploy checkout has no 'origin' remote"
        )
        _warn("[bold]This pod will NOT stay current[/] — the repo-puller "
              f"cannot reach origin ({reason}).")
        _info("  Fixes merged upstream will never reach this box.")
        _info("  A durable pod must be a real clone owned by the [bold]evolve[/] user:")
        _info("    see docs/runbook-linux-vm-pass-2026-06-11.md → "
              "[bold]Durable pod (VPS/production)[/]")
        _info("  (Only a throwaway local VM staged from a tarball is expected to look like this.)")
        return summary

    # 3. Establish + auth-test the deploy key (idempotent; the puller install
    #    in the next step re-confirms it as a backstop).
    deploy_key_fn = deploy_key_fn or _default_repo_deploy_key
    try:
        dk = deploy_key_fn()
    except Exception as e:
        summary["loud"] = True
        _warn(f"Deploy-key bootstrap could not run ({type(e).__name__}: {e}).")
        _info("  Re-run later: [bold]sudo evolve-admin repo-pull --setup-key[/]")
        return summary

    if not getattr(dk, "success", False):
        summary["loud"] = True
        _warn(f"Deploy-key bootstrap failed: {getattr(dk, 'error', '') or 'unknown error'}.")
        _info("  Re-run later: [bold]sudo evolve-admin repo-pull --setup-key[/]")
        return summary

    if getattr(dk, "auth_test_ok", False):
        # Happy durable path: clone-as-evolve means the bootstrap key IS the
        # puller key → GitHub already accepts it. "Just works", no prompt.
        summary["auth_ok"] = True
        _ok("Repo auto-update verified — deploy key accepted by GitHub.")
        return summary

    # 3b. Key not registered — but an HTTPS origin on a PUBLIC repo pulls
    #     anonymously (the standard public-clone install), so a deploy key is
    #     unnecessary there. Prove it with a real credential-free read before
    #     alarming the operator — beta testers cloning the public repo cannot
    #     register a deploy key on it anyway (that needs repo admin).
    #     Gate on the RAW origin URL: web_url normalizes git@… to https://…,
    #     and an SSH origin pulls over SSH no matter how readable the repo is
    #     anonymously — the shortcut would false-green a pod that can't pull.
    _origin_for_transport = remote.raw_url or remote.web_url
    if _origin_for_transport.startswith("https://"):
        anon_probe_fn = anon_probe_fn or _default_repo_anon_probe
        if anon_probe_fn(_origin_for_transport):
            summary["auth_ok"] = True
            _ok("Repo auto-update verified — origin is readable over HTTPS "
                "without credentials (public repo); no deploy key needed.")
            _info("  (A read-only deploy key is only needed for a private "
                  "origin: [bold]sudo evolve-admin repo-pull --setup-key[/])")
            return summary

    # 4. Key present but not registered yet → the pod can't self-update until
    #    the operator (or the optional auto-register) puts the key on GitHub.
    summary["loud"] = True

    load_pat_fn = load_pat_fn or _default_repo_load_pat
    register_fn = register_fn or _default_repo_register_key
    token = None
    try:
        token = load_pat_fn(shared_dir)
    except Exception:
        token = None

    if token and remote.web_url:
        from . import backup_keys as _bk
        parsed = _bk.parse_github_repo_url(remote.web_url)
        if parsed:
            owner, repo_name = parsed
            try:
                from .config import resolve_pod_context
                pod_label = resolve_pod_context(network).get("ssh_target_label", "evolve-pod")
            except Exception:
                pod_label = "evolve-pod"
            reg = None
            try:
                reg = register_fn(
                    token, owner, repo_name,
                    getattr(dk, "public_key", ""), "repo-puller",
                    read_only=True, title=f"evolve repo-puller ({pod_label})",
                )
            except Exception as e:
                _warn(f"Auto-register raised: {type(e).__name__}: {e}")
            if reg is not None and (getattr(reg, "added", False) or getattr(reg, "already_present", False)):
                summary["registered"] = True
                _ok("Read-only deploy key registered on GitHub automatically.")
                # Re-verify so a green pod reports green this run.
                dk2 = None
                try:
                    dk2 = deploy_key_fn()
                except Exception as e:
                    _info(f"  (post-register re-verify skipped: {type(e).__name__})")
                if dk2 is not None and getattr(dk2, "auth_test_ok", False):
                    summary["auth_ok"] = True
                    summary["loud"] = False
                    _ok("Repo auto-update verified after registration.")
                    return summary
            elif reg is not None and getattr(reg, "error", None):
                _warn(f"Auto-register did not complete: {reg.error}")

    # Manual walkthrough — loud, because a non-updating pod is a real gap on a
    # durable host (it is the expected fresh state, but the operator must act).
    _warn("[bold]This pod will NOT stay current until its deploy key is registered.[/]")
    try:
        from . import repo_puller as _rp
        _info(_rp.format_deploy_key_instructions(dk, repo_url=remote.web_url))
    except Exception as e:
        _warn(f"  Could not render deploy-key instructions ({type(e).__name__}: {e}); "
              f"re-run: sudo evolve-admin repo-pull --setup-key")
    return summary


def _register_primary_telegram_channel(
    primary_account: str,
    primary_bot_id_choice: str,
    evolve_telegram_token: str,
    *,
    home_fn: "Callable[[str], Path]" = user_home,
    runner: "Callable[..., Any]" = subprocess.run,
) -> str:
    """Register the primary gateway's Telegram channel (deferred, post-health).

    Runs ``openclaw channels status`` / ``add`` as the gateway account
    (``evolve`` on macOS, day-one ``evo`` on Linux). Two hard rules learned on
    the live ``evolve-vsp-pod`` (FIND-B):

      * cwd MUST be a dir the gateway account can traverse — ``openclaw`` is a
        Node binary and Node calls ``uv_cwd()`` at startup, EACCES-dying if it
        inherits the root-run wizard's cwd (e.g. the operator's home over SSH,
        or ``/var/lib/evolve/repo`` which ``evo`` cannot enter). Pass the
        gateway home (CLAUDE.md ``sudo -H -u <bot> openclaw`` gotcha).
      * The whole probe is timeout-guarded: a slow/hung gateway must degrade to
        a "register manually" warning, never crash the wizard with an unhandled
        ``TimeoutExpired`` *after* "Setup Complete" already printed.

    Returns a short status token for callers/tests:
    ``"already" | "registered" | "failed" | "timeout" | "error"``.
    """
    gw_home = home_fn(primary_account)
    manual = (f"  sudo -u {primary_account} env HOME={gw_home} "
              f"openclaw channels add --channel telegram --token <token>")
    try:
        cr = runner(
            ["sudo", "-u", primary_account, "env", f"HOME={gw_home}",
             "openclaw", "channels", "status"],
            cwd=str(gw_home), capture_output=True, text=True, timeout=10,
        )
        out = (cr.stdout or "").lower()
        if "running" in out and "telegram" in out:
            return "already"  # already registered
        with console.status(
            f"  Registering Telegram channel with {primary_bot_id_choice} gateway..."
        ):
            reg = runner(
                ["sudo", "-u", primary_account, "env", f"HOME={gw_home}",
                 "openclaw", "channels", "add",
                 "--channel", "telegram", "--token", evolve_telegram_token],
                cwd=str(gw_home), capture_output=True, text=True, timeout=15,
            )
        if reg.returncode == 0:
            _ok(f"{primary_bot_id_choice} Telegram channel registered")
            return "registered"
        _warn(f"Telegram registration failed — run manually:\n{manual}")
        return "failed"
    except subprocess.TimeoutExpired:
        _warn(
            f"Telegram channel check timed out talking to the "
            f"{primary_bot_id_choice} gateway — register manually:\n{manual}"
        )
        return "timeout"
    except OSError as e:
        _warn(f"Telegram channel registration skipped ({type(e).__name__}: {e}).")
        return "error"


# ── Main wizard ───────────────────────────────────────────────────────────────

def run_fresh_wizard(
    non_interactive: bool = False,
    network_path: Optional[Path] = None,
    bots_manifest: Optional[Path] = None,
    platform_opt: Optional[str] = None,
) -> None:
    """
    Full fresh-machine setup wizard.
    Runs as root (sudo required for account creation and service installs).

    Pod membership is explicit. In non-interactive mode the wizard
    requires `bots_manifest` (a JSON file listing the bots to register)
    and refuses to guess from filesystem state. The interactive flow
    accepts an optional manifest as a starting point but still presents
    discovered candidates for the operator to confirm.

    ``platform_opt`` is the ``--platform`` CLI value; it drives the
    platform gate below (both macOS and Linux auto-detect, so it is
    optional — pass it only to fail loudly on a platform mismatch).
    """
    # Platform gate FIRST — before any prompt or filesystem touch. macOS and
    # Linux both auto-detect and pass through (Linux pins the LINUX profile +
    # adapters); anything else hard-fails. See design-linux-port-2026-06-10.md §9.
    _resolve_platform_gate(platform_opt)

    # Linux preflight: refuse a repo source root the service user can't read
    # (e.g. staged under /root) BEFORE building the venv or installing daemons —
    # an unreadable source poisons the venv .pth and every daemon ExecStart.
    _preflight_repo_root_traversable()

    # Linux preflight: refuse a stale /Users tree (always cruft on Linux; a
    # leftover /Users/<bot>/.openclaw feeds the key scan a stale key — round-3 B).
    _preflight_no_stale_users_tree()

    # The Unix account the dedicated primary bot's gateway runs as. On macOS
    # the bot starts on the `evolve` service account and the E.2.b cutover
    # moves it to `evo`; on a fresh Linux pod there is no pre-separation
    # legacy, so we provision straight onto `evo` day-one and skip the
    # cutover (census §12 Q9 / W5).
    from platform_profile import get_profile as _get_profile
    primary_account = "evo" if _get_profile().name == "linux" else "evolve"

    net_path = network_path or DEFAULT_NETWORK_CONFIG
    total = 18

    console.print(Panel.fit(
        "[bold]Welcome to Evolve Setup[/]\n\n"
        "This wizard will set up a complete OpenClaw pod on this machine.\n"
        "It handles everything: account creation, OC install, channel config,\n"
        "Evolve deploy, and gateway startup.\n\n"
        "[dim]Re-run at any time — all steps are idempotent.[/]\n"
        "[dim]Press Ctrl+C to exit without making changes.[/]",
        title="⚡ Evolve",
    ))

    # Version-aware re-run detection. Anchor on the platform-keyed canonical
    # shared dir (config.py → platform_profile): /Users/Shared/evolve on macOS,
    # /var/lib/evolve on Linux. Hardcoding the macOS path here made the
    # re-run/repair probe blind to an existing install under EVOLVE_PLATFORM=linux.
    from .deploy import read_install_json, EVOLVE_VERSION
    _shared_dir_early = Path(DEFAULT_SHARED_DIR)
    install_info = read_install_json(_shared_dir_early)

    if install_info:
        installed_ver = install_info.get("version", "unknown")
        installed_at = install_info.get("installed_at", "")[:10]

        def _parse_ver(v: str) -> tuple[int, ...]:
            try:
                return tuple(int(x) for x in v.split("."))
            except (ValueError, AttributeError):
                return (0,)

        cur_tup = _parse_ver(EVOLVE_VERSION)
        inst_tup = _parse_ver(installed_ver)

        if cur_tup == inst_tup:
            _warn(f"Evolve v{installed_ver} already installed (installed {installed_at}) — running in repair mode.")
            _info("Already-completed steps will be skipped.")
        elif cur_tup > inst_tup:
            _warn(f"Upgrading Evolve v{installed_ver} → v{EVOLVE_VERSION} (installed {installed_at}).")
            _info("User data and configs will be preserved.")
        else:
            _warn(f"Warning: installed v{installed_ver} is newer than codebase v{EVOLVE_VERSION}.")
            _info("Downgrade may cause issues. Proceed with caution.")
        console.print()
        is_repair = True
    elif (_shared_dir_early / "network.json").exists():
        _warn("Existing Evolve install detected (no install.json — pre-v0.3 install).")
        _info("Running in repair mode — already-completed steps will be skipped.")
        console.print()
        is_repair = True
    else:
        is_repair = False

    # Pre-flight health scan (repair/upgrade mode only) — show what's broken
    # before the wizard touches anything, so the user sees a before-state.
    # Uses phase="pre" so launchd/gateway checks are excluded: those can't
    # pass before a deploy has run and would only produce misleading failures.
    if is_repair and net_path.exists():
        try:
            from .health import run_health_check, print_report, apply_all_fixes
            _pre = run_health_check(network_path=net_path, phase="pre")
            if not _pre.ok or _pre.warned:
                console.print("[bold]Pre-flight scan (current state):[/]")
                print_report(_pre, verbose=False)
                console.print()
                fixable = [c for c in _pre.checks if c.status != "PASS" and c.fix_args]
                if fixable:
                    _info(f"Auto-fixing {len(fixable)} pre-flight issue(s)...")
                    fix_results = apply_all_fixes(_pre)
                    for fr in fix_results:
                        if fr.ok:
                            _ok(f"{fr.name}: {fr.message}")
                        else:
                            _warn(f"{fr.name}: {fr.message}")
                    console.print()
            else:
                _ok("Pre-flight scan passed")
        except Exception:
            pass  # never block setup on a pre-flight scan failure

    # Load existing config values to use as prompt defaults
    _existing = _load_existing_config(net_path)
    if any(_existing.values()):
        _info("[dim]Existing config found — previous values will be offered as defaults.[/]")
        console.print()

    # ── Step 1: Pod identity ──────────────────────────────────────────────────
    _step(1, total, "Pod identity")
    pod_name = _ask("Pod name", _existing.get("pod_name") or "my-pod", non_interactive)
    pod_name = pod_name.replace(" ", "-").lower()
    _ok(f"Pod: [bold]{pod_name}[/]")

    # Pod timezone — single source of truth for date rendering across the
    # admin UI, plugin, and analyzer. Storage stays UTC; only rendering converts.
    # Skip the prompt on repairs where network.json already has the field; offer
    # it explicitly if the field is missing (older installs from before #740).
    if _existing.get("_timezone_present") and _existing.get("timezone"):
        pod_tz = _existing["timezone"]
        _skip(f"Pod timezone: [bold]{pod_tz}[/] (from existing network.json)")
    else:
        pod_tz = _ask_timezone(_existing.get("timezone") or "", non_interactive)
        _ok(f"Pod timezone: [bold]{pod_tz}[/]")

    # ── Step 2: Bot roster ────────────────────────────────────────────────────
    _step(2, total, "Bot roster")


    from .wizard import _find_existing_keys, _create_bot_flow, find_oc_candidates

    bots: list[BotSpec] = []
    # Carry the operator's personal Telegram chat ID entered while creating a
    # bot here through to Step 11, where it pre-fills the Evolve-alerts chat
    # default (same person — don't ask for the same ID twice). Stays "" when
    # no interactive bot supplied one (manifest / claimed-install paths).
    bot_alert_chat_id = ""

    def _candidate_label(c) -> str:
        """Human-readable display string for an OcCandidate."""
        sugg = f" (suggested bot_id={c.suggested_bot_id})" if c.suggested_bot_id else ""
        admin = " [admin account]" if c.looks_like_admin else ""
        member = " [already in pod]" if c.is_pod_member else ""
        return f"user={c.user} port={c.port or 'unknown'}{sugg}{admin}{member}"

    if non_interactive:
        # Pod membership is explicit even in non-interactive mode. Refuse
        # to guess from filesystem state — require an operator-provided
        # manifest. This is the inverse of the historical auto-include
        # behavior that produced the 2026-05-03 phantom-bot incident.
        if not bots_manifest:
            console.print(
                "[red]Error:[/] --non-interactive --fresh requires "
                "[bold]--bots-manifest <path>[/].\n"
                "  Pod membership must be explicit; the wizard will not "
                "guess from filesystem state.\n"
                "  Manifest format (JSON):\n"
                '    {"bots": [{"bot_id": "admin_bot", "port": 19000, '
                '"user": "admin_bot", "role": "member", "multi_user": false}]}'
            )
            sys.exit(1)
        try:
            bots = load_bots_manifest(bots_manifest)
        except ManifestError as e:
            console.print(f"[red]Error:[/] {e}")
            sys.exit(1)
        _ok(f"Loaded {len(bots)} bot(s) from manifest: "
            + ", ".join(f"{b.name}:{b.port}" for b in bots))
    else:
        # If an interactive operator provided a manifest, seed the bot list
        # from it; they can still confirm/edit during the rest of the flow.
        if bots_manifest:
            try:
                bots = load_bots_manifest(bots_manifest)
                _ok(f"Loaded {len(bots)} bot(s) from manifest")
            except ManifestError as e:
                _warn(f"Could not parse manifest: {e} — falling back to discovery")
                bots = []

    candidates = find_oc_candidates()
    addable = [c for c in candidates if not c.is_pod_member]

    if not non_interactive:
        # Discover existing OC instances first
        _info("Scanning for existing OpenClaw instances...")

        if addable:
            _info(f"Found {len(addable)} install(s) available to claim:")
            for i, c in enumerate(addable, 1):
                _info(f"  [{i}] {_candidate_label(c)}")
            console.print()
            _info("Enter numbers to include (e.g. 1 2 3), or [bold]a[/] for all, [bold]n[/] for none:")
            choice = _ask("Claim which installs?", "a", non_interactive).strip().lower()
            chosen: list = []
            if choice in ("a", ""):
                chosen = list(addable)
            elif choice == "n":
                chosen = []
            else:
                for tok in choice.replace(",", " ").split():
                    if tok.isdigit():
                        idx = int(tok) - 1
                        if 0 <= idx < len(addable):
                            chosen.append(addable[idx])
            for c in chosen:
                # Ask for the bot_id under which to register this candidate.
                # Default to the suggested name (parsed from gateway plist) or
                # the macOS user — but the operator decides.
                default_id = c.suggested_bot_id or c.user
                bot_id = _ask(
                    f"  Bot ID for user={c.user}",
                    default_id,
                    non_interactive,
                ).strip() or default_id
                bots.append(BotSpec(
                    name=bot_id,
                    user=c.user,
                    port=c.port or 19000,
                    role="member",
                ))
        else:
            if candidates:
                _info("All discovered installs are already pod members.")
            else:
                _info("No existing OpenClaw instances found.")

        console.print()

        # Create first bot if none selected
        while not bots:
            _info("You need at least one bot. Let's create one.")
            existing_keys = _find_existing_keys()
            bot = _create_bot_flow(existing_keys)
            if bot:
                bots.append(_botspec_from_wizard_bot(bot))
                if not bot_alert_chat_id and bot.get("chat_id"):
                    bot_alert_chat_id = bot["chat_id"]
            else:
                if not _confirm("You need at least one bot. Try again?", default=True, non_interactive=non_interactive):
                    console.print("[red]At least one bot is required. Exiting.[/]")
                    sys.exit(1)

        # Offer to add more
        while _confirm("Add a new bot to the pod?", default=False, non_interactive=non_interactive):
            existing_keys = _find_existing_keys()
            bot = _create_bot_flow(existing_keys)
            if bot:
                bots.append(_botspec_from_wizard_bot(bot))
                if not bot_alert_chat_id and bot.get("chat_id"):
                    bot_alert_chat_id = bot["chat_id"]
            else:
                break

    console.print()
    for b in bots:
        _info(f"  • [bold]{b.name}[/] — port {b.port}")

    # Pre-flight config validation
    console.print()
    for bot in bots:
        for issue in _validate_bot_config(bot.name):
            _warn(f"Config issue detected in {bot.name}:")
            _info(f"  {issue}")
            _info(f"  → Evolve will automatically fix this before installing. No action needed.")

    # ── Primary-bot designation ──────────────────────────────────────────────
    # Every Evolve install now ships with a dedicated ``evo`` primary bot
    # on the ``evolve`` Unix account. The old "dedicated vs. existing"
    # prompt was retired (2026-05-20) — once the Better Engine direction
    # established evo as a first-class member of every pod, asking the
    # operator to choose stopped being a meaningful decision (the
    # security-hardened dedicated config is the right answer 100% of
    # the time, and adopting an existing bot left its openclaw config
    # at risk of subtle skew).
    #
    # ``primary_bot_id_choice`` + ``primary_mode`` are retained as variables
    # because downstream code still consults ``primary_mode`` for inert
    # one-liners (gateway-port write, analytics-dir seeding, the post-deploy
    # verify message). We always set dedicated/evo here. The heavyweight
    # existing-primary *provisioning* branch (its own /Users/evolve account
    # setup + ``evolve:staff`` chown) was removed in the 8.3 Linux-port path
    # sweep once the one-release grace window elapsed — it was marked dead on
    # 2026-05-20 (#1353) and the fleet has shipped many releases since, so no
    # live install still carries ``role: "primary"`` on a pre-evo bot.
    primary_bot_id_choice: str = "evo"
    primary_mode: str = "dedicated"
    if not non_interactive:
        _info(
            "[bold]Primary bot[/] — a dedicated [bold]evo[/] bot will be "
            "provisioned on the evolve Unix account (security-hardened "
            "openclaw config). This is the standard Evolve setup; no "
            "choice required."
        )
    _ok(f"Primary bot: [bold]{primary_bot_id_choice}[/] ({primary_mode})")

    # ── Step 3: Security configuration ───────────────────────────────────────
    _step(3, total, "Security configuration")
    evolve_backup_url = _run_security_config_step(non_interactive, _existing)

    # ── Step 4: Admin user identity ───────────────────────────────────────────
    _step(4, total, "Admin user")
    _info("What is the admin username on this machine?")
    _info("(This user has sudo access and will execute privileged actions.)")
    try:
        import getpass as _gp
        default_admin = _existing.get("admin_user") or _gp.getuser()
    except Exception:
        default_admin = _existing.get("admin_user") or "admin"
    admin_user = _ask("Admin username", default_admin, non_interactive)
    _ok(f"Admin user: [bold]{admin_user}[/]")

    # ── Step 5: Prerequisites ─────────────────────────────────────────────────
    _step(5, total, "Check prerequisites")

    prereqs = _check_prerequisites()
    abort = False
    oc_missing = False
    for p in prereqs:
        if p.ok:
            detail = f" [dim]{p.detail}[/]" if p.detail else ""
            console.print(f"  [green]✅[/] {p.name}{detail}")
        elif p.name.startswith("OpenClaw"):
            console.print(f"  [yellow]⚠️ [/] {p.name} — {p.detail}")
            oc_missing = True
        elif p.hard:
            console.print(f"  [red]❌[/] {p.name} — {p.detail}")
            abort = True
        else:
            console.print(f"  [yellow]⚠️ [/] {p.name} — {p.detail}")

    if abort:
        console.print("\n[red]Prerequisites not met. Fix the issues above and re-run.[/]")
        sys.exit(1)

    # ── Step 6: Host power & sleep ────────────────────────────────────────────
    # Any dedicated, always-on Mac is a supported host (Phase 8.2) — but a
    # host that sleeps silently kills every launchd job and gateway, so the
    # power posture is a first-class prereq. Explains + offers the pmset
    # fix; never hard-blocks.
    _step(6, total, "Host power & sleep")
    host_posture = _run_power_posture_step(non_interactive)

    # ── Step 7: Dedicated-host acknowledgment ─────────────────────────────────
    _step(7, total, "Dedicated-host acknowledgment")
    dedication_ack = _run_dedication_ack_step(
        non_interactive, _existing.get("host") or {}, admin_user,
    )

    # ── Step 8: Install OpenClaw (if needed) ──────────────────────────────────
    _step(8, total, "Install OpenClaw")
    if not oc_missing:
        oc_path = (
            shutil.which("openclaw")
            or shutil.which("openclaw", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
        )
        _skip(f"OpenClaw already installed at {oc_path}")
    else:
        if not _confirm("OpenClaw not found — install via npm now?", True, non_interactive):
            console.print("[red]OpenClaw is required. Install it manually and re-run.[/]")
            sys.exit(1)
        if not _install_openclaw_npm():
            console.print("[red]OpenClaw install failed. Install manually and re-run.[/]")
            sys.exit(1)

    # ── Step 9: Create bot accounts ───────────────────────────────────────────
    _step(9, total, "Create bot accounts")
    from platform_profile import get_profile as _get_profile
    _account_noun = "macOS" if _get_profile().name == "macos" else "Linux"
    for bot in bots:
        with console.status(f"  Creating {_account_noun} account '{bot.name}'..."):
            if not _create_bot_account(bot.name):
                console.print(f"[red]Failed to create account for {bot.name}. Fix and re-run.[/]")
                sys.exit(1)

    # ── Step 10: Install OpenClaw on each bot ─────────────────────────────────
    _step(10, total, "Set up OpenClaw per bot")
    for bot in bots:
        with console.status(f"  Configuring OC for '{bot.name}'..."):
            if not _setup_oc_for_bot(bot.name, bot.port):
                console.print(f"[red]OC setup failed for {bot.name}. Fix and re-run.[/]")
                sys.exit(1)

    # ── Step 11: Configure Telegram ───────────────────────────────────────────
    _step(11, total, "Configure Evolve alerts")
    _info("Evolve needs to know how to send you alerts (proposals ready, bot down, cost spikes, etc.)")
    _info("")
    _info("This step configures how [bold]Evolve[/] itself communicates with you.")
    _info("Bot channel configuration (Telegram tokens per bot) can be managed in the Evolve admin UI after setup.")
    _info("")
    _info("  [bold]headless[/]   — Web UI only (http://localhost:5050). No chat interface for Evolve.")
    _info("                  Good if you prefer a browser or are always at your machine.")
    _info("")
    _info("  [bold]dedicated[/]  — Evolve gets its own Telegram bot (recommended).")
    _info("                  Infrastructure alerts stay separate from your personal bots.")
    _info("                  Useful if you manage your pod remotely from your phone.")
    _info("")
    _existing_comms = _existing.get("comms_mode") or "dedicated"
    evolve_comms_mode = _ask("Evolve comms mode (headless/dedicated)", _existing_comms, non_interactive).strip().lower()
    if evolve_comms_mode not in ("headless", "dedicated"):
        evolve_comms_mode = "headless"

    telegram_token = ""
    alert_chat_id = ""
    evolve_telegram_token = ""
    telegram_tested = False

    if evolve_comms_mode == "dedicated":
        _existing_token = _existing.get("telegram_token") or ""
        if _existing_token:
            _info("")
            _info(f"  [dim]Existing Evolve Telegram token found (ending …{_existing_token[-6:]}). Press Enter to reuse.[/]")
        else:
            _info("")
            _info("Create a Telegram bot for Evolve at [bold]@BotFather[/]:")
            _info("  1. Message @BotFather and send [bold]/newbot[/]")
            _info("  2. Choose a name (e.g. \"My Pod Evolve\") and username (e.g. @mypod_evolve_bot)")
            _info("  3. Copy the token it gives you")
            _info("")
        evolve_telegram_token = _ask("Evolve Telegram bot token (or Enter to skip)", _existing_token, non_interactive)
        if not evolve_telegram_token:
            _warn("No token provided — falling back to headless mode.")
            evolve_comms_mode = "headless"
        else:
            with console.status("  Testing Evolve Telegram connection..."):
                bot_username = _test_telegram_token(evolve_telegram_token)
            if bot_username:
                _ok(f"Evolve connected as @{bot_username}")
            else:
                _warn("Could not verify token — writing anyway.")
            telegram_token = evolve_telegram_token

        if evolve_telegram_token:
            _info("")
            _info("Your Telegram chat ID is needed to receive Evolve alerts.")
            _info("(Message [bold]@userinfobot[/] on Telegram to find your ID)")
            # Default to the chat ID already entered in Step 2 (same operator)
            # so the common case is just pressing Enter; an existing-config
            # value wins on re-runs, and it stays editable for operators who
            # want infra alerts on a different chat.
            _chat_default = _evolve_alert_chat_default(
                _existing.get("chat_id") or "", bot_alert_chat_id
            )
            if bot_alert_chat_id and not _existing.get("chat_id"):
                _info("(Press Enter to reuse the chat ID you entered above.)")
            alert_chat_id = _ask("Your Telegram chat ID", _chat_default, non_interactive)

    _ok(f"Evolve comms: [bold]{evolve_comms_mode}[/]")

    # ── Security alert channel (no longer a forced onboarding step) ───────────
    # A dedicated security-alert bot used to be Step 12. It's been demoted to an
    # opt-in Settings control (separate follow-up), so the wizard no longer
    # prompts for it. The CRITICAL-finding fallback in audit._send_security_alert
    # reads ONLY {shared_dir}/keystore/security-alert-{token,chat-id}; if we stop
    # populating those, the dispatcher-down fallback silently drops critical
    # alerts. So we default the security channel to the main Evolve channel
    # (byte-identical to pressing Enter at the old Step 12) and keep the
    # keystore-write below intact — UNLESS a dedicated channel was configured
    # previously, in which case we preserve it rather than clobber it.
    sec_token = telegram_token
    sec_chat_id = alert_chat_id
    _existing_sec_token = _existing.get("security_alert_token") or ""
    _existing_sec_chat = _existing.get("security_alert_chat_id") or ""
    if _existing_sec_token and _existing_sec_token != telegram_token:
        sec_token = _existing_sec_token
        sec_chat_id = _existing_sec_chat or alert_chat_id

    # ── Step 12: Shared directory ──────────────────────────────────────────────
    _step(12, total, "Set up shared directory")
    shared_dir_str = _ask(
        "Shared directory path", _existing.get("shared_dir") or str(DEFAULT_SHARED_DIR), non_interactive
    )
    shared_dir = Path(shared_dir_str)

    with console.status(f"  Creating {shared_dir}..."):
        try:
            setup_shared(shared_dir)
            _ok(f"Shared directory: {shared_dir}")
        except subprocess.CalledProcessError as e:
            _warn(f"Shared directory setup had errors: {e}")
        except Exception as e:
            _warn(f"Shared directory: {e}")

    # Fresh-pod bring-up settle window: stamp the start NOW — shared_dir exists,
    # but Step 13's deploy (harden → ACL-reassert cycle) and Step 15's monitor
    # daemons haven't run yet. Monitors that fire during bring-up will see this
    # marker (and no settled marker) and withhold transient setup-state
    # findings until `mark_settled` lands at the end of the wizard. Fresh pods
    # only — never on repair/upgrade. See docs/spec-pod-bringup-settle-2026-06-23.md.
    if not is_repair:
        try:
            from signals.settle_gate import mark_bringup_started
            mark_bringup_started(shared_dir, trigger="fresh-wizard")
        except Exception as e:
            _warn(f"Could not stamp bring-up settle marker: {e}")

    # ── Step 13: Deploy Evolve ────────────────────────────────────────────────
    _step(13, total, "Deploy Evolve")

    # Build network config
    bots_cfg: dict = {}
    members: list[str] = []
    # First-install bots get the same graduated new-bot daily-cap default
    # that later-added bots do (deploy.add_bot's contract): a creation-time
    # ``created_at`` stamp is written to better-engine-config below (after
    # save_network) so the resolver age-grades the cap — $10/day for the
    # first 7 days, then the pod default. Nothing static is materialized
    # (a static value never graduates); operators can still set an explicit
    # per-bot override later via the UI / action.cost.
    for bot in bots:
        bots_cfg[bot.name] = _bot_network_entry(bot)
        members.append(bot.name)

    # ── Primary bot ──────────────────────────────────────────────────────────
    # For the dedicated branch, the bot doesn't exist in the roster yet — we
    # add its network.json entry here. The Unix account is `primary_account`
    # (evolve on macOS pre-cutover, evo day-one on Linux); the bot is named
    # per the operator's choice (default "evo"). For the existing-bot branch,
    # flip the chosen bot's role to "primary".
    if primary_mode == "dedicated":
        primary_entry = bots_cfg.get(primary_bot_id_choice, {})
        primary_entry["role"] = "primary"
        primary_entry["user"] = primary_account  # account name ≠ bot id
        # The primary's gateway port + comms mode live on its OWN bot entry —
        # the canonical per-bot location (see config.get_bot_port). Readers
        # resolve them via primary_bot.primary_bot_gateway_port / _comms_mode
        # (evo-account-separation S1); no separate top-level `evolve` block
        # keyed by the legacy literal is written any more.
        primary_entry["port"] = _EVOLVE_PORT
        primary_entry["comms_mode"] = evolve_comms_mode
        if evolve_backup_url:
            primary_entry["backupRepoUrl"] = evolve_backup_url
        bots_cfg[primary_bot_id_choice] = primary_entry
    else:
        # Existing bot adopts the primary role; do NOT touch its openclaw
        # config — that happens later only in the dedicated branch.
        if primary_bot_id_choice in bots_cfg:
            bots_cfg[primary_bot_id_choice]["role"] = "primary"
            # Record the primary's comms mode on its own entry (its port is
            # already set from its own provisioning — leave it alone).
            bots_cfg[primary_bot_id_choice]["comms_mode"] = evolve_comms_mode
        if evolve_backup_url:
            # Backup remote still attaches to the primary bot entry — the
            # workspace dir lives on the evolve account, which the chosen
            # bot may or may not share. Operator can adjust if needed.
            bots_cfg[primary_bot_id_choice]["backupRepoUrl"] = evolve_backup_url

    # Pod-wide embedding default. The static DEFAULT_EMBEDDING_CHAIN is fine
    # here — the runtime resolver in analyzer/embeddings.py filters this list
    # to providers that actually have credentials at use time, and the operator
    # can edit it via AI Optimization. Writing it makes the chain visible in
    # the admin UI from day one.
    _embedding_default_chain: list[str]
    try:
        from embeddings import DEFAULT_EMBEDDING_CHAIN  # type: ignore
        _embedding_default_chain = list(DEFAULT_EMBEDDING_CHAIN)
    except Exception:
        _embedding_default_chain = ["gemini", "openai", "local"]

    network: dict = {
        "networkId": pod_name,
        "admin_user": admin_user,
        # Host posture (Phase 8.2): hardware identity, power/sleep state at
        # setup time, and the operator's dedicated-host acknowledgment.
        # The ack is the durable record of the informed-operator choice the
        # threat model's single-tenant assumption rests on (§2).
        "host": {**host_posture, "dedication_ack": dedication_ack},
        # Top-level field: the bot_id that handles sysadmin alerts &
        # proposals. Consumers resolve via primary_bot.primary_bot_id.
        # The primary's gateway port + comms mode live on its OWN bots[] entry
        # (seeded above) — NOT a legacy top-level `evolve` block keyed by the
        # literal, which a reader assuming "evolve" would mint a phantom
        # primary surface from (evo-account-separation S1, W10-F #7 cleanup).
        "primary": primary_bot_id_choice,
        "members": members,
        "sharedDir": str(shared_dir),
        # IANA tz name for UI/plugin/analyzer rendering. Storage stays UTC.
        "timezone": pod_tz,
        "thresholds": {},
        "classifiers": {
            "tier": {"tier": "tier3", "fallback": "keyword"},
        },
        "models": {
            "embedding": {
                "default_chain": _embedding_default_chain,
            },
        },
        "alerts": {
            "channel": "telegram",
            "chatId": alert_chat_id,
        },
        "security": _build_security_section(),
        "heal": {
            "failuresBeforeProposal": 3,
            "windowHours": 24,
            "slowThresholdMs": 3000,
            "restartCooldownMin": 10,
            "checkTimeoutSec": 5,
        },
        "bots": bots_cfg,
    }

    # Preserve existing mcp_bridge config so it survives step-12 rewrites
    # (step 14 will update it, but if the user skips step 14 it would otherwise be lost)
    if _existing.get("mcp_bridge"):
        network["mcp_bridge"] = _existing["mcp_bridge"]

    # Seed the built-in assistant(s) (evo/evolve) a default purpose so their
    # Setup checklist's purpose row reads Done from day one (idempotent — only
    # seeds when absent, never overwrites). On a live pod the admin Setup-
    # checklist read path (web.routes_setup_checklist) re-asserts this; doing it
    # here means a fresh pod's primary carries its purpose the moment
    # network.json is first written.
    try:
        from .config import ensure_reserved_bot_purposes
        ensure_reserved_bot_purposes(network)
    except Exception as _e:
        _warn(f"setup: reserved-bot purpose seed skipped ({_e})")

    net_path.parent.mkdir(parents=True, exist_ok=True)
    save_network(network, net_path)
    _ok(f"network.json written: {net_path}")

    # Stamp each bot's created_at into better-engine-config (canonical store
    # since Phase 4 of the 2026-06 cost-cap normalization) so the graduated
    # new-bot daily-cap default applies — $10/day for the first 7 days, then
    # the pod default. Best-effort: an import or write failure here just means
    # a bot looks "mature" and falls straight to the pod default (safe).
    try:
        from datetime import datetime, timezone

        from .deploy import _be_set_bot_created_at
        _setup_now = datetime.now(timezone.utc)
        for bot in bots:
            try:
                _be_set_bot_created_at(shared_dir, bot.name, _setup_now)
            except Exception as _exc:
                _warn(f"setup: created_at stamp failed for {bot.name}: {_exc}")
    except Exception as _exc:
        _warn(f"setup: better-engine-config writer unavailable ({_exc}); "
              f"new-bot graduated cap will fall through to the pod default")

    deploy_errors = _deploy_evolve(bots, network, net_path)

    # Mid-wizard health check: verify bot gateways came up after deploy, before
    # proceeding to Step 15.  Use phase="mid" so we only probe gateways (with
    # a short retry window) — launchd and file-security checks are skipped
    # because infra plists aren't installed until Step 15.
    try:
        from .health import run_health_check
        _mid = run_health_check(
            network_path=net_path,
            members_override=[b.name for b in bots],
            phase="mid",
        )
        _mid_fails = [
            c for c in _mid.checks
            if c.status == "FAIL" and c.category == "gateways"
        ]
        if _mid_fails:
            # After the 45 s retry window (health.py phase=="mid"), a gateway
            # still not answering /evolve/status is almost always just slow
            # plugin load, not a broken install — and the final health scan
            # re-verifies every gateway authoritatively. Frame this as
            # informational, not an alarm, so operators don't abort a healthy
            # setup over a gateway that is merely still starting.
            _info("Gateways are still finishing startup:")
            for c in _mid_fails:
                _info(f"  • {c.name} — {c.detail}")
            _info("  This is normally just the gateway plugin still loading; it is")
            _info("  re-checked authoritatively by the health scan at the end of setup.")
            if not non_interactive:
                if not _confirm("Continue setup? (recommended)", default=True, non_interactive=non_interactive):
                    console.print("[yellow]Setup paused. Re-run any time — all steps are idempotent.[/]")
                    sys.exit(1)
    except Exception:
        pass  # never block setup on a mid-wizard health check failure

    # ── Step 14: Repo access (auto-update credential) ─────────────────────────
    # Record pod.repo_url + establish/verify the repo-puller deploy key BEFORE
    # the puller daemon is installed (Step 15). Makes the tarball-freeze class
    # (no .git / no remote / unregistered key) LOUD at install time instead of
    # the silent "repo-puller stale" hint. Near-no-op verify on macOS (the
    # deploy checkout is always a real clone there).
    _step(14, total, "Repo access (auto-update)")
    try:
        _run_repo_access_step(
            net_path, network, shared_dir, non_interactive=non_interactive,
        )
    except Exception as _e:
        # Never block setup on the repo-access verify — the puller install in
        # Step 15 still bootstraps the key as a backstop.
        _warn(f"  Repo-access step skipped ({type(_e).__name__}: {_e}); "
              f"the repo-puller install will still bootstrap the deploy key.")

    # ── Step 15: Provision primary bot OC instance + sudoers ─────────────────
    _step(15, total, "Provision primary bot OC instance")

    # Write security alert token to keystore before provisioning
    if sec_token.strip() and sec_chat_id.strip():
        keystore_dir = shared_dir / "keystore"
        keystore_dir.mkdir(parents=True, exist_ok=True)
        (keystore_dir / "security-alert-token").write_text(sec_token.strip())
        (keystore_dir / "security-alert-chat-id").write_text(sec_chat_id.strip())
        subprocess.run(
            [
                "sudo", "/bin/chmod", "600",
                str(keystore_dir / "security-alert-token"),
                str(keystore_dir / "security-alert-chat-id"),
            ],
            check=False,
        )
        _ok("Security alert channel configured")

    if primary_mode == "dedicated":
        _provision_evo_oc(
            pod_name, shared_dir, admin_user, bots, non_interactive,
            telegram_token=evolve_telegram_token,
            bot_id=primary_bot_id_choice,
            gateway_account=primary_account,
        )
        if primary_account == "evolve":
            # macOS: the wizard above provisioned the primary bot on
            # /Users/evolve/ (the legacy shape). Bundle the account
            # separation migration here so the operator doesn't have to run
            # the cutover by hand on every new install — the bot lands at
            # /Users/evo/.openclaw/ with network.json's user="evo" before
            # the wizard finishes. Same helpers the operator runbook calls
            # (docs/runbook-evo-account-separation-cutover-2026-05-26.md);
            # the cutover is idempotent and re-running it is a no-op.
            with console.status("  Provisioning evo macOS account (Phase E.2.a)..."):
                _evo_ok = _provision_evo_account()
            if not _evo_ok:
                _warn(
                    "evo account provisioning failed; install will continue "
                    "but the dedicated primary bot stays on /Users/evolve/. "
                    "Run `sudo evolve-admin provision-evo-account` then "
                    "`sudo evolve-admin migrate-evo-account-cutover --confirm` "
                    "later to finish the migration."
                )
            else:
                _log_admin_action("create_user", "ok", bot="evo")
                with console.status(
                    "  Migrating primary bot to /Users/evo/ (Phase E.2.b)..."
                ):
                    _cutover_ok = _perform_evo_cutover(net_path, dry_run=False)
                if not _cutover_ok:
                    _warn(
                        "evo cutover failed; bot stays on /Users/evolve/. "
                        "Run `sudo evolve-admin migrate-evo-account-cutover "
                        "--confirm` later to retry."
                    )
        else:
            # Fresh Linux: _provision_evo_oc already created the `evo`
            # account and provisioned the gateway straight onto it (day-one,
            # via gateway_account="evo"). No pre-separation legacy exists, so
            # the E.2.a/E.2.b cutover dance is unnecessary — network.json's
            # user is already "evo" (set in Step 13).
            _log_admin_action("create_user", "ok", bot="evo")

    # Write sudoers drop-ins
    _info("  Writing sudoers drop-in for admin user...")
    all_bots_for_sudoers = bots + ([type("B", (), {"name": "evolve"})()] if not any(b.name == "evolve" for b in bots) else [])
    if not _write_sudoers(admin_user, all_bots_for_sudoers):
        _warn("  Sudoers setup failed — run manually: sudo visudo -f /etc/sudoers.d/evolve-admin")

    _info("  Writing sudoers drop-in for evolve service user...")
    if not _write_evolve_sudoers():
        _warn("  evolve sudoers setup failed — run manually: sudo visudo -f /etc/sudoers.d/evolve")

    # Create per-bot shared analytics dirs (turns, summaries, metrics)
    # Include the dedicated evo bot when present — it runs the plugin and
    # generates turns too. For the existing-bot path the chosen bot is
    # already in `bots`, so no stub needed.
    _info("  Creating per-bot shared analytics directories...")
    if primary_mode == "dedicated":
        _evo_stub = type("B", (), {"name": primary_bot_id_choice})()
        _all_dirs_bots = bots + (
            [] if any(b.name == primary_bot_id_choice for b in bots if hasattr(b, "name")) else [_evo_stub]
        )
    else:
        _all_dirs_bots = bots
    _setup_bot_shared_dirs(_all_dirs_bots, shared_dir)

    # Update network.json — only for the dedicated branch where we
    # provisioned a new gateway. For existing bots, the gateway already
    # has its port from Step 2 and we leave it alone.
    if primary_mode == "dedicated":
        # Gateway port + comms mode live on the primary's OWN bots[] entry —
        # no legacy top-level `evolve` block (evo-account-separation S1).
        # Readers resolve via primary_bot.primary_bot_gateway_port / _comms_mode.
        _primary_bot_cfg = network.setdefault("bots", {}).setdefault(primary_bot_id_choice, {})
        _primary_bot_cfg["port"] = _EVOLVE_PORT
        _primary_bot_cfg.setdefault("role", "primary")
        _primary_bot_cfg.setdefault("user", "evolve")
        _primary_bot_cfg.setdefault("comms_mode", evolve_comms_mode)
        # W10-F #7: re-assert the top-level primary pointer in the dedicated
        # branch's FINAL write (the else branch already does this at the bottom).
        # The Step-14 literal sets it, but pinning it on the last save_network
        # is cheap insurance against any intervening rewrite dropping it —
        # health resolves the sysadmin-alert bot through network["primary"].
        network["primary"] = primary_bot_id_choice
        save_network(network, net_path)
        _ok(f"  network.json updated: primary bot '{primary_bot_id_choice}' port={_EVOLVE_PORT}")
    else:
        # Defensive: make sure the chosen existing bot keeps its primary role.
        network.setdefault("bots", {}).setdefault(primary_bot_id_choice, {})["role"] = "primary"
        network["primary"] = primary_bot_id_choice
        save_network(network, net_path)
        _ok(f"  network.json updated: primary bot is existing '{primary_bot_id_choice}'")

    # ── Install all infra launchd jobs ────────────────────────────────────────
    # This covers audit.py, heal.py, weekly_review.py, backup.py, spend_alert.py,
    # cron_alert.py, analyze/report/outcome/expansion/morning_digest jobs, the
    # admin UI daemon, and all first-party Evolve apps (security-cve-scan, etc.).
    # Must run after _provision_evo_oc so the evolve user and venv exist.
    _info("  Installing Evolve infrastructure jobs and apps...")
    try:
        from .deploy import install_evolve_infra_jobs
        evolve_dir_path = shared_dir  # install_evolve_infra_jobs uses this for log paths
        infra_result = install_evolve_infra_jobs(
            evolve_dir=evolve_dir_path,
            shared_dir=shared_dir,
        )
        for step in infra_result.steps:
            _info(f"    {step}")
        if infra_result.success:
            _ok("  Evolve infrastructure jobs installed")
        else:
            for err in infra_result.errors:
                _warn(f"  Infra job install: {err}")
            _warn("  Some infrastructure jobs may not be active — check 'evolve-admin status'")
    except Exception as _e:
        _warn(f"  Infrastructure job install failed: {_e}")
        _warn("  Run 'sudo evolve-admin install-infra-jobs' to complete this step")

    # Operational thresholds no longer ship as a legacy shared_dir/thresholds.json
    # file. v2 stores them in code (pod_report.DEFAULT_OVERRIDES) merged with
    # network.json → pod_report.thresholds; deploy.py actively removes the old
    # file as obsolete. The previous writer here referenced a long-removed
    # DEFAULT_THRESHOLDS symbol/_meta shape and crashed on a fresh Linux install
    # ("'NoneType' object has no attribute '__dict__'"), writing nothing useful
    # even when it didn't. Dropped — defaults ship in code (W10-E).

    # ── Step 16: Verify ───────────────────────────────────────────────────────
    _step(16, total, "Verify")
    from .deploy import verify_plugin_live
    import time
    time.sleep(2)  # give gateways a moment

    all_live = True

    for bot in bots:
        status = verify_plugin_live(bot.name, bot.port)
        if status:
            _ok(f"{bot.name} gateway: http://localhost:{bot.port}  ({status})")
        else:
            _warn(f"{bot.name} gateway: http://localhost:{bot.port}  — not responding yet (may still be starting)")
            all_live = False

    # The primary bot's gateway (dedicated branch only) was just bootstrapped
    # + immediately kickstarted at the end of step 12 (to reload identity docs).
    # It typically needs 60-90 s to fully initialise — longer than any polling
    # loop here is worth. We skip it in this step and check it after step 17,
    # once enough wall-clock time has passed. For the existing-bot branch the
    # gateway was already verified above.
    if primary_mode == "dedicated":
        _info(f"{primary_bot_id_choice} gateway: http://localhost:{_EVOLVE_PORT}  — starting (verified after step 17)")

    # ── Step 17: Claude Desktop / MCP Bridge (optional) ──────────────────────
    _step(17, total, "Claude Desktop integration (optional)")
    _info("The MCP Bridge lets Claude Desktop on your laptop access this pod's live context —")
    _info("bot memory, tasks, proposals, and handoffs — using your Anthropic subscription.")
    _info("")
    _info("  [dim]Skip this step to configure it later via the admin UI → Maintenance → Claude Access.[/]")
    _info("")

    _existing_mcp = _existing.get("mcp_bridge") or {}
    if isinstance(_existing_mcp, str):
        _existing_mcp = {}

    _default_tailscale = _existing_mcp.get("tailscale_hostname", "") if isinstance(_existing_mcp, dict) else ""
    tailscale_hostname = _sanitize_mcp_hostname(_ask(
        "Tailscale hostname of this machine (e.g. mini.tail1234.ts.net) — blank to skip",
        _default_tailscale,
        non_interactive,
    ))

    if not tailscale_hostname:
        _skip("MCP Bridge skipped — configure later via admin UI if needed")
    else:
        _default_port = str(_existing_mcp.get("port", 5051)) if isinstance(_existing_mcp, dict) else "5051"
        mcp_port_str = _ask("MCP Bridge port", _default_port, non_interactive).strip()
        try:
            mcp_port = int(mcp_port_str)
        except ValueError:
            mcp_port = 5051

        # Pick a sensible default primary bot: the first registered
        # member-role bot, excluding the primary bot and any configured
        # security/audit bot. The exclusion list is data-driven from network
        # config — never hardcoded names.
        _security_bot = (load_network(net_path).get("security") or {}).get("botId") or ""
        _excluded = {primary_bot_id_choice, _security_bot} - {""}
        _bot_names = [b.name for b in bots if b.name not in _excluded]
        _default_primary = (
            _existing_mcp.get("primary_context_bot")
            or (_bot_names[0] if _bot_names else (bots[0].name if bots else None))
        )
        if not _default_primary:
            _warn("No bots configured — skipping MCP primary-context-bot prompt")
            primary_context_bot = ""
        else:
            primary_context_bot = _ask(
                f"Primary context bot [{', '.join(_bot_names)}]",
                _default_primary,
                non_interactive,
            ).strip() or _default_primary

        # Write mcp_bridge config into network.json
        network_now = load_network(net_path)
        network_now.setdefault("mcp_bridge", {}).update({
            "enabled": True,
            "port": mcp_port,
            "primary_context_bot": primary_context_bot,
            "tailscale_hostname": tailscale_hostname,
            "auth": network_now.get("mcp_bridge", {}).get("auth") or {"mode": "tailscale", "api_key": None},
        })
        save_network(network_now, net_path)
        _ok(f"MCP Bridge config saved (primary bot: {primary_context_bot}, port: {mcp_port})")

        # Offer to install the bridge service now
        if _confirm("Install and start MCP Bridge service now?", True, non_interactive):
            from . import mcp_service as _mcp
            with console.status("  Installing MCP Bridge launchd service…"):
                ok, msg = _mcp.install(port=mcp_port, network=net_path)
            if ok:
                _ok(msg)
                _ok(f"CLAUDE.md written to {_mcp._admin_home() / 'CLAUDE.md'}")
            else:
                _warn(f"MCP Bridge install: {msg}")
                _info("  Run manually: evolve-admin mcp-bridge install")
        else:
            _info("  Run later: evolve-admin mcp-bridge install")

        # Print Claude Desktop config snippet
        _info("")
        _info("[bold]Add to Claude Desktop config[/] on your laptop:")
        _info(f"  ~/Library/Application Support/Claude/claude_desktop_config.json")
        _info("")
        snippet = (
            '{\n'
            '  "mcpServers": {\n'
            '    "evolve-pod": {\n'
            f'      "url": "http://{tailscale_hostname}:{mcp_port}/sse"\n'
            '    }\n'
            '  }\n'
            '}'
        )
        console.print(f"[dim]{snippet}[/]")

    # ── Step 18: HTTPS on the LAN (PWA-ready) ────────────────────────────────
    # Sub-spec: docs/spec-pwa-phase0-https-2026-05-18.md §3.4 + §5.4.
    # The MCP-bridge step (17) already resolved `tailscale_hostname`, so by
    # this point we know whether the operator has Tailscale set up. The HTTPS
    # phase wraps `enable_https_if_possible` (which never raises) and prints
    # one of four operator-facing lines per the sub-spec decision tree.
    _step(18, total, "HTTPS on the LAN (for PWA install on phones)")
    _run_https_phase(net_path, non_interactive=non_interactive)

    # ── Summary ───────────────────────────────────────────────────────────────
    # evolve gateway status is verified by the health scan below — it needs the
    # acpx plugin to be fully loaded before /evolve/status responds, which takes
    # ~10 s after the HTTP server binds. The health scan runs after install.json
    # is written, giving enough time, and is authoritative for all 4 gateways.
    console.print()
    if deploy_errors:
        console.print(Panel.fit(
            f"[yellow]Setup completed with {len(deploy_errors)} warning(s).[/]\n\n"
            + "\n".join(f"  • {e}" for e in deploy_errors)
            + "\n\nRe-run [bold]evolve-admin setup --fresh[/] to retry.",
            title="⚡ Setup — Review needed",
        ))
    else:
        _primary_line = (
            f"\n  [green]✅[/] {primary_bot_id_choice} gateway: http://localhost:{_EVOLVE_PORT}"
            if primary_mode == "dedicated"
            else f"\n  [green]✅[/] {primary_bot_id_choice} (primary)"
        )
        console.print(Panel.fit(
            "[green bold]Your pod is running![/]\n\n"
            + "\n".join(
                f"  [green]✅[/] {b.name} gateway: http://localhost:{b.port}"
                for b in bots
            )
            + _primary_line
            + "\n\n"
            "  [green]✅[/] Evolve admin UI: [bold]http://127.0.0.1:5050[/] "
            "(already running as a service)\n"
            "     First visit asks for a pairing code — get one with "
            "[bold]sudo evolve-admin pair[/]\n\n"
            "[dim]Metrics collected nightly at 1am.\n"
            "Analysis + proposals generated Sunday at 2am.[/]\n\n"
            f"Send a message to your bot to get started.",
            title="⚡ Setup Complete",
        ))

    console.print()
    console.print(f"  Network config:      [bold]{net_path}[/]")
    console.print(f"  Shared data:         [bold]{shared_dir}[/]")
    console.print()

    # ── Next steps: open the admin UI from the laptop ─────────────────────────
    # The wizard runs on the deploy box (mini). The admin UI is reached from
    # the operator's laptop over an SSH tunnel. Make the laptop-side
    # one-liner the first thing they see after "Setup Complete".
    try:
        import socket
        _hostname = socket.gethostname().split(".")[0] or "mini"
    except Exception:
        _hostname = "mini"
    console.print(Panel.fit(
        "[bold]From your laptop, open the admin UI:[/]\n\n"
        "  [green]Quickest[/] — forward the port over SSH:\n"
        f"    [bold]ssh -N -L 5050:localhost:5050 {_hostname} &[/]\n"
        f"    [dim]then open[/] [bold]http://localhost:5050[/]\n\n"
        "  [dim]Persistent tunnel that auto-reconnects — clone the evolve repo\n"
        "  on the laptop, pip install packages/analyzer + packages/admin\n"
        "  (evolve-admin is not on PyPI), then:[/]\n"
        f"    [bold]evolve-admin connect --host {_hostname}[/]\n"
        "  [dim]Manage it later:[/] "
        "[bold]evolve-admin connect --status[/] | [bold]--uninstall[/]",
        title="🌐  Open admin UI from your laptop",
    ))
    console.print()

    console.print("  [bold]Useful commands:[/]")
    console.print("    [dim]# Status & monitoring[/]")
    console.print("    [bold]evolve-admin status[/]                   — pod health summary")
    console.print("    [bold]evolve-admin health[/]                   — full scan (add --fix to auto-repair)")
    console.print("    [bold]evolve-admin menu[/]                     — interactive bot-config menu (no syntax to remember)")
    console.print("    [bold]sudo evolve-admin pair[/]                — pairing code for a new browser/device")
    console.print()
    console.print("    [dim]# Restart services[/]")
    console.print("    [bold]evolve-admin restart[/]                  — restart admin UI server")
    console.print("    [bold]sudo evolve-admin restart-gateways[/]    — restart all bot gateways")
    console.print("    [bold]sudo evolve-admin restart-gateways team_bot_a[/] — restart a single gateway")
    console.print()
    # Platform-keyed recovery commands: launchctl on macOS, systemctl on Linux
    # (W10-E — the completion banner used to print launchctl on a Linux pod).
    if _get_profile().name == "linux":
        console.print("    [dim]# Manual systemctl (if needed)[/]")
        console.print("    [bold]sudo systemctl restart ai.openclaw.<bot>-gateway[/]")
        console.print("    [bold]sudo systemctl restart ai.evolve.evolve.admin-ui[/]")
    else:
        console.print("    [dim]# Manual launchctl (if needed)[/]")
        console.print("    [bold]sudo launchctl kickstart -k system/ai.openclaw.<bot>-gateway[/]")
        console.print("    [bold]sudo launchctl kickstart -k system/ai.evolve.evolve.admin-ui[/]")
    console.print()
    console.print("    [dim]# Deploy & upgrade[/]")
    console.print("    [bold]sudo evolve-admin deploy <bot>[/]        — redeploy a specific bot")
    console.print("    [bold]sudo evolve-admin upgrade[/]             — upgrade Evolve to latest version")
    console.print()

    # Write install.json so future runs can detect version + mode.
    # Stamp every bot that is reachable (plugin live) as deployed at the current
    # version so the admin UI doesn't show them as "unknown".
    from .deploy import write_install_json, deploy_stamp
    from datetime import datetime, timezone
    try:
        all_bot_names = [b.name for b in bots]
        if primary_bot_id_choice not in all_bot_names:
            all_bot_names.append(primary_bot_id_choice)
        now = datetime.now(timezone.utc).isoformat()
        # deploy_stamp() carries the monotonic sha/commit_count alongside the
        # display version so get_bot_sync_status can decide synced by identity.
        bot_versions = {
            b.name: deploy_stamp(now)
            for b in bots
            if _plugin_already_installed(b.name, b.port)
        }
        write_install_json(
            shared_dir=shared_dir,
            network_id=pod_name,
            bots=all_bot_names,
            bot_versions=bot_versions,
        )
    except Exception as e:
        _warn(f"Could not write install.json: {e}")

    # ── Post-gateway evolve-access re-assert + verify + heal (W10-G r8/r9) ────
    # The deploy-time set_evolve_read_acl ran BEFORE the gateways started. Each
    # gateway re-hardens its .openclaw to 0700 (clamping evolve's traverse mask)
    # and creates agents/.../auth-profiles.json + the workspace/evolve queue files
    # mode 0700/0600 (clamping their own masks) — the round-6/7/8 EACCES that only
    # surfaces once daemons run. heal_evolve_access re-grants (recomputes every
    # clamped child mask + re-plants the default ACL), explicitly re-widens the
    # .openclaw mask (`setfacl -m m::rwX`), then VERIFIES via the perms seam's
    # getfacl EFFECTIVE check (evolve's own effective perms regardless of who runs
    # the wizard — root here, not evolve — so a direct-IO probe would no-op-pass).
    #
    # Round-9: the gateway re-hardens AGAIN on every `openclaw` invocation against
    # it — and the Telegram channel-add + plugin-install steps BELOW both do — so a
    # single pass here was a FALSE green (evo broke AFTER the round-8 verify). The
    # heal therefore runs in TWO places: HERE (so the health scan's openclaw.json
    # read sees an intact mask) and — load-bearing — as the genuinely-LAST step
    # after plugin install (`_final_evolve_access_pass` call at end of run_setup).
    # Loud, non-fatal — ensure_pod_perms (every deploy) + the hourly
    # pod_perms_drift_monitor also self-heal between runs.
    def _final_evolve_access_pass(when: str) -> None:
        try:
            from .config import get_bot_user as _get_bot_user
            from . import secret_config_perms as _scp
            _net_now = load_network(net_path)
            _access_pairs = [(b.name, b.effective_user) for b in bots]
            if primary_bot_id_choice not in [n for n, _ in _access_pairs]:
                _access_pairs.append((primary_bot_id_choice, _get_bot_user(primary_bot_id_choice, _net_now)))
            for _bid, _buser in _access_pairs:
                if _scp.heal_evolve_access(_bid, _buser):
                    _ok(f"evolve access verified for {_bid} ({when}: traverse + reads secrets + writes queue)")
                else:
                    _warn(f"evolve access still broken for {_bid} ({when}) "
                          "— run `sudo evolve-admin ensure-pod-perms`")
        except Exception as _e:
            _warn(f"evolve-access re-assert skipped ({when}): {_e}")

    console.print()
    console.print("[bold]Verifying evolve access to bot configs (post-gateway)…[/]")
    _final_evolve_access_pass("post-gateway")

    # ── Health scan + fix ─────────────────────────────────────────────────────
    # Run a full health check as a final verification pass so any lingering
    # permission or config issues are surfaced immediately after setup.
    console.print()
    console.print("[bold]Final health scan…[/]")
    try:
        from .health import run_health_check, print_report, apply_all_fixes
        health_report = run_health_check(
            network_path=net_path,
            members_override=[b.name for b in bots],
        )
        print_report(health_report, verbose=False)

        fixable = [c for c in health_report.checks if c.status != "PASS" and c.fix_args]
        if fixable and not non_interactive:
            if _confirm(
                f"Fix {len(fixable)} issue(s) now?",
                default=True,
                non_interactive=non_interactive,
            ):
                fix_results = apply_all_fixes(health_report)
                for fr in fix_results:
                    if fr.ok:
                        _ok(f"{fr.name}: {fr.message}")
                    else:
                        _warn(f"{fr.name}: {fr.message}")
        elif fixable and non_interactive:
            # In non-interactive mode (CI/scripted), apply fixes automatically
            apply_all_fixes(health_report)

        still_failing = [c for c in health_report.checks if c.status == "FAIL"]
        still_warned = [c for c in health_report.checks if c.status == "WARN"]
        if still_failing:
            _warn(f"{len(still_failing)} failure(s) remain after auto-fix:")
            for c in still_failing:
                _warn(f"  ✗ {c.name} — {c.detail}")
            _warn("Re-run: [bold]evolve-admin health --fix[/]")
        if still_warned:
            _warn(f"{len(still_warned)} warning(s) need attention:")
            for c in still_warned:
                fix_hint = f"  Fix: {c.fix_cmd}" if c.fix_cmd else ""
                _warn(f"  ⚠ {c.name} — {c.detail}{fix_hint}")
    except Exception as e:
        _warn(f"Health scan failed: {e}")

    # ── Telegram channel registration (deferred) ──────────────────────────────
    # The gateway may not have been reachable when _provision_evo_oc ran.
    # Now that the health scan has ensured it's up, try channel registration
    # against the gateway account — evolve on macOS, evo (day-one) on Linux.
    # Only the dedicated branch provisioned the primary gateway here.
    if evolve_telegram_token and primary_mode == "dedicated":
        _register_primary_telegram_channel(
            primary_account, primary_bot_id_choice, evolve_telegram_token,
        )

    # ── Install evolve plugin on the primary bot ────────────────────────────
    # Dedicated branch only — the existing-bot path leaves the chosen bot's
    # plugins alone (the plugin gets installed via the normal deploy_bot
    # flow during Step 13, just like any other member bot).
    if primary_mode == "dedicated":
        _info(f"Installing Evolve plugin on {primary_bot_id_choice} gateway...")
        try:
            from .deploy import install_oc_plugin, ensure_plugin_config
            # Materialize the evolve plugin config (botId, role, networkId, …)
            # BEFORE `openclaw plugins install` runs — install validates the
            # plugin's config block against the schema (botId required) and the
            # member-bot path (Step 13) seeds it first for exactly this reason.
            # The evo path used to skip this pre-step and relied on
            # _evolve_openclaw_config's seed, which `plugins install` could reset
            # to {} → "invalid config: must have required property 'botId'"
            # (W10-E). network now carries the primary bot entry (written above
            # in Step 14), so botId resolves correctly.
            ensure_plugin_config(primary_bot_id_choice, network)
            install_oc_plugin(primary_bot_id_choice, port=_EVOLVE_PORT, network=network)
            _ok(f"Evolve plugin installed on {primary_bot_id_choice} gateway")
        except Exception as _e:
            _warn(f"Evolve plugin install failed: {_e}")
            _warn(f"Run manually: sudo evolve-admin deploy --bot {primary_bot_id_choice}")

    # ── Final evolve-access pass — the genuinely-LAST perms step (W10-G r9) ────
    # Must run AFTER the Telegram channel-add + plugin-install above, both of which
    # invoke `openclaw` against the primary gateway → the gateway re-hardens its
    # .openclaw to 0700 → the chmod's zeroed group bits re-clamp evolve's ACL mask
    # to --- → defer-runner + manifest-reflex-runner PermissionError on the queue.
    # The round-8 post-gateway verify ran BEFORE these steps, so it was a false
    # green. This heal+verify is the load-bearing one (re-grant + `setfacl -m
    # m::rwX` + EFFECTIVE re-verify); the hourly pod_perms_drift_monitor keeps it
    # healed against any later gateway restart.
    console.print()
    console.print("[bold]Final evolve-access check (after primary hardening)…[/]")
    _final_evolve_access_pass("final, post-hardening")

    # Bring-up complete: first full deploy + access-verify succeeded. Stamp the
    # pod as settled so monitors resume firing transient setup-state findings
    # (ACL/perms) normally. Idempotent. See docs/spec-pod-bringup-settle-2026-06-23.md.
    try:
        from signals.settle_gate import mark_settled
        mark_settled(shared_dir, trigger="fresh-wizard")
    except Exception as _e:
        _warn(f"Could not stamp pod-settled marker: {_e}")
