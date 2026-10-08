"""tests/test_no_conflict_markers.py — no tracked file carries an unresolved
merge conflict.

Two `internal/dispatch/done/` briefs reached `main` on 2026-09-15 and
2026-09-16 with conflict markers sitting in their YAML front-matter, from
rename conflicts (`inflight/` -> `done/`) committed as-is. Nothing noticed
for a day and a half: they are markdown, so no compiler saw them, no linter
covers that tree, and `tools/preflight` has no repo-wide check for the
shape. What DID notice was `tools/meta-dispatch-eligible`, which reported
both entries as `invalid` — the lane's own bookkeeping could no longer read
the state of two finished chips, and an invalid entry is silent unless
somebody runs the tool and reads its least-interesting field.

The detection rule is the TRIPLE, not any single line. `^=======$` on its own
is a reStructuredText underline and occurs legitimately in this repo (for
example `roster_baseline.py`), so matching it alone would red the suite on
prose. A file is flagged only when it carries BOTH an opening `<<<<<<<` and a
closing `>>>>>>>` line — a shape no docstring produces.

Scoped through `scan_scope.scan_files()`, so in CI it reads the tracked tree
and under `tools/preflight` it also reads untracked-and-not-ignored files: a
conflict marker in a file the author has not `git add`ed yet is the same
defect one push earlier, and this gate is cheap enough to see it. Registered
in `scan_scope.REPO_SCANNERS` as SCAN_SCOPE, which is what
`test_scan_scope.py::test_every_repo_scanner_declares_its_scope` requires of
any new repo-wide scanner.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _REPO_ROOT / "tools"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


scan_scope = _load("scan_scope_for_conflict_markers", _TOOLS / "scan_scope.py")

# Built from parts so this file does not match its own rule. Git writes seven
# characters; a rename conflict writes eight, with the path appended after a
# colon, so both widths and both "bare or labelled" forms are covered.
_OPEN = re.compile(r"^" + "<" * 7 + r"<?(?:[ \t].*)?$")
_CLOSE = re.compile(r"^" + ">" * 7 + r">?(?:[ \t].*)?$")

_MAX_BYTES = 4_000_000  # a marker lives in a text file; skip anything huge


def _conflicted(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # binary or unreadable: not a conflicted text file
    opens, closes = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if _OPEN.match(line):
            opens.append(f"line {n}")
        elif _CLOSE.match(line):
            closes.append(f"line {n}")
    if opens and closes:
        return opens + closes
    return []


def test_no_tracked_file_carries_a_conflict_marker():
    offenders = {}
    for rel in scan_scope.scan_files(_REPO_ROOT):
        p = _REPO_ROOT / rel
        try:
            if not p.is_file() or p.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        hits = _conflicted(p)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "tracked files contain unresolved merge-conflict markers — resolve "
        "them, do not delete one side blindly (both sides of the two dispatch "
        "briefs that hit this were legitimate front-matter keys): "
        + "; ".join(f"{k} ({', '.join(v)})" for k, v in sorted(offenders.items()))
    )


def test_the_rule_needs_both_halves_not_one_line(tmp_path: Path):
    """The control: a lone `=======` (a reStructuredText underline, which this
    repo really contains) must NOT be flagged, and a real conflict must be."""
    underline = tmp_path / "docstring.py"
    underline.write_text('"""Title\n' + "=" * 7 + '\n\nprose\n"""\n')
    assert _conflicted(underline) == []

    conflict = tmp_path / "conflicted.md"
    conflict.write_text(
        "a: 1\n" + "<" * 7 + " HEAD\nb: 2\n" + "=" * 7 + "\nb: 3\n"
        + ">" * 7 + " other\n"
    )
    assert _conflicted(conflict), "a real conflict must be flagged"


def test_a_rename_conflicts_eight_char_markers_are_caught(tmp_path: Path):
    """git writes EIGHT characters for a rename/rename conflict and appends
    the path — the exact shape of the two briefs that reached main."""
    p = tmp_path / "renamed.md"
    p.write_text(
        "<" * 8 + " HEAD:internal/dispatch/done/x.md\npr: 1\n" + "=" * 8
        + "\nstarted: now\n" + ">" * 8 + " refs/pm/main:internal/dispatch/inflight/x.md\n"
    )
    assert _conflicted(p)
