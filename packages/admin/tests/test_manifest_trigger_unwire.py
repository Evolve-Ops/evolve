"""Tests for ``unwire_event_triggers`` — uninstall-time trigger unregistration.

Base-spec §8.4 step 3 (internal/spec-manifest-v7-2026-05-20.md), landed via
manifest-v7 Slice 1 (internal/spec-manifest-v7-slicing-2026-06-10.md §3.2).

The plugin's Layer C compiles ``event_triggers[]`` straight from the bot's
``manifests/`` directory and invalidates its cache on the DIRECTORY mtime.
Two properties matter beyond the field edit itself:

- the rewrite must go through rename (dir-entry change) so a running
  gateway notices within its 5s rescan window;
- it must run before file unlink in the uninstall sequence, so the
  failed-delete resume window can't fire triggers at deleted scripts.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import manifest as manifest_mod  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    delete_manifest,
    unwire_event_triggers,
)


@pytest.fixture()
def manifests_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point applications_dir at a tmp manifests dir for a fake bot."""
    d = tmp_path / "workspace" / "manifests"
    d.mkdir(parents=True)
    monkeypatch.setattr(
        manifest_mod, "applications_dir", lambda shared_dir, bot_id: d
    )
    return d


def _write_app(manifests_dir: Path, app_id: str, **overrides) -> Path:
    data = {
        "id": app_id,
        "bot_id": "member-bot-a",
        "name": app_id,
        "pkg_id": "p-test0001",
        "schema_version": 22,
        "invocation_mode": "plugin_intercept",
        "event_triggers": [
            {
                "id": "t1",
                "match": {"channel": "any", "pattern": "^!research\\b"},
                "invocation": {
                    "script": "scripts/research.py",
                    "stdout_protocol": "atlas_research",
                    "on_failure": "silent",
                },
            }
        ],
        "status": "active",
    }
    data.update(overrides)
    path = manifests_dir / f"{app_id}.json"
    path.write_text(json.dumps(data))
    return path


def test_unwire_clears_triggers_and_downgrades_mode(manifests_dir: Path) -> None:
    path = _write_app(manifests_dir, "research-app")
    result = unwire_event_triggers("research-app", "member-bot-a", Path("/unused"))
    assert result["ok"] is True
    assert result["unwired"] is True
    assert result["trigger_count"] == 1

    data = json.loads(path.read_text())
    assert data["event_triggers"] == []
    assert data["invocation_mode"] == "agent_invokes"
    # Only the trigger wiring changes — everything else survives verbatim.
    assert data["pkg_id"] == "p-test0001"
    assert data["status"] == "active"


def test_unwire_bumps_directory_mtime(manifests_dir: Path) -> None:
    """The plugin's trigger cache is keyed on the manifests-DIR mtime; an
    in-place write never bumps it, so the unwire must land via rename."""
    _write_app(manifests_dir, "research-app")
    before = manifests_dir.stat().st_mtime_ns
    time.sleep(0.02)
    result = unwire_event_triggers("research-app", "member-bot-a", Path("/unused"))
    assert result["unwired"] is True
    assert manifests_dir.stat().st_mtime_ns > before


def test_unwire_noop_when_nothing_wired(manifests_dir: Path) -> None:
    path = _write_app(
        manifests_dir, "plain-app",
        event_triggers=[], invocation_mode="agent_invokes",
    )
    before = path.stat().st_mtime_ns
    result = unwire_event_triggers("plain-app", "member-bot-a", Path("/unused"))
    assert result == {"ok": True, "unwired": False, "trigger_count": 0}
    # No rewrite on the no-op path.
    assert path.stat().st_mtime_ns == before


def test_unwire_clears_triggers_even_in_agent_invokes_mode(manifests_dir: Path) -> None:
    # agent_invokes manifests get no Layer C interception, but the audit
    # daemon still matches against event_triggers[] — uninstall clears
    # them regardless of mode.
    path = _write_app(manifests_dir, "audited-app", invocation_mode="agent_invokes")
    result = unwire_event_triggers("audited-app", "member-bot-a", Path("/unused"))
    assert result["unwired"] is True
    data = json.loads(path.read_text())
    assert data["event_triggers"] == []
    assert data["invocation_mode"] == "agent_invokes"


def test_unwire_missing_manifest(manifests_dir: Path) -> None:
    result = unwire_event_triggers("no-such-app", "member-bot-a", Path("/unused"))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_unwire_reports_unparseable_manifest(manifests_dir: Path) -> None:
    (manifests_dir / "broken-app.json").write_text("{not json")
    result = unwire_event_triggers("broken-app", "member-bot-a", Path("/unused"))
    assert result["ok"] is False
    assert "could not read" in result["error"]


def test_delete_manifest_unwires_before_finalize(
    manifests_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot delete path runs the unwire first and reports it."""
    _write_app(manifests_dir, "research-app")

    observed: dict = {}
    real_finalize = manifest_mod.finalize_manifest_deletion

    def _spy_finalize(application_id: str, bot_id: str, shared_dir: Path) -> dict:
        # At finalize time the manifest must already be unwired on disk.
        data = json.loads((manifests_dir / f"{application_id}.json").read_text())
        observed["triggers_at_finalize"] = data["event_triggers"]
        return real_finalize(application_id, bot_id, shared_dir)

    monkeypatch.setattr(manifest_mod, "finalize_manifest_deletion", _spy_finalize)

    result = delete_manifest("research-app", "member-bot-a", Path("/unused"))
    assert result["ok"] is True
    assert result["triggers_unwired"]["unwired"] is True
    assert observed["triggers_at_finalize"] == []
    assert not (manifests_dir / "research-app.json").exists()
