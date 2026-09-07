"""HTTP route for the multi-pod hub switcher — multi-pod M2 §3.1(b).

The admin SPA can't read ``network.json`` directly, so this serves the
one thing the sidebar pod-switcher chrome needs in a single shot:

  GET /api/peers
      ``{ "current": { "name": <this pod's networkId>,
                       "version": <footer version string> },
          "peers":   [ { "name": ..., "adminBaseUrl": ... }, ... ] }``

      ``current.name`` fills the pod-identity gap the chrome has today
      (the sidebar shows only the logo + version, never ``networkId``).
      ``peers`` is the operator-maintained sibling registry from
      ``network.json::peers`` — **links only, no tokens** (the hard v1
      invariant). Malformed peer rows are dropped (``resolve_peers``) so
      the switcher never blocks on a bad entry.

This is a **read** route behind the admin server's existing device-auth
gate (``server._enforce_device_auth`` covers every non-exempt path; this
route is not exempt). It is emphatically **NOT** the future M3 deposit
route — that is the first non-loopback *write* and is reviewed
separately as auditor-grade. No liveness/health probing of siblings
happens here or anywhere in M2 (links-only; no CORS).

Spec: internal/design-multi-pod-2026-06-11.md §3, §3.1.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from flask import Flask, Response, jsonify

from ..config import load_network, resolve_peers

log = logging.getLogger(__name__)


def _pod_version_string() -> str:
    """The pod's version string — same value the sidebar footer renders.

    Mirrors ``server.api_version`` (``GET /api/version``), which is the
    canonical source the footer's ``#ui-version`` already shows. Kept here
    (rather than imported) so this blueprint stays self-contained and the
    no-growth-capped ``server.py`` is untouched; the ``v0.44-`` prefix must
    stay in sync with ``api_version`` if it is ever bumped.
    """
    try:
        commit = subprocess.check_output(
            ["git", "describe", "--always", "--tags", "--dirty"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001 — version is cosmetic; never 500 on it
        commit = "unknown"
    return f"v0.44-{commit}"


def register_peers_routes(app: Flask, network_path: Path) -> None:
    """Register ``GET /api/peers`` (multi-pod hub switcher)."""

    @app.get("/api/peers")
    def api_peers() -> Response:
        try:
            network = load_network(network_path)
        except Exception as exc:  # noqa: BLE001
            # A missing/corrupt network.json shouldn't break the chrome —
            # degrade to single-pod with an unknown name rather than 500.
            log.warning("peers: could not load network config: %s", exc)
            network = {}
        name = str(network.get("networkId") or "").strip() or "this-pod"
        peers = resolve_peers(network)
        return jsonify({
            "current": {"name": name, "version": _pod_version_string()},
            "peers": peers,
        })


__all__ = ("register_peers_routes",)
