"""``evolve-admin application migrate-specs`` — the AL-1.5a readiness census.

internal/build-AL-1.5-spec-vnext.md §2. The census answers two questions and the
tests here are about it answering them HONESTLY:

  * which artifacts derive as a v-next Spec, and which cannot — ``blocked`` is
    the gate on AL-3.1 (publish then install elsewhere);
  * what the frozen design-§5 field list would LOSE — ``no_home`` for real
    content with no §5 field, ``unclassified`` for a key nobody has decided
    about at all.

And one invariant that is load-bearing for the whole chip: **the census
rewrites nothing**. v-next is migrate-on-read, so ``--apply`` writes the table
and touches no manifest, Instance or gallery Spec — still true in AL-1.5b,
which added a third question the census has to answer honestly:

  * how much of the pod is v-next ALREADY (``shape``), versus how much the
    reader migrated on the way past.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.spec_migration import (  # noqa: E402
    CENSUS_TABLE_VERSION,
    DISPOSITION_ENVELOPE,
    DISPOSITION_NO_HOME,
    FIELD_DISPOSITION,
    KIND_APP_SPEC,
    KIND_INSTANCE,
    KIND_MANIFEST,
    KIND_SPEC,
    SHAPE_LEGACY,
    SHAPE_VNEXT,
    STATUS_BLOCKED,
    STATUS_CLEAN,
    STATUS_DRAFT,
    STATUS_PARTIAL,
    build_report,
    census_table_path,
)

BOT = "atlas"


@pytest.fixture
def pod(tmp_path: Path, monkeypatch):
    """A shared_dir + a bot manifests dir, both under tmp_path.

    ``applications_dir`` resolves to the BOT's home, not shared_dir, so the
    workspace lookup is redirected rather than assumed (same shape as
    test_id_migration's fixture — the two census the same population).
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


def _repo_pack() -> tuple[str, set[str]]:
    """``(pkg_id, {path…})`` of a real files-pack in the repo gallery.

    A "complete" manifest has to be complete about ``package.files`` too — an
    app with no files has nothing for a deterministic install to materialize
    (design §6), so the fixture is keyed on a real pack rather than declaring
    ``files: []`` and reading clean vacuously. That vacuity is exactly what
    the live-pod census caught: 39 rows read ``clean`` only because they had
    nothing to check.
    """
    repo_gallery = _ADMIN_DIR.parent.parent / "gallery"
    packs = sorted(repo_gallery.glob("*/files/manifest.json"))
    assert packs, ("no files-pack in the repo gallery — the sha-verified "
                   "install path design §6 makes 'the only path' has no fixture")
    app_dir = packs[0].parent.parent
    pkg_id = sorted(p.stem for p in app_dir.glob("p-*.json"))[0]
    paths = {f["path"] for f in json.loads(packs[0].read_text())["files"]}
    return pkg_id, paths


def _complete_manifest(**over) -> dict:
    base = {
        "pkg_id": _repo_pack()[0],
        "app_id": "morning-brief",
        "id": "morning-brief",
        "name": "Morning Brief",
        "bot_id": BOT,
        "purpose": "Sends you a morning summary.",
        "definition_status": "defined",
        "created_at": "2026-05-01T09:00:00Z",
        "scheduled_actions": [{
            "id": "brief",
            "trigger": {"schedule": "0 7 * * *"},
            "install": {"command": "python3 scripts/brief.py"},
            "delivery_contract": {"user_facing": True},
        }],
        "files": [],
    }
    base.update(over)
    return base


def _entry(report, app_id: str):
    return next(e for e in report.entries if e.app_id == app_id)


# ── the census verdicts ──────────────────────────────────────────────────────

def test_a_complete_manifest_is_clean(pod) -> None:
    """Complete means installable: a conforming identity, a purpose, and a
    sha-verified package."""
    shared, manifests = pod
    _write(manifests / "morning-brief.json", _complete_manifest())
    report = build_report(shared, [BOT])
    entry = _entry(report, "morning-brief")
    assert entry.status == STATUS_CLEAN
    assert entry.problems == []
    assert entry.kind == KIND_MANIFEST


def test_a_manifest_with_no_package_files_is_partial_not_clean(pod) -> None:
    """The live-pod census (2026-08-18) read 39 artifacts as ``clean`` purely
    because they declared no files — "nothing to check" rendered as "ready".
    An empty package is now a stated problem, so ``clean`` means installable."""
    shared, manifests = pod
    _write(manifests / "nofiles.json", _complete_manifest(
        app_id="nofiles", id="nofiles", pkg_id="", files=[]))
    entry = _entry(build_report(shared, [BOT]), "nofiles")
    assert entry.status == STATUS_PARTIAL
    assert any("package.files is empty" in p for p in entry.problems)


def test_an_incomplete_manifest_is_partial_with_its_reasons(pod) -> None:
    shared, manifests = pod
    _write(manifests / "half.json", {
        "app_id": "half", "id": "half", "name": "Half", "bot_id": BOT,
    })
    entry = _entry(build_report(shared, [BOT]), "half")
    assert entry.status == STATUS_PARTIAL
    assert any("purpose is empty" in p for p in entry.problems)


def test_a_draft_is_its_own_status_not_a_failure(pod) -> None:
    """design §3: a discovered draft has no identity to confer. That is the
    scanner holding the line, so it must not read as blocked."""
    shared, manifests = pod
    _write(manifests / "draft-x.json", {
        "id": "draft-x", "draft_id": "draft-abc12345",
        "definition_status": "discovered", "name": "Draft X", "bot_id": BOT,
    })
    report = build_report(shared, [BOT])
    assert report.entries[0].status == STATUS_DRAFT
    assert report.entries[0].app_id == ""
    assert report.blocking == []


def test_an_identityless_non_draft_blocks(pod) -> None:
    """No conforming id and no draft_id: it cannot be published or granted
    against, which is what AL-3.1 is gated on."""
    shared, manifests = pod
    _write(manifests / "nameless.json", {"name": "Nameless", "bot_id": BOT})
    report = build_report(shared, [BOT])
    assert report.entries[0].status == STATUS_BLOCKED
    assert len(report.blocking) == 1


def test_gallery_specs_and_v7_instances_are_censused_too(pod) -> None:
    """The population is manifests + v7-arc Instances + gallery Specs across
    every tier — the same set migrate-ids walks."""
    shared, manifests = pod
    _write(manifests / "inst.json", {
        "app_id": "inst", "instance_id": "i-1234abcd", "manifest_shape": "v7-arc",
        "name": "Inst", "bot_id": BOT, "purpose": "Does a thing.",
    })
    _write(shared / "gallery" / "local" / "weekly" / "2026.06.01-1.0.json", {
        "app_id": "weekly", "spec_id": "weekly", "name": "Weekly",
        "objective": {"primary": "Summarize the week."},
        "spec_version": "2026.06.01-1.0",
    })
    _write(shared / "gallery" / "imported" / "pod-b" / "shared-app" / "2026.05.01-1.0.json", {
        "app_id": "shared-app", "spec_id": "shared-app", "name": "Shared",
        "objective": {"primary": "Came from elsewhere."},
    })
    report = build_report(shared, [BOT])
    kinds = {e.app_id: e.kind for e in report.entries}
    assert kinds == {"inst": KIND_INSTANCE, "weekly": KIND_SPEC,
                     "shared-app": KIND_SPEC}
    assert _entry(report, "weekly").spec_version == 2026060110
    assert _entry(report, "weekly").legacy_spec_version == "2026.06.01-1.0"


def test_an_unreadable_artifact_is_an_error_not_a_crash(pod) -> None:
    shared, manifests = pod
    _write(manifests / "ok.json", _complete_manifest())
    (manifests / "broken.json").write_text("{not json")
    report = build_report(shared, [BOT])
    assert len(report.errors) == 1
    assert "broken.json" in report.errors[0]
    assert len(report.entries) == 1          # the census continued


# ── what the frozen field list loses ─────────────────────────────────────────

def test_no_home_is_reported_only_when_the_field_is_populated(pod) -> None:
    """The output has to be "what would really be lost on THIS pod", not a
    restatement of the schema."""
    shared, manifests = pod
    # NB conforming slugs: resolve_app_id only honors the app_id FIELD when it
    # matches APP_ID_PATTERN (3-48 chars), else it falls through to the legacy
    # chain — so a one-letter test id would silently resolve to pkg_id.
    _write(manifests / "a.json", _complete_manifest(
        app_id="app-a", id="app-a", build_spec="# How to build it",
        tags=[], goals=[]))
    entry = _entry(build_report(shared, [BOT]), "app-a")
    assert "build_spec" in entry.no_home
    assert "tags" not in entry.no_home       # present but empty
    assert "goals" not in entry.no_home


def test_no_home_counts_aggregate_across_the_pod(pod) -> None:
    shared, manifests = pod
    _write(manifests / "a.json", _complete_manifest(
        app_id="app-a", id="app-a", build_spec="x", inputs=["mail"]))
    _write(manifests / "b.json", _complete_manifest(
        app_id="app-b", id="app-b", build_spec="y"))
    counts = build_report(shared, [BOT]).no_home_counts
    assert counts["build_spec"] == 2
    assert counts["inputs"] == 1
    assert list(counts) == sorted(counts, key=lambda k: (-counts[k], k))


def test_an_undecided_key_surfaces_as_unclassified(pod) -> None:
    """The one outcome that must never pass silently: a field nobody has
    assigned a disposition to."""
    shared, manifests = pod
    _write(manifests / "a.json", _complete_manifest(some_future_field="?"))
    report = build_report(shared, [BOT])
    assert report.entries[0].unclassified == ["some_future_field"]
    assert report.unclassified_counts == {"some_future_field": 1}


def test_every_manifest_dataclass_field_has_a_disposition() -> None:
    """design §5 defers the per-field migration note to this chip.
    FIELD_DISPOSITION is that note, and it has to be complete — otherwise the
    'unclassified' signal is just noise from an unfinished table."""
    import dataclasses
    from evolve_admin.applications.manifest import ApplicationManifest
    missing = [f.name for f in dataclasses.fields(ApplicationManifest)
               if f.name not in FIELD_DISPOSITION]
    assert missing == []


def test_every_v7_schema_property_has_a_disposition() -> None:
    """The other two artifact shapes on the pod — the v7-arc Spec and
    Instance — are covered too, so the census is complete for all three."""
    schemas_dir = _ADMIN_DIR.parent.parent / "docs" / "schemas"
    for name in ("manifest-v7-spec", "manifest-v7-instance"):
        props = json.loads((schemas_dir / f"{name}.schema.json").read_text())["properties"]
        assert [k for k in props if k not in FIELD_DISPOSITION] == [], name


def test_no_home_fields_are_not_also_claimed_by_a_spec_field() -> None:
    """A field cannot both feed §5 and have no home — that contradiction
    would make the gap measurement meaningless."""
    no_home = {k for k, v in FIELD_DISPOSITION.items() if v == DISPOSITION_NO_HOME}
    assert no_home and not (no_home & {"app_id", "name", "purpose", "files"})


# ── the invariant: nothing on disk changes ───────────────────────────────────

def test_apply_writes_the_table_and_rewrites_nothing(pod) -> None:
    shared, manifests = pod
    manifest_path = _write(manifests / "morning-brief.json", _complete_manifest())
    spec_path = _write(
        shared / "gallery" / "local" / "weekly" / "2026.06.01-1.0.json",
        {"app_id": "weekly", "spec_id": "weekly", "name": "Weekly",
         "objective": {"primary": "Summarize the week."}})
    before = {p: p.read_bytes() for p in (manifest_path, spec_path)}

    report = build_report(shared, [BOT], apply=True)

    assert {p: p.read_bytes() for p in before} == before, \
        "migrate-specs must not rewrite any artifact — v-next is migrate-on-read"
    table = json.loads(census_table_path(shared).read_text())
    # v2 (AL-1.5b) added the per-row `shape` column and `shape_counts`.
    assert table["version"] == CENSUS_TABLE_VERSION
    assert table["counts"][STATUS_CLEAN] == 1        # the packaged manifest
    assert table["counts"][STATUS_PARTIAL] == 1     # the Spec declares no files
    assert len(table["entries"]) == 2
    assert report.dry_run is False


def test_dry_run_writes_nothing_at_all(pod) -> None:
    shared, manifests = pod
    _write(manifests / "morning-brief.json", _complete_manifest())
    report = build_report(shared, [BOT])
    assert not census_table_path(shared).exists()
    assert report.table_path == str(census_table_path(shared))
    assert report.dry_run is True


def test_the_census_is_idempotent(pod) -> None:
    """Two runs over an unchanged pod produce the same verdicts — a census
    that drifts is not evidence of anything."""
    shared, manifests = pod
    _write(manifests / "morning-brief.json", _complete_manifest())
    _write(manifests / "half.json", {"app_id": "half", "id": "half",
                                     "name": "Half", "bot_id": BOT})
    first = build_report(shared, [BOT], apply=True)
    second = build_report(shared, [BOT], apply=True)
    assert [e.to_dict() for e in first.entries] == \
        [e.to_dict() for e in second.entries]


# ── package.files sha256 — the deterministic-install evidence ────────────────

def test_the_census_resolves_shas_from_the_repo_files_pack(pod) -> None:
    """``package.files[].sha256`` lives in the files-pack metadata, never in
    the manifest. The census has to go and get it, or every artifact on the
    pod reads ``partial`` for one uniform missing-sha complaint and the
    signal is gone.

    Deliberately run against a REAL pack in the repo gallery rather than a
    mock: the lookup crosses two modules and a directory convention, and a
    mocked pack would pass while the convention drifted.
    """
    pkg_id, pack_paths = _repo_pack()

    shared, manifests = pod
    _write(manifests / "packed.json", _complete_manifest(
        app_id="packed", id="packed", pkg_id=pkg_id, files=[]))
    entry = _entry(build_report(shared, [BOT]), "packed")

    assert entry.status == STATUS_CLEAN
    assert not any("sha256" in p for p in entry.problems)

    from evolve_admin.applications.app_spec import spec_from_manifest
    # AL-1.5b moved this out of spec_migration: the writer needs the same
    # answer, and two derivations that disagree about sha256 would make a
    # written Spec less installable than the artifact it came from.
    from evolve_admin.applications.app_spec_store import (
        resolve_package_files as _resolve_package_files,
    )
    spec = spec_from_manifest(
        {"app_id": "packed", "name": "Packed", "purpose": "x.", "pkg_id": pkg_id},
        package_files=_resolve_package_files({"pkg_id": pkg_id}))
    assert {f["path"] for f in spec.package["files"]} == pack_paths
    assert all(len(f["sha256"]) == 64 for f in spec.package["files"])


def test_a_missing_files_pack_degrades_to_no_pack_not_an_error(pod) -> None:
    shared, manifests = pod
    _write(manifests / "nopack.json", _complete_manifest(
        app_id="nopack", id="nopack", pkg_id="p-doesnotexist",
        files=[{"path": "scripts/x.py", "purpose": "main"}]))
    entry = _entry(build_report(shared, [BOT]), "nopack")
    assert entry.status == STATUS_PARTIAL
    assert any("sha256" in p for p in entry.problems)


# ── AL-1.5b: the census learns the v-next shape ──────────────────────────────

def _write_vnext(shared: Path, manifest: dict):
    """Land a v-next Spec through the real writer, not a hand-rolled dict —
    a census test that invents its own envelope would pass while the writer
    and the reader disagreed."""
    from evolve_admin.applications.app_spec_store import (
        spec_from_artifact, write_spec,
    )
    return write_spec(spec_from_artifact(manifest), shared)


def test_a_written_vnext_spec_is_censused(pod) -> None:
    """The writer's output is part of the pod population — otherwise the
    artifact this whole arc produces is the one thing the census cannot see."""
    shared, manifests = pod
    _write_vnext(shared, _complete_manifest())
    report = build_report(shared, [BOT])
    entry = _entry(report, "morning-brief")
    assert entry.kind == KIND_APP_SPEC
    assert entry.shape == SHAPE_VNEXT


def test_shape_separates_v_next_from_migrated_on_read(pod) -> None:
    """"How much of this pod is v-next yet?" has to be answerable — that is
    the number AL-1.5c's rollout is measured against."""
    shared, manifests = pod
    _write(manifests / "legacy-one.json",
           _complete_manifest(app_id="legacy-one", id="legacy-one"))
    _write(manifests / "legacy-two.json",
           _complete_manifest(app_id="legacy-two", id="legacy-two"))
    _write_vnext(shared, _complete_manifest())

    report = build_report(shared, [BOT])
    assert report.shape_counts == {SHAPE_VNEXT: 1, SHAPE_LEGACY: 2}
    assert {e.app_id: e.shape for e in report.entries} == {
        "legacy-one": SHAPE_LEGACY,
        "legacy-two": SHAPE_LEGACY,
        "morning-brief": SHAPE_VNEXT,
    }


def test_shape_counts_report_zero_rather_than_omitting_the_key(pod) -> None:
    """A missing key reads as "not measured"; a zero is an answer."""
    shared, manifests = pod
    _write(manifests / "morning-brief.json", _complete_manifest())
    assert build_report(shared, [BOT]).shape_counts == {
        SHAPE_VNEXT: 0, SHAPE_LEGACY: 1}


def test_shape_is_read_from_the_artifact_not_from_the_directory(pod) -> None:
    """A legacy manifest that lands in the v-next directory is still legacy.
    Keying on the walk instead of the discriminator would make the census
    report what it expected to find rather than what is there."""
    shared, manifests = pod
    _write(shared / "apps" / "specs" / "impostor.json",
           _complete_manifest(app_id="impostor", id="impostor"))
    entry = _entry(build_report(shared, [BOT]), "impostor")
    assert entry.shape == SHAPE_LEGACY
    assert entry.kind == KIND_APP_SPEC  # where it was found, honestly reported


def test_a_vnext_spec_has_no_unclassified_keys(pod) -> None:
    """The census's loudest signal is 'a key nobody decided about'. It firing
    against the artifact this arc exists to produce would be the table being
    incomplete, not the artifact being wrong."""
    shared, manifests = pod
    _write_vnext(shared, _complete_manifest())
    report = build_report(shared, [BOT])
    assert report.unclassified_counts == {}
    assert _entry(report, "morning-brief").unclassified == []


def test_the_envelope_keys_are_dispositioned_as_envelope() -> None:
    """Not ``spec`` — the marker says which shape the FILE is, not anything
    about the app. Same bucket as ``schema_version`` / ``manifest_shape``,
    which AL-1.5a filed under ``instance`` for want of anywhere better."""
    from evolve_admin.applications.app_spec import (
        SPEC_SHAPE_FIELD, SPEC_SHAPE_VERSION_FIELD,
    )
    for key in (SPEC_SHAPE_FIELD, SPEC_SHAPE_VERSION_FIELD,
                "schema_version", "manifest_shape"):
        assert FIELD_DISPOSITION[key] == DISPOSITION_ENVELOPE, key


def test_every_vnext_spec_field_has_a_disposition() -> None:
    """The completeness test the other three artifact shapes already have,
    extended to the fourth."""
    from evolve_admin.applications.app_spec import SPEC_FIELDS
    assert [f for f in SPEC_FIELDS if f not in FIELD_DISPOSITION] == []


def test_a_vnext_spec_round_trips_through_the_census_unchanged(pod) -> None:
    """The census derives an AppSpec for every artifact. For one that already
    IS a v-next Spec, that derivation must be identity — re-derivation would
    zero ``runs``/``kind``, and the census would report the writer's own
    output as broken."""
    from evolve_admin.applications.app_spec_store import (
        read_spec, spec_from_artifact,
    )

    shared, manifests = pod
    source = _complete_manifest()
    path = _write_vnext(shared, source)
    entry = _entry(build_report(shared, [BOT]), "morning-brief")
    derived = spec_from_artifact(source)
    assert read_spec(path) == derived
    assert entry.spec_version == derived.spec_version
    assert entry.status == STATUS_CLEAN


def test_apply_still_rewrites_no_artifact_now_that_a_writer_exists(pod) -> None:
    """1.5b makes the REFLEX write a Spec. It does not turn the census into a
    pod-wide migration — rewriting the pod into a shape whose readers do not
    exist yet is the opposite of migrate-on-read."""
    shared, manifests = pod
    manifest_path = _write(manifests / "morning-brief.json", _complete_manifest())
    vnext_path = _write_vnext(shared, _complete_manifest(
        app_id="already-vnext", id="already-vnext"))
    before = {p: p.read_bytes() for p in (manifest_path, vnext_path)}

    build_report(shared, [BOT], apply=True)

    assert {p: p.read_bytes() for p in before} == before
    # …and no NEW Spec appeared for the legacy manifest.
    assert sorted(p.name for p in (shared / "apps" / "specs").glob("*.json")) == [
        "already-vnext.json"]


def test_the_table_carries_the_shape_column(pod) -> None:
    shared, manifests = pod
    _write(manifests / "morning-brief.json", _complete_manifest())
    _write_vnext(shared, _complete_manifest(app_id="vnext-app", id="vnext-app"))
    build_report(shared, [BOT], apply=True)
    table = json.loads(census_table_path(shared).read_text())
    assert table["shape_counts"] == {SHAPE_VNEXT: 1, SHAPE_LEGACY: 1}
    assert {r["app_id"]: r["shape"] for r in table["entries"]} == {
        "morning-brief": SHAPE_LEGACY, "vnext-app": SHAPE_VNEXT}
