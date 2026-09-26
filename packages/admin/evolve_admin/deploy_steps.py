"""Deploy-flow step UI + post-step verifications.

Two things live here so ``cli.py`` (a frozen hot-hazard file) doesn't have to
carry them:

  * ``_deploy_step`` — the padded "  <label> … ✅/❌" line printer the
    ``deploy`` / ``setup evolve-user`` flows use for every step. Pure stdout;
    ``fail`` exits non-zero.
  * ``verify_gateway_loaded_new_plugin`` — the post-condition that guarantees a
    deploy leaves the bot's OpenClaw gateway running the FRESHLY-installed
    plugin, not the old one.

Why the gateway verification exists (evolve-vps darwin, 2026-07-01, #3362): a
``deploy <bot>`` bounces the gateway only as a SIDE EFFECT of
``install_bot_gateway_plist()``. On every plugin-ONLY change the unit/plist is
byte-identical, so the bounce is the skip-path ``scheduler.restart()`` inside
``_install_job_ensuring_restart``. When that silently no-ops, the OLD gateway
process keeps holding the port, the install's own port-bind wait is satisfied by
it, and the deploy reports green while the gateway serves the OLD plugin until a
human restarts it. This module closes that gap with two verified, cross-platform
guarantees.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from .deploy import restart_gateway, verify_plugin_live
from .deploy_verify import verify_bot_gateway_running_new_plugin


def _deploy_step(label: str) -> "tuple[Any, Any]":
    """Print a padded step label. Returns (ok_fn, fail_fn)."""
    PAD = 40
    sys.stdout.write(f"  {label:<{PAD}}")
    sys.stdout.flush()

    def ok(msg: str = "done") -> None:
        print(f"✅ {msg}")

    def fail(msg: str) -> None:
        print(f"❌ {msg}")
        sys.exit(1)

    return ok, fail


def verify_gateway_loaded_new_plugin(
    bot_id: str, port: "int | None", deploy_began_at: float,
) -> None:
    """Post-condition for the gateway step: the deploy leaves the gateway
    running the FRESHLY-installed plugin — restart it if the plugin (re)install
    did not bounce it, then verify by a real plugin-liveness probe, not merely
    "the port is bound".

    See the module docstring for the bug this closes (evolve-vps darwin, #3362).

    Two verified guarantees, both cross-platform (the seam-based PID lookup and
    the POSIX ``ps -o lstart`` probe work on launchd and systemd alike):

      1. The gateway PID restarted AFTER ``deploy_began_at``. If not (a stale
         PID == the observed bug, or the gateway is down), force
         ``restart_gateway()`` once and re-check.
      2. The plugin actually answers on ``/evolve/status`` — proof the new
         plugin LOADED and is serving, distinct from "a process is up".

    A visible step line is printed either way; an unrecoverable failure exits
    non-zero (via ``_deploy_step``'s ``fail``) so the deploy can never report a
    phantom success.
    """
    ok, fail = _deploy_step(f"Verifying gateway loaded new plugin for {bot_id}...")

    # 1. Did the gateway process actually bounce since the deploy began?
    vres = verify_bot_gateway_running_new_plugin(
        bot_id=bot_id, deploy_began_at_epoch=deploy_began_at,
    )
    if not vres.ok:
        # The install's bounce was skipped / no-op'd (the observed bug), or the
        # gateway is not running. Force a restart, once, then re-verify.
        try:
            restart_gateway(bot_id)
        except Exception as e:
            fail(f"gateway did not restart and forced restart failed: {e}")
            return  # unreachable — fail() exits; keeps type-checkers happy
        # Give launchd/systemd a moment to publish the new PID before re-probing.
        time.sleep(2)
        vres = verify_bot_gateway_running_new_plugin(
            bot_id=bot_id, deploy_began_at_epoch=deploy_began_at,
        )
        if not vres.ok:
            fail(f"gateway still not restarted after forced restart: {vres.summary}")
            return

    # 2. Is the freshly-loaded plugin actually serving? (not merely "port bound")
    if port:
        live: "str | None" = None
        # Up to ~30s of patience — a cold VPS first-boot needs time to fork +
        # load the plugin; verify_plugin_live sleeps 3s per attempt internally.
        for _ in range(10):
            live = verify_plugin_live(bot_id, port)
            if live:
                break
        if not live:
            fail(
                f"gateway restarted but plugin did not answer /evolve/status on "
                f":{port} — check the bot's gateway.err.log"
            )
            return
        ok(live)
    else:
        # No port to probe HTTP — the PID-restart guarantee is the best we have.
        ok(vres.summary)
