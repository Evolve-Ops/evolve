"""Tests for evolve_admin.oc_surface — OpenClaw surface snapshot + drift-diff.

Covers:
- snapshot determinism (same input → identical bytes)
- surface collection: extensions, channels (catalog + bundled), config-schema keys
- DirSource and TarballSource produce the same file-derived surfaces
- diff: synthetic old/new → expected added/removed/changed sets, flat item list
- CLI surface: --help parsing via an injected runner; not-comparable when one
  side is uncollected
- dependency cross-reference: empty when oc_deps absent, intersects when present
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import types
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_surface as ocs


# ── Fixtures: a synthetic OpenClaw dist tree ────────────────────────────────


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _make_oc_root(
    tmp_path: Path,
    *,
    version: str = "2026.6.1",
    bundled: dict[str, str] | None = None,
    catalog: list[dict] | None = None,
    config_schema: dict | None = None,
) -> Path:
    """Build a minimal OpenClaw package root on disk. ``bundled`` maps
    extension-dir → channel-id; ``catalog`` is the channel-catalog entries."""
    root = tmp_path / "openclaw"
    _write(root / "package.json", {"name": "openclaw", "version": version})
    bundled = bundled if bundled is not None else {"signal": "signal", "telegram": "telegram"}
    for ext_dir, cid in bundled.items():
        _write(
            root / "dist" / "extensions" / ext_dir / "package.json",
            {"name": f"@openclaw/{ext_dir}", "version": version,
             "openclaw": {"channel": {"id": cid, "label": cid.title()}}},
        )
    if catalog is None:
        catalog = [{
            "name": "@openclaw/whatsapp",
            "openclaw": {
                "channel": {"id": "whatsapp", "label": "WhatsApp"},
                "install": {"clawhubSpec": "clawhub:@openclaw/whatsapp",
                            "defaultChoice": "clawhub", "minHostVersion": ">=2026.4.25"},
            },
        }]
    _write(root / "dist" / "channel-catalog.json", {"entries": catalog})
    if config_schema is None:
        config_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "auth": {"type": "object",
                         "properties": {"profilesPath": {"type": "string"}}},
                "channels": {"type": "object"},
            },
        }
    if config_schema:
        _write(root / "dist" / "config-schema.json", config_schema)
    return root


def _make_tarball(root: Path) -> bytes:
    """Re-pack a dist tree into an npm-shaped .tgz (entries under package/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                data = path.read_bytes()
                ti = tarfile.TarInfo(name=f"package/{rel}")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ── Determinism ─────────────────────────────────────────────────────────────


def test_snapshot_is_deterministic(tmp_path):
    root = _make_oc_root(tmp_path)
    a = ocs.snapshot_json(ocs.build_snapshot(ocs.DirSource(root), version="2026.6.1"))
    b = ocs.snapshot_json(ocs.build_snapshot(ocs.DirSource(root), version="2026.6.1"))
    assert a == b
    # No timestamps leak into the snapshot.
    assert "checked_at" not in a and "generated_at" not in a


def test_snapshot_json_is_sorted_and_newline_terminated(tmp_path):
    root = _make_oc_root(tmp_path)
    text = ocs.snapshot_json(ocs.build_snapshot(ocs.DirSource(root), version="x"))
    assert text.endswith("\n")
    # sort_keys means re-dumping the parsed object yields identical bytes.
    reparsed = json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert reparsed == text


# ── Surface collection ──────────────────────────────────────────────────────


def test_collects_extensions_and_channels(tmp_path):
    root = _make_oc_root(
        tmp_path,
        bundled={"signal": "signal", "telegram": "telegram", "irc": "irc"},
    )
    snap = ocs.build_snapshot(ocs.DirSource(root), version="2026.6.1")
    s = snap["surfaces"]
    assert s["extensions"] == ["irc", "signal", "telegram"]
    # Catalog channel (whatsapp) + bundled channels are unioned.
    assert sorted(s["channels"]) == ["irc", "signal", "telegram", "whatsapp"]
    assert s["channels"]["whatsapp"]["source"] == "catalog"
    assert s["channels"]["signal"]["source"] == "extensions"


def test_config_schema_keys_dotted_paths(tmp_path):
    root = _make_oc_root(tmp_path)
    snap = ocs.build_snapshot(ocs.DirSource(root), version="x")
    cfg = snap["surfaces"]["config_schema_keys"]
    assert cfg["collected"] is True
    assert "auth.profilesPath" in cfg["keys"]
    assert "auth" in cfg["keys"] and "channels" in cfg["keys"]


def test_config_schema_not_collected_when_absent(tmp_path):
    root = _make_oc_root(tmp_path, config_schema={})  # no schema file written
    snap = ocs.build_snapshot(ocs.DirSource(root), version="x")
    cfg = snap["surfaces"]["config_schema_keys"]
    assert cfg["collected"] is False
    assert cfg["keys"] == []


def test_tarball_source_matches_dir_source(tmp_path):
    root = _make_oc_root(
        tmp_path, bundled={"signal": "signal", "sms": "sms"},
    )
    dir_snap = ocs.build_snapshot(ocs.DirSource(root), version="2026.6.1")
    tar = _make_tarball(root)
    src = ocs.TarballSource(tar)
    try:
        tar_snap = ocs.build_snapshot(src, version="2026.6.1")
    finally:
        src.close()
    # File-derived surfaces are identical regardless of source.
    for surface in ("extensions", "channels", "config_schema_keys"):
        assert dir_snap["surfaces"][surface] == tar_snap["surfaces"][surface]


# ── Diff ────────────────────────────────────────────────────────────────────


def _snap(exts, chans, cfg_keys=None, cli=None, version="v"):
    return {
        "schema_version": 1,
        "oc_version": version,
        "surfaces": {
            "cli_commands": cli or {"collected": False, "commands": {}},
            "extensions": sorted(exts),
            "channels": {c: {"label": c, "source": "extensions"} for c in chans},
            "config_schema_keys": (
                {"collected": True, "keys": sorted(cfg_keys)}
                if cfg_keys is not None else {"collected": False, "keys": []}
            ),
            "dependency_fingerprints": {},
        },
    }


def test_diff_added_removed_changed(tmp_path):
    old = _snap(["signal", "telegram", "imessage"], ["signal", "telegram"],
               cfg_keys=["auth.profilesPath", "channels"], version="2026.6.1")
    new = _snap(["signal", "telegram", "whatsapp"], ["signal", "telegram", "whatsapp"],
               cfg_keys=["auth.store", "channels"], version="2026.6.5")
    diff = ocs.diff_snapshots(old, new)

    assert diff["surfaces"]["extensions"] == {"added": ["whatsapp"], "removed": ["imessage"]}
    assert diff["surfaces"]["channels"]["added"] == ["whatsapp"]
    assert diff["surfaces"]["channels"]["removed"] == []
    assert diff["surfaces"]["config_schema_keys"]["added"] == ["auth.store"]
    assert diff["surfaces"]["config_schema_keys"]["removed"] == ["auth.profilesPath"]
    assert diff["any_change"] is True
    # Flat, namespaced item list drives the cross-reference + summary.
    assert "extension:whatsapp" in diff["changed_surface_items"]
    assert "extension:imessage" in diff["changed_surface_items"]
    assert "config_key:auth.store" in diff["changed_surface_items"]
    assert "channel:whatsapp" in diff["changed_surface_items"]


def test_diff_channel_metadata_change_is_detected():
    old = {"surfaces": {"channels": {"matrix": {"label": "Matrix", "min_host_version": ">=2026.5.0"}}}}
    new = {"surfaces": {"channels": {"matrix": {"label": "Matrix", "min_host_version": ">=2026.6.0"}}}}
    diff = ocs.diff_snapshots(old, new)
    changed = diff["surfaces"]["channels"]["changed"]
    assert len(changed) == 1 and changed[0]["id"] == "matrix"
    assert "channel:matrix" in diff["changed_surface_items"]


def test_diff_no_change():
    snap = _snap(["signal"], ["signal"], cfg_keys=["a"])
    diff = ocs.diff_snapshots(snap, snap)
    assert diff["any_change"] is False
    assert diff["changed_surface_items"] == []


def test_diff_cli_comparable_only_when_both_collected():
    old = _snap(["x"], ["x"], cli={"collected": True, "commands": {"update": ["--json"]}})
    # candidate side uncollected (tarball can't run --help)
    new = _snap(["x"], ["x"], cli={"collected": False, "commands": {}})
    diff = ocs.diff_snapshots(old, new)
    assert diff["surfaces"]["cli_commands"]["comparable"] is False
    # An uncomparable CLI surface contributes nothing to the change list.
    assert diff["changed_surface_items"] == []


def test_diff_cli_flag_change():
    old = _snap(["x"], ["x"], cli={"collected": True, "commands": {"message send": ["--text"]}})
    new = _snap(["x"], ["x"], cli={"collected": True,
                                   "commands": {"message send": ["--text", "--device"]}})
    diff = ocs.diff_snapshots(old, new)
    cli = diff["surfaces"]["cli_commands"]
    assert cli["comparable"] is True
    assert cli["changed"][0]["added_flags"] == ["--device"]
    assert "cli:message send" in diff["changed_surface_items"]


# ── CLI --help parsing ──────────────────────────────────────────────────────


def test_cli_help_parsing_via_injected_runner(tmp_path):
    root = _make_oc_root(tmp_path)
    root_help = (
        "Usage: openclaw [options] [command]\n\n"
        "Options:\n  -V, --version   show version\n  --json   machine output\n\n"
        "Commands:\n  update [options]   manage updates\n  message            send messages\n"
    )
    update_help = "Usage: openclaw update [options]\n\nOptions:\n  --json   json output\n  --check  check only\n"
    message_help = "Usage: openclaw message [options]\n\nOptions:\n  --to <id>   recipient\n"

    def fake_runner(argv):
        if argv[1:] == ["--help"]:
            return 0, root_help
        if argv[1:] == ["update", "--help"]:
            return 0, update_help
        if argv[1:] == ["message", "--help"]:
            return 0, message_help
        return 1, ""

    snap = ocs.build_snapshot(
        ocs.DirSource(root), version="x",
        cli_path="/fake/openclaw", cli_runner=fake_runner,
    )
    cli = snap["surfaces"]["cli_commands"]
    assert cli["collected"] is True and cli["via"] == "help"
    assert set(cli["commands"]) >= {"", "update", "message"}
    assert "--json" in cli["commands"]["update"]
    assert "--to" in cli["commands"]["message"]


def test_cli_registry_preferred_over_help(tmp_path):
    # A static dist command registry is symmetric (works from a tarball) and
    # wins over running --help.
    root = _make_oc_root(tmp_path)
    _write(root / "dist" / "cli-manifest.json",
           {"commands": {"update": {"flags": {"json": {}, "check": {}}}}})
    called = {"runner": False}

    def fake_runner(argv):
        called["runner"] = True
        return 0, ""

    snap = ocs.build_snapshot(
        ocs.DirSource(root), version="x", cli_path="/fake", cli_runner=fake_runner,
    )
    cli = snap["surfaces"]["cli_commands"]
    assert cli["via"] == "registry"
    assert cli["commands"]["update"] == ["--check", "--json"]
    assert called["runner"] is False  # registry short-circuits the subprocess


# ── Dependency cross-reference (oc_deps) ────────────────────────────────────


def test_cross_reference_absent_oc_deps(monkeypatch):
    # Ensure no oc_deps is importable.
    monkeypatch.setitem(sys.modules, "oc_deps", None)
    diff = {"changed_surface_items": ["channel:signal", "config_key:auth.store"]}
    cross = ocs.cross_reference_dependencies(diff)
    assert cross["available"] is False
    assert cross["depended_changed"] == []


def test_cross_reference_with_oc_deps(monkeypatch):
    fake = types.ModuleType("oc_deps")
    fake.OC_DEPENDENCIES = [
        "config_key:auth.store",            # bare-string shape
        {"surface": "channel:signal"},      # dict shape
        {"surface": "extension:irc"},       # not in the diff
    ]
    monkeypatch.setitem(sys.modules, "oc_deps", fake)
    diff = {"changed_surface_items": ["channel:signal", "config_key:auth.store", "channel:matrix"]}
    cross = ocs.cross_reference_dependencies(diff)
    assert cross["available"] is True
    assert cross["depended_changed"] == ["channel:signal", "config_key:auth.store"]


def test_summarize_diff_reads_naturally():
    old = _snap(["signal", "imessage"], ["signal"], cfg_keys=["a"])
    new = _snap(["signal", "whatsapp"], ["signal", "whatsapp"], cfg_keys=["b"])
    diff = ocs.diff_snapshots(old, new)
    summary = ocs.summarize_diff(diff, {"available": True, "depended_changed": ["channel:whatsapp"]})
    assert "extension change" in summary
    assert "Evolve depends on 1" in summary
