"""
audit_pod_config.py — Sync admin-side network.json → per-bot pod_config.json.

The audit runner runs as the bot user and cannot read pod-wide
``network.json`` (it lives at ``/Users/Shared/evolve/network.json``, owned
by ``evolve``, not world-readable). It also doesn't need the whole config —
just the audit-relevant slice. This module renders that slice as a small
JSON file under each bot's workspace and keeps it in sync.

Lifecycle:
  - Written during ``deploy_bot`` so every freshly-deployed bot has a
    pod_config.json before its first audit fires.
  - Rewritten by ``sync_all_pods()`` whenever ``network.json`` saves
    (admin server hooks ``save_network`` → ``sync_all_pods``).
  - Read by ``app_audit_runner.py`` at the start of each tick. Re-reads
    each invocation so config changes propagate within the hourly cycle
    without restarting the bot's LaunchDaemon.

Path: ``/Users/<bot>/.openclaw/workspace/evolve/pod_config.json``.

The runner doesn't ship without this file — its absence is a deploy bug,
not a soft fallback. But to keep the runner crash-resilient, we ALSO
embed a sensible default copy in the runner itself; the runner uses the
synced file when present and the default when not (logging a warning).

See docs/spec-app-audit-2026-05-16.md §7.5.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .manifest import (
    AUDIT_CADENCE_MONTHLY,
    DEFAULT_AUDIT_CADENCE,
    effective_audit_cadence,
)


logger = logging.getLogger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────


def pod_config_path(bot_user: str) -> Path:
    """Absolute path to the bot's synced pod config file.

    Platform-keyed via ``user_home`` (``/Users/<user>`` on macOS,
    ``/home/<user>`` on Linux). A hardcoded ``/Users/<bot>`` here was the last
    residual ``/Users`` writer on a Linux fresh install (W10-G round-6): the
    ``save_network`` → ``sync_all_pods`` hook materialised a root-owned
    ``/Users/<bot>/.openclaw/workspace/evolve/pod_config.json`` tree even though
    every other path had been platform-keyed.
    """
    from ..config import user_home
    return user_home(bot_user) / ".openclaw" / "workspace" / "evolve" / "pod_config.json"


# ── Rendering ────────────────────────────────────────────────────────────────


def render_pod_config(network: dict, bot_id: str) -> dict[str, Any]:
    """Render the audit-relevant slice of network.json for *bot_id*.

    Pre-resolves the per-bot effective cadence so the runner never has to
    know about per-bot overrides — it just reads its single ``audit.cadence``
    value. Same pattern for ceilings and feature flags.

    Returns a dict suitable for ``json.dump`` to ``pod_config.json``.
    """
    app_audit = (network or {}).get("app_audit") or {}
    ceilings = app_audit.get("ceilings") or {}

    effective_cadence = effective_audit_cadence(None, bot_id, network)

    # ── Infra audit (Workstream B-infra) ────────────────────────────────────
    # Pod-wide settings (not per-bot — the runner is admin-side). Synced
    # into the per-bot pod_config purely for transparency so each bot can
    # show "the pod's infra audit runs daily" in its own context. The
    # admin runner reads the same block from network.json directly.
    infra_audit = (network or {}).get("infra_audit") or {}
    infra_ceilings = infra_audit.get("ceilings") or {}

    return {
        "schema_version": 1,
        "audit": {
            "cadence": effective_cadence,
            "calibration_mode": bool(app_audit.get("calibration_mode", True)),
            "audit_on_critical_structural": bool(
                app_audit.get("audit_on_critical_structural", True)
            ),
            "tier3_tier": app_audit.get("tier3_tier", "tier2"),
            "ceilings": {
                "max_auto_fix_per_run":   int(ceilings.get("max_auto_fix_per_run", 3)),
                "max_proposals_per_run":  int(ceilings.get("max_proposals_per_run", 5)),
                "max_tokens_per_audit":   int(ceilings.get("max_tokens_per_audit", 100_000)),
            },
        },
        # Pod-wide infrastructure audit settings. See
        # docs/spec-audit-extensions-2026-05-17.md §4.3 and
        # applications/infra_audit.py.
        "infra_audit": {
            "default_cadence": _validate_infra_cadence(
                infra_audit.get("default_cadence", "daily"),
            ),
            "calibration_mode": bool(infra_audit.get("calibration_mode", True)),
            "tier3_tier": infra_audit.get("tier3_tier", "tier2"),
            # Element-level overrides — e.g. {"acls": "off"} to silence the
            # ACL check on a pod where the operator has accepted a known
            # ACL drift. Empty by default.
            "element_overrides": dict(infra_audit.get("element_overrides") or {}),
            "ceilings": {
                "max_proposals_per_run": int(
                    infra_ceilings.get("max_proposals_per_run", 5)
                ),
                "max_tokens_per_audit": int(
                    infra_ceilings.get("max_tokens_per_audit", 100_000)
                ),
            },
        },
        # Workstream B-skills (spec-audit-extensions §4.1 / §4.2). Substrate-
        # audit policy resolved per-bot. Same shape as the app audit block
        # so the runner can branch on element type without translating.
        "skill_audit": _render_substrate_audit_slice(
            (network or {}).get("skill_audit") or {}, bot_id, network,
        ),
        "provider_audit": _render_substrate_audit_slice(
            (network or {}).get("provider_audit") or {}, bot_id, network,
        ),
    }


_VALID_SUBSTRATE_CADENCES = ("never", "quarterly", "monthly", "weekly", "daily")


def _render_substrate_audit_slice(
    cfg: dict, bot_id: str, network: dict,
) -> dict[str, Any]:
    """Render the skill/provider audit slice with per-bot overrides resolved.

    Same three-layer precedence as app audit (pod default → per-bot →
    per-element manifest), but substrate elements don't have manifests
    in v1 so we resolve pod default + per-bot only. ``cfg`` is the
    network.json substrate-audit block (skill_audit or provider_audit).
    """
    ceilings = cfg.get("ceilings") or {}
    per_bot = (cfg.get("bot_cadence") or {}).get(bot_id)
    pod_default = cfg.get("default_cadence", "weekly")
    cadence = per_bot if per_bot in _VALID_SUBSTRATE_CADENCES else (
        pod_default if pod_default in _VALID_SUBSTRATE_CADENCES else "weekly"
    )
    return {
        "default_cadence": cadence,
        "calibration_mode": bool(cfg.get("calibration_mode", True)),
        "ceilings": {
            "max_auto_fix_per_run":   int(ceilings.get("max_auto_fix_per_run", 3)),
            "max_proposals_per_run":  int(ceilings.get("max_proposals_per_run", 5)),
            "max_tokens_per_audit":   int(ceilings.get("max_tokens_per_audit", 100_000)),
        },
    }


def _validate_infra_cadence(value: Any) -> str:
    """Allow only the cadences the infra runner understands. Mirrors the
    audit cadence validation in manifest.py but lives here because the
    infra runner has a narrower set."""
    valid = ("off", "daily", "weekly", "manual")
    if isinstance(value, str) and value in valid:
        return value
    return "daily"


def default_pod_config() -> dict[str, Any]:
    """Fallback shape when no synced file is present.

    The runner uses this when ``pod_config.json`` is missing — a freshly-
    deployed bot whose first audit fires before the admin server's sync
    hook ran. Defaults match ``config._default_network``'s app_audit block
    so behavior matches a freshly-installed pod.
    """
    return {
        "schema_version": 1,
        "audit": {
            "cadence": DEFAULT_AUDIT_CADENCE,
            "calibration_mode": True,
            "audit_on_critical_structural": True,
            "tier3_tier": "tier2",
            "ceilings": {
                "max_auto_fix_per_run": 3,
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100_000,
            },
        },
        "infra_audit": {
            "default_cadence": "daily",
            "calibration_mode": True,
            "tier3_tier": "tier2",
            "element_overrides": {},
            "ceilings": {
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100_000,
            },
        },
        "skill_audit": {
            "default_cadence": "weekly",
            "calibration_mode": True,
            "ceilings": {
                "max_auto_fix_per_run": 3,
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100_000,
            },
        },
        "provider_audit": {
            "default_cadence": "weekly",
            "calibration_mode": True,
            "ceilings": {
                "max_auto_fix_per_run": 3,
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100_000,
            },
        },
    }


# ── Write strategy ──────────────────────────────────────────────────────────
#
# The file lands under the bot's workspace, which is bot-owned. The admin
# server runs as ``evolve`` and writes via the same /tmp staging + sudo cp
# pattern used for INSTALLED_APPS.md, manifests, etc. (See CLAUDE.md.)
# Sudoers grant: ``evolve ALL=(root) NOPASSWD: /bin/cp /tmp/evolve-pod-config-*.json
# /Users/*/.openclaw/workspace/evolve/pod_config.json``.


def write_pod_config(network: dict, bot_id: str, bot_user: str) -> bool:
    """Render and write the bot's pod_config.json. Returns True on success.

    Idempotent: if the rendered content matches what's already on disk,
    skip the write (and the sudo invocation) so the file's mtime doesn't
    change unnecessarily. Operators reading the file timestamp to gauge
    freshness see "this only changes when policy changes."
    """
    dest = pod_config_path(bot_user)
    new_content = json.dumps(render_pod_config(network, bot_id), indent=2)

    # Idempotency check.
    try:
        existing = dest.read_text()
        if existing.strip() == new_content.strip():
            return True
    except PermissionError:
        # Fall through to sudo /bin/cat for the comparison check.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(dest)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() == new_content.strip():
                return True
        except subprocess.SubprocessError:
            pass
    except (FileNotFoundError, OSError):
        pass  # File doesn't exist yet — proceed with the write.

    # Direct write first (works when evolve has write ACL on workspace/evolve/).
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_content)
        return True
    except PermissionError:
        pass
    except OSError as exc:
        logger.warning("pod_config: direct write failed for %s: %s", bot_user, exc)
        # Fall through to sudo path.

    # Fallback: /tmp staging + sudo /bin/cp.
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-pod-config-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        # mkdir the parent on the bot side if missing — using sudo since
        # we're writing into a bot-owned tree.
        try:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(dest.parent)],
                capture_output=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            logger.warning(
                "pod_config: sudo cp failed for %s: %s",
                bot_user, r.stderr.strip()[:200],
            )
            return False
        # Make the file readable by the bot (the audit_runner reads it).
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(dest)],
            capture_output=True, timeout=5,
        )
        return True
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def sync_all_pods(network: dict | None = None) -> dict[str, bool]:
    """Sync pod_config.json to every bot in the network. Returns {bot_id: ok}.

    Called from the admin server's ``save_network`` hook so policy changes
    propagate immediately. Best-effort: a write failure on one bot doesn't
    stop the rest.
    """
    if network is None:
        try:
            from ..config import load_network
            network = load_network()
        except Exception as exc:
            logger.warning("pod_config: load_network failed: %s", exc)
            return {}

    bots = (network or {}).get("bots") or {}
    # Bot blocks can exist for bots that aren't pod members yet — the
    # add-bot wizard records a planned bot's purpose{} in network.json
    # before any account exists. Syncing those would attempt privileged
    # writes into a home directory that doesn't exist (worst case,
    # sudo-mkdir'ing a phantom /Users/<bot> that wedges the later real
    # account creation). When a members roster is present, it is the
    # source of truth for which bots are deployed — sync only those.
    members = (network or {}).get("members")
    member_set = (
        {str(m) for m in members}
        if isinstance(members, list) and members else None
    )
    out: dict[str, bool] = {}
    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            continue
        if member_set is not None and bot_id not in member_set:
            continue  # planned / not-yet-provisioned — nothing to sync
        bot_user = cfg.get("user") or bot_id
        try:
            out[bot_id] = write_pod_config(network, bot_id, bot_user)
        except Exception as exc:
            logger.warning("pod_config: write_pod_config crashed for %s: %s", bot_id, exc)
            out[bot_id] = False
    return out
