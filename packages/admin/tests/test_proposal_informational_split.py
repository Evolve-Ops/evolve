"""/api/arbiter/proposals tags + filters observation/FYI proposals
(Effectiveness-Layer triage §11) so the actionable queue stays clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture
def client(tmp_path):
    from evolve_admin.web.server import create_app
    from testing.harness import make_config_patch_proposal, make_investigation_proposal
    from arbiter.store import write_proposal

    shared = tmp_path / "shared"
    (shared / "proposals" / "pending").mkdir(parents=True)

    target = tmp_path / "cfg.json"
    target.write_text("{}")
    actionable = make_config_patch_proposal(  # ConfigPatch — a real action
        target_path=f"{target}::ui.theme", value="dark", bot_id="team_bot_a",
    )
    info = make_investigation_proposal(bot_id="team_bot_a")  # Investigation — FYI
    for p in (actionable, info):
        write_proposal(p, shared, subdir="pending",
                       maintain_signal_backrefs=False, coalesce=False)

    net = tmp_path / "network.json"
    net.write_text(json.dumps(
        {"bots": {"team_bot_a": {"user": "team_bot_a"}}, "sharedDir": str(shared)}
    ))
    app = create_app(network_path=net)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, actionable.id, info.id


def _ids(resp):
    return {p["id"] for p in resp.get_json()["proposals"]}


def test_each_proposal_is_tagged(client):
    c, act_id, info_id = client
    by_id = {p["id"]: p for p in c.get("/api/arbiter/proposals?include=pending").get_json()["proposals"]}
    assert by_id[act_id]["informational"] is False
    assert by_id[info_id]["informational"] is True


def test_actionable_filter_excludes_observations(client):
    c, act_id, info_id = client
    ids = _ids(c.get("/api/arbiter/proposals?include=pending&informational=false"))
    assert act_id in ids
    assert info_id not in ids


def test_informational_filter_returns_only_observations(client):
    c, act_id, info_id = client
    ids = _ids(c.get("/api/arbiter/proposals?include=pending&informational=true"))
    assert info_id in ids
    assert act_id not in ids
