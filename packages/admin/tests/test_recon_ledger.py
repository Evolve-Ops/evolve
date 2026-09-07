"""
Tests for the materialized reconciliation ledger (recon_ledger.py).

Covers the five-bucket join end to end with synthetic v7-arc Instances and
on-disk marker files in a tmp workspace:

  - owned_ok           (incl. a LINEAGE hit: retired spec_id → owned_ok)
  - attach_candidate   (ownable, marker resolves, no Instance claims it)
  - scrub_candidate    (ineligible: telemetry path + AGENTS.md → lifecycle
                        ``ineligible``; and unresolvable spec → ``orphaned``)
  - missing_marker     (Instance claims a file on disk with no marker)
  - missing_file       (Instance claims a path that is absent on disk)

Plus the reverse spec index, atomic persistence + round-trip, and the keystone
that a marker on a never-ownable path can never become an attach_candidate.

Placeholder bot names only (no real bot identities) per the public-launch
scrub guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.provenance import FileLifecycle, embed_marker
from evolve_admin.applications.recon_ledger import (
    Bucket,
    build_recon_ledger,
    load_recon_ledger,
    rows_from_dict,
)


# ── Fixtures / builders ───────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A single bot workspace under tmp_path with bot_home patched."""
    bot_id = "personal_bot"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(rl, "bot_home", lambda _bid, *a, **k: bot_dir)

    return {
        "bot_id": bot_id,
        "shared_dir": tmp_path / "shared",
        "bot_dir": bot_dir,
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "scripts": workspace / "scripts",
    }


def _make_instance(
    env, instance_id: str, spec_id: str, realized: list[dict],
    *, prior_spec_ids: list[str] | None = None, spec_version: str = "2026.05.20-1.0",
) -> dict:
    prov: dict = {
        "spec_id": spec_id,
        "spec_version": spec_version,
        "installed_at": "2026-05-20T00:00:00Z",
        "installed_by": "test",
    }
    if prior_spec_ids:
        prov["prior_spec_ids"] = list(prior_spec_ids)
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": prov,
        "realized_files": realized,
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))
    return inst


def _rf(path: Path, file_id: str) -> dict:
    return {
        "logical_name": path.stem,
        "path": str(path.resolve()),
        "file_id": file_id,
        "marker_state": "OWNED",
    }


def _write(env, relpath: str, content: str = "x = 1\n") -> Path:
    p = env["workspace"] / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _stamp(p: Path, spec_id: str, file_id: str, version: str = "2026.05.20-1.0") -> None:
    embed_marker(
        p, pkg_ids=[spec_id], file_id=file_id,
        pkg_versions={spec_id: version}, file_version=version,
        keyword="spec", merge=False,
    )


def _build(env):
    return build_recon_ledger(env["shared_dir"], [env["bot_id"]], persist=False)


def _by_path(rows, suffix):
    return [r for r in rows if r.path.endswith(suffix)]


def _rf_rel(relpath: str, file_id: str) -> dict:
    """A realized_files[] entry storing a WORKSPACE-RELATIVE path — the
    migrate_v7/v13 form the #3303 CWD-resolve bug mangled. (The default ``_rf``
    stores ``str(path.resolve())``, an ABSOLUTE path, which is exactly why the
    pre-existing suite never tripped the bug.)"""
    return {
        "logical_name": Path(relpath).stem,
        "path": relpath,                       # relative, NOT resolved
        "file_id": file_id,
        "marker_state": "OWNED",
    }


# ── #3303 regression: CWD-independent join key ────────────────────────────────

