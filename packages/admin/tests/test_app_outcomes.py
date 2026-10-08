"""tests/test_app_outcomes.py — the outcome view's reader (design §3.2).

The single reader ``get_app_outcomes`` wraps ``outcome_by_app.load_
outcome_by_app`` the way ``pod_state.app_usage`` wraps ``usage_by_app``
(test_pod_state_app_usage.py's own pattern): pre-write the rollup JSON a
real run would have produced, then assert what the reader does with it.
The one new rule this reader owns: an app_id no bot's Tracker has ever
produced a row for reads ``measured: False`` — never a fabricated zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.app_outcomes import get_app_outcomes  # noqa: E402

BOT_A = "bot-a"
BOT_B = "bot-b"


def _metric(count: int, ids: "list[str] | None" = None) -> dict:
    return {"count": count, "card_ids": ids or [], "truncated": False}


def _zero_metrics() -> dict:
    from outcome_by_app import METRICS
    return {m: _metric(0) for m in METRICS}


def _entry(**overrides) -> dict:
    windows = {"d1": _zero_metrics(), "d7": _zero_metrics(), "d30": _zero_metrics()}
    for key, metrics in (overrides.pop("windows", None) or {}).items():
        windows[key].update(metrics)
    return {
        "windows": windows,
        "users": overrides.pop("users", {}),
        "daily": overrides.pop("daily", {}),
        "backlog": overrides.pop("backlog", {
            "open_cards": 0, "backlog_age_p50_days": None,
            "backlog_age_max_days": None, "blocked_over_3_days": 0,
        }),
    }


def _write(shared: Path, bot: str, apps: dict) -> None:
    (shared / bot).mkdir(parents=True, exist_ok=True)
    (shared / bot / "outcome-by-app.json").write_text(json.dumps({
        "schema_version": 1, "bot_id": bot, "apps": apps,
    }))


# ── Tri-state honesty ────────────────────────────────────────────────────────

def test_app_with_no_rows_anywhere_reads_not_measured(tmp_path: Path):
    _write(tmp_path, BOT_A, {"assistant": _entry()})
    out = get_app_outcomes(tmp_path, "project-manager", [BOT_A], period="d7")
    assert out == {"app_id": "project-manager", "period": "d7", "measured": False}


def test_bot_with_no_rollup_at_all_is_skipped_not_an_error(tmp_path: Path):
    out = get_app_outcomes(tmp_path, "assistant", ["never-rolled-up"], period="d7")
    assert out["measured"] is False


# ── Merge across bots ────────────────────────────────────────────────────────

def test_merges_two_bots_windows_and_users(tmp_path: Path):
    _write(tmp_path, BOT_A, {"assistant": _entry(
        windows={"d7": {"moved_by_bot": _metric(2, ["p1", "p2"])}},
        users={"user": {"d7": {**_zero_metrics(), "moved_by_bot": _metric(2, ["p1", "p2"])}}},
        backlog={"open_cards": 3, "backlog_age_p50_days": 10.0,
                 "backlog_age_max_days": 20.0, "blocked_over_3_days": 1},
    )})
    _write(tmp_path, BOT_B, {"assistant": _entry(
        windows={"d7": {"moved_by_bot": _metric(1, ["p9"])}},
        users={"user": {"d7": {**_zero_metrics(), "moved_by_bot": _metric(1, ["p9"])}}},
        backlog={"open_cards": 1, "backlog_age_p50_days": 5.0,
                 "backlog_age_max_days": 5.0, "blocked_over_3_days": 0},
    )})

    out = get_app_outcomes(tmp_path, "assistant", [BOT_A, BOT_B], period="d7")

    assert out["measured"] is True
    assert out["bots"] == [BOT_A, BOT_B]
    assert out["total"]["moved_by_bot"]["count"] == 3
    assert out["total"]["moved_by_bot"]["card_ids"] == ["p1", "p2", "p9"]
    assert out["users"]["user"]["moved_by_bot"]["count"] == 3
    assert out["backlog"]["open_cards"] == 4
    assert out["backlog"]["blocked_over_3_days"] == 1
    assert out["backlog"]["backlog_age_max_days"] == 20.0


def test_a_bot_without_this_app_id_does_not_poison_the_merge(tmp_path: Path):
    """bot-b has a Tracker but never a project-shape list — its rollup
    simply omits 'project-manager', and the merge must not treat that as
    zero rows for bot-b (it is not a row at all)."""
    _write(tmp_path, BOT_A, {"project-manager": _entry(
        windows={"d7": {"resolved_by_bot": _metric(1, ["o1"])}},
    )})
    _write(tmp_path, BOT_B, {"assistant": _entry()})

    out = get_app_outcomes(tmp_path, "project-manager", [BOT_A, BOT_B], period="d7")
    assert out["measured"] is True
    assert out["bots"] == [BOT_A]
    assert out["total"]["resolved_by_bot"]["count"] == 1


def test_period_must_be_a_known_window(tmp_path: Path):
    with pytest.raises(ValueError):
        get_app_outcomes(tmp_path, "assistant", [BOT_A], period="d14")
