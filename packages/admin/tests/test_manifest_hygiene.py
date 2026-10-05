"""
Tests for manifest_hygiene.reconcile_orphan_markers — the auto-reconcile that
turns Reflect's orphan_file findings into proper Instance.realized_files[]
entries.

Covers:
  - happy path: N orphans, one Instance, all resolved
  - idempotency: re-run with --apply skips entries already listed
  - dry-run vs apply: dry-run reports but doesn't mutate disk
  - unmatched: marker spec_id has no Instance on this bot
  - ambiguous: primary spec_id matches multiple Instances (shouldn't happen
    in practice but classification must be explicit)
  - multi-spec marker: primary becomes the owner, secondaries recorded
  - skipped_already_listed: a finding whose path is already in
    realized_files[] is a no-op
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.manifest_hygiene import (
    ReconcileResult,
    reconcile_orphan_markers,
)
from evolve_admin.applications.provenance import embed_marker


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Bot workspace + manifests dir under tmp_path, with bot_home patched
    in both manifest_hygiene and reflect (since the helper delegates to
    reflect() to harvest findings)."""
    bot_id = "team_bot_a"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "evolve").mkdir(parents=True)
    (workspace / "memory").mkdir(parents=True)

    from evolve_admin.applications import manifest_hygiene as mh
    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(mh, "bot_home", lambda _bid: bot_dir)
    monkeypatch.setattr(rf, "bot_home", lambda _bid: bot_dir)
    # reflect now classifies via the recon ledger; point its bot_home at tmp.
    monkeypatch.setattr(rl, "bot_home", lambda _bid: bot_dir)

    return {
        "bot_id": bot_id,
        "bot_dir": bot_dir,
        "workspace": workspace,
        "manifests": workspace / "manifests",
    }


def _make_instance(env, instance_id: str, spec_id: str, realized: list[dict],
                   prior_spec_ids: list[str] | None = None) -> Path:
    """Drop a v7-arc Instance JSON in the bot's manifests dir.

    ``prior_spec_ids`` populates the supersession chain so a marker carrying a
    *retired* spec_id still resolves to this live Instance via lineage (the
    atlas regression: a re-spec'd app whose files carry the old ``p-…``).
    """
    provenance = {
        "spec_id": spec_id,
        "spec_version": "2026.06.01-1.0",
        "installed_at": "2026-06-01T00:00:00Z",
        "installed_by": "test",
    }
    if prior_spec_ids:
        provenance["prior_spec_ids"] = list(prior_spec_ids)
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": provenance,
        "realized_files": realized,
        "status": "active",
    }
    path = env["manifests"] / f"{instance_id}.json"
    path.write_text(json.dumps(inst))
    return path


def _make_discovered_app(env, app_id: str, spec_id: str,
                         realized: list[dict] | None = None) -> Path:
    """Drop a v7-arc ``discovered`` app manifest with NO materialized Instance.

    Mirrors the live atlas Task Manager case: ``manifest_shape == "v7-arc"``,
    ``definition_status == "discovered"``, ``instance_id is None``,
    ``realized_files == []``, and the Spec binding carried only in
    ``provenance.spec_id``. The manifest's file name is the app ``id`` (the live
    convention), NOT an instance_id — which the discovered app does not have.
    """
    inst = {
        "id": app_id,
        "name": app_id.replace("-", " ").title(),
        "instance_id": None,
        "app_id": None,
        "pkg_id": spec_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "definition_status": "discovered",
        "source": "discovered",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.06.01-1.0",
            "installed_at": "2026-06-01T00:00:00Z",
            "installed_by": "scanner",
        },
        "realized_files": list(realized or []),
        "status": "active",
    }
    path = env["manifests"] / f"{app_id}.json"
    path.write_text(json.dumps(inst))
    return path


