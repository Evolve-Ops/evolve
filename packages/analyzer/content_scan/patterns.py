"""content_scan.patterns — pattern-matching primitives for the catalog.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §3.2 (scan
exclusions) + §5.2 (scan cycle).

Four matcher kinds:
  - ``regex``       — re.findall against the file content (with optional flags)
  - ``html_comment`` — find every <!-- ... --> region; report any whose opening
                     marker isn't in the Evolve-marker allowlist (wildcards
                     supported via ``*`` glob)
  - ``line_length`` — flag any line longer than threshold chars
  - ``structural``  — fire when file size < min_size_bytes (kind only fires
                     when the file matches applies_to filter)

Scan exclusions applied before regex / line_length matchers:
  - Fenced code blocks (triple backtick) — legitimate long base64 / shell
    chains live in docs.
  - Evolve-managed regions — content between known marker pairs is
    Evolve-owned and dynamically rewritten.

The structural matcher applies *before* exclusions (file-level metric).
The html_comment matcher applies *after* fenced-code stripping but
*independent* of the marker-block exclusion (we want to see the
comments themselves).
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Pattern


# ── Match record ──────────────────────────────────────────────────────────────

@dataclass
class Match:
    pattern_id: str
    severity: str
    line: int
    column_start: int
    excerpt: str
    pattern_kind: str = ""  # regex | html_comment | line_length | structural
    context_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "pattern_kind": self.pattern_kind,
            "severity": self.severity,
            "line": self.line,
            "column_start": self.column_start,
            "excerpt": self.excerpt,
            "context_lines": list(self.context_lines),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _strip_fenced_blocks(text: str) -> str:
    """Replace fenced code blocks with whitespace of equal length.

    Preserves line numbers — important so reported ``line`` matches the
    original file. Replacement is space + newline so per-character
    positions also stay aligned for offset-to-line conversion.
    """
    def _replace(m: re.Match) -> str:
        block = m.group(0)
        return "".join(" " if c != "\n" else "\n" for c in block)
    return _FENCED_BLOCK_RE.sub(_replace, text)


_REGION_BEGIN_RE = re.compile(r"<!--[^>]*?:begin\s*-->")


def _strip_evolve_managed_regions(text: str, allowlist: list[str]) -> str:
    """Replace content inside Evolve-managed marker regions with whitespace.

    Walks the text for ``<!-- ...:begin -->`` markers. For each one the
    allowlist allows (literal or wildcard match), derives the corresponding
    ``:end -->`` marker and — if that's also allowlist-allowed — strips the
    span between them. The walker handles nested regions correctly because
    the outermost begin/end pair swallows the inner markers along with
    everything else.

    Pure-literal-pair matching (the prior implementation) failed when
    wildcard allowlist entries like ``<!-- evolve-managed:* -->`` were
    meant to cover begin/end region pairs as well as standalone markers
    — operators would add ``<!-- evolve-managed:foo:begin -->`` /
    ``...:end -->`` and the content between them would still be scanned.
    """
    if not allowlist:
        return text

    out: list[str] = []
    pos = 0
    while True:
        m = _REGION_BEGIN_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        begin = m.group(0)
        if not _is_marker_allowed(begin, allowlist):
            out.append(begin)
            pos = m.end()
            continue
        # Derive the matching end marker, preserving whatever spacing the
        # source used between ``:begin`` and ``-->``.
        end = re.sub(r":begin(\s*-->)", r":end\1", begin)
        if end == begin or not _is_marker_allowed(end, allowlist):
            out.append(begin)
            pos = m.end()
            continue
        end_idx = text.find(end, m.end())
        if end_idx < 0:
            # No matching end — fail open, leave the begin in place and let
            # downstream content fall through to ordinary scanning.
            out.append(begin)
            pos = m.end()
            continue
        span = text[m.start():end_idx + len(end)]
        # Preserve line numbers by keeping newlines and replacing all other
        # chars with spaces.
        out.append("".join(" " if c != "\n" else "\n" for c in span))
        pos = end_idx + len(end)
    return "".join(out)


def _offset_to_line(text: str, offset: int) -> tuple[int, int]:
    """Convert byte offset to (1-based line number, 0-based column)."""
    prefix = text[: max(0, offset)]
    line = prefix.count("\n") + 1
    last_newline = prefix.rfind("\n")
    col = offset - (last_newline + 1) if last_newline >= 0 else offset
    return line, col


def _context(lines: list[str], line_1based: int) -> list[str]:
    """Return up to ±1 lines around the matched line (1-based index)."""
    idx = line_1based - 1
    start = max(0, idx - 1)
    end = min(len(lines), idx + 2)
    return lines[start:end]


def _is_marker_allowed(marker: str, allowlist: list[str]) -> bool:
    """Glob match a comment string against the allowlist.

    Each allowlist entry is a literal-or-wildcard ``<!-- ... -->`` form;
    ``fnmatch`` provides shell-style ``*`` semantics.
    """
    marker_normalized = marker.strip()
    for allowed in allowlist:
        if "*" in allowed:
            if fnmatch.fnmatchcase(marker_normalized, allowed):
                return True
        elif marker_normalized == allowed.strip():
            return True
    return False


def _compile_flags(flag_names: list[str]) -> int:
    flag_bits = 0
    for f in flag_names or []:
        f_low = f.lower()
        if f_low == "ignorecase":
            flag_bits |= re.IGNORECASE
        elif f_low == "multiline":
            flag_bits |= re.MULTILINE
        elif f_low == "dotall":
            flag_bits |= re.DOTALL
    return flag_bits


# ── Per-kind matchers ─────────────────────────────────────────────────────────

def _match_regex(
    pattern: Pattern,
    text_for_match: str,
    raw_lines: list[str],
    limit: int = 20,
) -> list[Match]:
    """Find regex matches in ``text_for_match`` (already stripped of excluded
    regions); report locations in line/column space of the raw file."""
    if not pattern.pattern:
        return []
    try:
        compiled = re.compile(pattern.pattern, _compile_flags(pattern.flags))
    except re.error:
        return []
    matches: list[Match] = []
    for m in compiled.finditer(text_for_match):
        if len(matches) >= limit:
            break
        line, col = _offset_to_line(text_for_match, m.start())
        excerpt = m.group(0)
        if len(excerpt) > 200:
            excerpt = excerpt[:197] + "…"
        matches.append(Match(
            pattern_id=pattern.id,
            pattern_kind="regex",
            severity=pattern.severity,
            line=line,
            column_start=col,
            excerpt=excerpt,
            context_lines=_context(raw_lines, line),
        ))
    return matches


def _match_html_comment_unknown(
    pattern: Pattern,
    text_for_match: str,
    raw_lines: list[str],
    allowlist: list[str],
    limit: int = 20,
) -> list[Match]:
    """Report HTML comments whose marker isn't in the Evolve allowlist."""
    matches: list[Match] = []
    for m in _HTML_COMMENT_RE.finditer(text_for_match):
        if len(matches) >= limit:
            break
        marker = m.group(0)  # full "<!-- ... -->"
        if _is_marker_allowed(marker, allowlist):
            continue
        line, col = _offset_to_line(text_for_match, m.start())
        excerpt = marker
        if len(excerpt) > 200:
            excerpt = excerpt[:197] + "…"
        matches.append(Match(
            pattern_id=pattern.id,
            pattern_kind="html_comment",
            severity=pattern.severity,
            line=line,
            column_start=col,
            excerpt=excerpt,
            context_lines=_context(raw_lines, line),
        ))
    return matches


