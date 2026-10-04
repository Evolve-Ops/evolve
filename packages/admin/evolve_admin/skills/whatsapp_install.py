"""evolve_admin.skills.whatsapp_install — WhatsApp skill install flow.

Wires OC's officially-shipped ``@openclaw/whatsapp`` plugin (verified live
on the mini, OC v2026.6.1: ``openclaw plugins search whatsapp`` returns
``@openclaw/whatsapp`` with ``defaultChoice: clawhub``) through a four-step
wizard that mirrors the bundled-plugin pattern used by ``imessage_install``
and ``runway_install``:

  1. Install the @openclaw/whatsapp plugin from ClawHub on the bot's
     OC instance (one-time per bot).
  2. Pair a WhatsApp account via QR code (Baileys / WhatsApp Web).
  3. Write the ``channels.whatsapp.accounts.<id>`` block + flip
     ``plugins.entries.whatsapp.enabled = true`` in the bot's openclaw.json.
  4. Kickstart the gateway; live probe via
     ``openclaw channels status --channel whatsapp --probe`` confirms
     the plugin is connected.

The pairing step is the novel piece — every other channel skill captures a
static credential string (BotFather token, Slack OAuth code, etc.). WhatsApp
uses Baileys device-link pairing: the operator scans a QR code with the
WhatsApp app on their phone, the device-link handshake completes, and
Baileys writes its multi-file auth state to ``authDir``. From then on the
bot connects directly to WhatsApp's servers using that stored state.

This is *not* the WhatsApp Business Cloud API. No Meta approval, no business
verification, no per-message billing — the operator either uses their own
WhatsApp account or sets up a separate phone + eSIM (per the OC blurb in
the channel catalog).

Spec: ``internal/spec-whatsapp-skill-2026-06-04.md``
Companion: ``_qr_pairing.py`` (session manager for the QR step)
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import bot_user_for as _bot_user_for
from . import _oc_install_common as _oc_common
from . import _qr_pairing

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the WhatsApp skill.
WHATSAPP_SKILL_ID = "whatsapp"

#: OC plugin name shipped on ClawHub.
WHATSAPP_PLUGIN_NPM = "@openclaw/whatsapp"

#: ClawHub install spec — `openclaw plugins install clawhub:@openclaw/whatsapp`.
WHATSAPP_CLAWHUB_SPEC = "clawhub:@openclaw/whatsapp"

#: Auth-dir path relative to the bot's home — where Baileys persists the
#: device-link session files. Owned by the bot user.
WHATSAPP_AUTH_DIR_REL = ".openclaw/whatsapp/auth"

#: Default per-bot account id used when an operator pairs a single phone
#: number. Multi-account-per-bot is a v2 follow-on.
DEFAULT_ACCOUNT_ID = "primary"

#: Timeout for the live ``openclaw channels status`` probe. Short on purpose
#: — if the CLI hangs we'd rather return ``unknown`` than block the admin UI.
WHATSAPP_PROBE_TIMEOUT_S = 12


# ── Plain-language access panel ───────────────────────────────────────────────

#: Describes what the bot will/won't do. Written for the Plex test.
WHATSAPP_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": WHATSAPP_SKILL_ID,
    "skill_display_name": "WhatsApp",
    "summary": (
        "Lets this bot send and receive WhatsApp messages on a phone number "
        "you connect. The bot links to WhatsApp the same way WhatsApp Web does — "
        "by scanning a QR code with your phone. We recommend using a separate "
        "phone number (a spare SIM or eSIM) rather than your personal one."
    ),
    "will": [
        "Send WhatsApp messages to people and groups it has been added to",
        "Read messages in those chats so it can respond",
        "Send photos, documents, and formatted text up to 50 MB",
        "Show as 'WhatsApp Web' under Linked Devices on your phone",
    ],
    "wont": [
        "Join chats or groups it hasn't been added to",
        "Read your other WhatsApp Web sessions or your phone's other chats",
        "Send messages without your instruction (unless it's a configured channel)",
        "Share your WhatsApp account with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The connection to WhatsApp is stored only on this bot's user account on "
        "your machine, as the 'linked device' files WhatsApp Web uses. You can "
        "revoke access at any time from this page, or by going to WhatsApp on "
        "your phone → Settings → Linked Devices → tap this device → Log Out."
    ),
}


# ── Install status ────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the WhatsApp install flow.

    Status values (state machine):

    * ``plugin_not_installed`` — ``@openclaw/whatsapp`` isn't in the bot's
      OC plugin install records. The wizard offers an "Install plugin" step
      that runs ``openclaw plugins install clawhub:@openclaw/whatsapp`` as
      the bot user.
    * ``legacy_orphan`` — plugin not installed BUT a ``channels.whatsapp``
      block exists with no ``accounts`` sub-key. Residue from a pre-Phase-1.3
      install path (a documented case from 2026-06-04 carried this shape).
      The Skills page should NOT offer "+ Add" here — the operator already
      had something going and abandoned it. Surface the Uninstall affordance
      instead so they can clean up; re-install can follow afterwards.
    * ``account_not_paired`` — plugin is installed but no
      ``channels.whatsapp.accounts.<id>`` block exists with a populated
      ``authDir``. Wizard offers the QR pairing step.
    * ``auth_dir_corrupt`` — authDir is on disk but ``creds.json`` is
      missing / unparseable. Wizard offers re-pair.
    * ``disabled`` — account is configured but ``enabled: false`` for
      the channel or the account itself.
    * ``oc_probe_failed`` — everything looks right but the live probe
      returns not-connected. Usually transient (gateway settling) or
      indicates WhatsApp on the phone logged the device out.
    * ``active`` — plugin installed + account paired + probe says
      connected. The only state that lets the catalog read green.
    * ``unknown`` — pre-flight check failed; ``error`` has the detail.
    """

    bot_id: str
    status: str
    account_id: str | None = None
    paired_phone: str | None = None
    auth_dir: str | None = None
    plugin_version: str | None = None
    oc_probe_ok: bool = False
    oc_probe_detail: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": WHATSAPP_SKILL_ID,
            "status": self.status,
            "account_id": self.account_id,
            "paired_phone": self.paired_phone,
            "auth_dir": self.auth_dir,
            "plugin_version": self.plugin_version,
            "oc_probe_ok": self.oc_probe_ok,
            "oc_probe_detail": self.oc_probe_detail,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives to complete a WhatsApp install."""

    id: str
    label: str
    description: str = ""
    endpoint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    access_panel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "access_panel": self.access_panel,
        }


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Build the ordered steps remaining for *status*.

    Returns an empty list for ``active`` (nothing to do) or ``unknown``
    (UI surfaces the error directly).
    """
    if status.status in ("active", "unknown"):
        return []

    plan: list[InstallStep] = []

    if status.status == "plugin_not_installed":
        plan.append(InstallStep(
            id="install_plugin",
            label="Install the WhatsApp plugin",
            description=(
                "Install OpenClaw's official WhatsApp plugin on this bot. "
                "This is a one-time setup step; it takes about a minute."
            ),
            endpoint=f"/api/skills/install/{WHATSAPP_SKILL_ID}/install-plugin",
            payload={"bot_id": status.bot_id},
            access_panel=dict(WHATSAPP_ACCESS_PANEL),
        ))
        plan.append(InstallStep(
            id="pair_qr",
            label="Connect a WhatsApp account",
            description=(
                "Scan a QR code on your phone to connect a WhatsApp account "
                "to this bot. We recommend a separate phone number rather "
                "than your personal one."
            ),
            endpoint=f"/api/skills/install/{WHATSAPP_SKILL_ID}/pair/start",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    if status.status in ("account_not_paired", "auth_dir_corrupt"):
        plan.append(InstallStep(
            id="pair_qr",
            label="Connect a WhatsApp account",
            description=(
                "Scan a QR code on your phone to connect a WhatsApp account "
                "to this bot. The QR refreshes every 20 seconds — scan "
                "promptly to keep the link from expiring."
            ),
            endpoint=f"/api/skills/install/{WHATSAPP_SKILL_ID}/pair/start",
            payload={"bot_id": status.bot_id},
            access_panel=dict(WHATSAPP_ACCESS_PANEL),
        ))
        return plan

    if status.status == "disabled":
        plan.append(InstallStep(
            id="reenable",
            label="Re-enable the WhatsApp connection",
            description=(
                "WhatsApp is configured but currently turned off for this bot. "
                "Re-enable it to start sending and receiving messages again."
            ),
            endpoint=f"/api/skills/install/{WHATSAPP_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    if status.status == "oc_probe_failed":
        plan.append(InstallStep(
            id="reprobe",
            label="Check connection again",
            description=(
                "The bot is set up for WhatsApp but isn't responding yet. "
                "This usually clears within a minute after the bot restarts. "
                "Check WhatsApp on your phone → Linked Devices to confirm "
                "this connection is still listed."
            ),
            endpoint=f"/api/skills/install/{WHATSAPP_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ))

    return plan


# ── OC wiring helpers ─────────────────────────────────────────────────────────
#
# Mirrors ``telegram_install.enable_channel_in_oc_config`` /
# ``imessage_install.enable_channel_in_oc_config`` — read-merge-write the
# bot's openclaw.json with the channels.whatsapp.accounts.<id> block and the
# plugins.entries.whatsapp flag, then caller kickstarts.


#: Default channel-level WhatsApp fields. These match OC's schema defaults
#: so an operator inspecting openclaw.json doesn't see surprises. Only set
#: at first-pair; preserved on re-pair.
_DEFAULT_WHATSAPP_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist",
    "mediaMaxMb": 50,
}


def enable_account_in_oc_config(
    bot_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    auth_dir: str | None = None,
    paired_phone: str | None = None,
) -> tuple[bool, str | None]:
    """Merge ``channels.whatsapp.accounts.<account_id>`` + flip
    ``plugins.entries.whatsapp.enabled`` to True in the bot's openclaw.json.

    Idempotent. Operator-set channel-level fields (e.g. dmPolicy) survive
    a re-pair; only the per-account ``enabled`` + ``authDir`` + ``name``
    fields are rewritten.

    Caller is responsible for kickstart afterwards.
    """
    if auth_dir is None:
        # Default to the standard per-bot auth dir path
        from ..config import bot_home as _bh
        auth_dir = str(_bh(bot_id) / WHATSAPP_AUTH_DIR_REL)

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    wa = channels.setdefault("whatsapp", {})
    for key, default in _DEFAULT_WHATSAPP_CHANNEL_FIELDS.items():
        wa.setdefault(key, default)
    # Install flow is source of truth for the channel-enabled flag.
    wa["enabled"] = True

    accounts = wa.setdefault("accounts", {})
    account_block = accounts.setdefault(account_id, {})
    account_block["enabled"] = True
    account_block["authDir"] = auth_dir
    account_block.setdefault("name", f"{bot_id}-{account_id}")
    if paired_phone is not None:
        # OC's schema doesn't include phoneNumber per se, but stashing it
        # here as an operator-visible label is harmless and useful for the
        # admin UI's inventory display.
        account_block["phoneNumber"] = paired_phone

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("whatsapp", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


def disable_account_in_oc_config(
    bot_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str | None]:
    """Inverse of :func:`enable_account_in_oc_config`.

    REMOVES ``channels.whatsapp.accounts.<account_id>`` (not just flips
    ``enabled: false`` — the old behaviour left a half-shape orphan that
    the Skills page then nagged the operator to "complete the install").

    If that was the last account (or the bot is carrying a pre-Phase-1.3
    legacy ``channels.whatsapp = {enabled: false, dmPolicy, ...}`` orphan
    with no ``accounts`` sub-key at all — a documented 2026-06-04 case
    carried this shape), also removes:

    * the whole ``channels.whatsapp`` block,
    * ``plugins.entries.whatsapp``,
    * any ``plugins.installs[k]`` keyed by the plugin spec
      (``@openclaw/whatsapp`` / ``clawhub:@openclaw/whatsapp`` /
      ``whatsapp``).

    Each removal is idempotent: missing keys are a no-op. Writes once at
    the end via the shared atomic-write helper.
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.get("channels")
    wa = channels.get("whatsapp") if isinstance(channels, dict) else None

    accounts: dict | None = None
    if isinstance(wa, dict):
        raw_accounts = wa.get("accounts")
        if isinstance(raw_accounts, dict):
            accounts = raw_accounts
            accounts.pop(account_id, None)  # idempotent

    # Decide whether the channel as a whole is now "empty" — either
    # because we just removed the last account, OR because the bot is
    # carrying a legacy bare-shape ``channels.whatsapp`` block with no
    # ``accounts`` sub-key. In both cases the channel is dead weight
    # the operator can't reach without a re-install.
    channel_is_empty = (
        isinstance(wa, dict)
        and (not accounts or len(accounts) == 0)
    )

    if channel_is_empty and isinstance(channels, dict):
        channels.pop("whatsapp", None)

        # Clear the plugin entry (the OC plugin loader keys off this).
        entries = (cfg.get("plugins") or {}).get("entries")
        if isinstance(entries, dict):
            entries.pop("whatsapp", None)

        # Clear any plugins.installs keys for the WhatsApp plugin spec
        # — these are what ``resolve_status`` reads to decide whether the
        # plugin is installed at all. Without this, a re-install on the
        # same bot would skip the "Install plugin" step thinking the
        # ClawHub plugin was already there, but the bot's plugins
        # registry has nothing to load.
        installs = (cfg.get("plugins") or {}).get("installs")
        if isinstance(installs, dict):
            for key in (
                WHATSAPP_PLUGIN_NPM,
                WHATSAPP_CLAWHUB_SPEC,
                WHATSAPP_SKILL_ID,
            ):
                installs.pop(key, None)

    return _oc_common.write_oc_config(bot_id, cfg)


def revoke_account(
    bot_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> tuple[bool, str | None]:
    """Tear down a paired WhatsApp account for a bot.

    1. Run ``openclaw channels logout --channel whatsapp --account <id>``
       (best-effort — if WhatsApp on the phone is already offline, OC may
       not be able to send the logout signal but the local files clear).
    2. Clear ``channels.whatsapp.accounts.<id>`` from openclaw.json
       (via :func:`disable_account_in_oc_config`).
    3. Wipe ``authDir`` recursively.
    4. Kickstart the gateway so OC unloads the plugin.

    Returns ``(ok, error)``.
    """
    # Step 1: best-effort logout — never fail the revoke if this returns
    # non-zero (often does when the phone is offline).
    try:
        import os as _os
        from ..config import bot_home as _bh_logout
        bot_cfg_logout = _bh_logout(bot_id) / ".openclaw" / "openclaw.json"
        subprocess.run(
            ["sudo",
             "--preserve-env=OPENCLAW_CONFIG_PATH",
             "-H", "-u", _bot_user_for(bot_id), "-n",
             "/opt/homebrew/bin/openclaw",
             "channels", "logout",
             "--channel", "whatsapp",
             "--account", account_id],
            capture_output=True, text=True, timeout=15,
            env={**_os.environ, "OPENCLAW_CONFIG_PATH": str(bot_cfg_logout)},
            cwd=str(_bh_logout(bot_id)),
        )
    except Exception as exc:
        log.debug("whatsapp revoke: logout step non-fatal: %s", exc)

    # Step 2: clear the OC config block
    ok, err = disable_account_in_oc_config(bot_id, account_id=account_id)
    if not ok:
        return False, err or "disable_failed"

    # Step 3: wipe authDir. Use sudo /bin/rm since the dir is owned by
    # the bot user (evolve can't unlink directly).
    try:
        from ..config import bot_home as _bh
        auth_dir = _bh(bot_id) / WHATSAPP_AUTH_DIR_REL
        if auth_dir.exists():
            subprocess.run(
                ["sudo", "/bin/rm", "-rf", str(auth_dir)],
                capture_output=True, text=True, timeout=10,
            )
    except Exception as exc:
        log.debug("whatsapp revoke: authDir wipe non-fatal: %s", exc)

    # Step 4: kickstart
    ok2, err2 = _oc_common.kickstart_gateway(bot_id)
    if not ok2:
        return False, err2 or "kickstart_failed"

    return True, None


# ── Status resolver ───────────────────────────────────────────────────────────


def _auth_dir_is_populated(path: str | Path) -> bool:
    """True iff the Baileys auth dir contains a parseable ``creds.json``
    with the expected shape. Catches the "operator deleted authDir
    manually" case before we waste a CLI probe."""
    p = Path(path)
    creds = p / "creds.json"
    if not creds.exists():
        return False
    try:
        data = json.loads(creds.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    # Baileys' creds.json carries at minimum noiseKey + signedIdentityKey.
    # Be liberal — if EITHER is present, treat as populated (Baileys minor
    # versions tweak field shapes; we don't want to false-negative).
    if not isinstance(data, dict):
        return False
    return bool(data.get("noiseKey") or data.get("signedIdentityKey")
                or data.get("me") or data.get("registered"))


def _probe_oc_channel_status(
    bot_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    timeout: int = WHATSAPP_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run ``openclaw channels status --channel whatsapp --probe --json``
    against the bot's gateway.

    Returns a dict with ``connected: bool`` and optional ``error`` /
    ``detail`` / ``paired_phone``. Hard 12 s timeout — never block the
    admin UI on a hung CLI.
    """
    from ..config import bot_home as _bh
    bot_cfg = _bh(bot_id) / ".openclaw" / "openclaw.json"

    cmd = [
        "/opt/homebrew/bin/openclaw",
        "channels", "status",
        "--channel", "whatsapp",
        "--probe",
        "--json",
        "--timeout", str(timeout * 1000),
    ]
    env = {
        "OPENCLAW_CONFIG_PATH": str(bot_cfg),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(_bh(bot_id)),
    }
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"connected": False, "error": "probe_timeout"}
    except FileNotFoundError:
        return {"connected": False, "error": "openclaw_cli_not_found"}
    except Exception as exc:
        return {"connected": False, "error": f"probe_exception: {exc.__class__.__name__}"}

    parsed: dict[str, Any] = {}
    try:
        if r.stdout.strip():
            parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        parsed = {}

    if isinstance(parsed, dict):
        # OC's JSON shape can be flat or nested by channel. Check both.
        candidate = (
            parsed.get("whatsapp")
            or next(
                (c for c in (parsed.get("channels") or [])
                 if isinstance(c, dict) and c.get("id") == "whatsapp"),
                None,
            )
            or parsed
        )
        if isinstance(candidate, dict):
            connected = (
                candidate.get("connected") is True
                or candidate.get("ok") is True
                or candidate.get("state") in ("connected", "ready", "active")
                or candidate.get("status") in ("connected", "ready", "active")
            )
            if connected:
                return {
                    "connected": True,
                    "paired_phone": (
                        candidate.get("phoneNumber")
                        or candidate.get("paired_phone")
                        or candidate.get("me")
                    ),
                    "detail": candidate.get("detail") or candidate.get("message"),
                }

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        return {
            "connected": False,
            "error": "probe_failed",
            "detail": stderr[:200] if stderr else f"exit_{r.returncode}",
        }
    return {"connected": False, "error": "probe_inconclusive"}


def resolve_status(
    bot_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    read_oc_config: Callable[[str], tuple[dict | None, str | None]] | None = None,
    auth_dir_probe: Callable[[str], bool] | None = None,
    probe_oc_channel: Callable[[str], dict[str, Any]] | None = None,
) -> InstallStatus:
    """Resolve the bot's current WhatsApp install state.

    Three stages:
      1. Plugin installed? (read openclaw.json::plugins.installs)
      2. Account paired (authDir populated)? (read openclaw.json + disk)
      3. Live probe says connected? (openclaw channels status --probe)

    Only ``active`` requires all three to pass. The probe never returns
    True unless OC explicitly says so — load-bearing F3 rule from the May
    audit.
    """
    _read = read_oc_config or _oc_common.read_oc_config
    _populated = auth_dir_probe or _auth_dir_is_populated
    _probe = probe_oc_channel or _probe_oc_channel_status

    try:
        cfg, err = _read(bot_id)
    except Exception as exc:
        return InstallStatus(bot_id=bot_id, status="unknown",
                             error=f"oc_read_failed: {exc}")
    if cfg is None:
        return InstallStatus(bot_id=bot_id, status="unknown",
                             error=err or "oc_read_failed")

    # Stage 1: plugin install record
    installs = (cfg.get("plugins") or {}).get("installs") or {}
    plugin_record = (
        installs.get(WHATSAPP_PLUGIN_NPM)
        or installs.get(WHATSAPP_CLAWHUB_SPEC)
        or installs.get(WHATSAPP_SKILL_ID)
    )
    if not plugin_record:
        # Differentiate "truly never installed" from "abandoned mid-install
        # so there's a channels.whatsapp orphan but no plugin install record."
        # The orphan shape is `channels.whatsapp = {enabled, dmPolicy, ...}`
        # with NO `accounts` sub-key — what the old disable_account_in_oc_config
        # left behind when it flipped enabled:false instead of removing.
        # See internal/skills-deep-audit-2026-05-30.md F2 (asymmetric revoke).
        wa_orphan = (cfg.get("channels") or {}).get("whatsapp")
        if isinstance(wa_orphan, dict) and wa_orphan:
            accounts_field = wa_orphan.get("accounts")
            if not isinstance(accounts_field, dict) or not accounts_field:
                return InstallStatus(bot_id=bot_id, status="legacy_orphan")
        return InstallStatus(bot_id=bot_id, status="plugin_not_installed")

    plugin_version: str | None = None
    if isinstance(plugin_record, dict):
        plugin_version = plugin_record.get("version")

    # Stage 2: account block + authDir on disk
    channels = cfg.get("channels") or {}
    wa = channels.get("whatsapp") or {}
    accounts = wa.get("accounts") or {}
    acct = accounts.get(account_id) if isinstance(accounts, dict) else None

    if not isinstance(acct, dict):
        return InstallStatus(
            bot_id=bot_id, status="account_not_paired",
            account_id=account_id, plugin_version=plugin_version,
        )

    auth_dir = acct.get("authDir")
    if not auth_dir:
        return InstallStatus(
            bot_id=bot_id, status="account_not_paired",
            account_id=account_id, plugin_version=plugin_version,
        )

    if not _populated(auth_dir):
        return InstallStatus(
            bot_id=bot_id, status="auth_dir_corrupt",
            account_id=account_id, auth_dir=auth_dir,
            plugin_version=plugin_version,
        )

    if acct.get("enabled") is False or wa.get("enabled") is False:
        return InstallStatus(
            bot_id=bot_id, status="disabled",
            account_id=account_id, auth_dir=auth_dir,
            plugin_version=plugin_version,
        )

    # Stage 3: live probe
    try:
        probe = _probe(bot_id)
    except Exception as exc:
        probe = {"connected": False, "error": f"probe_exception: {exc}"}

    if not probe.get("connected"):
        return InstallStatus(
            bot_id=bot_id, status="oc_probe_failed",
            account_id=account_id, auth_dir=auth_dir,
            plugin_version=plugin_version,
            oc_probe_ok=False,
            oc_probe_detail=probe.get("detail") or probe.get("error"),
        )

    return InstallStatus(
        bot_id=bot_id, status="active",
        account_id=account_id, auth_dir=auth_dir,
        plugin_version=plugin_version,
        paired_phone=probe.get("paired_phone") or acct.get("phoneNumber"),
        oc_probe_ok=True,
        oc_probe_detail=probe.get("detail"),
    )


# ── Plugin install ────────────────────────────────────────────────────────────


def install_plugin(bot_id: str) -> tuple[bool, str | None]:
    """Run ``openclaw plugins install clawhub:@openclaw/whatsapp`` as the
    bot user. Idempotent — re-running on an already-installed bot is a
    no-op success per OC's CLI semantics.

    Returns ``(ok, error_message)``.
    """
    bot_user = _bot_user_for(bot_id)
    from ..config import bot_home as _bh
    bot_cfg = _bh(bot_id) / ".openclaw" / "openclaw.json"

    import os as _os
    cmd = [
        "sudo",
        "--preserve-env=OPENCLAW_CONFIG_PATH",
        "-H", "-u", bot_user, "-n",
        "/opt/homebrew/bin/openclaw",
        "plugins", "install", WHATSAPP_CLAWHUB_SPEC,
    ]
    env = {**_os.environ, "OPENCLAW_CONFIG_PATH": str(bot_cfg)}
    bot_home = str(_bh(bot_id))
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env,
            cwd=bot_home,
        )
    except FileNotFoundError:
        return False, "openclaw_cli_not_found"
    except PermissionError:
        return False, "sudoers_grant_missing"
    except subprocess.TimeoutExpired:
        return False, "install_timeout"
    except Exception as exc:
        return False, f"install_exception: {exc.__class__.__name__}: {exc}"

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        return False, f"install_failed (exit {r.returncode}): {stderr[:200] or 'unknown'}"
    return True, None


# ── Pairing session API (thin wrappers over _qr_pairing) ──────────────────────


def start_pairing_session(bot_id: str) -> dict[str, Any]:
    """Start a QR pairing session for ``bot_id``. Returns the session record."""
    return _qr_pairing.start_session(bot_id, channel=WHATSAPP_SKILL_ID)


def poll_pairing_session(session_id: str) -> dict[str, Any] | None:
    """Return the current snapshot of ``session_id``, or ``None`` if unknown."""
    return _qr_pairing.poll_session(session_id)


def cancel_pairing_session(session_id: str) -> bool:
    """Cancel an in-flight pairing session. Returns True if found."""
    return _qr_pairing.cancel_session(session_id)


def finalize_pairing(bot_id: str, *, account_id: str = DEFAULT_ACCOUNT_ID) -> InstallStatus:
    """Called by the admin server after a pairing session reaches ``paired``.

    Writes the channels.whatsapp.accounts.<id> block to openclaw.json,
    kickstarts the gateway, and re-resolves status so the response carries
    the live state.
    """
    ok, err = enable_account_in_oc_config(bot_id, account_id=account_id)
    if not ok:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"oc_config_write_failed: {err}",
        )

    ok2, err2 = _oc_common.kickstart_gateway(bot_id)
    if not ok2:
        # Config wrote; surface the kickstart issue but don't fail outright
        # — the operator can restart the bot from the UI if needed.
        log.warning("whatsapp finalize kickstart failed for %s: %s", bot_id, err2)

    return resolve_status(bot_id, account_id=account_id)


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": WHATSAPP_SKILL_ID,
    "display_name": WHATSAPP_ACCESS_PANEL["skill_display_name"],
    "summary": WHATSAPP_ACCESS_PANEL["summary"],
    "access_panel": dict(WHATSAPP_ACCESS_PANEL),
    "config_keys": ["accounts.primary.authDir", "accounts.primary.phoneNumber"],
}
