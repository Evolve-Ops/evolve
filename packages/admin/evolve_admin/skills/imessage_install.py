"""evolve_admin.skills.imessage_install — iMessage skill install flow.

.. note::
   **REWIRED 2026-06-04 to use OC's bundled @openclaw/imessage plugin**
   (PR following the OC coverage audit at
   ``docs/openclaw-coverage-audit-2026-06-04.md``). The 2026-05-30
   withdrawal was correct about the *Evolve* implementation being broken
   end-to-end — but the audit's framework missed that OC ships its own
   iMessage channel plugin at ``dist/extensions/imessage/``. The
   2026-06-04 audit caught the gap, and this module now mirrors the
   bundled-plugin pattern (compare ``runway_install.py``) instead of
   reimplementing the runtime adapter.

   What changed structurally:

     * **Inbound path** is owned by OC's bundled plugin. Our home-rolled
       ``imessage_plugin/poller.py`` + the ``install_imessage_poller``
       LaunchDaemon in ``deploy.py`` are now dead code (deprecated in
       this PR; deletion queued as a follow-on).
     * **Outbound path** is owned by OC's bundled plugin (imsg bridge).
       ``imessage_helpers.send_message`` survives for status probes but
       is no longer the runtime sender.
     * **Wiring** is via ``channels.imessage`` block + ``plugins.entries.
       imessage`` flag in the bot's ``openclaw.json``, written through
       ``_oc_install_common.write_oc_config`` (same as telegram /
       discord / slack), then kickstart.
     * **Status probe** is OC's own ``openclaw channels status
       --channel imessage --probe`` — same load-bearing rule from the
       2026-05-30 audit's F3: never report ``active`` from config
       presence alone.

   The three load-bearing failures from the deep audit dissolve because
   OC owns the code paths that failed:

     1. "set-handle never installed the LaunchDaemon" → there's no
        LaunchDaemon to install; OC's plugin runs inside the gateway.
     2. "SEND has zero runtime exposure" → OC's imsg bridge is the
        sender; the plugin advertises ``send_message`` as a channel
        tool to the bot.
     3. "Poller is one-way" → OC's plugin is bidirectional by design.

iMessage is a ``kind=local_system`` skill: Mac-only, TCC-permission-gated,
no OAuth, no cloud, no API key. The "skill" is the ability for any bot to:

  1. **Send** iMessages via OC's bundled plugin → imsg → Messages.app.
  2. **Read / receive** incoming iMessages via OC's bundled plugin →
     chat.db poll → channel-plugin message hand-off into the bot's
     session loop.

Both capabilities require operating-system-level permissions (TCC grants
to the *evolve* user that runs OC's gateway):

  * **Full Disk Access** (FDA) — required to read chat.db, which lives in
    ~/Library/Messages/. Without FDA, the file is accessible only to the
    process that owns it (Messages.app) and root.

  * **Automation → Messages.app** — required to drive Messages.app via
    AppleScript for sending. macOS sandboxes AppleScript automation per-app;
    the evolve service user must be granted permission to control Messages.app
    in System Settings → Privacy & Security → Automation.

Additionally, the bot operator must configure:

  3. **An iMessage handle** — the iCloud email or phone number that identifies
     this bot's Messages account. This is what contacts text to reach the bot
     when it's used as a primary channel.

Install flow state machine
--------------------------

    no_tcc_fda
        ↓ (user grants FDA in System Settings)
    no_tcc_automation
        ↓ (user grants Automation in System Settings)
    messages_not_running
        ↓ (user opens Messages.app)
    not_signed_in
        ↓ (user signs in to iMessage on this machine)
    handle_not_configured
        ↓ (operator enters bot's iMessage handle via UI)
    not_wired_to_oc
        ↓ (admin writes channels.imessage + plugins.entries.imessage,
           kickstarts the bot's gateway so OC loads the bundled plugin)
    oc_probe_failed
        ↓ (transient — OC probe returns not-connected; UI surfaces error
           and offers re-probe; usually clears on gateway restart)
    active

Each state has one or more install steps. The ``build_install_plan`` function
maps state → ordered list of InstallStep objects for the UI to drive.

Plex-test requirement
---------------------
Every user-facing string must be jargon-free. "Full Disk Access", "Automation",
and "System Settings" are acceptable (macOS UI terms). Acronyms like TCC,
AppleScript, ROWID, etc., must not appear in user-facing text.

Local-only privacy guarantee
----------------------------
No data leaves the machine. The install flow configures local permissions only.
The per-bot configuration goes into the bot's own ``openclaw.json`` and is
never uploaded to any external service.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _oc_install_common as _oc_common

log = logging.getLogger(__name__)

# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the iMessage skill.
IMESSAGE_SKILL_ID = "imessage"

#: Skill kind — "local_system" means TCC-permission-gated, local-only.
IMESSAGE_SKILL_KIND = "local_system"

#: Legacy filesystem marker path (pre-2026-06-04 rewire). Kept here so the
#: deprecated home-rolled poller can still find it during the transition;
#: deletion is queued as a follow-on cleanup PR.
IMESSAGE_CONFIG_PATH = ".openclaw/skills/imessage.json"

#: Default chat.db path on macOS. OC's bundled plugin uses this when
#: ``channels.imessage.dbPath`` isn't explicitly set. We always pass it
#: explicitly to make the config self-documenting on operator inspection.
IMESSAGE_DEFAULT_DB_PATH = str(Path("~/Library/Messages/chat.db").expanduser())

#: Default service mode. ``auto`` lets the plugin route via iMessage when
#: the recipient is on iMessage and fall back to SMS otherwise. Operator-
#: overrideable via openclaw.json; the wizard never asks (Plex-test).
IMESSAGE_DEFAULT_SERVICE = "auto"

#: Timeout for the live ``openclaw channels status`` probe. Short on
#: purpose — if the gateway hangs we'd rather return ``unknown`` than
#: block the admin UI.
IMESSAGE_PROBE_TIMEOUT_S = 12


# ── Plain-language access panel ───────────────────────────────────────────────

#: Describes what the bot will/won't do. Written for the Plex test.
#: Every line maps to a capability OC's bundled @openclaw/imessage plugin
#: actually delivers — the May-2026 audit's F5 rule (no aspirational
#: present-tense promises) is the load-bearing constraint here.
IMESSAGE_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": IMESSAGE_SKILL_ID,
    "skill_display_name": "iMessage",
    "summary": (
        "Lets this bot send and receive iMessages on this Mac. "
        "Messages are read from and sent through the Messages app on this machine — "
        "no data leaves your Mac, and no third-party service is involved."
    ),
    "will": [
        "Receive iMessages sent to the bot's iMessage address and respond",
        "Send replies in conversations the bot is already part of",
        "Send photos, links, and formatted text via the Messages app",
        "Show as a connection in this Mac's Messages app",
    ],
    "wont": [
        "Access your Apple ID password or account credentials",
        "Start brand-new conversations with people who haven't messaged the bot",
        "Read messages from contacts the bot isn't authorised to talk to",
        "Upload any message content to an external service",
        "Access iCloud data beyond what's already on this Mac",
    ],
    "where_credentials_live": (
        "No credentials are stored by Evolve. iMessage access is controlled entirely by "
        "macOS permissions (Full Disk Access + Automation) that you grant in System Settings. "
        "You can revoke these at any time in System Settings → Privacy & Security, "
        "or sign out of iMessage in the Messages app on this Mac."
    ),
    "kind": IMESSAGE_SKILL_KIND,
    "tcc_permissions_required": [
        {
            "name": "Full Disk Access",
            "why": "Needed to read conversation history from the Messages database on this Mac.",
            "settings_path": "System Settings → Privacy & Security → Full Disk Access",
        },
        {
            "name": "Automation → Messages",
            "why": "Needed to send messages through the Messages app.",
            "settings_path": "System Settings → Privacy & Security → Automation",
        },
    ],
}


# ── Install status ────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the iMessage skill install flow.

    Status values (state machine):

    * ``no_tcc_fda`` — Full Disk Access not granted; can't read chat.db.
    * ``no_tcc_automation`` — Automation (Messages.app) not granted; can't send.
    * ``messages_not_running`` — Messages.app is not currently running.
    * ``not_signed_in`` — Messages.app is running but not signed in to iMessage.
    * ``handle_not_configured`` — TCC OK, Messages running + signed in, but the
      bot's iMessage handle hasn't been entered yet.
    * ``not_wired_to_oc`` — handle captured but ``channels.imessage`` block
      isn't yet present in the bot's ``openclaw.json``. The wizard's
      ``set-handle`` endpoint writes the block and kickstarts the gateway,
      which transitions the bot to ``oc_probe_failed`` (transient) or
      ``active`` (steady-state) within a few seconds.
    * ``oc_probe_failed`` — OC's bundled @openclaw/imessage plugin is
      wired but its live probe (``openclaw channels status --channel
      imessage``) returns not-connected. Usually self-clears after
      gateway settle; persistence means TCC dropped or Messages.app
      isn't responding.
    * ``active`` — all prerequisites met AND the OC probe confirms the
      plugin is connected. Only this state lets the catalog read green.
    * ``unknown`` — pre-flight check failed; ``error`` has the detail.
    """

    bot_id: str
    status: str  # see docstring values
    tcc_fda_granted: bool = False
    tcc_automation_granted: bool = False
    messages_app_running: bool = False
    signed_in: bool = False
    imessage_handle: str | None = None  # the bot's iMessage account handle
    allowed_senders: list[str] = field(default_factory=list)
    active_since: str | None = None
    # ── OC-wiring fields (2026-06-04 rewire) ──────────────────────────────
    oc_channel_wired: bool = False  # channels.imessage block + plugin entry present
    oc_plugin_enabled: bool = False  # plugins.entries.imessage.enabled is True
    oc_probe_ok: bool = False        # live probe says the plugin is connected
    oc_probe_detail: str | None = None
    # ────────────────────────────────────────────────────────────────────
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": IMESSAGE_SKILL_ID,
            "kind": IMESSAGE_SKILL_KIND,
            "status": self.status,
            "tcc_fda_granted": self.tcc_fda_granted,
            "tcc_automation_granted": self.tcc_automation_granted,
            "messages_app_running": self.messages_app_running,
            "signed_in": self.signed_in,
            "imessage_handle": self.imessage_handle,
            "allowed_senders": self.allowed_senders,
            "active_since": self.active_since,
            "oc_channel_wired": self.oc_channel_wired,
            "oc_plugin_enabled": self.oc_plugin_enabled,
            "oc_probe_ok": self.oc_probe_ok,
            "oc_probe_detail": self.oc_probe_detail,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives to complete the iMessage install.

    Steps are in order. The ``id`` field is what the UI dispatches on;
    ``label`` is the human-readable progress string (Plex-test friendly).
    """

    id: str
    label: str
    description: str = ""
    endpoint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    access_panel: dict[str, Any] | None = None
    settings_link: str | None = None  # deep-link or instruction for System Settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "access_panel": self.access_panel,
            "settings_link": self.settings_link,
        }


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Build the ordered steps remaining for *status*.

    Steps accumulate — if the earliest blocking step is not completed, it
    appears first. Steps that are already satisfied do not appear.

    Returns an empty list if status is ``active`` (nothing to do).
    """
    if status.status == "active":
        return []

    if status.status == "unknown":
        return []

    plan: list[InstallStep] = []

    # Step 1: Full Disk Access
    if not status.tcc_fda_granted:
        plan.append(InstallStep(
            id="grant_fda",
            label="Allow Evolve to read conversation history",
            description=(
                "Open System Settings → Privacy & Security → Full Disk Access, "
                "then turn on the toggle next to the Evolve service. "
                "This lets the bot read your Messages conversation history on this Mac."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/check-tcc",
            payload={"bot_id": status.bot_id},
            access_panel=dict(IMESSAGE_ACCESS_PANEL),
            settings_link="x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        ))

    # Step 2: Automation grant
    if not status.tcc_automation_granted:
        plan.append(InstallStep(
            id="grant_automation",
            label="Allow Evolve to send messages through the Messages app",
            description=(
                "Open System Settings → Privacy & Security → Automation, "
                "then expand the Evolve entry and turn on the toggle next to Messages. "
                "This lets the bot send iMessages on your behalf."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/check-tcc",
            payload={"bot_id": status.bot_id},
            settings_link="x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
        ))

    # Step 3: Messages.app running
    if not status.messages_app_running:
        plan.append(InstallStep(
            id="open_messages",
            label="Open the Messages app",
            description=(
                "The Messages app must be running to send iMessages. "
                "Open Messages from your Applications folder or Dock, "
                "then click the button below to continue."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/check-tcc",
            payload={"bot_id": status.bot_id},
        ))

    # Step 4: Signed in to iMessage
    if not status.signed_in:
        plan.append(InstallStep(
            id="sign_in_imessage",
            label="Sign in to iMessage",
            description=(
                "In the Messages app, go to Messages → Settings → iMessage "
                "and sign in with an Apple ID. "
                "Each bot that will receive incoming messages needs its own Apple ID "
                "signed in on this machine."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/check-tcc",
            payload={"bot_id": status.bot_id},
        ))

    # Step 5: Configure bot's iMessage handle. The /set-handle endpoint
    # validates the handle, writes the bot's openclaw.json (channels.imessage
    # block + plugins.entries.imessage.enabled), and kickstarts the gateway
    # so OC's bundled @openclaw/imessage plugin loads. The legacy filesystem
    # marker file is no longer load-bearing — the channels.imessage block in
    # openclaw.json is the source of truth.
    if not status.imessage_handle:
        plan.append(InstallStep(
            id="set_handle",
            label="Enter this bot's iMessage address",
            description=(
                "Enter the email address or phone number that others will text "
                "to reach this bot. This is usually the Apple ID email used to "
                "sign in to iMessage on this machine."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/set-handle",
            payload={"bot_id": status.bot_id},
        ))
        return plan

    # Step 6: Wire OC's bundled @openclaw/imessage plugin. If the handle is
    # captured but the channel block isn't in openclaw.json (e.g. operator
    # set the handle via a v1 wizard, then this PR rolled out), the wizard
    # offers a one-click "Finish setup" that re-writes the OC config and
    # kickstarts the gateway.
    if status.imessage_handle and not status.oc_channel_wired:
        plan.append(InstallStep(
            id="wire_oc_channel",
            label="Finish setup",
            description=(
                "Connect this bot's iMessage address to the Messages app so it "
                "can start receiving and replying. This takes a few seconds and "
                "restarts the bot briefly."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/set-handle",
            payload={"bot_id": status.bot_id, "handle": status.imessage_handle,
                     "allowed_senders": status.allowed_senders, "rewire": True},
        ))
        return plan

    # Step 7: Probe failed despite wiring. Most often transient (gateway
    # mid-restart); offer re-probe.
    if status.oc_channel_wired and not status.oc_probe_ok:
        plan.append(InstallStep(
            id="reprobe",
            label="Check connection again",
            description=(
                "The bot is connected to iMessage but isn't responding yet. "
                "This usually clears within a minute after the bot restarts — "
                "check again, or open Messages and confirm you're signed in."
            ),
            endpoint=f"/api/skills/install/{IMESSAGE_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ))

    return plan


# ── OC channel wiring (the load-bearing rewire 2026-06-04) ────────────────────
#
# The bundled @openclaw/imessage plugin reads from ``channels.imessage`` in the
# bot's openclaw.json plus the ``plugins.entries.imessage.enabled`` flag.
# Without both keys (and a kickstart so the running gateway re-reads the file)
# OC's plugin doesn't load and inbound iMessages are silently dropped even
# though the operator entered a handle.
#
# Canonical shape, written by ``enable_channel_in_oc_config``::
#
#   {
#     "channels": {
#       "imessage": {
#         "enabled": true,
#         "handle": "<bot's iMessage address>",
#         "dbPath": "/Users/evolve/Library/Messages/chat.db",
#         "service": "auto",
#         "allowFrom": ["<sender 1>", ...]
#       }
#     },
#     "plugins": {"entries": {"imessage": {"enabled": true}}}
#   }
#
# Read/write/kickstart mechanics are shared with the other messaging skills
# via ``_oc_install_common`` (see ``telegram_install.enable_channel_in_oc_config``
# for the closest mirror). The channel-block *shape* is owned here.


#: Default channel-policy fields the gateway expects alongside ``handle``.
#: Mirrors OC's IMessageConfig defaults so an operator inspecting openclaw.json
#: doesn't see surprises.
_DEFAULT_IMESSAGE_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "service": IMESSAGE_DEFAULT_SERVICE,
}


def enable_channel_in_oc_config(
    bot_id: str,
    handle: str,
    *,
    allowed_senders: list[str] | None = None,
    db_path: str | None = None,
    service: str | None = None,
) -> tuple[bool, str | None]:
    """Merge ``channels.imessage`` + ``plugins.entries.imessage`` into the
    bot's openclaw.json and write it back.

    Idempotent — keys already present (e.g. operator-set ``allowFrom``) are
    preserved. ``handle`` and ``plugins.entries.imessage.enabled`` are always
    rewritten to the install-flow values.

    Caller is responsible for kickstarting the gateway after this returns
    successfully — pair with ``kickstart_gateway(bot_id)`` for the full
    set-and-reload dance. (Mirrors ``telegram_install.enable_channel_in_oc_config``.)
    """
    if not handle or not handle.strip():
        return False, "handle_empty"

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    imsg = channels.setdefault("imessage", {})
    for key, default in _DEFAULT_IMESSAGE_CHANNEL_FIELDS.items():
        imsg.setdefault(key, default)
    # Install flow is source of truth for these three fields:
    imsg["enabled"] = True
    imsg["handle"] = handle.strip()
    if service is not None:
        imsg["service"] = service
    # dbPath: always write the explicit path so the config is self-documenting;
    # OC will fall back to its own default if we omit it but operators reading
    # openclaw.json shouldn't have to guess.
    imsg["dbPath"] = db_path or IMESSAGE_DEFAULT_DB_PATH
    # allowFrom: only overwrite if explicitly provided. ``None`` preserves any
    # existing operator-set allowlist; ``[]`` clears it (open mode).
    if allowed_senders is not None:
        imsg["allowFrom"] = [s.strip() for s in allowed_senders if s and s.strip()]

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("imessage", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


def disable_channel_in_oc_config(bot_id: str) -> tuple[bool, str | None]:
    """Inverse of ``enable_channel_in_oc_config`` — clears the channel + plugin
    entries from openclaw.json. Preserves the parent ``channels`` and
    ``plugins.entries`` dicts so other channels stay wired.

    Idempotent — safe to call when nothing's wired.
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.get("channels") or {}
    if "imessage" in channels:
        channels["imessage"] = {"enabled": False}

    entries = (cfg.get("plugins") or {}).get("entries") or {}
    if "imessage" in entries:
        entries["imessage"] = {"enabled": False}

    return _oc_common.write_oc_config(bot_id, cfg)


def revoke_account(bot_id: str) -> tuple[bool, str | None]:
    """Tear down iMessage wiring for a bot.

    1. Clear ``channels.imessage`` + ``plugins.entries.imessage`` from
       openclaw.json (via ``disable_channel_in_oc_config``).
    2. Delete the legacy filesystem marker if present (left over from the
       pre-rewire install path).
    3. Kickstart the bot's gateway so OC reloads without the iMessage
       plugin.

    Best-effort: even if the marker delete fails, the OC-side disable +
    kickstart still happens so the dashboard truthfully shows the bot is
    disconnected. TCC grants (FDA + Automation) are NOT revoked here —
    those are pod-wide grants on the evolve user, not per-bot, and other
    bots may still need them.

    Returns ``(ok, error)``.
    """
    ok, err = disable_channel_in_oc_config(bot_id)
    if not ok:
        return False, err or "disable_failed"

    # Best-effort: clear the legacy filesystem marker
    try:
        from ..config import bot_home as _bh
        marker = _bh(bot_id) / IMESSAGE_CONFIG_PATH
        if marker.exists():
            try:
                marker.unlink()
            except PermissionError:
                subprocess.run(
                    ["sudo", "/bin/rm", "-f", str(marker)],
                    capture_output=True, text=True, timeout=5,
                )
    except Exception as exc:
        # Don't fail the revoke if the marker cleanup hiccups — the OC
        # config is already disabled, which is what matters.
        log.debug("imessage revoke: marker cleanup non-fatal: %s", exc)

    ok2, err2 = _oc_common.kickstart_gateway(bot_id)
    if not ok2:
        return False, err2 or "kickstart_failed"

    return True, None


def _read_oc_imessage_block(bot_id: str) -> tuple[dict | None, dict | None, str | None]:
    """Return ``(channels.imessage, plugins.entries.imessage, error)``.

    Either dict may be ``None`` if absent — distinguishes "missing" from
    "present-but-empty". On read failure, both are ``None`` and ``error``
    has the reason.
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return None, None, err or "oc_read_failed"
    channels = cfg.get("channels") or {}
    plugins_entries = (cfg.get("plugins") or {}).get("entries") or {}
    return channels.get("imessage"), plugins_entries.get("imessage"), None


def _probe_oc_channel_status(
    bot_id: str,
    *,
    timeout: int = IMESSAGE_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run ``openclaw channels status --channel imessage --probe --json``
    against the bot's gateway.

    Returns a dict with at least ``connected: bool`` and optional ``error``
    / ``detail`` keys. Hard timeout (default 12s) — if OC's CLI hangs we
    return ``{connected: False, error: "probe_timeout"}`` rather than
    blocking the admin UI.

    The CLI returns JSON to stdout when --json is set; we parse and look
    for ``connected``, ``state``, or ``status`` keys (OC's exact shape can
    shift across versions — be liberal in parsing, strict in classification).
    """
    # OC's openclaw CLI requires node + a config context. Run it via the
    # bot's per-bot config so the probe targets the right gateway.
    from ..config import bot_home as _bh
    bot_cfg = _bh(bot_id) / ".openclaw" / "openclaw.json"

    cmd = [
        "/opt/homebrew/bin/openclaw",
        "channels", "status",
        "--channel", "imessage",
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

    # Even non-zero exit can carry useful JSON (OC sometimes exits 1 when
    # not-connected but still emits valid status JSON). Always try to parse.
    parsed: dict[str, Any] = {}
    try:
        if r.stdout.strip():
            parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        parsed = {}

    # OC's JSON shape varies; check the most common signals in order.
    # Be skeptical: only return ``connected=True`` if we have explicit
    # positive evidence. ``state == "connected"`` or ``ok == True`` or
    # an explicit ``connected: true`` all qualify; everything else is
    # "not connected".
    if isinstance(parsed, dict):
        # Nested shape: {"imessage": {"connected": true}} or
        # {"channels": [{"id": "imessage", "connected": true}]}
        candidate = (
            parsed.get("imessage")
            or next(
                (c for c in (parsed.get("channels") or [])
                 if isinstance(c, dict) and c.get("id") == "imessage"),
                None,
            )
            or parsed  # flat shape
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


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    check_tcc_fda: "callable[[], bool] | None" = None,
    check_tcc_automation: "callable[[], bool] | None" = None,
    check_messages_running: "callable[[], bool] | None" = None,
    check_signed_in: "callable[[], tuple[bool, str | None]] | None" = None,
    read_config: "callable[[str], dict | None] | None" = None,
    read_oc_block: "callable[[str], tuple[dict | None, dict | None, str | None]] | None" = None,
    probe_oc_channel: "callable[[str], dict[str, Any]] | None" = None,
) -> InstallStatus:
    """Resolve the current install status for a bot.

    All checks are injectable callables for testability. The production wiring
    uses the real TCC/AppleScript checks from ``imessage_helpers`` plus the
    OC channel-block + probe helpers from this module; tests pass in stubs.

    Args:
        bot_id: The bot's logical id.
        check_tcc_fda: callable() → bool. Checks whether FDA is granted.
        check_tcc_automation: callable() → bool. Checks Automation grant.
        check_messages_running: callable() → bool. Checks if Messages.app is running.
        check_signed_in: callable() → (bool, handle_str | None).
        read_config: callable(bot_id) → dict | None. Reads per-bot legacy
            imessage.json marker (only used as a fallback handle source if
            OC's openclaw.json doesn't carry one yet).
        read_oc_block: callable(bot_id) → (channels.imessage | None,
            plugins.entries.imessage | None, error | None). Reads the OC
            wiring blocks. Defaults to ``_read_oc_imessage_block``.
        probe_oc_channel: callable(bot_id) → dict[str, Any]. Runs the live
            ``openclaw channels status`` probe. Defaults to
            ``_probe_oc_channel_status``. CRITICAL: never assume probe
            success without an explicit ``connected: True`` return — the
            May-incident anti-pattern was treating absent-probe as
            equivalent to passing.

    Returns:
        InstallStatus with the first blocking issue as the status value.
        Only the ``active`` state requires all of: TCC ok, Messages signed
        in, handle captured, OC channel block present, plugin enabled,
        AND live probe returns connected.
    """
    from .imessage_helpers import (
        chat_db_path,
        is_messages_app_running,
        is_signed_in_to_imessage,
    )

    # ── Default implementations ───────────────────────────────────────────────

    def _default_check_fda() -> bool:
        """Try to open chat.db — if readable, FDA is granted."""
        db = chat_db_path()
        try:
            if not db.exists():
                # DB doesn't exist yet (fresh macOS install or Messages never opened).
                # Try to stat ~/Library/Messages/ instead.
                messages_dir = Path("~/Library/Messages").expanduser()
                messages_dir.stat()
                return True  # directory is accessible
            db.stat()
            # Try a minimal read to confirm read access
            with open(db, "rb") as f:
                f.read(16)
            return True
        except PermissionError:
            return False
        except OSError:
            return False

    def _default_check_automation() -> bool:
        """Try a benign AppleScript against Messages to check automation grant."""
        import subprocess
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "Messages" to return (count of services)'],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _default_read_config(bid: str) -> "dict | None":
        import json
        from ..config import bot_home
        cfg = bot_home(bid) / IMESSAGE_CONFIG_PATH
        try:
            if cfg.exists():
                return json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        except PermissionError:
            import subprocess
            r = subprocess.run(
                ["sudo", "/bin/cat", str(cfg)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                try:
                    return json.loads(r.stdout)
                except Exception:
                    pass
        return None

    _check_fda = check_tcc_fda or _default_check_fda
    _check_auto = check_tcc_automation or _default_check_automation
    _check_running = check_messages_running or is_messages_app_running
    _check_signin = check_signed_in or is_signed_in_to_imessage
    _read_cfg = read_config or _default_read_config
    _read_oc = read_oc_block or _read_oc_imessage_block
    _probe_oc = probe_oc_channel or _probe_oc_channel_status

    # ── Run checks in dependency order ────────────────────────────────────────

    try:
        fda_ok = bool(_check_fda())
    except Exception as exc:
        return InstallStatus(bot_id=bot_id, status="unknown", error=f"fda_check_failed: {exc}")

    try:
        auto_ok = bool(_check_auto())
    except Exception as exc:
        return InstallStatus(bot_id=bot_id, status="unknown", error=f"automation_check_failed: {exc}")

    try:
        running = bool(_check_running())
    except Exception as exc:
        return InstallStatus(bot_id=bot_id, status="unknown", error=f"running_check_failed: {exc}")

    signed_in = False
    imessage_handle: str | None = None
    try:
        signed_in, imessage_handle = _check_signin()
    except Exception:
        pass

    # Read per-bot config for handle + allowed_senders
    config: dict = {}
    try:
        config = _read_cfg(bot_id) or {}
    except Exception:
        pass

    configured_handle: str | None = config.get("handle") or imessage_handle or None
    allowed_senders: list[str] = config.get("allowed_senders") or []
    active_since: str | None = config.get("active_since") or None

    # ── Determine status (first blocking issue wins) ──────────────────────────

    if not fda_ok:
        return InstallStatus(
            bot_id=bot_id,
            status="no_tcc_fda",
            tcc_fda_granted=False,
            tcc_automation_granted=auto_ok,
            messages_app_running=running,
            signed_in=signed_in,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
        )

    if not auto_ok:
        return InstallStatus(
            bot_id=bot_id,
            status="no_tcc_automation",
            tcc_fda_granted=True,
            tcc_automation_granted=False,
            messages_app_running=running,
            signed_in=signed_in,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
        )

    if not running:
        return InstallStatus(
            bot_id=bot_id,
            status="messages_not_running",
            tcc_fda_granted=True,
            tcc_automation_granted=True,
            messages_app_running=False,
            signed_in=False,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
        )

    if not signed_in:
        return InstallStatus(
            bot_id=bot_id,
            status="not_signed_in",
            tcc_fda_granted=True,
            tcc_automation_granted=True,
            messages_app_running=True,
            signed_in=False,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
        )

    if not configured_handle:
        return InstallStatus(
            bot_id=bot_id,
            status="handle_not_configured",
            tcc_fda_granted=True,
            tcc_automation_granted=True,
            messages_app_running=True,
            signed_in=True,
            imessage_handle=None,
            allowed_senders=allowed_senders,
        )

    # ── OC wiring stages (2026-06-04 rewire) ──────────────────────────────
    # The handle is captured, all TCC prerequisites are good. Now check
    # whether OC's bundled @openclaw/imessage plugin is actually loaded
    # and probe-reachable.

    try:
        oc_channel, oc_plugin_entry, oc_err = _read_oc(bot_id)
    except Exception as exc:
        oc_channel, oc_plugin_entry, oc_err = None, None, f"oc_read_failed: {exc}"

    # Pick up the handle/allowFrom from the OC block when it's authoritative
    # (the rewire treats channels.imessage as truth; the legacy filesystem
    # marker is only a fallback)
    if isinstance(oc_channel, dict):
        if oc_channel.get("handle"):
            configured_handle = oc_channel.get("handle")
        oc_allowed = oc_channel.get("allowFrom")
        if isinstance(oc_allowed, list) and oc_allowed:
            allowed_senders = [str(s) for s in oc_allowed if str(s).strip()]

    oc_channel_wired = (
        isinstance(oc_channel, dict)
        and bool(oc_channel.get("handle"))
        and oc_channel.get("enabled") is not False
    )
    oc_plugin_enabled = (
        isinstance(oc_plugin_entry, dict)
        and oc_plugin_entry.get("enabled") is True
    )

    if not (oc_channel_wired and oc_plugin_enabled):
        return InstallStatus(
            bot_id=bot_id,
            status="not_wired_to_oc",
            tcc_fda_granted=True,
            tcc_automation_granted=True,
            messages_app_running=True,
            signed_in=True,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
            oc_channel_wired=oc_channel_wired,
            oc_plugin_enabled=oc_plugin_enabled,
            oc_probe_ok=False,
            error=oc_err,
        )

    # Final stage: live probe via openclaw channels status. This is the
    # load-bearing check — never return ``active`` from config presence
    # alone (see deep-audit F3 + the May incident).
    try:
        probe = _probe_oc(bot_id)
    except Exception as exc:
        probe = {"connected": False, "error": f"probe_exception: {exc}"}

    if not probe.get("connected"):
        return InstallStatus(
            bot_id=bot_id,
            status="oc_probe_failed",
            tcc_fda_granted=True,
            tcc_automation_granted=True,
            messages_app_running=True,
            signed_in=True,
            imessage_handle=configured_handle,
            allowed_senders=allowed_senders,
            oc_channel_wired=True,
            oc_plugin_enabled=True,
            oc_probe_ok=False,
            oc_probe_detail=probe.get("detail") or probe.get("error"),
        )

    return InstallStatus(
        bot_id=bot_id,
        status="active",
        tcc_fda_granted=True,
        tcc_automation_granted=True,
        messages_app_running=True,
        signed_in=True,
        imessage_handle=configured_handle,
        allowed_senders=allowed_senders,
        active_since=active_since,
        oc_channel_wired=True,
        oc_plugin_enabled=True,
        oc_probe_ok=True,
        oc_probe_detail=probe.get("detail"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────

SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": IMESSAGE_SKILL_ID,
    "kind": IMESSAGE_SKILL_KIND,
    "display_name": IMESSAGE_ACCESS_PANEL["skill_display_name"],
    "summary": IMESSAGE_ACCESS_PANEL["summary"],
    "access_panel": dict(IMESSAGE_ACCESS_PANEL),
    # No provider_id — iMessage is non-OAuth; TCC permissions, not tokens.
    "config_keys": ["handle", "allowed_senders", "active_since"],
    # Channel-matrix platform honesty (docs/design-linux-port-2026-06-10.md
    # §8): upstream's @openclaw/imessage requires a macOS host (Messages.app
    # + chat.db). ``platforms`` lists the platform_profile names the skill
    # can be OFFERED on; absence of the field means platform-neutral. The
    # constraint is catalog data — consuming surfaces filter through
    # ``evolve_admin.skills.supported_on_host()``, never by naming skills.
    "platforms": ["macos"],
}