def _match_line_length(
    pattern: Pattern,
    raw_lines: list[str],
    limit: int = 10,
) -> list[Match]:
    if pattern.threshold <= 0:
        return []
    matches: list[Match] = []
    for i, line in enumerate(raw_lines, start=1):
        if len(line) >= pattern.threshold:
            excerpt = line if len(line) <= 200 else line[:197] + "…"
            matches.append(Match(
                pattern_id=pattern.id,
                pattern_kind="line_length",
                severity=pattern.severity,
                line=i,
                column_start=0,
                excerpt=excerpt,
                context_lines=_context(raw_lines, i),
            ))
            if len(matches) >= limit:
                break
    return matches


def _match_structural(
    pattern: Pattern,
    filename: str,
    file_size_bytes: int,
) -> list[Match]:
    """File-level metric — fires if size < min_size_bytes."""
    if not pattern.min_size_bytes:
        return []
    if pattern.applies_to and filename not in pattern.applies_to:
        return []
    if file_size_bytes >= pattern.min_size_bytes:
        return []
    return [Match(
        pattern_id=pattern.id,
        pattern_kind="structural",
        severity=pattern.severity,
        line=0,
        column_start=0,
        excerpt=(
            f"file {filename} is {file_size_bytes} bytes "
            f"(expected ≥ {pattern.min_size_bytes})"
        ),
        context_lines=[],
    )]


# ── Public entry point ────────────────────────────────────────────────────────

def scan_file(
    text: str,
    filename: str,
    patterns: list[Pattern],
    evolve_markers_allowlist: list[str],
    file_size_bytes: int | None = None,
) -> list[Match]:
    """Run all catalog patterns against one file's content.

    Returns a flat list of Match records; the caller groups them per
    (bot, file, pattern_id) for signal emission.
    """
    if file_size_bytes is None:
        file_size_bytes = len(text.encode("utf-8"))

    raw_lines = text.splitlines()

    # Pre-compute the exclusion-stripped text once (most patterns use it).
    stripped = _strip_fenced_blocks(text)
    stripped = _strip_evolve_managed_regions(stripped, evolve_markers_allowlist)

    all_matches: list[Match] = []
    for pattern in patterns:
        if pattern.applies_to and pattern.kind != "structural":
            # applies_to on non-structural patterns: only run for matching files.
            if filename not in pattern.applies_to:
                continue
        if pattern.kind == "regex":
            all_matches.extend(_match_regex(pattern, stripped, raw_lines))
        elif pattern.kind == "html_comment":
            all_matches.extend(_match_html_comment_unknown(
                pattern, stripped, raw_lines, evolve_markers_allowlist,
            ))
        elif pattern.kind == "line_length":
            all_matches.extend(_match_line_length(pattern, raw_lines))
        elif pattern.kind == "structural":
            all_matches.extend(_match_structural(pattern, filename, file_size_bytes))
        # Unknown kinds are silently skipped — operator can add more later.

    return all_matches
