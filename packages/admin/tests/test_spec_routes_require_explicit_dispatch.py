"""tests/test_spec_routes_require_explicit_dispatch.py

Covers the ``forge.require_explicit_dispatch`` opt-in flag that switches
the spec wizard's Approve & Build action back to the two-step gate
(approve → review on Forge Jobs page → click Dispatch). Default is the
one-click flow: Approve dispatches immediately.

Two layers:
  1. ``config.get_forge_require_explicit_dispatch`` — resolution order
     (per-bot.forge → per-bot flat alias → pod-wide → default False).
  2. ``spec_routes.api_specs_approve`` — when the flag is set for a bot,
     create the forge job but DO NOT call ``_dispatch_forge_job``; the
     response entry carries ``dispatched=False``. When the flag is unset
     (the default), the job IS dispatched and the entry carries
     ``dispatched=True``.

These tests pin the contract the wizard's Step-3 modal relies on: it
reads ``dispatched`` on each forge_jobs entry to render either
"Building X" (one-click default) or "Queued — needs dispatch" (opt-in).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ─────────────────────────────────────────────────────────────────────────────
# config.get_forge_require_explicit_dispatch — resolution order
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigResolution:

    def test_defaults_to_false(self):
        """No config → one-click flow (Approve dispatches immediately)."""
        from evolve_admin.config import get_forge_require_explicit_dispatch
        assert get_forge_require_explicit_dispatch("any-bot", {}) is False
        assert get_forge_require_explicit_dispatch(
            "any-bot", {"bots": {}}
        ) is False
        assert get_forge_require_explicit_dispatch(
            "any-bot", {"bots": {"any-bot": {}}}
        ) is False

    def test_per_bot_forge_block_wins(self):
        """``bots[bot].forge.require_explicit_dispatch`` is the most-specific
        location and overrides everything else."""
        from evolve_admin.config import get_forge_require_explicit_dispatch
        net = {
            "forge": {"require_explicit_dispatch": False},
            "bots": {
                "team_bot_a": {
                    "forge_require_explicit_dispatch": False,
                    "forge": {"require_explicit_dispatch": True},
                },
            },
        }
        assert get_forge_require_explicit_dispatch("team_bot_a", net) is True

    def test_per_bot_flat_alias(self):
        """``bots[bot].forge_require_explicit_dispatch`` (flat) is honoured
        when no ``forge`` sub-block is present — operators sometimes set
        flat fields out of habit and we shouldn't punish that."""
        from evolve_admin.config import get_forge_require_explicit_dispatch
        net = {
            "bots": {
                "team_bot_a": {
                    "forge_require_explicit_dispatch": True,
                },
            },
        }
        assert get_forge_require_explicit_dispatch("team_bot_a", net) is True

    def test_pod_wide_fallback(self):
        """``forge.require_explicit_dispatch`` at the top level applies
        when no per-bot override exists. Useful for operators who want
        every bot in the pod to require explicit dispatch."""
        from evolve_admin.config import get_forge_require_explicit_dispatch
        net = {
            "forge": {"require_explicit_dispatch": True},
            "bots": {"team_bot_a": {}},
        }
        assert get_forge_require_explicit_dispatch("team_bot_a", net) is True
        # Per-bot override beats pod-wide
        net["bots"]["team_bot_a"]["forge"] = {
            "require_explicit_dispatch": False,
        }
        assert get_forge_require_explicit_dispatch("team_bot_a", net) is False

    @pytest.mark.parametrize("truthy", [True, "true", "yes", "on", 1, "1"])
    def test_truthy_values_accepted(self, truthy):
        from evolve_admin.config import get_forge_require_explicit_dispatch
        net = {
            "bots": {"b": {"forge": {"require_explicit_dispatch": truthy}}},
        }
        assert get_forge_require_explicit_dispatch("b", net) is True

    @pytest.mark.parametrize("falsy", [False, "false", "no", "off", 0, "0"])
    def test_falsy_values_accepted(self, falsy):
        from evolve_admin.config import get_forge_require_explicit_dispatch
        net = {
            "bots": {"b": {"forge": {"require_explicit_dispatch": falsy}}},
        }
        assert get_forge_require_explicit_dispatch("b", net) is False


# ─────────────────────────────────────────────────────────────────────────────
# spec_routes.api_specs_approve — dispatch behavior
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path: Path):
    """Stand up a sharedDir with a network.json + specs dir."""
    (tmp_path / "specs").mkdir()
    (tmp_path / "applications").mkdir()
    (tmp_path / "manifests").mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    return tmp_path


