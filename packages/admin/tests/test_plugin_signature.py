"""tests/test_plugin_signature.py — canonical digest + manifest stamping
+ install-time verification for the signed-bypass design.

Spec: docs/spec-plugin-install-trust-2026-06-06.md.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure the in-tree package wins over any editable install at a sibling
# repo path (per CLAUDE.md's worktree-install-shadow note).
_HERE = Path(__file__).resolve()
_PKG_ROOT = _HERE.parent.parent  # packages/admin
sys.path.insert(0, str(_PKG_ROOT))

from evolve_admin.plugin_signature import (  # noqa: E402
    DIGEST_ALGORITHM,
    canonical_dist_digest,
    stamp_manifest_in_place,
    verify_plugin_signature,
)


# ── canonical_dist_digest ────────────────────────────────────────────────────

def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


class TestCanonicalDistDigest:
    def test_stable_across_runs(self, tmp_path):
        """Same content → same digest, every time."""
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha", "sub/b.js": b"beta"})
        assert canonical_dist_digest(d) == canonical_dist_digest(d)

    def test_independent_of_mtime(self, tmp_path):
        """Touching a file (mtime change, same bytes) doesn't change the digest."""
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha"})
        before = canonical_dist_digest(d)
        # Touch with a far-future mtime; content unchanged.
        os.utime(d / "a.js", (time.time() + 86400, time.time() + 86400))
        after = canonical_dist_digest(d)
        assert before == after

    def test_independent_of_creation_order(self, tmp_path):
        """Files added in different orders → same digest if content matches."""
        d1 = tmp_path / "dist1"
        d2 = tmp_path / "dist2"
        _write_tree(d1, {"a.js": b"alpha", "b.js": b"beta"})
        # Write d2 in reverse — sort makes the order irrelevant.
        _write_tree(d2, {"b.js": b"beta", "a.js": b"alpha"})
        assert canonical_dist_digest(d1) == canonical_dist_digest(d2)

    def test_changes_on_content_edit(self, tmp_path):
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha"})
        before = canonical_dist_digest(d)
        (d / "a.js").write_bytes(b"ALPHA")
        assert canonical_dist_digest(d) != before

    def test_changes_on_file_added(self, tmp_path):
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha"})
        before = canonical_dist_digest(d)
        _write_tree(d, {"c.js": b"new"})
        assert canonical_dist_digest(d) != before

    def test_changes_on_file_removed(self, tmp_path):
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha", "b.js": b"beta"})
        before = canonical_dist_digest(d)
        (d / "b.js").unlink()
        assert canonical_dist_digest(d) != before

    def test_changes_on_file_renamed(self, tmp_path):
        """Rename without content change still bumps the digest — the path is
        part of the hashed line. Protects against shuffle attacks."""
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"same"})
        before = canonical_dist_digest(d)
        (d / "a.js").rename(d / "b.js")
        assert canonical_dist_digest(d) != before

    def test_symlinks_ignored(self, tmp_path):
        """Symlinks don't contribute to the digest — they would be a path
        past content hashing (point at a file outside the tree)."""
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"alpha"})
        outside = tmp_path / "outside.js"
        outside.write_bytes(b"sneak")
        (d / "link.js").symlink_to(outside)
        # Digest is the same as the dir with only the real file.
        d_plain = tmp_path / "dist_plain"
        _write_tree(d_plain, {"a.js": b"alpha"})
        assert canonical_dist_digest(d) == canonical_dist_digest(d_plain)

    def test_rejects_non_directory(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            canonical_dist_digest(tmp_path / "missing")

    def test_format(self, tmp_path):
        d = tmp_path / "dist"
        _write_tree(d, {"a.js": b"x"})
        digest = canonical_dist_digest(d)
        assert digest.startswith("sha256:")
        # 64 hex chars after the prefix.
        assert len(digest) == len("sha256:") + 64


# ── stamp_manifest_in_place ──────────────────────────────────────────────────

class TestStampManifest:
    def test_stamps_trust_block(self, tmp_path):
        m = tmp_path / "openclaw.plugin.json"
        m.write_text(json.dumps({"id": "evolve", "main": "dist/index.js"}))
        stamp_manifest_in_place(
            m, digest="sha256:abc", built_from_commit="deadbeef"
        )
        data = json.loads(m.read_text())
        assert data["x-evolve-trust"] == {
            "distDigest": "sha256:abc",
            "digestAlgorithm": DIGEST_ALGORITHM,
            "builtFromCommit": "deadbeef",
        }

    def test_preserves_other_fields(self, tmp_path):
        m = tmp_path / "openclaw.plugin.json"
        original = {
            "id": "evolve",
            "main": "dist/index.js",
            "hooks": {"allowConversationAccess": True},
            "contracts": {"tools": ["defer", "pod_status"]},
        }
        m.write_text(json.dumps(original))
        stamp_manifest_in_place(m, digest="sha256:abc")
        data = json.loads(m.read_text())
        # Every original field still present, untouched.
        for k, v in original.items():
            assert data[k] == v

    def test_omits_commit_when_blank(self, tmp_path):
        m = tmp_path / "openclaw.plugin.json"
        m.write_text(json.dumps({"id": "evolve"}))
        stamp_manifest_in_place(m, digest="sha256:abc", built_from_commit="")
        data = json.loads(m.read_text())
        assert "builtFromCommit" not in data["x-evolve-trust"]

    def test_idempotent(self, tmp_path):
        """Re-stamping with the same digest produces no write churn."""
        m = tmp_path / "openclaw.plugin.json"
        m.write_text(json.dumps({"id": "evolve"}))
        stamp_manifest_in_place(m, digest="sha256:abc")
        first = m.read_text()
        first_mtime = m.stat().st_mtime_ns
        time.sleep(0.01)
        stamp_manifest_in_place(m, digest="sha256:abc")
        assert m.read_text() == first
        # Idempotent path returns before writing — mtime unchanged.
        assert m.stat().st_mtime_ns == first_mtime

    def test_replaces_existing_trust_block(self, tmp_path):
        m = tmp_path / "openclaw.plugin.json"
        m.write_text(json.dumps({
            "id": "evolve",
            "x-evolve-trust": {"distDigest": "sha256:old"},
        }))
        stamp_manifest_in_place(m, digest="sha256:new")
        data = json.loads(m.read_text())
        assert data["x-evolve-trust"]["distDigest"] == "sha256:new"


# ── verify_plugin_signature ──────────────────────────────────────────────────

class TestVerifyPluginSignature:
    def _make_plugin(self, plugin_dir: Path, dist_files: dict[str, bytes],
                     manifest: dict) -> None:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        _write_tree(plugin_dir / "dist", dist_files)
        (plugin_dir / "openclaw.plugin.json").write_text(json.dumps(manifest))

    def test_match_returns_ok(self, tmp_path):
        plugin = tmp_path / "plugin"
        files = {"index.js": b"hello"}
        self._make_plugin(plugin, files, {"id": "evolve"})
        digest = canonical_dist_digest(plugin / "dist")
        stamp_manifest_in_place(plugin / "openclaw.plugin.json", digest=digest)
        ok, msg = verify_plugin_signature(plugin)
        assert ok is True
        assert msg == ""

    def test_tampered_dist_detected(self, tmp_path):
        plugin = tmp_path / "plugin"
        self._make_plugin(plugin, {"index.js": b"hello"}, {"id": "evolve"})
        digest = canonical_dist_digest(plugin / "dist")
        stamp_manifest_in_place(plugin / "openclaw.plugin.json", digest=digest)
        # Tamper post-stamp.
        (plugin / "dist" / "index.js").write_bytes(b"goodbye")
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "digest mismatch" in msg
        assert "modified" in msg

    def test_unstamped_manifest_rejected(self, tmp_path):
        plugin = tmp_path / "plugin"
        self._make_plugin(plugin, {"index.js": b"hello"}, {"id": "evolve"})
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "not stamped" in msg
        # Mentions the recovery path.
        assert "rebuild" in msg

    def test_missing_manifest_rejected(self, tmp_path):
        plugin = tmp_path / "plugin"
        (plugin / "dist").mkdir(parents=True)
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "manifest not found" in msg

    def test_malformed_manifest_rejected(self, tmp_path):
        plugin = tmp_path / "plugin"
        (plugin / "dist").mkdir(parents=True)
        (plugin / "openclaw.plugin.json").write_text("not json {{{")
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "could not read manifest" in msg

    def test_missing_dist_rejected(self, tmp_path):
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        (plugin / "openclaw.plugin.json").write_text(json.dumps({
            "id": "evolve",
            "x-evolve-trust": {
                "distDigest": "sha256:abc",
                "digestAlgorithm": DIGEST_ALGORITHM,
            },
        }))
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "could not compute digest" in msg

    def test_unknown_algorithm_rejected(self, tmp_path):
        plugin = tmp_path / "plugin"
        self._make_plugin(plugin, {"index.js": b"hello"}, {
            "id": "evolve",
            "x-evolve-trust": {
                "distDigest": "sha256:abc",
                "digestAlgorithm": "blake3-something",
            },
        })
        ok, msg = verify_plugin_signature(plugin)
        assert ok is False
        assert "unsupported digest algorithm" in msg