def _stamp_orphan(env, subdir: str, name: str, spec_ids: list[str],
                   file_id: str = "f-abcd1234",
                   version: str = "2026.06.01-1.0") -> Path:
    """Write a file with a v7 spec= marker but DON'T add it to any
    Instance's realized_files[] — simulating the 'bot wrote it without
    calling extend_application' regression Reflect catches."""
    p = env["workspace"] / subdir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# stub for {name}\n")
    embed_marker(
        p,
        pkg_ids=spec_ids,
        file_id=file_id,
        pkg_versions={sid: version for sid in spec_ids},
        file_version=version,
        keyword="spec",
        merge=False,
    )
    return p


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_five_orphans_one_instance_dry_run(self, env):
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        for i in range(5):
            _stamp_orphan(env, "scripts", f"helper_{i}.py", ["p-aaaaaaaa"],
                          file_id=f"f-{i:08x}")

        result = reconcile_orphan_markers(env["bot_id"], apply=False)
        assert isinstance(result, ReconcileResult)
        assert result.applied is False
        assert len(result.resolved) == 5
        assert all(r.instance_id == "i-1" for r in result.resolved)
        assert all(r.spec_id == "p-aaaaaaaa" for r in result.resolved)
        assert result.ambiguous == []
        assert result.unmatched == []

        # Dry-run MUST NOT mutate the Instance JSON on disk.
        inst_after = json.loads(
            (env["manifests"] / "i-1.json").read_text())
        assert inst_after["realized_files"] == []

    def test_five_orphans_one_instance_apply(self, env):
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        for i in range(5):
            _stamp_orphan(env, "scripts", f"helper_{i}.py", ["p-aaaaaaaa"],
                          file_id=f"f-{i:08x}")

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert result.applied is True
        assert len(result.resolved) == 5

        # Apply must have written all 5 entries into realized_files[].
        inst_after = json.loads(
            (env["manifests"] / "i-1.json").read_text())
        assert len(inst_after["realized_files"]) == 5
        paths = sorted(rf["path"] for rf in inst_after["realized_files"])
        assert all(p.endswith(".py") for p in paths)
        # Marker state stays OWNED — marker was already on disk, we just
        # added the Instance side of the binding.
        assert all(rf["marker_state"] == "OWNED" for rf in inst_after["realized_files"])
        # file_id pulled from the on-disk marker (NOT freshly minted), so
        # it carries the version suffix.
        assert all("@2026.06.01-1.0" in rf["file_id"]
                   for rf in inst_after["realized_files"])


# ── Idempotency ───────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_rerun_after_apply_is_noop(self, env):
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        _stamp_orphan(env, "scripts", "once.py", ["p-aaaaaaaa"],
                      file_id="f-12345678")

        r1 = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(r1.resolved) == 1
        # After the first apply, the orphan is no longer an orphan — Reflect
        # won't surface it on the next pass, so reconcile sees nothing to do.
        r2 = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert r2.resolved == []
        assert r2.skipped_already_listed == []

        inst_after = json.loads(
            (env["manifests"] / "i-1.json").read_text())
        # Still exactly one entry — no duplicate.
        assert len(inst_after["realized_files"]) == 1

    def test_path_already_in_realized_files_skipped(self, env):
        # Construct the corner case where the path is in realized_files[]
        # but Reflect still flagged it as orphan — happens when the stored
        # path doesn't match the resolved on-disk path (path-normalization
        # mismatch). Auto-reconcile should detect this and skip.
        f = _stamp_orphan(env, "scripts", "preexisting.py", ["p-aaaaaaaa"],
                          file_id="f-99999999")
        # Use a deliberately-non-normalized variant so Reflect's expected-set
        # doesn't match and the file is reported as an orphan, but the
        # reconcile path-normalization DOES match.
        weird_path = str(f.parent / "." / f.name)
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[
            {
                "logical_name": "preexisting",
                "path": weird_path,
                "file_id": "f-99999999@2026.06.01-1.0",
                "marker_state": "OWNED",
            },
        ])

        # If Reflect doesn't actually flag this as orphan (because path
        # normalization happens to match), the test is vacuously true —
        # the more important assertion is the count after reconcile.
        reconcile_orphan_markers(env["bot_id"], apply=True)
        inst_after = json.loads(
            (env["manifests"] / "i-1.json").read_text())
        # The realized_files[] must still have exactly ONE entry for this
        # path; reconcile must not have appended a duplicate.
        matching = [rf for rf in inst_after["realized_files"]
                    if Path(rf["path"]).resolve() == f.resolve()]
        assert len(matching) == 1


