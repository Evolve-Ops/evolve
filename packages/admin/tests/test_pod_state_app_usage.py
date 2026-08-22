"""tests/test_pod_state_app_usage.py — the pod_state.app_usage evo tool.

Evo's read window onto the AL-1.3 per-app rollup. The contract under
test is the honesty contract (docs/design-app-attribution-2026-08-15 §3):
grades come back additive, the unattributed bucket + coverage share ride
along on every answer, an unmeasured bot says so instead of reporting
zero usage, and a bot id that is not in network.json is refused rather
than turned into a filesystem read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import RiskTier, pod_state_app_usage  # noqa: E402
from evolve_admin.evo.tools.facades import advertised_manifest  # noqa: E402

BOT = "team_bot_a"


def _window(turns: int, cost: float) -> dict:
    return {"turns": turns, "input_tokens": turns * 10,
            "output_tokens": turns * 20, "cost_estimated": cost}


def _rollup() -> dict:
    grades = {
        "total": _window(6, 1.2),
        "scheduled": _window(6, 1.2),
        "explicit": _window(0, 0.0),
        "inferred": _window(2, 0.4),
    }
    return {
        "schema_version": 1,
        "generated_at": "2026-08-17T03:35:00.000000Z",
        "bot_id": BOT,
        "apps": {
            "digest": {
                "first_seen_ts": "2026-08-11T06:00:00.000Z",
                "last_seen_ts": "2026-08-17T06:00:00.000Z",
                "d1": {k: dict(v) for k, v in grades.items()},
                "d7": {k: dict(v) for k, v in grades.items()},
                "d30": {k: dict(v) for k, v in grades.items()},
            },
            "notes": {
                "first_seen_ts": "2026-08-16T09:00:00.000Z",
                "last_seen_ts": "2026-08-16T09:00:00.000Z",
                "d1": {k: dict(v) for k, v in grades.items()},
                "d7": {k: dict(v) for k, v in grades.items()},
                "d30": {k: dict(v) for k, v in grades.items()},
            },
        },
        "unattributed": {
            k: {**_window(80, 16.0), "legacy_schema_turns": 30}
            for k in ("d1", "d7", "d30")
        },
        "evolve_overhead": {k: _window(4, 0.8) for k in ("d1", "d7", "d30")},
        "coverage": {
            k: {"attributed_turns": 12, "inferred_turns": 4,
                "unattributed_turns": 80, "legacy_schema_turns": 30,
                "evolve_overhead_turns": 4, "app_turns_total": 96,
                "unattributed_turns_share": 0.8333,
                "unattributed_cost_share": 0.8333}
            for k in ("d1", "d7", "d30")
        },
    }


@pytest.fixture
def pod(tmp_path: Path):
    shared = tmp_path / "shared"
    (shared / BOT).mkdir(parents=True)
    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "members": [BOT], "bots": {BOT: {}}, "sharedDir": str(shared),
    }))
    return {"network": network, "shared": shared}


def _write_rollup(pod, payload: dict, bot: str = BOT) -> None:
    (pod["shared"] / bot).mkdir(parents=True, exist_ok=True)
    (pod["shared"] / bot / "usage-by-app.json").write_text(json.dumps(payload))


# ── Registration ─────────────────────────────────────────────────────────────

def test_tool_is_registered_as_a_read_tool() -> None:
    tool = _tools.lookup("pod_state.app_usage")
    assert tool is not None
    assert tool.risk_tier is RiskTier.READ
    # Read tools never need a validate() gate.
    assert getattr(tool, "validate", None) is None


def test_tool_is_advertised_through_the_pod_state_facade() -> None:
    """Registering without wiring the facade would leave the tool
    invisible to the model — the facade is what list_tools serves."""
    facade = next(t for t in advertised_manifest() if t["name"] == "pod_state")
    assert "app_usage" in facade["input_schema"]["properties"]["query"]["enum"]


# ── Payload shape ────────────────────────────────────────────────────────────

def test_returns_grades_additively_with_coverage(pod) -> None:
    _write_rollup(pod, _rollup())
    out = pod_state_app_usage._handler(pod["network"])

    assert out["ok"] is True
    assert out["window"] == "d7"
    bot = out["bots"][BOT]
    assert bot["measured"] is True and bot["attributed"] is True
    digest = bot["apps"]["digest"]
    assert digest["total"]["turns"] == 6
    assert digest["inferred"]["turns"] == 2
    assert digest["last_seen_ts"] == "2026-08-17T06:00:00.000Z"
    # The two numbers evo must quote alongside any per-app figure.
    assert bot["unattributed"]["turns"] == 80
    assert bot["coverage"]["unattributed_turns_share"] == 0.8333
    assert bot["evolve_overhead"]["turns"] == 4


def test_app_id_filters_to_one_app(pod) -> None:
    _write_rollup(pod, _rollup())
    out = pod_state_app_usage._handler(pod["network"], app_id="notes")
    assert set(out["bots"][BOT]["apps"]) == {"notes"}
    # Coverage is NOT filtered away with the apps — it is pod truth.
    assert out["bots"][BOT]["coverage"]["unattributed_turns"] == 80


def test_unknown_app_id_returns_no_rows_not_an_error(pod) -> None:
    _write_rollup(pod, _rollup())
    out = pod_state_app_usage._handler(pod["network"], app_id="nope")
    assert out["ok"] is True
    assert out["bots"][BOT]["apps"] == {}


@pytest.mark.parametrize("days,window", [(1, "d1"), (7, "d7"), (30, "d30")])
def test_days_selects_the_window(pod, days: int, window: str) -> None:
    _write_rollup(pod, _rollup())
    out = pod_state_app_usage._handler(pod["network"], days=days)
    assert out["window"] == window


def test_bad_days_is_refused(pod) -> None:
    out = pod_state_app_usage._handler(pod["network"], days=90)
    assert out["ok"] is False
    assert "1, 7 or 30" in out["error"]


# ── Honest failure modes ─────────────────────────────────────────────────────

def test_missing_rollup_is_unmeasured_not_zero(pod) -> None:
    out = pod_state_app_usage._handler(pod["network"])
    assert out["ok"] is True
    assert out["bots"][BOT] == {"measured": False, "apps": {}}


def test_unregistered_bot_is_refused(pod) -> None:
    """A bot id that is not in network.json never reaches the filesystem —
    the same whitelist guard the /api/primary/state routes apply."""
    out = pod_state_app_usage._handler(pod["network"], bot_id="../../etc")
    assert out["ok"] is False
    assert "not registered" in out["error"]


def test_only_the_named_bot_is_read(pod) -> None:
    other = "team_bot_b"
    (pod["shared"] / other).mkdir(parents=True)
    _write_rollup(pod, _rollup())
    _write_rollup(pod, _rollup(), bot=other)

    out = pod_state_app_usage._handler(pod["network"], bot_id=BOT)
    assert set(out["bots"]) == {BOT}


def test_malformed_rollup_degrades_to_unmeasured(pod) -> None:
    (pod["shared"] / BOT / "usage-by-app.json").write_text("{ truncated")
    out = pod_state_app_usage._handler(pod["network"])
    assert out["bots"][BOT]["measured"] is False
