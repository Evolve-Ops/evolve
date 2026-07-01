"""arbiter.appliers.hook — Enable/Disable webhook ingress, edit hook policy,
mutate hook baseline.

Spec: docs/spec-hook-governance-2026-05-10.md §3.4.

Five appliers in one module — they share the openclaw.json + baseline
read/write helpers. Each registers itself with the applier dispatch
on import.

Two surfaces:
  - openclaw.json hooks{}: EnableWebhookIngress / DisableWebhookIngress
    / UpdateWebhookMapping / UpdatePluginHookPolicy. All trigger
    safe_write_bot_config + auto-restart_gateway (mirrors MCP Phase E).
  - {shared_dir}/policy/hook-baseline.json: UpdateHookBaseline. No
    bot-side write, no restart needed.

Auto-reject rules (spec §5.4):
  - EnableWebhookIngress with empty token or empty allowed_agent_ids
    is rejected (unbounded blast radius).
  - UpdatePluginHookPolicy setting allow_prompt_injection=true on a
    plugin not in baseline's trusted_prompt_mutators is rejected.
  - UpdateHookBaseline.set_plugin_policy with allow_prompt_injection=
    true is rejected unless the plugin is already in trusted_prompt_
    mutators (closes the "add yourself, then enable" loop).
"""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import (
    EnableWebhookIngress,
    DisableWebhookIngress,
    UpdateWebhookMapping,
    UpdatePluginHookPolicy,
    UpdateHookBaseline,
)


# ── Shared dir resolution ─────────────────────────────────────────────────────

def _shared_dir() -> Path:
    import os
    candidates = [os.environ.get("EVOLVE_SHARED_DIR"), str(CANONICAL_SHARED_DIR)]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return CANONICAL_SHARED_DIR


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

def _load_baseline(shared_dir: Path):
    """Load the hook baseline. Lazy import for hermetic tests."""
    try:
        from hooks import baseline as _bl
        return _bl.load(shared_dir)
    except ImportError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# EnableWebhookIngress
# ─────────────────────────────────────────────────────────────────────────────

