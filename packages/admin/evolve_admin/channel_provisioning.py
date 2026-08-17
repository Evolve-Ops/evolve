"""evolve_admin.channel_provisioning — add a messaging channel to an EXISTING bot.

Spec: docs/spec-users-meta-2026-06-15.md §"M1 — Multi-platform messaging per bot".

The gap this closes
--------------------
OpenClaw runs channels concurrently ("configure multiple and OpenClaw will
route per chat"). Evolve never had a path to reach that state: the install
wizard picks ONE channel per bot (``pairing/config.py``), and every subsequent
channel-config writer in the tree is a per-platform install flow
(``skills/telegram_install``, ``skills/slack_install``, …) reachable only from
that platform's own Skills-page route. There was no channel-agnostic
"bot B should also be on channel C" operation.

This module is that operation, and ONLY that operation. It is deliberately
thin: every mechanic it needs already exists and is owned by someone else.

  * **What channel C is** — ``channel_registry``. Never a literal. The
    registry's ``install`` column decides whether an OC plugin install is
    even needed (invariant 7; ``tools/channel-literal-lint`` enforces it).
  * **Installing an OC plugin** — ``oc_neutralize.install_externalized_plugin``
    (META:skills owns plugin install + trust; we call their helper, we do not
    grow a second install path).
  * **Reading/writing the bot's openclaw.json** — ``skills._oc_install_common``
    (``/tmp`` stage → ``sudo /bin/cp`` → ``chown`` → ``chmod 600``, plus the
    first-messaging-channel activation hook). openclaw.json is token-bearing;
    that helper is the one 0600-correct writer.
  * **Placing a credential** — ``web.server._apply_credential_to_oc_dict``,
    the ``_RUNTIME_MIRROR_PATH`` registry. We never hand-write a token field:
    the discord ``botToken``-vs-``token`` incident (skills-deep-audit
    2026-05-30 P0-3) is what hand-written token keys cost.

Merge, never replace
---------------------
The channel block is merged with ``setdefault`` into the EXISTING ``channels``
map, exactly like the per-platform install flows. Operator-set fields
(``mode``, ``allowFrom``, ``appToken``, a channel allowlist) survive a redo.
Re-running with the same inputs writes nothing at all — the read-back diff is
the idempotence check, so a no-op call does not burn a sudo round-trip or bump
the mtime the gateway watches.

Does a deploy clobber this?  **No.**
-------------------------------------
Verified against ``deploy.py`` at the time of writing: deploy never
regenerates openclaw.json from a template or a desired-state channel list. The
only deploy-path writer, ``ensure_plugin_config`` (deploy.py ~2109), reads the
live file (``json.loads``), mutates that same dict, and hands it to
``safe_write_bot_config``. The single ``channels`` mutation in the whole file
is a field-name repair on ``channels.telegram`` guarded on the block already
existing. Nothing prunes unknown channel ids. The materializer's scope is
``plugins.entries.evolve.config`` plus a fixed dotpath list, none of which is
under ``channels``.

Two non-deploy paths DO rewrite ``channels`` wholesale and are worth knowing:
``setup_wizard._provision_evo_oc`` (re-running wizard evo-provisioning rebuilds
the evo account's config from scratch) and ``oc_neutralize.strip_externalized_refs``
(flips ``enabled`` false during the OC-upgrade neutralize dance). Neither is
``deploy_bot``.

Restarts
---------
This module does NOT restart a gateway unless the caller passes
``restart_gateway=True``. A channel written to disk is inert until the gateway
re-reads it, so the outcome always reports ``restart_required`` — that is the
caller's decision to act on, not ours to make on a live pod.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import channel_registry

log = logging.getLogger(__name__)


# ── Plugin-install outcome vocabulary ───────────────────────────────────
#
# Which of these a run reports is decided by the registry's ``install``
# column, never by the channel id. INSTALL_CORE channels are bundled with
# OpenClaw and take the NOT_REQUIRED branch; the plugin classes take the
# install branch. Getting that backwards means either a pointless npm
# install on every Telegram add or a channel that never loads.
PLUGIN_NOT_REQUIRED = "not_required"      # registry says install=core
PLUGIN_ALREADY_INSTALLED = "already_installed"
PLUGIN_INSTALLED = "installed"
PLUGIN_SKIPPED = "skipped"                # caller passed install_plugin=False
PLUGIN_FAILED = "failed"

#: Channel-block keys the install flows all seed. Values are only ever
#: applied with ``setdefault`` — an operator-set value always wins.
_STREAMING_OFF = {"mode": "off"}
_DM_POLICY_PAIRING = "pairing"
_GROUP_POLICY_ALLOWLIST = "allowlist"


@dataclass
class AddChannelOutcome:
    """What happened, and what the caller must still do.

    ``ok`` is True only when the channel block is present and correct on
    disk. ``restart_required`` is True whenever the on-disk config changed
    and the gateway has not been restarted by us — the channel exists but
    is not live until something restarts it.
    """

    ok: bool
    bot_id: str
    channel_id: str
    plugin_state: str = PLUGIN_NOT_REQUIRED
    config_changed: bool = False
    credential_applied: bool = False
    credential_pending: bool = False
    restart_required: bool = False
    gateway_restarted: bool = False
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape (what the admin route returns)."""
        return {
            "ok": self.ok,
            "bot_id": self.bot_id,
            "channel_id": self.channel_id,
            "plugin_state": self.plugin_state,
            "config_changed": self.config_changed,
            "credential_applied": self.credential_applied,
            "credential_pending": self.credential_pending,
            "restart_required": self.restart_required,
            "gateway_restarted": self.gateway_restarted,
            "error": self.error,
            "notes": list(self.notes),
        }


