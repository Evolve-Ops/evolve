"""Deploy's primary-heal cannot bring a bot's tier config into existence.

``_heal_primary_from_tier_config`` runs on EVERY deploy against EVERY bot and
re-asserts ``{"tiers": <current>}``, which makes it by far the highest-blast-
radius caller of the writer whose create path changed in the #3566 follow-up.
It is also the one caller that is out of scope, and this file is why: it reads
the bot's config first and RETURNS EARLY when there are no tier entries, so it
can only ever hit the writer's preserve/fold branches — never the create one.

That early return is pre-existing behaviour. It is pinned here because the
claim "deploy is unaffected" is load-bearing for the change, and because a
future edit that made the heal write unconditionally would silently turn a
routine deploy into a fleet-wide config-minting pass.

Note ``full_config_get`` projects a rungs/roles file back to the legacy tierN
view (``oc_model.synthesize_legacy_tiers``), so an empty ``tiers`` really does
mean "this bot has no tier definitions at all", in either shape.
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


class _RecordingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.writes: list[dict] = []

    def full_config_set_with_error(self, bot_id, updates, network_path=None):
        self.writes.append(updates)
        return ({"primary": "anthropic/claude-haiku-4-5", "tiers": {}}, None)


@pytest.fixture
def _reset_runtime():
    yield
    set_runtime(None)


@pytest.mark.parametrize("full_config", [
    {"tiers": {}},                       # file exists, defines no tiers
    {"primary": "anthropic/claude-haiku-4-5"},   # no tiers key at all
])
def test_heal_does_not_write_when_the_bot_has_no_tier_config(
    _reset_runtime, full_config,
):
    rt = _RecordingRuntime()
    rt.seed("team_bot_a", full_config=full_config)
    set_runtime(rt)

    result = deploy.DeployResult(bot_id="team_bot_a", success=True)
    deploy._heal_primary_from_tier_config(
        "team_bot_a", "member", Path("/tmp/network-not-read.json"), result,
    )

    assert rt.writes == [], (
        "primary-heal wrote tiers for a bot that has none — on a never-seeded "
        "bot that write would CREATE the bot's tier config as a deploy side "
        "effect, which is provisioning's job, not deploy's (#3566)"
    )
    assert result.success is True


def test_heal_still_writes_for_a_bot_that_has_tier_config(_reset_runtime):
    """The other half: the early return must not have swallowed the real heal."""
    rt = _RecordingRuntime()
    rt.seed("team_bot_a", full_config={"tiers": {"tier3": {"models": ["m/n"]}}})
    set_runtime(rt)

    result = deploy.DeployResult(bot_id="team_bot_a", success=True)
    deploy._heal_primary_from_tier_config(
        "team_bot_a", "member", Path("/tmp/network-not-read.json"), result,
    )

    assert rt.writes == [{"tiers": {"tier3": {"models": ["m/n"]}}}]
