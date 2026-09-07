"""cost_reconcile — our estimate against the provider's actual bill.

Nothing in the pod had ever compared the two, which is how a
family-substring price table overstated the power model ~3x for months:
$32.09 estimated against $11.08 billed for UTC 2026-09-04
(``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §1). These tests
pin the comparison, the drift band, and the two states the weekly receipt has
to render — reconciled, and not reconciled.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_reconcile as cr  # noqa: E402
import turn_cost  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

CONSOLE_CSV = """date,model,cost
2026-09-04,claude-opus-5,10.08
2026-09-04,claude-haiku-4-6,1.00
2026-09-03,claude-opus-5,8.16
Total,,19.24
"""


def _catalog_file(shared_dir: Path, refreshed_at: str = "2026-09-04T00:00:00Z") -> None:
    (shared_dir / "model-pricing.json").write_text(json.dumps({
        "refreshed_at": refreshed_at,
        "models": [{
            "provider": "anthropic",
            "model_id": "claude-opus-5",
            "input_cost_per_token": 5.0 / 1e6,
            "output_cost_per_token": 25.0 / 1e6,
            "cache_read_cost_per_token": 0.5 / 1e6,
            "cache_write_cost_per_token": 6.25 / 1e6,
        }],
    }))
    turn_cost.reset_pricing_catalog_cache()


def _turn(ts: str, *, input_tokens: int, cost=None, cost_source=None,
          model: str = "claude-opus-5", provider: str = "anthropic") -> dict:
    turn = {
        "ts": ts,
        "model": model,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    if cost is not None:
        turn["cost"] = cost
    if cost_source is not None:
        turn["cost_source"] = cost_source
    return turn


@pytest.fixture(autouse=True)
def _clean_catalog_memo():
    turn_cost.reset_pricing_catalog_cache()
    yield
    turn_cost.reset_pricing_catalog_cache()


# ── The console export ────────────────────────────────────────────────────────


def test_console_csv_sums_per_day_and_skips_junk_rows(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text(CONSOLE_CSV)
    assert cr.parse_console_csv(path) == {
        "2026-09-04": 11.08,
        "2026-09-03": 8.16,
    }


def test_console_csv_tolerates_other_header_spellings(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text(
        "Usage Date,Line Item,Cost (USD)\n2026-09-04,claude-opus-5,\"$1,000.50\"\n"
    )
    assert cr.parse_console_csv(path) == {"2026-09-04": 1000.50}


def test_a_file_with_no_usable_row_is_refused_not_read_as_zero(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("alpha,beta\n1,2\n")
    with pytest.raises(cr.ConsoleCsvError):
        cr.parse_console_csv(path)


# ── The comparison ────────────────────────────────────────────────────────────


# The finding's day, reconstructed. Ten power-model turns of this size were
# what the substring estimator charged ~$3.209 each for: it priced
# claude-opus-5 at the PREVIOUS generation's $15/MTok input rate. The
# catalog's own row for the id says $5/MTok.
_FIXTURE_INPUT_TOKENS = 213_933
_OLD_FAMILY_RATE_PER_MTOK = 15.0


def test_the_fixture_day_lands_within_ten_percent_of_the_bill(tmp_path):
    """The finding's day, end to end: the substring estimator read $32.09;
    priced from the catalog by exact id the same turns land within 10% of
    the $11.08 the provider console actually billed."""
    _catalog_file(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text(CONSOLE_CSV)
    substring_cost = round(
        _FIXTURE_INPUT_TOKENS * _OLD_FAMILY_RATE_PER_MTOK / 1e6, 6,
    )
    assert substring_cost == pytest.approx(3.209, abs=1e-3)
    turns = [
        _turn("2026-09-04T0%d:00:00Z" % i,
              input_tokens=_FIXTURE_INPUT_TOKENS, cost=substring_cost)
        for i in range(10)
    ]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, turns=turns, now=NOW,
    )
    # No --provider was passed: the export's model column named claude-opus-5,
    # and the catalog places that id under exactly one provider.
    assert result.provider == "anthropic"
    by_day = {d.date_iso: d for d in result.days}
    sep4 = by_day["2026-09-04"]
    # Every recorded $3.209 was superseded by the catalog's own rate.
    assert sep4.repriced_turns == 10
    assert sep4.console_usd == pytest.approx(11.08)
    assert sep4.ratio is not None
    assert 0.9 <= sep4.ratio <= 1.1, (
        f"estimate ${sep4.estimate_usd:.2f} vs bill ${sep4.console_usd:.2f}"
    )
    assert not cr.ratio_out_of_band(sep4.ratio)
    # Before: what the same turns' recorded costs sum to. After: what the
    # catalog prices them at. $32.09 -> $10.70 against a bill of $11.08.
    assert sum(t["cost"] for t in turns) == pytest.approx(32.09, abs=0.01)
    assert sep4.estimate_usd == pytest.approx(10.70, abs=0.01)


def test_a_three_times_estimate_is_drift(tmp_path):
    """The pre-fix state: no catalog, and a recorded cost nothing can
    supersede. The ratio must land outside the band."""
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,11.08\n")
    turns = [
        _turn("2026-09-04T0%d:00:00Z" % i, input_tokens=0, cost=3.209,
              cost_source="provider")
        for i in range(10)
    ]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=turns, now=NOW,
    )
    day = result.days[0]
    assert day.estimate_usd == pytest.approx(32.09)
    assert day.ratio == pytest.approx(2.8962, abs=1e-3)
    assert cr.ratio_out_of_band(day.ratio)
    assert day.delta_usd == pytest.approx(21.01)


def test_unpriced_turns_are_counted_not_summed_as_zero(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,5.00\n")
    turns = [{
        "ts": "2026-09-04T01:00:00Z",
        "model": "mystery-model", "provider": "mystery-cloud",
        "input_tokens": 1000, "output_tokens": 0,
    }]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="mystery-cloud",
        turns=turns, now=NOW,
    )
    day = result.days[0]
    assert day.unpriced_turns == 1
    assert not day.measurable
    assert day.estimate_usd == 0.0    # a floor, and the count says so


def test_turns_outside_the_exported_days_are_not_counted(tmp_path):
    _catalog_file(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,5.00\n")
    turns = [
        _turn("2026-09-04T23:59:59Z", input_tokens=1_000_000),
        _turn("2026-09-05T00:00:01Z", input_tokens=1_000_000),
    ]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=turns, now=NOW,
    )
    assert len(result.days) == 1
    assert result.days[0].estimate_usd == pytest.approx(5.0)


def test_a_day_with_no_turns_read_is_not_drift(tmp_path):
    """A $0.00 estimate from an empty read is "did not measure". Reporting it
    as a ratio of 0.00 would raise drift on the absence of data."""
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,11.08\n")
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=NOW,
    )
    day = result.days[0]
    assert day.turns_seen == 0
    assert day.estimate_usd == 0.0
    assert day.ratio is None
    assert result.ratio is None
    assert not cr.ratio_out_of_band(day.ratio)
    cr.write_reconcile(tmp_path, result)
    line = cr.receipt_lines(tmp_path, now=NOW)[0]
    assert "nothing to compare" in line and "no turns read" in line


def test_a_day_the_console_did_not_bill_has_no_ratio(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,0.00\n")
    turns = [_turn("2026-09-04T01:00:00Z", input_tokens=0, cost=0.0)]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=turns, now=NOW,
    )
    assert result.days[0].turns_seen == 1
    assert result.days[0].ratio is None
    assert not cr.ratio_out_of_band(None)


# ── One provider's bill, one provider's estimate ─────────────────────────────


def _mixed_catalog(shared_dir: Path) -> None:
    """A catalog that prices both an anthropic and an xai model."""
    (shared_dir / "model-pricing.json").write_text(json.dumps({
        "refreshed_at": "2026-09-04T00:00:00Z",
        "models": [
            {"provider": "anthropic", "model_id": "claude-opus-5",
             "input_cost_per_token": 5.0 / 1e6,
             "output_cost_per_token": 25.0 / 1e6},
            {"provider": "xai", "model_id": "grok-4",
             "input_cost_per_token": 3.0 / 1e6,
             "output_cost_per_token": 15.0 / 1e6},
        ],
    }))
    turn_cost.reset_pricing_catalog_cache()


def test_another_providers_turns_do_not_inflate_the_estimate(tmp_path):
    """The defect F1 names, in one comparison.

    The PoC bot ran 166 xAI requests in the same week it ran the power model.
    An anthropic-only console export against an all-provider estimate makes
    the ratio move with the MODEL MIX: here the anthropic side alone matches
    the bill exactly, and folding the grok turn in would read as 1.6x drift
    and send a warning about prices that are right.
    """
    _mixed_catalog(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,5.00\n")
    turns = [
        _turn("2026-09-04T01:00:00Z", input_tokens=1_000_000),
        _turn("2026-09-04T02:00:00Z", input_tokens=1_000_000,
              model="grok-4", provider="xai"),
    ]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, turns=turns, now=NOW,
    )
    day = result.days[0]
    assert result.provider == "anthropic"          # inferred from the CSV
    assert day.estimate_usd == pytest.approx(5.00)  # anthropic only
    assert day.ratio == pytest.approx(1.0)
    assert not cr.ratio_out_of_band(day.ratio)
    # The excluded side is reported, not silently dropped.
    assert day.excluded_turns == 1
    assert day.excluded_usd == pytest.approx(3.00)
    assert day.priced_turns == 1
    # Unscoped, this same day would have read 8/5 = 1.6 and fired drift.
    unscoped = cr.estimate_by_utc_day(
        ["2026-09-04"], provider="xai", shared_dir=tmp_path, turns=turns,
    )["2026-09-04"]
    assert unscoped.usd == pytest.approx(3.00)
    assert unscoped.excluded == 1


def test_the_excluded_turns_reach_the_receipt(tmp_path):
    _mixed_catalog(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,5.00\n")
    turns = [
        _turn("2026-09-04T01:00:00Z", input_tokens=1_000_000),
        _turn("2026-09-04T02:00:00Z", input_tokens=1_000_000,
              model="grok-4", provider="xai"),
    ]
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=path, shared_dir=tmp_path, turns=turns, now=NOW,
    ))
    lines = cr.receipt_lines(tmp_path, now=NOW)
    assert "anthropic" in lines[0]
    assert any("1 turn on other providers (~$3.00)" in ln for ln in lines)


def test_a_provider_level_guess_is_named_on_the_receipt(tmp_path):
    """A model the catalog cannot price is still costed — from the provider's
    mid-range rate. The receipt has to say that is what happened."""
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,3.00\n")
    turns = [_turn("2026-09-04T01:00:00Z", input_tokens=1_000_000)]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=turns, now=NOW,
    )
    day = result.days[0]
    assert day.provider_guess_turns == 1
    assert day.priced_turns == 1
    cr.write_reconcile(tmp_path, result)
    lines = cr.receipt_lines(tmp_path, now=NOW)
    assert any("provider-level guess" in ln for ln in lines)


def test_an_explicit_provider_beats_the_inference(tmp_path):
    _mixed_catalog(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,3.00\n")
    turns = [_turn("2026-09-04T01:00:00Z", input_tokens=1_000_000,
                   model="grok-4", provider="xai")]
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="xai",
        turns=turns, now=NOW,
    )
    assert result.provider == "xai"
    assert result.days[0].estimate_usd == pytest.approx(3.00)
    assert result.days[0].excluded_turns == 0


def test_an_ambiguous_model_column_is_refused_not_guessed(tmp_path):
    """Two providers publishing the same id is exactly when a guess would
    silently pick which bill we are checking against."""
    (tmp_path / "model-pricing.json").write_text(json.dumps({
        "refreshed_at": "2026-09-04T00:00:00Z",
        "models": [
            {"provider": "anthropic", "model_id": "claude-opus-5",
             "input_cost_per_token": 5.0 / 1e6,
             "output_cost_per_token": 25.0 / 1e6},
            {"provider": "bedrock", "model_id": "claude-opus-5",
             "input_cost_per_token": 6.0 / 1e6,
             "output_cost_per_token": 30.0 / 1e6},
        ],
    }))
    turn_cost.reset_pricing_catalog_cache()
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,3.00\n")
    with pytest.raises(cr.ProviderScopeError):
        cr.reconcile(console_csv=path, shared_dir=tmp_path, turns=[], now=NOW)


def test_an_export_with_no_model_column_requires_the_provider(tmp_path):
    _mixed_catalog(tmp_path)
    path = tmp_path / "costs.csv"
    path.write_text("date,cost\n2026-09-04,3.00\n")
    assert cr.console_models(path) == []
    with pytest.raises(cr.ProviderScopeError) as exc:
        cr.reconcile(console_csv=path, shared_dir=tmp_path, turns=[], now=NOW)
    assert "--provider" in str(exc.value)
    # …and naming it is all that was missing.
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=NOW,
    )
    assert result.provider == "anthropic"


def test_a_pod_with_no_catalog_cannot_infer_and_says_so(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,claude-opus-5,3.00\n")
    assert cr.infer_provider(["claude-opus-5"], None) is None
    with pytest.raises(cr.ProviderScopeError):
        cr.reconcile(console_csv=path, shared_dir=tmp_path, turns=[], now=NOW)


# ── Persistence + the receipt ────────────────────────────────────────────────


def test_write_then_read_round_trips_one_file_per_day(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text(CONSOLE_CSV)
    result = cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=NOW,
    )
    written = cr.write_reconcile(tmp_path, result)
    assert {p.name for p in written} == {
        "reconcile-2026-09-03.json", "reconcile-2026-09-04.json",
    }
    doc = cr.read_reconcile(tmp_path, "2026-09-04")
    assert doc["console_usd"] == pytest.approx(11.08)
    assert [d["date"] for d in doc["days"]] == ["2026-09-04"]
    latest = cr.latest_reconcile(tmp_path, today=NOW.date())
    assert latest["days"][0]["date"] == "2026-09-04"


def test_receipt_says_so_when_nothing_reconciled_this_week(tmp_path):
    _catalog_file(tmp_path, refreshed_at="2026-09-05T00:00:00Z")
    lines = cr.receipt_lines(tmp_path, now=NOW)
    assert lines[0] == "estimate vs provider bill: not reconciled this week"
    assert not any("catalog" in line for line in lines[1:])


def test_receipt_renders_the_comparison_when_one_exists(tmp_path):
    _catalog_file(tmp_path, refreshed_at="2026-09-05T00:00:00Z")
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,11.08\n")
    turns = [_turn("2026-09-04T01:00:00Z", input_tokens=0, cost=32.09,
                   cost_source="provider")]
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=turns, now=NOW,
    ))
    line = cr.receipt_lines(tmp_path, now=NOW)[0]
    assert line.startswith("estimate vs provider bill: $32.09 vs $11.08")
    assert "ratio 2.90" in line


def test_a_stale_pricing_catalog_is_called_out(tmp_path):
    _catalog_file(tmp_path, refreshed_at="2026-08-01T00:00:00Z")
    assert cr.catalog_age_days(tmp_path, now=NOW) == pytest.approx(35.5, abs=0.1)
    lines = cr.receipt_lines(tmp_path, now=NOW)
    assert any("pricing catalog: 35 days old" in line for line in lines)


def test_a_pod_with_no_catalog_says_so_rather_than_nothing(tmp_path):
    assert cr.catalog_age_days(tmp_path, now=NOW) is None
    lines = cr.receipt_lines(tmp_path, now=NOW)
    assert lines[-1] == "pricing catalog: none mirrored on this pod"


def test_a_reconcile_RUN_this_week_counts_even_for_an_older_day(tmp_path):
    """The window is on when the reconcile was RUN, not on the day it read.

    An operator who downloads Monday's export on Wednesday has reconciled
    this week. Walking the filenames — the original shape — called that "not
    reconciled this week" and made the receipt say nothing had been checked
    on the very week it was (PR #4038 review, F4).
    """
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-08-01,x,5.00\n")
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=NOW,
    ))
    doc = cr.latest_reconcile(tmp_path, today=NOW.date())
    assert doc is not None
    assert doc["days"][0]["date"] == "2026-08-01"
    assert not cr.receipt_lines(tmp_path, now=NOW)[0].endswith(
        "not reconciled this week"
    )


def test_a_reconcile_RUN_before_the_window_is_not_this_week(tmp_path):
    """The other direction: a run from last month, whatever day it read."""
    path = tmp_path / "costs.csv"
    path.write_text("date,model,cost\n2026-09-04,x,5.00\n")
    old_run = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=old_run,
    ))
    assert cr.latest_reconcile(tmp_path, today=NOW.date()) is None
    assert cr.receipt_lines(tmp_path, now=NOW)[0].endswith("not reconciled this week")


def test_the_newest_RUN_wins_not_the_newest_day(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("date,model,cost\n2026-09-04,x,5.00\n")
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=path, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    newer = tmp_path / "new.csv"
    newer.write_text("date,model,cost\n2026-08-30,x,7.00\n")
    cr.write_reconcile(tmp_path, cr.reconcile(
        console_csv=newer, shared_dir=tmp_path, provider="anthropic",
        turns=[], now=NOW,
    ))
    doc = cr.latest_reconcile(tmp_path, today=NOW.date())
    assert doc["days"][0]["date"] == "2026-08-30"
    assert doc["console_usd"] == pytest.approx(7.00)


def test_a_document_with_no_generated_at_falls_back_to_its_day(tmp_path):
    """A hand-edited or older file still places itself, conservatively."""
    out = tmp_path / "cost"
    out.mkdir()
    (out / "reconcile-2026-09-04.json").write_text(json.dumps({
        "schema_version": 1, "estimate_usd": 1.0, "console_usd": 1.0,
        "ratio": 1.0, "days": [{"date": "2026-09-04", "turns_seen": 1}],
    }))
    assert cr.latest_reconcile(tmp_path, today=NOW.date()) is not None
    (out / "reconcile-2026-09-04.json").write_text(json.dumps({
        "schema_version": 1, "estimate_usd": 1.0, "console_usd": 1.0,
        "ratio": 1.0, "days": [{"date": "2026-06-01", "turns_seen": 1}],
    }))
    # The FILENAME day is what the fallback reads, and it is still in window.
    assert cr.latest_reconcile(tmp_path, today=NOW.date()) is not None