class TestCwdIndependentJoinKey:
    """#3303: a ``realized_files[].path`` stored workspace-relative must JOIN to
    its on-disk marker regardless of the process CWD. The ``admin-ui`` daemon
    runs with CWD ``/`` (no ``WorkingDirectory`` in its plist); the old
    ``_resolve_key`` did ``Path(rel).resolve()`` → ``/scripts/...``, so the
    registry↔disk join collapsed — ``owned_ok`` was always 0, every claim became
    a false ``missing_file`` and every disk marker a false attach/scrub. These
    tests resolve from ``/`` (daemon-faithful) and FAIL on origin/main.
    """

    def test_relative_realized_file_is_owned_ok_under_foreign_cwd(self, env, monkeypatch):
        # '/' is exactly what launchd hands the admin-ui daemon.
        monkeypatch.chdir("/")

        p = _write(env, "scripts/atlas_lib/composer.py")
        _stamp(p, "p-555f7671", "f-d4ac0c02")
        # The manifest claims the file by its WORKSPACE-RELATIVE path.
        _make_instance(
            env, "i-1", "p-555f7671",
            realized=[_rf_rel("scripts/atlas_lib/composer.py", "f-d4ac0c02")],
        )

        res = _build(env)

        owned = _by_path(res.rows(Bucket.OWNED_OK), "composer.py")
        assert len(owned) == 1, res.counts()
        assert owned[0].spec_id == "p-555f7671"
        assert owned[0].path == "scripts/atlas_lib/composer.py"
        # The bug's signature buckets are empty for this file.
        assert not _by_path(res.rows(Bucket.MISSING_FILE), "composer.py")
        assert not _by_path(res.rows(Bucket.ATTACH_CANDIDATE), "composer.py")
        assert res.counts()[Bucket.MISSING_FILE] == 0

    def test_relative_claim_missing_and_unmarked_under_foreign_cwd(self, env, monkeypatch):
        # Locks pass 2's ``workspace / rel`` existence reconstruction: a
        # relative claim with a present-but-unmarked file is missing_marker; an
        # absent one is missing_file — never crashing on the CWD-mangled key.
        monkeypatch.chdir("/")

        _write(env, "scripts/nomark.py", content="# bare\n")  # present, no marker
        _make_instance(env, "i-1", "p-abcd1234", realized=[
            _rf_rel("scripts/nomark.py", "f-nm000001"),
            _rf_rel("scripts/gone.py", "f-gone0001"),       # absent
        ])

        res = _build(env)
        mm = _by_path(res.rows(Bucket.MISSING_MARKER), "nomark.py")
        assert len(mm) == 1 and mm[0].path == "scripts/nomark.py"
        mf = _by_path(res.rows(Bucket.MISSING_FILE), "gone.py")
        assert len(mf) == 1 and mf[0].path == "scripts/gone.py"


# ── owned_ok ──────────────────────────────────────────────────────────────────

class TestOwnedOk:
    def test_claimed_and_marked_is_owned_ok(self, env):
        p = _write(env, "scripts/keep.py")
        _stamp(p, "p-aaaa1111", "f-keep0001")
        _make_instance(env, "i-1", "p-aaaa1111", realized=[_rf(p, "f-keep0001")])

        res = _build(env)
        rows = _by_path(res.rows(Bucket.OWNED_OK), "keep.py")
        assert len(rows) == 1
        assert rows[0].spec_id == "p-aaaa1111"
        assert rows[0].lifecycle == FileLifecycle.OWNED

    def test_retired_spec_id_resolves_via_lineage(self, env):
        """A marker carrying a RETIRED spec_id still maps to the live Instance
        that superseded it — owned_ok, not a false orphan."""
        p = _write(env, "scripts/legacy.py")
        # On-disk marker still carries the OLD spec id.
        _stamp(p, "p-old00000", "f-leg00001")
        # Live Instance is bound to a NEW spec id; old id is in its lineage and
        # it still claims the file.
        _make_instance(
            env, "i-1", "p-new11111",
            realized=[_rf(p, "f-leg00001")],
            prior_spec_ids=["p-old00000"],
        )

        res = _build(env)
        rows = _by_path(res.rows(Bucket.OWNED_OK), "legacy.py")
        assert len(rows) == 1
        # Lineage resolved the retired marker id to the live current spec id.
        assert rows[0].spec_id == "p-new11111"
        assert rows[0].reason == "claimed_marker_resolved_via_lineage"
        # And it is NOT reported in any other bucket.
        for b in (Bucket.ATTACH_CANDIDATE, Bucket.SCRUB_CANDIDATE):
            assert not _by_path(res.rows(b), "legacy.py")


