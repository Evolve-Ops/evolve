"""evolve_admin.skills._oc_install_common — shared install-flow helpers.

Two mechanics are identical across every messaging-skill install (Telegram,
Slack, Discord, …):

1. **Read-merge-write the bot's openclaw.json** with the credential and the
   ``plugins.entries.<id>.enabled = true`` flag, using ``/tmp`` staging +
   ``sudo /bin/cp`` because the file is owned by the bot user and the
   evolve admin server can't sudo as the bot (per CLAUDE.md).
2. **Kickstart the bot's gateway** so the running process re-reads
   openclaw.json. Without this, the channel plugin doesn't load and inbound
   messages are silently dropped even though the credential is on disk.

The channel-block *shape* (which keys exist under ``channels.<id>``) differs
per skill, so each install module supplies that itself. This module owns
just the read/write/kickstart mechanics so they don't drift between skills.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


# Channel-connect hook (U1 activation fix, 2026-06-11). write_oc_config
# is the chokepoint every messaging-skill connect flows through, so the
# "this bot just gained its first messaging channel" transition is
# detected here and handed to briefing_activation — the recorded
# morning-briefing decision on a day-1 channel-less bot activates the
# moment a channel arrives. Injectable for tests; the default fires the
# real activation check. Never allowed to fail the config write.
_channel_connect_hook = None


def set_channel_connect_hook(fn) -> None:
    """Replace the first-messaging-channel hook (tests). ``None``
    restores the production default."""
    global _channel_connect_hook
    _channel_connect_hook = fn


def _default_channel_connect_hook(
    bot_id: str, *, before: set, after: set,
) -> None:
    from ..briefing_activation import on_channels_registered

    on_channels_registered(bot_id, before=before, after=after)


def bot_oc_json_path(bot_id: str) -> Path:
    """Return the bot's openclaw.json path (uses bot_home, not /Users/{bot_id})."""
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / ".openclaw" / "openclaw.json"


def read_oc_config(bot_id: str) -> tuple[dict | None, str | None]:
    """Read the bot's openclaw.json. Direct read first, ``sudo /bin/cat`` fallback.

    Returns ``(config_dict, error_message)``. On success the first field is
    the parsed dict and the second is None; on failure the first is None.
    """
    p = bot_oc_json_path(bot_id)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), None
    except PermissionError:
        pass
    except Exception as exc:
        return None, f"oc_read_failed: {exc.__class__.__name__}: {exc}"

    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None, f"sudo cat failed: {r.stderr.strip() or 'unknown'}"
        return json.loads(r.stdout), None
    except Exception as exc:
        return None, f"oc_read_failed: {exc.__class__.__name__}: {exc}"