# ── Unknown spec_id → scrub, not reconcile (recon-ledger thin-reader) ─────────

class TestUnmatched:
    def test_orphan_with_unknown_spec_id_is_scrubbed_not_reconciled(self, env):
        # An ownable file whose marker points at p-bbbb, which has no Instance
        # on this bot. Reflect (now a thin reader of the recon ledger) routes
        # an unresolvable spec_id to ``scrub_candidate`` — the owning app is
        # gone, so the marker should be stripped, NOT attached. It therefore
        # never reaches reconcile_orphan_markers as an orphan_file, so reconcile
        # surfaces nothing for it.
        from evolve_admin.applications.reflect import reflect

        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        _stamp_orphan(env, "scripts", "leftover.py", ["p-bbbbbbbb"],
                      file_id="f-77777777")

        # reconcile sees no reconcilable orphan — the file is a scrub candidate.
        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert result.resolved == []
        assert result.ambiguous == []
        assert result.unmatched == []

        # Reflect classifies it as scrub (unresolvable spec, lifecycle orphaned).
        findings = reflect(env["bot_id"]).findings
        scrub = [f for f in findings if f.kind == "scrub_candidate"
                 and f.file_path.endswith("leftover.py")]
        assert len(scrub) == 1
        assert scrub[0].reason == "unresolvable_spec"
        assert scrub[0].proposed_action["kind"] == "strip_marker"

        # No write should have happened to p-aaaa's Instance.
        inst_after = json.loads(
            (env["manifests"] / "i-1.json").read_text())
        assert inst_after["realized_files"] == []


# ── Multi-spec marker ─────────────────────────────────────────────────────────

class TestMultiSpec:
    def test_primary_spec_id_wins_secondaries_recorded(self, env):
        # Two Instances exist. The orphan's marker lists BOTH spec_ids,
        # primary first. Auto-reconcile attaches to the primary, records
        # secondaries on the resolved entry.
        _make_instance(env, "i-primary", "p-aaaaaaaa", realized=[])
        _make_instance(env, "i-secondary", "p-bbbbbbbb", realized=[])
        _stamp_orphan(env, "scripts", "shared.py",
                      ["p-aaaaaaaa", "p-bbbbbbbb"],
                      file_id="f-cccc4444")

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(result.resolved) == 1
        entry = result.resolved[0]
        assert entry.spec_id == "p-aaaaaaaa"
        assert entry.instance_id == "i-primary"
        assert entry.secondary_spec_ids == ["p-bbbbbbbb"]

        # Only the primary Instance got the realized_files[] update.
        primary_after = json.loads(
            (env["manifests"] / "i-primary.json").read_text())
        secondary_after = json.loads(
            (env["manifests"] / "i-secondary.json").read_text())
        assert len(primary_after["realized_files"]) == 1
        assert secondary_after["realized_files"] == []


# ── Ambiguous (multiple Instances claim same spec_id) ─────────────────────────

class TestAmbiguous:
    def test_multiple_instances_for_same_spec_id_surfaces_ambiguous(self, env):
        # Pathological case — shouldn't happen under v7-arc one-Instance-per-Spec
        # invariant, but reconcile must classify rather than pick arbitrarily.
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        _make_instance(env, "i-2", "p-aaaaaaaa", realized=[])
        _stamp_orphan(env, "scripts", "contested.py", ["p-aaaaaaaa"],
                      file_id="f-deadbeef")

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert result.resolved == []
        assert len(result.ambiguous) == 1
        assert sorted(result.ambiguous[0].instance_ids) == ["i-1", "i-2"]

        # Neither Instance got mutated.
        for iid in ("i-1", "i-2"):
            inst = json.loads(
                (env["manifests"] / f"{iid}.json").read_text())
            assert inst["realized_files"] == []


