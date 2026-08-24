"""arbiter.appliers — Action kind dispatch table.

Each Action variant has a corresponding Applier module implementing the
``Applier`` protocol from ``base``. Dispatch happens by ``action.kind``.

L1 ships only the appliers needed for existing proposals and the kinds
legacy migration produces:
  - ConfigPatch (wraps the existing ``apply.py`` safety gates)
  - WorkflowInstruction (writes a markdown file)
  - Investigation (no-op — closes the proposal without mutation)

Other action kinds land with their generators (L2+).
"""

from arbiter.appliers.base import (
    Applier,
    ApplyResult,
    RevertResult,
    get_applier,
    known_action_kinds,
    register_applier,
)
from arbiter.appliers import (
    config_patch,  # noqa: F401 — registers on import
    workflow_instruction,  # noqa: F401
    investigation,  # noqa: F401
    soul_edit,  # noqa: F401 — L6
    deprecate_app,  # noqa: F401 — L6
    promote_app,  # noqa: F401 — AL-1.7 discovered → defined vouch
    throttle_generator,  # noqa: F401 — L6 (covers PauseGenerator too)
    tier_adjustment,  # noqa: F401 — Budget Hawk hard-cap downgrade
    manifest_update,  # noqa: F401 — test_gate_backfill set_fields
    build_app,  # noqa: F401 — RSI new-app dispatch to forge
    retire_orphan,  # noqa: F401 — app_posture_reflection delete_orphan (PR9)
    mcp_server,  # noqa: F401 — Install/Remove/UpdateMcpServerConfig (Phase B)
    plugin,  # noqa: F401 — Enable/Disable/UpdateAllowDeny/UpdateBaseline (Phase B)
    hook,  # noqa: F401 — Enable/DisableWebhookIngress, UpdateWebhookMapping, UpdatePluginHookPolicy, UpdateHookBaseline (Phase B)
    content_scan,  # noqa: F401 — UpdateContentScanCatalog (Phase A)
    add_signal_collection,  # noqa: F401 — proposal_synthesizer SignalGapProposal
    permissions,  # noqa: F401 — UpdatePermissionConfig (Phase B1)
    autonomy_posture,  # noqa: F401 — UpdateAutonomyPosture (autonomy ladder Phase B)
    model_catalog,  # noqa: F401 — ReconcileModelCatalog (catalog-tier drift)
    adopt_model,  # noqa: F401 — AdoptModel (model_discovery adoption, Phase 5)
    agent_defaults,  # noqa: F401 — UpdateAgentDefaults (cache-retention L2)
)

__all__ = [
    "Applier",
    "ApplyResult",
    "RevertResult",
    "get_applier",
    "known_action_kinds",
    "register_applier",
]
