"""tests/test_disk_reclaim_apply.py — the privileged disk-reclaim reaper (PR2).

Companion to test_disk_reclaim.py (PR1, the read-only scanner). Covers
disk_reclaim_apply.reclaim / handle_reclaim_request and the
POST /api/host-health/reclaim endpoint after the PR2 audit (Option B):

  - the reaper acts ONLY on what a fresh scan_reclaimable reports
    (empty/cleared paths are never touched);
  - npm caches are `rm`'d (whole _cacache/_npx leaf, never .npm wholesale);
  - LOGS ARE NEVER RECLAIMED — the truncate primitive is gone; the scanner
    still reports their size but the reaper skips them with a reason;
  - the privileged delete resolves every path component with O_NOFOLLOW and
    pins the delete to the verified parent dirfd, so an INTERMEDIATE-component
    symlink (a `.npm` swapped for a symlink, before OR after the walk) can
    never redirect the rm out of the bot's home;
  - symlink / `..` / wrong-shape / outside-root paths are rejected;
  - partial scans and per-path failures degrade safely (tri-state, never a
    silent success);
  - the endpoint returns freed bytes + a fresh disk reading and is idempotent.

The sudo/fs layer is injected via the runner seam (a real `rm` never runs
under sudo here): RecordingRunner records every (parent_fd, leaf) call and
performs the equivalent delete relative to the verified fd — exactly mirroring
production's `fchdir`-pinned `rm -rf -- <leaf>` — so idempotency, "acts only on
scanned paths", and the no-escape proof are exercised end-to-end.
Caps are monkeypatched tiny so fixture logs cost no real disk (sparse files).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import disk_reclaim, disk_reclaim_apply  # noqa: E402

KB = 1024
SMALL_CAP = 8 * KB  # tiny caps so sparse fixtures are cheap


@pytest.fixture(autouse=True)
def _tiny_caps(monkeypatch):
    """Shrink the size caps so a sparse few-KB file counts as 'oversized'."""
    monkeypatch.setattr(disk_reclaim, "EVOLVE_LOG_CAP", SMALL_CAP)
    monkeypatch.setattr(disk_reclaim, "OC_LOG_CAP", SMALL_CAP)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def _fixture_tree(base: Path) -> Path:
    """A /Users-shaped tree with reclaimable npm caches + (non-reclaimable) logs.

    Returns the ROOT to pass as ``roots`` (its children are homes). The logs
    are present so tests can assert the reaper LEAVES them alone (the scanner
    still sizes them).
    """
    users = base / "Users"
    home = users / "alice"
    _sparse(home / ".npm" / "_cacache" / "index" / "a", 4 * KB)
    _sparse(home / ".npm" / "_cacache" / "b", 3 * KB)
    _sparse(home / ".npm" / "_npx" / "x", 2 * KB)
    _sparse(home / ".evolve" / "logs" / "evolve.log", SMALL_CAP + KB)   # over cap
    _sparse(home / ".evolve" / "logs" / "small.log", KB)               # under cap
    _sparse(home / ".openclaw" / "logs" / "openclaw.1.log", SMALL_CAP + KB)  # rotated, over
    _sparse(home / ".openclaw" / "logs" / "openclaw.log", 10 * KB)     # live log
    return users


class RecordingRunner:
    """Runner seam fake mirroring SudoReclaimRunner.rm_leaf.

    Records every (parent_inode, leaf) call and performs the delete the SAME
    way production does — ``fchdir`` to the verified parent fd, then delete the
    BARE leaf name relative to it — so the fixture is mutated (idempotency) and
    the delete provably lands inside the pinned inode. ``fail`` is a set of
    leaf names that simulate a sudo/fs failure (records the call, no mutation).
    """

    def __init__(self, *, fail=()):
        self.rm_leaves: list[str] = []
        self.rm_calls: list[tuple[int, str]] = []  # (parent st_ino, leaf)
        self.fail = set(fail)

    def rm_leaf(self, parent_fd, leaf):
        self.rm_leaves.append(leaf)
        self.rm_calls.append((os.fstat(parent_fd).st_ino, leaf))
        if leaf in self.fail:
            return False, "sudo: a password is required"
        cwd_fd = os.open(".", os.O_RDONLY)
        try:
            os.fchdir(parent_fd)            # pin to the verified parent inode
            shutil.rmtree(leaf, ignore_errors=True)  # bare leaf, relative
        finally:
            os.fchdir(cwd_fd)
            os.close(cwd_fd)
        return True, "ok"


# ── The truncate primitive is gone (audit Option B) ───────────────────────────


def test_no_truncate_or_log_reclaim_primitive_remains():
    # The runner exposes ONLY the npm-cache rm leaf; no truncate, no rm_tree.
    assert hasattr(disk_reclaim_apply.SudoReclaimRunner, "rm_leaf")
    assert not hasattr(disk_reclaim_apply.SudoReclaimRunner, "truncate")
    assert not hasattr(disk_reclaim_apply.SudoReclaimRunner, "rm_tree")
    # No truncate binary literal survives in the module (the docstring still
    # explains WHY truncate was dropped; only executable references must go).
    assert not hasattr(disk_reclaim_apply, "_TRUNCATE")
    src = Path(disk_reclaim_apply.__file__).read_text()
    assert "/usr/bin/truncate" not in src
    assert "os.truncate" not in src
    # Only the npm cache is reclaimable; logs are reported but never reaped.
    assert disk_reclaim_apply.RECLAIMABLE_CATEGORIES == frozenset(
        {disk_reclaim.CAT_NPM_CACHE})
    assert disk_reclaim.CAT_EVOLVE_LOGS not in disk_reclaim_apply.RECLAIMABLE_CATEGORIES
    assert disk_reclaim.CAT_OC_ROTATED not in disk_reclaim_apply.RECLAIMABLE_CATEGORIES


# ── Safety gate: cheap string-shape classifier ────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/Users/alice/.npm/_cacache", disk_reclaim.CAT_NPM_CACHE),
        ("/Users/alice/.npm/_npx", disk_reclaim.CAT_NPM_CACHE),
        # — rejections —
        ("/Users/alice/.npm", None),                       # .npm wholesale
        ("/Users/alice/.npm/_cacache/sub", None),          # nested under leaf
        ("/Users/alice/.npm/../.ssh", None),               # .. escape
        ("/Users/alice/.npm/_other", None),                # not a known leaf
        # logs are NO LONGER a reclaim target (npm-only reaper)
        ("/Users/alice/.evolve/logs/run.log", None),
        ("/Users/alice/.openclaw/logs/openclaw.1.log", None),
        ("/etc/passwd", None),                             # outside roots
        ("/Users/alice/.config/something", None),          # wrong shape
        ("relative/.npm/_cacache", None),                  # not absolute
    ],
)
def test_classify_path(path, expected):
    assert disk_reclaim_apply._classify_path(path, ("/Users",)) == expected


# ── The privileged O_NOFOLLOW resolver ────────────────────────────────────────


def test_open_verified_parent_happy_path(tmp_path):
    home = tmp_path / "Users" / "alice"
    (home / ".npm" / "_cacache").mkdir(parents=True)
    fd, leaf = disk_reclaim_apply._open_verified_parent(str(home), [".npm", "_cacache"])
    try:
        assert fd is not None
        assert leaf == "_cacache"
        # fd is the verified `.npm` dir.
        assert os.fstat(fd).st_ino == os.stat(home / ".npm").st_ino
    finally:
        if fd is not None:
            os.close(fd)


def test_open_verified_parent_rejects_intermediate_symlink(tmp_path):
    """`.npm` is a symlink AT WALK TIME → O_NOFOLLOW open fails (ELOOP).

    This is the INTERMEDIATE-component case the audit flagged: an absolute-path
    `sudo rm` would follow `.npm` and escape; the openat/O_NOFOLLOW walk
    refuses to traverse it, so no fd is ever handed to the delete.
    """
    home = tmp_path / "Users" / "alice"
    home.mkdir(parents=True)
    evil = tmp_path / "evil"
    (evil / "_cacache").mkdir(parents=True)
    os.symlink(evil, home / ".npm")
    fd, reason = disk_reclaim_apply._open_verified_parent(str(home), [".npm", "_cacache"])
    assert fd is None
    assert "reject" in reason.lower()


def test_open_verified_parent_rejects_symlinked_home(tmp_path):
    """A symlinked HOME is rejected too (O_NOFOLLOW on the final component)."""
    users = tmp_path / "Users"
    users.mkdir(parents=True)
    real = tmp_path / "realhome"
    (real / ".npm" / "_cacache").mkdir(parents=True)
    os.symlink(real, users / "alice")
    fd, reason = disk_reclaim_apply._open_verified_parent(
        str(users / "alice"), [".npm", "_cacache"])
    assert fd is None


def test_open_verified_parent_rejects_symlinked_leaf(tmp_path):
    """A symlinked LEAF is rejected by the type check (belt-and-suspenders).

    Even if it slipped through, `rm` would only unlink the final-operand
    symlink (no follow) — but the resolver refuses it up front.
    """
    home = tmp_path / "Users" / "alice"
    (home / ".npm").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, home / ".npm" / "_cacache")
    fd, reason = disk_reclaim_apply._open_verified_parent(str(home), [".npm", "_cacache"])
    assert fd is None
    assert "not a directory" in reason.lower()


def test_intermediate_symlink_swap_after_walk_does_not_escape(tmp_path):
    """THE PROOF: with `.npm` swapped to a symlink AFTER the verified walk,
    the fchdir-pinned delete still removes the REAL _cacache and touches
    NOTHING the symlink points at.

    Mirrors production exactly: `_open_verified_parent` holds the verified
    `.npm` dirfd; the delete fchdirs to THAT fd and `rm`s the bare leaf. A
    mid-window rename of `.npm` to a symlink cannot redirect a held fd, and a
    bare leaf relative to the pinned inode never re-resolves an attacker path.
    """
    home = tmp_path / "Users" / "alice"
    cacache = home / ".npm" / "_cacache"
    cacache.mkdir(parents=True)
    (cacache / "blob").write_text("real-cache")
    evil = tmp_path / "evil"
    (evil / "_cacache").mkdir(parents=True)
    (evil / "_cacache" / "secret").write_text("DO NOT TOUCH")

    fd, leaf = disk_reclaim_apply._open_verified_parent(str(home), [".npm", "_cacache"])
    assert fd is not None and leaf == "_cacache"

    # Bot races the reaper: rename the real .npm away, drop a symlink to evil.
    os.rename(home / ".npm", home / ".npm.real")
    os.symlink(evil, home / ".npm")

    # Production's delete: fchdir to the HELD fd, rm the bare leaf.
    cwd_fd = os.open(".", os.O_RDONLY)
    try:
        os.fchdir(fd)
        shutil.rmtree(leaf, ignore_errors=True)
    finally:
        os.fchdir(cwd_fd)
        os.close(cwd_fd)
        os.close(fd)

    # The REAL cache (held inode, now named .npm.real) was deleted...
    assert not (home / ".npm.real" / "_cacache").exists()
    # ...and the symlink's target — another location entirely — is intact.
    assert (evil / "_cacache" / "secret").read_text() == "DO NOT TOUCH"


# ── reclaim() — acts only on freshly-scanned npm caches ────────────────────────


def test_reclaim_rm_npm_only_never_logs(tmp_path):
    users = _fixture_tree(tmp_path)
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)

    # Both npm leaves rm'd by BARE NAME, never .npm itself.
    assert sorted(runner.rm_leaves) == ["_cacache", "_npx"]

    # npm dirs are gone.
    assert not (users / "alice" / ".npm" / "_cacache").exists()
    assert not (users / "alice" / ".npm" / "_npx").exists()

    # LOGS ARE UNTOUCHED — full size, not truncated (the reaper never acts on
    # them; only the scanner reports their size).
    evolve_log = users / "alice" / ".evolve" / "logs" / "evolve.log"
    oc_log = users / "alice" / ".openclaw" / "logs" / "openclaw.1.log"
    assert evolve_log.stat().st_size == SMALL_CAP + KB
    assert oc_log.stat().st_size == SMALL_CAP + KB

    # No log category appears in the acted-on per_category records.
    acted = {c["category"] for c in result["per_category"] if c["acted"]}
    assert acted == {disk_reclaim.CAT_NPM_CACHE}
    assert result["ok"] is True
    assert result["freed_bytes"] > 0
    assert not result["errors"]


def test_reclaim_all_does_not_emit_log_skip_noise(tmp_path):
    """A default 'reclaim all' run reaps npm and stays QUIET about logs —
    no skip records for the read-only-reported log categories."""
    users = _fixture_tree(tmp_path)
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=RecordingRunner())
    cats = {c["category"] for c in result["per_category"]}
    assert cats == {disk_reclaim.CAT_NPM_CACHE}
    assert not any(c.get("skipped") for c in result["per_category"])


def test_reclaim_explicit_log_category_is_skipped_not_error(tmp_path):
    """Asking for a log category is a SKIP with a reason, never an error."""
    users = _fixture_tree(tmp_path)
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(
        categories=[disk_reclaim.CAT_EVOLVE_LOGS], roots=(str(users),), runner=runner)
    assert runner.rm_leaves == []          # nothing reaped
    assert result["ok"] is True            # skip ≠ error
    assert result["freed_bytes"] == 0
    assert not result["errors"]
    rec = [c for c in result["per_category"] if c["category"] == disk_reclaim.CAT_EVOLVE_LOGS]
    assert rec and rec[0]["skipped"] is True
    assert "bounded at source" in rec[0]["reason"]
    # And the log on disk is untouched.
    assert (users / "alice" / ".evolve" / "logs" / "evolve.log").stat().st_size == SMALL_CAP + KB


def test_reclaim_skips_empty_paths(tmp_path):
    users = tmp_path / "Users"
    home = users / "bob"
    (home / ".npm" / "_cacache").mkdir(parents=True)   # empty → size 0 → not scanned
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)
    assert runner.rm_leaves == []
    assert result["freed_bytes"] == 0
    assert result["ok"] is True


def test_reclaim_is_idempotent(tmp_path):
    users = _fixture_tree(tmp_path)
    first = disk_reclaim_apply.reclaim(roots=(str(users),), runner=RecordingRunner())
    assert first["freed_bytes"] > 0
    # Second run: nothing left to reclaim.
    runner2 = RecordingRunner()
    second = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner2)
    assert second["freed_bytes"] == 0
    assert runner2.rm_leaves == []


def test_reclaim_category_filter_npm(tmp_path):
    users = _fixture_tree(tmp_path)
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(
        categories=[disk_reclaim.CAT_NPM_CACHE], roots=(str(users),), runner=runner,
    )
    assert len(runner.rm_leaves) == 2
    cats_acted = {c["category"] for c in result["per_category"] if c["acted"]}
    assert cats_acted == {disk_reclaim.CAT_NPM_CACHE}


# ── reclaim() — escape attempts rejected end-to-end ───────────────────────────


def test_reclaim_rejects_symlinked_npm(tmp_path):
    """A symlinked `.npm` (pointing outside the home) is sized by the scanner
    but the privileged resolver refuses to open it — the escape is closed and
    the runner never issues a delete."""
    users = tmp_path / "Users"
    home = users / "alice"
    home.mkdir(parents=True)
    outside = tmp_path / "outside"
    _sparse(outside / "_cacache" / "blob", 5 * KB)
    os.symlink(outside, home / ".npm")

    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)

    # The reaper NEVER issued a delete for the symlink-reached path.
    assert runner.rm_leaves == []
    # Outside content is untouched.
    assert (outside / "_cacache" / "blob").exists()
    # The rejection is recorded (tri-state), not a silent success.
    assert result["errors"]
    assert any("resolver rejected" in e["error"] for e in result["errors"])
    npm = next(c for c in result["per_category"] if c["category"] == disk_reclaim.CAT_NPM_CACHE)
    assert npm["partial"] is True
    assert npm["acted"] == 0


def test_reclaim_rejects_scanner_path_outside_shape(tmp_path, monkeypatch):
    """Even if the scanner emitted a bogus path in the npm category, the
    string-shape gate rejects it before any fd is opened."""
    users = _fixture_tree(tmp_path)
    real = disk_reclaim.scan_reclaimable

    def _poison(roots=disk_reclaim.DEFAULT_ROOTS):
        out = real(roots)
        for c in out["categories"]:
            if c["category"] == disk_reclaim.CAT_NPM_CACHE:
                c["entries"].append({"path": "/etc/shadow", "bytes": 999})
        return out

    monkeypatch.setattr(disk_reclaim, "scan_reclaimable", _poison)
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)
    # The two legit leaves acted; the poisoned path rejected by the gate.
    assert sorted(runner.rm_leaves) == ["_cacache", "_npx"]
    assert any(e.get("path") == "/etc/shadow" and "safety gate" in e["error"]
               for e in result["errors"])


# ── reclaim() — partial / failure safety ──────────────────────────────────────


def test_reclaim_partial_scan_flag_propagates(tmp_path, monkeypatch):
    users = _fixture_tree(tmp_path)
    real = disk_reclaim.scan_reclaimable

    def _scan_with_partial(roots=disk_reclaim.DEFAULT_ROOTS):
        out = real(roots)
        for c in out["categories"]:
            if c["category"] == disk_reclaim.CAT_NPM_CACHE:
                c["partial"] = True
        return out

    monkeypatch.setattr(disk_reclaim, "scan_reclaimable", _scan_with_partial)
    runner = RecordingRunner()
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)
    npm = next(c for c in result["per_category"] if c["category"] == disk_reclaim.CAT_NPM_CACHE)
    # Acted on what it found, but flagged the incomplete picture.
    assert npm["acted"] == 2
    assert npm["partial"] is True


def test_reclaim_per_path_failure_recorded(tmp_path):
    users = _fixture_tree(tmp_path)
    runner = RecordingRunner(fail={"_cacache"})
    result = disk_reclaim_apply.reclaim(roots=(str(users),), runner=runner)

    assert result["ok"] is False
    # sudo-denied failures carry the recovery hint.
    assert any("refresh-sudoers" in e["error"] for e in result["errors"])
    npm = next(c for c in result["per_category"] if c["category"] == disk_reclaim.CAT_NPM_CACHE)
    assert npm["partial"] is True
    # The other npm leaf still succeeded — failure is per-path, not all-or-nothing.
    assert npm["acted"] == 1


# ── handle_reclaim_request (the endpoint logic) ───────────────────────────────


def test_handle_reclaim_request_wraps_result(monkeypatch):
    canned = {
        "ok": True, "freed_bytes": 12345,
        "per_category": [{"category": "npm_cache", "label": "x", "method": "rm",
                          "freed_bytes": 12345, "acted": 1, "partial": False}],
        "errors": [],
    }
    seen = {}

    def fake_reclaim(categories=None, **kw):
        seen["categories"] = categories
        return canned

    monkeypatch.setattr(disk_reclaim_apply, "reclaim", fake_reclaim)
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: {"percent": 71.0})
    reset = {"called": False}
    monkeypatch.setattr(disk_reclaim, "reset_scan_cache",
                        lambda: reset.__setitem__("called", True))

    body, status = disk_reclaim_apply.handle_reclaim_request({})
    assert status == 200
    assert body["freed_bytes"] == 12345
    assert body["freed_human"]  # humanized
    assert body["disk"] == {"percent": 71.0}
    assert reset["called"] is True
    # No categories filter when absent.
    assert seen["categories"] is None


def test_handle_reclaim_request_filters_unknown_categories(monkeypatch):
    seen = {}
    monkeypatch.setattr(disk_reclaim_apply, "reclaim",
                        lambda categories=None, **kw: seen.update(categories=categories)
                        or {"ok": True, "freed_bytes": 0, "per_category": [], "errors": []})
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: None)
    disk_reclaim_apply.handle_reclaim_request(
        {"categories": ["npm_cache", "evil", "../etc"]})
    # Only the known category id survives; the bogus strings are dropped.
    assert seen["categories"] == ["npm_cache"]


def test_handle_reclaim_request_log_category_survives_filter(monkeypatch):
    """A log category id is 'known' so it reaches reclaim() (which skips it
    with a reason) — it must NOT be filtered out as if unknown."""
    seen = {}
    monkeypatch.setattr(disk_reclaim_apply, "reclaim",
                        lambda categories=None, **kw: seen.update(categories=categories)
                        or {"ok": True, "freed_bytes": 0, "per_category": [], "errors": []})
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: None)
    disk_reclaim_apply.handle_reclaim_request(
        {"categories": [disk_reclaim.CAT_EVOLVE_LOGS, disk_reclaim.CAT_NPM_CACHE]})
    assert seen["categories"] == [disk_reclaim.CAT_EVOLVE_LOGS, disk_reclaim.CAT_NPM_CACHE]


def test_handle_reclaim_request_skipped_log_is_200(tmp_path, monkeypatch):
    """End-to-end: a request that names only a log category returns 200 with a
    skipped record (not 207), via the injected runner — no real sudo."""
    users = _fixture_tree(tmp_path)
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: None)
    runner = RecordingRunner()
    # roots default to DEFAULT_ROOTS inside reclaim(); patch reclaim to scope it.
    real_reclaim = disk_reclaim_apply.reclaim
    monkeypatch.setattr(
        disk_reclaim_apply, "reclaim",
        lambda categories=None, **kw: real_reclaim(
            categories=categories, roots=(str(users),), runner=runner))
    body, status = disk_reclaim_apply.handle_reclaim_request(
        {"categories": [disk_reclaim.CAT_EVOLVE_LOGS]})
    assert status == 200
    assert runner.rm_leaves == []
    skipped = [c for c in body["per_category"] if c.get("skipped")]
    assert skipped and skipped[0]["category"] == disk_reclaim.CAT_EVOLVE_LOGS


def test_handle_reclaim_request_partial_is_207(monkeypatch):
    monkeypatch.setattr(disk_reclaim_apply, "reclaim",
                        lambda categories=None, **kw: {
                            "ok": False, "freed_bytes": 1,
                            "per_category": [], "errors": [{"path": "/x", "category": "npm_cache", "error": "boom"}]})
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: None)
    body, status = disk_reclaim_apply.handle_reclaim_request({})
    assert status == 207
    assert body["ok"] is False


# ── Endpoint wiring (POST /api/host-health/reclaim) ───────────────────────────


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "signals").mkdir(parents=True)
    network = {"members": [], "bots": {}, "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def test_reclaim_endpoint_returns_freed_bytes(app, monkeypatch):
    monkeypatch.setattr(
        disk_reclaim_apply, "reclaim",
        lambda categories=None, **kw: {
            "ok": True, "freed_bytes": 999, "per_category": [], "errors": []},
    )
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: {"percent": 70.0})
    with app.test_client() as c:
        resp = c.post("/api/host-health/reclaim", json={"categories": ["npm_cache"]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
    assert data["freed_bytes"] == 999
    assert data["disk"] == {"percent": 70.0}
    assert "freed_human" in data


def test_reclaim_endpoint_partial_status(app, monkeypatch):
    monkeypatch.setattr(
        disk_reclaim_apply, "reclaim",
        lambda categories=None, **kw: {
            "ok": False, "freed_bytes": 0, "per_category": [],
            "errors": [{"path": "/x", "category": "npm_cache", "error": "sudo denied"}]},
    )
    monkeypatch.setattr(disk_reclaim_apply, "_fresh_disk", lambda: None)
    with app.test_client() as c:
        resp = c.post("/api/host-health/reclaim", json={})
        assert resp.status_code == 207
        data = resp.get_json()
    assert data["ok"] is False
    assert data["errors"]
