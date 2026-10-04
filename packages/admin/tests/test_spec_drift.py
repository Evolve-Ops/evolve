"""
Tests for spec_drift (S3c, v7-arc §8.1.3).

Covers:
- Version parsing — chronological tuple order, multi-digit major/minor
- _latest_spec_version across local / builtin / imported tiers
- detect_drift finding kinds: drift, downgrade, spec_missing
- Match-exact case: no finding when current == latest
- Edge cases: unparseable provenance, missing provenance fields, no Instances
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.spec_drift import (
    DriftFinding,
    DriftResult,
    _gallery_dirs_for_spec,
    _latest_spec_version,
    _parse_spec_version,
    detect_drift,
    instance_drift_status,
)


# ── Version parsing ──────────────────────────────────────────────────────────


class TestParseSpecVersion:
    def test_canonical(self):
        assert _parse_spec_version("2026.05.20-1.0") == (2026, 5, 20, 1, 0)

    def test_multi_digit_major(self):
        assert _parse_spec_version("2026.05.20-10.3") == (2026, 5, 20, 10, 3)

    def test_chronological_tuple_ordering(self):
        """Lexical compare would put 10.0 BEFORE 2.0 — tuple form fixes it."""
        ten = _parse_spec_version("2026.05.20-10.0")
        two = _parse_spec_version("2026.05.20-2.0")
        assert ten > two

    def test_date_change(self):
        a = _parse_spec_version("2026.05.20-1.0")
        b = _parse_spec_version("2026.06.01-1.0")
        assert b > a

    def test_unparseable_returns_none(self):
        assert _parse_spec_version("not-a-version") is None
        assert _parse_spec_version("") is None
        assert _parse_spec_version(None) is None  # type: ignore[arg-type]
        # Missing version suffix
        assert _parse_spec_version("2026.05.20") is None


# ── Gallery dir resolution ───────────────────────────────────────────────────


class TestGalleryDirsForSpec:
    def test_includes_local_and_builtin(self, tmp_path):
        spec_id = "p-aaaa1111"
        dirs = _gallery_dirs_for_spec(spec_id, tmp_path)
        assert tmp_path / "gallery" / "local" / spec_id in dirs
        assert tmp_path / "gallery" / "builtin" / spec_id in dirs

    def test_imported_pods_picked_up(self, tmp_path):
        spec_id = "p-aaaa1111"
        imp = tmp_path / "gallery" / "imported"
        (imp / "pod-other-1").mkdir(parents=True)
        (imp / "pod-other-2").mkdir(parents=True)
        dirs = _gallery_dirs_for_spec(spec_id, tmp_path)
        assert imp / "pod-other-1" / spec_id in dirs
        assert imp / "pod-other-2" / spec_id in dirs

    def test_missing_imported_root_ok(self, tmp_path):
        # No imported/ dir exists — should not crash
        dirs = _gallery_dirs_for_spec("p-aaaa1111", tmp_path)
        assert len(dirs) == 2  # local + builtin only


# ── _latest_spec_version ─────────────────────────────────────────────────────


def _write_spec_file(shared_dir: Path, tier: str, spec_id: str, version: str) -> Path:
    d = shared_dir / "gallery" / tier / spec_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{version}.json"
    p.write_text(json.dumps({"spec_id": spec_id, "spec_version": version}))
    return p


class TestLatestSpecVersion:
    def test_finds_latest_in_local(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.22-1.0")
        assert _latest_spec_version("p-a", tmp_path) == "2026.05.22-1.0"

    def test_latest_across_tiers(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(tmp_path, "builtin", "p-a", "2026.05.21-1.0")
        # The latest is in builtin, even though local has one too.
        assert _latest_spec_version("p-a", tmp_path) == "2026.05.21-1.0"

    def test_latest_from_imported_pod(self, tmp_path):
        # Spec also got shared from another pod with a newer version
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        imp = tmp_path / "gallery" / "imported" / "pod-other" / "p-a"
        imp.mkdir(parents=True)
        (imp / "2026.05.25-1.0.json").write_text("{}")
        assert _latest_spec_version("p-a", tmp_path) == "2026.05.25-1.0"

    def test_multi_digit_major_wins(self, tmp_path):
        """Pin: lexical ordering must not produce 1.0 > 10.0."""
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-10.0")
        assert _latest_spec_version("p-a", tmp_path) == "2026.05.20-10.0"

    def test_no_spec_returns_none(self, tmp_path):
        assert _latest_spec_version("p-missing", tmp_path) is None

    def test_skips_non_version_filenames(self, tmp_path):
        d = tmp_path / "gallery" / "local" / "p-a"
        d.mkdir(parents=True)
        (d / "README.json").write_text("{}")  # not a version
        (d / "2026.05.20-1.0.json").write_text("{}")
        assert _latest_spec_version("p-a", tmp_path) == "2026.05.20-1.0"


# ── detect_drift fixture ─────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Synthetic bot workspace + shared_dir, with bot_home patched."""
    bot_id = "team_bot_a"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)

    shared_dir = tmp_path / "shared"
    (shared_dir / "gallery").mkdir(parents=True)

    from evolve_admin.applications import spec_drift as sd
    monkeypatch.setattr(sd, "bot_home", lambda _bid: bot_dir)

    return {
        "bot_id": bot_id,
        "bot_dir": bot_dir,
        "manifests": workspace / "manifests",
        "shared_dir": shared_dir,
    }


