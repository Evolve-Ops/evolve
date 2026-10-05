"""conftest.py — CI quarantine hook for packages/analyzer.

Reads ``ci-quarantine.txt`` at the repo root and deselects every listed
test during collection.  This lets the blocking CI gate run the full suite
while skipping the known-baseline failures — so only *new* breakage fails
the build.

Format of ci-quarantine.txt:
  <package>/<pytest-node-id>  # reason comment
Lines starting with '#' and blank lines are ignored.
Package prefix "analyzer/" is stripped; the remainder is the node-id for
this package.  Lines with other package prefixes are skipped silently.
"""
from __future__ import annotations

from pathlib import Path


def _load_quarantine() -> frozenset[str]:
    quarantine_file = Path(__file__).resolve().parents[2] / "ci-quarantine.txt"
    if not quarantine_file.exists():
        return frozenset()
    ids: set[str] = set()
    for raw in quarantine_file.read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if not line.startswith("analyzer/"):
            continue
        # strip the "analyzer/" prefix to get the pytest node-id for this package
        ids.add(line[len("analyzer/"):])
    return frozenset(ids)


_QUARANTINED = _load_quarantine()


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if not _QUARANTINED:
        return
    for item in items:
        # item.nodeid is relative to the rootdir (packages/analyzer)
        if item.nodeid in _QUARANTINED:
            item.add_marker("skip")