def default_channel_fields(spec: channel_registry.ChannelSpec) -> dict[str, Any]:
    """Seed fields for a new ``channels.<id>`` block, derived from the
    registry's capability columns rather than from the channel's name.

    ``enabled`` is the only unconditional field. The two policy fields are
    gated on the capability that makes them meaningful: a ``dmPolicy`` of
    "pairing" presumes an allowlist to pair INTO, and ``groupPolicy`` is
    nonsense on a channel with no group concept. A channel whose registry
    row says it does neither (e.g. the SMS row) gets a bare enabled block
    instead of two policy keys OC would reject.

    Callers override or extend via ``add_channel_to_bot(channel_fields=…)``.
    """
    fields: dict[str, Any] = {"enabled": True, "streaming": dict(_STREAMING_OFF)}
    if spec.supports_dms and spec.supports_allowlist:
        fields["dmPolicy"] = _DM_POLICY_PAIRING
    if spec.supports_groups and spec.supports_allowlist:
        fields["groupPolicy"] = _GROUP_POLICY_ALLOWLIST
    return fields


def channel_needs_plugin_install(spec: channel_registry.ChannelSpec) -> bool:
    """True iff adding this channel requires an ``openclaw plugins install``.

    Reads the registry's ``install`` column:

      * ``core``            → False. Bundled with OpenClaw; the channel block
                              alone is enough.
      * ``official-plugin`` → True (``@openclaw/*`` on npm/clawhub).
      * ``external-plugin`` → True (third-party namespace).
      * ``None``            → False, but the channel is not provisionable at
                              all — :func:`add_channel_to_bot` rejects it
                              earlier, before this is reached.
    """
    return spec.install in (
        channel_registry.INSTALL_OFFICIAL_PLUGIN,
        channel_registry.INSTALL_EXTERNAL_PLUGIN,
    )


# ── Injectable seams ────────────────────────────────────────────────────
#
# Production wiring is resolved lazily inside each helper so importing this
# module stays cheap (no Flask, no subprocess at import time) and so tests
# can substitute every side-effecting step.

ConfigReader = Callable[[str], "tuple[Optional[dict], Optional[str]]"]
ConfigWriter = Callable[[str, dict], "tuple[bool, Optional[str]]"]
PluginLister = Callable[[str], "set[str]"]
PluginInstaller = Callable[[str, str], "tuple[bool, Optional[str]]"]
GatewayRestarter = Callable[[str], "tuple[bool, Optional[str]]"]


