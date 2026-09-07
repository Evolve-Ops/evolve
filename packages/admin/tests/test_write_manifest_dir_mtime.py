"""Tests for ``_write_manifest_as_bot`` — manifest writes must be visible
to the plugin's Layer C trigger cache.

The plugin invalidates its compiled-trigger cache on the manifests
DIRECTORY mtime (``_getManifestTriggers`` in
packages/plugin/src/observer/TurnObserver.ts). The pre-fix writer
overwrote the destination in place (``shutil.copy2`` / ``sudo cp``),
which never bumps the dir mtime — a pause/archive status flip sat
unread by a running gateway until some unrelated dir-entry change.

Contract pinned here (mirrors ``unwire_event_triggers``):

- the primary write path is same-dir temp + rename (dir-entry change →
  the gateway notices within its 5s rescan window);
- temp names are dot-prefixed so a crashed write never surfaces as a
  manifest to ``_glob_manifests`` or the plugin's scan;
- the staged-copy fallbacks still land the write when the rename path
  is unavailable (pre-scan bot without the dir-write ACL).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import server  # noqa: E402


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: the sudo /bin/cp (and sudo mkdir) branches must be
    unreachable in these tests. Every dest lives in tmp_path, so the
    direct paths always succeed — but if a refactor ever changes that,
    fail loudly instead of invoking real sudo (which passwordless-sudo
    CI runners would happily execute)."""
    import subprocess as _sp

    def _forbidden(*args, **kwargs):
        raise AssertionError(f"test invoked subprocess.run: {args[:1]}")

    monkeypatch.setattr(_sp, "run", _forbidden)


@pytest.fixture
def manifests_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point _bot_manifests_dir at a tmp dir so tests never touch /Users/<bot>/."""
    d = tmp_path / "workspace" / "manifests"
    d.mkdir(parents=True)
    monkeypatch.setattr(server, "_bot_manifests_dir", lambda bot_id, user=None: d)
    monkeypatch.setattr(server, "_resolve_bot_user", lambda bot_id: bot_id)
    return d


def _age_dir(d: Path) -> float:
    """Set the dir mtime an hour into the past; return the aged mtime."""
    past = time.time() - 3600
    os.utime(d, (past, past))
    return d.stat().st_mtime


def test_write_bumps_directory_mtime(manifests_dir: Path) -> None:
    """The plugin-visibility contract: updating an existing manifest must
    change the directory mtime (i.e. go through rename, not in-place copy)."""
    dest = manifests_dir / "research-app.json"
    dest.write_text(json.dumps({"id": "research-app", "status": "active"}))
    before = _age_dir(manifests_dir)

    ok = server._write_manifest_as_bot(
        "member-bot-a", "research-app", {"id": "research-app", "status": "paused"}
    )

    assert ok is True
    assert json.loads(dest.read_text())["status"] == "paused"
    assert manifests_dir.stat().st_mtime > before, (
        "dir mtime unchanged — the write went in place, so a running "
        "gateway's trigger cache will never see this status flip"
    )


def test_write_leaves_no_temp_files(manifests_dir: Path) -> None:
    server._write_manifest_as_bot("member-bot-a", "app", {"id": "app"})
    assert [p.name for p in manifests_dir.iterdir()] == ["app.json"]


def test_temp_names_invisible_to_manifest_glob(manifests_dir: Path) -> None:
    """A temp file orphaned by a crash mid-write must not list as a manifest."""
    (manifests_dir / ".app-abc123.tmp").write_text("{}")
    (manifests_dir / "real-app.json").write_text('{"id": "real-app"}')
    names = sorted(Path(p).name for p in server._glob_manifests(manifests_dir))
    assert names == ["real-app.json"]


def test_fallback_when_dir_mkstemp_denied(
    manifests_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-scan bot: no write ACL on the manifests dir → mkstemp raises and
    the staged-copy fallback must still land the write (in place, stale
    cache accepted)."""
    dest = manifests_dir / "app.json"
    dest.write_text(json.dumps({"id": "app", "status": "active"}))

    real_mkstemp = tempfile.mkstemp

    def deny_in_manifests_dir(*args, **kwargs):
        if str(kwargs.get("dir", "")) == str(manifests_dir):
            raise PermissionError("no ACL yet")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", deny_in_manifests_dir)

    ok = server._write_manifest_as_bot(
        "member-bot-a", "app", {"id": "app", "status": "paused"}
    )

    assert ok is True
    assert json.loads(dest.read_text())["status"] == "paused"
    assert [p.name for p in manifests_dir.iterdir()] == ["app.json"]


def test_fallback_when_rename_fails(
    manifests_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename failure after a successful temp write: the temp file is
    cleaned up and the staged-copy fallback still lands the write."""
    dest = manifests_dir / "app.json"
    dest.write_text(json.dumps({"id": "app", "status": "active"}))

    real_replace = os.replace

    def deny_into_manifests_dir(src, dst, **kwargs):
        if str(dst).startswith(str(manifests_dir)):
            raise PermissionError("simulated rename failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", deny_into_manifests_dir)

    ok = server._write_manifest_as_bot(
        "member-bot-a", "app", {"id": "app", "status": "paused"}
    )

    assert ok is True
    assert json.loads(dest.read_text())["status"] == "paused"
    assert [p.name for p in manifests_dir.iterdir()] == ["app.json"]
