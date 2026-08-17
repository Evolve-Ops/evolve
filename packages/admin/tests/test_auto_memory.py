"""tests/test_auto_memory.py — Phase A scanner + inventory endpoint.

Tier 2.4 of the OpenClaw admin coverage roadmap. Phase A is
inventory only: walk each bot's ``~/.openclaw/workspace/memory/``
and stat the sibling ``~/.openclaw/memory/main.sqlite`` index DB.

Tests use ``tmp_path`` to simulate bot home directories — no real
ACL / sudo dance needed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _make_bot_home(tmp_path: Path, bot_id: str) -> Path:
    home = tmp_path / "Users" / bot_id
    home.mkdir(parents=True)
    return home


def _seed_memory(
    bot_home: Path,
    files: dict[str, str],
    *,
    mtime: float | None = None,
) -> Path:
    """Create ``<home>/.openclaw/workspace/memory/`` with files.

    Keys with ``/`` create nested files (e.g. ``articles/x.md``).
    """
    mem = bot_home / ".openclaw" / "workspace" / "memory"
    mem.mkdir(parents=True)
    for rel, content in files.items():
        f = mem / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        if mtime is not None:
            os.utime(f, (mtime, mtime))
    return mem


def _seed_index_db(bot_home: Path, content: bytes = b"sqlite-stub") -> Path:
    """Create a stub ``~/.openclaw/memory/main.sqlite`` for size/mtime tests."""
    idx = bot_home / ".openclaw" / "memory" / "main.sqlite"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(content)
    return idx


# ── scan_bot — happy path ────────────────────────────────────────────────────


def test_scan_bot_no_memory_dir_returns_empty_inventory(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.bot_id == "team_bot_a"
    assert inv.bot_user == "team_bot_a"
    assert inv.memory_dir_exists is False
    assert inv.file_count == 0
    assert inv.total_bytes == 0
    assert inv.index_db_size_bytes == 0
    assert inv.error == ""


def test_scan_bot_aggregates_top_level_files(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    _seed_memory(
        home,
        {
            "MEMORY.md": "index",
            "2026-03-13.md": "daily entry",
            "feedback_one.md": "feedback content",
        },
    )
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.memory_dir_exists is True
    assert inv.file_count == 3
    assert inv.total_bytes > 0


def test_scan_bot_recurses_into_subdirectories(tmp_path):
    """OC's memory dir contains topical subdirs (articles/, .dreams/)
    that the FTS index treats as part of the corpus. We mirror that."""
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    _seed_memory(
        home,
        {
            "MEMORY.md": "index",
            "articles/sarver-chief-of-staff.md": "article body",
            ".dreams/2026-05-16.md": "dream",
        },
    )
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.file_count == 3


def test_scan_bot_records_oldest_newest_modified(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    mem = _seed_memory(home, {"old.md": "o", "new.md": "n"})
    old_t = time.time() - 86400 * 30
    new_t = time.time() - 10
    os.utime(mem / "old.md", (old_t, old_t))
    os.utime(mem / "new.md", (new_t, new_t))

    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.oldest_modified_at < inv.newest_modified_at
    assert inv.oldest_modified_at
    assert inv.newest_modified_at


def test_scan_bot_handles_empty_memory_dir(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    (home / ".openclaw" / "workspace" / "memory").mkdir(parents=True)
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.memory_dir_exists is True
    assert inv.file_count == 0
    assert inv.total_bytes == 0
    assert inv.oldest_modified_at == ""
    assert inv.newest_modified_at == ""


# ── scan_bot — index DB ──────────────────────────────────────────────────────


def test_scan_bot_reports_index_db_size_and_mtime(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    _seed_index_db(home, content=b"x" * 1024)
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.index_db_size_bytes == 1024
    assert inv.index_db_modified_at
    assert inv.index_db_path.endswith(".openclaw/memory/main.sqlite")


def test_scan_bot_missing_index_db_reports_zero(tmp_path):
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    _seed_memory(home, {"MEMORY.md": "x"})
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.index_db_size_bytes == 0
    assert inv.index_db_modified_at == ""
    assert inv.index_db_path  # path still reported for transparency


def test_scan_bot_index_db_independent_of_memory_dir(tmp_path):
    """An index DB can exist without workspace/memory/ (and vice versa)."""
    from evolve_admin import auto_memory

    home = _make_bot_home(tmp_path, "team_bot_a")
    _seed_index_db(home, content=b"y" * 42)
    inv = auto_memory.scan_bot("team_bot_a", bot_user="team_bot_a", home=home)
    assert inv.memory_dir_exists is False
    assert inv.index_db_size_bytes == 42


# ── scan_pod ─────────────────────────────────────────────────────────────────


def test_scan_pod_walks_every_member(tmp_path, monkeypatch):
    from evolve_admin import auto_memory

    home_a = _make_bot_home(tmp_path, "bot_a")
    home_b = _make_bot_home(tmp_path, "bot_b")
    _seed_memory(home_a, {"MEMORY.md": "a"})
    _seed_memory(home_b, {"MEMORY.md": "b" * 100, "feedback_t.md": "t" * 50})
    _seed_index_db(home_a, content=b"i" * 200)

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda b, n: tmp_path / "Users" / b,
    )
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_user", lambda b, n: b
    )

    network = {"members": ["bot_a", "bot_b"], "bots": {}}
    pod = auto_memory.scan_pod(network)
    assert len(pod["bots"]) == 2
    assert pod["pod_total_files"] == 3
    assert pod["pod_total_bytes"] > 0
    assert pod["pod_index_db_bytes"] == 200
    assert pod["scanned_at"]


def test_scan_pod_empty_members(tmp_path):
    from evolve_admin import auto_memory

    pod = auto_memory.scan_pod({"members": [], "bots": {}})
    assert pod["bots"] == []
    assert pod["pod_total_files"] == 0
    assert pod["pod_total_bytes"] == 0
    assert pod["pod_index_db_bytes"] == 0


# ── HTTP endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network = {
        "members": ["bot_a"],
        "bots": {"bot_a": {"user": "bot_a", "port": 1234}},
        "sharedDir": str(shared_dir),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    home_a = _make_bot_home(tmp_path, "bot_a")
    _seed_memory(home_a, {"MEMORY.md": "x", "feedback_t.md": "t" * 200})
    _seed_index_db(home_a, content=b"z" * 500)

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda b, n: tmp_path / "Users" / b,
    )
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_user", lambda b, n: b
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def test_inventory_endpoint_returns_pod_summary(app):
    client = app.test_client()
    resp = client.get("/api/auto-memory/inventory")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["pod_total_files"] == 2
    assert d["pod_index_db_bytes"] == 500
    assert len(d["bots"]) == 1
    bot = d["bots"][0]
    assert bot["bot_id"] == "bot_a"
    assert bot["memory_dir_exists"] is True
    assert bot["file_count"] == 2
    assert bot["index_db_size_bytes"] == 500


def test_inventory_endpoint_handles_empty_pod(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps({"members": [], "bots": {}, "sharedDir": str(shared_dir)})
    )
    app = create_app(network_path)
    app.config["TESTING"] = True
    resp = app.test_client().get("/api/auto-memory/inventory")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["bots"] == []
    assert d["pod_total_files"] == 0
    assert d["pod_index_db_bytes"] == 0
