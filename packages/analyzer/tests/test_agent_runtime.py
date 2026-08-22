"""AgentRuntime seam — Phase A (roadmap 4.3).

Behavior-preserving: OpenClawRuntime delegates to oc_cli; get_runtime() returns it
by default. FakeRuntime lets the stack run with no OpenClaw/macOS present, and
both adapters structurally satisfy the AgentRuntime Protocol.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

ar = importlib.import_module("runtime.agent_runtime")


def teardown_function():
    ar.set_runtime(None)  # don't leak a fake into other tests


def test_default_runtime_is_openclaw():
    ar.set_runtime(None)
    assert isinstance(ar.get_runtime(), ar.OpenClawRuntime)


def test_openclaw_adapter_delegates_to_oc_cli(monkeypatch):
    seen = {}

    def fake_status(bot_id):
        seen["bot"] = bot_id
        return {"up": True}

    monkeypatch.setattr("oc_cli.oc_status", fake_status, raising=False)
    out = ar.OpenClawRuntime().status("team_bot_a")
    assert out == {"up": True}
    assert seen["bot"] == "team_bot_a"


def test_openclaw_mutator_delegates_and_returns_bool(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "oc_cli.oc_model_set",
        lambda b, p, fb, network_path=None: calls.append((b, p, fb, network_path)) or True,
        raising=False,
    )
    ok = ar.OpenClawRuntime().model_set("team_bot_a", "anthropic/claude-haiku-4-5", ["fallback/x"])
    assert ok is True
    assert calls == [("team_bot_a", "anthropic/claude-haiku-4-5", ["fallback/x"], None)]


def test_set_runtime_swaps_the_adapter():
    fake = ar.FakeRuntime()
    ar.set_runtime(fake)
    assert ar.get_runtime() is fake


def test_fake_runtime_seed_and_read():
    fake = ar.FakeRuntime()
    fake.seed("team_bot_a", status={"up": True}, models=["haiku", "sonnet"])
    assert fake.status("team_bot_a") == {"up": True}
    assert fake.models("team_bot_a") == ["haiku", "sonnet"]
    assert fake.status("unknown_bot") is None


def test_fake_runtime_records_mutations():
    fake = ar.FakeRuntime()
    assert fake.model_set("team_bot_a", "tier3", ["tier2", "tier1"]) is True
    assert fake.gateway_restart("team_bot_a") == {"ok": True, "method": "fake"}
    assert ("model_set", "team_bot_a", "tier3", ["tier2", "tier1"]) in fake.calls
    assert ("gateway_restart", "team_bot_a") in fake.calls
    # model_set updates seeded state so a later read reflects it
    assert fake.model_get("team_bot_a") == {"primary": "tier3", "fallback_order": ["tier2", "tier1"]}


def test_both_adapters_satisfy_the_protocol():
    assert isinstance(ar.OpenClawRuntime(), ar.AgentRuntime)
    assert isinstance(ar.FakeRuntime(), ar.AgentRuntime)


# ── Phase B seam extensions ──────────────────────────────────────────────────


def test_openclaw_security_audit_forwards_deep_and_err_out(monkeypatch):
    """Phase D: the security-audit knobs the old command() callers passed
    (deep / timeout / cache_ttl / _err_out) reach oc_security_audit."""
    seen = {}

    def fake_audit(bot_id, *, deep=False, timeout=60, cache_ttl=0, _err_out=None):
        seen.update(bot=bot_id, deep=deep, timeout=timeout, cache_ttl=cache_ttl, err=_err_out)
        return {"findings": []}

    monkeypatch.setattr("oc_cli.oc_security_audit", fake_audit, raising=False)
    err: list = []
    out = ar.OpenClawRuntime().security_audit("team_bot_a", deep=True, _err_out=err)
    assert out == {"findings": []}
    assert seen["bot"] == "team_bot_a"
    assert seen["deep"] is True
    # defaults match the heavyweight/always-fresh values every caller used
    assert seen["timeout"] == 60 and seen["cache_ttl"] == 0
    assert seen["err"] is err


def test_openclaw_cron_list_forwards_err_out(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "oc_cli.oc_cron_list",
        lambda bot_id, *, _err_out=None: seen.update(bot=bot_id, err=_err_out) or [],
        raising=False,
    )
    err: list = []
    ar.OpenClawRuntime().cron_list("team_bot_a", _err_out=err)
    assert seen == {"bot": "team_bot_a", "err": err}


def test_openclaw_doctor_fix_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "oc_cli.oc_doctor_fix",
        lambda bot_id, *, timeout=20: seen.update(bot=bot_id, timeout=timeout) or "ok\n",
        raising=False,
    )
    assert ar.OpenClawRuntime().doctor_fix("team_bot_a") == "ok\n"
    assert seen == {"bot": "team_bot_a", "timeout": 20}


def test_openclaw_set_identity_passes_agent_and_name(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "oc_cli.oc_set_identity",
        lambda bot_id, *, agent_id, name, timeout=20: seen.update(
            bot=bot_id, agent_id=agent_id, name=name, timeout=timeout
        ) or "ok\n",
        raising=False,
    )
    out = ar.OpenClawRuntime().set_identity("team_bot_a", agent_id="main", name="Renamed")
    assert out == "ok\n"
    assert seen == {"bot": "team_bot_a", "agent_id": "main", "name": "Renamed", "timeout": 20}


def test_openclaw_full_config_set_returns_dict_and_threads_network_path(monkeypatch):
    seen = {}

    def fake_set(bot_id, updates, network_path=None):
        seen["args"] = (bot_id, updates, network_path)
        return {"tiers": updates.get("tiers")}

    monkeypatch.setattr("oc_cli.oc_full_config_set", fake_set, raising=False)
    out = ar.OpenClawRuntime().full_config_set(
        "team_bot_a", {"tiers": {"tier1": "x"}}, network_path="/tmp/net.json"
    )
    assert out == {"tiers": {"tier1": "x"}}
    assert seen["args"] == ("team_bot_a", {"tiers": {"tier1": "x"}}, "/tmp/net.json")


def test_openclaw_full_config_set_with_error_delegates(monkeypatch):
    monkeypatch.setattr(
        "oc_cli.oc_full_config_set_with_error",
        lambda b, u, network_path=None: (None, "boom"),
        raising=False,
    )
    result, err = ar.OpenClawRuntime().full_config_set_with_error("team_bot_a", {"tiers": {}})
    assert result is None and err == "boom"


def test_network_path_forwarded_by_keyword_not_position(monkeypatch):
    """The seam must pass network_path as a KEYWORD to oc_cli. A caller (or test
    double) shaped ``def f(bot_id, updates, **kw)`` — the real provisioning/UI
    pattern — accepts network_path only as a keyword; a positional 3rd arg would
    raise ``takes 2 positional arguments but 3 were given``. Regression for the
    Phase B migration of routes_admin's user-tier-override endpoint."""
    seen = {}

    def stub(bot_id, updates, **kw):  # mirrors test_user_tier_override's double
        seen["positional"] = (bot_id, updates)
        seen["kw"] = kw
        return (updates, None)

    monkeypatch.setattr("oc_cli.oc_full_config_set_with_error", stub, raising=False)
    # network_path omitted by the caller → seam still forwards it, as a keyword
    ar.OpenClawRuntime().full_config_set_with_error("team_bot_a", {"tiers": {}})
    assert seen["positional"] == ("team_bot_a", {"tiers": {}})
    assert seen["kw"] == {"network_path": None}