# ── attach_candidate ──────────────────────────────────────────────────────────

class TestAttachCandidate:
    def test_ownable_marked_unclaimed_is_attach(self, env):
        # A live app exists (claims a different file); the stray file's marker
        # resolves to it but no realized_files entry lists the stray.
        claimed = _write(env, "scripts/main.py")
        _stamp(claimed, "p-bbbb2222", "f-main0001")
        _make_instance(env, "i-1", "p-bbbb2222", realized=[_rf(claimed, "f-main0001")])

        stray = _write(env, "scripts/stray.py", content="y = 2\n")
        _stamp(stray, "p-bbbb2222", "f-stray001")

        res = _build(env)
        rows = _by_path(res.rows(Bucket.ATTACH_CANDIDATE), "stray.py")
        assert len(rows) == 1
        assert rows[0].spec_id == "p-bbbb2222"
        assert rows[0].reason == "marker_resolves_no_claim"
        assert rows[0].lifecycle == FileLifecycle.UNOWNED


# ── scrub_candidate ───────────────────────────────────────────────────────────

class TestScrubCandidate:
    def test_telemetry_path_is_scrub_ineligible(self, env):
        # A marker mis-stamped onto platform telemetry under evolve/ must scrub.
        p = _write(
            env, "evolve/audit_outbox/_ingested/rec-1.json",
            content=json.dumps({"finding": "x"}),
        )
        _stamp(p, "p-cccc3333", "f-rec00001")
        # Even with a (wrongly) claiming Instance, the keystone wins.
        _make_instance(env, "i-1", "p-cccc3333", realized=[_rf(p, "f-rec00001")])

        res = _build(env)
        rows = _by_path(res.rows(Bucket.SCRUB_CANDIDATE), "rec-1.json")
        assert len(rows) == 1
        assert rows[0].lifecycle == FileLifecycle.INELIGIBLE
        assert rows[0].reason == "ineligible_path"
        # Keystone: never attach, never owned.
        assert not _by_path(res.rows(Bucket.ATTACH_CANDIDATE), "rec-1.json")
        assert not _by_path(res.rows(Bucket.OWNED_OK), "rec-1.json")

    def test_agents_md_is_scrub_ineligible(self, env):
        p = _write(env, "AGENTS.md", content="# Standing instructions\n")
        _stamp(p, "p-dddd4444", "f-agents01")

        res = _build(env)
        rows = _by_path(res.rows(Bucket.SCRUB_CANDIDATE), "AGENTS.md")
        assert len(rows) == 1
        assert rows[0].lifecycle == FileLifecycle.INELIGIBLE
        assert rows[0].reason == "ineligible_path"

    def test_unresolvable_spec_is_scrub_orphaned(self, env):
        # Ownable path, marker present, but the owning app is gone (no Instance,
        # and no lineage covers it).
        p = _write(env, "scripts/ghost.py")
        _stamp(p, "p-dead0000", "f-ghost001")
        # An unrelated live Instance exists so spec_index is non-empty.
        other = _write(env, "scripts/live.py")
        _stamp(other, "p-eeee5555", "f-live0001")
        _make_instance(env, "i-1", "p-eeee5555", realized=[_rf(other, "f-live0001")])

        res = _build(env)
        rows = _by_path(res.rows(Bucket.SCRUB_CANDIDATE), "ghost.py")
        assert len(rows) == 1
        assert rows[0].lifecycle == FileLifecycle.ORPHANED
        assert rows[0].reason == "unresolvable_spec"
        assert rows[0].spec_id is None


# ── missing_marker / missing_file ─────────────────────────────────────────────

