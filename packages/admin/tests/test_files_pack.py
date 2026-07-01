"""tests/test_files_pack.py — F-P.1 foundation for the files-pack hybrid.

Spec: docs/spec-files-pack-hybrid-2026-06-03.md.

Covers ``evolve_admin.applications.files_pack``:

  * substitute_placeholders — substitution scoped to declared
    placeholders, double-brace escapes, hard-error on empty resolution,
    rejection of unknown placeholder names
  * load_files_pack_metadata — happy path, missing-file → None,
    malformed JSON / missing format_version / bad mode / bad sha256 /
    bad files-list shape all surface as FilesPackFormatError
  * verify_files_pack_integrity — clean pass, missing-on-disk,
    sha256 mismatch, size mismatch
  * compute_files_pack_sha256 — SHA of the manifest.json file
    (deterministic across runs)
  * resolve_install_context — every-key-required validation
  * Schema v19 manifest field accepts and round-trips a files_pack
    object

No actual install behaviour change in F-P.1 — these are pure-data
primitives the install dispatcher will compose in F-P.2.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.files_pack import (  # noqa: E402
    FILES_PACK_FORMAT_VERSION,
    KNOWN_PLACEHOLDERS,
    FilesPackError,
    FilesPackFormatError,
    FilesPackPlaceholderError,
    compute_files_pack_sha256,
    load_files_pack_metadata,
    resolve_install_context,
    substitute_placeholders,
    verify_files_pack_integrity,
)
from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    ApplicationManifest,
)


# ── Test data helpers ───────────────────────────────────────────────────────


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_files_pack(
    base: Path, files: list[dict],
    *, format_version: str = FILES_PACK_FORMAT_VERSION,
    snapshot_source: dict | None = None,
) -> Path:
    """Materialise a files-pack directory under ``base`` and return its path."""
    base.mkdir(parents=True, exist_ok=True)
    meta_files = []
    for f in files:
        rel = f["path"]
        content = f.pop("_content", "")
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, int(f["mode"], 8))
        # Compute SHA if not explicitly provided (so tests can declare
        # broken SHAs deliberately).
        f.setdefault("sha256", _sha256_text(content))
        f.setdefault("size_bytes", len(content.encode("utf-8")))
        meta_files.append(f)
    manifest = {
        "format_version": format_version,
        "snapshot_source": snapshot_source or {
            "bot_id": "team-bot-a",
            "pkg_version": "2026.06.03-1.3",
            "snapshot_at": "2026-06-03T12:00:00Z",
        },
        "files": meta_files,
    }
    (base / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return base


def _ctx(**overrides) -> dict[str, str]:
    """A minimal install context with the 7 v1 placeholders populated."""
    ctx = {
        "bot_id": "personal-bot",
        "bot_user": "personal-bot",
        "workspace": "/Users/personal-bot/.openclaw/workspace",
        "shared_dir": "/Users/Shared/evolve",
        "pkg_id": "p-9bfa1c84",
        "app_id": "task-manager",
        "installed_at": "2026-06-03T08:00:00Z",
    }
    ctx.update(overrides)
    return ctx


# ── substitute_placeholders — happy paths ───────────────────────────────────


def test_substitute_replaces_declared_placeholders():
    content = "WORKSPACE={workspace}\nBOT={bot_id}\n"
    out = substitute_placeholders(content, ["workspace", "bot_id"], _ctx())
    assert "WORKSPACE=/Users/personal-bot/.openclaw/workspace" in out
    assert "BOT=personal-bot" in out


def test_substitute_leaves_undeclared_placeholders_alone():
    """The safety property — a literal `{bot_id}` in a Python docstring
    example doesn't get accidentally substituted just because the
    context has a bot_id value."""
    content = (
        'def example():\n'
        '    """Use --bot {bot_id} on the CLI."""\n'
        '    return "{workspace}"\n'
    )
    # Only declare workspace — bot_id should pass through.
    out = substitute_placeholders(content, ["workspace"], _ctx())
    assert "{bot_id}" in out
    assert "/Users/personal-bot/.openclaw/workspace" in out


def test_substitute_no_declared_returns_content_unchanged():
    """An empty declared list short-circuits — no regex pass at all."""
    content = "literal {bot_id} unchanged\nplus {{escape}} unchanged"
    out = substitute_placeholders(content, [], _ctx())
    assert out == content


def test_substitute_honours_double_brace_escapes():
    """`{{bot_id}}` -> `{bot_id}` in output (Python str.format
    convention) even when bot_id is declared. Lets the snapshot tool
    capture literal `{bot_id}` text from a source file by escaping."""
    content = (
        "real: {bot_id}\n"
        "escaped: {{bot_id}}\n"
    )
    out = substitute_placeholders(content, ["bot_id"], _ctx())
    assert "real: personal-bot" in out
    assert "escaped: {bot_id}" in out
    # And the escape characters themselves are gone.
    assert "{{" not in out
    assert "}}" not in out


def test_substitute_handles_consecutive_placeholders():
    content = "{bot_id}/{workspace}/{pkg_id}"
    out = substitute_placeholders(
        content, ["bot_id", "workspace", "pkg_id"], _ctx(),
    )
    assert out == "personal-bot//Users/personal-bot/.openclaw/workspace/p-9bfa1c84"


def test_substitute_idempotent_when_pre_substituted():
    """Running substitution on already-substituted content is a no-op
    (idempotency property — useful when the dispatcher retries)."""
    content = "WORKSPACE=/Users/personal-bot/.openclaw/workspace"
    out1 = substitute_placeholders(content, ["workspace", "bot_id"], _ctx())
    out2 = substitute_placeholders(out1, ["workspace", "bot_id"], _ctx())
    assert out1 == out2 == content


# ── substitute_placeholders — error paths ───────────────────────────────────


def test_substitute_rejects_unknown_placeholder_name():
    """A typo in metadata (e.g. ``"botid"`` instead of ``"bot_id"``) is
    caught BEFORE substitution so the operator sees the typo, not a
    silently-unmodified file."""
    with pytest.raises(FilesPackPlaceholderError, match="not in"):
        substitute_placeholders("WORKSPACE={workspace}", ["botid"], _ctx())


def test_substitute_rejects_empty_resolution_hard_error():
    """Spec §7 Q3: empty/missing context value is a hard error, not a
    silent empty substitution. Prevents files-pack installs from
    producing ``WORKSPACE=`` lines that look fine but break."""
    ctx = _ctx()
    ctx["workspace"] = ""    # operator's network.json is malformed
    with pytest.raises(FilesPackPlaceholderError, match="resolve to empty"):
        substitute_placeholders("WORKSPACE={workspace}", ["workspace"], ctx)


def test_substitute_rejects_missing_key_in_context():
    """Same hard-error contract when the context dict is missing the
    key entirely (vs containing an empty string)."""
    ctx = _ctx()
    del ctx["bot_id"]
    with pytest.raises(FilesPackPlaceholderError):
        substitute_placeholders("{bot_id}", ["bot_id"], ctx)


def test_substitute_error_names_the_problem_placeholder():
    ctx = _ctx()
    ctx["pkg_id"] = ""
    with pytest.raises(FilesPackPlaceholderError, match="pkg_id"):
        substitute_placeholders("{pkg_id}", ["pkg_id"], ctx)


# ── load_files_pack_metadata ────────────────────────────────────────────────


def test_load_metadata_returns_none_for_missing_manifest(tmp_path: Path):
    """Spec: absence of files/manifest.json means 'no files-pack',
    dispatcher falls through to LLM-forge. Distinguish from a
    malformed-but-present manifest (which raises)."""
    assert load_files_pack_metadata(tmp_path) is None
    # Also: directory itself missing.
    assert load_files_pack_metadata(tmp_path / "no-such-dir") is None


def test_load_metadata_happy_path(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "scripts/tasks.py", "mode": "0644", "_content": "print('hi')\n",
         "placeholders": []},
        {"path": "scripts/task-check.sh", "mode": "0755",
         "_content": "#!/bin/bash\necho ok\n",
         "placeholders": ["bot_id", "workspace"]},
    ])
    meta = load_files_pack_metadata(pack)
    assert meta is not None
    assert meta.format_version == "1.0"
    assert meta.snapshot_source["bot_id"] == "team-bot-a"
    assert len(meta.files) == 2
    assert meta.files[0].path == "scripts/tasks.py"
    assert meta.files[0].mode == "0644"
    assert meta.files[1].placeholders == ["bot_id", "workspace"]


def test_load_metadata_rejects_invalid_json(tmp_path: Path):
    pack = tmp_path / "fp"
    pack.mkdir()
    (pack / "manifest.json").write_text("not json {")
    with pytest.raises(FilesPackFormatError, match="could not read"):
        load_files_pack_metadata(pack)


def test_load_metadata_rejects_top_level_array(tmp_path: Path):
    pack = tmp_path / "fp"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps([{"path": "x"}]))
    with pytest.raises(FilesPackFormatError, match="JSON object"):
        load_files_pack_metadata(pack)


def test_load_metadata_rejects_missing_format_version(tmp_path: Path):
    pack = tmp_path / "fp"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        {"files": [], "snapshot_source": {}}
    ))
    with pytest.raises(FilesPackFormatError, match="format_version"):
        load_files_pack_metadata(pack)


def test_load_metadata_warns_on_unknown_format_version(tmp_path: Path, caplog):
    """Forward-compatible: tolerate newer format_version values with a
    warning. Tighten when an actual break ships."""
    pack = _write_files_pack(
        tmp_path / "fp",
        [{"path": "x", "mode": "0644", "_content": "x"}],
        format_version="9.9",
    )
    with caplog.at_level("WARNING"):
        meta = load_files_pack_metadata(pack)
    assert meta is not None
    assert any("format_version" in r.message for r in caplog.records)


def test_load_metadata_rejects_bad_mode(tmp_path: Path):
    """Mode must be POSIX octal. ``"rwxr-xr-x"`` and ``"0o755"`` are
    plausible typos to catch."""
    for bad_mode in ("rwxr-xr-x", "0o755", "755", ""):
        pack = tmp_path / f"fp-{bad_mode or 'empty'}"
        pack.mkdir()
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0",
            "snapshot_source": {},
            "files": [{
                "path": "x", "mode": bad_mode,
                "sha256": "0" * 64,
            }],
        }))
        with pytest.raises(FilesPackFormatError, match="mode|missing"):
            load_files_pack_metadata(pack)


def test_load_metadata_rejects_bad_sha256(tmp_path: Path):
    """Reject the obvious typos. (Uppercase hex is normalised to
    lowercase rather than rejected — that's a common convention
    for SHA strings and harmless.)"""
    for bad_sha in ("not-hex", "abc123", ""):
        pack = tmp_path / f"fp-{bad_sha[:6] or 'empty'}"
        pack.mkdir()
        (pack / "manifest.json").write_text(json.dumps({
            "format_version": "1.0",
            "snapshot_source": {},
            "files": [{
                "path": "x", "mode": "0644", "sha256": bad_sha,
            }],
        }))
        with pytest.raises(FilesPackFormatError, match="sha256"):
            load_files_pack_metadata(pack)


def test_load_metadata_accepts_uppercase_sha256(tmp_path: Path):
    """Uppercase SHA hex is a common convention — normalise to lowercase
    rather than rejecting outright. The integrity check uses lowercase
    internally so this round-trips correctly."""
    pack_dir = tmp_path / "fp"
    pack_dir.mkdir()
    content = "alpha\n"
    (pack_dir / "a.txt").write_text(content)
    os.chmod(pack_dir / "a.txt", 0o644)
    (pack_dir / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {},
        "files": [{
            "path": "a.txt", "mode": "0644",
            "sha256": _sha256_text(content).upper(),  # uppercase form
        }],
    }))
    meta = load_files_pack_metadata(pack_dir)
    assert meta is not None
    # Stored lowercase post-normalisation.
    assert meta.files[0].sha256 == _sha256_text(content)
    # And integrity check passes against the on-disk file.
    assert verify_files_pack_integrity(pack_dir, meta) == []


def test_load_metadata_rejects_non_list_files(tmp_path: Path):
    pack = tmp_path / "fp"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {},
        "files": "not a list",
    }))
    with pytest.raises(FilesPackFormatError, match="must be a list"):
        load_files_pack_metadata(pack)


# ── verify_files_pack_integrity ─────────────────────────────────────────────


def test_verify_integrity_clean_pack_returns_empty(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.txt", "mode": "0644", "_content": "alpha\n"},
        {"path": "b.txt", "mode": "0644", "_content": "beta\n"},
    ])
    meta = load_files_pack_metadata(pack)
    findings = verify_files_pack_integrity(pack, meta)
    assert findings == []


def test_verify_integrity_flags_missing_file(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.txt", "mode": "0644", "_content": "alpha\n"},
    ])
    # Delete the actual file but keep the metadata.
    (pack / "a.txt").unlink()
    meta = load_files_pack_metadata(pack)
    findings = verify_files_pack_integrity(pack, meta)
    assert len(findings) == 1
    assert findings[0].kind == "missing"
    assert findings[0].path == "a.txt"


def test_verify_integrity_flags_sha_mismatch(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.txt", "mode": "0644", "_content": "alpha\n"},
    ])
    # Tamper with the on-disk content.
    (pack / "a.txt").write_text("alpha-but-different\n")
    meta = load_files_pack_metadata(pack)
    findings = verify_files_pack_integrity(pack, meta)
    assert len(findings) == 1
    assert findings[0].kind == "sha_mismatch"


def test_verify_integrity_flags_size_mismatch(tmp_path: Path):
    """size_bytes mismatch is a separate finding from sha mismatch —
    the metadata's size_bytes is supposed to round-trip too."""
    pack_dir = tmp_path / "fp"
    pack_dir.mkdir()
    content = "alpha\n"
    (pack_dir / "a.txt").write_text(content)
    os.chmod(pack_dir / "a.txt", 0o644)
    # Declare a wrong size but the correct SHA, to isolate the size check.
    (pack_dir / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {},
        "files": [{
            "path": "a.txt", "mode": "0644",
            "sha256": _sha256_text(content),
            "size_bytes": 9999,
        }],
    }))
    meta = load_files_pack_metadata(pack_dir)
    findings = verify_files_pack_integrity(pack_dir, meta)
    assert len(findings) == 1
    assert findings[0].kind == "size_mismatch"


