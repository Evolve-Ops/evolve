"""Annotation gate for the AL-1.4b reader sweep — the ONE gate, repo-wide.

Brief: ``docs/build-AL-1.4-app-id-canonical.md`` §3 (the gate), §7 (the areas).

CONSOLIDATION (2026-08-18). Two gates briefly existed: this one (#3695, AST +
per-site, but over two HARDCODED area lists under ``applications/``) and a
repo-wide one (#3704, discovery + every language, but per-FILE text matching).
Each covered the other's blind spot — #3695 could not see areas 1/2/3/4c, the
analyzer or the plugin, because its lists never grew; #3704 counted a file as
covered when its annotation sat anywhere in the file, which is how it passed
while 54 reads were genuinely uncovered. They are now one gate: this module's
AST precision, applied by DISCOVERY over all three package roots, plus a
weaker text pass for the non-Python surfaces AST cannot parse. See
``test_al_1_4b_identity_gate.py``.

§3 states the gate as a grep run once at review time. This module makes it a
test, with two corrections to the literal regex and one concession to how the
sweep was actually written.

**Correction 1 — hits are found by AST, not by regex.** §3's regex matches
text, so it fires on docstring prose (a module *documenting* ``provenance
.spec_id``) and misses real reads. Measured across area 4a's 21 files on the
commit that swept them: the regex matches 99 lines for 84 real reads — 18 false
positives, and 4 false negatives (``adopt.py`` ×2, ``cleanup_invalid_claims
.py``, ``manifest.py``), each a ``.get('<field>')`` written with SINGLE quotes
inside an f-string, which ``get\\("<field>"\\)`` cannot see. Walking for
``Attribute`` nodes and ``.get("<field>")`` calls is narrower where the regex
was noisy and wider where it was blind.

**Correction 2 — coverage is structural, not a line-distance lookback.** An
annotation must sit inside the hit's enclosing top-level ``def``/``class``, or
in the comment preamble directly above it, never far enough to reach into the
PRECEDING block. A distance rule credits a neighbouring function's annotation
and reds spuriously the moment a line is inserted.

**Concession — module-level notes are vouched, not verified.** The areas were
swept by different chips in different styles. Some annotated per site; area 4a
(#3684) wrote one thorough note per module instead, claiming the whole module
at once ("AL-1.4b swept this module and kept every ``spec_id`` in it because
…"). Both are legitimate and the module-level notes are substantively correct,
but a whole-module claim is not per-site checkable: a read added to such a
module tomorrow inherits the vouch without anyone re-deciding it.

So this gate reports two populations rather than pretending to one standard:
``per_site`` files, which it genuinely verifies, and ``module_vouched`` files,
which it counts and names but cannot check. Treating the second as passing
would overstate what the gate proves; treating it as failing would red main for
a style choice made deliberately. Naming the split is the honest option, and
the count is the thing to watch — a module that grows new identity reads under
a blanket vouch is where this sweep will rot first.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: The legacy identity fields AL-1.4 collapses onto ``app_id``.
LEGACY_ID_FIELDS = frozenset({"spec_id", "instance_id", "pkg_id"})

#: The canonical annotation, used in failure messages and by new code.
ANNOTATION = "identity: see resolve_app_id"

#: What the gate ACCEPTS. Two conventions were written independently and both
#: are correct: area 1's ``identity: NOT resolve_app_id (§8 D1)`` (this read
#: deliberately does not go through the resolver) alongside the canonical
#: ``identity: see resolve_app_id``. Requiring the literal canonical string
#: flagged 19 already-annotated reads as bare — a gate failing on house style
#: rather than on substance. The rule is: name the concern (``identity:``) and
#: name the resolver, so the annotation is greppable from either direction.
#: ``appIdOf`` is the TS twin, for the text pass below.
ANNOTATION_RE = re.compile(r"identity:.*?(resolve_app_id|appIdOf)", re.IGNORECASE)

#: Non-Python surfaces: no AST, so these get the weaker per-FILE text check.
TEXT_SURFACE_SUFFIXES = (".ts", ".js", ".html")

#: The legacy-field pattern, for the text pass only.
TEXT_FIELD_RE = re.compile(r"\bspec_id\b|\binstance_id\b|\bpkg_id\b")

#: The resolvers themselves — naming the legacy chain IS their subject matter.
RESOLVER_PATHS = (
    "packages/admin/evolve_admin/applications/app_identity.py",
    "packages/plugin/src/apps/appIdentity.ts",
)

#: How far above an enclosing ``def`` its comment block may start.
PREAMBLE = 8


def identity_read_lines(tree: ast.Module) -> dict[int, set[str]]:
    """Line -> legacy fields genuinely READ on it (attribute or ``.get``)."""
    hits: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in LEGACY_ID_FIELDS:
            hits.setdefault(node.lineno, set()).add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in LEGACY_ID_FIELDS
        ):
            hits.setdefault(node.lineno, set()).add(node.args[0].value)
    return hits


def has_module_note(tree: ast.Module) -> bool:
    """True when the module DOCSTRING carries a whole-module identity vouch."""
    doc = ast.get_docstring(tree) or ""
    return bool(ANNOTATION_RE.search(doc))


def search_floor(tree: ast.Module, line: int) -> int:
    """Earliest line an annotation may sit on to cover a hit at ``line``."""
    blocks = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    start = line  # module level: only the preamble window applies
    prev_end = 0
    for node in blocks:
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end:
            start = node.lineno
            break
        if end < line:
            prev_end = end
    return max(prev_end + 1, start - PREAMBLE)


def uncovered_hits(name: str, src: str) -> list[str]:
    """Identity reads in ``src`` with no annotation in their enclosing block.

    Always per-site; callers decide whether a module-level vouch exempts the
    file (see :func:`sweep_area`).
    """
    lines = src.split("\n")
    tree = ast.parse(src)
    out: list[str] = []
    for line_no in sorted(identity_read_lines(tree)):
        i = line_no - 1
        if ANNOTATION_RE.search(lines[i]):
            continue
        if any(ANNOTATION_RE.search(prev)
               for prev in lines[search_floor(tree, line_no) - 1:i]):
            continue
        out.append(f"{name}:{line_no}: {lines[i].strip()}")
    return out


class AreaSweep:
    """What the gate can and cannot prove about one area."""

    def __init__(self) -> None:
        self.uncovered: list[str] = []      # per-site files, genuinely unannotated
        self.module_vouched: list[str] = [] # files claimed wholesale, unverifiable
        self.vouched_reads = 0              # reads inside those files
        self.checked_reads = 0              # reads the gate actually verified

    @property
    def total_reads(self) -> int:
        return self.checked_reads + self.vouched_reads


def sweep_area(app_dir: Path, files: tuple[str, ...]) -> AreaSweep:
    """Run the gate over ``files``, splitting verified from vouched."""
    result = AreaSweep()
    for name in files:
        src = (app_dir / name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        reads = len(identity_read_lines(tree))
        if has_module_note(tree):
            result.module_vouched.append(name)
            result.vouched_reads += reads
            continue
        result.checked_reads += reads
        result.uncovered += uncovered_hits(name, src)
    return result


# ── Discovery (the half that came from #3704) ────────────────────────────────
#
# The hardcoded AREA_4A_FILES / AREA_4B_FILES tuples this module started with
# could only ever check the two areas someone had typed out. A file added to
# `applications/` tomorrow, and every file in areas 1/2/3/4c, the analyzer and
# the plugin, were invisible to the gate no matter how many reads they grew.
# Discovery removes the list, so the gate's coverage is a property of the repo
# rather than of somebody's memory.

#: Roots the sweep covers, relative to the repo root.
SWEEP_ROOTS = (
    "packages/admin/evolve_admin",
    "packages/analyzer",
    "packages/plugin/src",
)


def repo_root(start: Path) -> Path:
    """The repo root, found by walking up to the dir holding ``packages/``."""
    for cand in (start, *start.parents):
        if (cand / "packages").is_dir() and (cand / "tools").is_dir():
            return cand
    raise RuntimeError(f"repo root not found above {start}")


def _skip(rel: str) -> bool:
    return (
        "/tests/" in rel
        or "/test/" in rel
        or "/node_modules/" in rel
        or "/dist/" in rel
        or rel in RESOLVER_PATHS
    )


def discover_python(root: Path) -> list[Path]:
    """Every non-test ``.py`` under the sweep roots with a real identity read.

    "Real" is the AST definition — an attribute access or a ``.get("<field>")``
    — so a module that merely *documents* ``provenance.spec_id`` in prose is not
    dragged in.
    """
    out: list[Path] = []
    for rel_root in SWEEP_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if _skip(rel):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            if identity_read_lines(tree):
                out.append(p)
    return out


def discover_text_surface(root: Path) -> list[Path]:
    """Non-Python files naming a legacy id field.

    TS / JS / HTML get the weaker PER-FILE check: there is no AST here, so the
    gate can only ask whether the file explains itself somewhere, not whether
    each site is covered. Kept deliberately rather than dropped — the plugin's
    observer and the admin SPA both read identity, and a gate that silently
    stopped at ``.py`` would have called that surface clean without looking.
    """
    out: list[Path] = []
    for rel_root in SWEEP_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in TEXT_SURFACE_SUFFIXES:
                continue
            rel = p.relative_to(root).as_posix()
            if _skip(rel):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if TEXT_FIELD_RE.search(text):
                out.append(p)
    return out


def sweep_paths(root: Path, paths: list[Path]) -> AreaSweep:
    """:func:`sweep_area`, keyed on absolute paths so it spans packages."""
    result = AreaSweep()
    for p in paths:
        rel = p.relative_to(root).as_posix()
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        reads = len(identity_read_lines(tree))
        if has_module_note(tree):
            result.module_vouched.append(rel)
            result.vouched_reads += reads
            continue
        result.checked_reads += reads
        result.uncovered += uncovered_hits(rel, src)
    return result
