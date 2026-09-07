"""ALPHA-2 — the pod-wide sync response says how many bots scanned degraded.

Audit B2a's operator-visible half: clicking *Sync all bots* on a pod whose
provider key does not resolve must leave the operator knowing the model phase
did not run. Per-bot fields alone do not do that — a three-bot rollup that
reports ``total_discovered: 0`` and nothing else reads as "there is nothing
here".

Reasons are GROUPED, because five bots sharing one missing key is one thing to
fix, not five.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.reflect import ReflectResult  # noqa: E402
from evolve_admin.applications import sync as sync_mod  # noqa: E402
from evolve_admin.web import routes_applications_sync as routes  # noqa: E402


BOTS = ["bot-a", "bot-b", "bot-c"]


def _fake_result(bot_id: str, *, degraded: bool, reason: str = "no_llm_provider_key"):
    kwargs = {}
    if degraded:
        note, remedy = ("Discovery ran without a model.",
                        "Add a provider key under Plugins → Credentials.")
        kwargs = {
            "llm_phase": sync_mod.LLM_PHASE_SKIPPED_DEGRADED,
            "llm_degraded": True, "llm_degraded_reason": reason,
            "llm_degraded_note": note, "llm_degraded_remedy": remedy,
        }
    else:
        kwargs = {"llm_phase": sync_mod.LLM_PHASE_RAN}
    return sync_mod._result(
        bot_id, "escalated", "…", 0, [], ReflectResult(bot_id=bot_id), **kwargs,
    )


@pytest.fixture
def pod(tmp_path, monkeypatch):
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {b: {"user": b} for b in BOTS},
    }))

    def _install(per_bot):
        monkeypatch.setattr(
            routes, "load_network", lambda *_a, **_kw: json.loads(network.read_text()),
        )
        monkeypatch.setattr(sync_mod, "sync_bot",
                            lambda bot, *a, **kw: per_bot(bot))
        app = Flask(__name__)
        routes.register_applications_sync_routes(app, network)
        app.testing = True
        return app.test_client()

    return _install


def _post(client) -> dict:
    res = client.post("/api/applications/sync/pod")
    assert res.status_code == 200, res.data
    return res.get_json()


def test_a_wholly_degraded_pod_names_every_bot_once_under_one_reason(pod):
    body = _post(pod(lambda b: _fake_result(b, degraded=True)))
    assert body["degraded_bots"] == BOTS
    assert len(body["llm_degraded_reasons"]) == 1, (
        "one missing provider key is one thing to fix, not three"
    )
    entry = body["llm_degraded_reasons"][0]
    assert entry["reason"] == "no_llm_provider_key"
    assert entry["bots"] == BOTS
    assert entry["note"] and entry["remedy"], (
        "a banner with no remedy is the dead-end chip principle-alerts forbids"
    )


def test_a_healthy_pod_reports_no_degradation_at_all(pod):
    body = _post(pod(lambda b: _fake_result(b, degraded=False)))
    assert body["degraded_bots"] == []
    assert body["llm_degraded_reasons"] == []


def test_a_partly_degraded_pod_names_only_the_affected_bots(pod):
    body = _post(pod(lambda b: _fake_result(b, degraded=(b == "bot-b"))))
    assert body["degraded_bots"] == ["bot-b"]
    assert body["llm_degraded_reasons"][0]["bots"] == ["bot-b"]


def test_two_different_reasons_stay_two_lines(pod):
    def per_bot(b):
        if b == "bot-a":
            return _fake_result(b, degraded=True, reason="no_llm_provider_key")
        if b == "bot-b":
            return _fake_result(b, degraded=True, reason="scan_error")
        return _fake_result(b, degraded=False)

    body = _post(pod(per_bot))
    assert [r["reason"] for r in body["llm_degraded_reasons"]] == [
        "no_llm_provider_key", "scan_error",
    ]


def test_the_per_bot_slots_still_carry_their_own_fields(pod):
    body = _post(pod(lambda b: _fake_result(b, degraded=True)))
    assert all(s["llm_phase"] == sync_mod.LLM_PHASE_SKIPPED_DEGRADED
               for s in body["bots"])


def test_a_bot_that_raised_does_not_break_the_rollup(pod):
    def per_bot(b):
        if b == "bot-b":
            raise RuntimeError("boom")
        return _fake_result(b, degraded=True)

    body = _post(pod(per_bot))
    assert body["degraded_bots"] == ["bot-a", "bot-c"]
    assert {"bot_id": "bot-b", "error": "boom"} in body["bots"]
