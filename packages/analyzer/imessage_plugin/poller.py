"""imessage_plugin.poller — iMessage primary-channel daemon (V2.1-6, Layer 2).

.. deprecated:: 2026-06-04 (bundled-plugin rewire)

    This poller is DEAD CODE post-rewire. The 2026-06-04 iMessage rewire
    moved the inbound + outbound paths to OC's bundled ``@openclaw/imessage``
    plugin at ``dist/extensions/imessage/``. Three reasons this module
    is now unreachable:

      1. ``install_imessage_poller`` in ``deploy.py`` was removed from the
         ``deploy_bot`` step pipeline (the LaunchDaemon is no longer
         installed for any bot).
      2. The poller POSTs to ``http://127.0.0.1:<port>/api/chat`` which
         **was never a real endpoint** on OC's gateway — confirmed live
         via curl probe + grep against OC v2026.6.1 (``/api/chat`` only
         appears as a prefix for ``/api/chat/media/outgoing/``). The
         original V2.1-6 design was structurally broken at the wire level.
      3. The bundled OC plugin handles chat.db polling internally and
         hands messages to the bot's session loop directly — bypassing
         HTTP entirely.

    Deletion is queued as a follow-on cleanup PR alongside removal of
    ``install_imessage_poller`` from ``deploy.py``. Kept on disk for
    one PR cycle so the cleanup diff stays reviewable.

This is the receive side of the iMessage integration. Polls chat.db every
15 seconds for new messages addressed to the bot's configured iMessage handle,
filters by the allowed_senders allowlist, and forwards each new message as a
turn event to the bot's OpenClaw gateway.

Architecture decision: shared evolve-account vs per-bot Messages.app
--------------------------------------------------------------------
There are two possible architectures:

OPTION A — Per-bot Messages.app account (ideal, operationally heavy)
    Each bot that uses iMessage as a primary channel has its own Apple ID
    signed in to Messages.app on the mini. Incoming messages land in a
    separate account so there's zero cross-contamination between bots.
    Downside: each Apple ID requires an iPhone (or Apple device) to create,
    2FA, and periodic re-authentication. For a pod with 5+ bots, this is
    operationally expensive.

OPTION B — Shared evolve-account Messages.app + per-bot allowlist (chosen for v1)
    A single Messages.app instance runs under the mac admin account (or
    dedicated iMessage account). All bots share one inbox. The poller
    disambiguates by looking at the bot's configured allowed_senders list:
    only messages from senders on a bot's allowlist are forwarded to that bot.
    Simpler operationally; less isolated. Suitable for the household/small-team
    use case where the pod admin controls who has the contact.

v1 ships Option B. Option A remains documented here for when per-bot isolation
is needed (e.g. a Carla-shape pod with multiple clients each texting a
different bot's number).

Conversation state
------------------
The poller tracks the ``last_polled_rowid`` (highest message ROWID seen) in a
state file at ``<bot_workspace>/imessage_poller_state.json``. On startup it
reads the current max ROWID and uses it as the initial watermark so it does
not replay historical messages.

Gateway API
-----------
Forwards turn events via HTTP POST to the bot's gateway endpoint:
  POST http://127.0.0.1:<port>/api/chat
  {
    "message": "<message text>",
    "session": "imessage-<sender_handle>",
    "metadata": {
      "channel": "imessage",
      "sender": "<sender_handle>",
      "rowid": <int>,
      "guid": "<str>",
    }
  }

Each sender gets their own session id (``imessage-<sender_handle>``) so
conversation continuity is preserved per contact.

Privacy
-------
No message content is logged. The poller logs sender handles and rowids
for diagnostic purposes only (at DEBUG level). At INFO level only
event counts are logged.

Running
-------
Invoked by the per-bot LaunchAgent (see deploy.py):
  python3 -m imessage_plugin.poller \\
      --bot-id <bot_id> \\
      --port <gateway_port> \\
      --poll-interval 15

Or directly for debugging:
  cd /Users/Shared/evolve-repo/packages/analyzer
  python3 -m imessage_plugin.poller --bot-id team_bot_a --port 18080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("imessage_poller")

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_POLL_INTERVAL_S: int = 15
STATE_FILE_NAME = "imessage_poller_state.json"

# ── Poller class ──────────────────────────────────────────────────────────────


class IMessagePoller:
    """Poll chat.db for new messages and forward them to the gateway.

    Args:
        bot_id: Logical bot id (used for config lookup and session naming).
        gateway_port: Port the bot's OpenClaw gateway listens on.
        config: The parsed imessage.json config dict for this bot.
        db_path: Override the chat.db path (for testing).
        poll_interval: Seconds between polls.
        workspace_dir: Override the workspace dir for state files (for testing).
    """

    def __init__(
        self,
        bot_id: str,
        gateway_port: int,
        config: dict,
        *,
        db_path: Optional[Path] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL_S,
        workspace_dir: Optional[Path] = None,
    ) -> None:
        self.bot_id = bot_id
        self.gateway_port = gateway_port
        self.config = config
        self.poll_interval = poll_interval

        # iMessage handle and allowlist from config
        self.bot_handle: str = config.get("handle") or ""
        self.allowed_senders: frozenset[str] = frozenset(
            s.strip() for s in (config.get("allowed_senders") or []) if s.strip()
        )
        # If allowed_senders is empty → open mode (any sender is forwarded)
        self.open_mode: bool = len(self.allowed_senders) == 0

        # DB path
        from evolve_admin.skills.imessage_helpers import chat_db_path
        self.db_path: Path = db_path or chat_db_path()

        # State file for last-seen ROWID watermark
        if workspace_dir is not None:
            self.state_path = workspace_dir / STATE_FILE_NAME
        else:
            # Default: ~/.openclaw/workspace/evolve/imessage_poller_state.json
            # Use bot_home() to handle the team_bot_b/personal_bot_user case.
            try:
                from evolve_admin.config import bot_home
            except Exception:
                from evolve_config import bot_home  # type: ignore[no-redef]
            self.state_path = (
                bot_home(bot_id) / ".openclaw" / "workspace" / "evolve" / STATE_FILE_NAME
            )

        self._last_rowid: int = 0
        self._running: bool = False

    # ── State management ──────────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load the last-seen ROWID from the state file."""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._last_rowid = int(data.get("last_rowid", 0))
                log.info("poller: loaded state, last_rowid=%d", self._last_rowid)
            else:
                # First run: initialize watermark to the current max ROWID so
                # we don't replay history.
                self._last_rowid = self._get_max_rowid()
                self._save_state()
                log.info("poller: first run, initialized last_rowid=%d", self._last_rowid)
        except Exception as exc:
            log.warning("poller: could not load state: %s; starting from 0", exc)
            self._last_rowid = 0

    def _save_state(self) -> None:
        """Persist the current ROWID watermark atomically."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"last_rowid": self._last_rowid, "updated_at": datetime.now(tz=timezone.utc).isoformat()}),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            log.warning("poller: could not save state: %s", exc)

    # ── DB queries ────────────────────────────────────────────────────────────

    def _get_max_rowid(self) -> int:
        """Return the current max ROWID in the message table, or 0 on error."""
        try:
            import sqlite3
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    def _poll_new_messages(self) -> list[dict]:
        """Return messages with ROWID > self._last_rowid, from non-self handles.

        Filters:
        - is_from_me = 0 (only incoming messages)
        - ROWID > last_rowid (only new since last poll)
        - handle.id in allowed_senders OR open_mode

        Returns a list of message dicts (same shape as imessage_helpers).
        """
        try:
            import sqlite3
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT
                        m.ROWID        AS rowid,
                        m.guid         AS guid,
                        m.text         AS text,
                        h.id           AS handle_id,
                        m.date         AS date,
                        m.service      AS service
                    FROM message m
                    LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.ROWID > ?
                      AND m.is_from_me = 0
                      AND m.text IS NOT NULL
                      AND m.text != ''
                    ORDER BY m.ROWID ASC
                    """,
                    (self._last_rowid,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            log.warning("poller: DB query failed: %s", exc)
            return []

        messages: list[dict] = []
        for row in rows:
            handle = row["handle_id"] or ""
            if not handle:
                continue
            if not self.open_mode and handle not in self.allowed_senders:
                log.debug("poller: skipping message from non-allowlisted sender")
                continue
            messages.append({
                "rowid": row["rowid"],
                "guid": row["guid"],
                "text": row["text"] or "",
                "handle_id": handle,
                "date": row["date"],
                "service": row["service"] or "iMessage",
            })

        return messages

    # ── Gateway forwarding ────────────────────────────────────────────────────

    def _forward_to_gateway(self, msg: dict) -> bool:
        """POST a message to the bot's gateway. Returns True on success."""
        session_id = f"imessage-{msg['handle_id']}"
        payload = json.dumps({
            "message": msg["text"],
            "session": session_id,
            "metadata": {
                "channel": "imessage",
                "sender": msg["handle_id"],
                "rowid": msg["rowid"],
                "guid": msg.get("guid", ""),
            },
        }).encode("utf-8")

        url = f"http://127.0.0.1:{self.gateway_port}/api/chat"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                log.debug("poller: forwarded rowid=%d, status=%d", msg["rowid"], resp.status)
                return True
        except urllib.error.URLError as exc:
            log.warning("poller: gateway forward failed (rowid=%d): %s", msg["rowid"], exc)
            return False
        except Exception as exc:
            log.warning("poller: unexpected error forwarding rowid=%d: %s", msg["rowid"], exc)
            return False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the poll loop. Blocks until interrupted."""
        self._running = True

        # Handle SIGTERM gracefully
        def _handle_signal(sig: int, frame: object) -> None:
            log.info("poller: received signal %d, stopping", sig)
            self._running = False

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        self._load_state()

        log.info(
            "poller: starting for bot=%s port=%d interval=%ds open_mode=%s",
            self.bot_id, self.gateway_port, self.poll_interval, self.open_mode,
        )

        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                log.error("poller: unexpected error in poll cycle: %s", exc, exc_info=True)

            # Sleep in small chunks to respond to SIGTERM quickly
            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1)

        log.info("poller: stopped")

    def _poll_once(self) -> int:
        """Run one poll cycle. Returns number of messages forwarded."""
        messages = self._poll_new_messages()
        forwarded = 0

        for msg in messages:
            success = self._forward_to_gateway(msg)
            if success:
                forwarded += 1
            # Advance the watermark even on failure — the message won't be
            # re-tried (it would spam the gateway). The operator can investigate
            # via logs. Advancing ensures we don't get stuck on a bad message.
            if msg["rowid"] > self._last_rowid:
                self._last_rowid = msg["rowid"]

        if messages:
            self._save_state()
            log.info("poller: processed %d messages, forwarded %d", len(messages), forwarded)

        return forwarded


# ── Config loading ────────────────────────────────────────────────────────────


def _load_imessage_config(bot_id: str) -> dict:
    """Load the imessage.json config for a bot. Returns empty dict if not found."""
    _IMESSAGE_CONFIG_PATH = ".openclaw/skills/imessage.json"
    try:
        from evolve_admin.config import bot_home
        try:
            from evolve_admin.skills.imessage_install import IMESSAGE_CONFIG_PATH as _IMESSAGE_CONFIG_PATH  # noqa: F841
        except Exception:
            pass
    except Exception:
        from evolve_config import bot_home  # type: ignore[no-redef]
    cfg_path = bot_home(bot_id) / _IMESSAGE_CONFIG_PATH

    try:
        if cfg_path.exists():
            return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("poller: could not load imessage config for %s: %s", bot_id, exc)
    return {}


# ── CLI entry point ───────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="iMessage primary-channel poller for Evolve bots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bot-id", required=True, help="Logical bot id")
    p.add_argument("--port", type=int, required=True, help="Gateway port")
    p.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"Seconds between polls (default {DEFAULT_POLL_INTERVAL_S})",
    )
    p.add_argument(
        "--db-path",
        help="Override path to chat.db (for testing)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default INFO)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    config = _load_imessage_config(args.bot_id)
    if not config:
        log.warning("poller: no imessage config found for %s — running in open mode", args.bot_id)

    db_path: Optional[Path] = Path(args.db_path) if args.db_path else None

    poller = IMessagePoller(
        bot_id=args.bot_id,
        gateway_port=args.port,
        config=config,
        db_path=db_path,
        poll_interval=args.poll_interval,
    )
    poller.run()


if __name__ == "__main__":
    main()
