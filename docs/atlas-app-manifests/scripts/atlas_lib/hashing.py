"""Atlas — salted member-ID hashing.

The salt is created once at install time, persisted at workspace/atlas/.capture-salt,
and never logged. Hashing is sha256(salt + ":" + member_id), first 16 hex chars.

Without the salt, hashes are not reversible (member IDs are bounded to Telegram IDs,
but the salt prevents trivial reversal even by anyone with the index).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[atlas:hashing] {msg}", file=sys.stderr)


def read_or_create_salt(salt_path: Path) -> str:
    """Return the persistent salt for this Atlas install. Creates it if absent.

    File mode is 0o600 — read/write by owner only.
    """
    if salt_path.exists():
        try:
            return salt_path.read_text().strip()
        except OSError as exc:
            _log(f"could not read salt at {salt_path}: {exc}")
            raise
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_urlsafe(32)
    # Write with restrictive permissions
    fd = os.open(str(salt_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, salt.encode("utf-8"))
    finally:
        os.close(fd)
    return salt


def hash_member(member_id: str, salt: str) -> str:
    """Return first 16 hex chars of sha256(salt + ':' + member_id)."""
    if not salt:
        raise ValueError("salt is required — refusing to hash with empty salt")
    digest = hashlib.sha256(f"{salt}:{member_id}".encode("utf-8")).hexdigest()
    return digest[:16]
