"""tests/test_evo_deploy_skills.py — Evolve-shipped skill loader hookup.

Tests for ``ensure_evo_skills_extra_dir`` — the deploy hook that
registers the Evolve-shipped skills directory with OC's six-source
skill loader at source-1 (lowest) precedence so workspace
customization always wins (spec §14.2).

Mirrors test patterns in test_evo_deploy_integration (when present)
and test_evo_tools.py — tmp_path fixtures, monkeypatched config
readers + writers, no real /Users/<bot> access.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo.tools import deploy_skills  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _network(primary: str = "evolve") -> dict:
    return {
        "primary": primary,
        "sharedDir": "/tmp/test-shared",
        "bots": {
            "evolve": {"role": "primary", "user": "evolve"},
            "team_bot_a": {"role": "member", "user": "team_bot_a"},
        },
    }


def _patch_read(monkeypatch, oc: dict | None, err: str | None = None) -> None:
    """Stub _read_bot_oc_config to return the given config (or error)."""
    def fake(bot_id, bot_user):
        if oc is None:
            return None, err
        return oc, None
    monkeypatch.setattr(deploy_skills, "_read_bot_oc_config", fake)


def _patch_write_capture(monkeypatch) -> dict:
    """Stub _safe_write_bot_oc_config; return a dict that records the
    last write so tests can assert on it."""
    captured: dict = {}

    def fake(bot_id, new_config, *, reason, bot_user):
        captured["bot_id"] = bot_id
        captured["config"] = new_config
        captured["reason"] = reason
        captured["bot_user"] = bot_user
        return True, "ok"

    monkeypatch.setattr(deploy_skills, "_safe_write_bot_oc_config", fake)
    return captured


def _patch_write_fail(monkeypatch, msg: str = "boom") -> None:
    def fake(bot_id, new_config, *, reason, bot_user):
        return False, msg
    monkeypatch.setattr(deploy_skills, "_safe_write_bot_oc_config", fake)


# ─────────────────────────────────────────────────────────────────────────────
# evolve_skills_dir constant
# ─────────────────────────────────────────────────────────────────────────────


def test_evolve_skills_dir_points_at_deploy_checkout():
    """The constant must point at the deploy-managed checkout location,
    NOT the user's dev clone. Production reads happen from
    /Users/Shared/evolve-repo on the mini."""
    p = deploy_skills.evolve_skills_dir()
    assert p == (
        "/Users/Shared/evolve-repo/packages/analyzer/evolve_bot/skills"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Primary-bot gate
# ─────────────────────────────────────────────────────────────────────────────


def test_skips_non_primary_bot():
    """Member bots aren't part of the source-1 hookup."""
    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "team_bot_a", _network(),
    )
    assert changed is False
    assert status == "skipped:not-primary"


def test_skips_when_no_primary_resolved():
    """A network with no primary at all should also skip — gateways
    that haven't been onboarded yet shouldn't crash the deploy."""
    net = {"bots": {}}
    changed, status = deploy_skills.ensure_evo_skills_extra_dir("evolve", net)
    assert changed is False
    assert status == "skipped:not-primary"


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency: unchanged when already present
# ─────────────────────────────────────────────────────────────────────────────