def _default_read(bot_id: str) -> tuple[Optional[dict], Optional[str]]:
    from .skills._oc_install_common import read_oc_config

    return read_oc_config(bot_id)


def _default_write(bot_id: str, cfg: dict) -> tuple[bool, Optional[str]]:
    from .skills._oc_install_common import write_oc_config

    return write_oc_config(bot_id, cfg)


def _default_restart(bot_id: str) -> tuple[bool, Optional[str]]:
    from .skills._oc_install_common import kickstart_gateway

    return kickstart_gateway(bot_id)


def _default_installed_plugin_ids(bot_id: str) -> set[str]:
    from .config import load_network
    from .safe_upgrade import _installed_plugin_ids

    return _installed_plugin_ids(bot_id, load_network())


def _default_plugin_installer(
    bot_id: str, npm_package: str,
) -> tuple[bool, Optional[str]]:
    """Install an OC channel plugin for ``bot_id``.

    DEPOSIT (META:skills owns plugin install + trust): this delegates to
    ``oc_neutralize.install_externalized_plugin``, the existing helper the
    brave gap-fill and the OC-upgrade dance both use. It shells
    ``sudo -u <bot> -H openclaw plugins install --force <pkg@version>`` with
    ``cwd="/tmp"`` (Node's ``uv_cwd()`` dies on a cwd the bot cannot
    traverse) under the ``evolve ALL=(ALL) NOPASSWD: SETENV: <oc_path>``
    grant — so no new sudoers grant is needed here.

    Since 2026-08-11 that helper carries the **Layer 1 provenance gate**
    (``plugin_provenance``): the package is classified against Evolve's in-repo
    provenance table before the install runs, and an unlisted one is refused
    with a named reason plus a Signal — which arrives here as ``(False, err)``
    and lands in ``AddChannelOutcome`` as ``PLUGIN_FAILED`` + a note. Every row
    ``channel_needs_plugin_install`` routes here today classifies as known, so
    this is a no-op on the current paths; a future ``external-plugin`` row is
    the case it bites. It is a supply-chain-*target* check, not a content scan —
    OpenClaw 2026.7 removed its install-time scanner and the replacement
    (``security.installPolicy``) is Layer 2, unbuilt. We deliberately do not
    grow a second install path here.
    Design: docs/design-plugin-install-provenance-gate-2026-08-11.md.
    """
    from .config import get_bot_user, load_network
    from .oc_neutralize import install_externalized_plugin

    network = load_network()
    bot_user = get_bot_user(bot_id, network)
    ok, err = install_externalized_plugin(bot_user, npm_package)
    return ok, (err or None)


# ── The operation ───────────────────────────────────────────────────────


