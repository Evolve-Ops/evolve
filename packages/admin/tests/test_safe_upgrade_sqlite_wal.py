"""The OpenClaw state-DB reader must see rows OpenClaw only just wrote.

OC 2026.7 keeps ``installed_plugin_index`` in a WAL-mode SQLite DB, so a row
it writes lives in the ``-wal`` sidecar until a checkpoint folds it into the
main file. ``safe_upgrade._read_installs_from_sqlite`` used to open with
``immutable=1``, which by design reads the main file ALONE — so it returned
the last CHECKPOINTED snapshot and could not tell that from a current one.

Measured on the pod 2026-08-18, same DB, same query, minutes after an
``openclaw plugins install``::

    immutable=1 : codex.spec == "@openclaw/codex"             (29 min stale)
    mode=ro     : codex.spec == "@openclaw/codex@2026.7.1-1"  (current)

That stale read put deploy's unpinned-spec reconciler in a loop — read
"unpinned", run a real npm install to fix it, OC writes the pinned row into
the WAL, next read still says "unpinned" — on every deploy, forever. The
same reader also backs three upgrade-gate helpers, where a stale-but-parseable
answer silently defeats a fail-safe built for exactly the "OpenClaw moved its
on-disk layout" case.

These tests use a REAL WAL-mode database with a genuinely uncheckpointed row
(the writer connection stays open, which is what keeps the WAL from being
folded in) — a mocked sqlite3 would have happily passed the old code.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from evolve_admin import safe_upgrade as su

_SCHEMA = """
CREATE TABLE installed_plugin_index (
  id TEXT PRIMARY KEY,
  host_contract_version TEXT,
  compat_registry_version TEXT,
  generated_at_ms INTEGER,
  install_records_json TEXT
)
"""


def _records(spec: str) -> str:
    return json.dumps({
        "codex": {
            "source": "npm",
            "spec": spec,
            "resolvedName": "@openclaw/codex",
            "resolvedVersion": "2026.7.1-1",
            "installPath": "/tmp/np/node_modules/@openclaw/codex",
        },
    })


@pytest.fixture
def wal_db(tmp_path: Path):
    """A WAL-mode DB whose newest row is still in the -wal sidecar.

    Yields ``(path, writer_conn)``. The writer stays OPEN for the duration —
    closing the last connection checkpoints and removes the WAL, which would
    quietly erase the very condition under test.
    """
    path = tmp_path / "openclaw.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")   # never fold in behind our back
    conn.execute(_SCHEMA)
    conn.execute(
        "INSERT INTO installed_plugin_index VALUES (?,?,?,?,?)",
        ("idx", "1", "1", 1_000, _records("@openclaw/codex")),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # row 1 is now in the main file

    # Row 2 — committed, deliberately NOT checkpointed: this is what OC looks
    # like the moment after `plugins install`.
    conn.execute(
        "INSERT INTO installed_plugin_index VALUES (?,?,?,?,?)",
        ("idx-new", "1", "1", 2_000, _records("@openclaw/codex@2026.7.1-1")),
    )
    conn.commit()
    assert (tmp_path / "openclaw.sqlite-wal").exists(), "fixture did not produce a WAL"
    try:
        yield path, conn
    finally:
        conn.close()


def _spec(payload: dict) -> str:
    return payload["installRecords"]["codex"]["spec"]


def test_reads_the_uncheckpointed_row(wal_db):
    """The regression: the pinned spec OC just wrote must be visible."""
    path, _conn = wal_db
    out = su._read_installs_from_sqlite(path)
    assert out is not None
    assert _spec(out) == "@openclaw/codex@2026.7.1-1", (
        "reader saw the checkpointed snapshot, not what OpenClaw actually wrote"
    )
    assert out["_wal_visible"] is True


def test_immutable_read_is_the_stale_one(wal_db):
    """Pins the mechanism itself, so a future 'simplification' back to
    immutable=1 fails loudly instead of silently going stale again."""
    path, _conn = wal_db
    stale = su._query_install_index(f"file:{path}?mode=ro&immutable=1")
    fresh = su._query_install_index(f"file:{path}?mode=ro")
    assert json.loads(stale[0])["codex"]["spec"] == "@openclaw/codex"
    assert json.loads(fresh[0])["codex"]["spec"] == "@openclaw/codex@2026.7.1-1"


def test_falls_back_to_immutable_when_the_wal_read_fails(wal_db, monkeypatch):
    """immutable=1 stays the fallback: it is the only mode that works when a
    plain mode=ro connection cannot establish its read (see the natural
    reproduction below). A degraded read is better than none — but it must
    SAY it is degraded.

    This one INJECTS the failure so it runs everywhere, including as root;
    ``test_natural_readonly_wal_failure_*`` reproduces the same condition for
    real."""
    path, _conn = wal_db
    real = su._query_install_index

    def _wal_denied(uri: str):
        if "immutable=1" not in uri:
            return None          # simulate SQLITE_CANTOPEN on the -shm
        return real(uri)

    monkeypatch.setattr(su, "_query_install_index", _wal_denied)
    out = su._read_installs_from_sqlite(path)
    assert out is not None
    assert _spec(out) == "@openclaw/codex"       # the stale snapshot
    assert out["_wal_visible"] is False, (
        "a consumer making a write/safety decision must be able to tell"
    )


# ── the natural reproduction ─────────────────────────────────────────────────
#
# The fallback is not hypothetical. On the pod (2026-08-18) a real OpenClaw
# state DB — the evolve service user's leftover profile — failed a mode=ro read
# on its FIRST statement with `unable to open database file`, no live process
# holding it, while immutable=1 read all 74 tables fine.
#
# The condition below is that shape, built from scratch: a WAL database whose
# committed frames are still in the -wal, whose transient -shm is gone (a
# reboot or tmp sweep removes it; the -wal survives), and whose directory the
# reader cannot write. A read-only connection must rebuild the -shm to see the
# WAL, cannot, and refuses the read entirely rather than quietly serving the
# older main-file contents. immutable=1 does serve them — which is exactly why
# the fallback must be labelled: it hands back a view that is provably missing
# a committed write.

def _crashed_wal_db(dir_path: Path) -> Path:
    """A WAL DB with an uncheckpointed row and no -shm, written by a process
    that exited without closing (a clean close would checkpoint and erase the
    condition)."""
    path = dir_path / "openclaw.sqlite"
    child = f"""
