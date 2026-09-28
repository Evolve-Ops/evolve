"""
Tests for the Reflect phase — now a thin reader of the recon ledger
(internal/spec-app-identity-ledger-2026-06-27.md §3 five buckets, §5 row
``apps-reflect-thin-reader``).

Covers:
  - orphan_file:      attach_candidate — ownable file marked, no Instance claims it
  - missing_marker:   file in Instance.realized_files[], no marker on disk
  - stale_pkg_marker: file has v6 marker, Instance claims it (migration miss)
  - missing_disk_file: Instance claims a path with no file on disk
  - scrub_candidate:  marker on a never-ownable path (telemetry / AGENTS.md) or
                      a marker whose spec is gone — NEVER surfaced as attach
  - lineage:          a marker carrying a retired spec_id resolves to its live
                      Instance → owned_ok (absent from findings)
  - skip non-v7-arc Instances, skip dirs, empty workspace handled gracefully
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.reflect import (
    ReflectFinding,
    ReflectResult,
    reflect,
)
from evolve_admin.applications.provenance import embed_marker


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A v7-arc Instance + bot workspace under tmp_path."""
    bot_id = "team_bot_a"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    # Patch bot_home wherever it is read. Ownership classification now lives in
    # recon_ledger (the single authority reflect reads), so its bot_home is the
    # one that drives the workspace walk; reflect's own bot_home only feeds the
    # instance count + warning. Patch both so the tmp workspace is honored.
    from evolve_admin.applications import reflect as rf
    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(rf, "bot_home", lambda _bid: bot_dir)
    monkeypatch.setattr(rl, "bot_home", lambda _bid: bot_dir)

    return {
        "bot_id": bot_id,
        "bot_dir": bot_dir,
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "scripts": workspace / "scripts",
    }


def _make_instance(
    env,
    instance_id: str,
    spec_id: str,
    realized: list[dict],
    prior_spec_ids: list[str] | None = None,
) -> dict:
    """Create a v7-arc Instance file in the bot's manifests dir."""
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
            **({"prior_spec_ids": prior_spec_ids} if prior_spec_ids else {}),
        },
        "realized_files": realized,
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))
    return inst


def _make_script(env, name: str, content: str = "print('hi')\n") -> Path:
    p = env["scripts"] / name
    p.write_text(content)
    return p


# ── Happy path: empty workspace ──────────────────────────────────────────────

class TestNoIssues:
    def test_no_v7_instances_emits_warning(self, env):
        result = reflect(env["bot_id"])
        assert result.instances_checked == 0
        assert any("no v7-arc Instances" in w for w in result.warnings)
        assert result.findings == []

    def test_v7_instance_with_no_files_no_findings(self, env):
        _make_instance(env, "i-1", "p-a", realized=[])
        result = reflect(env["bot_id"])
        assert result.instances_checked == 1
        assert result.findings == []


# ── Orphan file detection ────────────────────────────────────────────────────

