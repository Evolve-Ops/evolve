"""Tests for the ``evolve-admin export-app`` CLI subcommand.

The command is a thin wrapper around ``applications.export_engine.
build_export_draft`` plus the publish-to-gallery step. Tests stub the
pipeline (its own behaviour is covered by
``test_export_engine_stage_0*``) and assert:

  * argument validation (bot/manifest required, slug-required-for-publish,
    ANTHROPIC_API_KEY required)
  * happy paths (verdict summary printed, draft written to --out,
    publish writes to gallery/<slug>/<pkg_id>.json)
  * error paths (unknown bot, missing manifest, already-forged
    manifest, pipeline ValueError → exit 2, broken-verdict refuses
    publish without --force)
  * status-flip from draft → active on publish
  * refuse-to-overwrite + --force escape hatch
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import cli  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _scanned(**overrides) -> dict:
    base = {
        "id": "i-9c16b1c7",
        "display_name": "Unified Task System",
        "files": [{"path": "scripts/tasks.py", "role": "build_artifact"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
def pod_layout(tmp_path: Path, monkeypatch):
    """A fake pod tree with one bot + one scanner manifest.

    Patches ``bot_home`` to resolve into the temp tree and
    ``load_network`` to return a deterministic network containing the
    one bot. ``ANTHROPIC_API_KEY`` is set so the CLI fails-fast paths
    we don't want to take stay out of the way.
    """
    pod = tmp_path / "pod"
    pod.mkdir()
    bot_workspace = pod / "team-bot-a" / ".openclaw" / "workspace"
    (bot_workspace / "manifests").mkdir(parents=True)
    (bot_workspace / "manifests" / "i-9c16b1c7.json").write_text(
        json.dumps(_scanned()),
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, network=None: pod / bot_id,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *_args, **_kw: {"bots": {"team-bot-a": {}}},
    )

    gallery = tmp_path / "gallery"
    network = pod / "network.json"
    network.write_text(json.dumps({"bots": {"team-bot-a": {}}}))
    return {
        "pod": pod, "gallery": gallery, "network": network,
        "manifest_path": bot_workspace / "manifests" / "i-9c16b1c7.json",
    }


def _runner_invoke(args, *, monkeypatch=None):
    """Wrap CliRunner.invoke to make assertions simpler."""
    runner = CliRunner()
    return runner.invoke(cli.main, args, catch_exceptions=False)


# ── Argument validation ──────────────────────────────────────────────────────


def test_export_app_requires_bot(pod_layout):
    res = _runner_invoke(["export-app"])
    assert res.exit_code != 0
    assert "--bot" in res.output or "Missing option" in res.output


def test_export_app_requires_manifest(pod_layout):
    res = _runner_invoke(["export-app", "--bot", "team-bot-a"])
    assert res.exit_code != 0
    assert "--manifest" in res.output or "Missing option" in res.output


def test_export_app_publish_requires_slug(pod_layout):
    res = _runner_invoke([
        "export-app",
        "--bot", "team-bot-a",
        "--manifest", "i-9c16b1c7",
        "--publish",
    ])
    assert res.exit_code == 2
    assert "--slug" in res.output


def test_export_app_requires_api_key(pod_layout, monkeypatch):
    """Without ANTHROPIC_API_KEY in the env, the CLI fails fast before
    touching network.json or attempting the LLM call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = _runner_invoke([
        "export-app",
        "--bot", "team-bot-a",
        "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 2
    assert "ANTHROPIC_API_KEY" in res.output


# ── Source validation ────────────────────────────────────────────────────────


def test_export_app_rejects_unknown_bot(pod_layout):
    res = _runner_invoke([
        "export-app", "--bot", "no-such-bot", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 2
    assert "not in network.json" in res.output


def test_export_app_rejects_missing_manifest(pod_layout):
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-deadbeef",
    ])
    assert res.exit_code == 2
    assert "No manifest found" in res.output


def test_export_app_rejects_already_forged_manifest(pod_layout):
    pod_layout["manifest_path"].write_text(json.dumps(_scanned(
        pkg_id="p-abcd1234",
        build_spec="# Already forged",
    )))
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 2
    assert "already carries pkg_id or build_spec" in res.output


# ── Happy paths ──────────────────────────────────────────────────────────────


def _stub_pipeline(monkeypatch, draft):
    """Patch build_export_draft to return a fixed dict so we can
    exercise the CLI's print + write + publish logic without LLMs."""
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        lambda *args, **kwargs: draft,
    )


def _good_draft() -> dict:
    return {
        "pkg_id": "p-deadbeef",
        "pkg_version": "2026.06.03-1.0",
        "build_spec": "# Build Spec\n\nBody",
        "status": "draft",
        "export_stage": "0d",
        "round_trip": {
            "verdict": "good",
            "structural_findings": [],
            "dry_run_missing": [],
            "dry_run_extra": [],
            "dry_run_failed": False,
        },
        "export_meta": {"deriver_model": "x"},
    }


def test_export_app_prints_verdict_summary(pod_layout, monkeypatch):
    _stub_pipeline(monkeypatch, _good_draft())
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 0, res.output
    assert "Round-trip verdict" in res.output
    assert "good" in res.output
    assert "p-deadbeef" in res.output
    assert "2026.06.03-1.0" in res.output


def test_export_app_writes_draft_to_out(tmp_path, pod_layout, monkeypatch):
    _stub_pipeline(monkeypatch, _good_draft())
    out = tmp_path / "draft.json"
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--out", str(out),
    ])
    assert res.exit_code == 0, res.output
    assert out.is_file()
    written = json.loads(out.read_text())
    assert written["pkg_id"] == "p-deadbeef"
    assert written["status"] == "draft"   # not flipped without --publish


