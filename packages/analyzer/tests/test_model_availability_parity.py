"""tests/test_model_availability_parity.py — TS<->Python availability parity.

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 3.A.

Availability-aware role resolution ships on BOTH sides (ModelRouter.ts
``resolveRoleAvailability`` and primary_bot.py ``resolve_role_with_availability``).
The two must resolve a given (pod, bot, credentialed) triple to the SAME
{model, resolvedRole, degraded, reason} per role. This pins the Python side to
the shared fixture
``packages/plugin/tests/fixtures/model-availability-parity.json``; the TS test
(``modelRouter.availabilityParity.test.mjs``) pins the TS side to the same file.

It also doubles as the §A acceptance matrix:
  - a non-LLM-only credential set grays every role (uncredentialed);
  - within-rung credentialed fallback is preferred over degradation;
  - an uncredentialed role degrades DOWN the ladder with the honest reason;
  - judge is off-ladder and reports uncredentialed when diversity is
    unsatisfiable under the credentialed set.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from primary_bot import (  # noqa: E402
    merge_model_catalog,
    available_providers_for_resolution,
    resolve_role_with_availability,
    _resolve_judge_availability,
    llm_providers_from_catalog,
)

_FIXTURE = (
    _ANALYZER_DIR.parent
    / "plugin"
    / "tests"
    / "fixtures"
    / "model-availability-parity.json"
)


def _load_scenarios() -> list[dict]:
    return json.loads(_FIXTURE.read_text())["scenarios"]


def _resolve(merged: dict, role: str, avail: set[str] | None) -> dict:
    if role == "judge":
        return _resolve_judge_availability(merged, avail)
    return resolve_role_with_availability(merged, role, avail)


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=lambda s: s["name"])
def test_python_availability_matches_shared_fixture(scenario):
    merged = merge_model_catalog(scenario["pod"], scenario["bot"])
    credentialed = {str(p).lower() for p in scenario["credentialed"]}
    avail = available_providers_for_resolution(merged, credentialed)
    for role, exp in scenario["expect"].items():
        got = _resolve(merged, role, avail)
        assert got["model"] == exp["model"], (
            f"{scenario['name']}: role {role} model {got['model']!r} != {exp['model']!r}"
        )
        assert got["resolved_role"] == exp["resolvedRole"], (
            f"{scenario['name']}: role {role} resolvedRole {got['resolved_role']!r} "
            f"!= {exp['resolvedRole']!r}"
        )
        assert got["degraded"] == exp["degraded"], (
            f"{scenario['name']}: role {role} degraded {got['degraded']} != {exp['degraded']}"
        )
        assert got["reason"] == exp["reason"], (
            f"{scenario['name']}: role {role} reason {got['reason']!r} != {exp['reason']!r}"
        )


def test_llm_capable_set_is_catalog_derived_not_literal():
    """The LLM-capable provider set comes from the catalog rung clusters, so a
    non-LLM credential (brave) is filtered WITHOUT naming it in logic."""
    merged = merge_model_catalog({}, {})
    llm = llm_providers_from_catalog(merged)
    assert "anthropic" in llm and "openai" in llm and "google" in llm
    assert "brave" not in llm
    # brave credentialed -> intersected out of the resolution set.
    avail = available_providers_for_resolution(merged, {"brave", "anthropic"})
    assert avail == {"anthropic"}


def test_none_credentialed_fails_open_for_presentation():
    """Unknown credential state (None) resolves every role as configured — a
    reader that can't see credentials must not gray every role."""
    merged = merge_model_catalog({}, {})
    avail = available_providers_for_resolution(merged, None)
    got = resolve_role_with_availability(merged, "max", avail)
    assert got["model"] == "anthropic/claude-fable-5"
    assert got["degraded"] is False and got["reason"] is None