def test_verify_integrity_size_zero_skips_size_check(tmp_path: Path):
    """size_bytes=0 in the metadata means 'don't check size' — keeps
    the field optional for hand-authored manifests."""
    pack_dir = tmp_path / "fp"
    pack_dir.mkdir()
    content = "alpha\n"
    (pack_dir / "a.txt").write_text(content)
    os.chmod(pack_dir / "a.txt", 0o644)
    (pack_dir / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {},
        "files": [{
            "path": "a.txt", "mode": "0644",
            "sha256": _sha256_text(content),
            "size_bytes": 0,   # opt-out
        }],
    }))
    meta = load_files_pack_metadata(pack_dir)
    assert verify_files_pack_integrity(pack_dir, meta) == []


# ── compute_files_pack_sha256 ───────────────────────────────────────────────


def test_compute_top_level_sha_is_deterministic(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.txt", "mode": "0644", "_content": "hello\n"},
    ])
    s1 = compute_files_pack_sha256(pack)
    s2 = compute_files_pack_sha256(pack)
    assert s1 == s2
    assert len(s1) == 64


def test_compute_top_level_sha_changes_when_per_file_changes(tmp_path: Path):
    """Any per-file SHA change in the per-file manifest moves the
    top-level SHA — single 'did anything change' digest property."""
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.txt", "mode": "0644", "_content": "v1\n"},
    ])
    s1 = compute_files_pack_sha256(pack)

    # Rewrite the files-pack with new content (and therefore new
    # per-file SHA stored in the per-file manifest.json).
    pack2 = _write_files_pack(tmp_path / "fp2", [
        {"path": "a.txt", "mode": "0644", "_content": "v2\n"},
    ])
    s2 = compute_files_pack_sha256(pack2)
    assert s1 != s2