def test_export_app_renders_drift_findings(pod_layout, monkeypatch):
    draft = _good_draft()
    draft["round_trip"]["verdict"] = "drift"
    draft["round_trip"]["structural_findings"] = [
        {"kind": "missing_cli_in_build_spec",
         "detail": "subcommand X is missing",
         "hint": "deriver may have collapsed it"},
    ]
    draft["round_trip"]["dry_run_missing"] = ["scripts/extra.py"]
    _stub_pipeline(monkeypatch, draft)
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 0, res.output
    assert "drift" in res.output
    assert "subcommand X is missing" in res.output
    assert "scripts/extra.py" in res.output
    assert "deriver may have collapsed it" in res.output


def test_export_app_reports_broken_with_dry_run_failed(pod_layout, monkeypatch):
    draft = _good_draft()
    draft["round_trip"]["verdict"] = "broken"
    draft["round_trip"]["dry_run_failed"] = True
    _stub_pipeline(monkeypatch, draft)
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 0
    assert "broken" in res.output
    assert "Dry-run failed" in res.output


# ── Pipeline failures ────────────────────────────────────────────────────────


def test_export_app_handles_pipeline_value_error(pod_layout, monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("api_key bad")),
    )
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 2
    assert "Pipeline rejected" in res.output
    assert "api_key bad" in res.output


def test_export_app_handles_pipeline_crash_with_exit_3(pod_layout, monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
    ])
    assert res.exit_code == 3
    assert "crashed" in res.output


# ── Publish path ─────────────────────────────────────────────────────────────


def test_export_app_publish_writes_to_gallery(pod_layout, monkeypatch):
    _stub_pipeline(monkeypatch, _good_draft())
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--slug", "unified-task-system",
        "--gallery-dir", str(pod_layout["gallery"]),
        "--publish",
    ])
    assert res.exit_code == 0, res.output
    target = pod_layout["gallery"] / "unified-task-system" / "p-deadbeef.json"
    assert target.is_file()
    published = json.loads(target.read_text())
    # Status flipped to active.
    assert published["status"] == "active"
    assert "Published to" in res.output


def test_export_app_publish_refuses_existing_target(pod_layout, monkeypatch):
    target = pod_layout["gallery"] / "unified-task-system" / "p-deadbeef.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"existing": true}')

    _stub_pipeline(monkeypatch, _good_draft())
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--slug", "unified-task-system",
        "--gallery-dir", str(pod_layout["gallery"]),
        "--publish",
    ])
    assert res.exit_code == 2
    assert "already exists" in res.output
    # Existing file untouched.
    assert json.loads(target.read_text()) == {"existing": True}


def test_export_app_publish_force_overwrites_existing(pod_layout, monkeypatch):
    target = pod_layout["gallery"] / "unified-task-system" / "p-deadbeef.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"existing": true}')

    _stub_pipeline(monkeypatch, _good_draft())
    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--slug", "unified-task-system",
        "--gallery-dir", str(pod_layout["gallery"]),
        "--publish", "--force",
    ])
    assert res.exit_code == 0, res.output
    published = json.loads(target.read_text())
    assert published["pkg_id"] == "p-deadbeef"  # overwritten with the draft


def test_export_app_publish_refuses_broken_verdict_without_force(
    pod_layout, monkeypatch,
):
    draft = _good_draft()
    draft["round_trip"]["verdict"] = "broken"
    _stub_pipeline(monkeypatch, draft)

    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--slug", "unified-task-system",
        "--gallery-dir", str(pod_layout["gallery"]),
        "--publish",
    ])
    assert res.exit_code == 2
    assert "Round-trip verdict is 'broken'" in res.output
    # Nothing was written.
    assert not (pod_layout["gallery"] / "unified-task-system").exists()


def test_export_app_publish_broken_with_force_writes_anyway(
    pod_layout, monkeypatch,
):
    """Force lets the operator commit a broken-verdict export when
    they know the build_spec is fine (the LLM may have failed the
    dry-run because of a model quirk, not a real defect)."""
    draft = _good_draft()
    draft["round_trip"]["verdict"] = "broken"
    _stub_pipeline(monkeypatch, draft)

    res = _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--slug", "unified-task-system",
        "--gallery-dir", str(pod_layout["gallery"]),
        "--publish", "--force",
    ])
    assert res.exit_code == 0, res.output
    target = pod_layout["gallery"] / "unified-task-system" / "p-deadbeef.json"
    assert target.is_file()


# ── Flag threading ───────────────────────────────────────────────────────────


def test_export_app_threads_no_strip_source_specific_flag(pod_layout, monkeypatch):
    captured = {}

    def fake_build(*a, **kw):
        captured.update(kw)
        return _good_draft()

    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        fake_build,
    )
    _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--no-strip-source-specific",
    ])
    assert captured["strip_source_specific"] is False


def test_export_app_threads_skip_round_trip_flag(pod_layout, monkeypatch):
    captured = {}

    def fake_build(*a, **kw):
        captured.update(kw)
        return _good_draft()

    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        fake_build,
    )
    _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--skip-round-trip",
    ])
    assert captured["skip_round_trip"] is True


def test_export_app_threads_previous_pkg_version(pod_layout, monkeypatch):
    captured = {}

    def fake_build(*a, **kw):
        captured.update(kw)
        return _good_draft()

    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.build_export_draft",
        fake_build,
    )
    _runner_invoke([
        "export-app", "--bot", "team-bot-a", "--manifest", "i-9c16b1c7",
        "--previous-pkg-version", "2026.06.02-1.3",
    ])
    assert captured["previous_pkg_version"] == "2026.06.02-1.3"
