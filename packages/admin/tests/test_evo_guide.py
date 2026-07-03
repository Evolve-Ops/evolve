"""tests/test_evo_guide.py — bot guide storage + session_surface injection.

Spec: docs/spec-evo-wizard-2026-05-05.md §5.

Exercises the guide module (read / write / round-trip / error paths), the
admin endpoints (GET/PUT), the CLI (show-guide / set-guide), and the
session_surface integration (guide block formatted into the system prefix
when present, silent fallback when absent or malformed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Storage module
# ─────────────────────────────────────────────────────────────────────────────


def test_read_returns_none_for_absent_guide(tmp_path):
    from evolve_admin.evo import guide

    assert guide.read_guide(tmp_path, "team_bot_a") is None
    assert guide.guide_exists(tmp_path, "team_bot_a") is False


def test_write_then_read_round_trips_body_and_frontmatter(tmp_path):
    from evolve_admin.evo import guide

    body = "# Team_bot_a Guide\n\nTeam_bot_a handles CI.\n"
    fm = {"audience": "team", "tone": "direct", "do_say": ["ping team_bot_a on CI"]}
    g = guide.write_guide(tmp_path, "team_bot_a",
                          frontmatter=fm, body=body, authored_by="pod_admin_user")
    assert g.body == body
    assert g.frontmatter["audience"] == "team"
    assert g.frontmatter["bot_id"] == "team_bot_a"
    assert g.last_edited_at is not None
    assert g.authored_at is not None
    assert g.authored_by == "pod_admin_user"

    # Read back: identical body, structured frontmatter
    g2 = guide.read_guide(tmp_path, "team_bot_a")
    assert g2 is not None
    assert g2.body == body
    assert g2.frontmatter["audience"] == "team"
    assert g2.frontmatter["do_say"] == ["ping team_bot_a on CI"]


def test_write_preserves_authored_at_across_writes(tmp_path):
    from evolve_admin.evo import guide

    g1 = guide.write_guide(tmp_path, "team_bot_a",
                           frontmatter={"audience": "v1"}, body="v1",
                           authored_by="pod_admin_user")
    g2 = guide.write_guide(tmp_path, "team_bot_a",
                           frontmatter={"audience": "v2"}, body="v2")
    assert g2.authored_at == g1.authored_at
    # authored_by preserved across writes when not overridden
    assert g2.authored_by == "pod_admin_user"


def test_write_authored_by_override(tmp_path):
    from evolve_admin.evo import guide

    guide.write_guide(tmp_path, "team_bot_a", body="v1", authored_by="alice")
    g2 = guide.write_guide(tmp_path, "team_bot_a", body="v2", authored_by="bob")
    assert g2.authored_by == "bob"


def test_unknown_frontmatter_keys_round_trip(tmp_path):
    """Permissive schema — unknown keys survive read+write so we can add
    structured fields later without breaking older guides."""
    from evolve_admin.evo import guide

    guide.write_guide(tmp_path, "team_bot_a",
                      frontmatter={"future_field": "hello", "audience": "team"},
                      body="body", authored_by="pod_admin_user")
    g = guide.read_guide(tmp_path, "team_bot_a")
    assert g.frontmatter["future_field"] == "hello"


def test_malformed_frontmatter_raises(tmp_path):
    from evolve_admin.evo import guide

    path = guide.guide_path(tmp_path, "team_bot_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nopened but: never\nclosed", encoding="utf-8")
    with pytest.raises(guide.GuideError):
        guide.read_guide(tmp_path, "team_bot_a")


def test_load_for_session_surface_swallows_errors(tmp_path):
    """Runtime hot path returns None on any failure (vs read_guide which
    raises) so a corrupt guide doesn't break a bot's session."""
    from evolve_admin.evo import guide

    path = guide.guide_path(tmp_path, "team_bot_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbroken yaml: : :\n---\nbody", encoding="utf-8")
    assert guide.load_for_session_surface(tmp_path, "team_bot_a") is None


def test_load_for_session_surface_skips_when_no_last_edited_at(tmp_path):
    """A file without ``last_edited_at`` is treated as a stub, not a guide.
    Per spec §5.2: only inject when last_edited_at is non-null."""
    from evolve_admin.evo import guide

    path = guide.guide_path(tmp_path, "team_bot_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbot_id: team_bot_a\naudience: team\n---\n\nbody\n",
                    encoding="utf-8")
    assert guide.load_for_session_surface(tmp_path, "team_bot_a") is None


def test_atomic_write_does_not_leave_tmp_on_success(tmp_path):
    from evolve_admin.evo import guide

    guide.write_guide(tmp_path, "team_bot_a", body="v1", authored_by="pod_admin_user")
    leftover = list((tmp_path / "bot_guides").glob("*.tmp"))
    assert leftover == []


# ─────────────────────────────────────────────────────────────────────────────
# session_surface integration
# ─────────────────────────────────────────────────────────────────────────────


def test_session_surface_returns_empty_when_no_guide(tmp_path):
    import session_surface

    assert session_surface.load_bot_guide_block("team_bot_a", tmp_path) == ""


def test_session_surface_returns_empty_when_no_bot_id(tmp_path):
    import session_surface

    assert session_surface.load_bot_guide_block(None, tmp_path) == ""


def test_session_surface_renders_present_guide(tmp_path):
    import session_surface
    from evolve_admin.evo import guide

    guide.write_guide(
        tmp_path, "team_bot_a",
        frontmatter={
            "audience": "engineering team",
            "tone": "direct, no emoji",
            "do_say": ["ping team_bot_a on CI failures"],
            "dont_say": ["no HR or payroll"],
        },
        body="# Team_bot_a\n\nTeam_bot_a is the team CI assistant.\n",
        authored_by="pod_admin_user",
    )
    block = session_surface.load_bot_guide_block("team_bot_a", tmp_path)
    assert block.startswith("[BOT GUIDE")
    # Distinguishes itself from SOUL.md
    assert "SOUL.md" in block
    # Structured fields surfaced
    assert "engineering team" in block
    assert "direct, no emoji" in block
    assert "ping team_bot_a on CI failures" in block
    assert "no HR or payroll" in block
    # Body included
    assert "Team_bot_a is the team CI assistant" in block


def test_session_surface_swallows_malformed_guide(tmp_path):
    import session_surface
    from evolve_admin.evo import guide

    # Write a malformed file directly
    path = guide.guide_path(tmp_path, "team_bot_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbroken: : :\n---\nbody\n", encoding="utf-8")
    assert session_surface.load_bot_guide_block("team_bot_a", tmp_path) == ""


def test_build_session_prefix_includes_guide(tmp_path):
    import session_surface
    from evolve_admin.evo import guide

    guide.write_guide(tmp_path, "team_bot_a",
                      frontmatter={"audience": "team"},
                      body="Team_bot_a body.",
                      authored_by="pod_admin_user")
    block = session_surface.load_bot_guide_block("team_bot_a", tmp_path)
    prefix = session_surface.build_session_prefix(guide_block=block)
    # Pod conduct comes first, then guide
    conduct_pos = prefix.find("POD CONDUCT")
    guide_pos = prefix.find("[BOT GUIDE")
    if conduct_pos == -1:
        # Fallback rendering when POD_CONDUCT.md isn't reachable
        assert guide_pos > -1
    else:
        assert guide_pos > conduct_pos


def test_build_session_prefix_omits_guide_when_empty():
    import session_surface

    prefix = session_surface.build_session_prefix(guide_block="")
    assert "[BOT GUIDE" not in prefix


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {"members": ["team_bot_a", "admin_bot"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(evo_routes, "save_network", _atomic_save)
    monkeypatch.setattr(_cfg, "save_network", _atomic_save)

    app = Flask(__name__)
    app.config["TESTING"] = True
    evo_routes.register_evo_routes(app, network_path)
    return app, tmp_path


def test_route_get_returns_404_for_absent(evo_app):
    app, _ = evo_app
    client = app.test_client()
    r = client.get("/api/evo/guide/team_bot_a")
    assert r.status_code == 404
    data = r.get_json()
    assert data["exists"] is False


def test_route_put_then_get_round_trips(evo_app):
    app, _ = evo_app
    client = app.test_client()

    r = client.put("/api/evo/guide/team_bot_a", json={
        "frontmatter": {"audience": "team", "tone": "direct"},
        "body": "# Team_bot_a Guide\n\nTeam_bot_a handles CI.\n",
        "authored_by": "pod_admin_user",
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["body"].endswith("Team_bot_a handles CI.\n")
    assert data["frontmatter"]["audience"] == "team"

    r = client.get("/api/evo/guide/team_bot_a")
    assert r.status_code == 200
    fetched = r.get_json()
    assert fetched["exists"] is True
    assert fetched["frontmatter"]["audience"] == "team"
    assert fetched["frontmatter"]["authored_by"] == "pod_admin_user"


def test_route_put_validates_types(evo_app):
    app, _ = evo_app
    client = app.test_client()
    # frontmatter must be a dict
    r = client.put("/api/evo/guide/team_bot_a", json={
        "frontmatter": "not-a-dict",
        "body": "ok",
    })
    assert r.status_code == 400


def test_route_get_returns_422_for_malformed(evo_app):
    app, shared_dir = evo_app
    client = app.test_client()

    # Plant a malformed guide directly
    from evolve_admin.evo import guide as _guide
    path = _guide.guide_path(shared_dir, "team_bot_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbroken yaml: : :\n---\nbody\n", encoding="utf-8")

    r = client.get("/api/evo/guide/team_bot_a")
    assert r.status_code == 422
    assert "malformed" in r.get_json()["error"]


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner_with_network(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import config as _cfg

    network = {"members": ["team_bot_a", "admin_bot"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_cfg, "save_network", _atomic_save)
    monkeypatch.setenv("SUDO_USER", "pod_admin_user")  # stable actor across hosts
    return CliRunner(), tmp_path, network_path


def test_cli_show_guide_when_absent(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "show-guide", "team_bot_a",
    ])
    assert r.exit_code == 0
    assert "no guide recorded" in r.output


def test_cli_set_guide_from_file(cli_runner_with_network, tmp_path):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    src = tmp_path / "team_bot_a-draft.md"
    src.write_text(
        "---\naudience: engineering team\ntone: direct\n---\n\n"
        "# Team_bot_a Guide\n\nTeam_bot_a handles CI.\n",
        encoding="utf-8",
    )

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-guide", "team_bot_a", "--file", str(src),
    ])
    assert r.exit_code == 0, r.output
    assert "wrote team_bot_a guide" in r.output

    # Read back
    from evolve_admin.evo import guide as _guide
    g = _guide.read_guide(shared_dir, "team_bot_a")
    assert g is not None
    assert g.frontmatter["audience"] == "engineering team"
    assert g.frontmatter["tone"] == "direct"
    assert "Team_bot_a handles CI" in g.body
    # CLI defaulted authored_by to SUDO_USER on first write
    assert g.authored_by == "pod_admin_user"


def test_cli_set_guide_then_show(cli_runner_with_network, tmp_path):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    src = tmp_path / "team_bot_a-draft.md"
    src.write_text(
        "---\naudience: team\ndo_say: [ping on CI]\n---\n\n"
        "Body content here.\n",
        encoding="utf-8",
    )
    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-guide", "team_bot_a", "--file", str(src),
    ])
    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "show-guide", "team_bot_a",
    ])
    assert r.exit_code == 0
    assert "audience" in r.output
    assert "team" in r.output
    assert "Body content here" in r.output


def test_cli_set_guide_authored_by_override(cli_runner_with_network, tmp_path):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    src = tmp_path / "team_bot_a-draft.md"
    src.write_text("---\naudience: team\n---\n\nBody.\n", encoding="utf-8")
    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-guide", "team_bot_a", "--file", str(src),
        "--authored-by", "alice",
    ])
    from evolve_admin.evo import guide as _guide
    g = _guide.read_guide(shared_dir, "team_bot_a")
    assert g.authored_by == "alice"


def test_cli_set_guide_missing_file_exits_nonzero(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-guide", "team_bot_a", "--file", "/nonexistent/path.md",
    ])
    assert r.exit_code != 0
    assert "not found" in r.output


def test_cli_show_guide_raw_pipes_clean(cli_runner_with_network, tmp_path):
    """`show-guide --raw` should emit something parseable by set-guide."""
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    src = tmp_path / "team_bot_a-draft.md"
    src.write_text(
        "---\naudience: team\n---\n\n# Body\n",
        encoding="utf-8",
    )
    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-guide", "team_bot_a", "--file", str(src),
    ])
    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "show-guide", "team_bot_a", "--raw",
    ])
    assert r.exit_code == 0
    # Output contains both the frontmatter fence and the body
    assert "---" in r.output
    assert "audience: team" in r.output
    assert "# Body" in r.output
