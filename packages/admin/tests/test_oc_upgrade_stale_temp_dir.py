"""Tests for the npm stale-temp-dir hint classifier in
evolve_admin.ocadmin.

When `npm install -g openclaw` fails with ENOTEMPTY because a prior
failed install left a `.openclaw-XXX` sibling in node_modules, the
upgrade command needs to produce a tailored remediation hint that
names the stale path. For any other npm failure it should fall back
to the generic registry/disk/sudoers hint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import ocadmin


def _make_install(
    d: Path, *, version: str | None = "2026.6.10", mjs: bool = True, dist: bool = True
) -> Path:
    """Materialize a (possibly broken) openclaw install dir for tests. A
    COMPLETE install needs the manifest + both entrypoints (openclaw.mjs +
    dist/index.js); omit any to model a husk/truncated extraction."""
    d.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (d / "package.json").write_text(json.dumps({"name": "openclaw", "version": version}))
    if mjs:
        (d / "openclaw.mjs").write_text("// entrypoint\n")
    if dist:
        (d / "dist").mkdir(exist_ok=True)
        (d / "dist" / "index.js").write_text("// gateway entry\n")
    return d


# Real ENOTEMPTY stderr captured from a failed `sudo npm install -g
# --prefix=/opt/homebrew openclaw@...` on the test pod mini, 2026-05-22.
_REAL_ENOTEMPTY_STDERR = """\
npm error code ENOTEMPTY
npm error syscall rename
npm error path /opt/homebrew/lib/node_modules/openclaw
npm error dest /opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q
npm error errno -66
npm error ENOTEMPTY: directory not empty, rename \
'/opt/homebrew/lib/node_modules/openclaw' -> '/opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q'
"""


def test_enotempty_stale_temp_dir_produces_tailored_hint():
    hint = ocadmin._format_npm_install_error_hint(_REAL_ENOTEMPTY_STDERR)
    # Names the specific stale path so the operator can copy-paste the rm.
    assert "/opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q" in hint
    # Includes the actual remediation command, not just an explanation.
    assert "sudo rm -rf /opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q" in hint
    # Does not fall through to the generic hint when we know the cause.
    assert "registry unreachable" not in hint


def test_enotempty_without_openclaw_temp_dir_uses_generic_hint():
    # Some other package's ENOTEMPTY shouldn't trigger our tailored path.
    stderr = (
        "npm error code ENOTEMPTY\n"
        "npm error syscall rename\n"
        "npm error dest /opt/homebrew/lib/node_modules/.some-other-pkg-XYZ\n"
    )
    hint = ocadmin._format_npm_install_error_hint(stderr)
    assert "registry unreachable" in hint
    assert ".openclaw-" not in hint


def test_unrelated_npm_failure_uses_generic_hint():
    stderr = (
        "npm error code E404\n"
        "npm error 404 Not Found - GET https://registry.npmjs.org/openclaw\n"
    )
    hint = ocadmin._format_npm_install_error_hint(stderr)
    assert "registry unreachable" in hint
    assert "stale npm temp dir" not in hint


def test_enotempty_with_unparseable_path_falls_back_to_wildcard():
    # ENOTEMPTY mentioned with `.openclaw-` but the absolute path isn't
    # extractable from this truncated form — the hint should still
    # surface remediation, falling back to a wildcard under the known
    # node_modules dir.
    stderr = "npm error ENOTEMPTY: directory not empty, .openclaw-"
    hint = ocadmin._format_npm_install_error_hint(stderr)
    assert "stale npm temp dir" in hint
    assert ".openclaw-*" in hint


# ── install-health classifier ────────────────────────────────────────────────


def test_install_health_complete_install_is_healthy(tmp_path):
    d = _make_install(tmp_path / "openclaw")
    assert ocadmin._openclaw_install_is_healthy(d) is True


def test_install_health_missing_entrypoint_is_broken(tmp_path):
    # The mini 2026-06-30 husk: package.json gone, no entrypoints.
    d = _make_install(tmp_path / "openclaw", version=None, mjs=False, dist=False)
    assert ocadmin._openclaw_install_is_healthy(d) is False
    assert ocadmin._openclaw_install_has_manifest(d) is False


def test_install_health_pkgjson_without_entrypoint_is_broken(tmp_path):
    d = _make_install(tmp_path / "openclaw", version="2026.6.10", mjs=False, dist=False)
    assert ocadmin._openclaw_install_is_healthy(d) is False
    # …but it HAS a manifest, so recovery would never destroy it.
    assert ocadmin._openclaw_install_has_manifest(d) is True


def test_install_health_mjs_without_dist_is_broken(tmp_path):
    # Truncated extraction: manifest + openclaw.mjs but no dist/index.js.
    d = _make_install(tmp_path / "openclaw", version="2026.6.10", mjs=True, dist=False)
    assert ocadmin._openclaw_install_is_healthy(d) is False


def test_install_health_pkgjson_without_version_is_broken(tmp_path):
    d = tmp_path / "openclaw"
    d.mkdir()
    (d / "package.json").write_text(json.dumps({"name": "openclaw"}))  # no version
    (d / "openclaw.mjs").write_text("// entrypoint\n")
    assert ocadmin._openclaw_install_is_healthy(d) is False


def test_install_health_unparseable_pkgjson_is_broken(tmp_path):
    d = tmp_path / "openclaw"
    d.mkdir()
    (d / "package.json").write_text("{ not json")
    (d / "openclaw.mjs").write_text("// entrypoint\n")
    assert ocadmin._openclaw_install_is_healthy(d) is False


# ── stale-temp-dir cleanup: inverted-swap guard ──────────────────────────────


def test_cleanup_promotes_complete_staging_when_live_is_broken(tmp_path, monkeypatch):
    """The mini 2026-06-30 case: live `openclaw` is a broken husk, a single
    complete `.openclaw-XXX` sibling holds the only good install. Cleanup must
    PROMOTE it (rm husk + mv staging) — never delete it as stale residue."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    live = _make_install(nm / "openclaw", version=None, mjs=False, dist=False)  # husk
    staged = _make_install(nm / ".openclaw-2N5mgx4q", version="2026.6.10")  # complete

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)
    monkeypatch.setattr(ocadmin.click, "confirm", lambda *a, **k: True)

    # Emulate `sudo /bin/rm -rf` / `sudo /bin/mv` without privileges.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["sudo", "/bin/rm", "-rf"]:
            import shutil
            shutil.rmtree(cmd[3], ignore_errors=True)
        elif cmd[:2] == ["sudo", "/bin/mv"]:
            Path(cmd[2]).rename(cmd[3])

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(ocadmin.subprocess, "run", fake_run)

    assert ocadmin._check_and_clean_stale_npm_temp_dirs() is True
    # Promoted: staging gone, live now the complete install.
    assert not staged.exists()
    assert ocadmin._openclaw_install_is_healthy(live)
    # It moved (mv), it did NOT just delete the staging dir.
    assert any(c[:2] == ["sudo", "/bin/mv"] for c in calls)