@pytest.fixture
def app(shared, monkeypatch):
    """Flask app with spec routes registered, network.json overridden."""
    from evolve_admin import config as _cfg
    monkeypatch.setattr(
        _cfg, "DEFAULT_NETWORK_CONFIG", shared / "network.json"
    )
    from evolve_admin.web import spec_routes  # noqa: F401

    # Route manifest writes to tmp instead of /Users/<bot>/...
    def _appdir(_shared_dir, bot_id):
        out = shared / "workspaces" / bot_id / "manifests"
        out.mkdir(parents=True, exist_ok=True)
        return out
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir)

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    spec_routes.register_spec_routes(flask_app, shared)
    return flask_app


def _seed_session(shared: Path, bot_id: str = "team_bot_a") -> str:
    """Persist a SpecSession in 'draft' status with one draft ready to
    approve. Returns the session_id."""
    from evolve_admin.applications.spec_session import (
        SpecSession, SpecDraft, save_session,
    )
    draft = SpecDraft(
        version=1,
        display_name="Trip Research",
        description="Plan trips for the household.",
        build_spec="# Trip Research\n\nResearch trip ideas.",
        application_tags=["productivity"],
        requirements={"integrations": [], "secrets": [],
                      "python_packages": [], "system": []},
        app_dependencies=[],
        test_command="python3 -m pytest tests/",
        test_exemption_reason="",
        conflicts=[],
        suggestions=[],
        usage={},
        created_at="2026-06-05T12:00:00Z",
    )
    session = SpecSession(
        session_id="s-test1234",
        status="draft",
        target_bots=[bot_id],
        input="Plan a trip",
        drafts=[draft.to_dict()],
        feedback_history=[],
        approved_version=None,
        forge_jobs=[],
        created_at="2026-06-05T12:00:00Z",
        updated_at="2026-06-05T12:00:00Z",
        created_by="ui",
    )
    save_session(session, shared)
    return session.session_id


def _set_network(shared: Path, payload: dict) -> None:
    """Overwrite the test sharedDir's network.json."""
    payload.setdefault("sharedDir", str(shared))
    (shared / "network.json").write_text(json.dumps(payload))


