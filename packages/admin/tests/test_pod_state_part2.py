"""tests/test_pod_state_part2.py — Bundle 3-part-2 pod_state functions.

Covers:
  - spend_rollup: per-bot + pod-wide; window cap; missing-rollup handling.
  - recent_turns: opt-out detection vs. empty buffer; limit cap.
  - describe_bot: composition; not-found case; integrations + apps counts.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _write_cost_rollup(shared_dir: Path, *, bot_id: str, day_iso: str,
                      total_usd: float, input_tokens: int = 100,
                      output_tokens: int = 50,
                      by_model: dict | None = None) -> Path:
    """Write one daily cost-rollup file in the shape cost_rollup.py
    emits — see analyzer/cost_rollup.py:27 for the schema."""
    d = shared_dir / "metrics" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "total_usd": total_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "by_model": by_model or {},
    }
    path = d / f"cost-{day_iso}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _yesterday() -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=1)).isoformat()


def _days_ago(n: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def _write_network(shared_dir: Path, bots: dict) -> None:
    (shared_dir / "network.json").write_text(
        json.dumps({"sharedDir": str(shared_dir), "bots": bots}),
        encoding="utf-8",
    )


# ── spend_rollup ────────────────────────────────────────────────────────────


class TestSpendRollup:
    def test_per_bot_with_no_data_returns_zeros(self, shared_dir):
        from evolve_admin.pod_state import spend_rollup
        result = spend_rollup(shared_dir, bot_id="team_bot_a", window="7d")
        assert result["total_usd"] == 0.0
        assert result["input_tokens"] == 0
        assert result["bot_id"] == "team_bot_a"
        assert result["days"] == 7

    def test_per_bot_sums_window(self, shared_dir):
        from evolve_admin.pod_state import spend_rollup

        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_today(),
                           total_usd=1.50, input_tokens=200)
        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_yesterday(),
                           total_usd=2.25, input_tokens=300)
        # 10 days ago — outside a 7-day window.
        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_days_ago(10),
                           total_usd=99.99)

        result = spend_rollup(shared_dir, bot_id="team_bot_a", window="7d")
        assert result["total_usd"] == pytest.approx(3.75)
        assert result["input_tokens"] == 500
        # 10-days-ago row excluded.
        assert result["total_usd"] < 99.99

    def test_window_falls_back_to_default_on_invalid(self, shared_dir):
        """Bot can pass garbage; we coerce to '7d' rather than raising.
        The filters dict echoes the requested value so the LLM can
        detect coercion (silent broadening was the failure mode caught
        in Bundle 3-part-1's list_signals review)."""
        from evolve_admin.pod_state import spend_rollup
        result = spend_rollup(shared_dir, bot_id="team_bot_a", window="garbage")
        assert result["window"] == "7d"
        # Coercion is visible via filters.
        assert result["filters"]["window"] == "7d"
        assert result["filters"]["requested_window"] == "garbage"

    def test_filters_echo_for_default_call(self, shared_dir):
        """Even when no coercion happens, filters reflects what was
        applied — same shape every call."""
        from evolve_admin.pod_state import spend_rollup
        result = spend_rollup(shared_dir, bot_id="team_bot_a", window="7d")
        assert result["filters"]["window"] == "7d"
        assert result["filters"]["bot_id"] == "team_bot_a"

    def test_top_models_returns_sorted_by_cost(self, shared_dir):
        from evolve_admin.pod_state import spend_rollup

        _write_cost_rollup(
            shared_dir, bot_id="team_bot_a", day_iso=_today(),
            total_usd=10.0, input_tokens=1000, output_tokens=500,
            by_model={
                "claude-haiku": {"cost_usd": 0.50, "input_tokens": 1000,
                                 "output_tokens": 500},
                "claude-opus": {"cost_usd": 8.00, "input_tokens": 2000,
                                "output_tokens": 1000},
                "claude-sonnet": {"cost_usd": 1.50, "input_tokens": 1500,
                                  "output_tokens": 750},
            },
        )
        result = spend_rollup(shared_dir, bot_id="team_bot_a", window="7d")
        models = [m["model"] for m in result["by_model_top"]]
        assert models[0] == "claude-opus"   # highest cost first
        # Top-3 only.
        assert len(result["by_model_top"]) <= 3

    def test_pod_wide_sums_across_bots(self, shared_dir):
        from evolve_admin.pod_state import spend_rollup

        _write_network(shared_dir, {
            "team_bot_a": {"role": "member"},
            "admin_bot": {"role": "member"},
        })
        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_today(),
                           total_usd=2.00)
        _write_cost_rollup(shared_dir, bot_id="admin_bot", day_iso=_today(),
                           total_usd=3.00)

        result = spend_rollup(shared_dir, window="7d")
        assert result["bot_id"] is None
        assert result["total_usd"] == pytest.approx(5.00)
        # by_bot only includes bots with non-zero spend.
        assert set(result["by_bot"].keys()) == {"team_bot_a", "admin_bot"}

    def test_pod_wide_excludes_zero_bots_from_by_bot(self, shared_dir):
        """Two paths to "no spend": no rollup file at all, and a rollup
        file present but totaling 0.00. Both must exclude the bot
        from by_bot. The first version only tested the missing-file
        path; second-pass review of Bundle 3-part-2 flagged the gap."""
        from evolve_admin.pod_state import spend_rollup

        _write_network(shared_dir, {
            "team_bot_a": {"role": "member"},
            "admin_bot": {"role": "member"},
            "ghost": {"role": "member"},
        })
        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_today(),
                           total_usd=2.00)
        # admin_bot has no rollup file → excluded.
        # ghost has a zero-spend rollup → also excluded.
        _write_cost_rollup(shared_dir, bot_id="ghost", day_iso=_today(),
                           total_usd=0.0, input_tokens=0, output_tokens=0)

        result = spend_rollup(shared_dir, window="7d")
        assert "team_bot_a" in result["by_bot"]
        assert "admin_bot" not in result["by_bot"]
        assert "ghost" not in result["by_bot"]


