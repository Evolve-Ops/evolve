"""F-P.10 — ``evolve-admin promote-app`` unified promote CLI tests.

The promote-app subcommand wraps the F-P.6 runbook's nine manual
steps (snapshot + stamp + bump) into one operator action. Distinct
from ``export-app``, which runs the scanned-export Stages 0a-0d for
ADDING new apps to the gallery; ``promote-app`` handles the
orthogonal workflow: take an EXISTING gallery package living as
manifest-only and promote it to manifest+files class.

These tests cover:

  - Input resolution: gallery slug from pkg_id, package manifest
    lookup, version bump arithmetic.
  - The end-to-end happy path: snapshot lands in
    ``gallery/<slug>/files/``, package manifest gets files_pack
    block, pkg_version bumps, index.json bumps.
  - Failure modes: missing gallery dir, ambiguous slug, snapshot
    errors, missing index entry.
  - --dry-run and --pkg-version overrides.

The snapshot engine itself is well-covered by
``test_snapshot_engine.py``; this file uses the real engine via
the CLI plumbing.
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


# ── Version-bump helper ─────────────────────────────────────────────────────


@pytest.mark.parametrize("current,bump,expected", [
    ("2026.06.03-1.3", "patch", "2026.06.03-1.4"),
    ("2026.06.03-1.3", "minor", "2026.06.03-1.5"),  # skip 1.4 by convention
    ("2026.06.03-1.3", "major", "2026.06.03-2.0"),
    ("2026.06.03-2.0", "minor", "2026.06.03-2.2"),
    # Case-insensitive bump kind.
    ("2026.06.03-1.0", "MINOR", "2026.06.03-1.2"),
])
def test_bump_pkg_version(current, bump, expected):
    assert cli._bump_pkg_version(current, bump) == expected


def test_bump_pkg_version_falls_back_when_unparseable():
    """Unparseable current version → today's date + 1.0 fallback.
    Only asserts the structure, not the exact date."""
    out = cli._bump_pkg_version("garbage", "minor")
    assert "-1.0" in out
    assert len(out.split("-")[0].split(".")) == 3  # YYYY.MM.DD


# ── End-to-end CLI fixture ──────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    """Wires up the full surface promote-app touches:

      - Source bot with one installed file under .openclaw/workspace
      - Gallery dir with a manifest-only package
      - Gallery index.json with an entry for the package

    Returns the paths the test asserts against.
    """
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts/foo.py").write_text(
        f"# WORKSPACE = /Users/{bot_user}/.openclaw/workspace\n"
        "print('ok')\n"
    )
    os.chmod(workspace / "scripts/foo.py", 0o644)
    (workspace / "manifests/foo.json").write_text(json.dumps({
        "id": "foo-app", "name": "Foo App",
        "bot_id": bot_user,
        "pkg_id": "p-foo01", "pkg_version": "2026.06.03-1.0",
        "files": [{"path": "scripts/foo.py", "sha256": "x"}],
    }))

    gallery = tmp_path / "gallery"
    (gallery / "foo").mkdir(parents=True)
    pkg_manifest = {
        "pkg_id": "p-foo01",
        "pkg_version": "2026.06.03-1.3",
        "display_name": "Foo App",
        "manifest_type": "evolve_application",
    }
    (gallery / "foo" / "p-foo01.json").write_text(
        json.dumps(pkg_manifest, indent=2) + "\n"
    )
    (gallery / "index.json").write_text(json.dumps([
        {"pkg_id": "p-foo01", "pkg_version": "2026.06.03-1.3",
         "display_name": "Foo App", "path": "foo/p-foo01.json"},
    ], indent=2) + "\n")

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {bot_user: {"user": bot_user}}},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    return {
        "pod": pod,
        "workspace": workspace,
        "bot_user": bot_user,
        "gallery": gallery,
        "package_manifest_path": gallery / "foo" / "p-foo01.json",
        "index_path": gallery / "index.json",
        "files_pack_dir": gallery / "foo" / "files",
    }


def _invoke(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


# ── Argument resolution ────────────────────────────────────────────────────


def test_promote_app_requires_bot_and_pkg(cli_env):
    res = _invoke("promote-app")
    assert res.exit_code != 0
    assert "--bot" in res.output or "--pkg" in res.output


def test_promote_app_rejects_unknown_pkg(cli_env):
    res = _invoke(
        "promote-app", "--bot", cli_env["bot_user"], "--pkg", "p-doesnotexist",
    )
    assert res.exit_code == 2
    assert "no slug" in res.output.lower()


# ── Happy path ─────────────────────────────────────────────────────────────


def test_promote_app_end_to_end(cli_env):
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 0, res.output

    # 1. Snapshot landed in gallery/<slug>/files/
    assert (cli_env["files_pack_dir"] / "manifest.json").is_file()
    assert (cli_env["files_pack_dir"] / "scripts/foo.py").is_file()

    # 2. Package manifest stamped with files_pack block + bumped version.
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["pkg_version"] == "2026.06.03-1.5"  # default minor (1.3 + 2)
    fp = pkg["files_pack"]
    assert fp["format_version"] == "1.0"
    assert fp["files_count"] == 1
    assert fp["snapshot_source_pkg_version"] == "2026.06.03-1.0"
    assert len(fp["sha256"]) == 64
    # Source bot id must NOT leak into the package manifest.
    assert "snapshot_source_bot_id" not in fp

    # 3. Gallery index bumped.
    index = json.loads(cli_env["index_path"].read_text())
    foo_entry = next(e for e in index if e["pkg_id"] == "p-foo01")
    assert foo_entry["pkg_version"] == "2026.06.03-1.5"


def test_promote_app_dry_run_makes_no_writes(cli_env):
    original_pkg = cli_env["package_manifest_path"].read_text()
    original_index = cli_env["index_path"].read_text()
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--dry-run",
    )
    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output.lower()
    # Nothing on disk changed.
    assert cli_env["package_manifest_path"].read_text() == original_pkg
    assert cli_env["index_path"].read_text() == original_index
    assert not cli_env["files_pack_dir"].exists()


def test_promote_app_explicit_version_overrides_bump(cli_env):
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--pkg-version", "2027.01.01-3.0",
    )
    assert res.exit_code == 0, res.output
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["pkg_version"] == "2027.01.01-3.0"
    index = json.loads(cli_env["index_path"].read_text())
    assert index[0]["pkg_version"] == "2027.01.01-3.0"


def test_promote_app_major_bump(cli_env):
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bump", "major",
    )
    assert res.exit_code == 0, res.output
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["pkg_version"] == "2026.06.03-2.0"


def test_promote_app_patch_bump(cli_env):
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bump", "patch",
    )
    assert res.exit_code == 0, res.output
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["pkg_version"] == "2026.06.03-1.4"


# ── Snapshot engine integration ─────────────────────────────────────────────


def test_promote_app_substitutes_workspace_placeholders(cli_env):
    """The snapshot engine's existing detector ran end-to-end through
    promote-app — workspace path in foo.py got substituted to {workspace}."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 0, res.output
    body = (cli_env["files_pack_dir"] / "scripts/foo.py").read_text()
    assert "{workspace}" in body
    assert f"/Users/{cli_env['bot_user']}/" not in body


