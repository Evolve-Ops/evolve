"""``evolve-admin pricing warm`` and the health control "fast-rung price known".

The rule these pin (D-CS2): a model the price source cannot price stays ABSENT
from the cache — never 0 — and every surface that meets the gap NAMES the bot
and model (D-CS7). The fetcher is injected; CI makes no live call.
"""
from __future__ import annotations

import json
from pathlib import Path

import model_pricing as mp
import primary_bot
import pytest

from evolve_admin import health, pricing_warm

_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "controls" / "health__check_fast_rung_price"
CHAINS = {"team_bot_a": {"fast": ["acme/fast-1"], "power": ["acme/big-1"]},
          "team_bot_b": {"fast": ["acme/ghost-9"]}}


@pytest.fixture(autouse=True)
def _chains(monkeypatch):
    monkeypatch.setattr(primary_bot, "bot_tier_models",
                        lambda network, bot, tier: list(CHAINS.get(bot, {}).get(tier, [])))


def _litellm(**models: tuple[float, float]) -> dict:
    return {f"acme/{m}": {"litellm_provider": "acme", "input_cost_per_token": i,
                          "output_cost_per_token": o} for m, (i, o) in models.items()}


def _fetcher(litellm: dict | None):
    def get(url: str) -> dict:
        if litellm is None or "models.dev" in url:
            raise OSError("connection refused")
        return litellm
    return get


def test_unpriced_model_stays_absent_is_named_and_exits_zero(tmp_path: Path) -> None:
    lines, code = pricing_warm.warm(tmp_path, {}, ["team_bot_a", "team_bot_b"],
                                    fetcher=_fetcher(_litellm(**{"fast-1": (1e-6, 5e-6), "big-1": (3e-6, 9e-6)})))
    assert code == 0
    assert "acme/fast-1: filled" in lines and "acme/big-1: filled" in lines
    (ghost,) = [ln for ln in lines if ln.startswith("acme/ghost-9")]
    assert "could not fetch" in ghost and "no input+output price" in ghost
    cache = mp.read_pricing_cache(tmp_path)
    assert mp.lookup_price(cache, "acme", "ghost-9") is None  # absent — never a 0
    again, _ = pricing_warm.warm(tmp_path, {}, ["team_bot_a"],
                                 fetcher=_fetcher(_litellm(**{"fast-1": (1e-6, 5e-6), "big-1": (3e-6, 9e-6)})))
    assert again == ["acme/fast-1: unchanged", "acme/big-1: unchanged"]


def test_unreachable_source_exits_nonzero_and_leaves_the_cache(tmp_path: Path) -> None:
    mp.write_pricing_cache(tmp_path, {"models": [{"provider": "acme", "model_id": "fast-1",
                                                  "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6}]})
    before = (tmp_path / mp.PRICING_CACHE_NAME).read_text()
    lines, code = pricing_warm.warm(tmp_path, {}, ["team_bot_a"], fetcher=_fetcher(None))
    assert code == 1
    assert lines[-1].startswith("could not reach the price source")
    assert (tmp_path / mp.PRICING_CACHE_NAME).read_text() == before


def test_deploy_hook_never_raises(tmp_path: Path, monkeypatch) -> None:
    # A fixture network.json WITH sharedDir, and the fetch stubbed: a missing
    # network.json falls back to the real {shared_dir}, and the live fetcher
    # writes there whenever one source answers (the CI red on #4471).
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(shared), "members": ["team_bot_a"]}))
    real_fetch = mp.fetch_pricing_catalog
    monkeypatch.setattr(mp, "fetch_pricing_catalog", lambda **kw: real_fetch(
        refreshed_at=kw["refreshed_at"], fetcher=_fetcher(_litellm(**{"fast-1": (1e-6, 5e-6)}))))
    said: list[str] = []
    pricing_warm.warm_best_effort(net, said.append)
    assert said and all(s.startswith("  pricing:") for s in said)
    assert "  pricing: acme/fast-1: filled" in said
    assert (shared / mp.PRICING_CACHE_NAME).exists()

    def boom(**_kw):
        raise RuntimeError("source exploded")
    monkeypatch.setattr(mp, "fetch_pricing_catalog", boom)
    said.clear()
    pricing_warm.warm_best_effort(net, said.append)
    assert said == ["  pricing: warm skipped (source exploded) — deploy continues"]


def _check(tmp_path: Path, members: list[str]) -> health.CheckResult:
    report = health.HealthReport()
    health._check_fast_rung_price(report, {}, members, tmp_path)
    (only,) = [c for c in report.checks if c.name == "fast_rung_price"]
    return only


def _replay(tmp_path: Path, name: str, monkeypatch) -> tuple[health.CheckResult, dict]:
    fx = json.loads((_FIXTURES / name).read_text())
    monkeypatch.setitem(CHAINS, fx["bot_id"], {"fast": [fx["fast_model"]]})
    mp.write_pricing_cache(tmp_path, {"models": fx["cache_models"]})
    return _check(tmp_path, [fx["bot_id"]]), fx["expect"]


def test_known_good_fixture_passes(tmp_path: Path, monkeypatch) -> None:
    c, want = _replay(tmp_path, "known_good.json", monkeypatch)
    assert (c.status, c.category) == (want["status"], want["category"])
    assert want["detail_contains"] in c.detail


def test_known_bad_fixture_names_bot_and_model(tmp_path: Path, monkeypatch) -> None:
    c, want = _replay(tmp_path, "known_bad.json", monkeypatch)
    assert (c.status, c.category) == (want["status"], want["category"])
    assert want["detail_contains"] in c.detail
    assert c.fix_cmd == "sudo evolve-admin pricing warm"


def test_health_before_and_after_warm(tmp_path: Path) -> None:
    members = ["team_bot_a", "team_bot_b"]
    cold = _check(tmp_path, members)
    assert cold.status == health.WARN
    assert "team_bot_a (acme/fast-1)" in cold.detail and "team_bot_b (acme/ghost-9)" in cold.detail
    pricing_warm.warm(tmp_path, {}, members, fetcher=_fetcher(_litellm(**{"fast-1": (1e-6, 5e-6)})))
    warm = _check(tmp_path, members)
    assert "team_bot_a" not in warm.detail and "team_bot_b (acme/ghost-9)" in warm.detail