# ── recent_turns ─────────────────────────────────────────────────────────────


def _write_transcripts(shared_dir: Path, *, bot_id: str, turns: list) -> None:
    d = shared_dir / "metrics" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "recent-transcripts.json").write_text(
        json.dumps(turns), encoding="utf-8",
    )


class TestRecentTurns:
    def test_missing_buffer_reports_opted_out(self, shared_dir):
        """No file at all = bot has security scanning disabled."""
        from evolve_admin.pod_state import recent_turns
        result = recent_turns(shared_dir, bot_id="team_bot_a")
        assert result["turns"] == []
        assert result["count"] == 0
        assert result["opted_out"] is True

    def test_empty_buffer_reports_not_opted_out(self, shared_dir):
        """File exists but is empty = scanning enabled, no recent activity."""
        from evolve_admin.pod_state import recent_turns

        _write_transcripts(shared_dir, bot_id="team_bot_a", turns=[])
        result = recent_turns(shared_dir, bot_id="team_bot_a")
        assert result["turns"] == []
        assert result["opted_out"] is False

    def test_returns_tail_of_buffer(self, shared_dir):
        """The capture writes newest-last; we return the tail."""
        from evolve_admin.pod_state import recent_turns

        turns = [
            {"session_id": "s", "turn_index": i, "ts": f"2026-05-14T00:00:{i:02d}Z",
             "text": f"turn-{i}"}
            for i in range(20)
        ]
        _write_transcripts(shared_dir, bot_id="team_bot_a", turns=turns)

        result = recent_turns(shared_dir, bot_id="team_bot_a", limit=5)
        assert result["count"] == 5
        # Tail = most recent 5 = indices 15..19.
        assert [t["text"] for t in result["turns"]] == [
            "turn-15", "turn-16", "turn-17", "turn-18", "turn-19",
        ]

    def test_limit_capped_at_max(self, shared_dir):
        from evolve_admin.pod_state import recent_turns
        from evolve_admin.pod_state.turns import MAX_LIMIT

        turns = [
            {"session_id": "s", "turn_index": i, "ts": "2026-05-14T00:00:00Z",
             "text": f"t{i}"}
            for i in range(MAX_LIMIT + 10)
        ]
        _write_transcripts(shared_dir, bot_id="team_bot_a", turns=turns)

        result = recent_turns(shared_dir, bot_id="team_bot_a", limit=999)
        assert result["count"] == MAX_LIMIT

    def test_missing_bot_id_returns_opted_out(self, shared_dir):
        """Caller didn't specify bot_id — return safe empty, not crash."""
        from evolve_admin.pod_state import recent_turns
        result = recent_turns(shared_dir, bot_id="")
        assert result["turns"] == []
        assert result["opted_out"] is True

    def test_malformed_buffer_returns_empty_not_crash(self, shared_dir):
        """A corrupt buffer file must not break the bot's tool call."""
        from evolve_admin.pod_state import recent_turns

        d = shared_dir / "metrics" / "team_bot_a"
        d.mkdir(parents=True, exist_ok=True)
        (d / "recent-transcripts.json").write_text("not json at all", encoding="utf-8")

        result = recent_turns(shared_dir, bot_id="team_bot_a")
        assert result["turns"] == []
        # File exists, so opted_out is False even though malformed —
        # the bot can render "I had trouble reading the buffer" if it
        # wants to distinguish, but the safe default is "no turns".
        assert result["opted_out"] is False