class TestDispatchBehavior:

    def test_default_is_one_click_dispatch(self, app, shared):
        """Default config → Approve & Build creates the job AND dispatches.
        The forge_jobs entry carries ``dispatched=True``."""
        _set_network(shared, {"bots": {"team_bot_a": {}}})
        session_id = _seed_session(shared)
        with patch(
            "evolve_admin.web.spec_routes._dispatch_forge_job",
            return_value=(True, "ok"),
        ) as m_dispatch:
            with app.test_client() as c:
                resp = c.post(
                    f"/api/specs/{session_id}/approve",
                    json={"version": 1, "confirmed": True},
                )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert "errors" not in data, data
        assert len(data["forge_jobs"]) == 1
        entry = data["forge_jobs"][0]
        assert entry["bot_id"] == "team_bot_a"
        assert entry["dispatched"] is True
        m_dispatch.assert_called_once()
        # job_id passed to dispatch matches the entry
        assert m_dispatch.call_args.args[0] == entry["job_id"]
        assert m_dispatch.call_args.args[1] == "team_bot_a"

    def test_require_explicit_dispatch_skips_dispatch_call(self, app, shared):
        """When ``require_explicit_dispatch=true``, the approve handler
        creates the forge job but never calls _dispatch_forge_job — the
        job is left in 'queued' for the operator to dispatch from the
        Forge Jobs page. The response carries ``dispatched=False`` so
        the Step-3 modal can render the right copy."""
        _set_network(shared, {
            "bots": {
                "team_bot_a": {
                    "forge": {"require_explicit_dispatch": True},
                },
            },
        })
        session_id = _seed_session(shared)
        with patch(
            "evolve_admin.web.spec_routes._dispatch_forge_job",
            return_value=(True, "ok"),
        ) as m_dispatch:
            with app.test_client() as c:
                resp = c.post(
                    f"/api/specs/{session_id}/approve",
                    json={"version": 1, "confirmed": True},
                )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert "errors" not in data, data
        assert len(data["forge_jobs"]) == 1
        entry = data["forge_jobs"][0]
        assert entry["dispatched"] is False
        m_dispatch.assert_not_called()

    def test_pod_wide_flag_applies_to_all_bots(self, app, shared):
        """Pod-wide ``forge.require_explicit_dispatch=true`` gates every
        bot's dispatch in a single multi-bot approval."""
        _set_network(shared, {
            "forge": {"require_explicit_dispatch": True},
            "bots": {"team_bot_a": {}, "team_bot_b": {}},
        })
        from evolve_admin.applications.spec_session import (
            SpecSession, SpecDraft, save_session,
        )
        draft = SpecDraft(
            version=1, display_name="X", description="x",
            build_spec="# x", application_tags=["t"],
            requirements={"integrations": [], "secrets": [],
                          "python_packages": [], "system": []},
            app_dependencies=[], test_command="", test_exemption_reason="trivial",
            conflicts=[], suggestions=[], usage={},
            created_at="2026-06-05",
        )
        session = SpecSession(
            session_id="s-multi", status="draft",
            target_bots=["team_bot_a", "team_bot_b"],
            input="", drafts=[draft.to_dict()], feedback_history=[],
            approved_version=None, forge_jobs=[],
            created_at="2026-06-05", updated_at="2026-06-05",
            created_by="ui",
        )
        save_session(session, shared)

        with patch(
            "evolve_admin.web.spec_routes._dispatch_forge_job",
            return_value=(True, "ok"),
        ) as m_dispatch:
            with app.test_client() as c:
                resp = c.post(
                    "/api/specs/s-multi/approve",
                    json={"version": 1, "confirmed": True},
                )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["forge_jobs"]) == 2
        for entry in data["forge_jobs"]:
            assert entry["dispatched"] is False
        m_dispatch.assert_not_called()

    def test_per_bot_override_beats_pod_wide(self, app, shared):
        """Pod-wide=true but one bot opts back into one-click. The
        opted-out bot dispatches; the gated bot queues."""
        _set_network(shared, {
            "forge": {"require_explicit_dispatch": True},
            "bots": {
                "team_bot_a": {"forge": {"require_explicit_dispatch": False}},
                "team_bot_b": {},  # inherits pod-wide true
            },
        })
        from evolve_admin.applications.spec_session import (
            SpecSession, SpecDraft, save_session,
        )
        draft = SpecDraft(
            version=1, display_name="X", description="x",
            build_spec="# x", application_tags=["t"],
            requirements={"integrations": [], "secrets": [],
                          "python_packages": [], "system": []},
            app_dependencies=[], test_command="", test_exemption_reason="trivial",
            conflicts=[], suggestions=[], usage={},
            created_at="2026-06-05",
        )
        session = SpecSession(
            session_id="s-mix", status="draft",
            target_bots=["team_bot_a", "team_bot_b"],
            input="", drafts=[draft.to_dict()], feedback_history=[],
            approved_version=None, forge_jobs=[],
            created_at="2026-06-05", updated_at="2026-06-05",
            created_by="ui",
        )
        save_session(session, shared)

        dispatched_bots: list[str] = []

        def _capture(job_id, bot_id):
            dispatched_bots.append(bot_id)
            return (True, "ok")

        with patch(
            "evolve_admin.web.spec_routes._dispatch_forge_job",
            side_effect=_capture,
        ):
            with app.test_client() as c:
                resp = c.post(
                    "/api/specs/s-mix/approve",
                    json={"version": 1, "confirmed": True},
                )
        assert resp.status_code == 200
        data = resp.get_json()
        by_bot = {e["bot_id"]: e for e in data["forge_jobs"]}
        assert by_bot["team_bot_a"]["dispatched"] is True
        assert by_bot["team_bot_b"]["dispatched"] is False
        assert dispatched_bots == ["team_bot_a"]

    def test_response_carries_projection_for_step3_display(self, app, shared):
        """The forge_jobs entries carry the projected mid/high cost so
        the Step-3 modal can show the operator the same number they
        confirmed on Step 2 — without re-hitting the cost_estimate
        endpoint."""
        _set_network(shared, {"bots": {"team_bot_a": {}}})
        session_id = _seed_session(shared)
        with patch(
            "evolve_admin.web.spec_routes._dispatch_forge_job",
            return_value=(True, "ok"),
        ):
            with app.test_client() as c:
                resp = c.post(
                    f"/api/specs/{session_id}/approve",
                    json={"version": 1, "confirmed": True},
                )
        assert resp.status_code == 200
        entry = resp.get_json()["forge_jobs"][0]
        # Projection fields are present (may be None on estimator failure,
        # but the keys must exist so the UI can branch on truthiness).
        assert "projected_cost_mid_usd" in entry
        assert "projected_cost_high_usd" in entry