def add_channel_to_bot(
    bot_id: str,
    channel_id: str,
    *,
    credential: Optional[str] = None,
    credential_field: str = "bot_token",
    channel_fields: Optional[dict[str, Any]] = None,
    install_plugin: bool = True,
    restart_gateway: bool = False,
    read_config: Optional[ConfigReader] = None,
    write_config: Optional[ConfigWriter] = None,
    installed_plugin_ids: Optional[PluginLister] = None,
    plugin_installer: Optional[PluginInstaller] = None,
    restart: Optional[GatewayRestarter] = None,
) -> AddChannelOutcome:
    """Add messaging channel ``channel_id`` to the already-provisioned ``bot_id``.

    Idempotent. Safe to call on a bot that already has the channel: the
    config is re-read, the merge produces no diff, and nothing is written.

    Parameters
    ----------
    bot_id, channel_id
        ``channel_id`` is resolved through :mod:`channel_registry`; an id the
        registry does not know, or one that is not an OpenClaw channel at all
        (the ``email`` / ``webhook`` delivery ids), is rejected without
        touching disk.
    credential
        Optional channel credential. Placed via the ``_RUNTIME_MIRROR_PATH``
        registry, never by writing a token key directly. When the registry
        has no mapping for ``(channel_id, credential_field)`` the outcome
        reports ``credential_pending=True`` and names the platform install
        flow that owns credentialing for that channel — the channel block is
        still written, so the gateway loads the plugin and its own
        missing-credential diagnostic becomes observable.
    channel_fields
        Extra/override keys merged into the channel block. Applied with
        ``setdefault`` semantics on top of :func:`default_channel_fields`, so
        they never clobber a value already on disk.
    install_plugin
        When False, an official/external-plugin channel that is not already
        installed reports ``PLUGIN_SKIPPED`` and the run still writes the
        config (the operator installs the plugin separately). Core channels
        ignore this flag — there is nothing to install.
    restart_gateway
        **Explicit opt-in.** Default False: the config lands on disk and the
        outcome says a restart is required. Nothing on a live pod is
        restarted from this code path without the caller asking.

    Returns
    -------
    AddChannelOutcome
        Never raises for an expected failure — the failure mode is
        ``ok=False`` with ``error`` set.
    """
    outcome = AddChannelOutcome(ok=False, bot_id=bot_id, channel_id=channel_id)

    bot_id = (bot_id or "").strip()
    if not bot_id:
        outcome.error = "bot_id required"
        return outcome
    outcome.bot_id = bot_id

    spec = channel_registry.get(channel_id)
    if spec is None:
        outcome.error = f"unknown channel: {channel_id!r}"
        return outcome
    outcome.channel_id = spec.id

    if spec.install is None:
        # email / webhook: Evolve labels them, OpenClaw does not run them as
        # channels. Writing a channels.<id> block would produce a config OC
        # rejects at validate time and wedge every later deploy-time write.
        outcome.error = (
            f"{spec.display_label} is not an OpenClaw channel — "
            "nothing to provision"
        )
        return outcome
    if not spec.messaging_integration:
        outcome.error = f"{spec.display_label} is not a messaging channel"
        return outcome

    _read = read_config or _default_read
    _write = write_config or _default_write

    # ── 1. Plugin, only if the registry says so ──────────────────────────
    outcome.plugin_state = _ensure_plugin(
        spec,
        bot_id,
        install_plugin=install_plugin,
        installed_plugin_ids=installed_plugin_ids or _default_installed_plugin_ids,
        plugin_installer=plugin_installer or _default_plugin_installer,
        notes=outcome.notes,
    )
    if outcome.plugin_state == PLUGIN_FAILED:
        outcome.error = outcome.notes[-1] if outcome.notes else "plugin install failed"
        return outcome

    # ── 2. Read the live config ──────────────────────────────────────────
    cfg, err = _read(bot_id)
    if cfg is None:
        outcome.error = err or "oc_read_failed"
        return outcome

    before = copy.deepcopy(cfg)

    # ── 3. Merge the channel block — never replace the channels map ──────
    channels = cfg.setdefault("channels", {})
    if not isinstance(channels, dict):
        outcome.error = "openclaw.json channels is not an object"
        return outcome
    block = channels.setdefault(spec.id, {})
    if not isinstance(block, dict):
        outcome.error = f"openclaw.json channels.{spec.id} is not an object"
        return outcome

    seeds = default_channel_fields(spec)
    if channel_fields:
        seeds.update(channel_fields)
    for key, value in seeds.items():
        block.setdefault(key, value)
    # ``enabled`` is the one field this operation is the source of truth for:
    # "add the channel" means the channel is on.
    block["enabled"] = True

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    if isinstance(entries, dict):
        entry = entries.setdefault(spec.id, {})
        if isinstance(entry, dict):
            entry["enabled"] = True

    # ── 4. Credential through the registry helper, never by hand ─────────
    if credential:
        from .web.server import _apply_credential_to_oc_dict

        applied = _apply_credential_to_oc_dict(
            cfg, spec.id, credential_field, credential,
        )
        outcome.credential_applied = applied
        if not applied:
            outcome.credential_pending = True
            outcome.notes.append(
                f"no credential mapping for ({spec.id}, {credential_field}) in "
                "_RUNTIME_MIRROR_PATH — credential NOT written. Complete the "
                f"credential through the {spec.display_label} install flow "
                "(evolve_admin.skills)."
            )
    elif channel_needs_credential(spec, cfg):
        outcome.credential_pending = True
        outcome.notes.append(
            f"{spec.display_label} has no credential on disk yet — the "
            "channel block is written and the plugin will load, but the "
            "channel cannot connect until its install flow supplies one."
        )

    # ── 5. Write only on a real diff ─────────────────────────────────────
    if cfg == before:
        outcome.ok = True
        outcome.notes.append("no change — channel already configured")
        return outcome

    ok, werr = _write(bot_id, cfg)
    if not ok:
        outcome.error = werr or "oc_write_failed"
        return outcome
    outcome.ok = True
    outcome.config_changed = True
    outcome.restart_required = True

    # ── 6. Restart ONLY on explicit opt-in ───────────────────────────────
    if restart_gateway:
        rok, rerr = (restart or _default_restart)(bot_id)
        outcome.gateway_restarted = rok
        if rok:
            outcome.restart_required = False
        else:
            outcome.notes.append(f"gateway restart failed: {rerr or 'unknown'}")
    else:
        outcome.notes.append(
            "gateway restart required before the channel goes live "
            "(caller opt-in: restart_gateway=True, or the Skills-page "
            "kickstart)"
        )
    return outcome


