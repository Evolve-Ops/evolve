"""
evolve_admin.mcp_bridge.registry — Live bot registry backed by network.json.

Watches network.json for changes and maintains a current dict of BotInfo
objects. When bots are added or (re)configured in the pod, the registry
updates automatically — no restart required.

Refresh has three triggers, all routed through one ``_maybe_reload`` path:

* **Lazy mtime check on every read accessor** (``get`` / ``list`` /
  ``primary_id`` / ``bot_ids``). This is the load-bearing one: a config
  write by the Google setup wizard (or the CLI, or a hand-edit) is
  reflected on the *very next* tool call — no 30-second wait, no
  ``launchctl kickstart``. Mirrors the manifests-dir mtime trigger-cache
  pattern used elsewhere in Evolve. Historically a freshly-configured bot
  failed every Google tool call until a manual bridge restart (the bot then
  confabulated a wrong "OAuth token expired" diagnosis); the lazy reload
  closes that gap.
* **30-second background poll** as a backstop, so ``list_bots`` / the health
  endpoint stay current even when no tool call arrives.
* **SIGHUP** for an explicit external nudge.

Reads are torn-safe: a partial/unreadable ``network.json`` (a reader racing
the wizard's write) leaves the last-good state in place rather than crashing
the bridge or emptying the registry — see ``_reload``.

The registry is the single source of truth for which bots exist and what their
workspace roots are. All other bridge components read from it.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from evolve_admin.config import DEFAULT_NETWORK_CONFIG, get_bot_workspace, load_network, bot_home as _bot_home

from .workspace import BotWorkspace


@dataclass
class BotInfo:
    bot_id: str
    role: str          # "primary" | "member" | "security_bot"
    workspace: BotWorkspace

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"


class BotRegistry:
    """
    Thread-safe, live registry of bots in the pod.

    Usage:
        registry = BotRegistry()
        info = registry.get("admin_bot")   # BotInfo | None
        all_bots = registry.list()     # list[BotInfo]
        primary = registry.primary_id  # str | None
    """

    def __init__(
        self,
        network_path: Path = DEFAULT_NETWORK_CONFIG,
        poll_interval: int = 30,
        mcp_config: dict | None = None,
    ) -> None:
        self._path = network_path
        self._poll_interval = poll_interval
        self._mcp_config = mcp_config or {}

        self._bots: dict[str, BotInfo] = {}
        self._primary_id: str | None = None
        self._lock = threading.RLock()
        # Serializes reloads so concurrent readers (one per SSE connection)
        # coalesce into a single file read + swap instead of each doing I/O.
        self._reload_lock = threading.Lock()
        self._mtime: float = 0.0
        self._change_callbacks: list[Callable[[list[str], list[str]], None]] = []

        # Load immediately before returning to caller, then record the mtime so
        # the first lazy/poll check doesn't redundantly reload.
        ok, _, _ = self._reload(initial=True)
        if ok:
            self._mtime = self._stat_mtime()

        # Background watcher
        self._stop_event = threading.Event()
        self._watcher = threading.Thread(
            target=self._watch_loop, name="mcp-bridge-registry-watcher", daemon=True
        )
        self._watcher.start()

        # SIGHUP → immediate reload (Evolve sends this after bot add/remove)
        try:
            signal.signal(signal.SIGHUP, lambda *_: self._maybe_reload(force=True))
        except (OSError, ValueError):
            pass  # SIGHUP not available on Windows or in certain thread contexts

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, bot_id: str) -> BotInfo | None:
        self._maybe_reload()
        with self._lock:
            return self._bots.get(bot_id)

    def list(self) -> list[BotInfo]:
        self._maybe_reload()
        with self._lock:
            return list(self._bots.values())

    @property
    def primary_id(self) -> str | None:
        self._maybe_reload()
        with self._lock:
            return self._primary_id

    def bot_ids(self) -> list[str]:
        self._maybe_reload()
        with self._lock:
            return list(self._bots.keys())

    def on_change(self, cb: Callable[[list[str], list[str]], None]) -> None:
        """
        Register a callback invoked when the bot list changes.
        cb(added_ids, removed_ids) — called from the watcher thread.
        """
        self._change_callbacks.append(cb)

    def stop(self) -> None:
        self._stop_event.set()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _stat_mtime(self) -> float:
        """network.json mtime, or 0.0 if the file is briefly absent mid-write."""
        try:
            return self._path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self, force: bool = False) -> None:
        """Reload iff network.json's mtime advanced since the last good load.

        Runs on every read accessor so a config write (wizard / CLI /
        hand-edit) is reflected on the next tool call without a restart.
        The fast path is one ``stat`` + a float compare; the actual reload
        is serialized by ``_reload_lock`` so concurrent callers coalesce.

        ``_mtime`` is advanced ONLY when ``_reload`` succeeds, so a torn or
        unreadable file is retried on the following call (self-healing once
        the writer finishes) rather than being silently skipped.
        """
        mtime = self._stat_mtime()
        if not force and mtime == self._mtime:
            return
        added: list[str] = []
        removed: list[str] = []
        with self._reload_lock:
            # Re-stat under the lock: a concurrent caller may have just
            # reloaded while we waited.
            mtime = self._stat_mtime()
            if not force and mtime == self._mtime:
                return
            ok, added, removed = self._reload()
            if ok:
                self._mtime = mtime
        # Fire change callbacks OUTSIDE _reload_lock so a callback that touches
        # a read accessor (which would re-enter _maybe_reload) can't deadlock.
        if added or removed:
            self._fire_change_callbacks(added, removed)

    def _fire_change_callbacks(self, added: list[str], removed: list[str]) -> None:
        for cb in self._change_callbacks:
            try:
                cb(added, removed)
            except Exception:
                pass

    def _reload(self, initial: bool = False) -> tuple[bool, list[str], list[str]]:
        """Rebuild the bot map from network.json.

        Returns ``(ok, added, removed)``. ``added``/``removed`` are the
        membership delta the caller should announce via change callbacks
        (always empty when ``initial`` or when nothing changed); the caller
        fires them OUTSIDE ``_reload_lock`` to avoid callback re-entrancy.

        On a torn/unreadable read (a reader racing the wizard's non-atomic
        write) the last-good state is left untouched and ``ok=False`` is
        returned — the bridge stays up and the next call retries. A single
        malformed bot entry is skipped rather than aborting the whole reload.
        """
        try:
            net = load_network(self._path)
        except Exception:
            return False, [], []  # torn/unreadable — keep last-good, retry next call

        bots_cfg: dict = net.get("bots") or {}

        # Primary bot: MCP config overrides network.json "primary" field
        primary_id: str | None = (
            self._mcp_config.get("primary_context_bot")
            or net.get("primary")
            or (next(iter(bots_cfg)) if bots_cfg else None)
        )

        new_bots: dict[str, BotInfo] = {}
        for bot_id, cfg in bots_cfg.items():
            try:
                cfg = cfg if isinstance(cfg, dict) else {}
                ws_path = get_bot_workspace(bot_id)
                if ws_path is None:
                    ws_path = _bot_home(bot_id, net) / ".openclaw" / "workspace"
                new_bots[bot_id] = BotInfo(
                    bot_id=bot_id,
                    role=cfg.get("role") or "member",
                    workspace=BotWorkspace(bot_id, ws_path),
                )
            except Exception:
                # One garbage entry shouldn't blind the bridge to every
                # other bot — skip it and keep going.
                continue

        with self._lock:
            old_ids = set(self._bots)
            new_ids = set(new_bots)
            added = list(new_ids - old_ids)
            removed = list(old_ids - new_ids)

            self._bots = new_bots
            self._primary_id = primary_id

        if initial:
            return True, [], []
        return True, added, removed

    def _watch_loop(self) -> None:
        # Backstop for surfaces that read the registry without a tool call
        # in flight (health endpoint, list_bots). The lazy per-read check is
        # what makes wizard writes land on the next tool call.
        while not self._stop_event.wait(self._poll_interval):
            self._maybe_reload()
