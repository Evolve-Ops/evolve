"""tests/test_evo_apps.py — `evo apps` handler (replaces the stub)."""

from __future__ import annotations

import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


_PwResult = namedtuple("_PwResult", ["pw_dir"])


def _write_manifest(
    home: Path, app_id: str, name: str, desc: str = "", identity=None,
) -> None:
    d = home / ".openclaw" / "workspace" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": app_id, "name": name, "description": desc}
    if identity is not None:
        payload["identity"] = identity
    (d / f"{app_id}.json").write_text(json.dumps(payload))


@pytest.fixture
def fake_homes(tmp_path, monkeypatch):
    homes: dict[str, Path] = {}

    def fake_pwnam(user: str):
        if user in homes:
            return _PwResult(pw_dir=str(homes[user]))
        raise KeyError(user)

    from evolve_admin.evo.handlers import apps
    monkeypatch.setattr(apps.pwd, "getpwnam", fake_pwnam)
    return homes, tmp_path


def _call(fake_homes, bot_id: str, network: dict | None = None):
    _, tmp_path = fake_homes
    from evolve_admin.evo.handlers.apps import render
    network = network or {"sharedDir": str(tmp_path), "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_bot_with_no_manifests_dir(fake_homes):
    """A genuinely-absent manifests dir means the bot simply has no apps —
    NOT 'couldn't read'. Folding absence into 'unreadable' is the same
    exists()-lies bug the tri-state primitive fixes."""
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "No apps registered yet" in body


def test_bot_with_unreadable_manifests_dir(fake_homes, monkeypatch):
    """A permission wall on the manifests dir (EACCES under the bot's 0700
    home) is reported as unreadable — distinct from 'no apps'."""
    from evolve_admin.evo.handlers import apps
    from evolve_admin.evo.tools.fs_tristate import ReadState

    monkeypatch.setattr(apps, "stat_status", lambda p: ReadState.INDETERMINATE)
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "Couldn't read" in body


def test_bot_with_empty_manifests_dir(fake_homes):
    homes, tmp_path = fake_homes
    homes["admin_bot"] = tmp_path / "admin_bot"
    (tmp_path / "admin_bot" / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "No apps registered yet" in body
    assert "evo gallery" in body


def test_bot_lists_manifests(fake_homes):
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    _write_manifest(admin_bot, "app_a", "App Alpha", "Does alpha things")
    _write_manifest(admin_bot, "app_b", "Beta App", "")
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "**Apps — admin_bot** (2 installed)" in body
    assert "App Alpha — Does alpha things" in body
    assert "Beta App" in body


def test_bot_lists_prefer_identity_purpose(fake_homes):
    """The tray "list my apps" line renders the canonical `identity.purpose`,
    matching the Apps page tile + detail/Edit modal. A stale/contaminated
    top-level `description` (the atlas "Member Management" residue from the
    #3095/#3108 conflation incident) must NOT be what the tray shows when a
    correct `identity.purpose` exists. Falls back to top-level `description`
    only when no purpose is present (legacy / v7-arc manifests)."""
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot

    # 1. Both present + divergent (the atlas shape): purpose wins.
    _write_manifest(
        admin_bot, "app_contaminated", "Article Capture",
        desc="Member Management is a privacy-preserving member-records system.",
        identity={"purpose": "Surface ecosystem news to an enthusiast community."},
    )
    # 2. Only top-level description (no identity) → legacy fallback.
    _write_manifest(admin_bot, "app_legacy", "Legacy", desc="Sends a daily summary")
    # 3. identity present but purpose empty → fall back to description, not "".
    _write_manifest(
        admin_bot, "app_empty_purpose", "Empty Purpose",
        desc="Real description text", identity={"purpose": ""},
    )

    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    # Canonical: tray line == identity.purpose, NOT the contaminated text.
    assert "Article Capture — Surface ecosystem news to an enthusiast community." in body
    assert "Member Management" not in body
    # Legacy fallback intact: no identity.purpose → top-level description.
    assert "Legacy — Sends a daily summary" in body
    # Empty purpose falls through to description (not a blank line).
    assert "Empty Purpose — Real description text" in body


def test_bot_lists_non_dict_identity_does_not_crash(fake_homes):
    """A malformed non-dict `identity` must not raise and drop the app — the
    render path falls back to top-level `description` defensively."""
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    _write_manifest(
        admin_bot, "app_bad_identity", "Bad Identity",
        desc="Fallback description", identity="not-a-dict",
    )
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "Bad Identity — Fallback description" in body


def test_bot_truncates_long_lists(fake_homes):
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    for i in range(20):
        _write_manifest(admin_bot, f"app_{i:02d}", f"App {i:02d}", "")
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "(20 installed)" in body
    assert "and 8 more" in body  # 20 - 12 max


def test_bot_skips_dotfiles_and_non_json(fake_homes):
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    _write_manifest(admin_bot, "app_real", "Real App", "")
    manifests = admin_bot / ".openclaw" / "workspace" / "manifests"
    (manifests / ".scan-status.json").write_text("{}")  # skipped
    (manifests / "notes.txt").write_text("nope")        # skipped
    r = _call(fake_homes, "admin_bot")
    body = r.direct_send_message or ""
    assert "(1 installed)" in body
    assert "Real App" in body


def test_pod_aggregates(fake_homes):
    homes, tmp_path = fake_homes
    admin_bot = tmp_path / "admin_bot"
    team_bot_a = tmp_path / "team_bot_a"
    homes["admin_bot"] = admin_bot
    homes["team_bot_a"] = team_bot_a
    _write_manifest(admin_bot, "a", "A")
    _write_manifest(admin_bot, "b", "B")
    _write_manifest(team_bot_a, "c", "C")

    network = {"sharedDir": str(tmp_path), "members": ["admin_bot", "team_bot_a", "evolve"]}
    r = _call(fake_homes, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Apps — pod** (3 total)" in body
    assert body.index("admin_bot: 2") < body.index("team_bot_a: 1")
