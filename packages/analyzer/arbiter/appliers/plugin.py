"""arbiter.appliers.plugin — Enable / Disable / UpdateAllowDeny / UpdateBaseline.

Spec: internal/spec-plugin-inventory-2026-05-10.md §3.4.

Four appliers in one module — they share the openclaw.json + baseline
read/write helpers. Each registers itself with the applier dispatch on
import (see arbiter/appliers/__init__.py).

The first three (Enable/Disable/UpdateAllowDeny) touch a bot's
``openclaw.json``; they defer to ``evolve_admin.deploy.safe_write_bot_config``
which handles /tmp staging, schema validation, atomic .bak preservation,
and sudo /bin/cp + chown — same path Phase B of MCP used.

Auto-gateway-restart after a successful config write mirrors the MCP
appliers (Phase E of MCP). Restart failure is captured but doesn't roll
back; operator can fall back to manual restart from Maintenance.

The fourth (UpdatePluginBaseline) mutates the policy file under
{shared_dir} — no bot-side write, no restart needed.
"""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import (
    EnablePluginEntry,
    DisablePluginEntry,
    UpdatePluginAllowDeny,
    UpdatePluginBaseline,
    UpdatePluginConfig,
    UpdatePluginLoadPaths,
)


# Trusted directories permitted in plugins.load.paths additions (Phase C).
# Mirrors spec §5.4. Anything outside this whitelist requires expanding
# the pod baseline's expected_load_paths first.
_LOAD_PATH_WHITELIST = {
    "/Users/Shared/evolve-plugin",
    "/Users/Shared/evolve-plugin-staging",
}


# ── Shared dir resolution ─────────────────────────────────────────────────────

def _shared_dir() -> Path:
    """Resolve shared_dir at apply time (mirrors mcp_server.py pattern)."""
    import os
    candidates = [os.environ.get("EVOLVE_SHARED_DIR"), "/Users/Shared/evolve"]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return Path("/Users/Shared/evolve")


# ── openclaw.json helpers ─────────────────────────────────────────────────────

def _read_bot_oc_config(bot_id: str) -> tuple[dict | None, str | None]:
    from evolve_config import bot_home
    oc_path = bot_home(bot_id) / ".openclaw" / "openclaw.json"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"read_error: {exc}"
    if r.returncode != 0:
        return None, f"sudo_cat_rc={r.returncode}: {r.stderr.strip()}"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


def _safe_write_bot_oc_config(bot_id: str, new_config: dict, reason: str) -> tuple[bool, str]:
    try:
        from evolve_admin.deploy import safe_write_bot_config
    except ImportError as exc:
        return False, f"deploy.safe_write_bot_config not available: {exc}"
    return safe_write_bot_config(bot_id, new_config, reason=reason)


