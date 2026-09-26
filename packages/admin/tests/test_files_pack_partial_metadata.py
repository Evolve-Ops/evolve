"""F-P.4.x — partial files-pack metadata + integrity contract.

Covers the two metadata extensions and the cross-reference logic
the integrity sweep uses to honor the smart-forge model:

  - FilesPackMetadata.partial — operator-stamped intent
  - FilesPackMetadata.coverage_intent — free-form review hint

The gallery-wide sweep (test_files_pack_integrity.py) reads these
to decide whether a manifest's bundled-but-missing entries are
errors (default) or warnings (when partial=true).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.files_pack import (  # noqa: E402
    FilesPackMetadata,
    load_files_pack_metadata,
)


# ── FilesPackMetadata defaults ──────────────────────────────────────────────


def test_metadata_partial_defaults_to_false():
    """Backward compat: pre-existing files-packs that don't carry the
    field default to partial=False. The integrity sweep treats them
    as complete-coverage, same as before this PR."""
    m = FilesPackMetadata(
        format_version="1.0",
        snapshot_source={},
        files=[],
    )
    assert m.partial is False
    assert m.coverage_intent == ""


# ── Loader behaviour ──────────────────────────────────────────────────────


@pytest.fixture
def write_manifest(tmp_path: Path):
    """Returns a helper that writes a minimal files-pack manifest dict
    to ``<tmp>/files/manifest.json`` and returns the pack dir."""
    def _write(extra: dict) -> Path:
        pack = tmp_path / "files"
        pack.mkdir(exist_ok=True)
        base = {
            "format_version": "1.0",
            "snapshot_source": {
                "pkg_id": "p-test",
                "pkg_version": "1.0",
                "snapshot_at": "2026-06-04T00:00:00Z",
            },
            "files": [],
        }
        base.update(extra)
        (pack / "manifest.json").write_text(json.dumps(base))
        return pack
    return _write


def test_loader_reads_partial_flag_when_present(write_manifest):
    pack = write_manifest({"partial": True})
    metadata = load_files_pack_metadata(pack)
    assert metadata is not None
    assert metadata.partial is True


def test_loader_reads_coverage_intent_when_present(write_manifest):
    pack = write_manifest({"coverage_intent": "stable_scripts"})
    metadata = load_files_pack_metadata(pack)
    assert metadata is not None
    assert metadata.coverage_intent == "stable_scripts"


def test_loader_strips_coverage_intent_whitespace(write_manifest):
    pack = write_manifest({"coverage_intent": "  doc_skeletons  "})
    metadata = load_files_pack_metadata(pack)
    assert metadata.coverage_intent == "doc_skeletons"


def test_loader_partial_defaults_to_false_when_absent(write_manifest):
    """Existing files-packs without the field still load cleanly with
    partial=False (the default)."""
    pack = write_manifest({})
    metadata = load_files_pack_metadata(pack)
    assert metadata.partial is False
    assert metadata.coverage_intent == ""


def test_loader_treats_non_bool_partial_as_falsy(write_manifest):
    """Garbage-in-garbage-out safety: a string or null in `partial`
    coerces to bool — empty string / None → False, truthy → True."""
    pack = write_manifest({"partial": ""})
    assert load_files_pack_metadata(pack).partial is False
    pack = write_manifest({"partial": "yes"})  # truthy string
    assert load_files_pack_metadata(pack).partial is True


# ── Integrity-sweep contract (cross-reference behaviour) ────────────────────
#
# The actual gallery sweep tests live in test_files_pack_integrity.py and
# parametrize over the real gallery dir. These unit tests verify the
# building blocks: that load_files_pack_metadata correctly surfaces the
# fields the sweep reads.


def test_partial_metadata_carries_through_full_pipeline(write_manifest):
    """End-to-end on the metadata: write a manifest with all three new
    fields populated and confirm the loader exposes them all so the
    sweep + review UIs can read them."""
    pack = write_manifest({
        "partial": True,
        "coverage_intent": "stable_scripts",
        "files": [
            {"path": "scripts/foo.py", "mode": "0644",
             "sha256": "0" * 64, "size_bytes": 0, "placeholders": []},
        ],
    })
    metadata = load_files_pack_metadata(pack)
    assert metadata.partial is True
    assert metadata.coverage_intent == "stable_scripts"
    assert len(metadata.files) == 1
    assert metadata.files[0].path == "scripts/foo.py"
