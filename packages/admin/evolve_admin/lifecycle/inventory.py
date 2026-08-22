"""Bot artifact discovery — pure read-only inventory of everything that
belongs to a given bot, classified by which lifecycle action removes it.

The discovery layer is the unblocker for all three lifecycle paths
(detach / archive / delete). Operator runs:

    evolve-admin lifecycle inventory <bot>

and gets the full picture of where this bot lives: launchd plists,
openclaw config, crons, channels (with off-host integration warnings),
workspace credentials, signal/proposal state, etc. Each item carries
``removed_by`` flags (DETACH / ARCHIVE / DELETE) so the operator can
see exactly what each action would touch — and what they'll need to
clean up manually off-host.

Pure read-only: no subprocess calls except non-mutating directory
walks, no file writes, no network. Safe to run from any context.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, cast


# ─────────────────────────────────────────────────────────────────────────────
# Enums + datamodel
# ─────────────────────────────────────────────────────────────────────────────


class LifecycleAction(str, Enum):
    """One of the three lifecycle paths."""
    DETACH = "detach"      # remove Evolve from a still-running bot
    ARCHIVE = "archive"    # graceful retirement; reversible via restore
    DELETE = "delete"      # irreversible total removal (not yet implemented)


class ItemCategory(str, Enum):
    """Coarse buckets for inventory items so the renderer can group them."""
    NETWORK = "network"                # network.json membership / role
    MACOS_USER = "macos_user"           # OS account state
    LAUNCHD = "launchd"                 # per-bot launchd plists
    OPENCLAW_CONFIG = "openclaw_config" # openclaw.json fragments
    OPENCLAW_CRON = "openclaw_cron"     # OC-managed cron entries
    CHANNEL = "channel"                  # outbound integration (telegram, slack, etc.)
    WORKSPACE = "workspace"             # workspace dir + custom content
    CREDENTIAL = "credential"           # credentials/.env/secret files
    BACKUP = "backup"                   # backup repo URL + SSH deploy key
    SIGNAL = "signal"                   # firing/snoozed signals tagged bot_id
    PROPOSAL = "proposal"               # pending/approved proposals targeting bot
    INTENT = "intent"                    # config_intents declared overrides
    OFF_HOST = "off_host"               # external state Evolve cannot touch


@dataclass
class InventoryItem:
    """One discoverable thing belonging to the bot.

    ``removed_by`` is the set of lifecycle actions that destroy this item.
    A ``DETACH`` entry implies ``ARCHIVE`` and ``DELETE`` (those are
    supersets) — but the renderer expands explicitly so the operator
    never has to know the containment relationship.

    ``manual_action`` is a non-empty string when the item requires
    operator action outside Evolve's control (e.g. delete the GitHub
    backup repo, deactivate the Telegram bot via @BotFather). It's the
    text shown in the manual-cleanup checklist.
    """
    category: ItemCategory
    name: str                                  # short identifier
    detail: str = ""                            # human-readable specifics
    removed_by: frozenset[LifecycleAction] = field(default_factory=frozenset)
    manual_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "name": self.name,
            "detail": self.detail,
            "removed_by": sorted(a.value for a in self.removed_by),
            "manual_action": self.manual_action,
        }


@dataclass
class BotInventory:
    """The full discovery output for one bot."""
    bot_id: str
    macos_user: str | None
    is_primary: bool
    items: list[InventoryItem] = field(default_factory=list)
    # Top-level summary fields the renderer surfaces above the item list.
    summary: dict[str, Any] = field(default_factory=dict)

    def items_for(self, action: LifecycleAction) -> list[InventoryItem]:
        return [it for it in self.items if action in it.removed_by]

    def manual_cleanup(self) -> list[InventoryItem]:
        return [it for it in self.items if it.manual_action]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "macos_user": self.macos_user,
            "is_primary": self.is_primary,
            "summary": self.summary,
            "items": [it.to_dict() for it in self.items],
        }


# Convenience constants — common removed_by combinations.
_DETACH_AND_UP: frozenset[LifecycleAction] = frozenset({
    LifecycleAction.DETACH, LifecycleAction.ARCHIVE, LifecycleAction.DELETE,
})
_ARCHIVE_AND_UP: frozenset[LifecycleAction] = frozenset({
    LifecycleAction.ARCHIVE, LifecycleAction.DELETE,
})
_DELETE_ONLY: frozenset[LifecycleAction] = frozenset({LifecycleAction.DELETE})
_NONE: frozenset[LifecycleAction] = frozenset()


# ─────────────────────────────────────────────────────────────────────────────
# Top-level: compile_bot_inventory
# ─────────────────────────────────────────────────────────────────────────────


def compile_bot_inventory(
    bot_id: str,
    network: dict[str, Any] | None = None,
    *,
    shared_dir: Path | None = None,
    launchd_dir: Path = Path("/Library/LaunchDaemons"),
    home_resolver=None,
    user_resolver=None,
) -> BotInventory:
    """Walk every place artifacts for ``bot_id`` can hide.

    ``network`` defaults to the loaded ``network.json``.
    ``shared_dir`` defaults to network's ``sharedDir`` setting.
    ``launchd_dir`` is overridable for tests.
    ``home_resolver`` / ``user_resolver`` accept callables for tests; in
    production they fall back to ``evolve_config.bot_home`` and
    ``evolve_config.get_bot_user``.

    Returns a fully-populated :class:`BotInventory`. Never raises — any
    component that fails to read is silently omitted (the operator
    notices via the missing category, not a stack trace).
    """
    if network is None:
        network = _load_network_safely()
    if home_resolver is None or user_resolver is None:
        bh, gu = _default_resolvers()
        home_resolver = home_resolver or bh
        user_resolver = user_resolver or gu

    bots_cfg = (network.get("bots") or {}) if isinstance(network, dict) else {}
    bot_cfg = bots_cfg.get(bot_id) or {}
    members = network.get("members") or [] if isinstance(network, dict) else []
    is_primary = (network or {}).get("primary") == bot_id

    macos_user = _safe_call(user_resolver, bot_id, network) or bot_id
    home = _safe_call(home_resolver, bot_id, network) or Path(f"/Users/{macos_user}")
    shared_dir = shared_dir or Path(
        (network or {}).get("sharedDir", "/Users/Shared/evolve")
    )

    inv = BotInventory(
        bot_id=bot_id,
        macos_user=macos_user,
        is_primary=is_primary,
    )

    # ── 1. network.json membership ────────────────────────────────────
    if bot_id in members or bot_id in bots_cfg:
        inv.items.append(InventoryItem(
            category=ItemCategory.NETWORK,
            name="network.json membership",
            detail=(
                f"role={bot_cfg.get('role', 'member')} "
                f"port={bot_cfg.get('port', '?')} "
                + (f"primary=yes " if is_primary else "")
                + ("(member) " if bot_id in members else "(bots-only) ")
            ),
            removed_by=_ARCHIVE_AND_UP,  # detach flips evolve_disabled flag instead
        ))

    # ── 2. macOS user ────────────────────────────────────────────────
    home_exists = home.exists()
    inv.items.append(InventoryItem(
        category=ItemCategory.MACOS_USER,
        name=f"macOS user {macos_user!r}",
        detail=(
            f"home={home} "
            + ("(present)" if home_exists else "(home dir missing)")
        ),
        removed_by=_DELETE_ONLY,
    ))

    # ── 3. Per-bot launchd plists ────────────────────────────────────
    inv.items.extend(_discover_launchd_plists(bot_id, launchd_dir))

    # ── 4. openclaw.json fragments + channels + intents ─────────────
    inv.items.extend(_discover_openclaw_config(home, bot_cfg))

    # ── 5. openclaw cron entries ─────────────────────────────────────
    inv.items.extend(_discover_openclaw_crons(home))

    # ── 6. Workspace credentials + secrets ───────────────────────────
    inv.items.extend(_discover_workspace_secrets(home))

    # ── 7. Backup repo + SSH deploy key ──────────────────────────────
    inv.items.extend(_discover_backup_state(bot_id, bot_cfg))

    # ── 8. Signals + proposals tagged with this bot ──────────────────
    inv.items.extend(_discover_signals(shared_dir, bot_id))
    inv.items.extend(_discover_proposals(shared_dir, bot_id))

    # ── 9. config_intents ────────────────────────────────────────────
    inv.items.extend(_discover_config_intents(bot_cfg))

    # ── 10. Workspace dir summary ────────────────────────────────────
    inv.items.extend(_discover_workspace_summary(home))

    # Top-level summary for the renderer
    inv.summary = _build_summary(inv)
    return inv


# ─────────────────────────────────────────────────────────────────────────────
# Discovery helpers — each returns a list[InventoryItem]; each handles its own
# read failures by emitting nothing for the affected category.
# ─────────────────────────────────────────────────────────────────────────────


# Prefixes used for per-bot Evolve launchd labels. ai.openclaw.<bot>-gateway
# and -healthcheck are non-evolve (OpenClaw core), but they're still per-bot
# and the operator wants to see them. ai.evolve.<bot>.* and
# ai.openclaw.evolve.*.<bot> are Evolve-installed.
_BOT_LABEL_PATTERNS: list[tuple[str, frozenset[LifecycleAction]]] = [
    # OpenClaw core per-bot — gateway + healthcheck. ARCHIVE stops them
    # (retire-bot.stop_bot_services); DELETE removes the plists too.
    (r"^ai\.openclaw\.{bot}-gateway$", _ARCHIVE_AND_UP),
    (r"^ai\.openclaw\.{bot}-healthcheck$", _ARCHIVE_AND_UP),
    # Evolve-installed per-bot infra. DETACH removes these too — that's
    # the whole point of detach (strip Evolve, leave OpenClaw alone).
    (r"^ai\.evolve\.{bot}\..*$", _DETACH_AND_UP),
    (r"^ai\.openclaw\.evolve\..*\.{bot}$", _DETACH_AND_UP),
]


def _discover_launchd_plists(bot_id: str, launchd_dir: Path) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    if not launchd_dir.exists():
        return items
    # The regex set is per-bot; build them once.
    compiled = [
        (re.compile(p.format(bot=re.escape(bot_id))), removed_by)
        for p, removed_by in _BOT_LABEL_PATTERNS
    ]
    try:
        plists = sorted(launchd_dir.glob("*.plist"))
    except OSError:
        return items
    for plist in plists:
        label = plist.stem
        for pattern, removed_by in compiled:
            if pattern.match(label):
                items.append(InventoryItem(
                    category=ItemCategory.LAUNCHD,
                    name=label,
                    detail=str(plist),
                    removed_by=removed_by,
                ))
                break
    return items


def _discover_openclaw_config(
    home: Path, bot_cfg: dict[str, Any],
) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    oc_json_path = home / ".openclaw" / "openclaw.json"
    oc_data = _read_json(oc_json_path)
    if not isinstance(oc_data, dict):
        return items

    # The openclaw.json file itself — survives detach (cleaned), removed on archive/delete
    items.append(InventoryItem(
        category=ItemCategory.OPENCLAW_CONFIG,
        name="openclaw.json",
        detail=str(oc_json_path),
        removed_by=_ARCHIVE_AND_UP,
    ))

    # The evolve plugin block specifically — detach strips this in place.
    plugins_block = oc_data.get("plugins")
    if isinstance(plugins_block, dict):
        entries = plugins_block.get("entries") or {}
        if "evolve" in entries:
            items.append(InventoryItem(
                category=ItemCategory.OPENCLAW_CONFIG,
                name="plugins.entries.evolve",
                detail="Evolve plugin runtime config block",
                removed_by=_DETACH_AND_UP,
            ))
    elif isinstance(plugins_block, list):
        if any(
            p == "evolve" or (isinstance(p, dict) and p.get("id") == "evolve")
            for p in plugins_block
        ):
            items.append(InventoryItem(
                category=ItemCategory.OPENCLAW_CONFIG,
                name="plugins[] evolve entry",
                detail="Evolve plugin in legacy list-shape config",
                removed_by=_DETACH_AND_UP,
            ))

    # Channels — outbound integrations the operator must clean off-host.
    channels = oc_data.get("channels") or {}
    if isinstance(channels, dict):
        for ch_name, ch_cfg in channels.items():
            if not isinstance(ch_cfg, dict) or not ch_cfg.get("enabled"):
                continue
            items.append(_channel_item(ch_name, ch_cfg))

    # Exec policy — useful context for the operator.
    exec_policy = (oc_data.get("tools") or {}).get("exec") or {}
    if exec_policy:
        items.append(InventoryItem(
            category=ItemCategory.OPENCLAW_CONFIG,
            name="tools.exec policy",
            detail=f"security={exec_policy.get('security', '?')}",
            removed_by=_ARCHIVE_AND_UP,
        ))

    return items


def _channel_item(channel_name: str, ch_cfg: dict[str, Any]) -> InventoryItem:
    """Build a channel item with a manual_action callout for external state.

    Each channel type maps to a specific off-host cleanup playbook so the
    operator knows what to do outside Evolve's control.
    """
    name = channel_name
    detail_bits = []
    manual = ""
    if channel_name == "telegram":
        if ch_cfg.get("botToken"):
            detail_bits.append("bot token configured")
            manual = (
                "Deactivate this bot's Telegram bot via @BotFather "
                "(/deletebot then enter the bot username). The token "
                "in openclaw.json will keep working until you do this."
            )
    elif channel_name == "slack":
        manual = (
            "Revoke this bot's Slack workspace credentials at "
            "https://api.slack.com/apps (find the app, then Settings → "
            "Basic Information → Delete App). Also revoke any user "
            "tokens issued to it."
        )
    elif channel_name == "discord":
        manual = (
            "Remove this bot from the Discord developer portal "
            "(https://discord.com/developers/applications). The bot "
            "token remains valid until the app is deleted."
        )
    elif channel_name == "github":
        manual = (
            "If this bot used a personal GitHub access token, revoke it "
            "at https://github.com/settings/tokens. If it used a GitHub "
            "App, manage in https://github.com/settings/apps."
        )
    return InventoryItem(
        category=ItemCategory.CHANNEL,
        name=f"channel:{name}",
        detail=", ".join(detail_bits) or "(enabled)",
        # Channel config in openclaw.json gets archived/deleted with the
        # config file; detach strips evolve but leaves channels intact
        # because the bot keeps running and may still use them.
        removed_by=_ARCHIVE_AND_UP,
        manual_action=manual,
    )


def _discover_openclaw_crons(home: Path) -> list[InventoryItem]:
    """Read ~bot/.openclaw/cron/jobs.json and emit one item per job."""
    items: list[InventoryItem] = []
    jobs_path = home / ".openclaw" / "cron" / "jobs.json"
    data = _read_json(jobs_path)
    if not isinstance(data, dict):
        return items
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return items
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name") or job.get("id") or "(unnamed)"
        enabled = job.get("enabled", True)
        schedule = job.get("schedule") or "(no schedule)"
        items.append(InventoryItem(
            category=ItemCategory.OPENCLAW_CRON,
            name=f"oc-cron:{name}",
            detail=f"schedule={schedule} enabled={enabled}",
            # Crons stay on detach (they're the bot's; Evolve doesn't own
            # them). Archive nukes the workspace which includes them.
            removed_by=_ARCHIVE_AND_UP,
        ))
    return items


def _discover_workspace_secrets(home: Path) -> list[InventoryItem]:
    """Enumerate credentialed files under workspace/."""
    items: list[InventoryItem] = []
    workspace = home / ".openclaw" / "workspace"
    if not workspace.exists():
        return items
    # The credential locations Evolve's integration probes already look
    # at. Each one represents a different cleanup path off-host.
    candidates: list[tuple[Path, str, str]] = [
        (workspace / "credentials",
         "workspace/credentials/",
         "Plugin-managed OAuth secrets / tokens / service-account JSONs"),
        (workspace / ".env",
         "workspace/.env",
         "Dotenv with provider API keys (Slack, Telegram, etc.)"),
        (workspace / "manifests",
         "workspace/manifests/",
         "Bot-authored integration manifests (declarative, not secret)"),
        (home / ".config" / "gws",
         ".config/gws/",
         "Legacy Google Workspace OAuth tokens (oc gws --reauth)"),
        (home / ".dropbox",
         ".dropbox/",
         "Dropbox desktop sync metadata"),
        (home / ".config" / "gh",
         ".config/gh/",
         "GitHub CLI auth (token may be in keychain)"),
        (home / ".ssh",
         ".ssh/",
         "SSH keys (may include deploy keys for external repos)"),
    ]
    for path, display_name, description in candidates:
        if not _path_has_content(path):
            continue
        # workspace/manifests is internal-only; the rest are external
        # credential surfaces that need off-host attention.
        is_external_cred = "manifests" not in display_name
        items.append(InventoryItem(
            category=ItemCategory.CREDENTIAL,
            name=display_name,
            detail=description,
            removed_by=_ARCHIVE_AND_UP,
            manual_action=(
                "Review for credentials that need rotation/revocation "
                "off-host (e.g. revoke API keys at the provider, "
                "remove the bot's SSH key from external repos)."
                if is_external_cred else ""
            ),
        ))
    return items


def _discover_backup_state(bot_id: str, bot_cfg: dict[str, Any]) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    backup_url = bot_cfg.get("backupRepoUrl")
    if backup_url:
        items.append(InventoryItem(
            category=ItemCategory.BACKUP,
            name="backup repo",
            detail=backup_url,
            removed_by=_NONE,  # never auto-deleted; always operator's call
            manual_action=(
                f"Backup repo {backup_url} is not auto-deleted by any "
                "lifecycle action. If you want to remove it, delete the "
                "repo at GitHub and revoke the bot's deploy key from "
                "the repo's Settings → Deploy keys page."
            ),
        ))
    # SSH deploy key — the evolve user holds it. Path naming convention:
    # /Users/evolve/.ssh/evolve-backup-<bot_id>
    deploy_key = Path(f"/Users/evolve/.ssh/evolve-backup-{bot_id}")
    if deploy_key.exists():
        items.append(InventoryItem(
            category=ItemCategory.BACKUP,
            name="ssh deploy key",
            detail=str(deploy_key),
            removed_by=_DELETE_ONLY,
            manual_action=(
                "Also remove the matching public key from the GitHub "
                "repo's Settings → Deploy keys page — the private key "
                "deletion alone doesn't sever the access grant on the "
                "remote side."
            ),
        ))
    return items


def _discover_signals(shared_dir: Path, bot_id: str) -> list[InventoryItem]:
    """Count firing/snoozed signals tagged with this bot_id.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): per-subdir counts come from ``signals.store.iter_signals``,
    falling back to a raw per-subdir read only when the analyzer signals
    package isn't importable.
    """
    items: list[InventoryItem] = []
    for state, subdir in (("firing", "firing"), ("snoozed", "snoozed")):
        count = _count_signals_for_bot_subdir(shared_dir, subdir, bot_id)
        if count == 0:
            continue
        items.append(InventoryItem(
            category=ItemCategory.SIGNAL,
            name=f"signals ({state})",
            detail=f"{count} signal(s) tagged bot_id={bot_id!r}",
            # Detach (remove-evolve) sweeps these. Archive does too.
            removed_by=_DETACH_AND_UP,
        ))
    return items


def _discover_proposals(shared_dir: Path, bot_id: str) -> list[InventoryItem]:
    """Count proposals targeting this bot.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): per-subdir counts come from ``arbiter.store.iter_proposals``,
    falling back to a raw per-subdir read only when the analyzer arbiter
    package isn't importable.
    """
    items: list[InventoryItem] = []
    counts: dict[str, int] = {}
    for sub in ("pending", "snoozed", "applied", "archived"):
        n = _count_proposals_for_bot_subdir(shared_dir, sub, bot_id)
        if n:
            counts[sub] = n
    for sub, n in counts.items():
        items.append(InventoryItem(
            category=ItemCategory.PROPOSAL,
            name=f"proposals ({sub})",
            detail=f"{n} proposal(s) targeting {bot_id!r}",
            removed_by=_ARCHIVE_AND_UP,
        ))
    return items


def _discover_config_intents(bot_cfg: dict[str, Any]) -> list[InventoryItem]:
    intents = bot_cfg.get("config_intents")
    if not isinstance(intents, list):
        return []
    items: list[InventoryItem] = []
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        items.append(InventoryItem(
            category=ItemCategory.INTENT,
            name=f"intent: {intent.get('field', '?')}",
            detail=(
                f"value={intent.get('value', '?')} "
                f"reason_id={intent.get('reason_id', '?')}"
            ),
            removed_by=_ARCHIVE_AND_UP,
        ))
    return items


def _discover_workspace_summary(home: Path) -> list[InventoryItem]:
    workspace = home / ".openclaw" / "workspace"
    if not workspace.exists():
        return []
    try:
        # Cheap walk — file count + total bytes. Don't include hidden files.
        file_count = 0
        total_bytes = 0
        for root, dirs, files in os.walk(workspace):
            for f in files:
                if f.startswith("."):
                    continue
                p = Path(root) / f
                try:
                    total_bytes += p.stat().st_size
                    file_count += 1
                except OSError:
                    continue
    except OSError:
        return []
    return [InventoryItem(
        category=ItemCategory.WORKSPACE,
        name="workspace contents",
        detail=(
            f"{file_count} file(s), "
            f"{_human_size(total_bytes)} under {workspace}"
        ),
        removed_by=_ARCHIVE_AND_UP,
    )]


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────


def _build_summary(inv: BotInventory) -> dict[str, Any]:
    """Top-line counts the renderer surfaces above the per-item list."""
    out: dict[str, Any] = {"total_items": len(inv.items)}
    for action in LifecycleAction:
        out[f"removed_by_{action.value}"] = len(inv.items_for(action))
    out["manual_cleanup_items"] = len(inv.manual_cleanup())
    return out


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _path_has_content(path: Path) -> bool:
    """True if ``path`` exists AND is non-empty (file with bytes, dir with entries)."""
    try:
        if not path.exists():
            return False
        if path.is_file():
            return path.stat().st_size > 0
        if path.is_dir():
            return any(True for _ in path.iterdir())
    except OSError:
        return False
    return False


def _count_signals_for_bot_subdir(shared_dir: Path, subdir: str, bot_id: str) -> int:
    """Count Signals in one signal-store subdir whose bot_id == bot_id.

    Store-first (``signals.store.iter_signals``); raw read of the subdir
    is the fallback when the analyzer signals package isn't importable.
    """
    def _raw_count() -> int:
        d = shared_dir / "signals" / subdir  # store-access-lint: analyzer-unavailable fallback
        if not d.exists():
            return 0
        n = 0
        try:
            for p in d.iterdir():
                if not p.name.endswith(".json"):
                    continue
                data = _read_json(p)
                if isinstance(data, dict) and data.get("bot_id") == bot_id:
                    n += 1
        except OSError:
            return 0
        return n

    try:
        from signals import store as _signals_store  # type: ignore[import-not-found]
    except Exception:
        return _raw_count()
    try:
        return sum(
            1
            for sig in _signals_store.iter_signals(
                shared_dir, subdirs=cast("Any", (subdir,))
            )
            if sig.bot_id == bot_id
        )
    except Exception:
        return _raw_count()


def _count_proposals_for_bot_subdir(shared_dir: Path, subdir: str, bot_id: str) -> int:
    """Count Proposals in one proposal-store subdir targeting ``bot_id``.

    Store-first (``arbiter.store.iter_proposals``); raw read of the subdir
    is the fallback when the analyzer arbiter package isn't importable.
    Proposals may carry the target as ``target_bot`` or ``bot_id``
    depending on the generator — be liberal, matching the pre-Phase-B
    behavior.
    """
    def _raw_count() -> int:
        d = shared_dir / "proposals" / subdir  # store-access-lint: analyzer-unavailable fallback
        if not d.exists():
            return 0
        n = 0
        try:
            for p in d.iterdir():
                if not p.name.endswith(".json"):
                    continue
                data = _read_json(p)
                if isinstance(data, dict):
                    target = data.get("target_bot") or data.get("bot_id")
                    if target == bot_id:
                        n += 1
        except OSError:
            return 0
        return n

    try:
        from arbiter import store as _arbiter_store  # type: ignore[import-not-found]
    except Exception:
        return _raw_count()
    try:
        n = 0
        for proposal in _arbiter_store.iter_proposals(
            shared_dir, subdirs=cast("Any", (subdir,))
        ):
            data = proposal.to_dict()
            target = data.get("target_bot") or data.get("bot_id")
            if target == bot_id:
                n += 1
        return n
    except Exception:
        return _raw_count()


def _human_size(n: int) -> str:
    """Format a byte count as KB / MB / GB."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if isinstance(n, float) else f"{n}{unit}"
        n = n / 1024
    return f"{n:.1f}TB"


def _safe_call(fn, *args, **kwargs):
    """Call ``fn`` swallowing exceptions; return None on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _load_network_safely() -> dict[str, Any]:
    """Lazy-import network loader; return {} on any failure."""
    try:
        from ..config import load_network, DEFAULT_NETWORK_CONFIG
        return load_network(DEFAULT_NETWORK_CONFIG) or {}
    except Exception:
        return {}


def _default_resolvers():
    """Lazy-import bot_home / get_bot_user defaults."""
    try:
        from ..config import bot_home, get_bot_user
        return bot_home, get_bot_user
    except Exception:
        def _fallback_home(bot_id, network):
            return Path(f"/Users/{bot_id}")
        def _fallback_user(bot_id, network):
            return bot_id
        return _fallback_home, _fallback_user
