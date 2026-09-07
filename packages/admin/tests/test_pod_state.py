"""tests/test_pod_state.py — pod_state read tools (Bundle 3 of the
primary-bot-interface spec).

Covers:
  - list_signals: state/producer/bot_id filtering, limit cap, empty case.
  - recent_watchdog: hours window, bot_id filter, ordering.
  - list_proposals: state→subdir routing, summary shape, limit cap.
  - pod_status: bots from network.json, firing-signal counts per bot
    and pod-wide.

Each test writes minimal fixtures into a temp shared_dir, calls the
function, and asserts the returned dict shape. No mocking of analyzer
internals — we exercise the real stores end-to-end so a store-API
change shows up as a pod_state test failure.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# Analyzer dir must be on sys.path so the pod_state modules can lazy-
# import ``signals.store``, ``arbiter.store``, etc.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


# ── list_signals ────────────────────────────────────────────────────────────


def _write_signal(shared_dir: Path, *, state: str = "firing", **overrides) -> str:
    """Write one Signal to the right subdir and return its id."""
    from schema.signal import Signal, new_signal_id
    from signals import store as _store

    defaults = dict(
        id=new_signal_id(),
        signature=f"{overrides.get('producer','test')}:{overrides.get('type','t')}:{overrides.get('bot_id','b')}",
        producer="test",
        type="t",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id="team_bot_a",
        title="t",
        body="",
    )
    defaults.update(overrides)
    defaults["state"] = state  # type: ignore[assignment]
    sig = Signal(**defaults)
    _store.write_signal(sig, shared_dir, subdir=_store.subdir_for_state(state))
    return sig.id


class TestListSignals:
    def test_empty_store_returns_empty(self, shared_dir):
        from evolve_admin.pod_state import list_signals

        result = list_signals(shared_dir)
        assert result["signals"] == []
        assert result["count"] == 0
        # The filters echo back the normalized state so the caller can
        # confirm what was actually applied.
        assert result["filters"]["state"] == "firing"

    def test_returns_firing_signals_by_default(self, shared_dir):
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", bot_id="team_bot_a")
        _write_signal(shared_dir, state="firing", bot_id="admin_bot")
        # An archived signal should NOT surface with the default filter.
        _write_signal(shared_dir, state="resolved", bot_id="team_bot_a")

        result = list_signals(shared_dir)
        assert result["count"] == 2
        bot_ids = sorted(s["bot_id"] for s in result["signals"])
        assert bot_ids == ["admin_bot", "team_bot_a"]

    def test_filters_by_bot_id(self, shared_dir):
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", bot_id="team_bot_a")
        _write_signal(shared_dir, state="firing", bot_id="admin_bot")

        result = list_signals(shared_dir, bot_id="team_bot_a")
        assert result["count"] == 1
        assert result["signals"][0]["bot_id"] == "team_bot_a"

    def test_filters_by_producer(self, shared_dir):
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", producer="cost_alert")
        _write_signal(shared_dir, state="firing", producer="security_warden")

        result = list_signals(shared_dir, producer="cost_alert")
        assert result["count"] == 1
        assert result["signals"][0]["producer"] == "cost_alert"

    def test_limit_caps_at_max(self, shared_dir):
        """An LLM can't request more than MAX_LIMIT — the caller's
        guardrail, not the route's."""
        from evolve_admin.pod_state import list_signals
        from evolve_admin.pod_state.signals import MAX_LIMIT

        for i in range(MAX_LIMIT + 25):
            _write_signal(shared_dir, state="firing", bot_id=f"b{i}",
                          signature=f"test:t:b{i}")

        result = list_signals(shared_dir, limit=10_000)
        assert result["count"] == MAX_LIMIT
        assert result["filters"]["limit"] == MAX_LIMIT

    def test_invalid_state_falls_back_to_firing_not_to_all(self, shared_dir):
        """A bot passing state='fring' should get firing results (the
        default), NOT silently broaden to every state ever recorded.
        The previous "invalid → None" coercion was caught in
        second-pass review as a silent-success-but-wrong path: the
        LLM typoes and the bot reports archived signals as if they
        were live.

        Caller can detect the coercion by comparing filters.state to
        the value they passed in.
        """
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", bot_id="team_bot_a",
                      signature="t:f:team_bot_a")
        _write_signal(shared_dir, state="resolved", bot_id="team_bot_a",
                      signature="t:r:team_bot_a")

        result = list_signals(shared_dir, state="nonsense")
        # Coerced to "firing", not None — does NOT include the
        # resolved signal.
        assert result["filters"]["state"] == "firing"
        assert result["count"] == 1
        assert result["signals"][0]["state"] == "firing"

    def test_state_all_fans_out_across_lifecycle(self, shared_dir):
        """state='all' (or None) must include firing + snoozed +
        resolved + dismissed. The first implementation routed
        through iter_active only, silently dropping every archived
        signal. Regression for the bug caught in second-pass review.
        """
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", bot_id="team_bot_a",
                      signature="t:firing:team_bot_a")
        _write_signal(shared_dir, state="snoozed", bot_id="team_bot_a",
                      signature="t:snoozed:team_bot_a", snoozed_until="2099-01-01T00:00:00Z")
        _write_signal(shared_dir, state="resolved", bot_id="team_bot_a",
                      signature="t:resolved:team_bot_a")
        _write_signal(shared_dir, state="dismissed", bot_id="team_bot_a",
                      signature="t:dismissed:team_bot_a")

        result = list_signals(shared_dir, state="all")
        states = {s["state"] for s in result["signals"]}
        assert states == {"firing", "snoozed", "resolved", "dismissed"}
        assert result["count"] == 4

    def test_state_none_equivalent_to_all(self, shared_dir):
        """Passing state=None explicitly must match state='all'."""
        from evolve_admin.pod_state import list_signals

        _write_signal(shared_dir, state="firing", signature="t:a:team_bot_a")
        _write_signal(shared_dir, state="resolved", signature="t:b:team_bot_a")

        result = list_signals(shared_dir, state=None)
        assert result["count"] == 2