class TestMissingMarker:
    def test_claimed_on_disk_no_marker(self, env):
        p = _write(env, "scripts/summary.py", content="# no marker here\n")
        _make_instance(env, "i-1", "p-ffff6666", realized=[_rf(p, "f-summ0001")])

        res = _build(env)
        rows = _by_path(res.rows(Bucket.MISSING_MARKER), "summary.py")
        assert len(rows) == 1
        assert rows[0].spec_id == "p-ffff6666"
        assert rows[0].file_id == "f-summ0001"
        assert rows[0].reason == "claimed_no_marker"


class TestMissingFile:
    def test_claimed_absent_on_disk(self, env):
        gone = env["scripts"] / "gone.py"  # never created
        _make_instance(env, "i-1", "p-aaaa7777", realized=[_rf(gone, "f-gone0001")])

        res = _build(env)
        rows = _by_path(res.rows(Bucket.MISSING_FILE), "gone.py")
        assert len(rows) == 1
        assert rows[0].spec_id == "p-aaaa7777"
        assert rows[0].reason == "claimed_absent_on_disk"
        assert rows[0].lifecycle == FileLifecycle.ORPHANED


# ── invalid_claim: the claims-side ownership keystone ─────────────────────────

class TestInvalidClaim:
    """A *claim* (realized_files[] entry) on a never-ownable path must classify
    as ``invalid_claim`` — NOT missing_marker (whose Fix=stamp would corrupt a
    secret/store file) and NOT missing_file. The claims-side twin of pass 1's
    scrub keystone. Mirrors the atlas evidence that motivated this bite.
    """

    def test_claimed_secret_file_on_disk_is_invalid_claim_not_missing_marker(self, env):
        # A salt file exists on disk (so the legacy path would call it
        # missing_marker → offer Fix=stamp, corrupting the secret).
        _write(env, "atlas/member-hash-salt.bin", content="\x00rawsalt")
        _make_instance(env, "i-1", "p-aaaa1111", realized=[
            _rf_rel("atlas/member-hash-salt.bin", "f-salt0001"),
        ])

        res = _build(env)
        inv = _by_path(res.rows(Bucket.INVALID_CLAIM), "member-hash-salt.bin")
        assert len(inv) == 1, res.counts()
        assert inv[0].reason == "non_ownable_claim"
        assert inv[0].lifecycle == FileLifecycle.INELIGIBLE
        # The corrupting buckets are empty for this file.
        assert not _by_path(res.rows(Bucket.MISSING_MARKER), "member-hash-salt.bin")
        assert not _by_path(res.rows(Bucket.MISSING_FILE), "member-hash-salt.bin")

    def test_claimed_manifest_store_file_absent_is_invalid_claim_not_missing_file(self, env):
        # A manifest-store self-reference, absent on disk: invalid_claim, not
        # missing_file (the verb is "remove from manifest", not restore).
        _make_instance(env, "i-1", "p-bbbb2222", realized=[
            _rf_rel("manifests/atlas-daily-digest.json", "f-mani0001"),
        ])

        res = _build(env)
        inv = _by_path(res.rows(Bucket.INVALID_CLAIM), "atlas-daily-digest.json")
        assert len(inv) == 1, res.counts()
        assert inv[0].reason == "non_ownable_claim"
        assert not _by_path(res.rows(Bucket.MISSING_FILE), "atlas-daily-digest.json")

    def test_atlas_mirror_split_proves_over_removal_guard(self, env, monkeypatch):
        """The load-bearing safety property, reproduced from the atlas atlas-digest
        evidence: C1 ownable real source → missing_marker (Fix=stamp KEPT); C2
        generated/secret + C3 store/index → invalid_claim (Fix=stamp KILLED);
        a deleted-but-ownable config stays missing_file; truly-owned stays
        owned_ok. NO legit owned/claimed file is dropped or reclassified.
        """
        monkeypatch.chdir("/")  # daemon-faithful CWD

        # C1 — 9 genuine ownable source/lib files, on disk, unmarked → missing_marker.
        c1 = [
            "scripts/atlas_digest.py",
            "scripts/atlas_lib/__init__.py",
            "scripts/atlas_lib/archive.py",
            "scripts/atlas_lib/classifier.py",
            "scripts/atlas_lib/config.py",
            "scripts/atlas_lib/fetchers.py",
            "scripts/atlas_lib/hashing.py",
            "scripts/atlas_lib/telegram_api.py",
            "scripts/atlas-digest-cron.sh",
        ]
        for rel in c1:
            _write(env, rel, content="# bare source, no marker\n")

        # C2 — generated runtime / secret files (on disk) → invalid_claim.
        c2 = [
            "atlas/.capture-salt",
            "atlas/member-hash-salt.bin",
            "atlas/capture-log.jsonl",
            "atlas/research-log.jsonl",
        ]
        for rel in c2:
            _write(env, rel, content="runtime-state\n")

        # C3 — manifest-store self-refs + telemetry index → invalid_claim.
        #   article-capture/daily-digest/knowledge on disk; on-demand-research +
        #   recap-automation absent (the "missing files" variants); archive index.
        c3_present = [
            "manifests/atlas-article-capture.json",
            "manifests/atlas-daily-digest.json",
            "manifests/atlas-knowledge.json",
            "archive/index.json",
        ]
        for rel in c3_present:
            _write(env, rel, content="{}\n")
        c3_absent = [
            "manifests/atlas-on-demand-research.json",
            "manifests/recap-automation.json",
        ]

        # A genuinely-deleted ownable config → stays missing_file (manual call).
        llm_config = "atlas/llm-config.json"

        # A truly-owned file (claimed + marked) → stays owned_ok.
        owned = _write(env, "scripts/atlas_lib/composer.py")
        _stamp(owned, "p-555f7671", "f-comp0001")

        realized = (
            [_rf_rel(r, f"f-c1{i:06d}") for i, r in enumerate(c1)]
            + [_rf_rel(r, f"f-c2{i:06d}") for i, r in enumerate(c2)]
            + [_rf_rel(r, f"f-cp{i:06d}") for i, r in enumerate(c3_present)]
            + [_rf_rel(r, f"f-ca{i:06d}") for i, r in enumerate(c3_absent)]
            + [_rf_rel(llm_config, "f-llm0001")]
            + [_rf_rel("scripts/atlas_lib/composer.py", "f-comp0001")]
        )
        _make_instance(env, "i-1", "p-555f7671", realized=realized)

        res = _build(env)
        counts = res.counts()

        # C1: every genuine source file is missing_marker (Fix=stamp KEPT).
        mm_paths = {r.path for r in res.rows(Bucket.MISSING_MARKER)}
        for rel in c1:
            assert rel in mm_paths, f"C1 {rel} must stay missing_marker; got {counts}"
        assert counts[Bucket.MISSING_MARKER] == len(c1)

        # C2 + C3: every never-ownable claim is invalid_claim (Fix=stamp KILLED).
        inv_paths = {r.path for r in res.rows(Bucket.INVALID_CLAIM)}
        for rel in c2 + c3_present + c3_absent:
            assert rel in inv_paths, f"{rel} must be invalid_claim; got {counts}"
        assert counts[Bucket.INVALID_CLAIM] == len(c2) + len(c3_present) + len(c3_absent)
        # None of the never-ownable claims leaked into a stampable/restorable bucket.
        for b in (Bucket.MISSING_MARKER, Bucket.MISSING_FILE):
            for rel in c2 + c3_present + c3_absent:
                assert rel not in {r.path for r in res.rows(b)}

        # llm-config.json: ownable + absent → stays missing_file (NOT removed).
        mf_paths = {r.path for r in res.rows(Bucket.MISSING_FILE)}
        assert llm_config in mf_paths
        assert counts[Bucket.MISSING_FILE] == 1

        # The truly-owned file is untouched.
        assert _by_path(res.rows(Bucket.OWNED_OK), "composer.py")

        # Every file lands in exactly one bucket.
        all_paths = [r.path for r in res.all_rows()]
        assert len(all_paths) == len(set(all_paths))