def channel_needs_credential(
    spec: channel_registry.ChannelSpec, cfg: dict[str, Any],
) -> bool:
    """Heuristic: does this channel's block still lack any credential-ish key?

    Used only to decide whether to flag ``credential_pending`` on a run that
    was given no credential. Deliberately shape-based rather than a per-
    channel key table — the key name differs per platform (``botToken`` /
    ``token`` / a QR-paired ``accounts`` map) and enumerating them here
    would be exactly the hardcoded channel table invariant 7 forbids.
    """
    block = (cfg.get("channels") or {}).get(spec.id)
    if not isinstance(block, dict):
        return True
    for key, value in block.items():
        lowered = str(key).lower()
        if ("token" in lowered or "secret" in lowered or "key" in lowered) and value:
            return False
        if lowered == "accounts" and value:
            return False
    return True


def provisionable_channels() -> tuple[channel_registry.ChannelSpec, ...]:
    """Registry rows this operation can actually add to a bot.

    A projection, not a copy: every row that OpenClaw runs as a messaging
    channel. A new registry row becomes addable for free; the ``email`` /
    ``webhook`` delivery ids stay out because ``install is None``.
    """
    return channel_registry.where(
        lambda c: c.install is not None and c.messaging_integration
    )


def _ensure_plugin(
    spec: channel_registry.ChannelSpec,
    bot_id: str,
    *,
    install_plugin: bool,
    installed_plugin_ids: PluginLister,
    plugin_installer: PluginInstaller,
    notes: list[str],
) -> str:
    """Resolve the plugin precondition. Returns one of the ``PLUGIN_*`` states."""
    if not channel_needs_plugin_install(spec):
        notes.append(
            f"{spec.display_label} is bundled with OpenClaw "
            f"(install={spec.install}) — no plugin install"
        )
        return PLUGIN_NOT_REQUIRED

    npm_package = spec.oc_plugin_id
    if not npm_package:
        # The registry's own invariant (test_channel_registry) forbids this
        # combination; treat it as a data bug rather than guessing a name.
        notes.append(f"{spec.id}: install={spec.install} but no oc_plugin_id")
        return PLUGIN_FAILED

    try:
        installed = installed_plugin_ids(bot_id)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not be fatal
        log.warning("installed-plugin probe failed for %s: %s", bot_id, exc)
        installed = set()

    if spec.id in installed:
        notes.append(f"{npm_package} already installed")
        return PLUGIN_ALREADY_INSTALLED

    if not install_plugin:
        notes.append(
            f"{npm_package} not installed and install_plugin=False — "
            "the channel block was written but the plugin must be installed "
            "separately before it can load"
        )
        return PLUGIN_SKIPPED

    ok, err = plugin_installer(bot_id, npm_package)
    if not ok:
        notes.append(f"plugin install failed for {npm_package}: {err or 'unknown'}")
        return PLUGIN_FAILED
    notes.append(f"installed {npm_package}")
    return PLUGIN_INSTALLED
