"""evolve_admin.skills.signal_install — Signal skill install flow.

.. warning::
   **LICENSING REVIEW REQUIRED BEFORE MERGE.** signal-cli is GPL-3.0 and
   the upstream libsignal protocol library is AGPL-3.0. The May 2026
   vetting verdict was REJECT — see the
   ``project_signal_cli_vetting_2026_05_14`` memory file. The substrate
   has since shifted: OpenClaw now bundles its own ``@openclaw/signal``
   plugin (verified live on the mini, OC v2026.6.1) which includes an
   ``install-signal-cli`` helper that downloads signal-cli into the bot
   user's home directory at install time. Evolve never touches
   signal-cli source, never bundles it, never links against it — we
   call ``openclaw plugins install clawhub:@openclaw/signal`` and OC
   does the rest. Whether that shape satisfies our Apache-2.0/MIT-only
   vetting bar is a licensing question pending review. A companion memo
   request is queued at
   ``~/.claude/specs/builds/signal-cli-licensing-revisit-2026-06-04.md``.
   If the review returns FAIL, withdraw this module and the routes that
   reference it; the rest of the install is structurally analogous to
   ``imessage_install`` / ``whatsapp_install`` and is mergeable on its
   own merits.

Wires OC's officially-shipped ``@openclaw/signal`` plugin through a
five-step wizard. Mirrors the bundled-plugin pattern from
``whatsapp_install`` with one extra step: signal-cli's ``link`` command
needs to know which phone number it's pairing with up-front, so the
wizard captures the operator's E.164 number before triggering the QR
flow. (WhatsApp's Baileys pairing infers the number from the scanning
device, so it doesn't need this step.)

Install steps
-------------

  1. Install the @openclaw/signal plugin from ClawHub on the bot's OC
     instance (one-time per bot). OC's installer transitively downloads
     signal-cli into the bot's home dir.
  2. Capture an E.164 phone number (the operator's existing Signal
     account number, or a fresh one provisioned for the bot).
  3. Pair via QR code — operator opens Signal on their phone, goes to
     Settings → Linked Devices, taps "Link a New Device", scans the QR.
  4. Write ``channels.signal.accounts.<number> = {number, configDir,
     deviceName}`` + flip ``plugins.entries.signal.enabled = true`` to
     openclaw.json.
  5. Kickstart the gateway; live probe via
     ``openclaw channels status --channel signal --probe`` confirms the
     plugin is connected.

Spec: ``internal/spec-signal-skill-2026-06-04.md`` (companion to
``internal/spec-whatsapp-skill-2026-06-04.md``)
Companion: ``_qr_pairing.py`` (session manager — same module WhatsApp uses)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import bot_user_for as _bot_user_for
from . import _oc_install_common as _oc_common
from . import _qr_pairing

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Signal skill.
SIGNAL_SKILL_ID = "signal"

#: OC plugin name shipped on ClawHub.
SIGNAL_PLUGIN_NPM = "@openclaw/signal"

#: ClawHub install spec — `openclaw plugins install clawhub:@openclaw/signal`.
SIGNAL_CLAWHUB_SPEC = "clawhub:@openclaw/signal"

#: signal-cli config-dir path relative to the bot's home — where signal-cli
#: persists the linked-device session files. Owned by the bot user.
#:
#: Note: signal-cli's default config dir on Linux is
#: ``$XDG_DATA_HOME/signal-cli/`` (typically ``~/.local/share/signal-cli/``).
#: We force a known location under the bot's openclaw dir so the inventory
#: probe + revoke can find it deterministically across operating systems.
SIGNAL_CONFIG_DIR_REL = ".openclaw/signal/config"

#: Default device name shown in the operator's Signal app's Linked Devices
#: list. Operator can override but this is what the wizard sends.
SIGNAL_DEFAULT_DEVICE_NAME_TEMPLATE = "evolve-{bot_id}"

#: Timeout for the live ``openclaw channels status`` probe. Short on purpose
#: — if the CLI hangs we'd rather return ``unknown`` than block the admin UI.
SIGNAL_PROBE_TIMEOUT_S = 12


# ── E.164 phone-number validation ────────────────────────────────────────────

#: E.164 format: leading +, 7-15 digits. signal-cli rejects anything else.
_E164_RE = re.compile(r"^\+\d{7,15}$")


def is_valid_e164(number: str | None) -> bool:
    """True iff ``number`` is a parseable E.164 phone-number string."""
    if not number:
        return False
    return bool(_E164_RE.match(number.strip()))


# ── Plain-language access panel ───────────────────────────────────────────────

#: Describes what the bot will/won't do. Written for the Plex test.
SIGNAL_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": SIGNAL_SKILL_ID,
    "skill_display_name": "Signal",
    "summary": (
        "Lets this bot send and receive Signal messages on a phone number "
        "you connect. The bot links to Signal the same way the desktop app "
        "does — by scanning a QR code with your phone. We recommend using a "
        "separate phone number (a spare SIM or eSIM) rather than your "
        "personal one."
    ),
    "will": [
        "Send Signal messages to people and groups it has been added to",
        "Read messages in those chats so it can respond",
        "Send photos, files, and formatted text",
        "Show as a 'Linked Device' in your Signal app's Settings",
    ],
    "wont": [
        "Join chats or groups it hasn't been added to",
        "Read your other Signal Desktop sessions or your phone's other chats",
        "Send messages without your instruction (unless it's a configured channel)",
        "Share your Signal account with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The connection to Signal is stored only on this bot's user account on "
        "your machine, as the 'linked device' files Signal Desktop uses. You can "
        "revoke access at any time from this page, or by going to Signal on "
        "your phone → Settings → Linked Devices → tap this device → Remove."
    ),
}


# ── Install status ────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Signal install flow.

    Status values (state machine):

    * ``plugin_not_installed`` — ``@openclaw/signal`` isn't in the bot's
      OC plugin install records. The wizard offers an "Install plugin"
      step that runs ``openclaw plugins install
      clawhub:@openclaw/signal`` as the bot user. OC's installer
      transitively downloads signal-cli.
    * ``number_not_captured`` — plugin installed but no E.164 number
      stored yet. Wizard offers the number-capture step before pairing.
    * ``account_not_paired`` — number captured, plugin installed, but no
      ``channels.signal.accounts.<number>`` block with a populated
      configDir. Wizard offers the QR pairing step.
    * ``config_dir_corrupt`` — configDir is on disk but the linked-
      device state files are missing / unparseable. Wizard offers
      re-pair.
    * ``disabled`` — account is configured but ``enabled: false`` for
      the channel or the account itself.
    * ``oc_probe_failed`` — everything looks right but the live probe
      returns not-connected. Usually transient (gateway settling) or
      indicates the operator removed the linked device from their phone.
    * ``active`` — plugin installed + account paired + probe says
      connected. The only state that lets the catalog read green.
    * ``unknown`` — pre-flight check failed; ``error`` has the detail.
    """

    bot_id: str
    status: str
    paired_number: str | None = None  # E.164
    config_dir: str | None = None
    device_name: str | None = None
    plugin_version: str | None = None
    oc_probe_ok: bool = False
    oc_probe_detail: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": SIGNAL_SKILL_ID,
            "status": self.status,
            "paired_number": self.paired_number,
            "config_dir": self.config_dir,
            "device_name": self.device_name,
            "plugin_version": self.plugin_version,
            "oc_probe_ok": self.oc_probe_ok,
            "oc_probe_detail": self.oc_probe_detail,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives to complete a Signal install."""

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
            label="Install the Signal plugin",
            description=(
                "Install OpenClaw's official Signal plugin on this bot. "
                "This downloads the small Signal connector — about a "
                "minute on a fast connection."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/install-plugin",
            payload={"bot_id": status.bot_id},
            access_panel=dict(SIGNAL_ACCESS_PANEL),
        ))
        plan.append(InstallStep(
            id="set_number",
            label="Enter the Signal phone number",
            description=(
                "Enter the phone number this bot will use on Signal, including "
                "the country code (e.g. +15551234567). We recommend a separate "
                "number rather than your personal one."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/set-number",
            payload={"bot_id": status.bot_id},
        ))
        plan.append(InstallStep(
            id="pair_qr",
            label="Connect via Signal on your phone",
            description=(
                "Open Signal on your phone, go to Settings → Linked Devices, "
                "tap 'Link New Device', and scan the QR code on screen."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/pair/start",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    if status.status == "number_not_captured":
        plan.append(InstallStep(
            id="set_number",
            label="Enter the Signal phone number",
            description=(
                "Enter the phone number this bot will use on Signal, including "
                "the country code (e.g. +15551234567)."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/set-number",
            payload={"bot_id": status.bot_id},
            access_panel=dict(SIGNAL_ACCESS_PANEL),
        ))
        plan.append(InstallStep(
            id="pair_qr",
            label="Connect via Signal on your phone",
            description=(
                "Open Signal on your phone, go to Settings → Linked Devices, "
                "tap 'Link New Device', and scan the QR code on screen."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/pair/start",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    if status.status in ("account_not_paired", "config_dir_corrupt"):
        plan.append(InstallStep(
            id="pair_qr",
            label="Connect via Signal on your phone",
            description=(
                "Open Signal on your phone, go to Settings → Linked Devices, "
                "tap 'Link New Device', and scan the QR code on screen. "
                "The QR refreshes periodically — scan promptly to keep the "
                "link from expiring."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/pair/start",
            payload={"bot_id": status.bot_id},
            access_panel=dict(SIGNAL_ACCESS_PANEL),
        ))
        return plan

    if status.status == "disabled":
        plan.append(InstallStep(
            id="reenable",
            label="Re-enable the Signal connection",
            description=(
                "Signal is configured but currently turned off for this bot. "
                "Re-enable it to start sending and receiving messages again."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    if status.status == "oc_probe_failed":
        plan.append(InstallStep(
            id="reprobe",
            label="Check connection again",
            description=(
                "The bot is set up for Signal but isn't responding yet. "
                "This usually clears within a minute after the bot restarts. "
                "Check Signal on your phone → Settings → Linked Devices to "
                "confirm this connection is still listed."
            ),
            endpoint=f"/api/skills/install/{SIGNAL_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ))

    return plan


# ── OC wiring helpers ─────────────────────────────────────────────────────────
#
# Mirrors ``whatsapp_install.enable_account_in_oc_config`` and
# ``telegram_install.enable_channel_in_oc_config`` — read-merge-write the
# bot's openclaw.json with the channels.signal.accounts.<id> block and the
# plugins.entries.signal flag, then caller kickstarts.
#
# Account-id strategy: signal-cli accounts are uniquely keyed by phone
# number, so the account_id IS the E.164 phone number (e.g.
# accounts["+15551234567"]). This differs from WhatsApp's account_id
# ("primary" / operator-assigned label) because Baileys lets one device
# pair with multiple accounts that aren't a priori identified — signal-cli
# requires the phone number up-front.


#: Default channel-level Signal fields. Match OC's schema defaults so an
#: operator inspecting openclaw.json doesn't see surprises.
_DEFAULT_SIGNAL_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist",
}


def _config_dir_for(bot_id: str, number: str) -> str:
    """Return the per-bot, per-number signal-cli config dir path.

    Each linked signal-cli account gets its own state subdir so the same
    bot can later pair multiple numbers without state cross-contamination.
    Stored under the bot's openclaw dir so the read ACL evolve already
    has covers it.
    """
    from ..config import bot_home as _bh
    safe_num = number.lstrip("+")  # filesystem-safe
    return str(_bh(bot_id) / SIGNAL_CONFIG_DIR_REL / safe_num)


def _device_name_for(bot_id: str) -> str:
    """Default device-name string shown in the operator's Signal app."""
    return SIGNAL_DEFAULT_DEVICE_NAME_TEMPLATE.format(bot_id=bot_id)


