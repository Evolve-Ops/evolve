"""
Network status reader — queries shared dir + OC CLI live status.
No direct reads of ~/.openclaw/ — uses OC CLI via oc_cli.py.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    load_network,
    get_bot_port,
    get_bot_user,
    user_home,
    DEFAULT_NETWORK_CONFIG,
)


def network_status(network_path: Path = DEFAULT_NETWORK_CONFIG) -> dict[str, Any]:
    """
    Return a status dict for every bot in the network.
    {
        "network_id": str,
        "shared_dir": str,
        "bots": {
            "mybot": {
                "role": str,
                "port": int,
                "live": bool,           # HTTP /evolve/status responded
                "live_data": dict,      # from HTTP (if live)
                "last_metric_date": str | None,
                "last_metric": dict | None,
            },
            ...
        }
    }
    """
    network = load_network(network_path)
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

    result: dict[str, Any] = {
        "network_id": network.get("networkId", "unknown"),
        "shared_dir": str(shared_dir),
        "primary": network.get("primary"),
        "bots": {},
    }

    members = network.get("members", [])
    bots_cfg = network.get("bots", {})
    security_bot_bot_id = (network.get("security_bot") or {}).get("bot")

    for bot_id in members:
        cfg = bots_cfg.get(bot_id, {})
        port = cfg.get("port") or get_bot_port(bot_id, network)
        role = cfg.get("role") or "member"
        if bot_id == security_bot_bot_id:
            role = "security_bot"

        # Per-bot module opts. Default-on modules (continuity_engine) read
        # True when the per-bot entry is missing; bots can opt out by
        # setting bots[<id>][continuity_engine][enabled] = false.
        _ce_cfg = cfg.get("continuity_engine")
        continuity_engine_enabled = (
            bool(_ce_cfg.get("enabled", True))
            if isinstance(_ce_cfg, dict)
            else True
        )

        bot_status: dict[str, Any] = {
            "role": role,
            "port": port,
            "multiUser": cfg.get("multiUser", False),
            "continuity_engine_enabled": continuity_engine_enabled,
            "live": False,
            "live_data": None,
            "last_metric_date": None,
            "last_metric": None,
            "proposal_count": 0,
        }

        # Try live OC CLI status
        bot_status["live"], bot_status["live_data"] = _oc_status(bot_id)
        # Enrich with OC-sourced fields when available
        if bot_status["live_data"]:
            ld = bot_status["live_data"]
            bot_status["oc_model"] = (
                ld.get("sessions", {}).get("defaults", {}).get("model")
                or ld.get("sessions", {}).get("defaultModel")
                or ld.get("agents", {}).get("defaults", {}).get("model")
            )
            bot_status["oc_sessions_active"] = ld.get("agents", {}).get("sessions")

        # Latest metric from shared dir (structure: metrics/YYYY-MM-DD/bot_id.json)
        metrics_dir = shared_dir / "metrics"
        if metrics_dir.exists():
            # Find the most recent date dir containing this bot's metric file
            date_dirs = sorted(
                [d for d in metrics_dir.iterdir() if d.is_dir()],
                key=lambda d: d.name,
                reverse=True,
            )
            for date_dir in date_dirs:
                bot_file = date_dir / f"{bot_id}.json"
                if bot_file.exists():
                    bot_status["last_metric_date"] = date_dir.name
                    try:
                        bot_status["last_metric"] = json.loads(bot_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
                    break

        # Compute UI-friendly status and model fields
        if bot_status["live"]:
            bot_status["status"] = "online"
        elif bot_status["last_metric_date"]:
            bot_status["status"] = "active"
        else:
            bot_status["status"] = "offline"
        # Get model from live_data (HTTP evolve/status) — OC CLI is too slow
        # live_data is from our /evolve/status endpoint which doesn't have model
        # Use last_metric if it has model, otherwise leave blank for now
        model = None
        if bot_status.get("last_metric"):
            model = bot_status["last_metric"].get("default_model")
        bot_status["model"] = model

        result["bots"][bot_id] = bot_status

    # Add the primary admin bot itself to the overview. Resolve its id via the
    # shared resolver (primary_bot.primary_bot_id), NOT the hardcoded "evolve"
    # literal — on a post-account-separation pod the primary is "evo"
    # (network.primary == "evo"), and the old literal injected a PHANTOM second
    # "evolve" tab beside the real primary (S2b,
    # internal/spec-evo-account-separation-2026-05-25.md). The resolver returns
    # "evolve" on legacy pods, so this stays backward-compatible by construction.
    # Mirrors the dashboard-card/uptime fix (#3057) and setup_status() (#3047/#3052).
    # The setup-checklist enricher iterates this same bots map, so keying under
    # the real primary id is also what stops it re-scaffolding bots.evolve.
    from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
    from primary_bot import primary_bot_gateway_port as _primary_bot_gateway_port  # type: ignore
    primary_id = _primary_bot_id(network)
    if primary_id:
        # primary_bot_gateway_port resolves bots[<primary>].port and already
        # carries the legacy top-level network["evolve"].gateway_port fallback
        # for pre-S1 pods, so no second lookup is needed here.
        primary_port = _primary_bot_gateway_port(network) or 19030
        _primary_ce_cfg = (
            (network.get("bots") or {}).get(primary_id, {}).get("continuity_engine")
        )
        _primary_ce_enabled = (
            bool(_primary_ce_cfg.get("enabled", True))
            if isinstance(_primary_ce_cfg, dict)
            else True
        )
        # When the primary is ALSO a network member (existing-bot-as-primary
        # setup), the member loop above already built its card — enrich that in
        # place rather than clobbering its multiUser/last_metric fields. In the
        # dedicated-primary setup the primary is NOT a member, so build a fresh
        # card.
        primary_status: dict[str, Any] = result["bots"].get(primary_id) or {
            "role": "primary",
            "port": primary_port,
            "continuity_engine_enabled": _primary_ce_enabled,
            "live": False,
            "live_data": None,
            "last_metric_date": None,
            "last_metric": None,
            "proposal_count": 0,
        }
        primary_status.setdefault("port", primary_port)
        try:
            import urllib.request as _urllib
            _url = f"http://localhost:{primary_port}/evolve/status"
            with _urllib.urlopen(_url, timeout=3) as _r:
                _data = json.loads(_r.read())
                primary_status["live"] = True
                primary_status["live_data"] = _data
        except Exception:
            pass
        if primary_status.get("live_data"):
            ld = primary_status["live_data"]
            primary_status["oc_model"] = (
                ld.get("sessions", {}).get("defaults", {}).get("model")
                or ld.get("sessions", {}).get("defaultModel")
                or ld.get("agents", {}).get("defaults", {}).get("model")
            )
            primary_status["oc_sessions_active"] = ld.get("agents", {}).get("sessions")
        # The admin server is serving this response, so the primary admin is
        # always live, and it always renders with the primary role.
        primary_status["live"] = True
        primary_status["status"] = "online"
        primary_status["role"] = "primary"
        result["bots"][primary_id] = primary_status

    # Proposals by stage
    result["pending_proposals"] = 0  # awaiting security review
    result["reviewed_proposals"] = 0  # passed security review, awaiting human approval
    result["rejected_proposals"] = 0  # auto-rejected by security reviewer
    for stage, key in (("pending", "pending_proposals"), ("reviewed", "reviewed_proposals"), ("rejected", "rejected_proposals")):
        d = shared_dir / "proposals" / stage
        if d.exists():
            result[key] = len([f for f in d.iterdir() if f.suffix == ".json"])

    return result


def _oc_status(bot_id: str) -> "tuple[bool, dict | None]":
    """
    Check bot liveness by hitting its evolve plugin endpoint directly.
    Falls back to oc_cli if port unknown.
    Returns (reachable, data).
    """
    import urllib.request
    from .config import load_network, DEFAULT_NETWORK_CONFIG, get_bot_port
    try:
        network = load_network(DEFAULT_NETWORK_CONFIG)
        port = get_bot_port(bot_id, network)
        if port:
            url = f"http://localhost:{port}/evolve/status"
            with urllib.request.urlopen(url, timeout=3) as r:
                import json
                data = json.loads(r.read())
                return True, data
    except Exception:
        pass
    # Fallback: try the runtime seam (may fail cross-user without sudo)
    try:
        from runtime.agent_runtime import get_runtime
        result = get_runtime().status(bot_id)
        if result is not None:
            return True, result
    except Exception:
        pass
    return False, None


def _macos_account_exists(username: str) -> bool:
    """True if the OS account exists (pwd lookup — POSIX-portable).

    Deliberately NOT routed through ``get_isolation().user_exists()``. As of
    the profile-keyed isolation factory (``runtime.isolation.get_isolation``
    now defaults to ``LinuxUserIsolation``/``getent`` when the active profile
    is Linux), the seam WOULD also be platform-correct in-daemon — so this is
    no longer a *correctness* workaround. But ``pwd.getpwnam`` stays the
    preference here: it resolves via nsswitch in-process on both macOS and
    Linux — the SAME mechanism ``user_home()`` relies on — so account-existence
    and home-resolution stay coherent with zero per-check subprocess and zero
    dependence on daemon-side adapter activation. (The function name is kept
    for diff minimality; it checks the OS account on whatever platform we run.)
    """
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def setup_status(network_path: Path = DEFAULT_NETWORK_CONFIG) -> dict[str, Any]:
    """Report first-time-setup state.

    A pod is "setup-complete" iff:
      - a primary bot RESOLVES via the shared resolver — i.e. one of:
        top-level ``network.primary``, the first ``bots{}`` entry whose
        ``role == "primary"``, or the legacy ``evolve`` fallback, AND
      - that resolved bot exists in network.json::bots, AND
      - the bot's macOS account exists, AND
      - the bot's openclaw.json file exists and parses.

    Failing any of these means setup is incomplete and the admin UI
    should drive the operator into the install-evo flow.

    Primary detection routes through ``primary_bot.primary_bot_id`` — the
    SAME resolver the rest of the engine uses — so the banner gate cannot
    disagree with the engine. Before this, the gate read only the top-level
    ``network.primary`` field, so a fresh-install pod with a working
    ``role:"primary"`` bot but an unset/legacy pointer (older provisioner
    builds never wrote ``network.primary``) falsely reported "no primary"
    and nagged the operator to install ``evo`` over a working primary.

    The downstream ``primary_registered`` check stays load-bearing: the
    resolver's top-level rule trusts ``network.primary`` WITHOUT confirming
    the named bot exists in ``bots{}``, so a genuinely-misconfigured pointer
    (primary names an unregistered bot) still fails registration and
    correctly fires the banner — this fix only flips the false-negative
    (working role:primary bot, missing pointer), never masks a real
    misconfiguration.

    Returns:
      {
        "setup_complete": bool,           # AND of all four detection booleans
        "has_primary": bool,              # a primary resolves via primary_bot_id
        "primary_bot_id": str | None,     # the resolved primary bot id
        "primary_registered": bool,       # primary_bot_id is in network.bots
        "primary_macos_account_ok": bool, # macOS account exists
        "primary_openclaw_json_ok": bool, # openclaw.json exists + parses
        "evo_install_target": "evo",      # bot_id used by the install-evo flow
      }
    """
    network = load_network(network_path)
    # Route through the shared resolver (analyzer package, on the admin
    # server's sys.path) so the gate agrees with the engine. Matches the
    # local-import style the rest of the admin uses for primary_bot — e.g.
    # web/server.py's api_gateway_status.
    from primary_bot import primary_bot_id as _resolve_primary_bot_id  # type: ignore
    primary_bot_id = _resolve_primary_bot_id(network)
    has_primary = bool(primary_bot_id)

    primary_registered = False
    primary_macos_account_ok = False
    primary_openclaw_json_ok = False

    if has_primary and primary_bot_id in (network.get("bots") or {}):
        primary_registered = True
        user = get_bot_user(primary_bot_id, network)
        primary_macos_account_ok = _macos_account_exists(user)
        if primary_macos_account_ok:
            # Resolve the primary's home cross-platform (pwd-first, profile
            # fallback) — never a hardcoded /Users/{user}. On a Linux pod the
            # evo home is /home/evo, so a /Users literal made this read miss
            # and fired the false install-evo banner. user_home() drives BOTH
            # the direct read below AND the sudo /bin/cat fallback (same
            # oc_path), so both legs are platform-correct. On macOS pwd returns
            # /Users/{user} — byte-identical to the old literal.
            oc_path = user_home(user) / ".openclaw" / "openclaw.json"
            try:
                json.loads(oc_path.read_text())
                primary_openclaw_json_ok = True
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
                # Fallback: evolve user may not have direct read; try sudo cat
                # (the same fallback the rest of the admin uses for bots
                # whose ACL isn't yet set up).
                try:
                    r = subprocess.run(
                        ["sudo", "-n", "/bin/cat", str(oc_path)],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        json.loads(r.stdout)
                        primary_openclaw_json_ok = True
                except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
                    pass

    setup_complete = (
        has_primary
        and primary_registered
        and primary_macos_account_ok
        and primary_openclaw_json_ok
    )

    return {
        "setup_complete": setup_complete,
        "has_primary": has_primary,
        "primary_bot_id": primary_bot_id,
        "primary_registered": primary_registered,
        "primary_macos_account_ok": primary_macos_account_ok,
        "primary_openclaw_json_ok": primary_openclaw_json_ok,
        "evo_install_target": "evo",
    }