def test_compute_top_level_sha_raises_when_no_manifest(tmp_path: Path):
    with pytest.raises(FilesPackFormatError, match="no manifest"):
        compute_files_pack_sha256(tmp_path / "no-such")


# ── resolve_install_context ─────────────────────────────────────────────────


def test_resolve_install_context_happy_path():
    ctx = resolve_install_context(
        bot_id="personal-bot",
        bot_user="personal-bot",
        workspace="/Users/personal-bot/.openclaw/workspace",
        pkg_id="p-9bfa1c84",
        app_id="task-manager",
        installed_at="2026-06-03T08:00:00Z",
    )
    assert ctx["bot_id"] == "personal-bot"
    assert ctx["shared_dir"] == "/Users/Shared/evolve"  # default
    # All KNOWN_PLACEHOLDERS keys present.
    assert set(ctx.keys()) == KNOWN_PLACEHOLDERS


def test_resolve_install_context_rejects_missing_keys():
    """Centralised hard-error: the dispatcher should never reach
    substitution with an unresolved placeholder."""
    with pytest.raises(FilesPackPlaceholderError, match="missing"):
        resolve_install_context(
            bot_id="",            # missing
            bot_user="personal-bot",
            workspace="/x",
            pkg_id="p-x",
            app_id="x",
            installed_at="t",
        )