# ── Reverse spec index ────────────────────────────────────────────────────────

class TestReverseSpecIndex:
    def test_spec_index_maps_current_and_lineage(self, env):
        p = _write(env, "scripts/keep.py")
        _stamp(p, "p-new11111", "f-keep0001")
        _make_instance(
            env, "i-9", "p-new11111",
            realized=[_rf(p, "f-keep0001")],
            prior_spec_ids=["p-old00000"],
            spec_version="2026.06.01-2.0",
        )

        res = _build(env)
        entry = res.spec_index["p-new11111"]
        assert entry.current_version == "2026.06.01-2.0"
        assert entry.owning_instance == "i-9"
        assert entry.prior_spec_ids == ["p-old00000"]
        assert any(f.endswith("keep.py") for f in entry.files)
        # Retired id resolves through lineage to the same live entry.
        assert res.lookup_spec("p-old00000") is entry
        assert res.lookup_spec("p-new11111") is entry


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_persist_and_load_roundtrip(self, env):
        p = _write(env, "scripts/keep.py")
        _stamp(p, "p-aaaa1111", "f-keep0001")
        _make_instance(env, "i-1", "p-aaaa1111", realized=[_rf(p, "f-keep0001")])

        res = build_recon_ledger(env["shared_dir"], [env["bot_id"]], persist=True)
        ledger_file = env["shared_dir"] / "recon_ledger.json"
        assert ledger_file.exists()

        loaded = load_recon_ledger(env["shared_dir"])
        assert loaded["schema"] == "recon-ledger-v1"
        assert loaded["counts"][Bucket.OWNED_OK] == 1
        assert loaded["bots"] == [env["bot_id"]]

        rows = rows_from_dict(loaded, Bucket.OWNED_OK)
        assert len(rows) == 1
        assert rows[0].path.endswith("keep.py")
        assert rows[0].spec_id == "p-aaaa1111"
        # In-memory result agrees with the persisted form.
        assert res.counts()[Bucket.OWNED_OK] == 1