class EnableWebhookIngressApplier:
    """Configure + enable a bot's openclaw.json hooks block."""

    def capture_snapshot(self, action: EnableWebhookIngress, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_block = oc.get("hooks") if oc else None
        return {
            "action_kind": "EnableWebhookIngress",
            "bot_id": action.bot_id,
            "prior_hooks_block": deepcopy(prior_block) if isinstance(prior_block, dict) else None,
            "prior_block_existed": isinstance(prior_block, dict),
            "read_error": err,
        }

    def apply(self, action: EnableWebhookIngress, bot_id: str) -> ApplyResult:
        # Auto-reject: empty token or no allowed agents
        if not action.token.strip():
            return ApplyResult(
                ok=False, details={"error": "empty_token"},
                message="Refusing: EnableWebhookIngress requires a non-empty token.",
            )
        if not action.allowed_agent_ids:
            return ApplyResult(
                ok=False, details={"error": "empty_allowed_agent_ids"},
                message=(
                    "Refusing: EnableWebhookIngress requires at least one "
                    "allowed_agent_id to bound blast radius."
                ),
            )

        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        oc["hooks"] = {
            "enabled": True,
            "token": action.token,
            "path": action.path or "/hooks",
            "allowedAgentIds": list(action.allowed_agent_ids),
            "allowedSessionKeyPrefixes": list(action.allowed_session_key_prefixes),
            "mappings": list(action.mappings),
            "maxBodyBytes": int(action.max_body_bytes),
        }
        if action.transforms_dir:
            oc["hooks"]["transformsDir"] = action.transforms_dir

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"EnableWebhookIngress on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "allowed_agent_ids": list(action.allowed_agent_ids),
                "mapping_count": len(action.mappings),
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Enabled webhook ingress on {action.bot_id} "
                f"({len(action.mappings)} mapping(s), {len(action.allowed_agent_ids)} allowed agent(s)). "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        prior_existed = bool(snapshot.get("prior_block_existed"))
        prior_block = snapshot.get("prior_hooks_block")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        if prior_existed and isinstance(prior_block, dict):
            oc["hooks"] = prior_block
        else:
            oc.pop("hooks", None)

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert EnableWebhookIngress on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True,
            details={"bot_id": bot, "restored_prior": prior_existed},
            message=f"Reverted EnableWebhookIngress on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# DisableWebhookIngress
# ─────────────────────────────────────────────────────────────────────────────

class DisableWebhookIngressApplier:
    """Set hooks.enabled=false, preserving the rest of the block."""

    def capture_snapshot(self, action: DisableWebhookIngress, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_enabled = None
        if oc:
            hooks = oc.get("hooks") or {}
            prior_enabled = hooks.get("enabled") if isinstance(hooks, dict) else None
        return {
            "action_kind": "DisableWebhookIngress",
            "bot_id": action.bot_id,
            "prior_enabled": prior_enabled,
            "read_error": err,
        }

    def apply(self, action: DisableWebhookIngress, bot_id: str) -> ApplyResult:
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        hooks = oc.get("hooks")
        if not isinstance(hooks, dict):
            return ApplyResult(
                ok=True, details={"no_op": True},
                message=f"No webhook ingress block on {action.bot_id}; nothing to disable.",
            )
        oc = deepcopy(oc)
        oc["hooks"]["enabled"] = False

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"DisableWebhookIngress on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Disabled webhook ingress on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        prior_enabled = snapshot.get("prior_enabled")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        hooks = oc.get("hooks")
        if isinstance(hooks, dict):
            if prior_enabled is None:
                hooks.pop("enabled", None)
            else:
                hooks["enabled"] = prior_enabled

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert DisableWebhookIngress on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": bot},
            message=f"Reverted DisableWebhookIngress on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdateWebhookMapping
# ─────────────────────────────────────────────────────────────────────────────

class UpdateWebhookMappingApplier:
    """Add / remove / replace a single mapping in hooks.mappings[]."""

    def capture_snapshot(self, action: UpdateWebhookMapping, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_mappings = None
        if oc:
            hooks = oc.get("hooks") or {}
            prior_mappings = hooks.get("mappings")
        return {
            "action_kind": "UpdateWebhookMapping",
            "bot_id": action.bot_id,
            "operation": action.operation,
            "mapping_id": action.mapping_id,
            "mapping": dict(action.mapping),
            "prior_mappings": list(prior_mappings) if isinstance(prior_mappings, list) else None,
            "read_error": err,
        }

    def apply(self, action: UpdateWebhookMapping, bot_id: str) -> ApplyResult:
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        hooks = oc.get("hooks")
        if not isinstance(hooks, dict):
            return ApplyResult(
                ok=False, details={"error": "ingress_not_configured"},
                message=f"No hooks block on {action.bot_id}; enable webhook ingress first.",
            )
        oc = deepcopy(oc)
        mappings = list((oc["hooks"]).get("mappings") or [])

        if action.operation == "add":
            mappings.append(action.mapping)
        elif action.operation == "remove":
            if not action.mapping_id:
                return ApplyResult(
                    ok=False, details={"error": "missing_mapping_id"},
                    message="UpdateWebhookMapping remove requires mapping_id",
                )
            mappings = [m for m in mappings if m.get("id") != action.mapping_id]
        elif action.operation == "replace":
            if not action.mapping_id:
                return ApplyResult(
                    ok=False, details={"error": "missing_mapping_id"},
                    message="UpdateWebhookMapping replace requires mapping_id",
                )
            mappings = [
                action.mapping if m.get("id") == action.mapping_id else m
                for m in mappings
            ]
        else:
            return ApplyResult(
                ok=False, details={"error": f"unknown_operation: {action.operation!r}"},
                message=f"Unknown UpdateWebhookMapping operation: {action.operation!r}",
            )

        oc["hooks"]["mappings"] = mappings
        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"UpdateWebhookMapping {action.operation} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "operation": action.operation,
                "mapping_count": len(mappings),
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"UpdateWebhookMapping {action.operation} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        prior_mappings = snapshot.get("prior_mappings")
        if prior_mappings is None:
            return RevertResult(ok=False, message="No prior mappings captured; cannot revert.")
        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        hooks = oc.setdefault("hooks", {})
        hooks["mappings"] = list(prior_mappings)
        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert UpdateWebhookMapping on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": bot},
            message=f"Reverted UpdateWebhookMapping on {bot} (restored {len(prior_mappings)} prior mapping(s)).",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePluginHookPolicy
# ─────────────────────────────────────────────────────────────────────────────

class UpdatePluginHookPolicyApplier:
    """Set allowConversationAccess / allowPromptInjection on one plugin entry."""

    def capture_snapshot(self, action: UpdatePluginHookPolicy, bot_id: str) -> dict:
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_policy = None
        if oc:
            entries = ((oc.get("plugins") or {}).get("entries") or {})
            entry = entries.get(action.plugin_name) or {}
            if isinstance(entry, dict):
                prior_policy = entry.get("hooks")
        return {
            "action_kind": "UpdatePluginHookPolicy",
            "bot_id": action.bot_id,
            "plugin_name": action.plugin_name,
            "prior_policy": deepcopy(prior_policy) if isinstance(prior_policy, dict) else None,
            "proposed_allow_conv": action.allow_conversation_access,
            "proposed_allow_inj": action.allow_prompt_injection,
            "read_error": err,
        }

    def apply(self, action: UpdatePluginHookPolicy, bot_id: str) -> ApplyResult:
        # Safety: allow_prompt_injection=true requires plugin in trusted_prompt_mutators
        shared = _shared_dir()
        bl = _load_baseline(shared)
        if bl is not None and action.allow_prompt_injection is True:
            trusted = set(bl.pod_default.trusted_prompt_mutators or [])
            if action.plugin_name not in trusted:
                return ApplyResult(
                    ok=False,
                    details={"plugin_name": action.plugin_name, "trusted": sorted(trusted)},
                    message=(
                        f"Refusing: cannot enable allowPromptInjection on {action.plugin_name!r} — "
                        "plugin not in trusted_prompt_mutators. Add via "
                        "UpdateHookBaseline.set_trusted_mutators first."
                    ),
                )

        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        entries = (oc.setdefault("plugins", {})).setdefault("entries", {})
        entry = entries.setdefault(action.plugin_name, {})
        hooks_block = entry.setdefault("hooks", {})

        if action.allow_conversation_access is not None:
            hooks_block["allowConversationAccess"] = bool(action.allow_conversation_access)
        if action.allow_prompt_injection is not None:
            hooks_block["allowPromptInjection"] = bool(action.allow_prompt_injection)

        # If the hooks block is now empty (both flags cleared), drop it
        if not hooks_block:
            entry.pop("hooks", None)

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"UpdatePluginHookPolicy {action.plugin_name} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "plugin_name": action.plugin_name,
                "allow_conversation_access": action.allow_conversation_access,
                "allow_prompt_injection": action.allow_prompt_injection,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Updated hook policy for {action.plugin_name!r} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        name = snapshot.get("plugin_name") or ""
        prior_policy = snapshot.get("prior_policy")

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        entries = (oc.get("plugins") or {}).get("entries") or {}
        entry = entries.get(name)
        if isinstance(entry, dict):
            if prior_policy is None:
                entry.pop("hooks", None)
            else:
                entry["hooks"] = prior_policy

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert UpdatePluginHookPolicy {name} on {bot}",
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": bot, "plugin_name": name},
            message=f"Reverted UpdatePluginHookPolicy for {name!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdateHookBaseline
# ─────────────────────────────────────────────────────────────────────────────

class UpdateHookBaselineApplier:
    """Mutate {shared_dir}/policy/hook-baseline.json."""

    def capture_snapshot(self, action: UpdateHookBaseline, bot_id: str) -> dict:
        shared = _shared_dir()
        bl = _load_baseline(shared)
        return {
            "action_kind": "UpdateHookBaseline",
            "operation": action.operation,
            "fields": dict(action.fields),
            "shared_dir": str(shared),
            "prior_baseline": bl.to_dict() if bl is not None else None,
        }

    def apply(self, action: UpdateHookBaseline, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        try:
            from hooks import baseline as _bl
        except ImportError as exc:
            return ApplyResult(
                ok=False, details={"error": str(exc)},
                message=f"hooks.baseline not importable: {exc}",
            )
        bl = _bl.load(shared)

        if action.operation == "set_webhook_ingress":
            enabled = bool(action.fields.get("enabled", False))
            rationale = str(action.fields.get("rationale") or "")
            bl.pod_default.webhook_ingress = _bl.WebhookIngressExpectation(
                enabled=enabled, rationale=rationale,
            )

        elif action.operation == "set_plugin_policy":
            plugin_name = (action.fields.get("plugin_name") or "").strip()
            if not plugin_name:
                return ApplyResult(
                    ok=False, details={"error": "missing_plugin_name"},
                    message="set_plugin_policy requires fields.plugin_name",
                )
            allow_conv = bool(action.fields.get("allow_conversation_access", False))
            allow_inj = bool(action.fields.get("allow_prompt_injection", False))
            # Auto-reject: allow_prompt_injection=true requires plugin already in
            # trusted_prompt_mutators (close the add-yourself-and-flip loop).
            if allow_inj and plugin_name not in set(bl.pod_default.trusted_prompt_mutators):
                return ApplyResult(
                    ok=False,
                    details={"plugin_name": plugin_name},
                    message=(
                        f"Refusing set_plugin_policy: cannot expect "
                        f"allow_prompt_injection=true for {plugin_name!r} — "
                        "plugin not in trusted_prompt_mutators. Add it via a "
                        "separate UpdateHookBaseline.set_trusted_mutators first."
                    ),
                )
            rationale = str(action.fields.get("rationale") or "")
            # Replace any existing expectation with the new one
            existing = [
                p for p in bl.pod_default.plugin_typed_hooks if p.plugin_name != plugin_name
            ]
            existing.append(_bl.PluginPolicyExpectation(
                plugin_name=plugin_name,
                allow_conversation_access=allow_conv,
                allow_prompt_injection=allow_inj,
                rationale=rationale,
            ))
            bl.pod_default.plugin_typed_hooks = existing

        elif action.operation == "set_trusted_mutators":
            plugins = list(action.fields.get("plugins") or [])
            if not isinstance(plugins, list):
                return ApplyResult(
                    ok=False, details={"error": "plugins must be a list"},
                    message="set_trusted_mutators.fields.plugins must be a list",
                )
            bl.pod_default.trusted_prompt_mutators = [str(p) for p in plugins]

        else:
            return ApplyResult(
                ok=False, details={"unknown_operation": action.operation},
                message=f"Unknown UpdateHookBaseline operation: {action.operation!r}",
            )

        _bl.write(bl, shared)
        return ApplyResult(
            ok=True,
            details={"operation": action.operation},
            message=f"Updated hook baseline ({action.operation}).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        shared = Path(snapshot.get("shared_dir") or "/Users/Shared/evolve")
        prior = snapshot.get("prior_baseline")
        if prior is None:
            return RevertResult(ok=False, message="No prior baseline captured; cannot revert.")
        try:
            from hooks.baseline import (
                HookBaseline, PodDefault, WebhookIngressExpectation,
                PluginPolicyExpectation, write as _write,
            )
        except ImportError as exc:
            return RevertResult(ok=False, message=f"hooks.baseline not importable: {exc}")

        pd_raw = prior.get("pod_default") or {}
        web = pd_raw.get("webhook_ingress") or {}
        plugin_raws = pd_raw.get("plugin_typed_hooks") or []
        restored = HookBaseline(
            version=int(prior.get("version") or 1),
            bootstrapped_at=str(prior.get("bootstrapped_at") or ""),
            pod_default=PodDefault(
                webhook_ingress=WebhookIngressExpectation(
                    enabled=bool(web.get("enabled", False)),
                    rationale=str(web.get("rationale") or ""),
                ),
                plugin_typed_hooks=[
                    PluginPolicyExpectation(
                        plugin_name=str(p.get("plugin_name") or ""),
                        allow_conversation_access=bool(p.get("allow_conversation_access", False)),
                        allow_prompt_injection=bool(p.get("allow_prompt_injection", False)),
                        rationale=str(p.get("rationale") or ""),
                    )
                    for p in plugin_raws if isinstance(p, dict)
                ],
                trusted_prompt_mutators=list(pd_raw.get("trusted_prompt_mutators") or []),
            ),
            per_bot_overrides=dict(prior.get("per_bot_overrides") or {}),
        )
        _write(restored, shared)
        return RevertResult(
            ok=True, details={"operation": snapshot.get("operation")},
            message="Reverted hook baseline to prior snapshot.",
        )


# ── Registration ──────────────────────────────────────────────────────────────

register_applier("EnableWebhookIngress", EnableWebhookIngressApplier())
register_applier("DisableWebhookIngress", DisableWebhookIngressApplier())
register_applier("UpdateWebhookMapping", UpdateWebhookMappingApplier())
register_applier("UpdatePluginHookPolicy", UpdatePluginHookPolicyApplier())
register_applier("UpdateHookBaseline", UpdateHookBaselineApplier())
