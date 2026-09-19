"""Tests for the Board store's ownership contract (board_store_perms.py).

WHAT THESE PIN — the 2026-09-04 phone test, in three parts:

  * **A root-run mint hands the store to the daemon.** ``sudo evolve-admin
    board token`` runs as root; without the adoption every file it writes is
    root-owned, and a 0600 token hash is then one the ``evolve`` daemon
    cannot open. Minting AS the daemon chowns nothing.
  * **An unreadable hash is a logged coverage gap, not a 401 like any
    other.** One warning per bot per process, and ``token_store_readable``
    tells the route it must not charge the failed-auth limiter — charging it
    is what refused the CORRECT token for the rest of the window.
  * **``ensure_pod_perms`` re-verifies it.** A root-owned board dir is drift
    with a repair attached, not a silence.

No live pod is touched: the fixture is a tmp_path shared dir, ownership is
simulated (``_owner_of``) and every chown is recorded rather than run.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin import board_store_perms as bp  # noqa: E402

BOT = "personal-bot"


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    """The one-warning-per-bot memo is process-global by design."""
    bs._UNREADABLE_WARNED.clear()
    yield
    bs._UNREADABLE_WARNED.clear()


@pytest.fixture()
def chowns(monkeypatch):
    """Record every ``os.chown`` the store issues instead of running one."""
    recorded: list[tuple[str, int, int]] = []

    def _record(path, uid, gid, *, follow_symlinks=True):
        recorded.append((str(path), uid, gid))

    monkeypatch.setattr(bp.os, "chown", _record)
    monkeypatch.setattr(bp, "_daemon_ids", lambda: (4242, 0))
    return recorded


def _as_root(monkeypatch):
    monkeypatch.setattr(bp, "_geteuid", lambda: 0)


def _as_daemon(monkeypatch):
    monkeypatch.setattr(bp, "_geteuid", lambda: 501)


# ── 1. the root writer adopts what it writes ────────────────────────────────


def test_mint_as_root_gives_the_whole_store_to_the_daemon(tmp_path, monkeypatch, chowns):
    _as_root(monkeypatch)
    token = bs.mint_token(tmp_path, BOT)

    owned = {p for p, _, _ in chowns}
    assert str(tmp_path / "boards") in owned
    assert str(tmp_path / "boards" / BOT) in owned
    assert str(tmp_path / "boards" / BOT / "token.sha256") in owned
    assert all((uid, gid) == (4242, 0) for _, uid, gid in chowns)
    # The hash is still the only thing on disk, still 0600.
    hash_file = tmp_path / "boards" / BOT / "token.sha256"
    assert token not in hash_file.read_text()
    assert oct(hash_file.stat().st_mode & 0o777) == "0o600"


def test_save_and_event_as_root_are_adopted_too(tmp_path, monkeypatch, chowns):
    _as_root(monkeypatch)
    board = bs.load_board(tmp_path, BOT)
    bs.add_card(board, title="Book the scan", cluster="health")
    bs.save_board(tmp_path, BOT, board)
    bs.append_event(tmp_path, BOT, {"event": "stocked"})

    owned = {p for p, _, _ in chowns}
    assert str(tmp_path / "boards" / BOT / "board.json") in owned
    assert str(tmp_path / "boards" / BOT / "events") in owned
    assert any(p.endswith(".jsonl") for p in owned)


def test_revoke_as_root_leaves_no_root_owned_store_behind(tmp_path, monkeypatch, chowns):
    _as_root(monkeypatch)
    bs.mint_token(tmp_path, BOT)
    chowns.clear()
    assert bs.revoke_token(tmp_path, BOT) is True
    assert str(tmp_path / "boards" / BOT) in {p for p, _, _ in chowns}


def test_mint_as_the_daemon_chowns_nothing(tmp_path, monkeypatch, chowns):
    _as_daemon(monkeypatch)
    token = bs.mint_token(tmp_path, BOT)
    assert chowns == []
    assert bs.verify_token(tmp_path, BOT, token) is True


def test_adopt_refuses_a_path_behind_a_planted_symlink(tmp_path, monkeypatch, chowns):
    _as_root(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    bp.adopt(link / "file")
    assert chowns == []


def test_adopt_survives_an_unresolvable_daemon_account(tmp_path, monkeypatch, caplog):
    _as_root(monkeypatch)
    monkeypatch.setattr(bp, "_daemon_ids", lambda: None)
    with caplog.at_level(logging.WARNING):
        bs.mint_token(tmp_path, BOT)  # must not raise — the token was printed
    assert any("cannot resolve" in r.message for r in caplog.records)


# ── 2. unreadable ≠ unminted ────────────────────────────────────────────────


@pytest.fixture()
def unreadable(monkeypatch):
    """Make the token hash present-but-unopenable, deterministically.

    A ``chmod 000`` would be a no-op for a suite running as root, and CI's
    uid is not something a test may assume.
    """
    real_read_text = Path.read_text

    def _deny(self, *a, **kw):
        if self.name == "token.sha256":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _deny)


def test_unreadable_hash_is_false_and_warns_exactly_once(
    tmp_path, monkeypatch, caplog, unreadable,
):
    _as_daemon(monkeypatch)
    (tmp_path / "boards" / BOT).mkdir(parents=True)
    (tmp_path / "boards" / BOT / "token.sha256").write_text("deadbeef\n")

    with caplog.at_level(logging.WARNING):
        assert bs.verify_token(tmp_path, BOT, "anything") is False
        assert bs.token_store_readable(tmp_path, BOT) is False
        assert bs.verify_token(tmp_path, BOT, "anything") is False

    warnings = [r for r in caplog.records if "unreadable" in r.message]
    assert len(warnings) == 1
    assert "ensure-pod-perms" in warnings[0].getMessage()
    assert BOT in warnings[0].getMessage()
    # The token itself never reaches a log line.
    assert "anything" not in warnings[0].getMessage()


def test_missing_hash_stays_an_ordinary_client_failure(tmp_path):
    # Unminted / revoked: the client IS wrong, and the limiter should charge.
    assert bs.token_store_readable(tmp_path, BOT) is True
    assert bs.verify_token(tmp_path, BOT, "anything") is False


def test_hostile_bot_id_never_raises_out_of_the_readable_probe(tmp_path):
    assert bs.token_store_readable(tmp_path, "../../etc") is True


# ── 3. the drift check ──────────────────────────────────────────────────────


def _own_everything_as(monkeypatch, owner: str):
    monkeypatch.setattr(bp, "_owner_of", lambda p: owner)


def test_no_board_dir_is_an_informational_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "daemon_user", lambda: "evolve")
    checks = bp.check_board_store(tmp_path, BOT)
    assert len(checks) == 1 and checks[0].ok
    assert "no board minted" in checks[0].detail


def test_daemon_owned_store_passes(tmp_path, monkeypatch, chowns):
    _as_daemon(monkeypatch)
    bs.mint_token(tmp_path, BOT)
    monkeypatch.setattr(bp, "daemon_user", lambda: "evolve")
    _own_everything_as(monkeypatch, "evolve")
    checks = bp.check_board_store(tmp_path, BOT)
    assert [c.ok for c in checks] == [True]


def test_root_owned_store_is_drift_naming_the_token_hash(tmp_path, monkeypatch, chowns):
    _as_daemon(monkeypatch)
    bs.mint_token(tmp_path, BOT)
    monkeypatch.setattr(bp, "daemon_user", lambda: "evolve")
    _own_everything_as(monkeypatch, "root")

    (check,) = bp.check_board_store(tmp_path, BOT)
    assert check.ok is False
    assert check.category == "board-store"
    assert "token.sha256" in check.detail
    assert "not owned by evolve" in check.detail
    assert check.apply is not None


def test_repair_as_root_adopts_every_path_in_the_store(tmp_path, monkeypatch, chowns):
    _as_daemon(monkeypatch)
    bs.mint_token(tmp_path, BOT)
    bs.append_event(tmp_path, BOT, {"event": "stocked"})
    monkeypatch.setattr(bp, "daemon_user", lambda: "evolve")

    owner = {"who": "root"}
    monkeypatch.setattr(bp, "_owner_of", lambda p: owner["who"])
    (check,) = bp.check_board_store(tmp_path, BOT)
    assert check.ok is False

    _as_root(monkeypatch)
    chowns.clear()

    def _record_and_repair(path, uid, gid, *, follow_symlinks=True):
        chowns.append((str(path), uid, gid))
        owner["who"] = "evolve"

    monkeypatch.setattr(bp.os, "chown", _record_and_repair)
    assert check.apply() is True

    owned = {p for p, _, _ in chowns}
    root = tmp_path / "boards" / BOT
    assert str(root) in owned
    assert str(root / "token.sha256") in owned
    assert str(root / "events") in owned
    assert any(p.endswith(".jsonl") for p in owned)


def test_repair_off_root_reports_the_operator_command_instead_of_sudo(
    tmp_path, monkeypatch, caplog, chowns,
):
    _as_daemon(monkeypatch)
    bs.mint_token(tmp_path, BOT)
    monkeypatch.setattr(bp, "daemon_user", lambda: "evolve")
    _own_everything_as(monkeypatch, "root")
    (check,) = bp.check_board_store(tmp_path, BOT)
    with caplog.at_level(logging.WARNING):
        assert check.apply() is False
    assert any("ensure-pod-perms" in r.getMessage() for r in caplog.records)
    assert chowns == []