# ── Mixed scenario, batch behavior ────────────────────────────────────────────

class TestMixed:
    def test_resolved_and_ambiguous_coexist_unknown_spec_scrubbed(self, env):
        from evolve_admin.applications.reflect import reflect

        _make_instance(env, "i-a", "p-aaaaaaaa", realized=[])
        _make_instance(env, "i-dup1", "p-bbbbbbbb", realized=[])
        _make_instance(env, "i-dup2", "p-bbbbbbbb", realized=[])

        # 2 resolvable (primary spec)
        _stamp_orphan(env, "scripts", "ok1.py", ["p-aaaaaaaa"],
                      file_id="f-00000001")
        _stamp_orphan(env, "scripts", "ok2.py", ["p-aaaaaaaa"],
                      file_id="f-00000002")
        # 1 ambiguous (two Instances claim p-bbbb)
        _stamp_orphan(env, "scripts", "amb.py", ["p-bbbbbbbb"],
                      file_id="f-00000003")
        # 1 with an unresolvable spec (no Instance for p-cccc): the thin reader
        # now routes this to scrub_candidate, so reconcile never sees it.
        _stamp_orphan(env, "scripts", "lost.py", ["p-cccccccc"],
                      file_id="f-00000004")

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(result.resolved) == 2
        assert len(result.ambiguous) == 1
        assert result.unmatched == []

        counts = result.counts_by_spec()
        assert counts["p-aaaaaaaa"]["resolved"] == 2
        assert counts["p-bbbbbbbb"]["ambiguous"] == 1
        assert "p-cccccccc" not in counts

        # The unresolvable-spec file is a scrub candidate in Reflect.
        scrub = [f for f in reflect(env["bot_id"]).findings
                 if f.kind == "scrub_candidate" and f.file_path.endswith("lost.py")]
        assert len(scrub) == 1
        assert scrub[0].reason == "unresolvable_spec"


# ── #3303 sibling: CWD-independent _normalize_path ────────────────────────────

class TestNormalizePathCwdIndependent:
    """#3303 sibling: the idempotency match in reconcile keys on
    ``_normalize_path``. A WORKSPACE-RELATIVE ``realized_files[].path`` and the
    ABSOLUTE on-disk path Reflect emits for the same file must normalize to the
    SAME key regardless of the daemon CWD ('/'). The old ``Path(rel).resolve()``
    bound the relative form to CWD → ``/scripts/...``, so the match failed and
    a duplicate realized_files entry was appended on every re-run.
    """

    def test_relative_and_absolute_match_under_foreign_cwd(self, env, monkeypatch):
        from evolve_admin.applications.manifest_hygiene import _normalize_path

        monkeypatch.chdir("/")
        workspace = env["workspace"]
        rel = "scripts/helper.py"
        on_disk = workspace / rel
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_text("# helper\n")

        rel_key = _normalize_path(rel, workspace)
        abs_key = _normalize_path(str(on_disk), workspace)

        # Same canonical key — the idempotency match holds.
        assert rel_key == abs_key
        # And the relative form was NOT bound to the CWD ('/scripts/...').
        assert rel_key != "/scripts/helper.py"


# ── Defect 1: shared resolver (classify == act) — the atlas regression ────────