def _make_instance(env, instance_id: str, spec_id: str, version: str) -> dict:
    """Drop a v7-arc Instance into the bot's manifests dir."""
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": version,
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": [],
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))
    return inst


# ── detect_drift ─────────────────────────────────────────────────────────────


class TestDetectDriftEmpty:
    def test_no_instances_returns_warning(self, env):
        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert result.instances_checked == 0
        assert any("no v7-arc Instances" in w for w in result.warnings)
        assert result.findings == []


class TestDetectDriftExactMatch:
    def test_current_equals_latest_no_finding(self, env):
        _make_instance(env, "i-1", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.20-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert result.instances_checked == 1
        assert result.findings == []


class TestDetectDriftDrift:
    def test_newer_spec_emits_drift_finding(self, env):
        _make_instance(env, "i-1", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.22-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.kind == "drift"
        assert f.spec_id == "p-a"
        assert f.current_version == "2026.05.20-1.0"
        assert f.latest_version == "2026.05.22-1.0"
        assert f.proposed_action["kind"] == "adopt_spec_version"

    def test_versions_behind_counts_minor_bumps(self, env):
        _make_instance(env, "i-1", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.20-1.3")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert result.findings[0].versions_behind == 3


class TestDetectDriftDowngrade:
    def test_instance_ahead_of_gallery(self, env):
        # Instance pins to a version that's newer than anything in the gallery.
        # Unusual; flagged as 'downgrade' so an operator can investigate.
        _make_instance(env, "i-1", "p-a", "2026.05.22-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.20-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.kind == "downgrade"
        assert f.current_version == "2026.05.22-1.0"
        assert f.latest_version == "2026.05.20-1.0"


class TestDetectDriftSpecMissing:
    def test_no_spec_file_at_all(self, env):
        _make_instance(env, "i-1", "p-deleted", "2026.05.20-1.0")
        # No spec files written

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.kind == "spec_missing"
        assert f.spec_id == "p-deleted"
        assert f.latest_version is None
        assert "expected_path" in f.proposed_action


class TestDetectDriftMixedBotState:
    def test_multiple_instances_independent(self, env):
        # i-1: drifted, i-2: exact match, i-3: spec_missing
        _make_instance(env, "i-1", "p-a", "2026.05.20-1.0")
        _make_instance(env, "i-2", "p-b", "2026.05.20-1.0")
        _make_instance(env, "i-3", "p-gone", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.22-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-b", "2026.05.20-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        kinds = sorted(f.kind for f in result.findings)
        assert kinds == ["drift", "spec_missing"]
        assert result.instances_checked == 3


class TestDetectDriftProvenanceEdges:
    def test_missing_spec_id_warns_and_skips(self, env):
        # Instance with no provenance.spec_id
        inst = {
            "instance_id": "i-bad",
            "bot_id": env["bot_id"],
            "schema_version": 14,
            "manifest_shape": "v7-arc",
            "provenance": {"spec_version": "2026.05.20-1.0"},  # spec_id missing
            "realized_files": [],
            "status": "active",
        }
        (env["manifests"] / "i-bad.json").write_text(json.dumps(inst))

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert result.findings == []
        assert any("missing provenance" in w for w in result.warnings)

    def test_unparseable_current_version_warns(self, env):
        _make_instance(env, "i-1", "p-a", "garbage-version-string")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.22-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        # garbage-version != latest, but garbage doesn't parse — should warn
        # and not crash.
        assert any("unparseable" in w for w in result.warnings)


class TestDetectDriftSkipsLegacy:
    def test_v13_instance_ignored(self, env):
        # Drop a v13-shape manifest (no manifest_shape=v7-arc)
        v13 = {
            "id": "old-app",
            "name": "Legacy",
            "schema_version": 13,
            "objective": "x",
        }
        (env["manifests"] / "old-app.json").write_text(json.dumps(v13))
        # Also a real v7-arc Instance with drift
        _make_instance(env, "i-1", "p-a", "2026.05.20-1.0")
        _write_spec_file(env["shared_dir"], "local", "p-a", "2026.05.22-1.0")

        result = detect_drift(env["bot_id"], env["shared_dir"])
        assert result.instances_checked == 1  # only the v7-arc one
        assert len(result.findings) == 1
        assert result.findings[0].kind == "drift"


# ── DriftResult.summary ──────────────────────────────────────────────────────


class TestDriftResultSummary:
    def test_summary_with_findings(self):
        r = DriftResult(bot_id="team_bot_a", instances_checked=3)
        r.findings.append(DriftFinding(
            kind="drift", bot_id="team_bot_a", instance_id="i-1",
            spec_id="p-a", current_version="x", latest_version="y",
        ))
        r.findings.append(DriftFinding(
            kind="drift", bot_id="team_bot_a", instance_id="i-2",
            spec_id="p-b", current_version="x", latest_version="y",
        ))
        r.findings.append(DriftFinding(
            kind="spec_missing", bot_id="team_bot_a", instance_id="i-3",
            spec_id="p-c", current_version="x", latest_version=None,
        ))

        s = r.summary()
        assert "instances=3" in s
        assert "findings=3" in s
        assert "drift=2" in s
        assert "spec_missing=1" in s

    def test_summary_no_findings(self):
        r = DriftResult(bot_id="team_bot_a", instances_checked=2)
        assert "findings=0" in r.summary()
        assert "none" in r.summary()


# ─────────────────────────────────────────────────────────────────────────────
# instance_drift_status — per-Instance helper used by the analytics endpoint
# (decorates app cards with drift info inline). Mirrors detect_drift's
# classification but operates on a single dict in memory rather than walking
# a bot's manifests dir.
# ─────────────────────────────────────────────────────────────────────────────


def _v7_instance(spec_id: str, version: str) -> dict:
    """Minimal v7-arc Instance dict for the inline-helper tests."""
    return {
        "instance_id": "i-1",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "provenance": {
            "spec_id": spec_id,
            "spec_version": version,
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
    }


class TestInstanceDriftStatus:
    def test_none_when_exact_match(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        out = instance_drift_status(_v7_instance("p-a", "2026.05.20-1.0"), tmp_path)
        assert out["kind"] == "none"
        assert out["current"] == "2026.05.20-1.0"
        assert out["latest"] == "2026.05.20-1.0"
        assert out["versions_behind"] == 0

    def test_drift_when_newer_exists(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.3")
        out = instance_drift_status(_v7_instance("p-a", "2026.05.20-1.0"), tmp_path)
        assert out["kind"] == "drift"
        assert out["latest"] == "2026.05.20-1.3"
        assert out["versions_behind"] == 3

    def test_downgrade_when_current_ahead(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.20-1.0")
        out = instance_drift_status(_v7_instance("p-a", "2026.05.22-1.0"), tmp_path)
        assert out["kind"] == "downgrade"

    def test_spec_missing_when_no_spec_files(self, tmp_path):
        # No spec files written
        out = instance_drift_status(_v7_instance("p-gone", "2026.05.20-1.0"), tmp_path)
        assert out["kind"] == "spec_missing"
        assert out["latest"] is None

    def test_unknown_for_v13_legacy(self, tmp_path):
        legacy = {"id": "old-app", "schema_version": 13}
        out = instance_drift_status(legacy, tmp_path)
        assert out["kind"] == "unknown"

    def test_unknown_when_provenance_missing(self, tmp_path):
        no_prov = {"manifest_shape": "v7-arc", "schema_version": 14}
        out = instance_drift_status(no_prov, tmp_path)
        assert out["kind"] == "unknown"

    def test_unknown_when_spec_version_unparseable(self, tmp_path):
        _write_spec_file(tmp_path, "local", "p-a", "2026.05.22-1.0")
        out = instance_drift_status(_v7_instance("p-a", "garbage"), tmp_path)
        assert out["kind"] == "unknown"
