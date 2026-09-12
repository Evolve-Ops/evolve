"""Anti-regression: the per-bot backup-SSH-key writer must stay dead.

Background: the 2026-06-07 incident traced to two writers (deploy.py and
server.py) clobbering each other at ``/Users/<bot>/.ssh/evolve-backup-<bot>``.
The unification spec at
``internal/spec-backup-key-distribution-unification-2026-06-08.md`` removes
the per-bot writer; this test fails CI if a future commit reintroduces
its shape.

Three forbidden patterns, each evaluated per source file:

  1. ``ssh-keygen`` invocation referencing ``evolve-backup-`` — only the
     unified writer at ``evolve_admin/backup_keys.py`` generates the
     canonical shared keypair.
  2. ``tempfile.mkstemp`` with ``evolve-backup-`` prefix — only the
     unified writer stages bot-targeted files through /tmp.
  3. ``sudo /bin/cp`` (or ``["sudo", "/bin/cp"]``) targeting
     ``evolve-backup-`` — only the unified writer copies into
     ``/Users/<bot>/.ssh/evolve-backup-<bot>{,.pub}``.

Allowlisted files: the unified writer itself, the analyzer's read-side
helper (``ssh_key_path``), this test, the spec doc.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_PACKAGES = _ROOT / "packages"

# Files allowed to mention the patterns. Paths relative to _ROOT so the
# test reads the same on the laptop and in CI.
_ALLOWLIST = {
    "packages/admin/evolve_admin/backup_keys.py",
    "packages/admin/tests/test_backup_keys.py",
    "packages/admin/tests/test_no_per_bot_backup_writer.py",
    "packages/analyzer/backup.py",   # READ side only — ssh_key_path
    "packages/admin/evolve_admin/setup_wizard.py",  # sudoers grant + cutover doc strings
}


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for p in _PACKAGES.rglob("*.py"):
        rel = p.relative_to(_ROOT).as_posix()
        if "__pycache__" in rel:
            continue
        files.append(p)
    return files


def _relpath(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


# Regexes are intentionally loose so a "creative" rewrite still trips.
# The unified writer satisfies them too — that's why it's allowlisted.
_RE_SSH_KEYGEN = re.compile(
    r"ssh-keygen[^\n]*evolve-backup-|evolve-backup-[^\n]*ssh-keygen",
)
_RE_MKSTEMP = re.compile(
    r"mkstemp[^\n]*evolve-backup-",
)
_RE_SUDO_CP = re.compile(
    r"sudo.*?/bin/cp[^\n]*evolve-backup-|/bin/cp[^\n]*evolve-backup-",
)


@pytest.mark.parametrize("pattern,name", [
    (_RE_SSH_KEYGEN, "ssh-keygen + evolve-backup-"),
    (_RE_MKSTEMP, "mkstemp + evolve-backup-"),
    (_RE_SUDO_CP, "sudo /bin/cp + evolve-backup-"),
])
def test_no_per_bot_backup_writer_shape(pattern: re.Pattern, name: str) -> None:
    """No file outside the allowlist may reproduce the forbidden shape."""
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_py_files():
        rel = _relpath(path)
        if rel in _ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append((rel, lineno, line.strip()[:140]))
    assert not offenders, (
        f"Forbidden pattern reintroduced ({name}). The per-bot backup-SSH-key "
        f"writer was deleted by the unification spec at "
        f"internal/spec-backup-key-distribution-unification-2026-06-08.md and must "
        f"NOT be reintroduced. Hits:\n"
        + "\n".join(f"  {rel}:{ln}  {snippet}" for rel, ln, snippet in offenders)
    )


def test_allowlist_paths_exist() -> None:
    """Sanity: every allowlisted path must exist. Drift here is a real bug."""
    missing = [p for p in _ALLOWLIST if not (_ROOT / p).exists()]
    assert not missing, f"Allowlist entries don't exist: {missing}"
