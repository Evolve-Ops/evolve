"""tests/test_pod_state_audits.py — list_audits and the shared
audit_state cache (Bundle 3 closeout: 8th and final spec §5.1 tool).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
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


@pytest.fixture(autouse=True)
def _reset_audit_state():
    """Audit cache is module-level; reset between tests so one test's
    cached results don't leak into another."""
    from evolve_admin import audit_state
    audit_state.reset()
    yield
    audit_state.reset()


# ── audit_state module ─────────────────────────────────────────────────────


class TestAuditState:
    def test_initial_snapshot_is_empty(self):
        from evolve_admin import audit_state
        snap = audit_state.snapshot()
        assert snap["data"] == {}
        assert snap["cached_at"] is None

    def test_aliased_with_server(self):
        """server.py's _audit_cache is aliased to audit_state._state.
        Writes through either name must be visible from the other —
        that's the whole point of the alias (single source of truth)."""
        from evolve_admin import audit_state
        from evolve_admin.web import server as srv

        srv._audit_cache["data"]["team_bot_a"] = {"summary": {"critical": 2}}
        srv._audit_cache["cached_at"] = 12345.0

        snap = audit_state.snapshot()
        assert snap["data"]["team_bot_a"]["summary"]["critical"] == 2
        assert snap["cached_at"] == 12345.0

    def test_snapshot_returns_copy_not_reference(self):
        """Mutating the returned dict must NOT corrupt the cache."""
        from evolve_admin import audit_state
        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 0}}
        snap = audit_state.snapshot()
        snap["data"]["team_bot_a"]["summary"]["critical"] = 999  # mutate the copy
        # Real cache is untouched.
        assert audit_state._state["data"]["team_bot_a"]["summary"]["critical"] == 0


# ── list_audits ─────────────────────────────────────────────────────────────


class TestListAudits:
    def test_empty_cache_reports_stale(self, shared_dir):
        """No background sweep has populated the cache — that's
        'stale', not 'no findings'."""
        from evolve_admin.pod_state import list_audits

        result = list_audits(shared_dir)
        assert result["audits"] == []
        assert result["count"] == 0
        assert result["stale"] is True
        assert result["cached_at"] is None

    def test_returns_per_bot_latest(self, shared_dir):
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state

        audit_state._state["data"]["team_bot_a"] = {
            "summary": {"critical": 1, "warn": 3, "info": 5}}
        audit_state._state["data"]["admin_bot"] = {
            "summary": {"critical": 0, "warn": 0, "info": 2}}
        audit_state._state["cached_at"] = time.time()

        result = list_audits(shared_dir)
        assert result["count"] == 2
        assert result["stale"] is False
        # Sorted by bot_id for deterministic output.
        bot_ids = [a["bot_id"] for a in result["audits"]]
        assert bot_ids == ["admin_bot", "team_bot_a"]
        # Result is stamped with bot_id so the renderer doesn't have
        # to walk a parent mapping.
        team_bot_a_audit = next(a for a in result["audits"] if a["bot_id"] == "team_bot_a")
        assert team_bot_a_audit["summary"]["critical"] == 1

    def test_filters_by_bot_id(self, shared_dir):
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state

        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 1}}
        audit_state._state["data"]["admin_bot"] = {"summary": {"critical": 0}}
        audit_state._state["cached_at"] = time.time()

        result = list_audits(shared_dir, bot_id="team_bot_a")
        assert result["count"] == 1
        assert result["audits"][0]["bot_id"] == "team_bot_a"

    def test_unknown_bot_id_reports_known_bot_false(self, shared_dir):
        """Distinguish 'bot exists but no findings' (known_bot=True,
        count=0) from 'bot id not in cache at all' (known_bot=False,
        count=0). The route layer 400s on unknown bots, but the
        library function is reachable directly by the MCP bridge —
        silent ambiguity caught in second-pass review of Bundle 3-
        part-3.
        """
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state

        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 0}}
        audit_state._state["cached_at"] = time.time()

        result = list_audits(shared_dir, bot_id="never-existed")
        assert result["count"] == 0
        assert result["known_bot"] is False
        # Still not stale — cache populated for OTHER bots.
        assert result["stale"] is False

    def test_known_bot_with_findings_reports_known_bot_true(self, shared_dir):
        """The positive case for known_bot."""
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state

        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 2}}
        audit_state._state["cached_at"] = time.time()

        result = list_audits(shared_dir, bot_id="team_bot_a")
        assert result["count"] == 1
        assert result["known_bot"] is True

    def test_future_dated_cached_at_reports_stale(self, shared_dir):
        """Negative delta (clock skew / NTP step / VM time warp) must
        produce stale=True, not silently 'forever fresh.' Regression
        for an edge case caught in second-pass review."""
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state

        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 0}}
        # 1 hour in the future.
        audit_state._state["cached_at"] = time.time() + 3600

        result = list_audits(shared_dir)
        assert result["stale"] is True

    def test_limit_caps_count(self, shared_dir):
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state
        from evolve_admin.pod_state.audits import MAX_LIMIT

        for i in range(MAX_LIMIT + 10):
            audit_state._state["data"][f"bot{i:03d}"] = {
                "summary": {"critical": 0}}
        audit_state._state["cached_at"] = time.time()

        result = list_audits(shared_dir, limit=10_000)
        assert result["count"] == MAX_LIMIT
        assert result["filters"]["limit"] == MAX_LIMIT

    def test_stale_when_cache_too_old(self, shared_dir):
        """Populated cache > FRESH_WINDOW_SECONDS ago is stale."""
        from evolve_admin.pod_state import list_audits
        from evolve_admin import audit_state
        from evolve_admin.pod_state.audits import FRESH_WINDOW_SECONDS

        audit_state._state["data"]["team_bot_a"] = {"summary": {"critical": 0}}
        audit_state._state["cached_at"] = time.time() - (FRESH_WINDOW_SECONDS + 60)

        result = list_audits(shared_dir)
        assert result["stale"] is True
        # But still returns the data so the bot has something to
        # talk about — better than empty.
        assert result["count"] == 1
