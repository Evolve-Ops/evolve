"""Roadmap 0.5/2.10 proof artifact: zero predictable /tmp staging names.

Predictable names like ``/tmp/evolve-<bot>-<purpose>.json`` feeding the sudoers
``/bin/cp /tmp/evolve-*`` grants are a local TOCTOU/symlink surface (an attacker
pre-creates or swaps the path between our write and the root cp). All staging
must go through ``evolve_admin.deploy._secure_stage`` / ``analyzer.staging
.secure_stage`` (mkstemp: O_EXCL + unguessable suffix).

This is the roadmap's own proof grep, executable: it fails if any non-test,
non-comment source line builds an f-string ``/tmp/evolve-…`` path again.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOTS = [
    _REPO_ROOT / "packages" / "admin" / "evolve_admin",
    _REPO_ROOT / "packages" / "analyzer",
]

# f"/tmp/evolve-…{…}…" — an interpolated (hence caller-shaped, predictable)
# staging path. Literal constants and mkstemp prefixes don't match.
_PREDICTABLE = re.compile(r"""f["']/tmp/evolve-[^"']*\{""")


def test_no_predictable_tmp_evolve_staging_names() -> None:
    offenders: list[str] = []
    for root in _SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            try:
                text = path.read_text()
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if _PREDICTABLE.search(line):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Predictable /tmp/evolve-* staging names found — use "
        "deploy._secure_stage / analyzer staging.secure_stage instead:\n"
        + "\n".join(offenders)
    )