# ── All five buckets in one pass ──────────────────────────────────────────────

class TestAllFiveBuckets:
    def test_one_of_each_bucket(self, env):
        # owned_ok
        ok = _write(env, "scripts/ok.py")
        _stamp(ok, "p-1111aaaa", "f-ok000001")
        # attach_candidate (marker resolves to the same live app, not claimed)
        stray = _write(env, "scripts/stray.py", content="s = 1\n")
        _stamp(stray, "p-1111aaaa", "f-stray001")
        # scrub_candidate (ineligible — telemetry)
        tel = _write(env, "evolve/signals/firing/s-1.json", content=json.dumps({"a": 1}))
        _stamp(tel, "p-1111aaaa", "f-tel00001")
        # missing_marker
        nm = _write(env, "scripts/nomark.py", content="# bare\n")
        # missing_file
        gone = env["scripts"] / "gone.py"

        _make_instance(env, "i-1", "p-1111aaaa", realized=[
            _rf(ok, "f-ok000001"),
            _rf(nm, "f-nomark01"),
            _rf(gone, "f-gone0001"),
        ])

        res = _build(env)
        counts = res.counts()
        assert counts[Bucket.OWNED_OK] == 1
        assert counts[Bucket.ATTACH_CANDIDATE] == 1
        assert counts[Bucket.SCRUB_CANDIDATE] == 1
        assert counts[Bucket.MISSING_MARKER] == 1
        assert counts[Bucket.MISSING_FILE] == 1
        # Every file lands in exactly one bucket.
        all_paths = [r.path for r in res.all_rows()]
        assert len(all_paths) == len(set(all_paths))