import sqlite3, os
c = sqlite3.connect({str(path)!r})
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute({_SCHEMA!r})
c.execute("INSERT INTO installed_plugin_index VALUES (?,?,?,?,?)",
          ("idx", "1", "1", 1000, {_records("@openclaw/codex")!r}))
c.commit()
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("INSERT INTO installed_plugin_index VALUES (?,?,?,?,?)",
          ("idx-new", "1", "1", 2000, {_records("@openclaw/codex@2026.7.1-1")!r}))
c.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", child], check=True)
    shm = path.with_name(path.name + "-shm")
    if shm.exists():
        shm.unlink()          # transient file; a reboot clears it, the -wal stays
    assert path.with_name(path.name + "-wal").exists(), "fixture lost its WAL"
    return path


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root can create the -shm regardless of directory mode, so the "
           "read-only failure this reproduces cannot occur as root",
)
def test_natural_readonly_wal_failure_degrades_and_says_so(tmp_path):
    path = _crashed_wal_db(tmp_path)
    os.chmod(tmp_path, 0o500)          # reader cannot create the -shm
    try:
        # Precondition: the failure is REAL, not injected.
        assert su._query_install_index(f"file:{path}?mode=ro") is None
        out = su._read_installs_from_sqlite(path)
        assert out is not None, "the fallback must still produce a usable read"
        assert out["_wal_visible"] is False
        # …and it is provably stale: the pinned spec is committed in the WAL,
        # and the answer we just handed back does not have it.
        assert _spec(out) == "@openclaw/codex"
    finally:
        os.chmod(tmp_path, 0o700)      # let pytest clean up