# ── recent_watchdog ─────────────────────────────────────────────────────────


def _write_watchdog(shared_dir: Path, *, ts: datetime, bot_id: str | None,
                    event_type: str = "gateway_instability") -> str:
    """Append one WatchdogEvent to the right daily JSONL file."""
    from schema.watchdog import WatchdogEvent, new_watchdog_event_id

    ev = WatchdogEvent(
        id=new_watchdog_event_id(),
        bot_id=bot_id,
        timestamp=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        event_type=event_type,  # type: ignore[arg-type]
        severity="warn",
        details={},
    )
    wdir = shared_dir / "watchdog"
    wdir.mkdir(parents=True, exist_ok=True)
    daily = wdir / f"{ts.date().isoformat()}.jsonl"
    with daily.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev.to_dict()) + "\n")
    return ev.id


class TestRecentWatchdog:
    def test_empty_returns_empty(self, shared_dir):
        from evolve_admin.pod_state import recent_watchdog

        result = recent_watchdog(shared_dir)
        assert result["events"] == []
        assert result["count"] == 0

    def test_returns_recent_events(self, shared_dir):
        from evolve_admin.pod_state import recent_watchdog

        now = datetime.now(timezone.utc)
        _write_watchdog(shared_dir, ts=now - timedelta(hours=1), bot_id="team_bot_a")
        _write_watchdog(shared_dir, ts=now - timedelta(hours=2), bot_id="admin_bot")

        result = recent_watchdog(shared_dir, hours=24)
        assert result["count"] == 2

    def test_excludes_events_outside_window(self, shared_dir):
        from evolve_admin.pod_state import recent_watchdog

        now = datetime.now(timezone.utc)
        _write_watchdog(shared_dir, ts=now - timedelta(hours=1), bot_id="team_bot_a")
        # 100 hours ago — outside a 24-hour window.
        _write_watchdog(shared_dir, ts=now - timedelta(hours=100),
                        bot_id="team_bot_a")

        result = recent_watchdog(shared_dir, hours=24)
        assert result["count"] == 1

    def test_orders_newest_first(self, shared_dir):
        """The LLM almost always wants 'what just happened' — newest
        first is the load-bearing ordering."""
        from evolve_admin.pod_state import recent_watchdog

        now = datetime.now(timezone.utc)
        _write_watchdog(shared_dir, ts=now - timedelta(hours=5),
                        event_type="gateway_instability", bot_id="team_bot_a")
        _write_watchdog(shared_dir, ts=now - timedelta(hours=1),
                        event_type="proposal_volume_deviation", bot_id="team_bot_a")

        result = recent_watchdog(shared_dir)
        # Newest event_type is proposal_volume_deviation (1h ago);
        # gateway_instability is older (5h ago).
        assert result["events"][0]["event_type"] == "proposal_volume_deviation"

    def test_filters_by_bot_id(self, shared_dir):
        from evolve_admin.pod_state import recent_watchdog

        now = datetime.now(timezone.utc)
        _write_watchdog(shared_dir, ts=now, bot_id="team_bot_a")
        _write_watchdog(shared_dir, ts=now, bot_id="admin_bot")

        result = recent_watchdog(shared_dir, bot_id="team_bot_a")
        assert result["count"] == 1
        assert result["events"][0]["bot_id"] == "team_bot_a"


