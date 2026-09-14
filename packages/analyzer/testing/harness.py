"""testing.harness — Helpers for building synthetic proposals and charters.

Keeps tests readable by wrapping the boilerplate of Proposal construction
into a small set of high-level factories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from schema.generator import Charter, Invariant
from schema.proposal import (
    Claim,
    ConfigPatch,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    WorkflowInstruction,
)


def _iso(dt: datetime | None = None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds")


def _new_id() -> str:
    return str(uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Proposal factories
# ─────────────────────────────────────────────────────────────────────────────


def make_investigation_proposal(
    *,
    bot_id: str = "test_bot",
    generator_id: str = "test_generator",
    dimension: str = "substrate_health",
    urgency: str = "operational_urgent",
    problem: str = "Test investigation",
    context: str = "Test context",
    audience: str = "pod_operator",
) -> Proposal:
    """Construct an Investigation proposal — no claim, no revert."""
    return Proposal(
        id=_new_id(),
        bot_id=bot_id,
        generator_id=generator_id,
        dimension=dimension,
        trigger_observations=[f"obs-{_new_id()[:8]}"],
        provenance=Provenance(technique="test_harness", confidence=0.8),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        ),
        urgency=urgency,  # type: ignore[arg-type]
        approval_audience=audience,  # type: ignore[arg-type]
        admin_surface_summary=problem[:120],
    )


def make_config_patch_proposal(
    *,
    target_path: str,
    operation: str = "set",
    value: Any = None,
    bot_id: str = "test_bot",
    generator_id: str = "test_generator",
    dimension: str = "substrate_health",
    urgency: str = "substrate_warn",
    problem: str = "Test config patch",
    claim_metric: str = "test.some_metric",
    claim_direction: str = "up",
    claim_window_days: int = 1,
    reversibility: str = "auto",
    blast_radius: str = "bot",
    touches: list[str] | None = None,
) -> Proposal:
    """Construct a ConfigPatch proposal with a claim.

    The caller is responsible for passing a ``target_path`` under a test
    temp directory if they're going to apply it.
    """
    return Proposal(
        id=_new_id(),
        bot_id=bot_id,
        generator_id=generator_id,
        dimension=dimension,
        trigger_observations=[f"obs-{_new_id()[:8]}"],
        provenance=Provenance(technique="test_harness", confidence=0.8),
        problem=problem,
        action=ConfigPatch(target_path=target_path, operation=operation, value=value),
        risk_tag=RiskTag(
            blast_radius=blast_radius,  # type: ignore[arg-type]
            reversibility=reversibility,  # type: ignore[arg-type]
            touches=touches or ["config"],
        ),
        claim=Claim(
            metric=claim_metric,
            direction=claim_direction,  # type: ignore[arg-type]
            magnitude=1.0,
            window_days=claim_window_days,
            baseline=0.0,
            fallback="revert",
        ),
        # revert_on_failure intentionally left None; arbiter.apply
        # populates it via capture_snapshot at apply time.
        urgency=urgency,  # type: ignore[arg-type]
        approval_audience="none",  # set by routing
        admin_surface_summary=problem[:120],
    )


def make_workflow_proposal(
    *,
    bot_id: str = "test_bot",
    path: str = "evolve/test-workflow.md",
    content: str = "# Test workflow",
    generator_id: str = "test_generator",
    dimension: str = "utility",
    urgency: str = "improvement",
    problem: str = "Test workflow instruction",
) -> Proposal:
    """Construct a WorkflowInstruction proposal."""
    return Proposal(
        id=_new_id(),
        bot_id=bot_id,
        generator_id=generator_id,
        dimension=dimension,
        trigger_observations=[f"obs-{_new_id()[:8]}"],
        provenance=Provenance(technique="test_harness", confidence=0.8),
        problem=problem,
        action=WorkflowInstruction(bot_id=bot_id, path=path, content=content),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["workflow_doc"],
        ),
        claim=Claim(
            metric="workflow.usage_count",
            direction="up",
            magnitude=1.0,
            window_days=14,
            baseline=0.0,
            fallback="revert",
        ),
        urgency=urgency,  # type: ignore[arg-type]
        approval_audience="bot_primary_user",
        admin_surface_summary=problem[:120],
        conversational_pitch="Want me to write a note about this?",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Charter factory
# ─────────────────────────────────────────────────────────────────────────────


def make_charter(
    *,
    generator_id: str = "test_generator",
    gen_type: str = "guardian",
    dimension: str = "substrate_health",
    purpose: str = "test generator",
    cadence: str = "hourly",
    action_allowlist: list[str] | None = None,
    touches_forbidden: list[str] | None = None,
    invariants: list[Invariant] | None = None,
) -> Charter:
    """Construct a minimal Charter for testing.

    By default includes an `action_kind_allowed` invariant permitting
    Investigation + ConfigPatch + WorkflowInstruction. Override via
    ``invariants`` to test invariant checking explicitly.
    """
    if invariants is None:
        invariants = [
            Invariant(
                id="default_allowlist",
                description="test allowlist",
                check_kind="action_kind_allowed",
                params={
                    "allowlist": action_allowlist
                    or ["Investigation", "ConfigPatch", "WorkflowInstruction"]
                },
            )
        ]
        if touches_forbidden:
            invariants.append(
                Invariant(
                    id="default_touches",
                    description="test touches forbidden",
                    check_kind="touches_forbidden",
                    params={"forbidden": touches_forbidden},
                )
            )

    return Charter(
        id=generator_id,
        type=gen_type,  # type: ignore[arg-type]
        dimension=dimension,
        purpose=purpose,
        cadence=cadence,  # type: ignore[arg-type]
        invariants=invariants,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Harness class — bundles a temp dir for hermetic tests
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TestHarness:
    """Manages a temp directory hosting shared state for a single test.

    Use via a context manager pattern; the caller is responsible for
    cleanup (typically via pytest ``tmp_path`` fixtures).

    Attributes:
        root:    base temp dir
        shared_dir: ``<root>/shared`` — where /Users/Shared/evolve stuff goes
        config_path: synthesized config file path the tests can patch
    """

    root: Path
    shared_dir: Path = field(init=False)
    config_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.shared_dir = self.root / "shared"
        self.shared_dir.mkdir(exist_ok=True, parents=True)
        (self.shared_dir / "proposals").mkdir(exist_ok=True)
        (self.shared_dir / "proposals" / "pending").mkdir(exist_ok=True)
        (self.shared_dir / "generators").mkdir(exist_ok=True)
        (self.shared_dir / "annotations").mkdir(exist_ok=True)
        self.config_path = self.root / "config.json"

    def make_config_patch_on_fixture(
        self, *, key: str, value: Any = "initial", **kwargs
    ) -> Proposal:
        """Build a ConfigPatch proposal targeting the harness's config_path."""
        target = f"{self.config_path}::{key}"
        return make_config_patch_proposal(target_path=target, value=value, **kwargs)

    def seed_config(self, data: dict) -> None:
        """Write an initial config JSON the ConfigPatch applier can patch."""
        import json as _json

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=2)
