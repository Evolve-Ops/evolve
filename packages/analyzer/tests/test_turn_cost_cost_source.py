"""``cost_source`` — preferring truth over the writer's recorded guess.

For UTC 2026-09-04 the PoC bot's turns log priced the day at $32.09 against a
provider console figure of $11.08
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §1). The plugin
had priced ``claude-opus-5`` by matching ``"opus"`` as a SUBSTRING, so a
current-generation id was charged the previous generation's rates; that number
went onto the turn record as ``cost``, and ``turn_cost`` took any non-zero
recorded cost as final.

Both halves are fixed here. The writer now stamps ``cost_source`` on every
record, and the reader prefers the catalog's price for the exact model id over
any recorded estimate — including a record written BEFORE the fix, which
carries no stamp at all. That is why no backfill script exists: history is
corrected on read.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import turn_cost  # noqa: E402


def _catalog(rows: list[tuple[str, str, float, float]]) -> dict:
    """A pricing catalog document. Rates are $/MTok in the arguments."""
    return {
        "refreshed_at": "2026-09-04T00:00:00Z",
        "models": [
            {
                "provider": provider,
                "model_id": model_id,
                "input_cost_per_token": inp / 1e6,
                "output_cost_per_token": out / 1e6,
                "cache_read_cost_per_token": None,
                "cache_write_cost_per_token": None,
            }
            for provider, model_id, inp, out in rows
        ],
    }


OPUS5 = _catalog([("anthropic", "claude-opus-5", 5.0, 25.0)])


def _turn(**over) -> dict:
    base = {
        "ts": "2026-09-04T10:00:00Z",
        "model": "claude-opus-5",
        "provider": "anthropic",
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_ambient_catalog(monkeypatch):
    """Never read the developer's own {shared_dir}/model-pricing.json."""
    monkeypatch.setattr(turn_cost, "load_pricing_catalog", lambda *_a, **_k: None)
    turn_cost.reset_pricing_catalog_cache()


def test_exact_id_prices_from_its_catalog_row():
    cost, resolution = turn_cost.turn_cost_detail(_turn(), catalog=OPUS5)
    assert resolution == "catalog"
    assert cost == pytest.approx(5.0)


def test_an_unknown_id_never_inherits_its_family_rate():
    """The whole defect in one assertion: an id the catalog does not know
    must not be charged what its FAMILY costs.

    The analyzer still has its provider-level offline row (B6: a known
    provider's unknown model must not read as free), so this resolves — but
    at the provider's mid-range rate, not at the previous generation's opus
    rate, and it says so: the rung reports as ``provider_table``, not as
    ``table``, because a rate published for some OTHER model of this provider
    is a guess and the receipt has to be able to name it as one (PR #4038
    review, F2). The plugin's own table has no provider rung at all and
    reports ``unpriced`` here; the two disagree deliberately, because only the
    analyzer's number is ever shown beside an unpriced-turn count.
    """
    catalog = _catalog([("anthropic", "claude-sonnet-4-6", 3.0, 15.0)])
    cost, resolution = turn_cost.turn_cost_detail(_turn(), catalog=catalog)
    assert resolution == "provider_table"
    assert resolution in turn_cost.GUESS_RESOLUTIONS
    assert cost != pytest.approx(15.0)   # NOT the opus family rate
    assert cost == pytest.approx(turn_cost.OFFLINE_PROVIDER_PRICING["anthropic"][0])


def test_an_exact_offline_row_is_not_reported_as_a_guess():
    """The other side of the split: the offline table's own row for this
    model id is a published price for the model that ran, so it stays
    ``table`` and is not counted as a guess."""
    turn = _turn(model="claude-sonnet-4-6")
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=None)
    assert resolution == "table"
    assert resolution not in turn_cost.GUESS_RESOLUTIONS
    assert cost == pytest.approx(3.0)


def test_a_provider_level_guess_is_counted_beside_the_total():
    """The total still includes the guess — B6 forbids a silent zero — but
    the count says how much of it is one, and for which provider."""
    turns = [
        _turn(model="claude-sonnet-4-6"),          # exact offline row
        _turn(),                                    # claude-opus-5: guess
        _turn(model="grok-9", provider="xai"),      # guess, other provider
    ]
    total = turn_cost.sum_turn_costs(turns, catalog=None)
    assert total.provider_guess_turns == 2
    assert total.guess_providers == ("anthropic", "xai")
    assert total.unpriced_turns == 0
    assert total.measurable          # every turn priced …
    assert not total.fully_published  # … but not every one from its own row
    note = turn_cost.provider_guess_note(
        total.provider_guess_turns, total.guess_providers,
    )
    assert note.startswith("2 turns priced at a provider-level guess")
    assert "anthropic, xai" in note


def test_no_guesses_reads_as_fully_published():
    total = turn_cost.sum_turn_costs(
        [_turn(model="claude-sonnet-4-6")], catalog=None,
    )
    assert total.fully_published
    assert total.provider_guess_turns == 0
    assert turn_cost.provider_guess_note(0) == ""


def test_an_unknown_provider_is_unpriced_never_zero():
    turn = _turn(model="mystery-model", provider="mystery-cloud")
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=None)
    assert resolution == "unpriced"
    assert cost is None