def test_cleanup_deletes_residue_when_live_is_healthy(tmp_path, monkeypatch):
    """Normal stale-residue case: live install is healthy, the `.openclaw-XXX`
    sibling is genuine junk → existing delete flow, never a promote."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    _make_install(nm / "openclaw", version="2026.6.10")  # healthy live
    staged = _make_install(nm / ".openclaw-deadbeef", version="2026.6.10")

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)
    monkeypatch.setattr(ocadmin.click, "confirm", lambda *a, **k: True)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["sudo", "/bin/rm", "-rf"]:
            import shutil
            shutil.rmtree(cmd[3], ignore_errors=True)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(ocadmin.subprocess, "run", fake_run)

    assert ocadmin._check_and_clean_stale_npm_temp_dirs() is True
    assert not staged.exists()  # deleted as residue
    # Delete path only — no mv/promote.
    assert all(c[:2] != ["sudo", "/bin/mv"] for c in calls)


def test_cleanup_refuses_when_multiple_complete_staging_and_live_broken(tmp_path, monkeypatch):
    """Live broken but two complete staging siblings → refuse to guess; abort
    so the operator picks. Never deletes either complete install."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    _make_install(nm / "openclaw", version=None, mjs=False, dist=False)  # husk
    s1 = _make_install(nm / ".openclaw-aaa111", version="2026.6.10")
    s2 = _make_install(nm / ".openclaw-bbb222", version="2026.6.9")

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)

    def _boom(cmd, **kwargs):  # cleanup must not shell out in this path
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(ocadmin.subprocess, "run", _boom)

    assert ocadmin._check_and_clean_stale_npm_temp_dirs() is False
    assert s1.exists() and s2.exists()  # both preserved


