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

from evolve_admin import plugin_signature as _ps  # noqa: E402
from evolve_admin.plugin_signature import (  # noqa: E402
    DIGEST_ALGORITHM,
    INSTALL_TREE_FILES,
    TREE_DIGEST_ALGORITHM,
    canonical_dist_digest,
    canonical_install_tree_digest,
    stamp_install_tree,
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
        """dist matches and a treeDigest is present → clean pass, no warning."""
        plugin = tmp_path / "plugin"
        files = {"index.js": b"hello"}
        self._make_plugin(plugin, files, {"id": "evolve"})
        stamp_install_tree(plugin)
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


# ── canonical_install_tree_digest — spec §4 gap 3 ────────────────────────────
#
# The dist-only digest left node_modules/ (executable code the gateway will
# not load the plugin without), package.json and package-lock.json outside
# every content control on the install path. These cover the extension.

def _make_install_tree(root: Path) -> None:
    """A minimal install dir shaped like what build_plugin() stages."""
    _write_tree(root / "dist", {"index.js": b"main", "observer/T.js": b"obs"})
    _write_tree(root / "node_modules", {
        "typebox/index.js": b"dep-code",
        "typebox/package.json": b'{"name":"typebox"}',
    })
    (root / "package.json").write_bytes(b'{"name":"evolve-plugin"}')
    (root / "package-lock.json").write_bytes(b'{"lockfileVersion":3}')
    (root / "openclaw.plugin.json").write_text(json.dumps({"id": "evolve"}))


class TestCanonicalInstallTreeDigest:
    def test_stable_across_runs(self, tmp_path):
        _make_install_tree(tmp_path)
        assert (canonical_install_tree_digest(tmp_path)
                == canonical_install_tree_digest(tmp_path))

    def test_independent_of_mtime(self, tmp_path):
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        future = time.time() + 86400
        os.utime(tmp_path / "node_modules" / "typebox" / "index.js",
                 (future, future))
        assert canonical_install_tree_digest(tmp_path) == before

    def test_detects_node_modules_content_change(self, tmp_path):
        """The gap this whole change exists to close."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        (tmp_path / "node_modules" / "typebox" / "index.js").write_bytes(
            b"dep-code; evil()")
        assert canonical_install_tree_digest(tmp_path) != before

    def test_detects_node_modules_file_added(self, tmp_path):
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        _write_tree(tmp_path / "node_modules", {"sneak/index.js": b"x"})
        assert canonical_install_tree_digest(tmp_path) != before

    def test_detects_node_modules_removed_entirely(self, tmp_path):
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        import shutil
        shutil.rmtree(tmp_path / "node_modules")
        assert canonical_install_tree_digest(tmp_path) != before

    def test_detects_package_json_change(self, tmp_path):
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        (tmp_path / "package.json").write_bytes(b'{"name":"evil"}')
        assert canonical_install_tree_digest(tmp_path) != before

    def test_detects_package_lock_change(self, tmp_path):
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        (tmp_path / "package-lock.json").write_bytes(b'{"lockfileVersion":9}')
        assert canonical_install_tree_digest(tmp_path) != before

    def test_still_covers_dist(self, tmp_path):
        """The tree digest supersedes dist coverage, it does not replace it
        with something narrower."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        (tmp_path / "dist" / "index.js").write_bytes(b"tampered")
        assert canonical_install_tree_digest(tmp_path) != before

    def test_stamping_does_not_invalidate_its_own_digest(self, tmp_path):
        """The manifest carries the stamp, so it is covered by *content* with
        the trust block stripped — writing the stamp must not change the value
        the stamp asserts."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["x-evolve-trust"] = {"treeDigest": before, "distDigest": "sha256:x"}
        # Also reformat wholesale — the digest is parse-based, not byte-based.
        m.write_text(json.dumps(data, indent=4, sort_keys=True))
        assert canonical_install_tree_digest(tmp_path) == before

    def test_detects_manifest_entrypoint_repoint(self, tmp_path):
        """Excluding the manifest outright left `main` repointable at an
        attacker-dropped file with both digests still verifying clean, without
        re-stamping anything. That is drift, not the accepted gap-2 limit."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["main"] = "evil.js"
        m.write_text(json.dumps(data))
        assert canonical_install_tree_digest(tmp_path) != before

    def test_detects_manifest_hook_grant_added(self, tmp_path):
        """`hooks` / `contracts` are capability grants — same class as `main`."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["hooks"] = {"allowConversationAccess": True}
        m.write_text(json.dumps(data))
        assert canonical_install_tree_digest(tmp_path) != before

    def test_unreadable_manifest_is_an_error(self, tmp_path):
        _make_install_tree(tmp_path)
        (tmp_path / "openclaw.plugin.json").write_text("not json {{{")
        with pytest.raises(ValueError):
            canonical_install_tree_digest(tmp_path)

    def test_ignores_unlisted_top_level_files(self, tmp_path):
        """Coverage is the set build_plugin() stages, not 'whatever is in the
        directory' — an OS-dropped .DS_Store must not fail every install."""
        _make_install_tree(tmp_path)
        before = canonical_install_tree_digest(tmp_path)
        (tmp_path / ".DS_Store").write_bytes(b"finder junk")
        assert canonical_install_tree_digest(tmp_path) == before

    def test_detects_symlink_retarget(self, tmp_path):
        """npm writes node_modules/.bin/* as symlinks. dist-v1 skips links
        outright, which would leave repointing one undetectable."""
        _make_install_tree(tmp_path)
        outside = tmp_path.parent / "real-tsc"
        outside.write_bytes(b"#!/bin/sh\n")
        evil = tmp_path.parent / "evil-tsc"
        evil.write_bytes(b"#!/bin/sh\n")  # same bytes, different path
        bindir = tmp_path / "node_modules" / ".bin"
        bindir.mkdir(parents=True)
        link = bindir / "tsc"
        link.symlink_to(outside)
        before = canonical_install_tree_digest(tmp_path)
        link.unlink()
        link.symlink_to(evil)
        assert canonical_install_tree_digest(tmp_path) != before

    def test_line_framing_has_no_path_value_collision(self, tmp_path):
        """Regression guard on the NUL framing. Under a colon-joined format
        (``l:<rel>:<target>``) these two trees produce identical lines: rel
        ``a`` + target ``b:c`` vs rel ``a:b`` + target ``c``. A symlink target
        is attacker-influenced, so the ambiguity is reachable."""
        one, two = tmp_path / "one", tmp_path / "two"
        for root in (one, two):
            (root / "node_modules").mkdir(parents=True)
            # Identical manifests — the only difference is the symlink shape.
            (root / "openclaw.plugin.json").write_text(json.dumps({"id": "evolve"}))
        (one / "node_modules" / "a").symlink_to("b:c")
        (two / "node_modules" / "a:b").symlink_to("c")
        assert (canonical_install_tree_digest(one)
                != canonical_install_tree_digest(two))

    def test_rejects_non_directory(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            canonical_install_tree_digest(tmp_path / "missing")

    def test_format(self, tmp_path):
        _make_install_tree(tmp_path)
        digest = canonical_install_tree_digest(tmp_path)
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64

    def test_differs_from_dist_digest(self, tmp_path):
        """Distinct algorithms, distinct roots — the two values must never be
        interchangeable, or a v1 stamp could satisfy a v2 check."""
        _make_install_tree(tmp_path)
        assert (canonical_install_tree_digest(tmp_path)
                != canonical_dist_digest(tmp_path / "dist"))


# ── stamp_install_tree ───────────────────────────────────────────────────────

class TestStampInstallTree:
    def test_round_trip_verifies(self, tmp_path):
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        trust = json.loads(
            (tmp_path / "openclaw.plugin.json").read_text())["x-evolve-trust"]
        assert trust["digestAlgorithm"] == DIGEST_ALGORITHM
        assert trust["treeDigestAlgorithm"] == TREE_DIGEST_ALGORITHM
        assert trust["distDigest"] != trust["treeDigest"]
        assert verify_plugin_signature(tmp_path) == (True, "")

    def test_idempotent(self, tmp_path):
        """Stamping writes into the manifest, which is outside the digest —
        so a second stamp on unchanged content is a no-op, not a churn loop."""
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        first = (tmp_path / "openclaw.plugin.json").read_text()
        first_mtime = (tmp_path / "openclaw.plugin.json").stat().st_mtime_ns
        time.sleep(0.01)
        stamp_install_tree(tmp_path)
        assert (tmp_path / "openclaw.plugin.json").read_text() == first
        assert (tmp_path / "openclaw.plugin.json").stat().st_mtime_ns == first_mtime

    def test_noop_without_manifest(self, tmp_path):
        _make_install_tree(tmp_path)
        (tmp_path / "openclaw.plugin.json").unlink()
        stamp_install_tree(tmp_path)  # must not raise
        assert not (tmp_path / "openclaw.plugin.json").exists()

    def test_noop_without_dist(self, tmp_path):
        """A half-built tree does not get blessed with a stamp."""
        _make_install_tree(tmp_path)
        import shutil
        shutil.rmtree(tmp_path / "dist")
        stamp_install_tree(tmp_path)
        data = json.loads((tmp_path / "openclaw.plugin.json").read_text())
        assert "x-evolve-trust" not in data


# ── verify_plugin_signature — install-tree coverage ──────────────────────────

class TestVerifyInstallTreeCoverage:
    def test_tampered_node_modules_detected(self, tmp_path):
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        (tmp_path / "node_modules" / "typebox" / "index.js").write_bytes(
            b"require('child_process').exec('curl evil')")
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "install-tree digest mismatch" in msg
        # dist/ is clean, so the message must not send the operator there.
        assert "node_modules" in msg

    def test_tampered_package_json_detected(self, tmp_path):
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        (tmp_path / "package.json").write_bytes(b'{"name":"evil"}')
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "install-tree digest mismatch" in msg

    def test_dist_tamper_still_reported_as_dist(self, tmp_path):
        """dist/ is checked first, so its message stays the specific one."""
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        (tmp_path / "dist" / "index.js").write_bytes(b"tampered")
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "digest mismatch" in msg
        assert "install-tree" not in msg

    def test_unknown_tree_algorithm_rejected(self, tmp_path):
        """A claim we cannot evaluate is a failure, not a pass."""
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["x-evolve-trust"]["treeDigestAlgorithm"] = "blake3-tree"
        m.write_text(json.dumps(data))
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "unsupported tree digest algorithm" in msg


# ── The staged rollout — see REQUIRE_TREE_DIGEST's docstring ─────────────────
#
# Flipping REQUIRE_TREE_DIGEST to True means inverting these two tests. They
# are deliberately adjacent so the pair is impossible to miss.

class TestTreeDigestStagedRollout:
    def _legacy_stamp(self, root: Path) -> None:
        """A manifest stamped the way builds before this change stamped it:
        distDigest only, no treeDigest."""
        _make_install_tree(root)
        stamp_manifest_in_place(
            root / "openclaw.plugin.json",
            digest=canonical_dist_digest(root / "dist"),
        )

    def test_legacy_stamp_passes_with_warning(self, tmp_path, monkeypatch):
        """Stage 1. A pod stamped by an older build must still be able to
        install — requiring the new field on day one is a fleet outage."""
        monkeypatch.setattr(_ps, "REQUIRE_TREE_DIGEST", False)
        self._legacy_stamp(tmp_path)
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is True
        assert msg != "", "a silently-degraded check is not an acceptable pass"
        assert "treeDigest" in msg
        assert "evolve-admin upgrade" in msg  # names the recovery

    def test_legacy_stamp_rejected_once_required(self, tmp_path, monkeypatch):
        """Stage 2. Once every pod has re-stamped, absence goes back to being
        a failure — the spec's 'absence of a claim is not a passing claim'."""
        monkeypatch.setattr(_ps, "REQUIRE_TREE_DIGEST", True)
        self._legacy_stamp(tmp_path)
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "treeDigest" in msg

    def test_ships_permissive(self, tmp_path):
        """Guard: this must be False in the release that introduces the field.
        Flip it only against REQUIRE_TREE_DIGEST's stated criterion."""
        assert _ps.REQUIRE_TREE_DIGEST is False

    def test_tamper_fails_closed_even_in_stage_one(self, tmp_path):
        """The permissive branch is scoped to *absence*. A stamped-and-wrong
        treeDigest fails closed regardless of the rollout stage."""
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        (tmp_path / "node_modules" / "typebox" / "index.js").write_bytes(b"evil")
        assert _ps.REQUIRE_TREE_DIGEST is False
        ok, _ = verify_plugin_signature(tmp_path)
        assert ok is False


# ── deploy.py ↔ plugin_signature.py contract ─────────────────────────────────

class TestCopySetMatchesDigestSet:
    def test_build_plugin_copies_every_digest_covered_file(self):
        """build_plugin() drives its top-level copy loop from
        INSTALL_TREE_FILES. If someone re-inlines a literal tuple there, a
        file could be staged-but-unhashed (or hashed-but-unstaged) again —
        which is exactly the class of gap this change closed."""
        import inspect
        import evolve_admin.deploy as _deploy
        src = inspect.getsource(_deploy.build_plugin)
        assert "*INSTALL_TREE_FILES" in src, (
            "build_plugin()'s copy loop must be driven by "
            "plugin_signature.INSTALL_TREE_FILES, not a literal tuple"
        )
        # And the manifest is staged but deliberately NOT in the digest set.
        assert "openclaw.plugin.json" not in INSTALL_TREE_FILES

    def test_stamp_is_called_with_the_install_dir(self):
        import inspect
        import evolve_admin.deploy as _deploy
        src = inspect.getsource(_deploy.build_plugin)
        assert "stamp_install_tree(PLUGIN_INSTALL_DIR" in src


# ── The end-to-end attack the manifest exclusion used to allow ───────────────

class TestManifestRepointIsCaught:
    def test_repointing_main_at_a_dropped_file_fails_verification(self, tmp_path):
        """Full scenario, no re-stamping: stamp a clean tree, then drop a file
        and repoint the manifest's entrypoint at it without touching dist/ or
        node_modules/. Both digests were previously still valid and the install
        proceeded. Regression guard — this must fail closed."""
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        assert verify_plugin_signature(tmp_path) == (True, "")

        (tmp_path / "evil.js").write_bytes(b"require('child_process').exec('x')")
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["main"] = "evil.js"
        m.write_text(json.dumps(data))

        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "install-tree digest mismatch" in msg
        # dist/ is untouched — the operator must not be sent there.
        assert "not in dist/" in msg


# ── The permissive branch is bounded to genuine ABSENCE ─────────────────────

class TestTreeDigestPresentButUnevaluatable:
    """A blank/None/non-string treeDigest is a claim we cannot evaluate. If it
    took the legacy path, a manifest writer could downgrade the check to
    dist-only just by blanking the field — and the warning would lie about
    why."""

    @pytest.mark.parametrize("bad", ["", None, 0, [], {}])
    def test_falsy_present_value_fails_closed(self, tmp_path, bad):
        _make_install_tree(tmp_path)
        stamp_install_tree(tmp_path)
        # Tamper too, so a permissive path would actually let something through.
        (tmp_path / "node_modules" / "typebox" / "index.js").write_bytes(b"evil")
        m = tmp_path / "openclaw.plugin.json"
        data = json.loads(m.read_text())
        data["x-evolve-trust"]["treeDigest"] = bad
        m.write_text(json.dumps(data))

        assert _ps.REQUIRE_TREE_DIGEST is False
        ok, msg = verify_plugin_signature(tmp_path)
        assert ok is False
        assert "unevaluatable" in msg
        # Must NOT claim the field is missing — it is present and wrong.
        assert "carries no" not in msg


# ── install_oc_plugin surfaces the rollout warning ──────────────────────────

class TestInstallSurfacesWarning:
    def test_warning_is_printed_when_ok_but_message(self, capsys, monkeypatch):
        """verify_plugin_signature can return (True, <warning>). The caller
        must emit it — otherwise the whole staged rollout is invisible and the
        branch could be deleted with no test noticing."""
        import evolve_admin.deploy as _deploy

        monkeypatch.setattr(
            _deploy, "verify_plugin_signature",
            lambda _d: (True, "carries no x-evolve-trust.treeDigest"),
        )
        monkeypatch.setattr(_deploy, "ensure_plugin_config", lambda *a, **k: None)
        monkeypatch.setattr(_deploy, "_clear_stale_plugin_install", lambda *a, **k: None)
        monkeypatch.setattr(_deploy, "_preflight_oc_version_match", lambda *a, **k: None)
        monkeypatch.setattr(_deploy, "run_cmd", lambda *a, **k: None)

        class _R:
            returncode = 0
            stdout = json.dumps({"valid": True})
            stderr = ""

        monkeypatch.setattr(_deploy.subprocess, "run", lambda *a, **k: _R())

        _deploy.install_oc_plugin("admin_bot", port=3000, network={
            "bots": {"admin_bot": {"port": 3000, "user": "admin_bot"}},
            "members": ["admin_bot"],
        })
        out = capsys.readouterr().out
        assert "plugin signature warning" in out
        assert "treeDigest" in out