class TestLineageSharedResolver:
    """The historical bug: the recon-ledger CLASSIFIER resolved a marker's
    retired spec_id to a live app (→ attach_candidate) via lineage, but the
    ACTION path used a SECOND, non-lineage index (current spec_ids only) and
    returned ``no_instance_for_spec_id`` — so clicking Attach on a candidate the
    modal listed failed. The fix routes the action through the SAME resolution
    the classifier uses. These tests pin that classify == act.
    """

    def test_atlas_retired_spec_marker_attaches_via_lineage(self, env):
        # Mirror atlas: the Task Manager app was re-spec'd. Its live Instance is
        # bound to p-new but its files on disk still carry the RETIRED marker
        # spec_id p-9bfa1c84 (recorded in the supersession chain).
        from evolve_admin.applications.reflect import reflect

        _make_instance(env, "i-taskmgr", "p-newnewnew", realized=[],
                       prior_spec_ids=["p-9bfa1c84"])
        for name in ("task-check.sh", "tasks.py", "task_updater.py"):
            _stamp_orphan(env, "scripts", name, ["p-9bfa1c84"],
                          file_id=f"f-{abs(hash(name)) & 0xffffffff:08x}")

        # The classifier (recon ledger, via reflect) places all three in
        # attach_candidate, resolved to the LIVE current spec_id p-newnewnew.
        findings = reflect(env["bot_id"]).findings
        orphans = [f for f in findings if f.kind == "orphan_file"]
        assert len(orphans) == 3
        assert all(o.spec_id == "p-newnewnew" for o in orphans)

        # The action attaches every one — no "no Instance for p-9bfa1c84".
        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(result.resolved) == 3
        assert result.unmatched == []
        assert result.ambiguous == []
        assert all(r.instance_id == "i-taskmgr" for r in result.resolved)
        # Recorded under the resolved CURRENT spec_id, not the retired marker id.
        assert all(r.spec_id == "p-newnewnew" for r in result.resolved)

        inst_after = json.loads((env["manifests"] / "i-taskmgr.json").read_text())
        names = sorted(Path(rf["path"]).name for rf in inst_after["realized_files"])
        assert names == ["task-check.sh", "task_updater.py", "tasks.py"]

    def test_invariant_every_attach_candidate_resolves(self, env):
        # The standing invariant: a spec_id that classifies as attach_candidate
        # ALWAYS resolves to an Instance in the action path — no_instance_for_spec_id
        # is impossible for a real candidate. Mix lineage + direct + multi-spec.
        from evolve_admin.applications.reflect import reflect

        _make_instance(env, "i-direct", "p-direct00", realized=[])
        _make_instance(env, "i-lineage", "p-current0", realized=[],
                       prior_spec_ids=["p-retired0"])
        _stamp_orphan(env, "scripts", "direct.py", ["p-direct00"], file_id="f-01010101")
        _stamp_orphan(env, "scripts", "lineage.py", ["p-retired0"], file_id="f-02020202")

        orphans = [f for f in reflect(env["bot_id"]).findings if f.kind == "orphan_file"]
        attach_specs = {o.spec_id for o in orphans}

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        # Every attach candidate became resolved; none fell to unmatched.
        assert len(result.resolved) == len(orphans)
        assert all(u.reason != "no_instance_for_spec_id" for u in result.unmatched)
        resolved_specs = {r.spec_id for r in result.resolved}
        assert resolved_specs == attach_specs  # action spec_ids == classifier's


# ── Defect 3: targeted + batch attach (paths=) ────────────────────────────────