def test_a_pre_fix_record_is_repriced_from_the_catalog_on_read():
    """No cost_source at all — a record from before 2026-09-04, whose cost
    the family-substring estimator wrote at 3x."""
    pre_fix = _turn(cost=15.0)          # 1 MTok at the old opus rate
    assert "cost_source" not in pre_fix
    cost, resolution = turn_cost.turn_cost_detail(pre_fix, catalog=OPUS5)
    assert resolution == "catalog"
    assert cost == pytest.approx(5.0)
    assert turn_cost.turn_cost(pre_fix, catalog=OPUS5) == pytest.approx(5.0)


def test_a_table_stamped_record_is_repriced_too():
    cost, resolution = turn_cost.turn_cost_detail(
        _turn(cost=15.0, cost_source="table"), catalog=OPUS5,
    )
    assert resolution == "catalog"
    assert cost == pytest.approx(5.0)


def test_a_provider_recorded_cost_always_wins():
    """The gateway billed this. No estimate may overrule a real figure."""
    cost, resolution = turn_cost.turn_cost_detail(
        _turn(cost=4.13, cost_source="provider"), catalog=OPUS5,
    )
    assert resolution == "provider"
    assert cost == pytest.approx(4.13)


def test_a_catalog_stamped_record_is_taken_as_written():
    cost, resolution = turn_cost.turn_cost_detail(
        _turn(cost=5.0, cost_source="catalog"), catalog=OPUS5,
    )
    assert resolution == "catalog"
    assert cost == pytest.approx(5.0)


def test_an_unpriced_record_still_reaches_the_offline_table():
    """cost is None on the record; the reader still tries every rung."""
    turn = _turn(model="claude-sonnet-4-6", cost=None, cost_source="unpriced")
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=None)
    assert resolution == "table"
    assert cost == pytest.approx(3.0)


def test_a_recorded_cost_stands_when_nothing_can_reprice_it():
    turn = _turn(model="mystery-model", provider="mystery-cloud", cost=0.42)
    cost, resolution = turn_cost.turn_cost_detail(turn, catalog=None)
    assert resolution == "recorded"
    assert cost == pytest.approx(0.42)


def test_sum_counts_the_turns_it_repriced():
    turns = [
        _turn(cost=15.0),                       # pre-fix, corrected to 5.0
        _turn(cost=5.0, cost_source="catalog"),  # already right
        _turn(cost=4.13, cost_source="provider"),
    ]
    total = turn_cost.sum_turn_costs(turns, catalog=OPUS5)
    assert total.usd == pytest.approx(5.0 + 5.0 + 4.13)
    assert total.repriced_turns == 1
    assert total.unpriced_turns == 0


# ── The plugin's offline table is the same table ─────────────────────────────

_TS_TABLE = (
    Path(__file__).parent.parent.parent
    / "plugin" / "src" / "observer" / "ModelPricing.ts"
)
_TS_ROW = re.compile(
    r'"([a-z0-9._/-]+)":\s*\{\s*input:\s*([0-9.]+),\s*output:\s*([0-9.]+),'
    r'\s*cacheWrite:\s*([0-9.]+),\s*cacheRead:\s*([0-9.]+)\s*\}'
)


def test_plugin_offline_table_matches_python():
    """The plugin and the analyzer must fall back to the SAME table.

    They are two spellings of one contract: the hot path prices in
    TypeScript, every reader re-prices in Python, and a divergence would put
    a different number on the record than the reader computes for it. Adding
    a row to one side without the other fails here.
    """
    source = _TS_TABLE.read_text()
    block = source.split("EXACT_MODEL_COSTS", 1)[1].split("};", 1)[0]
    ts_table = {
        m.group(1): tuple(float(m.group(i)) for i in (2, 3, 4, 5))
        for m in _TS_ROW.finditer(block)
    }
    assert ts_table, "no rows parsed out of ModelPricing.ts — did it move?"
    assert ts_table == turn_cost.OFFLINE_MODEL_PRICING


def test_the_plugin_table_holds_no_family_fragments():
    """A key that is a prefix of another key's model segment would let a
    substring match back in through the front door."""
    for key in turn_cost.OFFLINE_MODEL_PRICING:
        provider, _, model = key.partition("/")
        assert provider and model, f"{key} is not a qualified id"
        assert model not in ("opus", "sonnet", "haiku", "gemini", "grok", "gpt")


def test_catalog_cache_is_keyed_on_mtime(tmp_path, monkeypatch):
    """A catalog refreshed under a long-lived daemon is picked up on the next
    read, not at the next restart."""
    monkeypatch.undo()   # restore the real load_pricing_catalog
    turn_cost.reset_pricing_catalog_cache()
    path = tmp_path / "model-pricing.json"
    path.write_text(json.dumps(OPUS5))
    first = turn_cost.load_pricing_catalog(tmp_path)
    assert first is not None
    import os
    path.write_text(json.dumps(_catalog([("anthropic", "claude-opus-5", 7.0, 25.0)])))
    os.utime(path, (path.stat().st_atime + 60, path.stat().st_mtime + 60))
    again = turn_cost.load_pricing_catalog(tmp_path)
    assert again is not None
    assert again["models"][0]["input_cost_per_token"] == pytest.approx(7 / 1e6)
