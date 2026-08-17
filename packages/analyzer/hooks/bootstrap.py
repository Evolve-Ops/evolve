"""hooks.bootstrap — produce the initial pod baseline from deploy.py invariants.

Spec: docs/spec-hook-governance-2026-05-10.md §5.1.

The bootstrap encodes today's enforcement directly. ``deploy.py:1069``
sets ``allowConversationAccess=true`` on every bot's evolve plugin;
nothing else gets a hook policy; webhook ingress is disabled.

Once Phase B lands, deploy.py reads from this baseline file instead
of hardcoding the constant. Bootstrap stays as the first-run seed.
"""
from __future__ import annotations

from pathlib import Path

from evolve_util import now_iso as _utc_now_iso

from .baseline import (
    HookBaseline, PodDefault, WebhookIngressExpectation,
    PluginPolicyExpectation, write as _write_baseline, baseline_path,
)


def build_default_baseline() -> HookBaseline:
    """Produce the v1 baseline matching today's live pod state."""
    return HookBaseline(
        version=1,
        bootstrapped_at=_utc_now_iso(),
        pod_default=PodDefault(
            webhook_ingress=WebhookIngressExpectation(
                enabled=False,
                rationale=(
                    "Pod has no external webhook integrations configured today. "
                    "Enabling requires explicit operator approval through an "
                    "UpdateHookBaseline proposal in Phase B."
                ),
            ),
            plugin_typed_hooks=[
                PluginPolicyExpectation(
                    plugin_name="evolve",
                    allow_conversation_access=True,
                    allow_prompt_injection=False,
                    rationale=(
                        "Set by deploy.py since OC 2026.4.29 — required for "
                        "TurnObserver to receive agent_end/llm_output events. "
                        "Memory: project_oc_per_bot_hook_optin.md."
                    ),
                ),
            ],
            trusted_prompt_mutators=[],
        ),
        per_bot_overrides={},
    )


def write_default_if_missing(shared_dir: Path) -> bool:
    """Create the baseline file from build_default_baseline() if missing."""
    if baseline_path(shared_dir).exists():
        return False
    _write_baseline(build_default_baseline(), shared_dir)
    return True