class TestTargetedPaths:
    def test_single_path_attaches_only_that_file(self, env):
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        p1 = _stamp_orphan(env, "scripts", "one.py", ["p-aaaaaaaa"], file_id="f-00000001")
        _stamp_orphan(env, "scripts", "two.py", ["p-aaaaaaaa"], file_id="f-00000002")

        result = reconcile_orphan_markers(env["bot_id"], apply=True, paths=[str(p1)])
        assert len(result.resolved) == 1
        assert Path(result.resolved[0].file_path).name == "one.py"

        inst_after = json.loads((env["manifests"] / "i-1.json").read_text())
        names = [Path(rf["path"]).name for rf in inst_after["realized_files"]]
        assert names == ["one.py"]  # two.py NOT attached

    def test_batch_paths_one_write_per_instance(self, env):
        # Three files all on one Instance → one read-mutate-write, all attached.
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        ps = [
            _stamp_orphan(env, "scripts", f"b{i}.py", ["p-aaaaaaaa"],
                          file_id=f"f-0000000{i}")
            for i in range(3)
        ]
        result = reconcile_orphan_markers(
            env["bot_id"], apply=True, paths=[str(p) for p in ps])
        assert len(result.resolved) == 3
        inst_after = json.loads((env["manifests"] / "i-1.json").read_text())
        assert len(inst_after["realized_files"]) == 3

    def test_requested_non_candidate_path_is_unmatched(self, env):
        # A path that is NOT an attach candidate (no marker / not present) is
        # surfaced as not_attach_candidate rather than silently doing nothing.
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        _stamp_orphan(env, "scripts", "real.py", ["p-aaaaaaaa"], file_id="f-00000001")

        bogus = str(env["workspace"] / "scripts" / "ghost.py")
        result = reconcile_orphan_markers(env["bot_id"], apply=True, paths=[bogus])
        assert result.resolved == []
        assert len(result.unmatched) == 1
        assert result.unmatched[0].reason == "not_attach_candidate"

    def test_paths_none_attaches_all(self, env):
        _make_instance(env, "i-1", "p-aaaaaaaa", realized=[])
        for i in range(2):
            _stamp_orphan(env, "scripts", f"all{i}.py", ["p-aaaaaaaa"],
                          file_id=f"f-0000000{i}")
        result = reconcile_orphan_markers(env["bot_id"], apply=True, paths=None)
        assert len(result.resolved) == 2


# ── Discovered app: attach_candidate → attachable (no materialized Instance) ──

