"""Swappability proof for the AgentRuntime seam (roadmap 4.3, Phase D).

This is the executable version of the seam's headline payoff: *the stack
runs with no OpenClaw / macOS / launchd present*. We inject a
``FakeRuntime`` via ``set_runtime()``, seed it, then drive three
representative flows that each reach the seam through ``get_runtime()`` —

  * **Flow A — heal probe** (analyzer): ``heal._probe_gateway_once`` →
    ``runtime.health()``.
  * **Flow B — generator observe** (analyzer):
    ``generators.bot_config_integrity.observe`` → ``runtime.full_config_get()``.
  * **Flow C — fleet-models HTTP endpoint** (admin): ``GET /api/oc/models``
    → ``runtime.models()`` per bot.

— with ``subprocess.run`` / ``subprocess.Popen`` monkeypatched to fail
loudly. ``oc_cli`` shells out to ``openclaw`` exclusively through those two
(it ``import subprocess`` and calls ``subprocess.run`` / ``subprocess.Popen``
by attribute), so if any flow had bypassed the seam and hit the CLI, the
guard would raise. The flows complete, return the seeded data, and spawn
nothing — which is the swappability claim for the runtime dimension.

(Phase D also retired the ``command()`` / ``command_raw()`` escape hatch;
the typed-method forwarding is unit-tested in
``packages/analyzer/tests/test_agent_runtime.py``.)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin.web.routes_oc import register_oc_routes  # noqa: E402

from heal import _probe_gateway_once  # noqa: E402
from runtime.agent_runtime import FakeRuntime, set_runtime  # noqa: E402
from generators.bot_config_integrity.observe import (  # noqa: E402
    BotConfigIntegrityContext,
    observe as bot_config_integrity_observe,
)


class _OpenClawSpawned(AssertionError):
    """Raised when a flow tries to spawn a subprocess — i.e. shell out to
    openclaw/oc_cli — instead of going through the injected runtime."""


class RecordingRuntime(FakeRuntime):
    """FakeRuntime that also records read-method names.

    The base ``FakeRuntime`` only records *mutations* in ``.calls``; the
    three flows here are reads, so we record them in ``.reads`` to assert
    that each flow actually exercised the seam (not just that nothing
    crashed)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def health(self, bot_id, *, timeout=None):
        self.reads.append("health")
        return super().health(bot_id, timeout=timeout)

    def full_config_get(self, bot_id, network_path=None):
        self.reads.append("full_config_get")
        return super().full_config_get(bot_id, network_path=network_path)

    def models(self, bot_id):
        self.reads.append("models")
        return super().models(bot_id)


@pytest.fixture
def runtime():
    rt = RecordingRuntime()
    set_runtime(rt)
    try:
        yield rt
    finally:
        set_runtime(None)  # don't leak the fake into other tests' get_runtime()


@pytest.fixture
def ban_subprocess(monkeypatch):
    """Make any real subprocess spawn fail loudly.

    oc_cli is the only path that shells out to openclaw, and it does so via
    ``subprocess.run`` / ``subprocess.Popen`` (attribute access on the
    module), so patching both here catches every CLI spawn regardless of
    which flow attempts it."""

    def _boom(*args, **kwargs):
        raise _OpenClawSpawned(f"subprocess spawned (no openclaw expected): {args[:1]}")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def _seed_network(tmp_path: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["team_bot_a", "security_bot"],
        "bots": {
            "team_bot_a": {"role": "member", "port": 19002},
            "security_bot": {"role": "member", "port": 19001},
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def test_full_stack_runs_with_no_openclaw_subprocess(runtime, ban_subprocess, tmp_path):
    """Drive a heal check + a generator observe + a model endpoint against a
    seeded FakeRuntime, with subprocess spawning banned. All three reach the
    seam, return the seeded data, and shell out to nothing."""
    haiku = [{"id": "anthropic/claude-haiku-4-5"}]
    sonnet = [{"id": "anthropic/claude-sonnet-4-6"}]
    runtime.seed(
        "team_bot_a",
        health={"ok": True},
        full_config={"agents": {"defaults": {}}},
        models=haiku,
    )
    runtime.seed("security_bot", models=sonnet)

    # Flow A — analyzer heal probe → runtime.health()
    status = _probe_gateway_once("team_bot_a", port=19002, oc_health_timeout=30)
    assert status.healthy is True

    # Flow B — analyzer generator observe → runtime.full_config_get()
    ctx = BotConfigIntegrityContext(bot_id="team_bot_a", shared_dir=tmp_path / "shared")
    proposals = bot_config_integrity_observe(ctx)
    assert isinstance(proposals, list)  # ran end-to-end; seam fed it the config

    # Flow C — admin fleet-models HTTP endpoint → runtime.models() per bot.
    # register_oc_routes resolves patchable helpers via
    # sys.modules["evolve_admin.web.server"] at registration time, so the
    # server module must be importable first.
    import evolve_admin.web.server as _server
    assert _server is not None
    app = Flask(__name__)
    register_oc_routes(app, _seed_network(tmp_path))
    app.config["TESTING"] = True
    with app.test_client() as client:
        resp = client.get("/api/oc/models")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["team_bot_a"] == haiku
    assert data["security_bot"] == sonnet

    # The proof: every flow went through the seam (no _OpenClawSpawned raised
    # above means nothing shelled out), and each exercised its read method.
    assert "health" in runtime.reads
    assert "full_config_get" in runtime.reads
    assert runtime.reads.count("models") == 2  # one per bot, via the fleet fan-out


def test_subprocess_ban_is_non_vacuous(ban_subprocess):
    """Sanity check the guard itself fires — so "nothing spawned" in the
    stack test is a real result, not a silently-disabled fixture. (The
    positive proof that the *seam* carried the flows is the ``runtime.reads``
    + seeded-data assertions above: with the default adapter those reads are
    never recorded and the seeded data never comes back.)"""
    with pytest.raises(_OpenClawSpawned):
        subprocess.run(["echo", "hi"])
    with pytest.raises(_OpenClawSpawned):
        subprocess.Popen(["echo", "hi"])