def _restart_bot_gateway(bot_id: str) -> tuple[bool, str]:
    try:
        from evolve_admin.deploy import restart_gateway
    except ImportError as exc:
        return False, f"deploy.restart_gateway not available: {exc}"
    try:
        restart_gateway(bot_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"restart failed: {type(exc).__name__}: {exc}"
    return True, ""


# ── Baseline helpers ──────────────────────────────────────────────────────────

def _load_baseline_for_check(shared_dir: Path):
    """Load the plugin baseline for safety checks in appliers.

    Imports lazily so the analyzer's arbiter package doesn't permanently
    depend on the plugins package (matters for hermetic tests).
    """
    try:
        from plugins import baseline as _bl
        return _bl.load(shared_dir)
    except ImportError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# EnablePluginEntry
# ─────────────────────────────────────────────────────────────────────────────

class EnablePluginEntryApplier:
    """Set ``plugins.entries[plugin_name].enabled = true``."""

    def capture_snapshot(self, action: EnablePluginEntry, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_entry = None
        prior_enabled = None
        if oc is not None:
            entries = ((oc.get("plugins") or {}).get("entries") or {})
            prior_entry = entries.get(action.plugin_name)
            if isinstance(prior_entry, dict):
                prior_enabled = prior_entry.get("enabled")
        return {
            "action_kind": "EnablePluginEntry",
            "bot_id": action.bot_id,
            "plugin_name": action.plugin_name,
            "prior_entry_existed": prior_entry is not None,
            "prior_enabled": prior_enabled,
            "read_error": err,
        }

    def apply(self, action: EnablePluginEntry, bot_id: str) -> ApplyResult:
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entry = entries.setdefault(action.plugin_name, {})
        # Setting enabled=true via this applier creates the entry with just
        # {enabled: True}; existing entries keep their config / hooks / subagent
        # blocks intact (we only flip the flag).
        entry["enabled"] = True

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"EnablePluginEntry {action.plugin_name} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "plugin_name": action.plugin_name,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Enabled plugin {action.plugin_name!r} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        name = snapshot.get("plugin_name") or ""
        existed = bool(snapshot.get("prior_entry_existed"))
        prior_enabled = snapshot.get("prior_enabled")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        entries = (oc.get("plugins") or {}).get("entries") or {}
        if not existed:
            entries.pop(name, None)
        else:
            entry = entries.setdefault(name, {})
            if prior_enabled is None:
                entry.pop("enabled", None)
            else:
                entry["enabled"] = prior_enabled

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert EnablePluginEntry {name} on {bot}"
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot, "plugin_name": name},
            message=f"Reverted EnablePluginEntry {name!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# DisablePluginEntry
# ─────────────────────────────────────────────────────────────────────────────

class DisablePluginEntryApplier:
    """Set ``plugins.entries[plugin_name].enabled = false``.

    Refuses if the plugin is in the baseline's required_plugins. The bot
    would fire plugin_missing_required forever; that's the wrong shape
    of change.
    """

    def capture_snapshot(self, action: DisablePluginEntry, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_entry = None
        prior_enabled = None
        if oc is not None:
            entries = ((oc.get("plugins") or {}).get("entries") or {})
            prior_entry = entries.get(action.plugin_name)
            if isinstance(prior_entry, dict):
                prior_enabled = prior_entry.get("enabled")
        return {
            "action_kind": "DisablePluginEntry",
            "bot_id": action.bot_id,
            "plugin_name": action.plugin_name,
            "prior_entry_existed": prior_entry is not None,
            "prior_enabled": prior_enabled,
            "read_error": err,
        }

    def apply(self, action: DisablePluginEntry, bot_id: str) -> ApplyResult:
        # Safety: refuse to disable a required plugin
        shared = _shared_dir()
        bl = _load_baseline_for_check(shared)
        if bl is not None:
            from plugins.baseline import resolve_for
            resolved = resolve_for(bl, action.bot_id)
            if action.plugin_name in resolved.required:
                return ApplyResult(
                    ok=False,
                    details={"plugin_name": action.plugin_name, "required": True},
                    message=(
                        f"Refusing to disable {action.plugin_name!r} on {action.bot_id}: "
                        "it's in the baseline's required_plugins. Remove from the baseline "
                        "first via UpdatePluginBaseline."
                    ),
                )

        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entry = entries.setdefault(action.plugin_name, {})
        entry["enabled"] = False

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"DisablePluginEntry {action.plugin_name} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "plugin_name": action.plugin_name,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Disabled plugin {action.plugin_name!r} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        # Same revert shape as EnablePluginEntry — restore prior enabled flag
        bot = snapshot.get("bot_id") or bot_id
        name = snapshot.get("plugin_name") or ""
        existed = bool(snapshot.get("prior_entry_existed"))
        prior_enabled = snapshot.get("prior_enabled")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        entries = (oc.get("plugins") or {}).get("entries") or {}
        if not existed:
            entries.pop(name, None)
        else:
            entry = entries.setdefault(name, {})
            if prior_enabled is None:
                entry.pop("enabled", None)
            else:
                entry["enabled"] = prior_enabled

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert DisablePluginEntry {name} on {bot}"
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot, "plugin_name": name},
            message=f"Reverted DisablePluginEntry {name!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePluginAllowDeny
# ─────────────────────────────────────────────────────────────────────────────

class UpdatePluginAllowDenyApplier:
    """Replace ``plugins.allow`` and/or ``plugins.deny`` on a bot.

    Safety checks (post-baseline-load):
      - Proposed allow list must include all baseline-required plugins for
        the bot (otherwise the bot loses access to a required plugin).
      - Proposed allow list must not include any baseline-denied plugin.
      - Proposed deny list must not include a baseline-required plugin.
    """

    def capture_snapshot(self, action: UpdatePluginAllowDeny, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_allow = None
        prior_deny = None
        if oc is not None:
            plugins = oc.get("plugins") or {}
            prior_allow = plugins.get("allow")
            prior_deny = plugins.get("deny")
        return {
            "action_kind": "UpdatePluginAllowDeny",
            "bot_id": action.bot_id,
            "prior_allow": prior_allow,
            "prior_deny": prior_deny,
            "proposed_allow": action.allow,
            "proposed_deny": action.deny,
            "read_error": err,
        }

    def apply(self, action: UpdatePluginAllowDeny, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        bl = _load_baseline_for_check(shared)
        if bl is not None:
            from plugins.baseline import resolve_for
            resolved = resolve_for(bl, action.bot_id)
            if action.allow is not None:
                proposed = set(action.allow)
                missing_req = resolved.required - proposed
                if missing_req:
                    return ApplyResult(
                        ok=False,
                        details={"missing_required": sorted(missing_req)},
                        message=(
                            f"Refusing: proposed allow list for {action.bot_id} would exclude "
                            f"required plugin(s): {sorted(missing_req)}."
                        ),
                    )
                denied_present = proposed & resolved.denied
                if denied_present:
                    return ApplyResult(
                        ok=False,
                        details={"denied_in_allow": sorted(denied_present)},
                        message=(
                            f"Refusing: proposed allow list includes baseline-denied "
                            f"plugin(s): {sorted(denied_present)}."
                        ),
                    )
            if action.deny is not None:
                proposed_deny = set(action.deny)
                req_in_deny = resolved.required & proposed_deny
                if req_in_deny:
                    return ApplyResult(
                        ok=False,
                        details={"required_in_deny": sorted(req_in_deny)},
                        message=(
                            f"Refusing: proposed deny list includes required plugin(s): "
                            f"{sorted(req_in_deny)}."
                        ),
                    )

        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        if action.allow is not None:
            plugins["allow"] = list(action.allow)
        if action.deny is not None:
            plugins["deny"] = list(action.deny)

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"UpdatePluginAllowDeny on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "allow_set": action.allow is not None,
                "deny_set": action.deny is not None,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Updated plugins.allow / plugins.deny on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        prior_allow = snapshot.get("prior_allow")
        prior_deny = snapshot.get("prior_deny")
        proposed_allow = snapshot.get("proposed_allow")
        proposed_deny = snapshot.get("proposed_deny")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        # Only touch fields we modified in apply
        if proposed_allow is not None:
            if prior_allow is None:
                plugins.pop("allow", None)
            else:
                plugins["allow"] = list(prior_allow)
        if proposed_deny is not None:
            if prior_deny is None:
                plugins.pop("deny", None)
            else:
                plugins["deny"] = list(prior_deny)

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert UpdatePluginAllowDeny on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot},
            message=f"Reverted plugins.allow / plugins.deny on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePluginBaseline
# ─────────────────────────────────────────────────────────────────────────────

class UpdatePluginBaselineApplier:
    """Mutate {shared_dir}/policy/plugin-baseline.json.

    v2 (internal/spec-plugin-posture-rework-2026-06-06.md) only supports the
    ``set_pod_default`` operation. ``fields`` is a dict of top-level
    baseline field names → new values (``required_plugins``,
    ``denied_plugins``, ``expected_load_paths``). Unspecified fields are
    preserved.

    ``set_bot_override`` is rejected — v2 has no per-bot baseline
    overrides. Operator intent for per-bot plugin enable/disable goes
    through ``EnablePluginEntry`` / ``DisablePluginEntry`` against the
    bot's openclaw.json instead.
    """

    _BASELINE_FIELDS = {
        "required_plugins",
        "denied_plugins",
        "expected_load_paths",
    }

    def capture_snapshot(self, action: UpdatePluginBaseline, bot_id: str) -> dict:
        shared = _shared_dir()
        bl = _load_baseline_for_check(shared)
        prior_serialized = bl.to_dict() if bl is not None else None
        return {
            "action_kind": "UpdatePluginBaseline",
            "operation": action.operation,
            "bot_id": action.bot_id,
            "fields": dict(action.fields),
            "shared_dir": str(shared),
            "prior_baseline": prior_serialized,
        }

    def apply(self, action: UpdatePluginBaseline, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        try:
            from plugins import baseline as _bl
        except ImportError as exc:
            return ApplyResult(
                ok=False, details={"error": str(exc)},
                message=f"plugins.baseline not importable: {exc}",
            )

        if action.operation == "set_bot_override":
            return ApplyResult(
                ok=False,
                details={"operation": action.operation},
                message=(
                    "set_bot_override is no longer supported. The v2 plugin "
                    "baseline has no per-bot overrides; use EnablePluginEntry "
                    "or DisablePluginEntry to change a single bot's plugin "
                    "posture."
                ),
            )
        if action.operation != "set_pod_default":
            return ApplyResult(
                ok=False,
                details={"unknown_operation": action.operation},
                message=f"Unknown UpdatePluginBaseline operation: {action.operation!r}",
            )

        bl = _bl.load(shared)

        # Refuse removing a denylist entry without an explicit force flag.
        # Additions only on denied_plugins.
        new_denied = action.fields.get("denied_plugins")
        if new_denied is not None:
            prior = set(bl.denied_plugins)
            proposed = set(new_denied)
            if prior - proposed:
                return ApplyResult(
                    ok=False,
                    details={"removed_denied": sorted(prior - proposed)},
                    message=(
                        "Refusing to remove plugins from denied_plugins via "
                        "UpdatePluginBaseline. Removal requires a separate "
                        "operator-confirmed flow."
                    ),
                )
        for k, v in action.fields.items():
            if k in self._BASELINE_FIELDS:
                setattr(bl, k, list(v) if isinstance(v, (list, tuple)) else v)

        _bl.write(bl, shared)
        return ApplyResult(
            ok=True,
            details={"operation": action.operation},
            message=f"Updated plugin baseline ({action.operation}).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from evolve_config import CANONICAL_SHARED_DIR

        shared = Path(snapshot.get("shared_dir") or CANONICAL_SHARED_DIR)
        prior = snapshot.get("prior_baseline")
        if prior is None:
            return RevertResult(
                ok=False,
                message="No prior baseline captured in snapshot; cannot revert.",
            )
        try:
            from plugins.baseline import (
                PluginBaseline,
                default_expected_load_paths,
                write as _write_baseline,
            )
        except ImportError as exc:
            return RevertResult(ok=False, message=f"plugins.baseline not importable: {exc}")
        # Accept either v1 (pod_default block) or v2 (top-level fields)
        # snapshot shapes so reverts taken before the rework still work.
        # trusted_install_sources, if present in the snapshot, is
        # ignored — the field was retired with plugin_unverified_source.
        pd_raw = prior.get("pod_default") or {}
        restored = PluginBaseline(
            version=int(prior.get("version") or 2),
            required_plugins=list(
                prior.get("required_plugins")
                or pd_raw.get("required_plugins")
                or []
            ),
            denied_plugins=list(
                prior.get("denied_plugins")
                or pd_raw.get("denied_plugins")
                or []
            ),
            expected_load_paths=list(
                prior.get("expected_load_paths")
                or pd_raw.get("expected_load_paths")
                or default_expected_load_paths()
            ),
            bootstrapped_at=str(prior.get("bootstrapped_at") or ""),
        )
        _write_baseline(restored, shared)
        return RevertResult(
            ok=True,
            details={"operation": snapshot.get("operation")},
            message="Reverted plugin baseline to prior snapshot.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePluginConfig (Phase C)
# ─────────────────────────────────────────────────────────────────────────────

def _set_dotted(d: dict, dotted_key: str, value: Any) -> None:
    """Set value at a dotted-key path, creating dicts as needed."""
    parts = dotted_key.split(".")
    cur = d
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = value


def _unset_dotted(d: dict, dotted_key: str) -> None:
    """Remove key at a dotted-key path. Leaves empty parents alone."""
    parts = dotted_key.split(".")
    cur = d
    for k in parts[:-1]:
        if not isinstance(cur.get(k), dict):
            return
        cur = cur[k]
    cur.pop(parts[-1], None)


class UpdatePluginConfigApplier:
    """Mutate the ``config`` block of a plugin entry on a bot."""

    def capture_snapshot(self, action: UpdatePluginConfig, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_config = None
        if oc is not None:
            entries = ((oc.get("plugins") or {}).get("entries") or {})
            entry = entries.get(action.plugin_name) or {}
            prior_config = entry.get("config") if isinstance(entry, dict) else None
        return {
            "action_kind": "UpdatePluginConfig",
            "bot_id": action.bot_id,
            "plugin_name": action.plugin_name,
            "operation": action.operation,
            "fields": dict(action.fields),
            "prior_config": deepcopy(prior_config) if prior_config is not None else None,
            "read_error": err,
        }

    def apply(self, action: UpdatePluginConfig, bot_id: str) -> ApplyResult:
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entry = entries.setdefault(action.plugin_name, {})
        cfg = entry.setdefault("config", {})

        if action.operation == "set_keys":
            for k, v in action.fields.items():
                _set_dotted(cfg, k, v)
        elif action.operation == "unset_keys":
            for k in action.fields.keys():
                _unset_dotted(cfg, k)
        elif action.operation == "replace_block":
            entry["config"] = dict(action.fields)
        else:
            return ApplyResult(
                ok=False,
                details={"unknown_operation": action.operation},
                message=f"Unknown UpdatePluginConfig operation: {action.operation!r}",
            )

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"UpdatePluginConfig {action.operation} on {action.bot_id}:{action.plugin_name}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "plugin_name": action.plugin_name,
                "operation": action.operation,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Updated config for {action.plugin_name!r} on {action.bot_id} "
                f"({action.operation}). "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        name = snapshot.get("plugin_name") or ""
        prior_config = snapshot.get("prior_config")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        entries = (oc.get("plugins") or {}).get("entries") or {}
        entry = entries.get(name) or {}
        if isinstance(entry, dict):
            if prior_config is None:
                entry.pop("config", None)
            else:
                entry["config"] = prior_config

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert UpdatePluginConfig {name} on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot, "plugin_name": name},
            message=f"Reverted UpdatePluginConfig for {name!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePluginLoadPaths (Phase C)
# ─────────────────────────────────────────────────────────────────────────────

class UpdatePluginLoadPathsApplier:
    """Add or remove an entry in a bot's ``plugins.load.paths`` list.

    Safety: additions must be in _LOAD_PATH_WHITELIST. Expanding the
    whitelist requires UpdatePluginBaseline.set_pod_default to add the
    new directory to expected_load_paths first.

    Removals are accepted unconditionally — including paths from
    expected_load_paths, which will then trip plugin_load_path_drift
    on the next monitor cycle (the right shape; operator can see
    they've created a divergence).
    """

    def capture_snapshot(self, action: UpdatePluginLoadPaths, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_paths = None
        if oc is not None:
            prior_paths = ((oc.get("plugins") or {}).get("load") or {}).get("paths")
        return {
            "action_kind": "UpdatePluginLoadPaths",
            "bot_id": action.bot_id,
            "operation": action.operation,
            "path": action.path,
            "prior_paths": list(prior_paths) if isinstance(prior_paths, list) else None,
            "read_error": err,
        }

    def apply(self, action: UpdatePluginLoadPaths, bot_id: str) -> ApplyResult:
        if not action.path:
            return ApplyResult(
                ok=False, details={"error": "empty path"},
                message="UpdatePluginLoadPaths requires a non-empty path",
            )

        if action.operation == "add_path":
            if action.path not in _LOAD_PATH_WHITELIST:
                return ApplyResult(
                    ok=False,
                    details={"path": action.path, "whitelist": sorted(_LOAD_PATH_WHITELIST)},
                    message=(
                        f"Refusing to add {action.path!r} to plugins.load.paths: "
                        "not in the trusted-directory whitelist. Expand the pod "
                        "baseline's expected_load_paths via UpdatePluginBaseline first."
                    ),
                )
        elif action.operation != "remove_path":
            return ApplyResult(
                ok=False,
                details={"unknown_operation": action.operation},
                message=f"Unknown UpdatePluginLoadPaths operation: {action.operation!r}",
            )

        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        load = plugins.setdefault("load", {})
        paths = load.setdefault("paths", [])
        if not isinstance(paths, list):
            return ApplyResult(
                ok=False, details={"error": "plugins.load.paths is not a list"},
                message="plugins.load.paths has unexpected shape",
            )

        if action.operation == "add_path":
            if action.path in paths:
                return ApplyResult(
                    ok=True, details={"no_op": True},
                    message=f"Path {action.path!r} already present in load.paths.",
                )
            paths.append(action.path)
        else:  # remove_path
            if action.path not in paths:
                return ApplyResult(
                    ok=True, details={"no_op": True},
                    message=f"Path {action.path!r} not in load.paths; nothing to remove.",
                )
            paths.remove(action.path)

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"UpdatePluginLoadPaths {action.operation} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "operation": action.operation,
                "path": action.path,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"{action.operation} {action.path!r} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        prior_paths = snapshot.get("prior_paths")
        if prior_paths is None:
            return RevertResult(
                ok=False, message="No prior plugins.load.paths captured; cannot revert.",
            )
        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        plugins = oc.setdefault("plugins", {})
        load = plugins.setdefault("load", {})
        load["paths"] = list(prior_paths)

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert UpdatePluginLoadPaths on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot},
            message=f"Reverted plugins.load.paths on {bot} to prior list.",
        )


# ── Registration ──────────────────────────────────────────────────────────────

register_applier("EnablePluginEntry", EnablePluginEntryApplier())
register_applier("DisablePluginEntry", DisablePluginEntryApplier())
register_applier("UpdatePluginAllowDeny", UpdatePluginAllowDenyApplier())
register_applier("UpdatePluginBaseline", UpdatePluginBaselineApplier())
register_applier("UpdatePluginConfig", UpdatePluginConfigApplier())
register_applier("UpdatePluginLoadPaths", UpdatePluginLoadPathsApplier())