class TestDiscoveredApp:
    """The live atlas regression: clicking Attach on a ``discovered`` app's
    candidate returned ``unmatched`` ("No longer an attach candidate").

    A ``discovered`` app has a v7-arc manifest + a Spec (``provenance.spec_id``)
    but no materialized Instance — ``instance_id is None``, ``realized_files ==
    []``. The recon-ledger classifier resolves its marked files to
    ``attach_candidate`` (the marker spec resolves through ``build_spec_index``,
    which keys on the manifest's current spec_id regardless of instance_id). The
    OLD action resolved the owner via ``instance_id`` → ``manifests/{id}.json``;
    a null instance_id broke that, so a candidate the modal showed fell to
    ``unmatched``. classify == act now holds: the action resolves the owning
    manifest to its on-disk Path and attaches.
    """

    def test_task_manager_shaped_discovered_app_attaches(self, env):
        # Reproduce the live Task Manager case exactly.
        from evolve_admin.applications.reflect import reflect

        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        for name in ("task-check.sh", "tasks.py", "task_updater.py"):
            _stamp_orphan(env, "scripts", name, ["p-9bfa1c84"],
                          file_id=f"f-{abs(hash(name)) & 0xffffffff:08x}")

        # The classifier (recon ledger via reflect) places all three in
        # attach_candidate, bound to the discovered app's spec p-9bfa1c84.
        findings = reflect(env["bot_id"]).findings
        orphans = [f for f in findings if f.kind == "orphan_file"]
        assert len(orphans) == 3
        assert all(o.spec_id == "p-9bfa1c84" for o in orphans)

        # The action attaches all three — NO "no Instance for spec_id".
        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(result.resolved) == 3
        assert result.unmatched == []
        assert result.ambiguous == []
        # Label falls back to the manifest id (no instance_id on a discovered app).
        assert all(r.instance_id == "task-manager" for r in result.resolved)

        manifest_after = json.loads(
            (env["manifests"] / "task-manager.json").read_text())
        names = sorted(Path(rf["path"]).name
                       for rf in manifest_after["realized_files"])
        assert names == ["task-check.sh", "task_updater.py", "tasks.py"]
        # Attaching must NOT promote the discovered app to defined.
        assert manifest_after["definition_status"] == "discovered"
        # instance_id stays null — attach binds files, it does not materialize.
        assert manifest_after["instance_id"] is None

    def test_targeted_single_attach_on_discovered_app(self, env):
        # The exact per-row Attach the operator clicked: paths=[one file].
        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        p1 = _stamp_orphan(env, "scripts", "task-check.sh", ["p-9bfa1c84"],
                           file_id="f-11112222")
        _stamp_orphan(env, "scripts", "tasks.py", ["p-9bfa1c84"],
                      file_id="f-33334444")

        result = reconcile_orphan_markers(
            env["bot_id"], apply=True, paths=[str(p1)])
        assert len(result.resolved) == 1
        assert Path(result.resolved[0].file_path).name == "task-check.sh"
        assert result.unmatched == []

        manifest_after = json.loads(
            (env["manifests"] / "task-manager.json").read_text())
        names = [Path(rf["path"]).name
                 for rf in manifest_after["realized_files"]]
        assert names == ["task-check.sh"]  # tasks.py NOT attached (targeted)

    def test_dry_run_discovered_app_does_not_mutate(self, env):
        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        _stamp_orphan(env, "scripts", "task-check.sh", ["p-9bfa1c84"],
                      file_id="f-55556666")

        result = reconcile_orphan_markers(env["bot_id"], apply=False)
        assert len(result.resolved) == 1
        assert result.applied is False
        manifest_after = json.loads(
            (env["manifests"] / "task-manager.json").read_text())
        assert manifest_after["realized_files"] == []

    def test_idempotent_rerun_on_discovered_app(self, env):
        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        _stamp_orphan(env, "scripts", "task-check.sh", ["p-9bfa1c84"],
                      file_id="f-77778888")

        r1 = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert len(r1.resolved) == 1
        # After attach the file is claimed+marked → owned_ok → no longer an
        # orphan, so a re-run is a no-op (no duplicate realized_files entry).
        r2 = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert r2.resolved == []
        manifest_after = json.loads(
            (env["manifests"] / "task-manager.json").read_text())
        assert len(manifest_after["realized_files"]) == 1

    def test_invariant_unmatched_only_for_display_non_candidates(self, env):
        # The standing invariant the bug violated: ``unmatched`` is returned
        # ONLY for a path the display also shows as non-attachable (here: a
        # bogus path that is not an attach candidate at all). A discovered-app
        # candidate the display DOES show must never land in unmatched.
        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        real = _stamp_orphan(env, "scripts", "task-check.sh", ["p-9bfa1c84"],
                             file_id="f-99990000")
        bogus = str(env["workspace"] / "scripts" / "ghost.py")

        result = reconcile_orphan_markers(
            env["bot_id"], apply=True, paths=[str(real), bogus])
        resolved_names = {Path(r.file_path).name for r in result.resolved}
        assert "task-check.sh" in resolved_names
        # The only unmatched is the genuine non-candidate; the discovered-app
        # candidate is NOT unmatched.
        assert len(result.unmatched) == 1
        assert result.unmatched[0].reason == "not_attach_candidate"
        assert not any(u.reason == "no_instance_for_spec_id"
                       for u in result.unmatched)

    def test_two_discovered_apps_same_spec_is_ambiguous(self, env):
        # Adversarial: two discovered manifests sharing one current spec_id must
        # surface ``ambiguous`` (labelled by manifest id), not attach to one
        # arbitrarily — the ambiguity gate now covers discovered apps too.
        _make_discovered_app(env, "task-manager", "p-9bfa1c84")
        _make_discovered_app(env, "task-manager-dup", "p-9bfa1c84")
        _stamp_orphan(env, "scripts", "contested.sh", ["p-9bfa1c84"],
                      file_id="f-abababab")

        result = reconcile_orphan_markers(env["bot_id"], apply=True)
        assert result.resolved == []
        assert len(result.ambiguous) == 1
        assert sorted(result.ambiguous[0].instance_ids) == [
            "task-manager", "task-manager-dup"]
        # Neither manifest mutated.
        for app_id in ("task-manager", "task-manager-dup"):
            m = json.loads((env["manifests"] / f"{app_id}.json").read_text())
            assert m["realized_files"] == []