# ── list_proposals ──────────────────────────────────────────────────────────


def _write_proposal_minimal(shared_dir: Path, *, status: str = "pending",
                            title: str = "p", generator_id: str = "g",
                            bot_id: str = "team_bot_a") -> str:
    """Write a minimal proposal JSON directly into the right subdir.

    Proposal has a lot of required fields (provenance, action, risk_tag,
    …) — none of which list_proposals' summary actually surfaces.
    Bypassing the dataclass and writing the JSON directly keeps the
    fixture small and avoids coupling test churn to schema additions
    in unrelated fields.
    """
    from schema.proposal import new_proposal_id

    pid = new_proposal_id()
    body = {
        "id": pid,
        "schema_version": 1,
        "created_at": "2026-05-14T18:00:00+00:00",
        "bot_id": bot_id,
        "generator_id": generator_id,
        "dimension": "test",
        "trigger_observations": [],
        "provenance": {
            "technique": "test_fixture",
            "signals": {},
            "confidence": 0.5,
        },
        "problem": "test problem",
        "action": {"kind": "Investigation", "context": "test"},
        "risk_tag": {
            "blast_radius": "single_bot",
            "reversibility": "easy",
            "touches": [],
        },
        "approval_audience": "none",
        "urgency": "improvement",
        "admin_surface_summary": title,
        "conversational_pitch": None,
        "guardian_annotations": [],
        "conflicts_with": [],
        "status": status,
        "snoozed_until": None,
        "history": [],
        "revisions": [],
        "adjacency_type": None,
        "motivating_signals": [],
        "signature": "",
    }

    subdir_map = {
        "pending": "pending",
        "snoozed": "snoozed",
        "applied": "applied",
        "succeeded": "archived",
        "failed": "archived",
        "rejected": "archived",
    }
    subdir = subdir_map.get(status, "pending")
    pdir = shared_dir / "proposals" / subdir
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{pid}.json").write_text(json.dumps(body), encoding="utf-8")
    return pid


