"""Tests for the --bots-manifest parser used in non-interactive setup.

Pod membership is explicit. Non-interactive `evolve-admin setup --fresh`
requires the operator to supply a JSON manifest listing the exact bots
to register — the wizard refuses to guess from filesystem state. This
contract was added after the 2026-05-03 phantom-bot incident (personal_bot_user +
forge auto-included by an inferred discovery scan).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import (  # noqa: E402
    BotSpec,
    ManifestError,
    load_bots_manifest,
)


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


# ── Happy paths ───────────────────────────────────────────────────────────────


def test_minimal_entry(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": [{"bot_id": "admin_bot", "port": 19000}]})
    [bot] = load_bots_manifest(p)
    assert bot.name == "admin_bot"
    assert bot.port == 19000
    assert bot.role == "member"
    assert bot.multi_user is False
    assert bot.user == ""
    assert bot.effective_user == "admin_bot"


def test_explicit_user_propagates(tmp_path):
    """The whole reason BotSpec.user exists: team_bot_b lives on personal_bot_user."""
    p = _write(tmp_path / "m.json", {
        "bots": [{"bot_id": "team_bot_b", "port": 18790, "user": "personal_bot_user"}],
    })
    [bot] = load_bots_manifest(p)
    assert bot.name == "team_bot_b"
    assert bot.user == "personal_bot_user"
    assert bot.effective_user == "personal_bot_user"


def test_role_and_multi_user(tmp_path):
    p = _write(tmp_path / "m.json", {
        "bots": [
            {"bot_id": "evolve", "port": 19030, "role": "primary"},
            {"bot_id": "team_bot_c", "port": 19020, "multi_user": True},
        ],
    })
    bots = load_bots_manifest(p)
    assert bots[0].role == "primary"
    assert bots[1].multi_user is True


def test_null_role_defaults_to_member(tmp_path):
    """A null role in the manifest (e.g. from network.json corruption) must
    not propagate — it should fall back to 'member'."""
    p = _write(tmp_path / "m.json", {
        "bots": [{"bot_id": "personal_bot", "port": 19005, "role": None}],
    })
    [bot] = load_bots_manifest(p)
    assert bot.role == "member"


def test_name_synonym_accepted(tmp_path):
    """`name` is an accepted alias for `bot_id` since BotSpec stores it
    as `name` internally — operators writing manifests by hand may use
    either."""
    p = _write(tmp_path / "m.json", {"bots": [{"name": "admin_bot", "port": 19000}]})
    [bot] = load_bots_manifest(p)
    assert bot.name == "admin_bot"


def test_full_pod(tmp_path):
    """End-to-end shape matching the user's actual pod."""
    p = _write(tmp_path / "m.json", {
        "bots": [
            {"bot_id": "team_bot_a", "port": 18789},
            {"bot_id": "admin_bot", "port": 19000},
            {"bot_id": "team_bot_b", "port": 18790, "user": "personal_bot_user"},
            {"bot_id": "evolve", "port": 19030, "role": "primary"},
            {"bot_id": "team_bot_c", "port": 19020, "multi_user": True},
        ],
    })
    bots = load_bots_manifest(p)
    assert [b.name for b in bots] == ["team_bot_a", "admin_bot", "team_bot_b", "evolve", "team_bot_c"]
    assert next(b for b in bots if b.name == "team_bot_b").user == "personal_bot_user"
    assert next(b for b in bots if b.name == "evolve").role == "primary"
    assert next(b for b in bots if b.name == "team_bot_c").multi_user is True


# ── Error paths ───────────────────────────────────────────────────────────────


def test_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="cannot read"):
        load_bots_manifest(tmp_path / "nonexistent.json")


def test_invalid_json(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{ this is not json }")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_bots_manifest(p)


def test_empty_bots_list(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": []})
    with pytest.raises(ManifestError, match="non-empty"):
        load_bots_manifest(p)


def test_missing_bots_key(tmp_path):
    p = _write(tmp_path / "m.json", {"members": ["admin_bot"]})
    with pytest.raises(ManifestError, match="non-empty"):
        load_bots_manifest(p)


def test_top_level_not_object(tmp_path):
    p = _write(tmp_path / "m.json", ["admin_bot", "team_bot_a"])
    with pytest.raises(ManifestError):
        load_bots_manifest(p)


def test_entry_not_object(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": ["admin_bot"]})
    with pytest.raises(ManifestError, match=r"bots\[0\] must be an object"):
        load_bots_manifest(p)


def test_missing_bot_id(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": [{"port": 19000}]})
    with pytest.raises(ManifestError, match=r"bots\[0\].*bot_id"):
        load_bots_manifest(p)


def test_missing_port(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": [{"bot_id": "admin_bot"}]})
    with pytest.raises(ManifestError, match=r"bots\[0\].*port"):
        load_bots_manifest(p)


def test_non_integer_port(tmp_path):
    p = _write(tmp_path / "m.json",
               {"bots": [{"bot_id": "admin_bot", "port": "19000"}]})
    with pytest.raises(ManifestError, match=r"port"):
        load_bots_manifest(p)


def test_negative_port(tmp_path):
    p = _write(tmp_path / "m.json", {"bots": [{"bot_id": "admin_bot", "port": -1}]})
    with pytest.raises(ManifestError, match=r"port"):
        load_bots_manifest(p)


def test_error_points_at_offending_index(tmp_path):
    """When the manifest has multiple entries and one is bad, the error
    message must identify which index failed so the operator can fix it."""
    p = _write(tmp_path / "m.json", {
        "bots": [
            {"bot_id": "team_bot_a", "port": 18789},
            {"bot_id": "admin_bot"},  # missing port
            {"bot_id": "team_bot_b", "port": 18790},
        ],
    })
    with pytest.raises(ManifestError, match=r"bots\[1\]"):
        load_bots_manifest(p)