# ── describe_bot ────────────────────────────────────────────────────────────


class TestDescribeBot:
    def test_unknown_bot_returns_not_found(self, shared_dir):
        from evolve_admin.pod_state import describe_bot

        _write_network(shared_dir, {"team_bot_a": {"role": "member"}})
        result = describe_bot(shared_dir, bot_id="nonexistent")
        assert result["found"] is False
        assert result["bot_id"] == "nonexistent"
        assert result["role"] is None

    def test_returns_role_and_tier(self, shared_dir):
        from evolve_admin.pod_state import describe_bot

        _write_network(shared_dir, {
            "team_bot_a": {"role": "member", "tier": "full"},
        })
        result = describe_bot(shared_dir, bot_id="team_bot_a")
        assert result["found"] is True
        assert result["role"] == "member"
        assert result["tier"] == "full"

    def test_extracts_integrations_from_dict(self, shared_dir):
        """integrations as a dict-of-id (wizard's shape)."""
        from evolve_admin.pod_state import describe_bot

        _write_network(shared_dir, {
            "team_bot_a": {"role": "member",
                    "integrations": {"slack": {}, "github": {}}},
        })
        result = describe_bot(shared_dir, bot_id="team_bot_a")
        assert sorted(result["integrations"]) == ["github", "slack"]

    def test_extracts_integrations_from_list(self, shared_dir):
        """integrations as a flat list (older shape)."""
        from evolve_admin.pod_state import describe_bot

        _write_network(shared_dir, {
            "team_bot_a": {"role": "member",
                    "integrations": ["slack", "github"]},
        })
        result = describe_bot(shared_dir, bot_id="team_bot_a")
        assert sorted(result["integrations"]) == ["github", "slack"]

    def test_composes_firing_signals(self, shared_dir):
        from evolve_admin.pod_state import describe_bot
        from schema.signal import Signal, new_signal_id
        from signals import store as _store

        _write_network(shared_dir, {"team_bot_a": {"role": "member"}})
        for i in range(3):
            sig = Signal(
                id=new_signal_id(),
                signature=f"test:t{i}:team_bot_a",
                producer="test",
                type=f"t{i}",
                flavor="activity",
                severity="warn",
                scope="bot",
                bot_id="team_bot_a",
                title=f"sig {i}",
                body="",
                state="firing",
            )
            _store.write_signal(sig, shared_dir,
                                subdir=_store.subdir_for_state("firing"))

        result = describe_bot(shared_dir, bot_id="team_bot_a")
        assert result["firing_signals"]["count"] == 3

    def test_composes_spend(self, shared_dir):
        from evolve_admin.pod_state import describe_bot

        _write_network(shared_dir, {"team_bot_a": {"role": "member"}})
        _write_cost_rollup(shared_dir, bot_id="team_bot_a", day_iso=_today(),
                           total_usd=4.20)

        result = describe_bot(shared_dir, bot_id="team_bot_a")
        assert result["spend_7d_usd"] == pytest.approx(4.20)
