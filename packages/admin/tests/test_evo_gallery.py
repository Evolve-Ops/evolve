"""tests/test_evo_gallery.py — `evo gallery` handler."""

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


_FAKE_CATALOG = [
    {"pkg_id": "p1", "name": "Daily Health Tracker",
     "description": "Log meals and exercise.",
     "categories": ["personal-lifestyle"]},
    {"pkg_id": "p2", "name": "Email Assistant",
     "description": "Helps with email.",
     "categories": ["productivity"]},
    {"pkg_id": "p3", "name": "Cost Monitor",
     "description": "Tracks spend.",
     "categories": ["productivity"]},
]


@pytest.fixture
def patched(tmp_path, monkeypatch):
    homes: dict[str, Path] = {}

    def fake_pwnam(user: str):
        if user in homes:
            return _PwResult(pw_dir=str(homes[user]))
        raise KeyError(user)

    # The handler resolves homes through evolve_config.user_home (pwd-first,
    # profile-keyed fallback) — fake pwd where user_home consults it.
    import evolve_config

    from evolve_admin.evo.handlers import gallery
    monkeypatch.setattr(evolve_config.pwd, "getpwnam", fake_pwnam)
    monkeypatch.setattr(gallery, "_load_catalog", lambda: list(_FAKE_CATALOG))
    return homes, tmp_path


def _call(patched, bot_id: str, network: dict | None = None):
    _, tmp_path = patched
    from evolve_admin.evo.handlers.gallery import render
    network = network or {"sharedDir": str(tmp_path), "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_empty_catalog_renders_friendly_error(patched, monkeypatch):
    from evolve_admin.evo.handlers import gallery
    monkeypatch.setattr(gallery, "_load_catalog", lambda: [])
    r = _call(patched, "admin_bot")
    body = r.direct_send_message or ""
    assert "Couldn't read the gallery catalog" in body


def test_bot_lists_all_installable_when_nothing_installed(patched):
    r = _call(patched, "admin_bot")
    body = r.direct_send_message or ""
    assert "**Gallery — admin_bot** (3 installable, 0 already have)" in body
    assert "Daily Health Tracker" in body
    assert "Email Assistant" in body
    assert "evo install" in body


def test_bot_lists_apps_with_stable_numbering(patched):
    """Each entry is prefixed with `1.`, `2.`, …. The hint mentions the
    number form so `evo install 1` is discoverable from the gallery."""
    r = _call(patched, "admin_bot")
    body = r.direct_send_message or ""
    # Order is alphabetical by name: Cost Monitor, Daily Health Tracker,
    # Email Assistant → numbered 1, 2, 3 in that order.
    assert "1. Cost Monitor" in body
    assert "2. Daily Health Tracker" in body
    assert "3. Email Assistant" in body
    assert "evo install 1" in body


def test_available_for_bot_helper_matches_render_order(patched):
    """The helper `evo install <n>` calls must produce the same order
    the gallery handler renders — otherwise N-from-gallery would pick a
    different app than N-from-install."""
    from evolve_admin.evo.handlers.gallery import available_for_bot
    _, tmp_path = patched
    network = {"sharedDir": str(tmp_path), "members": ["admin_bot", "evolve"]}
    available = available_for_bot("admin_bot", network)
    names = [a["name"] for a in available]
    assert names == ["Cost Monitor", "Daily Health Tracker", "Email Assistant"]


def test_bot_filters_out_installed_apps(patched):
    homes, tmp_path = patched
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    manifests = admin_bot / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "app_email.json").write_text(json.dumps({
        "name": "Email Assistant", "id": "app_email",
    }))
    r = _call(patched, "admin_bot")
    body = r.direct_send_message or ""
    assert "(2 installable, 1 already have)" in body
    assert "Email Assistant" not in body
    assert "Daily Health Tracker" in body


def test_bot_already_has_everything(patched):
    homes, tmp_path = patched
    admin_bot = tmp_path / "admin_bot"
    homes["admin_bot"] = admin_bot
    manifests = admin_bot / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    for app in _FAKE_CATALOG:
        (manifests / f"{app['pkg_id']}.json").write_text(json.dumps({
            "name": app["name"], "id": app["pkg_id"],
        }))
    r = _call(patched, "admin_bot")
    body = r.direct_send_message or ""
    assert "(0 installable, 3 already have)" in body
    assert "You already have everything" in body


def test_pod_summary_groups_by_category(patched):
    network = {"sharedDir": str(patched[1]), "members": ["admin_bot", "evolve"]}
    r = _call(patched, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Gallery — pod** (3 apps in catalog)" in body
    assert "productivity: 2" in body
    assert "personal-lifestyle: 1" in body