class TestOrphanFile:
    def test_marker_with_no_instance_claim_is_orphan(self, env):
        # Instance exists but doesn't reference this file
        _make_instance(env, "i-1", "p-a", realized=[])
        # Stamp a marker on an unclaimed file
        p = _make_script(env, "stray.py", content="x = 1\n")
        embed_marker(
            p, pkg_ids=["p-a"], file_id="f-stray12",
            pkg_versions={"p-a": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0",
            keyword="spec", merge=False,
        )

        result = reflect(env["bot_id"])
        orphans = [f for f in result.findings if f.kind == "orphan_file"]
        assert len(orphans) == 1
        assert orphans[0].file_path.endswith("stray.py")
        assert orphans[0].spec_id == "p-a"
        assert orphans[0].proposed_action["kind"] == "attach_to_instance_or_archive"


# ── Missing marker detection ─────────────────────────────────────────────────

class TestMissingMarker:
    def test_instance_path_with_no_marker_flagged(self, env):
        # File on disk, but no marker
        p = _make_script(env, "summary.py", content="# no marker\n")
        # Instance claims this file
        _make_instance(env, "i-1", "p-a", realized=[
            {
                "logical_name": "summary",
                "path": str(p.resolve()),
                "file_id": "f-claim12@2026.05.20-1.0",
                "marker_state": "OWNED",
            },
        ])

        result = reflect(env["bot_id"])
        missing = [f for f in result.findings if f.kind == "missing_marker"]
        assert len(missing) == 1
        assert missing[0].instance_id == "i-1"
        assert missing[0].spec_id == "p-a"
        assert missing[0].proposed_action["kind"] == "stamp_marker"
        # The recon ledger strips the ``@version`` suffix from realized_files
        # file_ids; reflect carries the bare id through (re-stamping records the
        # current spec version separately).
        assert missing[0].proposed_action["file_id"] == "f-claim12"


# ── Stale v6 marker detection ────────────────────────────────────────────────

class TestStalePkgMarker:
    def test_pkg_marker_on_claimed_file_flagged(self, env):
        p = _make_script(env, "ingest.py")
        # Old v6 marker (keyword=pkg)
        embed_marker(
            p, pkg_ids=["p-a"], file_id="f-pkg12345",
            pkg_versions={"p-a": "2026.04.15-1.3"},
            file_version="2026.04.15-1.3",
            keyword="pkg", merge=False,
        )
        _make_instance(env, "i-1", "p-a", realized=[
            {
                "logical_name": "ingest",
                "path": str(p.resolve()),
                "file_id": "f-pkg12345@2026.05.20-1.0",
                "marker_state": "OWNED",
            },
        ])

        result = reflect(env["bot_id"])
        stale = [f for f in result.findings if f.kind == "stale_pkg_marker"]
        assert len(stale) == 1
        assert stale[0].proposed_action["kind"] == "rewrite_marker_to_spec"


# ── Mixed scenario ───────────────────────────────────────────────────────────

class TestMixedScenarios:
    def test_orphan_and_missing_marker_coexist(self, env):
        # File 1: orphan with marker
        p_orphan = _make_script(env, "orphan.py", content="orphan\n")
        embed_marker(
            p_orphan, pkg_ids=["p-a"], file_id="f-orphan12",
            pkg_versions={"p-a": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0",
            keyword="spec", merge=False,
        )
        # File 2: claimed but no marker
        p_missing = _make_script(env, "missing.py", content="no marker\n")
        _make_instance(env, "i-1", "p-a", realized=[
            {
                "logical_name": "missing",
                "path": str(p_missing.resolve()),
                "file_id": "f-missing1@2026.05.20-1.0",
                "marker_state": "OWNED",
            },
        ])

        result = reflect(env["bot_id"])
        kinds = sorted(f.kind for f in result.findings)
        assert "orphan_file" in kinds
        assert "missing_marker" in kinds

    def test_owned_file_with_correct_marker_emits_nothing(self, env):
        # Happy path — file claimed AND has correct v7 marker
        p = _make_script(env, "ok.py")
        embed_marker(
            p, pkg_ids=["p-a"], file_id="f-ok123456",
            pkg_versions={"p-a": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0",
            keyword="spec", merge=False,
        )
        _make_instance(env, "i-1", "p-a", realized=[
            {
                "logical_name": "ok",
                "path": str(p.resolve()),
                "file_id": "f-ok123456@2026.05.20-1.0",
                "marker_state": "OWNED",
            },
        ])

        result = reflect(env["bot_id"])
        assert result.findings == [], [f.kind for f in result.findings]


# ── Thin reader: scrub vs attach (the recon-ledger contract) ─────────────────

class TestScrubVsAttach:
    """The core fix: a marker on a never-ownable path (platform telemetry, an
    OpenClaw-standard file) must classify as ``scrub_candidate`` — never as an
    attachable ``orphan_file``. Only a genuine ownable-but-unregistered file is
    an attach candidate.
    """

    def test_audit_telemetry_is_scrub_never_attach(self, env):
        # A scanner mis-stamp on an audit-telemetry rec file under evolve/.
        _make_instance(env, "i-1", "p-app0001", realized=[])
        tele = (env["workspace"] / "evolve" / "audit_outbox" / "_ingested"
                / "2026-06-27")
        tele.mkdir(parents=True)
        rec = tele / "rec-abc123.json"
        rec.write_text(json.dumps({"event": "audit", "score": 1}))
        embed_marker(
            rec, pkg_ids=["p-app0001"], file_id="f-rec00001",
            pkg_versions={"p-app0001": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0", keyword="spec", merge=False,
        )

        result = reflect(env["bot_id"])
        scrub = [f for f in result.findings if f.kind == "scrub_candidate"]
        attach = [f for f in result.findings if f.kind == "orphan_file"]
        assert len(scrub) == 1, [f.kind for f in result.findings]
        assert scrub[0].file_path.endswith("rec-abc123.json")
        assert scrub[0].proposed_action["kind"] == "strip_marker"
        assert scrub[0].reason == "ineligible_path"
        assert scrub[0].lifecycle == "ineligible"
        # Critically: the telemetry file is NOT offered as an attachable orphan.
        assert all("rec-abc123.json" not in f.file_path for f in attach)

    def test_agents_md_is_scrub_never_attach(self, env):
        # AGENTS.md is an OpenClaw-standard identity file — never ownable.
        _make_instance(env, "i-1", "p-app0001", realized=[])
        agents = env["workspace"] / "AGENTS.md"
        agents.write_text("# Agent guidance\n\nstuff\n")
        embed_marker(
            agents, pkg_ids=["p-app0001"], file_id="f-agents01",
            pkg_versions={"p-app0001": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0", keyword="spec", merge=False,
        )

        result = reflect(env["bot_id"])
        scrub = [f for f in result.findings if f.kind == "scrub_candidate"]
        attach = [f for f in result.findings if f.kind == "orphan_file"]
        assert len(scrub) == 1, [f.kind for f in result.findings]
        assert scrub[0].file_path.endswith("AGENTS.md")
        assert scrub[0].reason == "ineligible_path"
        assert all("AGENTS.md" not in f.file_path for f in attach)

    def test_genuine_unregistered_script_is_attach_candidate(self, env):
        # An ownable script the bot authored + marked but forgot to register.
        _make_instance(env, "i-1", "p-app0001", realized=[])
        foo = _make_script(env, "foo.py", content="x = 1\n")
        embed_marker(
            foo, pkg_ids=["p-app0001"], file_id="f-foo00001",
            pkg_versions={"p-app0001": "2026.05.20-1.0"},
            file_version="2026.05.20-1.0", keyword="spec", merge=False,
        )

        result = reflect(env["bot_id"])
        attach = [f for f in result.findings if f.kind == "orphan_file"]
        scrub = [f for f in result.findings if f.kind == "scrub_candidate"]
        assert len(attach) == 1, [f.kind for f in result.findings]
        assert attach[0].file_path.endswith("foo.py")
        assert attach[0].spec_id == "p-app0001"
        assert attach[0].proposed_action["kind"] == "attach_to_instance_or_archive"
        assert scrub == []

    def test_retired_spec_id_resolves_via_lineage_is_owned(self, env):
        # The Instance is live under p-new but its marker carries the retired
        # p-old. Lineage must resolve it to owned_ok — absent from findings.
        foo = _make_script(env, "lineage.py", content="y = 2\n")
        _make_instance(
            env, "i-1", "p-new00ab",
            realized=[{
                "logical_name": "lineage",
                "path": str(foo.resolve()),
                "file_id": "f-lin00001@2026.05.20-1.0",
                "marker_state": "OWNED",
            }],
            prior_spec_ids=["p-old00cd"],
        )
        # Marker still carries the OLD (retired) spec_id.
        embed_marker(
            foo, pkg_ids=["p-old00cd"], file_id="f-lin00001",
            pkg_versions={"p-old00cd": "2026.04.01-1.0"},
            file_version="2026.04.01-1.0", keyword="spec", merge=False,
        )

        result = reflect(env["bot_id"])
        # Owned via lineage → no finding for this file at all.
        assert all("lineage.py" not in f.file_path for f in result.findings), \
            [(f.kind, f.file_path) for f in result.findings]

    def test_all_three_coexist_with_correct_classification(self, env):
        # One scrub (telemetry), one attach (script), one owned (claimed+marked).
        _make_instance(env, "i-1", "p-app0001", realized=[
            {"logical_name": "ok", "path": str((env["scripts"] / "ok.py").resolve()),
             "file_id": "f-ok000001@2026.05.20-1.0", "marker_state": "OWNED"},
        ])
        # owned
        ok = _make_script(env, "ok.py")
        embed_marker(ok, pkg_ids=["p-app0001"], file_id="f-ok000001",
                     pkg_versions={"p-app0001": "2026.05.20-1.0"},
                     file_version="2026.05.20-1.0", keyword="spec", merge=False)
        # attach
        stray = _make_script(env, "stray.py", content="z = 3\n")
        embed_marker(stray, pkg_ids=["p-app0001"], file_id="f-stray001",
                     pkg_versions={"p-app0001": "2026.05.20-1.0"},
                     file_version="2026.05.20-1.0", keyword="spec", merge=False)
        # scrub
        agents = env["workspace"] / "AGENTS.md"
        agents.write_text("# guidance\n")
        embed_marker(agents, pkg_ids=["p-app0001"], file_id="f-agents01",
                     pkg_versions={"p-app0001": "2026.05.20-1.0"},
                     file_version="2026.05.20-1.0", keyword="spec", merge=False)

        result = reflect(env["bot_id"])
        kinds = {f.file_path.split("/")[-1]: f.kind for f in result.findings}
        assert "ok.py" not in kinds                    # owned → no finding
        assert kinds.get("stray.py") == "orphan_file"  # attach candidate
        assert kinds.get("AGENTS.md") == "scrub_candidate"


# ── Invalid claim: never-ownable path in realized_files[] ────────────────────

class TestInvalidClaim:
    """A claim on a never-ownable path surfaces as ``invalid_claim`` with a
    remove-from-manifest action — NEVER a stampable ``missing_marker``. This is
    the safety fix: stamping a marker on a secret/store file corrupts it.
    """

    def test_claimed_secret_file_is_invalid_claim_not_missing_marker(self, env):
        # A salt file exists on disk but can never be owned.
        salt = env["workspace"] / "atlas" / "member-hash-salt.bin"
        salt.parent.mkdir(parents=True, exist_ok=True)
        salt.write_bytes(b"\x00rawsalt")
        _make_instance(env, "i-1", "p-app0001", realized=[
            {"logical_name": "salt", "path": str(salt.resolve()),
             "file_id": "f-salt0001@2026.05.20-1.0", "marker_state": "OWNED"},
        ])

        result = reflect(env["bot_id"])
        invalid = [f for f in result.findings if f.kind == "invalid_claim"]
        assert len(invalid) == 1, [f.kind for f in result.findings]
        assert invalid[0].file_path.endswith("member-hash-salt.bin")
        assert invalid[0].proposed_action["kind"] == "remove_from_realized_files"
        assert invalid[0].reason == "non_ownable_claim"
        # Critically: NOT offered as a stampable missing_marker.
        assert all("member-hash-salt.bin" not in f.file_path
                   for f in result.findings if f.kind == "missing_marker")

    def test_invalid_claim_action_is_not_a_stampable_kind(self, env):
        # The apply-fix endpoint whitelists stamp_marker / rewrite_marker_to_spec;
        # invalid_claim's action must be neither, so the endpoint rejects it.
        idx = env["workspace"] / "archive" / "index.json"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("{}\n")
        _make_instance(env, "i-1", "p-app0001", realized=[
            {"logical_name": "index", "path": str(idx.resolve()),
             "file_id": "f-idx00001@2026.05.20-1.0", "marker_state": "OWNED"},
        ])

        result = reflect(env["bot_id"])
        invalid = [f for f in result.findings if f.kind == "invalid_claim"]
        assert len(invalid) == 1
        assert invalid[0].proposed_action["kind"] not in (
            "stamp_marker", "rewrite_marker_to_spec",
        )

    def test_ownable_claim_unaffected_stays_missing_marker(self, env):
        # An ordinary source file claimed without a marker stays missing_marker
        # (Fix=stamp KEPT) — the over-removal guard.
        p = _make_script(env, "atlas_digest.py", content="# real source\n")
        _make_instance(env, "i-1", "p-app0001", realized=[
            {"logical_name": "atlas_digest", "path": str(p.resolve()),
             "file_id": "f-dig00001@2026.05.20-1.0", "marker_state": "OWNED"},
        ])

        result = reflect(env["bot_id"])
        mm = [f for f in result.findings if f.kind == "missing_marker"]
        assert len(mm) == 1
        assert mm[0].file_path.endswith("atlas_digest.py")
        assert mm[0].proposed_action["kind"] == "stamp_marker"
        assert all(f.kind != "invalid_claim" for f in result.findings)


# ── Missing-on-disk detection ────────────────────────────────────────────────

class TestMissingDiskFile:
    def test_instance_claims_absent_path_flagged(self, env):
        ghost = str((env["scripts"] / "ghost.py").resolve())
        _make_instance(env, "i-1", "p-app0001", realized=[
            {"logical_name": "ghost", "path": ghost,
             "file_id": "f-ghost001@2026.05.20-1.0", "marker_state": "OWNED"},
        ])

        result = reflect(env["bot_id"])
        missing = [f for f in result.findings if f.kind == "missing_disk_file"]
        assert len(missing) == 1
        assert missing[0].file_path.endswith("ghost.py")
        assert missing[0].instance_id == "i-1"
        assert missing[0].proposed_action["kind"] == "manual_remove_or_restore"


# ── Skips ────────────────────────────────────────────────────────────────────

class TestSkips:
    def test_legacy_v13_manifest_ignored(self, env):
        # A legacy v13 manifest file should NOT be treated as a v7-arc Instance.
        # If no real v7 Instances exist, this should emit the "no v7-arc" warning.
        (env["manifests"] / "legacy.json").write_text(json.dumps({
            "id": "legacy",
            "name": "Legacy",
            "schema_version": 13,
            # no manifest_shape
        }))

        result = reflect(env["bot_id"])
        assert result.instances_checked == 0
        assert any("no v7-arc" in w for w in result.warnings)

    def test_pycache_skipped(self, env):
        pyc_dir = env["scripts"] / "__pycache__"
        pyc_dir.mkdir()
        # File inside __pycache__ even with a marker shouldn't be flagged
        f = pyc_dir / "noise.py"
        f.write_text("# evolve: spec=p-x file=f-y\nstuff\n")
        _make_instance(env, "i-1", "p-x", realized=[])

        result = reflect(env["bot_id"])
        # No findings should reference any path inside __pycache__
        for finding in result.findings:
            assert "__pycache__" not in finding.file_path

    def test_manifests_dir_itself_skipped(self, env):
        # The instance JSON in manifests/ shouldn't be flagged as an orphan
        # of itself. Validate by creating one with no files referenced —
        # if Reflect walked manifests/, it'd find the instance JSON as
        # something to consider.
        _make_instance(env, "i-1", "p-a", realized=[])
        result = reflect(env["bot_id"])
        # No "orphan" finding for the instance file itself
        for f in result.findings:
            assert "manifests" not in f.file_path or "i-1.json" not in f.file_path


# ── Summary format ───────────────────────────────────────────────────────────

class TestSummary:
    def test_summary_lists_finding_counts_by_kind(self, env):
        # One orphan + one missing → summary shows both
        p_o = _make_script(env, "o.py")
        embed_marker(p_o, pkg_ids=["p-a"], file_id="f-o12345678",
                     pkg_versions={"p-a": "2026.05.20-1.0"},
                     file_version="2026.05.20-1.0", keyword="spec", merge=False)
        p_m = _make_script(env, "m.py", content="x\n")
        _make_instance(env, "i-1", "p-a", realized=[
            {"logical_name": "m", "path": str(p_m.resolve()),
             "file_id": "f-m1234567@2026.05.20-1.0", "marker_state": "OWNED"},
        ])

        result = reflect(env["bot_id"])
        s = result.summary()
        assert "team_bot_a" in s
        assert "missing_marker=1" in s
        assert "orphan_file=1" in s