def enable_account_in_oc_config(
    bot_id: str,
    *,
    number: str,
    config_dir: str | None = None,
    device_name: str | None = None,
) -> tuple[bool, str | None]:
    """Merge ``channels.signal.accounts.<number>`` + flip
    ``plugins.entries.signal.enabled`` to True in the bot's openclaw.json.

    Idempotent. Operator-set channel-level fields (e.g. dmPolicy) survive
    a re-pair; only per-account ``enabled`` + ``configDir`` + ``deviceName``
    get rewritten.

    Caller is responsible for kickstart afterwards. ``number`` MUST be
    pre-validated as E.164 — this function does not re-check.
    """
    if not is_valid_e164(number):
        return False, "number_invalid_e164"

    if config_dir is None:
        config_dir = _config_dir_for(bot_id, number)
    if device_name is None:
        device_name = _device_name_for(bot_id)

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    sg = channels.setdefault("signal", {})
    for key, default in _DEFAULT_SIGNAL_CHANNEL_FIELDS.items():
        sg.setdefault(key, default)
    sg["enabled"] = True

    accounts = sg.setdefault("accounts", {})
    account_block = accounts.setdefault(number, {})
    account_block["enabled"] = True
    account_block["number"] = number
    account_block["configDir"] = config_dir
    account_block["deviceName"] = device_name

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("signal", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


def disable_account_in_oc_config(
    bot_id: str,
    *,
    number: str,
) -> tuple[bool, str | None]:
    """Inverse of :func:`enable_account_in_oc_config`.

    REMOVES ``channels.signal.accounts.<number>`` from the bot's openclaw.json
    (the old behaviour flipped ``enabled: false`` instead, which left a
    half-shape orphan the Skills page then nagged the operator to "complete
    the install" — same bug shape as whatsapp_install pre-2026-06-04).

    If that was the last account, also removes:

    * the whole ``channels.signal`` block,
    * ``plugins.entries.signal``,
    * any ``plugins.installs[k]`` keyed by the plugin spec.

    Idempotent: missing keys are a no-op. Single atomic write at the end.
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.get("channels")
    sg = channels.get("signal") if isinstance(channels, dict) else None

    accounts: dict | None = None
    if isinstance(sg, dict):
        raw_accounts = sg.get("accounts")
        if isinstance(raw_accounts, dict):
            accounts = raw_accounts
            accounts.pop(number, None)

    # Channel is empty if we just removed the last account OR if it's a
    # legacy bare-shape ``channels.signal = {enabled: false, …}`` orphan
    # with no ``accounts`` sub-key (parallel to the documented 2026-06-04
    # whatsapp legacy-orphan case).
    channel_is_empty = (
        isinstance(sg, dict)
        and (not accounts or len(accounts) == 0)
    )

    if channel_is_empty and isinstance(channels, dict):
        channels.pop("signal", None)

        entries = (cfg.get("plugins") or {}).get("entries")
        if isinstance(entries, dict):
            entries.pop("signal", None)

        installs = (cfg.get("plugins") or {}).get("installs")
        if isinstance(installs, dict):
            for key in (SIGNAL_PLUGIN_NPM, SIGNAL_CLAWHUB_SPEC, SIGNAL_SKILL_ID):
                installs.pop(key, None)

    return _oc_common.write_oc_config(bot_id, cfg)


def revoke_account(
    bot_id: str,
    *,
    number: str,
) -> tuple[bool, str | None]:
    """Tear down a paired Signal account for a bot.

    1. Run ``openclaw channels logout --channel signal --account <number>``
       (best-effort — if Signal on the phone removed the device first, OC
       may return non-zero but the local files clear).
    2. Clear ``channels.signal.accounts.<number>`` from openclaw.json
       (via :func:`disable_account_in_oc_config`).
    3. Wipe the per-account configDir recursively.
    4. Kickstart the gateway so OC unloads the plugin's account.

    Returns ``(ok, error)``.
    """
    if not is_valid_e164(number):
        return False, "number_invalid_e164"

    # Step 1: best-effort logout
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
             "--channel", "signal",
             "--account", number],
            capture_output=True, text=True, timeout=15,
            env={**_os.environ, "OPENCLAW_CONFIG_PATH": str(bot_cfg_logout)},
            cwd=str(_bh_logout(bot_id)),
        )
    except Exception as exc:
        log.debug("signal revoke: logout step non-fatal: %s", exc)

    # Step 2: clear the OC config block
    ok, err = disable_account_in_oc_config(bot_id, number=number)
    if not ok:
        return False, err or "disable_failed"

    # Step 3: wipe configDir
    try:
        config_dir = _config_dir_for(bot_id, number)
        cd_path = Path(config_dir)
        if cd_path.exists():
            subprocess.run(
                ["sudo", "/bin/rm", "-rf", str(cd_path)],
                capture_output=True, text=True, timeout=10,
            )
    except Exception as exc:
        log.debug("signal revoke: configDir wipe non-fatal: %s", exc)

    # Step 4: kickstart
    ok2, err2 = _oc_common.kickstart_gateway(bot_id)
    if not ok2:
        return False, err2 or "kickstart_failed"

    return True, None


# ── Status resolver ───────────────────────────────────────────────────────────


def _config_dir_is_populated(path: str | Path) -> bool:
    """True iff the signal-cli config dir contains state files for a
    linked device. signal-cli writes ``data/<number>`` and
    ``data/<number>.d/`` after a successful link. We accept either
    shape — be liberal because signal-cli's directory layout has
    historically shifted between minor versions."""
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    # Look for data/ subdir with at least one file, OR any *.d/ subdir
    data_dir = p / "data"
    if data_dir.exists() and data_dir.is_dir() and any(data_dir.iterdir()):
        return True
    try:
        for entry in p.iterdir():
            if entry.is_dir() and entry.name.endswith(".d"):
                return True
    except OSError:
        return False
    return False


def _probe_oc_channel_status(
    bot_id: str,
    *,
    number: str,
    timeout: int = SIGNAL_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run ``openclaw channels status --channel signal --probe --json``
    against the bot's gateway.

    Returns a dict with ``connected: bool`` and optional ``error`` /
    ``detail`` keys. Hard 12 s timeout — never block the admin UI on a
    hung CLI.
    """
    from ..config import bot_home as _bh
    bot_cfg = _bh(bot_id) / ".openclaw" / "openclaw.json"

    cmd = [
        "/opt/homebrew/bin/openclaw",
        "channels", "status",
        "--channel", "signal",
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
        candidate = (
            parsed.get("signal")
            or next(
                (c for c in (parsed.get("channels") or [])
                 if isinstance(c, dict) and c.get("id") == "signal"),
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


def _pick_primary_account(
    accounts: dict[str, Any],
) -> tuple[str | None, dict | None]:
    """Pick the first enabled account from a channels.signal.accounts dict.

    Returns ``(number, account_block)`` for status display. Multi-account
    bots are a v2 follow-on; v1 surfaces only the first enabled.
    """
    for number, block in accounts.items():
        if isinstance(block, dict) and block.get("enabled") is not False:
            return number, block
    # No enabled accounts; pick any present so the UI can surface disabled
    for number, block in accounts.items():
        if isinstance(block, dict):
            return number, block
    return None, None


def resolve_status(
    bot_id: str,
    *,
    read_oc_config: Callable[[str], tuple[dict | None, str | None]] | None = None,
    config_dir_probe: Callable[[str], bool] | None = None,
    probe_oc_channel: Callable[[str, str], dict[str, Any]] | None = None,
) -> InstallStatus:
    """Resolve the bot's current Signal install state.

    Stages:
      1. Plugin installed? (read openclaw.json::plugins.installs)
      2. Any account block with a number? (read openclaw.json)
      3. configDir populated on disk? (probe filesystem)
      4. Live probe says connected? (openclaw channels status --probe)

    Only ``active`` requires all four to pass. The probe never returns
    True unless OC explicitly says so — load-bearing F3 rule.
    """
    _read = read_oc_config or _oc_common.read_oc_config
    _populated = config_dir_probe or _config_dir_is_populated
    _probe = probe_oc_channel or (lambda bid, num: _probe_oc_channel_status(bid, number=num))

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
        installs.get(SIGNAL_PLUGIN_NPM)
        or installs.get(SIGNAL_CLAWHUB_SPEC)
        or installs.get(SIGNAL_SKILL_ID)
    )
    if not plugin_record:
        return InstallStatus(bot_id=bot_id, status="plugin_not_installed")

    plugin_version: str | None = None
    if isinstance(plugin_record, dict):
        plugin_version = plugin_record.get("version")

    # Stage 2: number captured + any account block
    channels = cfg.get("channels") or {}
    sg = channels.get("signal") or {}
    accounts = sg.get("accounts") or {}

    if not isinstance(accounts, dict) or not accounts:
        return InstallStatus(
            bot_id=bot_id, status="number_not_captured",
            plugin_version=plugin_version,
        )

    number, acct = _pick_primary_account(accounts)
    if number is None or not isinstance(acct, dict):
        return InstallStatus(
            bot_id=bot_id, status="number_not_captured",
            plugin_version=plugin_version,
        )

    config_dir = acct.get("configDir")
    device_name = acct.get("deviceName")
    if not config_dir:
        return InstallStatus(
            bot_id=bot_id, status="account_not_paired",
            paired_number=number, device_name=device_name,
            plugin_version=plugin_version,
        )

    # Stage 3: configDir populated on disk
    if not _populated(config_dir):
        return InstallStatus(
            bot_id=bot_id, status="config_dir_corrupt",
            paired_number=number, config_dir=config_dir,
            device_name=device_name,
            plugin_version=plugin_version,
        )

    if acct.get("enabled") is False or sg.get("enabled") is False:
        return InstallStatus(
            bot_id=bot_id, status="disabled",
            paired_number=number, config_dir=config_dir,
            device_name=device_name,
            plugin_version=plugin_version,
        )

    # Stage 4: live probe
    try:
        probe = _probe(bot_id, number)
    except Exception as exc:
        probe = {"connected": False, "error": f"probe_exception: {exc}"}

    if not probe.get("connected"):
        return InstallStatus(
            bot_id=bot_id, status="oc_probe_failed",
            paired_number=number, config_dir=config_dir,
            device_name=device_name,
            plugin_version=plugin_version,
            oc_probe_ok=False,
            oc_probe_detail=probe.get("detail") or probe.get("error"),
        )

    return InstallStatus(
        bot_id=bot_id, status="active",
        paired_number=number, config_dir=config_dir,
        device_name=device_name,
        plugin_version=plugin_version,
        oc_probe_ok=True,
        oc_probe_detail=probe.get("detail"),
    )


# ── Plugin install ────────────────────────────────────────────────────────────


def install_plugin(bot_id: str) -> tuple[bool, str | None]:
    """Run ``openclaw plugins install clawhub:@openclaw/signal`` as the
    bot user. Idempotent. OC's installer transitively downloads
    signal-cli into the bot's home dir — first install can be slow on
    a fresh bot.

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
        "plugins", "install", SIGNAL_CLAWHUB_SPEC,
    ]
    env = {**_os.environ, "OPENCLAW_CONFIG_PATH": str(bot_cfg)}
    bot_home = str(_bh(bot_id))
    try:
        # 300 s timeout — signal-cli download + JVM warm can be slow
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
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


# ── Number capture step ───────────────────────────────────────────────────────
#
# Distinct from WhatsApp: signal-cli's link command needs the phone number
# upfront. We capture it via /set-number BEFORE triggering pair/start. The
# captured number lands in channels.signal.accounts.<number> with
# enabled:false (real flip happens at finalize_pairing) so the wizard can
# pick up state on a reload.


def capture_number(bot_id: str, number: str) -> tuple[bool, str | None]:
    """Pre-write a placeholder ``channels.signal.accounts.<number>`` block
    so the wizard can resume after reload + the pair_qr step has a target
    account to bind to. Validates E.164 format; no signal-cli call yet.

    Returns ``(ok, error)``.
    """
    if not is_valid_e164(number):
        return False, "number_invalid_e164"

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    sg = channels.setdefault("signal", {})
    for key, default in _DEFAULT_SIGNAL_CHANNEL_FIELDS.items():
        sg.setdefault(key, default)
    accounts = sg.setdefault("accounts", {})
    # Placeholder — enabled stays False until the pair_qr step completes.
    accounts.setdefault(number, {
        "enabled": False,
        "number": number,
        "configDir": _config_dir_for(bot_id, number),
        "deviceName": _device_name_for(bot_id),
    })

    return _oc_common.write_oc_config(bot_id, cfg)


# ── Pairing session API (thin wrappers over _qr_pairing) ──────────────────────


def start_pairing_session(bot_id: str) -> dict[str, Any]:
    """Start a QR pairing session for ``bot_id``. Returns the session record.

    The session manager spawns ``openclaw channels login --channel signal``
    as the bot user; OC's bundled plugin in turn calls
    ``signal-cli link`` and emits the resulting ``tsdevice://`` URI as a
    QR code the operator scans in their phone's Signal app.
    """
    return _qr_pairing.start_session(bot_id, channel=SIGNAL_SKILL_ID)


def poll_pairing_session(session_id: str) -> dict[str, Any] | None:
    """Return the current snapshot of ``session_id``, or ``None`` if unknown."""
    return _qr_pairing.poll_session(session_id)


def cancel_pairing_session(session_id: str) -> bool:
    """Cancel an in-flight pairing session. Returns True if found."""
    return _qr_pairing.cancel_session(session_id)


def finalize_pairing(bot_id: str) -> InstallStatus:
    """Called by the admin server after a pairing session reaches ``paired``.

    Looks up the captured E.164 number for the bot (from the placeholder
    written by :func:`capture_number`), writes the channels.signal.
    accounts.<number> block with enabled:true to openclaw.json,
    kickstarts the gateway, and re-resolves status so the response
    carries the live state.
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"oc_read_failed: {err}",
        )

    channels = cfg.get("channels") or {}
    sg = channels.get("signal") or {}
    accounts = sg.get("accounts") or {}
    # Pick the most-recently-captured number (the placeholder with
    # enabled:false). Multi-account v2 will need a more deliberate
    # selector; for v1 the captured number is unambiguous because
    # capture_number only writes one at a time.
    target_number: str | None = None
    for number, block in accounts.items():
        if isinstance(block, dict) and block.get("number") == number:
            target_number = number
            if block.get("enabled") is False:
                break  # prefer a placeholder over an already-enabled one
    if target_number is None:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error="no_captured_number_found",
        )

    ok, err = enable_account_in_oc_config(bot_id, number=target_number)
    if not ok:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"oc_config_write_failed: {err}",
        )

    ok2, err2 = _oc_common.kickstart_gateway(bot_id)
    if not ok2:
        log.warning(
            "signal finalize kickstart failed for %s: %s", bot_id, err2,
        )

    return resolve_status(bot_id)


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": SIGNAL_SKILL_ID,
    "display_name": SIGNAL_ACCESS_PANEL["skill_display_name"],
    "summary": SIGNAL_ACCESS_PANEL["summary"],
    "access_panel": dict(SIGNAL_ACCESS_PANEL),
    "config_keys": [
        "accounts.<number>.number",
        "accounts.<number>.configDir",
        "accounts.<number>.deviceName",
    ],
}