def test_unchanged_when_dir_already_in_extra_dirs(monkeypatch):
    """If the canonical dir is already listed, no write attempt."""
    canonical = deploy_skills.evolve_skills_dir()
    _patch_read(monkeypatch, {
        "agents": {
            "defaults": {
                "skills": {
                    "load": {
                        "extraDirs": [canonical, "/some/other/dir"],
                    },
                },
            },
        },
    })

    write_called = {"flag": False}

    def assert_not_called(*a, **k):
        write_called["flag"] = True
        return True, "ok"

    monkeypatch.setattr(deploy_skills, "_safe_write_bot_oc_config", assert_not_called)

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is False
    assert status == "unchanged"
    assert write_called["flag"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Created — fresh bot or missing extraDirs key
# ─────────────────────────────────────────────────────────────────────────────


def test_created_when_extra_dirs_absent(monkeypatch):
    """No agents.defaults.skills block at all → create the chain and
    add the dir."""
    _patch_read(monkeypatch, {"agents": {"defaults": {}}})
    captured = _patch_write_capture(monkeypatch)

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is True
    assert status == "created"

    new_cfg = captured["config"]
    extra = new_cfg["agents"]["defaults"]["skills"]["load"]["extraDirs"]
    assert extra == [deploy_skills.evolve_skills_dir()]
    assert "skills loader" in captured["reason"].lower()


def test_created_when_extra_dirs_present_but_missing_evolve_dir(monkeypatch):
    """Existing extraDirs list — operator has their own entries — append
    rather than replace. Preserves operator-added paths."""
    _patch_read(monkeypatch, {
        "agents": {
            "defaults": {
                "skills": {
                    "load": {
                        "extraDirs": ["/operator/custom/skills"],
                    },
                },
            },
        },
    })
    captured = _patch_write_capture(monkeypatch)

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is True
    assert status == "created"

    extra = captured["config"]["agents"]["defaults"]["skills"]["load"]["extraDirs"]
    # Operator's custom dir preserved + our dir appended
    assert "/operator/custom/skills" in extra
    assert deploy_skills.evolve_skills_dir() in extra
    assert len(extra) == 2


def test_created_when_extra_dirs_is_not_a_list(monkeypatch):
    """Operator hand-edited to set extraDirs to a string by mistake →
    treat as absent and rewrite with a clean list of one entry."""
    _patch_read(monkeypatch, {
        "agents": {
            "defaults": {
                "skills": {
                    "load": {
                        "extraDirs": "should-have-been-a-list",
                    },
                },
            },
        },
    })
    captured = _patch_write_capture(monkeypatch)

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is True
    assert status == "created"
    extra = captured["config"]["agents"]["defaults"]["skills"]["load"]["extraDirs"]
    assert extra == [deploy_skills.evolve_skills_dir()]


# ─────────────────────────────────────────────────────────────────────────────
# Error surfaces
# ─────────────────────────────────────────────────────────────────────────────


def test_read_failure_returns_error_status(monkeypatch):
    _patch_read(monkeypatch, None, err="permission denied")
    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is False
    assert status.startswith("error: read failed:")
    assert "permission denied" in status


def test_write_failure_returns_error_status(monkeypatch):
    _patch_read(monkeypatch, {})
    _patch_write_fail(monkeypatch, "disk full")

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
    )
    assert changed is False
    assert status.startswith("error: write failed:")
    assert "disk full" in status


# ─────────────────────────────────────────────────────────────────────────────
# Override path (test-only)
# ─────────────────────────────────────────────────────────────────────────────


def test_skills_dir_override_takes_precedence(monkeypatch):
    """The ``skills_dir`` kwarg overrides the canonical path. Used by
    fixture-driven tests; production callers never set it."""
    _patch_read(monkeypatch, {})
    captured = _patch_write_capture(monkeypatch)

    changed, status = deploy_skills.ensure_evo_skills_extra_dir(
        "evolve", _network(),
        skills_dir="/tmp/test-skills",
    )
    assert changed is True
    assert status == "created"
    extra = captured["config"]["agents"]["defaults"]["skills"]["load"]["extraDirs"]
    assert extra == ["/tmp/test-skills"]


# ─────────────────────────────────────────────────────────────────────────────
# Shipped skills directory exists on disk + ships investigate-firing-signal
# ─────────────────────────────────────────────────────────────────────────────


def test_evolve_shipped_skills_dir_exists_in_repo():
    """Sanity: the path we register must exist in the repo. If the
    skills/ tree gets accidentally deleted, this test catches it."""
    skills_dir = _ANALYZER_PKG / "evolve_bot" / "skills"
    assert skills_dir.is_dir(), (
        f"Expected Evolve-shipped skills dir at {skills_dir}; missing — "
        "the deploy hook will register a non-existent path."
    )


def test_first_shipped_skill_exists_and_has_required_frontmatter():
    """The first Evolve-shipped skill (per spec §14.2) is the
    'investigate firing signal end-to-end' recipe. Confirms it ships
    with the right frontmatter shape (name + description)."""
    skill = (
        _ANALYZER_PKG / "evolve_bot" / "skills"
        / "investigate-firing-signal" / "SKILL.md"
    )
    assert skill.is_file(), f"Expected starter skill at {skill}"
    text = skill.read_text()
    # YAML frontmatter present + required fields
    assert text.startswith("---\n")
    assert "name: investigate-firing-signal" in text
    assert "description:" in text
    # Metadata block carries the Evolve namespace
    assert "metadata:" in text
    assert "evolve:" in text
    # Body cites real tools from the registry — at least one pod_state
    # read tool and at least one action tool should appear.
    # Post-B7-Phase-2 the skill teaches the consolidated facade forms.
    assert 'pod_state(query="signals.firing"' in text
    assert 'proposal_action(action="apply"' in text or "signal_action(" in text