# ── Schema v20 manifest field ───────────────────────────────────────────────


def test_schema_version_is_28():
    """Pinned to surface unintended bumps. v28 adds drift_log (the
    drift-narrative log — docs/spec-apps-meta-2026-06-13.md §9.3, Bite 3);
    v27 added definition_status (the Defined/Discovered source-of-truth axis,
    §9); v26 widened the app_kind enum with "system" + stamped
    classifier_version into classification{} — Scanner Slice 2."""
    assert MANIFEST_SCHEMA_VERSION == 28


def test_manifest_files_pack_defaults_to_empty_dict():
    m = ApplicationManifest(id="task-manager", name="Task Manager", bot_id="personal-bot")
    assert m.files_pack == {}   # ← empty dict, not None


def test_manifest_files_pack_accepts_v19_shape():
    pack_meta = {
        "format_version": "1.0",
        "files_count": 6,
        "snapshot_source_pkg_version": "2026.06.03-1.3",
        "sha256": "a" * 64,
    }
    m = ApplicationManifest(
        id="task-manager", name="Task Manager",
        bot_id="personal-bot", files_pack=pack_meta,
    )
    assert m.files_pack == pack_meta
    assert m.files_pack["format_version"] == "1.0"


# ── Exception hierarchy sanity (the dispatcher will catch FilesPackError) ──


def test_all_specific_errors_inherit_from_FilesPackError():
    """A future dispatcher wanting "any files-pack error → fall back to
    LLM-forge" can ``except FilesPackError`` and catch all of them."""
    for sub in (FilesPackPlaceholderError, FilesPackFormatError):
        assert issubclass(sub, FilesPackError)
