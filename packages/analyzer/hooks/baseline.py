"""hooks.baseline — pod-curated hook policy baseline.

Spec: internal/spec-hook-governance-2026-05-10.md §3.1 (Pod hook baseline).

Single file at ``{shared_dir}/policy/hook-baseline.json``. The
monitor compares each bot's observed inventory against the resolved
baseline (pod_default + per_bot overrides).

The baseline file becomes the source of truth for what deploy.py
enforces today via a hardcoded ``allowConversationAccess=true`` on
the evolve plugin (deploy.py:1069). Phase B will refactor deploy.py
to read from this file; Phase A just observes against it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── Locations ─────────────────────────────────────────────────────────────────

def baseline_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "hook-baseline.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class WebhookIngressExpectation:
    enabled: bool = False
    rationale: str = ""


@dataclass
class PluginPolicyExpectation:
    """Per-plugin baseline expectation for hook flags."""

    plugin_name: str
    allow_conversation_access: bool
    allow_prompt_injection: bool
    rationale: str = ""


@dataclass
class PodDefault:
    webhook_ingress: WebhookIngressExpectation = field(default_factory=WebhookIngressExpectation)
    plugin_typed_hooks: list[PluginPolicyExpectation] = field(default_factory=list)
    # Plugins permitted to set allowPromptInjection=true. v1 ships empty;
    # populated via UpdateHookBaseline proposals when an operator approves
    # an exception. Spec §3.1.
    trusted_prompt_mutators: list[str] = field(default_factory=list)


@dataclass
class HookBaseline:
    version: int = 1
    pod_default: PodDefault = field(default_factory=PodDefault)
    # per_bot_overrides currently unused (v1 has no per-bot deviation for
    # hook policy; every bot gets identical evolve plugin hooks). Kept in
    # the schema for forward-compat.
    per_bot_overrides: dict[str, dict] = field(default_factory=dict)
    bootstrapped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bootstrapped_at": self.bootstrapped_at,
            "pod_default": {
                "webhook_ingress": asdict(self.pod_default.webhook_ingress),
                "plugin_typed_hooks": [asdict(p) for p in self.pod_default.plugin_typed_hooks],
                "trusted_prompt_mutators": list(self.pod_default.trusted_prompt_mutators),
            },
            "per_bot_overrides": dict(self.per_bot_overrides),
        }


@dataclass
class ResolvedBotBaseline:
    """Effective per-bot baseline produced by ``resolve_for``."""

    bot_id: str
    webhook_ingress_enabled: bool
    expected_plugin_policies: dict[str, PluginPolicyExpectation]
    trusted_prompt_mutators: set[str]


def resolve_for(baseline: HookBaseline, bot_id: str) -> ResolvedBotBaseline:
    """v1: pod-wide only. per_bot_overrides reserved for future use."""
    expected = {p.plugin_name: p for p in baseline.pod_default.plugin_typed_hooks}
    return ResolvedBotBaseline(
        bot_id=bot_id,
        webhook_ingress_enabled=baseline.pod_default.webhook_ingress.enabled,
        expected_plugin_policies=expected,
        trusted_prompt_mutators=set(baseline.pod_default.trusted_prompt_mutators),
    )


# ── Read / write ──────────────────────────────────────────────────────────────

def _load_raw(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load(shared_dir: Path) -> HookBaseline:
    raw = _load_raw(baseline_path(shared_dir))
    if raw is None:
        return HookBaseline()
    pd_raw = raw.get("pod_default") or {}
    web = pd_raw.get("webhook_ingress") or {}
    plugins_raw = pd_raw.get("plugin_typed_hooks") or []
    plugin_expectations = []
    for p in plugins_raw:
        if not isinstance(p, dict):
            continue
        plugin_expectations.append(
            PluginPolicyExpectation(
                plugin_name=str(p.get("plugin_name") or ""),
                allow_conversation_access=bool(p.get("allow_conversation_access", False)),
                allow_prompt_injection=bool(p.get("allow_prompt_injection", False)),
                rationale=str(p.get("rationale") or ""),
            )
        )
    return HookBaseline(
        version=int(raw.get("version") or 1),
        pod_default=PodDefault(
            webhook_ingress=WebhookIngressExpectation(
                enabled=bool(web.get("enabled", False)),
                rationale=str(web.get("rationale") or ""),
            ),
            plugin_typed_hooks=[p for p in plugin_expectations if p.plugin_name],
            trusted_prompt_mutators=list(pd_raw.get("trusted_prompt_mutators") or []),
        ),
        per_bot_overrides=dict(raw.get("per_bot_overrides") or {}),
        bootstrapped_at=str(raw.get("bootstrapped_at") or ""),
    )


def write(baseline: HookBaseline, shared_dir: Path) -> None:
    path = baseline_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = baseline.to_dict()
    payload["_comment"] = (
        "Pod-wide hook baseline. Phase A of "
        "internal/spec-hook-governance-2026-05-10.md. Today this is bootstrapped "
        "from deploy.py:1069 invariants (evolve plugin gets "
        "allowConversationAccess=true; everything else off). Phase B will "
        "expose UpdateHookBaseline proposals + refactor deploy.py to read "
        "from this file; for now manual edits work but bypass the audit trail."
    )
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
