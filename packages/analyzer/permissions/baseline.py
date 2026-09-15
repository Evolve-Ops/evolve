"""permissions.baseline — load/write {shared_dir}/policy/permission-baseline.json.

Spec: internal/spec-permission-posture-2026-05-10.md §3.1.

The baseline file is operator-curated. Today it captures the modal pod
posture as ``pod_default``, with per-bot divergences in
``per_bot_overrides``. Mutated only via ``UpdatePermissionBaseline``
proposals (Phase B).

The file lives under ``{shared_dir}`` which has evolve-user ACL — no
sudo / /tmp staging needed. Atomic via temp-file + rename.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_BASELINE: dict[str, Any] = {
    "version": 1,
    "pod_default": {
        "permission_config": {
            # ``security: "full"`` + ``ask: "on-miss"`` is the new member-bot
            # default since the 2026-05-25 pivot
            # (internal/spec-app-derived-permissions-2026-05-24.md). A member bot
            # runs as its own macOS user; "full" treats it like a trusted
            # agent rather than a hostile process. The permissions reconciler
            # writes a manifest-derived would-be allowlist to
            # ``exec-approvals.preview.json`` for operators who want the
            # opt-in toggle to ``security: "allowlist"`` (Phase C of the spec).
            #
            # Phase E.4 (2026-05-25) removed the primary-bot carve-out
            # that previously forced evo to ``security: "deny"``. After
            # Phase E.2.b cutover (evo runs as the unprivileged ``evo``
            # macOS user), the carve-out is no longer needed — see
            # internal/spec-evo-account-separation-2026-05-25.md §"Phase E.4".
            # The pod-default ``"full"`` now applies uniformly to every
            # bot, primary included.
            #
            # Pre-pivot value was "deny" (set to suppress permissive-mode
            # OC advisories on plugin-only bots). The deny default broke the
            # build-me-a-thing flow: team_bot_a's INSTALLED_APPS.md declared a
            # script the deny policy then refused at runtime, producing the
            # 2026-05-24 Slack failure.
            "tools.exec.security": "full",
            "tools.exec.ask": "on-miss",
            "tools.exec.host": None,
            "tools.fs.workspaceOnly": None,
            "tools.web.search.enabled": True,
            "tools.web.fetch.enabled": True,
            "commands.native": "auto",
            "commands.nativeSkills": "auto",
            "commands.elevated": None,
            "commands.useAccessGroups": None,
            "commands.ownerAllowFrom": None,
            # NB: sandbox posture is intentionally NOT tracked here. The OC
            # config schema has no top-level `sandbox` key — the real path
            # is `agents.defaults.sandbox.{mode,backend,workspaceAccess,…}`
            # (oc-config-schema.txt:4920) with `mode: "off"|"non-main"|"all"`
            # instead of a boolean `enabled`. The pre-2026-05-18 baseline
            # listed `sandbox.enabled` as a flat dotted key and ensure_plugin_config
            # tried to write it at top level, which OC's validator rejected with
            # `<root>: Unrecognized key: "sandbox"`. Until sandbox posture is
            # modelled against the real OC schema path, this layer ignores it.
        },
        "exec_approvals": {
            "defaults_expected_empty": True,
            "max_agent_approvals_warn": 50,
            "max_agent_approvals_alarm": 200,
            "denylist_patterns": [
                r"^rm\s+-rf\s+(/|<path>)",
                r"^curl\s+\S+\s*\|\s*(bash|sh|zsh)",
                r"^sudo(\s|$)",
                r"^chmod\s+.*777",
                r"^launchctl\s+(load|bootstrap)",
                r"^chown(\s|$)",
            ],
        },
        "scheduled_invocations": {
            "agent_turn_max_turns_required": True,
            "agent_turn_max_budget_usd_required": True,
            "denylist_patterns": [
                r"curl\s+\S+\s*\|\s*(bash|sh|zsh)",
            ],
        },
    },
    "per_bot_overrides": {},
}


def baseline_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "permission-baseline.json"


def load(shared_dir: Path) -> dict:
    """Return the baseline dict, falling back to DEFAULT_BASELINE if absent."""
    p = baseline_path(shared_dir)
    if not p.exists():
        return deepcopy(DEFAULT_BASELINE)
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULT_BASELINE)


def write(baseline: dict, shared_dir: Path) -> None:
    """Atomic temp + rename. No sudo (shared_dir is evolve-owned)."""
    target = baseline_path(shared_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    os.replace(tmp, target)


def write_default_if_missing(shared_dir: Path) -> bool:
    """Seed the baseline file with the default when first observed.

    Returns True if a new file was written, False if it already existed.
    Bootstrap should call this once at startup.
    """
    p = baseline_path(shared_dir)
    if p.exists():
        return False
    write(DEFAULT_BASELINE, shared_dir)
    return True


def resolve(baseline: dict, bot_id: str) -> dict:
    """Merge ``pod_default`` with the bot's per-bot override.

    Returns a flat dict of resolved permission_config fields for ``bot_id``.
    Used by drift detection (when wired in a later phase) and by the
    posture guard logic.
    """
    pod_default = (baseline.get("pod_default") or {}).get("permission_config") or {}
    override = (
        (baseline.get("per_bot_overrides") or {}).get(bot_id) or {}
    ).get("permission_config") or {}
    merged = deepcopy(pod_default)
    for k, v in override.items():
        merged[k] = v
    return merged


def denylist_for(baseline: dict, surface: str) -> list[str]:
    """Return the operator-curated denylist for 'approvals' or 'cron'."""
    pod_default = baseline.get("pod_default") or {}
    if surface == "approvals":
        return list((pod_default.get("exec_approvals") or {}).get("denylist_patterns") or [])
    if surface == "cron":
        return list((pod_default.get("scheduled_invocations") or {}).get("denylist_patterns") or [])
    return []