class TestListProposals:
    def test_empty_returns_empty(self, shared_dir):
        from evolve_admin.pod_state import list_proposals

        result = list_proposals(shared_dir)
        assert result["proposals"] == []
        assert result["count"] == 0

    def test_returns_pending_by_default(self, shared_dir):
        from evolve_admin.pod_state import list_proposals

        _write_proposal_minimal(shared_dir, status="pending", title="P1")
        _write_proposal_minimal(shared_dir, status="applied", title="P2")

        result = list_proposals(shared_dir)
        assert result["count"] == 1
        assert result["proposals"][0]["title"] == "P1"

    def test_summary_shape_drops_heavy_fields(self, shared_dir):
        """The summary must NOT include the full body / diff blob —
        the LLM tool call should stay small. The bot can fetch full
        envelopes via /api/proposals/<id> when it wants the diff."""
        from evolve_admin.pod_state import list_proposals

        _write_proposal_minimal(shared_dir, status="pending", title="P1")
        result = list_proposals(shared_dir)
        s = result["proposals"][0]
        # Required summary fields per the contract:
        assert {"id", "title", "status", "generator_id", "bot_id",
                "motivating_signals", "created_at"} <= set(s.keys())
        # Heavy fields must NOT leak through:
        assert "body" not in s
        assert "diff" not in s

    def test_state_filter_active_returns_pending_and_snoozed(self, shared_dir):
        from evolve_admin.pod_state import list_proposals

        _write_proposal_minimal(shared_dir, status="pending", title="P1")
        _write_proposal_minimal(shared_dir, status="snoozed", title="P2")
        _write_proposal_minimal(shared_dir, status="applied", title="P3")

        result = list_proposals(shared_dir, state="active")
        assert result["count"] == 2

    def test_unknown_state_falls_back_to_pending(self, shared_dir):
        from evolve_admin.pod_state import list_proposals

        _write_proposal_minimal(shared_dir, status="pending", title="P1")
        result = list_proposals(shared_dir, state="bogus")
        assert result["filters"]["state"] == "pending"
        assert result["count"] == 1


# ── pod_status ──────────────────────────────────────────────────────────────


def _write_network(shared_dir: Path, bots: dict) -> Path:
    """Write a minimal network.json."""
    network = {"bots": bots}
    path = shared_dir / "network.json"
    path.write_text(json.dumps(network), encoding="utf-8")
    return path


class TestPodStatus:
    def test_empty_network_returns_empty_bots(self, shared_dir):
        from evolve_admin.pod_state import pod_status

        result = pod_status(shared_dir)
        assert result["bots"] == []
        assert result["primary_id"] is None
        assert result["firing_signals_total"] == 0

    def test_lists_bots_from_network(self, shared_dir):
        from evolve_admin.pod_state import pod_status

        _write_network(shared_dir, {
            "evo": {"role": "primary", "tier": "full"},
            "team_bot_a": {"role": "member", "tier": "full"},
            "admin_bot": {"role": "member", "tier": "monitor"},
        })

        result = pod_status(shared_dir)
        assert result["primary_id"] == "evo"
        names = sorted(b["bot_id"] for b in result["bots"])
        assert names == ["admin_bot", "evo", "team_bot_a"]
        # Tier passes through to the summary.
        tiers = {b["bot_id"]: b["tier"] for b in result["bots"]}
        assert tiers["admin_bot"] == "monitor"

    def test_counts_firing_signals_per_bot(self, shared_dir):
        from evolve_admin.pod_state import pod_status

        _write_network(shared_dir, {
            "evo": {"role": "primary"},
            "team_bot_a": {"role": "member"},
        })
        # Two firing for team_bot_a, one for evo, one pod-wide.
        _write_signal(shared_dir, state="firing", bot_id="team_bot_a",
                      signature="t:a:team_bot_a", scope="bot")
        _write_signal(shared_dir, state="firing", bot_id="team_bot_a",
                      signature="t:b:team_bot_a", scope="bot")
        _write_signal(shared_dir, state="firing", bot_id="evo",
                      signature="t:a:evo", scope="bot")
        _write_signal(shared_dir, state="firing", bot_id=None,
                      signature="t:c:pod", scope="pod")

        result = pod_status(shared_dir)
        by_bot = {b["bot_id"]: b["firing_signals"] for b in result["bots"]}
        assert by_bot["team_bot_a"] == 2
        assert by_bot["evo"] == 1
        # Pod-wide signal counted separately, not against any bot.
        assert result["firing_signals_pod_wide"] == 1
        assert result["firing_signals_total"] == 4

    def test_accepts_preloaded_network_dict(self, shared_dir):
        """Callers (admin server, MCP bridge) often already have
        network.json loaded — passing it through avoids a second
        read."""
        from evolve_admin.pod_state import pod_status

        net = {"bots": {"primary_bot": {"role": "primary"}}}
        result = pod_status(shared_dir, network=net)
        assert result["primary_id"] == "primary_bot"