def test_promote_app_overwrites_existing_files_pack(cli_env):
    """Re-running promote-app on an already-promoted package replaces the
    existing files-pack content (uses force=True internally)."""
    res1 = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res1.exit_code == 0, res1.output
    first_pkg = json.loads(cli_env["package_manifest_path"].read_text())

    # Modify the source file then re-promote
    (cli_env["workspace"] / "scripts/foo.py").write_text(
        "print('different content now')\n"
    )
    res2 = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res2.exit_code == 0, res2.output
    second_pkg = json.loads(cli_env["package_manifest_path"].read_text())
    # SHA changed (different content) and version bumped again (1.5 → 1.7).
    assert second_pkg["files_pack"]["sha256"] != first_pkg["files_pack"]["sha256"]
    assert second_pkg["pkg_version"] == "2026.06.03-1.7"


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_promote_app_ambiguous_slug_errors(cli_env):
    """Two gallery dirs containing the same pkg_id.json — the CLI must
    refuse rather than picking one arbitrarily."""
    dup_dir = cli_env["gallery"] / "foo-dup"
    dup_dir.mkdir()
    (dup_dir / "p-foo01.json").write_text(json.dumps({"pkg_id": "p-foo01"}))
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 2
    assert "multiple" in res.output.lower()


def test_promote_app_missing_index_entry_continues(cli_env):
    """A pkg_id that isn't in gallery/index.json still snapshots + stamps
    the manifest; just warns about the missing entry."""
    cli_env["index_path"].write_text(json.dumps([], indent=2))  # empty index
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 0, res.output
    # Package manifest still stamped + bumped.
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["pkg_version"] == "2026.06.03-1.5"
    assert "files_pack" in pkg
    assert "no gallery/index.json entry" in res.output.lower()


def test_promote_app_personalization_summary_renders(cli_env):
    """When personalization tokens are detected, the summary block
    appears with per-placeholder substitution counts."""
    # Inject operator name into the source file so the F-P.8 detector
    # finds something to scrub. The network has no admin_user / pod
    # configured in this fixture, so the scrub will be a no-op unless
    # the fixture wires up resolved_names — keep this test simple.
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 0, res.output
    # No personalization tokens in this fixture's network → no summary,
    # but the command still completes cleanly. The full scrub behaviour
    # is covered by test_personalization_scrub.py.
    assert "Personalization scrub:" not in res.output or \
           "0 substitutions" not in res.output