# ── review-hardening regressions ─────────────────────────────────────────────


def _fake_run_factory(calls):
    """A subprocess.run stand-in that emulates `sudo -v` (rc 0), `sudo /bin/rm
    -rf` (rmtree), and `sudo /bin/mv` (rename) without privileges."""
    import shutil

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["sudo", "/bin/rm", "-rf"]:
            shutil.rmtree(cmd[3], ignore_errors=True)
        elif cmd[:2] == ["sudo", "/bin/mv"]:
            Path(cmd[2]).rename(cmd[3])

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    return fake_run


def test_cleanup_does_not_destroy_manifest_bearing_live(tmp_path, monkeypatch):
    """Safety (review F5): if the live install has a valid manifest it is NEVER
    destroyed by recovery, even if our entrypoint check reads it as unhealthy
    (e.g. an upstream layout change). Recovery must not fire; live survives."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    # Manifest present + version, but entrypoints absent by our check.
    live = _make_install(nm / "openclaw", version="2026.6.10", mjs=False, dist=False)
    _make_install(nm / ".openclaw-aaa111", version="2026.6.10")  # complete sibling

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)
    monkeypatch.setattr(ocadmin.click, "confirm", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(ocadmin.subprocess, "run", _fake_run_factory(calls))

    ocadmin._check_and_clean_stale_npm_temp_dirs()
    # Live (manifest-bearing) preserved — recovery never promoted over it.
    assert live.exists() and (live / "package.json").exists()
    assert all(c[:2] != ["sudo", "/bin/mv"] for c in calls)


def test_cleanup_symlink_sibling_is_not_promoted(tmp_path, monkeypatch):
    """Safety (review F2): a `.openclaw-*` SYMLINK to a complete install is never
    a promote source (mv would move the link, not a tree). Live broken + only a
    symlink sibling → no promote; falls through to delete-residue."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    _make_install(nm / "openclaw", version=None, mjs=False, dist=False)  # husk
    real = _make_install(tmp_path / "elsewhere" / "good", version="2026.6.10")
    link = nm / ".openclaw-link"
    link.symlink_to(real)

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)
    monkeypatch.setattr(ocadmin.click, "confirm", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(ocadmin.subprocess, "run", _fake_run_factory(calls))

    ocadmin._check_and_clean_stale_npm_temp_dirs()
    # The symlink was treated as residue (delete path), never mv-promoted; the
    # real target it pointed at is untouched.
    assert all(c[:2] != ["sudo", "/bin/mv"] for c in calls)
    assert real.exists() and (real / "openclaw.mjs").exists()


def test_cleanup_clears_leftover_residue_after_promote(tmp_path, monkeypatch):
    """Completeness (review F1): after promoting the one complete staging dir,
    any OTHER incomplete `.openclaw-XXX` siblings (which would still ENOTEMPTY a
    subsequent npm install) are cleared via the normal delete flow."""
    nm = tmp_path / "node_modules"
    nm.mkdir(parents=True)
    live = _make_install(nm / "openclaw", version=None, mjs=False, dist=False)  # husk
    good = _make_install(nm / ".openclaw-good", version="2026.6.10")  # complete
    junk = _make_install(nm / ".openclaw-junk", version=None, mjs=False, dist=False)

    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)
    monkeypatch.setattr(ocadmin.click, "confirm", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(ocadmin.subprocess, "run", _fake_run_factory(calls))

    assert ocadmin._check_and_clean_stale_npm_temp_dirs() is True
    # The complete dir was promoted into live…
    assert ocadmin._openclaw_install_is_healthy(live)
    assert not good.exists()
    assert any(c[:2] == ["sudo", "/bin/mv"] for c in calls)
    # …and the leftover incomplete sibling was cleared, not left to ENOTEMPTY.
    assert not junk.exists()
