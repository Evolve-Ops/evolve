"""F-P.10.x — promote-app per-file provenance annotation tests.

Covers the smart-forge model integration on top of promote-app:

  - --bundle-only PATTERN filters the snapshot to just matching paths,
    deletes the rest from the files-pack on disk, and writes
    provenance=forge annotations for the unmatched paths.
  - --partial stamps partial=true on the files-pack metadata without
    requiring patterns (operator opt-in).
  - The package manifest's files[] gets per-file provenance written
    so the F-P.4.x integrity sweep and the smart-forge dispatcher
    see the operator's intent.
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


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    """Pod + gallery wired up with a source bot whose install has THREE
    files — lets us test selective bundling (e.g. scripts/*.py only)."""
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    # Two scripts + one markdown template — the operator wants the
    # scripts bundled, markdown forge-generated per bot.
    (workspace / "scripts/tasks.py").write_text("print('tasks')\n")
    (workspace / "scripts/notify.py").write_text("print('notify')\n")
    (workspace / "HEARTBEAT.template.md").write_text(
        "# Heartbeat — gets tailored per bot\n"
    )
    os.chmod(workspace / "scripts/tasks.py", 0o644)
    os.chmod(workspace / "scripts/notify.py", 0o644)
    os.chmod(workspace / "HEARTBEAT.template.md", 0o644)
    (workspace / "manifests/foo.json").write_text(json.dumps({
        "id": "foo-app", "name": "Foo App",
        "bot_id": bot_user,
        "pkg_id": "p-foo01", "pkg_version": "2026.06.03-1.0",
        "files": [
            {"path": "scripts/tasks.py", "sha256": "x"},
            {"path": "scripts/notify.py", "sha256": "x"},
            {"path": "HEARTBEAT.template.md", "sha256": "x"},
        ],
    }))

    gallery = tmp_path / "gallery"
    (gallery / "foo").mkdir(parents=True)
    (gallery / "foo" / "p-foo01.json").write_text(json.dumps({
        "pkg_id": "p-foo01",
        "pkg_version": "2026.06.03-1.3",
        "display_name": "Foo App",
    }, indent=2) + "\n")
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
        "bot_user": bot_user,
        "gallery": gallery,
        "package_manifest_path": gallery / "foo" / "p-foo01.json",
        "files_pack_dir": gallery / "foo" / "files",
    }


def _invoke(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


# ── --bundle-only — selective bundling ──────────────────────────────────────


def test_bundle_only_filters_files_pack(cli_env):
    """--bundle-only 'scripts/*.py' bundles the two .py scripts and
    marks the markdown as forge. The files-pack on disk has only the
    two scripts after promote-app runs."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bundle-only", "scripts/*.py",
    )
    assert res.exit_code == 0, res.output

    # Files-pack contents: only the two .py scripts.
    assert (cli_env["files_pack_dir"] / "scripts/tasks.py").is_file()
    assert (cli_env["files_pack_dir"] / "scripts/notify.py").is_file()
    assert not (cli_env["files_pack_dir"] / "HEARTBEAT.template.md").exists()

    # Files-pack metadata: partial=true + 2 files.
    fp_meta = json.loads(
        (cli_env["files_pack_dir"] / "manifest.json").read_text()
    )
    assert fp_meta["partial"] is True
    assert len(fp_meta["files"]) == 2
    pack_paths = {f["path"] for f in fp_meta["files"]}
    assert pack_paths == {"scripts/tasks.py", "scripts/notify.py"}


def test_bundle_only_writes_provenance_to_package_manifest(cli_env):
    """Each manifest.files[] entry carries provenance="bundled" |
    "forge" so the F-P.4.x sweep + smart-forge dispatcher can read
    operator intent."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bundle-only", "scripts/*.py",
    )
    assert res.exit_code == 0, res.output

    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    files = pkg.get("files") or []
    by_path = {f["path"]: f for f in files}
    assert by_path["scripts/tasks.py"]["provenance"] == "bundled"
    assert by_path["scripts/notify.py"]["provenance"] == "bundled"
    assert by_path["HEARTBEAT.template.md"]["provenance"] == "forge"


def test_bundle_only_stamps_partial_on_package_manifest(cli_env):
    """package_manifest.files_pack.partial=true so the F-P.4.x sweep
    knows this is a partial pack."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bundle-only", "scripts/*.py",
    )
    assert res.exit_code == 0, res.output
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["files_pack"]["partial"] is True
    assert pkg["files_pack"]["files_count"] == 2


def test_bundle_only_multiple_patterns_union(cli_env):
    """Multiple --bundle-only flags union — file matches ANY pattern
    is bundled."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bundle-only", "scripts/tasks.py",
        "--bundle-only", "HEARTBEAT.template.md",
    )
    assert res.exit_code == 0, res.output
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    by_path = {f["path"]: f for f in pkg["files"]}
    assert by_path["scripts/tasks.py"]["provenance"] == "bundled"
    assert by_path["HEARTBEAT.template.md"]["provenance"] == "bundled"
    # notify.py wasn't named by either pattern → forge.
    assert by_path["scripts/notify.py"]["provenance"] == "forge"


def test_bundle_only_top_level_sha_changes_after_trim(cli_env):
    """The trimmed files-pack has fewer per-file entries → different
    top-level SHA. Package manifest's files_pack.sha256 reflects the
    trimmed state, not the pre-trim snapshot."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--bundle-only", "scripts/*.py",
    )
    assert res.exit_code == 0, res.output
    from evolve_admin.applications.files_pack import compute_files_pack_sha256
    actual_sha = compute_files_pack_sha256(cli_env["files_pack_dir"])
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    assert pkg["files_pack"]["sha256"] == actual_sha


# ── --partial without filtering ────────────────────────────────────────────


def test_partial_flag_stamps_metadata_without_filtering(cli_env):
    """--partial stamps partial=true on both metadata layers without
    requiring --bundle-only patterns. All files stay bundled; the
    operator just declares 'more coming later'."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
        "--partial",
    )
    assert res.exit_code == 0, res.output

    # Files-pack still has all 3 files.
    fp_meta = json.loads(
        (cli_env["files_pack_dir"] / "manifest.json").read_text()
    )
    assert len(fp_meta["files"]) == 3
    assert fp_meta["partial"] is True

    # Package manifest: every file bundled, partial=true on the block.
    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    by_path = {f["path"]: f for f in pkg["files"]}
    assert by_path["scripts/tasks.py"]["provenance"] == "bundled"
    assert by_path["HEARTBEAT.template.md"]["provenance"] == "bundled"
    assert pkg["files_pack"]["partial"] is True


# ── No flags — backward compat ─────────────────────────────────────────────


def test_default_no_partial_no_filter(cli_env):
    """Without --bundle-only or --partial, today's all-bundled behavior
    is preserved: every file bundled, no partial flag."""
    res = _invoke(
        "promote-app",
        "--bot", cli_env["bot_user"],
        "--pkg", "p-foo01",
    )
    assert res.exit_code == 0, res.output

    fp_meta = json.loads(
        (cli_env["files_pack_dir"] / "manifest.json").read_text()
    )
    # When partial flag not set, the field should be absent (not
    # explicitly false) — the loader defaults to False anyway.
    assert fp_meta.get("partial", False) is False
    assert len(fp_meta["files"]) == 3

    pkg = json.loads(cli_env["package_manifest_path"].read_text())
    # All files still get provenance annotations (default snapshot path
    # marks everything bundled).
    by_path = {f["path"]: f for f in pkg["files"]}
    assert by_path["scripts/tasks.py"]["provenance"] == "bundled"
    assert by_path["scripts/notify.py"]["provenance"] == "bundled"
    assert by_path["HEARTBEAT.template.md"]["provenance"] == "bundled"
    assert pkg["files_pack"].get("partial", False) is False


# ── Trim helper unit test ──────────────────────────────────────────────────


def test_trim_files_pack_to_subset_deletes_non_matched(tmp_path: Path):
    pack = tmp_path / "files"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts/foo.py").write_text("foo\n")
    (pack / "scripts/bar.sh").write_text("bar\n")
    os.chmod(pack / "scripts/foo.py", 0o644)
    os.chmod(pack / "scripts/bar.sh", 0o755)
    import hashlib
    foo_sha = hashlib.sha256(b"foo\n").hexdigest()
    bar_sha = hashlib.sha256(b"bar\n").hexdigest()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {},
        "files": [
            {"path": "scripts/foo.py", "mode": "0644",
             "sha256": foo_sha, "size_bytes": 4, "placeholders": []},
            {"path": "scripts/bar.sh", "mode": "0755",
             "sha256": bar_sha, "size_bytes": 4, "placeholders": []},
        ],
    }))
    count, sha = cli._trim_files_pack_to_subset(
        pack, {"scripts/foo.py"}, mark_partial=True,
    )
    assert count == 1
    assert (pack / "scripts/foo.py").is_file()
    assert not (pack / "scripts/bar.sh").exists()
    meta = json.loads((pack / "manifest.json").read_text())
    assert meta["partial"] is True
    assert len(meta["files"]) == 1
    assert meta["files"][0]["path"] == "scripts/foo.py"
