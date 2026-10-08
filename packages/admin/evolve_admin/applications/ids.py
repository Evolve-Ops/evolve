"""
ids.py — Stable ID generation and version helpers for the App Gallery & Forge system.

All IDs use a type-prefix + 8-character lowercase hex format:
    p-a3f91c8b   pkg_id     — stable identity of a gallery app package
    f-d4e8f901   file_id    — stable identity of a specific artifact file
    j-7bc21e44   job_id     — a forge job tracked by the continuity engine
    r-00000003   run_id     — sequential forge run within a job
    c-91d02ab7   chain_id   — a dependency-ordered install-job chain

Package versions use CalVer date + semantic major.minor:
    2026.04.12-1.3

File versions use CalVer date + daily sequence:
    2026.04.12.2

identity: see resolve_app_id — this module MINTS ids; it never resolves one.
The single ``pkg_id`` mention above names the ``p-`` prefix's meaning in the
id-format table. ``app_identity.resolve_app_id`` is the read side and imports
nothing from here.
"""

from __future__ import annotations

import re
import secrets
from datetime import date

from evolve_util import now_iso  # re-export; many modules import it from here

__all__ = [
    "new_pkg_id",
    "new_file_id",
    "new_job_id",
    "new_chain_id",
    "run_id_for",
    "is_valid_id",
    "id_prefix",
    "calver_today",
    "now_iso",
    "parse_pkg_version",
    "next_pkg_version",
    "compare_pkg_versions",
    "is_major_bump",
    "parse_file_version",
    "next_file_version",
]


# ── ID Generation ─────────────────────────────────────────────────────────────

def new_pkg_id() -> str:
    """Generate a new stable package ID.  e.g. 'p-a3f91c8b'"""
    return "p-" + secrets.token_hex(4)


def new_file_id() -> str:
    """Generate a new file artifact ID.  e.g. 'f-d4e8f901'"""
    return "f-" + secrets.token_hex(4)


def new_job_id() -> str:
    """Generate a new forge job ID.  e.g. 'j-7bc21e44'"""
    return "j-" + secrets.token_hex(4)


def new_chain_id() -> str:
    """Generate a new install-chain ID.  e.g. 'c-91d02ab7'"""
    return "c-" + secrets.token_hex(4)


def run_id_for(n: int) -> str:
    """Return the run ID for run number n (1-based).  e.g. run_id_for(3) → 'r-00000003'"""
    return f"r-{n:08d}"


# ── ID Validation ─────────────────────────────────────────────────────────────

_ID_PATTERN = re.compile(r'^[pfjrc]-[0-9a-f]{8}$')
_RUN_ID_PATTERN = re.compile(r'^r-\d{8}$')


def is_valid_id(value: str) -> bool:
    """Return True if value looks like a well-formed typed ID."""
    return bool(_ID_PATTERN.match(value)) or bool(_RUN_ID_PATTERN.match(value))


def id_prefix(value: str) -> str | None:
    """Return the prefix character of a typed ID, or None if not a valid ID."""
    if is_valid_id(value) and len(value) > 2:
        return value[0]
    return None


# ── CalVer helpers ─────────────────────────────────────────────────────────────

def calver_today() -> str:
    """Return today's date in CalVer format.  e.g. '2026.04.12'"""
    d = date.today()
    return f"{d.year}.{d.month:02d}.{d.day:02d}"


# now_iso is re-exported from evolve_util above (same "Z, seconds" format).


# ── Package Version (CalVer + major.minor) ────────────────────────────────────
#
# Format:  YYYY.MM.DD-MAJOR.MINOR
# Rules:
#   - Major bump: cross-app impact (exported_hooks changed, shared file schema changed)
#   - Minor bump: internal improvement (new file, bug fix, refinement)
#   - Date is always today (the date the forge run completed)
#   - No patch number — forge runs are frequent; patch granularity creates noise

_PKG_VERSION_PATTERN = re.compile(r'^(\d{4}\.\d{2}\.\d{2})-(\d+)\.(\d+)$')


def parse_pkg_version(version: str) -> tuple[str, int, int] | None:
    """
    Parse a package version string.
    Returns (date_str, major, minor) or None if unparseable.
    """
    m = _PKG_VERSION_PATTERN.match(version or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def next_pkg_version(current: str | None, major_bump: bool = False) -> str:
    """
    Compute the next package version.

    Args:
        current:    Current version string, or None for a fresh install.
        major_bump: True if the change has cross-app impact (major version bump).

    Returns:
        New version string like '2026.04.15-1.3'.

    On a fresh install (current is None), returns '<today>-1.0'.
    A new day always resets the minor counter if a bump type changes the date portion,
    but keeps the major/minor semantics intact.
    """
    today = calver_today()

    if not current:
        return f"{today}-1.0"

    parsed = parse_pkg_version(current)
    if not parsed:
        # Unparseable current version — start fresh
        return f"{today}-1.0"

    _date, major, minor = parsed

    if major_bump:
        return f"{today}-{major + 1}.0"
    else:
        return f"{today}-{major}.{minor + 1}"


def compare_pkg_versions(a: str, b: str) -> int:
    """
    Compare two package version strings.
    Returns -1, 0, or 1 (a < b, a == b, a > b).
    Unparseable versions sort as older than any valid version.
    """
    pa = parse_pkg_version(a)
    pb = parse_pkg_version(b)

    if pa is None and pb is None:
        return 0
    if pa is None:
        return -1
    if pb is None:
        return 1

    date_a, maj_a, min_a = pa
    date_b, maj_b, min_b = pb

    # Compare date first, then major, then minor
    for va, vb in [(date_a, date_b), (maj_a, maj_b), (min_a, min_b)]:
        if va < vb:
            return -1
        if va > vb:
            return 1
    return 0


def is_major_bump(old_version: str | None, new_version: str) -> bool:
    """
    Return True if new_version represents a major bump relative to old_version.
    A major bump means the major component increased.
    """
    if not old_version:
        return False
    old = parse_pkg_version(old_version)
    new = parse_pkg_version(new_version)
    if not old or not new:
        return False
    return new[1] > old[1]


# ── File Version (CalVer + daily sequence) ─────────────────────────────────────
#
# Format:  YYYY.MM.DD.N   (N = Nth modification today, 1-based)
# No semantic meaning — purely a change record.

_FILE_VERSION_PATTERN = re.compile(r'^(\d{4}\.\d{2}\.\d{2})\.(\d+)$')


def parse_file_version(version: str) -> tuple[str, int] | None:
    """
    Parse a file version string.
    Returns (date_str, sequence) or None if unparseable.
    """
    m = _FILE_VERSION_PATTERN.match(version or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def next_file_version(current: str | None) -> str:
    """
    Compute the next file version.

    Args:
        current: Current file version string, or None for a new file.

    Returns:
        New version string like '2026.04.15.2'.

    On a new day the sequence resets to 1.
    """
    today = calver_today()

    if not current:
        return f"{today}.1"

    parsed = parse_file_version(current)
    if not parsed:
        return f"{today}.1"

    date_str, seq = parsed
    if date_str == today:
        return f"{today}.{seq + 1}"
    else:
        # New day — reset sequence
        return f"{today}.1"
