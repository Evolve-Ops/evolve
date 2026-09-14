"""Tests for the handover-token cleanup cron entry point.

Verifies:
  • Expired tokens are pruned.
  • Unexpired (claimed or unclaimed) tokens are preserved.
  • Corrupt files are dropped.
  • The module entry point ``python3 -m evolve_admin.handover_cleanup`` runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (str(_ADMIN), str(_ANALYZER)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_token(shared: Path, token: str, *, expires: datetime, claimed: bool = False) -> None:
    d = shared / "handover-tokens"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{token}.json").write_text(json.dumps({
        "token": token,
        "bot_id": "diana_personal",
        "audience": "personal_bot_user",
        "message": "",
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(days=10)),
        "expires_at": _iso(expires),
        "claimed_at": _iso(datetime.now(timezone.utc)) if claimed else None,
        "preferences": None,
    }))


def test_cleanup_prunes_expired_keeps_active(tmp_path):
    from evolve_admin.handover import cleanup_expired
    shared = tmp_path / "shared"
    now = datetime.now(timezone.utc)

    _write_token(shared, "a" * 32, expires=now - timedelta(days=1))      # expired
    _write_token(shared, "b" * 32, expires=now + timedelta(days=1))      # active
    _write_token(shared, "c" * 32, expires=now + timedelta(hours=1),     # active+claimed
                 claimed=True)
    _write_token(shared, "d" * 32, expires=now - timedelta(seconds=1))   # just expired

    result = cleanup_expired(shared)
    assert result["removed"] == 2
    assert result["kept"] == 2

    remaining = {p.stem for p in (shared / "handover-tokens").glob("*.json")}
    assert remaining == {"b" * 32, "c" * 32}


def test_cleanup_drops_corrupt_files(tmp_path):
    from evolve_admin.handover import cleanup_expired
    shared = tmp_path / "shared"
    d = shared / "handover-tokens"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.json").write_text("{this isn't json")
    result = cleanup_expired(shared)
    assert result["removed"] == 1
    assert not (d / "bad.json").exists()


def test_cleanup_idempotent_on_empty_dir(tmp_path):
    from evolve_admin.handover import cleanup_expired
    shared = tmp_path / "shared"
    result = cleanup_expired(shared)
    assert result == {"removed": 0, "kept": 0}
    # Calling again is fine
    result2 = cleanup_expired(shared)
    assert result2 == {"removed": 0, "kept": 0}


def test_cleanup_module_entry_point(tmp_path):
    """`python3 -m evolve_admin.handover_cleanup --shared-dir <path>`
    runs end-to-end and prints a summary line."""
    shared = tmp_path / "shared"
    now = datetime.now(timezone.utc)
    _write_token(shared, "a" * 32, expires=now - timedelta(days=1))
    _write_token(shared, "b" * 32, expires=now + timedelta(days=1))

    r = subprocess.run(
        [
            sys.executable, "-m", "evolve_admin.handover_cleanup",
            "--shared-dir", str(shared),
        ],
        capture_output=True, text=True,
        cwd=str(_ADMIN),
        # Both THIS worktree's packages on the path — handover.py imports
        # evolve_util (analyzer package), and the dev machine's editable
        # install may point at a different checkout.
        env={
            **__import__("os").environ,
            "PYTHONPATH": __import__("os").pathsep.join(
                [str(_ADMIN), str(_ADMIN.parent / "analyzer")]
            ),
        },
    )
    assert r.returncode == 0, r.stderr
    assert "removed=1" in r.stdout
    assert "kept=1" in r.stdout
