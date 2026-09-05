"""hooks.monitor — diff observed inventory vs. baseline; emit Signals.

Spec: internal/spec-hook-governance-2026-05-10.md §3.3 (Signals) +
§5.2 (drift detection).

Signal types (severity in parens):
  - hook_webhook_unexpected_enabled (alert)   — ingress enabled but baseline says off
  - hook_webhook_mapping_changed (alert)      — ingress configured but signature drifts
  - hook_plugin_policy_silent_disable (alert) — required allow_conversation_access flipped
                                                false on an enabled plugin (the April
                                                incident class — this is the load-bearing
                                                signal)
  - hook_plugin_policy_unexpected (warn)      — plugin policy differs from baseline in
                                                either direction (catches both pre-position
                                                and silent revert)
  - hook_command_gate_enabled (warn)          — commands.plugins or commands.mcp = true
  - hook_openclaw_config_missing (info)       — bot has no openclaw.json

Producer name "hook_monitor" — distinct from "audit" / "mcp_monitor" /
"plugin_monitor" so sweep_resolve doesn't cross-affect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import inventory as _inv
from . import baseline as _bl
from . import bootstrap as _bootstrap

try:
    from signals import store as _signals_store
    from schema.signal import make_signature as _make_signature
except ImportError:  # pragma: no cover
    _signals_store = None  # type: ignore[assignment]
    _make_signature = None  # type: ignore[assignment]


PRODUCER = "hook_monitor"

_OWNED_TYPES = {
    "hook_webhook_unexpected_enabled",
    "hook_webhook_mapping_changed",
    "hook_webhook_transforms_dir_drift",
    "hook_plugin_policy_silent_disable",
    "hook_plugin_policy_unexpected",
    "hook_command_gate_enabled",
    "hook_openclaw_config_missing",
}


# ── transformsDir audit (Phase B) ─────────────────────────────────────────────
#
# When a bot has webhook ingress configured with a transformsDir, the monitor
# hashes the directory's contents and compares against a known-good baseline
# under {shared_dir}/hooks/transforms_baseline/<bot_id>/<server-id>/manifest.json.
# A drift signal fires when the hash set differs.
#
# Dormant on today's pod (no bot has ingress configured), but the audit code
# is in place so the moment an operator enables ingress with a transformsDir,
# the integrity check is active.


def _hash_directory_contents(dir_path: "Path") -> dict[str, str]:
    """Return {relative_path: sha256_hex} for every regular file under dir_path.

    Pure stdlib — sha256 of the bytes for each file. Symlinks aren't followed
    to avoid escaping the directory boundary.
    """
    import hashlib
    out: dict[str, str] = {}
    if not dir_path.exists() or not dir_path.is_dir():
        return out
    try:
        for entry in sorted(dir_path.rglob("*")):
            if not entry.is_file() or entry.is_symlink():
                continue
            try:
                rel = str(entry.relative_to(dir_path))
                h = hashlib.sha256(entry.read_bytes()).hexdigest()
                out[rel] = h
            except OSError:
                continue
    except OSError:
        pass
    return out


def _transforms_baseline_dir(shared_dir: "Path", bot_id: str) -> "Path":
    return shared_dir / "hooks" / "transforms_baseline" / bot_id


def _audit_transforms_dir(
    inv: "_inv.HookInventory",
    shared_dir: "Path",
) -> "dict[str, Any] | None":
    """Compare a bot's transformsDir contents against the cached baseline.

    Returns a finding dict (transforms_dir_drift) or None if no audit
    applies. Lazy baseline-create: on first observation we record the
    current hash set as the baseline; subsequent runs compare.
    """
    if (
        inv.webhook_ingress is None
        or not inv.webhook_ingress.configured
        or not inv.webhook_ingress.enabled
        or not inv.webhook_ingress.transforms_dir
    ):
        return None
    td_path = __import__("pathlib").Path(inv.webhook_ingress.transforms_dir)
    observed = _hash_directory_contents(td_path)
    if not observed:
        return None  # empty or unreadable; no fingerprint to compare

    baseline_path = _transforms_baseline_dir(shared_dir, inv.bot_id) / "manifest.json"
    if not baseline_path.exists():
        # First-run lazy baseline write — operator should review on first
        # ingress install, but don't fire a signal on the boot cycle.
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = baseline_path.with_suffix(".json.tmp")
        import json as _json
        tmp.write_text(_json.dumps({
            "transforms_dir": str(td_path),
            "recorded_at": inv.observed_at,
            "files": observed,
        }, indent=2, sort_keys=True))
        tmp.replace(baseline_path)
        return None

    import json as _json
    try:
        baseline_raw = _json.loads(baseline_path.read_text())
    except (OSError, _json.JSONDecodeError):
        return None
    baseline_files = baseline_raw.get("files") or {}
    if observed == baseline_files:
        return None

    added = sorted(set(observed) - set(baseline_files))
    removed = sorted(set(baseline_files) - set(observed))
    changed = sorted(
        path for path in (set(observed) & set(baseline_files))
        if observed[path] != baseline_files[path]
    )
    return {
        "type": "hook_webhook_transforms_dir_drift",
        "severity": "alert",
        "signature_scope": f"{inv.bot_id}:transforms-dir",
        "title": f"{inv.bot_id}: webhook transformsDir contents drifted",
        "body": (
            f"transformsDir {inv.webhook_ingress.transforms_dir!r} on {inv.bot_id} "
            "differs from the recorded baseline. This is a supply-chain surface — "
            "any new/changed file is executable in the ingress flow. Review and either "
            "update the baseline (after vetting) or roll back."
        ),
        "details": {
            "bot_id": inv.bot_id,
            "transforms_dir": inv.webhook_ingress.transforms_dir,
            "added_files": added,
            "removed_files": removed,
            "changed_files": changed,
        },
    }


def _diff_one_bot(
    inv: _inv.HookInventory,
    resolved: _bl.ResolvedBotBaseline,
    prior_inv: dict | None,
    shared_dir: "Path | None" = None,
) -> list[dict[str, Any]]:
    """Compute findings for one bot."""
    findings: list[dict[str, Any]] = []
    bot_id = inv.bot_id

    # 1. Missing openclaw.json — info, blocks the rest
    if not inv.openclaw_config_present:
        findings.append({
            "type": "hook_openclaw_config_missing",
            "severity": "info",
            "signature_scope": bot_id,
            "title": f"{bot_id}: openclaw.json not found",
            "body": (
                f"Bot {bot_id} is in network.json but has no openclaw.json at "
                f"{inv.openclaw_config_path}. Hook inventory cannot be checked."
            ),
            "details": {
                "bot_id": bot_id,
                "openclaw_config_path": inv.openclaw_config_path,
                "read_error": inv.read_error,
            },
        })
        return findings

    # 2. Webhook ingress checks
    ingress = inv.webhook_ingress
    if ingress is not None and ingress.configured:
        if ingress.enabled and not resolved.webhook_ingress_enabled:
            findings.append({
                "type": "hook_webhook_unexpected_enabled",
                "severity": "alert",
                "signature_scope": f"{bot_id}:ingress",
                "title": f"{bot_id}: webhook ingress is enabled (baseline says off)",
                "body": (
                    f"openclaw.json on {bot_id} has hooks.enabled=true but the pod "
                    "baseline expects ingress to be disabled. Webhook ingress is an "
                    "RCE-shape surface — token leakage gives an external party the "
                    "ability to trigger agent turns. Disable via openclaw.json or "
                    "issue an UpdateHookBaseline proposal to allow it."
                ),
                "details": {
                    "bot_id": bot_id,
                    "mapping_count": ingress.mapping_count,
                    "allowed_agent_ids": ingress.allowed_agent_ids,
                    "has_token": ingress.has_token,
                    "transforms_dir": ingress.transforms_dir,
                },
            })

        # Mapping-shape drift — only meaningful when prior_inv has the prior signature
        if prior_inv:
            prior_web = prior_inv.get("webhook_ingress") or {}
            prior_sig = prior_web.get("block_signature")
            if (
                prior_sig
                and ingress.block_signature
                and prior_sig != ingress.block_signature
            ):
                findings.append({
                    "type": "hook_webhook_mapping_changed",
                    "severity": "alert",
                    "signature_scope": f"{bot_id}:ingress:mapping",
                    "title": f"{bot_id}: webhook ingress configuration changed",
                    "body": (
                        f"Webhook ingress shape on {bot_id} differs from the prior cycle "
                        "(token rotation excluded — this is a real configuration change). "
                        "Confirm the change was intentional or revert via openclaw.json."
                    ),
                    "details": {
                        "bot_id": bot_id,
                        "prior_signature": prior_sig,
                        "new_signature": ingress.block_signature,
                    },
                })

    # 2b. transformsDir content drift (Phase B). Lazy-baselines on first
    # observation; subsequent runs fire alert on hash drift.
    if shared_dir is not None:
        td_finding = _audit_transforms_dir(inv, shared_dir)
        if td_finding is not None:
            findings.append(td_finding)

    # 3. Per-plugin policy checks
    expected = resolved.expected_plugin_policies  # plugin_name → PluginPolicyExpectation
    trusted_mutators = resolved.trusted_prompt_mutators
    for pol in inv.plugin_policies:
        exp = expected.get(pol.plugin_name)
        if exp is None:
            # No baseline expectation: any non-default policy is a soft warn so the
            # operator can either ratify it or revert. Default is False/False so
            # plugins without hook policies don't fire.
            if pol.allow_conversation_access or pol.allow_prompt_injection:
                # allow_prompt_injection is alert-level if the plugin isn't in
                # trusted_mutators; allow_conversation_access alone is warn.
                if pol.allow_prompt_injection and pol.plugin_name not in trusted_mutators:
                    findings.append({
                        "type": "hook_plugin_policy_unexpected",
                        "severity": "alert",
                        "signature_scope": f"{bot_id}:{pol.plugin_name}:prompt-injection",
                        "title": (
                            f"{bot_id}: plugin {pol.plugin_name!r} has "
                            "allowPromptInjection=true (not in trusted-mutators allowlist)"
                        ),
                        "body": (
                            f"Plugin {pol.plugin_name!r} can rewrite the bot's system "
                            "prompt arbitrarily. The baseline's trusted_prompt_mutators "
                            "allowlist is empty by default — additions require explicit "
                            "operator approval via UpdateHookBaseline."
                        ),
                        "details": {
                            "bot_id": bot_id,
                            "plugin_name": pol.plugin_name,
                            "plugin_enabled": pol.plugin_enabled,
                            "allow_conversation_access": pol.allow_conversation_access,
                            "allow_prompt_injection": pol.allow_prompt_injection,
                        },
                    })
                else:
                    findings.append({
                        "type": "hook_plugin_policy_unexpected",
                        "severity": "warn",
                        "signature_scope": f"{bot_id}:{pol.plugin_name}:policy",
                        "title": (
                            f"{bot_id}: plugin {pol.plugin_name!r} has hook policy "
                            "not in baseline"
                        ),
                        "body": (
                            f"Plugin {pol.plugin_name!r} has "
                            f"allowConversationAccess={pol.allow_conversation_access} / "
                            f"allowPromptInjection={pol.allow_prompt_injection}. The "
                            "baseline has no expectation for this plugin — either add "
                            "one via UpdateHookBaseline or clear the policy."
                        ),
                        "details": {
                            "bot_id": bot_id,
                            "plugin_name": pol.plugin_name,
                            "plugin_enabled": pol.plugin_enabled,
                            "allow_conversation_access": pol.allow_conversation_access,
                            "allow_prompt_injection": pol.allow_prompt_injection,
                        },
                    })
            continue

        # Baseline expectation exists — check both flags
        expected_conv = exp.allow_conversation_access
        observed_conv = pol.allow_conversation_access
        expected_inj = exp.allow_prompt_injection
        observed_inj = pol.allow_prompt_injection

        # SILENT-DISABLE — the April incident class. Specifically: baseline expects
        # allow_conversation_access=true on an enabled plugin, but observed is false.
        # Fires high-severity because it's an availability failure that looks benign.
        if (
            expected_conv and not observed_conv
            and pol.plugin_enabled
        ):
            findings.append({
                "type": "hook_plugin_policy_silent_disable",
                "severity": "alert",
                "signature_scope": f"{bot_id}:{pol.plugin_name}:silent-disable",
                "title": (
                    f"{bot_id}: plugin {pol.plugin_name!r} silently lost "
                    "allowConversationAccess"
                ),
                "body": (
                    f"Plugin {pol.plugin_name!r} is enabled on {bot_id} but its "
                    "allowConversationAccess flag is false — baseline says it should "
                    "be true. This is the April 2026 incident class: an OpenClaw "
                    "upgrade or manual edit silently dropped the flag and the plugin "
                    "stopped receiving agent_end / llm_output events. Re-set the flag "
                    "via openclaw.json or redeploy."
                ),
                "details": {
                    "bot_id": bot_id,
                    "plugin_name": pol.plugin_name,
                    "expected_allow_conversation_access": expected_conv,
                    "observed_allow_conversation_access": observed_conv,
                    "rationale": exp.rationale,
                },
            })
            continue  # silent-disable supersedes the generic policy_unexpected below

        # Generic policy drift (either direction)
        if observed_conv != expected_conv or observed_inj != expected_inj:
            severity = (
                "alert"
                if observed_inj and not expected_inj and pol.plugin_name not in trusted_mutators
                else "warn"
            )
            findings.append({
                "type": "hook_plugin_policy_unexpected",
                "severity": severity,
                "signature_scope": f"{bot_id}:{pol.plugin_name}:policy",
                "title": (
                    f"{bot_id}: plugin {pol.plugin_name!r} hook policy differs from baseline"
                ),
                "body": (
                    f"Plugin {pol.plugin_name!r} on {bot_id}: "
                    f"allowConversationAccess observed={observed_conv} expected={expected_conv}; "
                    f"allowPromptInjection observed={observed_inj} expected={expected_inj}. "
                    "Reconcile via openclaw.json edit or UpdateHookBaseline proposal."
                ),
                "details": {
                    "bot_id": bot_id,
                    "plugin_name": pol.plugin_name,
                    "plugin_enabled": pol.plugin_enabled,
                    "expected": {
                        "allow_conversation_access": expected_conv,
                        "allow_prompt_injection": expected_inj,
                    },
                    "observed": {
                        "allow_conversation_access": observed_conv,
                        "allow_prompt_injection": observed_inj,
                    },
                },
            })

    # 4. Command-gate (warn) — both commands.plugins and commands.mcp are watched.
    # MCP monitor also tracks commands.mcp; emitting both is intentional — they
    # surface in their respective tabs and the operator sees the same gap from
    # two angles.
    if inv.self_mutation_commands_plugins or inv.self_mutation_commands_mcp:
        which = []
        if inv.self_mutation_commands_plugins:
            which.append("commands.plugins")
        if inv.self_mutation_commands_mcp:
            which.append("commands.mcp")
        findings.append({
            "type": "hook_command_gate_enabled",
            "severity": "warn",
            "signature_scope": f"{bot_id}:cmd-gate",
            "title": f"{bot_id}: bot can self-mutate hook surfaces via slash commands",
            "body": (
                f"Bot {bot_id} has {' + '.join(which)} = true. This lets the bot mutate "
                "plugin hook policies (and/or MCP server config) via slash command at "
                "runtime, bypassing the proposal pipeline. Disable in openclaw.json "
                "unless this is intentionally enabled for a sandbox bot."
            ),
            "details": {
                "bot_id": bot_id,
                "commands_plugins": inv.self_mutation_commands_plugins,
                "commands_mcp": inv.self_mutation_commands_mcp,
            },
        })

    return findings


# ── Public entry point ────────────────────────────────────────────────────────

def run(
    shared_dir: Path,
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    emit_signals: bool = True,
) -> dict[str, Any]:
    """Run the hook monitor across the given bots.

    Side effects:
      - Bootstraps the baseline file on first run.
      - Writes inventory cache to {shared_dir}/hooks/inventory/<bot>.json.
      - Emits Signals via signals.store.observe() unless emit_signals=False.
      - Sweep-resolves prior PRODUCER signals that no longer recur.

    Returns:
      {bots_checked, findings, swept_resolved}
    """
    _bootstrap.write_default_if_missing(shared_dir)
    baseline = _bl.load(shared_dir)

    findings: list[dict[str, Any]] = []
    kept_signatures: set[str] = set()

    for bot_id in bot_ids:
        prior = _inv.load_inventory(shared_dir, bot_id)
        inv = _inv.read_inventory(bot_id, config)
        _inv.write_inventory(inv, shared_dir)
        resolved = _bl.resolve_for(baseline, bot_id)
        for finding in _diff_one_bot(inv, resolved, prior, shared_dir=shared_dir):
            finding["bot_id"] = bot_id
            findings.append(finding)

    swept_resolved = 0
    if emit_signals and _signals_store is not None and _make_signature is not None:
        for f in findings:
            sig = _make_signature(PRODUCER, f["type"], f["signature_scope"])
            kept_signatures.add(sig)
            try:
                _signals_store.observe(
                    shared_dir,
                    signature=sig,
                    producer=PRODUCER,
                    type=f["type"],
                    flavor="security",
                    severity=f["severity"],
                    scope="bot",
                    bot_id=f.get("bot_id"),
                    title=f["title"],
                    body=f["body"],
                    details=f.get("details") or {},
                )
            except Exception:  # noqa: BLE001
                continue
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: hook condition cleared on next monitor run",
                types=_OWNED_TYPES,
            )
            swept_resolved = len(swept)
        except Exception:  # noqa: BLE001
            swept_resolved = 0

    return {
        "bots_checked": len(bot_ids),
        "findings": findings,
        "swept_resolved": swept_resolved,
    }
