"""plugins.monitor — diff observed inventory vs. baseline; emit Signals.

Spec: docs/spec-plugin-posture-rework-2026-06-06.md.
Original: docs/spec-plugin-inventory-2026-05-10.md §3.3 (superseded for
the alert layer; the inventory it produces is still consumed by the
Plugins page).

The v2 monitor produces signals only for conditions that map to actual
operator risk:

  - plugin_missing_required (alert)            — required plugin disabled
                                                 or absent (no-op when
                                                 required_plugins=[])
  - plugin_denied_present (alert)              — a plugin in
                                                 denied_plugins is enabled
  - plugin_load_path_unexpected (alert)        — load.paths contains a
                                                 directory not in baseline
  - plugin_command_gate_enabled (warn)         — commands.plugins = true
  - plugin_openclaw_config_missing (info)      — bot has no openclaw.json

Retired (v1 → v2): plugin_unexpected_enabled, plugin_unexpected_disabled,
plugin_allow_list_missing, plugin_allow_list_drift, plugin_config_drift.
The retirements stop producing Signals on rollout; the next
sweep_resolve auto-archives any existing firing instances.

Also retired (2026-06-06 follow-up — see
docs/spec-plugin-posture-rework-2026-06-06.md §1.4 amendment):
plugin_unverified_source. The original v2 design treated
installs[*].source as a flat allowlist (path / evolve_app /
oc_plugin_add), but OC's real source values (npm / path / clawhub /
archive / marketplace) plus the separate clawhubChannel field make the
trust signal multi-dimensional. Inventory still carries the provenance
data for the Plugins page; we don't alert until we have a concrete
incident to anchor the trust rules. Type stays in _OWNED_TYPES so any
in-flight firings sweep-resolve cleanly.

Producer name "plugin_monitor" — distinct from "audit" and "mcp_monitor"
so the sweep_resolve doesn't cross-affect.
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


PRODUCER = "plugin_monitor"

_OWNED_TYPES = {
    "plugin_missing_required",
    "plugin_denied_present",
    "plugin_load_path_unexpected",
    "plugin_unverified_source",
    "plugin_command_gate_enabled",
    "plugin_openclaw_config_missing",
    # Retired types are listed here so sweep_resolve auto-archives any
    # firing instances on the first cycle after rollout. Once the
    # archive sweep runs once they can be removed (cosmetic only).
    "plugin_install_source_unauthorized",
    "plugin_unexpected_enabled",
    "plugin_unexpected_disabled",
    "plugin_allow_list_missing",
    "plugin_allow_list_drift",
    "plugin_config_drift",
}


# Operator docs per signal_type (PR #1603 convention — rendered as
# "What this means" + "How to fix" blocks in the Alerts UI).
_OPERATOR_DOCS: dict[str, tuple[str, str]] = {
    "plugin_openclaw_config_missing": (
        "The bot is listed in network.json but has no openclaw.json — "
        "the gateway probably never deployed. Plugin posture can't be "
        "checked because there's nothing to inventory yet.",
        "1. Deploy the bot:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin deploy <bot>\n"
        "2. If the deploy fails, check the deploy log:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/Shared/evolve/logs/admin-actions.jsonl | tail -20\n"
        "3. Once deploy succeeds, the next plugin_monitor pass will "
        "clear this Signal automatically",
    ),
    "plugin_missing_required": (
        "A plugin that the baseline marks as required isn't enabled "
        "on this bot. Any feature that depends on that plugin "
        "silently fails — the gateway runs but the plugin's hooks / "
        "tools / subagents aren't loaded.",
        "1. Open Security → Plugins for the affected bot\n"
        "2. Either enable the plugin in `openclaw.json` (via the "
        "UpdatePluginEntry proposal flow on the same page) or, if "
        "the plugin genuinely shouldn't run on this bot, remove it "
        "from `required_plugins` in "
        "`/Users/Shared/evolve/policy/plugin-baseline.json`\n"
        "3. Restart the gateway after enabling:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "plugin_denied_present": (
        "A plugin in the pod's `denied_plugins` list is enabled on "
        "this bot. The denylist marks plugins as never-allowed (usually "
        "because they have a known security or stability issue); the "
        "bot is currently loading that code. Active exposure, not "
        "drift.",
        "1. Open Security → Plugins for the affected bot and disable "
        "the named plugin immediately\n"
        "2. Investigate how it got enabled — check "
        "`/Users/Shared/evolve/logs/admin-actions.jsonl` for a "
        "matching plugin-enable entry\n"
        "3. If the denylist rule is wrong, update the baseline at "
        "`/Users/Shared/evolve/policy/plugin-baseline.json` — but "
        "only after the plugin is disabled\n"
        "4. Restart the gateway:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "plugin_load_path_unexpected": (
        "A directory in the bot's `plugins.load.paths` isn't in the "
        "baseline's `expected_load_paths`. Any plugin code dropped "
        "into that directory at runtime will load — supply-chain "
        "risk. Treat as alert-tier; drift suggests either a config "
        "mistake or an out-of-band change worth investigating.",
        "1. Identify the unexpected path from "
        "`details.unexpected_path`\n"
        "2. Verify it doesn't currently contain anything malicious:\n"
        "   ssh pod_admin_user@mini sudo /bin/ls -la <unexpected_path>\n"
        "3. Either remove the path from `plugins.load.paths` in the "
        "bot's openclaw.json, or update the baseline's "
        "`expected_load_paths` if the path is intentional\n"
        "4. Restart the gateway after applying:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "plugin_command_gate_enabled": (
        "The bot has `commands.plugins = true` — meaning operators "
        "(or anyone with chat access) can enable / disable plugins "
        "via the `/plugins` slash command at runtime, bypassing the "
        "proposal pipeline and any per-bot policy. Active drift "
        "vector: the bot's plugin posture can change without an "
        "audit trail.",
        "1. Edit `/Users/<bot>/.openclaw/openclaw.json` and set "
        "`commands.plugins = false` (or remove the line — the "
        "default is false)\n"
        "2. Restart the gateway to drop the runtime gate:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>\n"
        "3. After restart, plugin changes must flow through the "
        "proposal pipeline — propose UpdatePluginEntry via the "
        "admin UI instead",
    ),
}


def _operator_docs(signal_type: str) -> tuple[str, str] | None:
    return _OPERATOR_DOCS.get(signal_type)


# ── Per-bot diff ──────────────────────────────────────────────────────────────

def _diff_one_bot(
    inv: _inv.PluginInventory,
    resolved: _bl.ResolvedBotBaseline,
) -> list[dict[str, Any]]:
    """Compute findings for one bot's inventory vs. its resolved baseline."""
    findings: list[dict[str, Any]] = []
    bot_id = inv.bot_id

    # 1. Missing openclaw.json — info severity, blocks the rest
    if not inv.openclaw_config_present:
        findings.append({
            "type": "plugin_openclaw_config_missing",
            "severity": "info",
            "signature_scope": bot_id,
            "title": f"{bot_id}: openclaw.json not found",
            "body": (
                f"Bot {bot_id} is in network.json but has no openclaw.json at "
                f"{inv.openclaw_config_path}. Plugin inventory cannot be checked."
            ),
            "details": {
                "bot_id": bot_id,
                "openclaw_config_path": inv.openclaw_config_path,
                "read_error": inv.read_error,
            },
        })
        return findings

    enabled_set = {e.name for e in inv.entries if e.enabled}
    all_entry_names = {e.name for e in inv.entries}

    # 2. Missing required plugins (alert). No-op when required_plugins=[]
    #    (the v2 default).
    for req in sorted(resolved.required):
        if req not in enabled_set:
            findings.append({
                "type": "plugin_missing_required",
                "severity": "alert",
                "signature_scope": f"{bot_id}:{req}",
                "title": f"{bot_id}: required plugin {req!r} is not enabled",
                "body": (
                    f"Plugin {req!r} is required pod-wide but is not enabled on {bot_id}. "
                    "Operations dependent on it will silently fail. Re-enable via openclaw.json "
                    "or remove from required_plugins in the baseline if it's not really required."
                ),
                "details": {
                    "bot_id": bot_id, "plugin_name": req,
                    "currently_enabled": req in enabled_set,
                    "present_as_entry": req in all_entry_names,
                },
            })

    # 3. Denied plugins enabled (alert)
    for denied in sorted(resolved.denied):
        if denied in enabled_set:
            findings.append({
                "type": "plugin_denied_present",
                "severity": "alert",
                "signature_scope": f"{bot_id}:{denied}",
                "title": f"{bot_id}: denied plugin {denied!r} is enabled",
                "body": (
                    f"Plugin {denied!r} is in the pod's denied_plugins but is enabled on {bot_id}. "
                    "Disable immediately or remove from the denylist via UpdatePluginBaseline."
                ),
                "details": {"bot_id": bot_id, "plugin_name": denied},
            })

    # 4. Load-path drift (alert) — unauthorized directory in load.paths
    expected_paths = set(resolved.expected_load_paths)
    for observed_path in inv.load_paths:
        if observed_path not in expected_paths:
            findings.append({
                "type": "plugin_load_path_unexpected",
                "severity": "alert",
                "signature_scope": f"{bot_id}:{observed_path}",
                "title": f"{bot_id}: plugins.load.paths contains unexpected directory",
                "body": (
                    f"{bot_id}'s plugins.load.paths includes {observed_path!r}, which is not in "
                    f"the baseline's expected_load_paths ({sorted(expected_paths)}). Any plugin "
                    "code dropped into that directory will load — supply-chain risk."
                ),
                "details": {
                    "bot_id": bot_id, "unexpected_path": observed_path,
                    "expected_paths": sorted(expected_paths),
                },
            })

    # 5. Command gate (warn) — commands.plugins = true
    if inv.self_mutation_commands_plugins:
        findings.append({
            "type": "plugin_command_gate_enabled",
            "severity": "warn",
            "signature_scope": f"{bot_id}:cmd-gate",
            "title": f"{bot_id}: commands.plugins is true",
            "body": (
                f"Bot {bot_id} has commands.plugins=true. It can enable/disable plugins via "
                "/plugins slash command at runtime, bypassing the proposal pipeline."
            ),
            "details": {"bot_id": bot_id},
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
    """Run the plugin monitor across the given bots.

    Side effects:
      - Bootstraps the baseline file on first run (v2 shape).
      - Writes inventory cache to {shared_dir}/plugins/inventory/<bot>.json.
      - Emits Signals via signals.store.observe() unless emit_signals=False.
      - Sweep-resolves prior PRODUCER signals that no longer recur. The
        v2 sweep includes the retired v1 types in _OWNED_TYPES so any
        firing instances on rollout auto-archive on the first cycle.

    Returns:
      {bots_checked, findings, swept_resolved}
    """
    _bootstrap.write_default_if_missing(shared_dir)
    baseline = _bl.load(shared_dir)

    findings: list[dict[str, Any]] = []
    kept_signatures: set[str] = set()

    for bot_id in bot_ids:
        inv = _inv.read_inventory(bot_id, config)
        _inv.write_inventory(inv, shared_dir)
        resolved = _bl.resolve_for(baseline, bot_id)
        for finding in _diff_one_bot(inv, resolved):
            finding["bot_id"] = bot_id
            findings.append(finding)

    swept_resolved = 0
    if emit_signals and _signals_store is not None and _make_signature is not None:
        for f in findings:
            sig = _make_signature(PRODUCER, f["type"], f["signature_scope"])
            kept_signatures.add(sig)
            details = dict(f.get("details") or {})
            docs = _operator_docs(f["type"])
            if docs is not None:
                details["what_it_means"], details["fix_steps"] = docs
            try:
                _signals_store.observe(
                    shared_dir,
                    signature=sig,
                    producer=PRODUCER,
                    type=f["type"],
                    flavor="maintenance",
                    severity=f["severity"],
                    scope="bot",
                    bot_id=f.get("bot_id"),
                    title=f["title"],
                    body=f["body"],
                    details=details,
                )
            except Exception:  # noqa: BLE001
                continue
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: plugin condition cleared on next monitor run",
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