def test_openclaw_cron_runs_passes_job_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "oc_cli.oc_cron_runs",
        lambda b, job_id, limit=10: seen.update(b=b, job_id=job_id, limit=limit) or [],
        raising=False,
    )
    ar.OpenClawRuntime().cron_runs("team_bot_a", "job-123", 5)
    assert seen == {"b": "team_bot_a", "job_id": "job-123", "limit": 5}


def test_openclaw_keys_get_delegates(monkeypatch):
    monkeypatch.setattr(
        "oc_cli.oc_keys_get",
        lambda b, network_path=None: {"anthropic": True, "openai": False},
        raising=False,
    )
    assert ar.OpenClawRuntime().keys_get("team_bot_a") == {"anthropic": True, "openai": False}


def test_openclaw_memory_set_passes_provider_and_fallback(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "oc_cli.oc_memory_set",
        lambda b, provider, fallback=None, network_path=None: seen.update(
            b=b, provider=provider, fallback=fallback
        ) or True,
        raising=False,
    )
    assert ar.OpenClawRuntime().memory_set("team_bot_a", "voyage", "openai") is True
    assert seen == {"b": "team_bot_a", "provider": "voyage", "fallback": "openai"}


def test_fake_runtime_extension_reads_and_records():
    fake = ar.FakeRuntime()
    fake.seed("team_bot_a", security_audit={"findings": []}, cron_list=[{"id": 1}],
              cron_runs=[{"id": 1}], keys={"anthropic": True},
              doctor_fix="ok", set_identity="ok")
    assert fake.security_audit("team_bot_a", deep=True) == {"findings": []}
    assert fake.cron_list("team_bot_a", _err_out=[]) == [{"id": 1}]
    assert fake.cron_runs("team_bot_a", "job-1") == [{"id": 1}]
    assert fake.keys_get("team_bot_a") == {"anthropic": True}
    # doctor_fix / set_identity are the Phase D typed methods that replaced the
    # raw command_raw() escape hatch — they record into calls like other mutators
    assert fake.doctor_fix("team_bot_a") == "ok"
    assert fake.set_identity("team_bot_a", agent_id="main", name="Renamed") == "ok"
    assert ("doctor_fix", "team_bot_a") in fake.calls
    assert ("set_identity", "team_bot_a", "main", "Renamed") in fake.calls
    # full_config_set returns the updates dict (not bool) and seeds it for reads
    assert fake.full_config_set("team_bot_a", {"tiers": {"t1": "x"}}) == {"tiers": {"t1": "x"}}
    assert fake.full_config_get("team_bot_a") == {"tiers": {"t1": "x"}}
    result, err = fake.full_config_set_with_error("team_bot_a", {"routing": "auto"})
    assert result == {"routing": "auto"} and err is None
    assert fake.memory_set("team_bot_a", "voyage", "openai") is True
    assert ("memory_set", "team_bot_a", "voyage", "openai") in fake.calls


def test_runtime_module_imports_without_openclaw(monkeypatch):
    """The seam must import even where the OpenClaw CLI is absent (the lazy
    import in OpenClawRuntime._oc is what buys the test-without-a-Mac payoff)."""
    # constructing the adapter must not require oc_cli; only calling a method does
    rt = ar.OpenClawRuntime()
    assert rt is not None
