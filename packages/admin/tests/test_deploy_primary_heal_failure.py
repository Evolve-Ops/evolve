"""Workspace-step config-set failures must surface in DeployResult.

Incident (2026-06-10 canary): during ``sudo evolve-admin deploy <bot>`` the
primary-heal pass printed ``oc_full_config_set bot=<bot> exit=1`` to stderr
— openclaw.json was never updated — yet the "Setting up workspace" step
reported a plain "done". ``_heal_primary_from_tier_config`` logged the
failed write with ``result.log("[warn] …")``, which never flips
``DeployResult.success``, so the CLI step (cli.py::_full_deploy) saw
success and printed no warning summary.

Locked here:
  1. a failed ``full_config_set_with_error`` marks the DeployResult failed
     (success=False + an entry in ``.errors``) — the CLI step then renders
     "done (warnings: …)" and the deploy *continues* (record failure,
     continue, surfaced summary — the sibling non-fatal-step convention);
  2. the happy path stays non-fatal: a successful heal write leaves
     success=True.

The runtime is injected via ``set_runtime()`` (the AgentRuntime seam), not
by patching a module's ``subprocess`` — patch-by-module-name fakes stop
intercepting when code moves (see test_deploy_scheduler_migration.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import deploy  # noqa: E402
from runtime.agent_runtime import FakeRuntime, set_runtime  # noqa: E402


class FailingWriteRuntime(FakeRuntime):
    """FakeRuntime whose config writes fail like the canary's exit=1."""

    def full_config_set_with_error(self, bot_id, updates, network_path=None):
        self.calls.append(("full_config_set_with_error", bot_id, updates))
        return (None, "exit=1: openclaw config validate died (EACCES on cwd)")


@pytest.fixture
def _reset_runtime():
    yield
    set_runtime(None)  # don't leak the fake into other tests' get_runtime()


def test_failed_config_set_marks_deploy_result_failed(_reset_runtime):
    rt = FailingWriteRuntime()
    rt.seed("team_bot_a", full_config={"tiers": {"tier3": {"models": ["m"]}}})
    set_runtime(rt)

    result = deploy.DeployResult(bot_id="team_bot_a", success=True)
    deploy._heal_primary_from_tier_config(
        "team_bot_a", "member", Path("/tmp/does-not-matter-network.json"), result,
    )

    assert result.success is False, (
        "a failed config write left DeployResult.success=True — the CLI "
        "workspace step would report plain 'done' (the canary bug)"
    )
    assert any("primary-heal" in e and "exit=1" in e for e in result.errors)
    # The write was actually attempted (failure came from the seam, not a skip).
    assert any(c[0] == "full_config_set_with_error" for c in rt.calls)


def test_successful_heal_write_stays_non_fatal(_reset_runtime):
    rt = FakeRuntime()
    rt.seed("team_bot_a", full_config={"tiers": {"tier3": {"models": ["m"]}}})
    set_runtime(rt)

    result = deploy.DeployResult(bot_id="team_bot_a", success=True)
    deploy._heal_primary_from_tier_config(
        "team_bot_a", "member", Path("/tmp/does-not-matter-network.json"), result,
    )

    assert result.success is True
    assert result.errors == []