@pytest.mark.skipif(os.geteuid() == 0, reason="see above")
def test_natural_readonly_wal_failure_blocks_a_write_decision(tmp_path, monkeypatch):
    """The consequence that matters: deploy's re-pin reconciler gets None and
    skips, instead of 'fixing' a spec that is already pinned in the WAL."""
    path = _crashed_wal_db(tmp_path)
    os.chmod(tmp_path, 0o500)
    try:
        monkeypatch.setattr(su, "_installs_sqlite_path", lambda user: path)
        assert su.read_installs_for_write_decision("some_bot") is None
        # The read-only consumer still gets the degraded view.
        assert su._read_installs_json("some_bot")["_wal_visible"] is False
    finally:
        os.chmod(tmp_path, 0o700)


def test_returns_none_when_neither_mode_can_read(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "_query_install_index", lambda uri: None)
    assert su._read_installs_from_sqlite(tmp_path / "absent.sqlite") is None


def test_missing_db_is_none(tmp_path):
    assert su._read_installs_from_sqlite(tmp_path / "nope.sqlite") is None


def test_malformed_records_json_is_none(tmp_path):
    path = tmp_path / "openclaw.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    conn.execute("INSERT INTO installed_plugin_index VALUES (?,?,?,?,?)",
                 ("idx", "1", "1", 1_000, "{ truncated"))
    conn.commit()
    conn.close()
    assert su._read_installs_from_sqlite(path) is None


def test_read_installs_json_prefers_sqlite_and_carries_the_stamp(wal_db, monkeypatch):
    """The public reader keeps its shape (callers read installRecords) and
    now carries _wal_visible through."""
    path, _conn = wal_db
    monkeypatch.setattr(su, "_installs_sqlite_path", lambda user: path)
    out = su._read_installs_json("some_bot")
    assert out is not None
    assert _spec(out) == "@openclaw/codex@2026.7.1-1"
    assert out["_source"] == "state/openclaw.sqlite"
    assert out["_wal_visible"] is True


# ── the write-decision reader ────────────────────────────────────────────────

def test_write_decision_reader_returns_none_on_a_degraded_read(wal_db, monkeypatch):
    """deploy's re-pin reconciler must not act on a snapshot that may predate
    what OpenClaw just wrote — that is the loop this whole change closes."""
    path, _conn = wal_db
    monkeypatch.setattr(su, "_installs_sqlite_path", lambda user: path)
    monkeypatch.setattr(
        su, "_query_install_index",
        lambda uri: None if "immutable=1" not in uri else sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True).execute(
                su._INSTALL_INDEX_QUERY).fetchone(),
    )
    assert su.read_installs_for_write_decision("some_bot") is None
    # …while the read-only reader still hands back the degraded view, because
    # a stale answer beats no answer when nothing is being written.
    degraded = su._read_installs_json("some_bot")
    assert degraded is not None and degraded["_wal_visible"] is False


def test_write_decision_reader_passes_a_wal_visible_read_through(wal_db, monkeypatch):
    path, _conn = wal_db
    monkeypatch.setattr(su, "_installs_sqlite_path", lambda user: path)
    out = su.read_installs_for_write_decision("some_bot")
    assert out is not None
    assert _spec(out) == "@openclaw/codex@2026.7.1-1"


def test_deploy_repin_uses_the_write_decision_reader():
    """Wiring: the reconciler that re-installs plugins must read through the
    write-decision helper, not the plain reader."""
    import inspect

    from evolve_admin import deploy

    src = inspect.getsource(deploy)
    assert "read_installs_for_write_decision as _read_inst" in src
    assert "_read_installs_json as _read_inst" not in src