def write_oc_config(bot_id: str, cfg: dict) -> tuple[bool, str | None]:
    """Atomically write ``cfg`` back as the bot's openclaw.json.

    Stages to ``/tmp``, ``sudo /bin/cp``s into place, then chowns to the
    bot user and chmods to **0600** — openclaw.json holds the gateway +
    messaging-channel tokens, so it must not be world-readable. Same
    pattern as deploy.py (``safe_write_bot_config``) and the L2
    UpdatePermissionConfig applier (see project_l1_l2_applier_architecture
    memory).
    """
    from ..config import bot_home as _bot_home, get_bot_user, load_network

    network = load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / ".openclaw" / "openclaw.json")

    # Snapshot the pre-write messaging-channel set so the post-write
    # hook can detect the zero→some transition. Best-effort: an
    # unreadable prior config reads as "no channels", which at worst
    # re-fires the (idempotent) activation check.
    from ..channels import enabled_messaging_channels_from_config

    prior_cfg, _prior_err = read_oc_config(bot_id)
    channels_before = enabled_messaging_channels_from_config(prior_cfg)

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-oc-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)

        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, dest],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"oc cp failed: {r.stderr.strip() or 'unknown'}"

        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", dest],
            capture_output=True, text=True, timeout=10,
        )
        # openclaw.json carries the gateway token + every messaging-channel
        # bot token, so it MUST be 0600 — never world-readable. This used to
        # chmod 0644 (and `cp` preserves a pre-existing 0644), which on the
        # channel-connect chokepoint (#2707 made connecting a channel the
        # day-1 activation flow) left every freshly-connected bot's token
        # world-readable on a multi-user box. chmod preserves the evolve-user
        # read ACL, so the admin read path is unaffected. (Grant:
        # _render_evolve_sudoers §4 — chmod 600 .../openclaw.json.)
        subprocess.run(
            ["sudo", "/bin/chmod", "600", dest],
            capture_output=True, text=True, timeout=10,
        )

        # The write landed — fire the first-messaging-channel hook on
        # the zero→some transition. Never lets a hook problem fail a
        # write that already succeeded.
        try:
            channels_after = enabled_messaging_channels_from_config(cfg)
            hook = _channel_connect_hook or _default_channel_connect_hook
            hook(bot_id, before=channels_before, after=channels_after)
        except Exception:
            log.exception(
                "channel-connect hook failed for %s (config write itself "
                "succeeded)", bot_id,
            )
        return True, None
    except Exception as exc:
        return False, f"oc_write_failed: {exc.__class__.__name__}: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def disable_channel_in_oc_config(
    bot_id: str,
    channel_id: str,
    *,
    remove_plugin_entry: bool = True,
) -> tuple[bool, str | None]:
    """Inverse of the per-skill ``enable_channel_in_oc_config`` helpers.

    Removes ``channels.<channel_id>`` from openclaw.json and (by default)
    ``plugins.entries.<channel_id>`` too. Idempotent: if neither key
    exists, returns ``(True, None)`` without touching disk — important
    because the generic Uninstall button can fire against a bot whose
    state was already cleared by a prior call.

    Used by:

    * ``api_skills_telegram_revoke`` / ``slack_revoke`` / ``discord_revoke``
      to close the deep-audit 2026-05-30 F2 finding (revoke used to only
      delete the marker file and leave ``channels.<id>`` dangling in
      openclaw.json, so OC kept trying to load a channel with no working
      credential and the operator saw "Configured" on the Skills page).
    * Per-skill ``disable_account_in_oc_config`` helpers when they discover
      a legacy bare-shape ``channels.<id> = {enabled: false, …}`` orphan
      with no ``accounts`` sub-key (whatsapp / signal in particular —
      see ``whatsapp_install.disable_account_in_oc_config``).

    Returns ``(ok, error_message)``. Caller is responsible for kickstart
    afterwards (separate concern; not every caller wants to restart the
    gateway mid-flow).
    """
    cfg, err = read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    dirty = False
    channels = cfg.get("channels")
    if isinstance(channels, dict) and channel_id in channels:
        channels.pop(channel_id, None)
        dirty = True

    if remove_plugin_entry:
        entries = (cfg.get("plugins") or {}).get("entries")
        if isinstance(entries, dict) and channel_id in entries:
            entries.pop(channel_id, None)
            dirty = True

    if not dirty:
        # Nothing to do — already clean. Avoid a needless openclaw.json
        # rewrite (which would burn a sudo cp + chown round trip for no
        # state change, and could spuriously bump the file mtime that
        # the gateway watches).
        return True, None

    return write_oc_config(bot_id, cfg)


def kickstart_gateway(bot_id: str) -> tuple[bool, str | None]:
    """Kickstart the bot's OpenClaw gateway so it re-reads openclaw.json.

    Restarts ``ai.openclaw.<bot>-gateway`` via the Scheduler seam — the
    same label produced by ``deploy.per_bot_gateway_plist_label``. Returns
    ``(ok, error_message)``. No health-poll: callers can re-check status
    afterwards if they need confirmation.
    """
    from ..runtime import get_scheduler
    label = f"ai.openclaw.{bot_id}-gateway"
    ok, out = get_scheduler().restart(label)
    if not ok:
        # The seam's runner never raises — timeouts/OSErrors surface as
        # (rc=1, message) and land here too.
        return False, f"launchctl kickstart failed: {out.strip() or 'unknown'}"
    return True, None
