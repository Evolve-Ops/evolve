"""``evolve-admin application migrate-ids`` — the AL-1.4a census + backfill.

docs/build-AL-1.4-app-id-canonical.md §2. The table at
``{shared_dir}/apps/id-migration.json`` has no consumer in 1.4a; it exists so
1.4c can drop the legacy resolution fallback having first PROVED what still
depends on it. So the tests here are mostly about the census being honest:
which artifacts got a conforming ``app_id``, which could not, and — for v7-arc
Instances — which ``spec_id`` AL-1.5/1.4c will collapse their identity onto.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import id_migration  # noqa: E402
from evolve_admin.applications.id_migration import (  # noqa: E402
    KIND_INSTANCE,
    KIND_MANIFEST,
    KIND_SPEC,
    STATUS_ALREADY,
    STATUS_DRAFT,
    STATUS_NON_CONFORMING,
    STATUS_STAMPED,
    build_report,
    migration_table_path,
)

BOT = "atlas"


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    """A shared_dir + a bot manifests dir, both under tmp_path.

    ``applications_dir`` resolves to the BOT's home, not shared_dir, so the
    workspace lookup is redirected rather than assumed.
    """
    shared = tmp_path / "shared"
    workspace = tmp_path / "botws"
    (workspace / "manifests").mkdir(parents=True)
    import evolve_admin.config as cfg
    monkeypatch.setattr(cfg, "get_bot_workspace", lambda bot_id, user=None: workspace)
    return shared, workspace / "manifests"


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def _by_kind(report, kind):
    return [e for e in report.entries if e.kind == kind]


def test_dry_run_reports_without_touching_anything(pod) -> None:
    shared, caps = pod
    path = _write(caps / "morning-brief.json",
                  {"id": "morning-brief", "bot_id": BOT})
    before = path.read_bytes()

    report = build_report(shared, [BOT])

    assert report.dry_run is True
    assert [e.status for e in report.entries] == [STATUS_STAMPED]
    assert report.entries[0].app_id == "morning-brief"
    assert path.read_bytes() == before
    assert not migration_table_path(shared).exists()


def test_apply_stamps_and_writes_the_table(pod) -> None:
    shared, caps = pod
    _write(caps / "morning-brief.json", {"id": "morning-brief", "bot_id": BOT})
    _write(shared / "gallery" / "local" / "p-a3f91c8b" / "2026.08.17-1.0.json",
           {"spec_id": "p-a3f91c8b", "name": "Morning Brief"})

    report = build_report(shared, [BOT], apply=True)

    assert json.loads((caps / "morning-brief.json").read_text())["app_id"] \
        == "morning-brief"
    table = json.loads(migration_table_path(shared).read_text())
    assert table["version"] == id_migration.MIGRATION_TABLE_VERSION
    assert table["map"] == {
        "morning-brief": "morning-brief", "p-a3f91c8b": "p-a3f91c8b",
    }
    assert {e["kind"] for e in table["entries"]} == {KIND_MANIFEST, KIND_SPEC}


def test_the_map_is_an_identity_map_by_construction(pod) -> None:
    """1.4a cannot renumber anything and stay behavior-neutral.

    Pinned deliberately: if a later change makes ``map`` non-identity in 1.4a,
    some manifest's resolved id moved — which is exactly the failure this
    stage is built to avoid. The census in ``entries``, not the map, is what
    1.4c actually consumes.
    """
    shared, caps = pod
    _write(caps / "a.json", {"id": "app-a", "pkg_id": "p-a3f91c8b"})
    _write(caps / "b.json", {"id": "app-b"})
    report = build_report(shared, [BOT])
    assert all(legacy == app for legacy, app in report.mapping.items())


def test_apply_is_idempotent(pod) -> None:
    shared, caps = pod
    path = _write(caps / "morning-brief.json", {"id": "morning-brief"})

    build_report(shared, [BOT], apply=True)
    stamped = path.read_bytes()
    second = build_report(shared, [BOT], apply=True)

    assert path.read_bytes() == stamped
    assert second.written == []
    assert [e.status for e in second.entries] == [STATUS_ALREADY]


def test_non_conforming_ids_are_reported_as_blocking(pod) -> None:
    """These are precisely what stops 1.4c from dropping the fallback."""
    shared, caps = pod
    _write(caps / "UPPER.json", {"id": "UPPER-Case"})
    _write(caps / "ok.json", {"id": "fine-app"})

    report = build_report(shared, [BOT], apply=True)

    blocking = report.blocking
    assert [e.status for e in blocking] == [STATUS_NON_CONFORMING]
    assert blocking[0].legacy_id == "UPPER-Case"
    assert "app_id" not in json.loads((caps / "UPPER.json").read_text())


def test_a_draft_is_reported_as_a_draft_not_stamped(pod) -> None:
    """Design §3: a discovered draft must not acquire an app_id here either.

    The draft carries an ``id`` too — it is the filename stem — so a census
    that resolved the legacy chain first would "helpfully" stamp every draft
    on the pod and quietly reverse the mint's decision not to confer identity.
    """
    shared, caps = pod
    _write(caps / "maybe-an-app.json",
           {"id": "maybe-an-app", "draft_id": "draft-0123456789ab",
            "definition_status": "discovered"})

    report = build_report(shared, [BOT], apply=True)

    assert [e.status for e in report.entries] == [STATUS_DRAFT]
    assert "app_id" not in json.loads((caps / "maybe-an-app.json").read_text())
    assert report.blocking == []  # a draft is correct, not a 1.4c blocker


def test_instance_rows_carry_the_spec_id_1_4c_needs(pod) -> None:
    """AL-1.5/1.4c collapse instance identity onto the spec's app_id.

    1.4a deliberately does NOT: rewriting a v7-arc Instance's id would change
    what every resolver returns for it today. Recording the binding is how the
    later stage gets to do it from data instead of guesswork.
    """
    shared, caps = pod
    _write(caps / "morning-brief.json", {
        "instance_id": "morning-brief",
        "manifest_shape": "v7-arc",
        "provenance": {"spec_id": "p-a3f91c8b", "spec_version": "2026.08.17-1.0"},
    })

    report = build_report(shared, [BOT])

    (entry,) = _by_kind(report, KIND_INSTANCE)
    assert entry.app_id == "morning-brief"
    assert entry.spec_id == "p-a3f91c8b"


def test_scanner_state_and_history_files_are_skipped(pod) -> None:
    """``.scan-status.json`` and ``_history/`` are not manifests."""
    shared, caps = pod
    _write(caps / ".scan-status.json", {"id": "not-a-manifest"})
    _write(caps / "_history" / "old.json", {"id": "archived"})
    _write(caps / "real.json", {"id": "real-app"})

    report = build_report(shared, [BOT])

    assert [e.legacy_id for e in report.entries] == ["real-app"]


def test_gallery_tiers_are_all_walked(pod) -> None:
    shared, _caps = pod
    gallery = shared / "gallery"
    _write(gallery / "local" / "p-aaaaaaaa" / "2026.08.17-1.0.json",
           {"spec_id": "p-aaaaaaaa"})
    _write(gallery / "builtin" / "p-bbbbbbbb" / "2026.08.17-1.0.json",
           {"spec_id": "p-bbbbbbbb"})
    _write(gallery / "imported" / "pod-x" / "p-cccccccc" / "2026.08.17-1.0.json",
           {"spec_id": "p-cccccccc"})
    # Flat legacy imported files are gallery *packages*, not Specs.
    _write(gallery / "imported" / "p-dddddddd.json", {"pkg_id": "p-dddddddd"})

    report = build_report(shared, [])

    assert sorted(e.legacy_id for e in report.entries) == [
        "p-aaaaaaaa", "p-bbbbbbbb", "p-cccccccc",
    ]


def test_unreadable_artifact_is_an_error_not_a_crash(pod) -> None:
    shared, caps = pod
    (caps / "broken.json").write_text("{not json")
    _write(caps / "ok.json", {"id": "fine-app"})

    report = build_report(shared, [BOT])

    assert len(report.errors) == 1
    assert "broken.json" in report.errors[0]
    assert [e.legacy_id for e in report.entries] == ["fine-app"]
