"""anti_domains — Parse explicit "out of scope" markers from a bot's
workspace AGENTS.md and map them to ``domain:*`` tags.

Phase 2 follow-up per internal/spec-rsi-proposal-eligibility-2026-06-05.md.

Both pattern monitors (capability_gap_monitor,
engagement_amplifier_monitor) ask AGENTS.md whether a candidate
domain is in scope. Pre-anti-domain, the answer was either
``confirmed`` (domain keyword appears in purpose) or ``neutral`` /
``emergent`` (it doesn't). There was no way for the operator to say
"this bot explicitly does NOT do X" — so a workout pattern on a
sailing bot still emitted a Signal (just with neutral fit).

This module adds the third state. The operator marks "out of scope"
domains explicitly in AGENTS.md — e.g.:

    ## Out of scope
    - fitness
    - finance

…or inline:

    This bot is NOT for medical questions; out of scope: health.

The parser is conservative on purpose: it requires explicit markers
("out of scope", "excluded", "not for this bot", "don't"). It does
NOT try to interpret general negation in prose. False positives
(treating in-scope as excluded) would silence real RSI proposals;
false negatives (missing an actual exclusion) just keep current
behavior — clearly the safer direction.

The output is a set of ``domain:*`` tags, ready to compare against
the candidate domain in either monitor.
"""
from __future__ import annotations

import re
from typing import Iterable


# Section-header phrases that introduce an "out of scope" block. The
# section runs until the next markdown header (lines starting with
# ``#``) or two blank lines. Match is case-insensitive on the trimmed
# header text — operators write these by hand and we shouldn't be
# fussy about capitalization.
_EXCLUSION_HEADERS = (
    "out of scope",
    "out-of-scope",
    "not for this bot",
    "excluded",
    "exclusions",
    "don't",
    "do not",
    "not my job",
    "scope: excluded",
)

# Inline-phrase prefixes — when a line contains one of these followed
# by a colon, the rest of the line is parsed for excluded items.
_INLINE_EXCLUSION_PREFIXES = (
    "out of scope:",
    "out-of-scope:",
    "not in scope:",
    "excluded:",
    "exclusions:",
)


def _domain_keywords(shared_dir=None) -> dict[str, str]:
    """Return effective ``static ∪ dynamic`` vocabulary so the
    anti-domain parser speaks the same domain language as the
    pattern monitors. Pre-Layer-2 callers pass ``shared_dir=None``
    and get static-only behavior."""
    try:
        import _merged_vocabulary as mv

        return mv.effective_keywords(shared_dir)
    except ImportError:
        return {}


def _section_iter(purpose_text: str) -> Iterable[tuple[str, str]]:
    """Yield (header, body) for each markdown section in the text.

    A section starts at a heading line (``#``-prefixed) and runs
    until the next heading or end of text. The header is the heading
    text stripped of leading ``#`` markers and surrounding whitespace;
    the body is everything else in the section (heading line excluded).
    """
    header = ""
    body_lines: list[str] = []
    for raw in purpose_text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # Yield prior section if any.
            if header:
                yield header, "\n".join(body_lines)
            header = stripped.lstrip("# ").strip()
            body_lines = []
        else:
            if header:
                body_lines.append(line)
    if header:
        yield header, "\n".join(body_lines)


def _matches_exclusion_header(header: str) -> bool:
    h = header.lower().strip()
    return any(phrase in h for phrase in _EXCLUSION_HEADERS)


def _extract_items_from_body(body: str) -> list[str]:
    """Pull bullet items + comma-separated lists from a section body.

    Markdown bullets (``- item``, ``* item``, ``1. item``) and lines
    that look like comma-separated lists ("fitness, finance, health")
    both yield items. Empty lines, prose paragraphs, and indented
    code blocks are skipped — conservative parsing leans away from
    over-extraction.
    """
    items: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip code-fence boundaries.
        if line.startswith("```"):
            continue
        # Bullet markers.
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if bullet:
            items.append(bullet.group(1).strip())
            continue
        # Numbered list.
        numbered = re.match(r"^\d+\.\s+(.*)$", line)
        if numbered:
            items.append(numbered.group(1).strip())
            continue
        # Comma-separated single-line list — but only when the line
        # contains a comma AND is short enough to plausibly be a list
        # (not prose). 80 chars is the operator-readable threshold;
        # longer is almost always a sentence.
        if "," in line and len(line) <= 80:
            for piece in line.split(","):
                piece = piece.strip().rstrip(".")
                if piece:
                    items.append(piece)
    return items


# Per-item word cap. The inline-prefix path parses everything after
# ``out of scope:`` as items, then splits on commas + ``and``. Without
# a length / word check, prose tails like ``Out of scope: but I do
# help with health when asked`` would yield one "item" containing the
# whole sentence — and the substring match would catch ``health`` and
# wrongly exclude domain:health. Real exclusion items are 1–3 words
# ("fitness", "meal planning", "personal finance"). Anything longer
# is prose, not a list entry, and we skip it.
#
# Audit reference: P2 finding in the 2026-06-05 review of PR #2198.
_MAX_WORDS_PER_INLINE_ITEM = 3


def _is_short_phrase(item: str) -> bool:
    """True iff the item is plausibly a domain-naming phrase (≤ 3 words).

    Stricter than the bullet / section-header path because the inline
    prefix path can't tell a list from a prose sentence on its own —
    the operator might write "Out of scope: but we sometimes help" and
    we don't want the parser to extract excluded domains from that
    apologetic prose.
    """
    if not item:
        return False
    words = [w for w in item.split() if w]
    return 0 < len(words) <= _MAX_WORDS_PER_INLINE_ITEM


def _items_from_inline_prefixes(purpose_text: str) -> list[str]:
    """Find lines containing an inline-prefix like ``out of scope:``
    and yield items extracted from the substring after the prefix.

    Per-item word cap (see ``_is_short_phrase``) drops prose-shaped
    tails so an operator who writes apologetic prose after the prefix
    doesn't have words from that prose silently treated as exclusion
    items."""
    items: list[str] = []
    for raw_line in purpose_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        for prefix in _INLINE_EXCLUSION_PREFIXES:
            idx = lower.find(prefix)
            if idx == -1:
                continue
            tail = line[idx + len(prefix):].strip()
            if not tail:
                continue
            # Tail may be a single item or comma-separated list. Split
            # on commas + 'and' as common operator phrasing.
            for piece in re.split(r",|\band\b", tail, flags=re.IGNORECASE):
                piece = piece.strip().rstrip(".").rstrip(";")
                if not piece:
                    continue
                if not _is_short_phrase(piece):
                    # Prose, not a list item — skip rather than risk a
                    # substring match against random sentence words.
                    continue
                items.append(piece)
            break  # only one prefix per line
    return items


def _items_to_domains(
    items: Iterable[str], kw_map: dict[str, str]
) -> set[str]:
    """Map extracted items to ``domain:*`` tags via the keyword vocab.

    Multi-word items match if ANY of their words is a known keyword.
    Conservative: items with no keyword overlap yield no domain
    (rather than guessing). That keeps the parser's signal-to-noise
    high — an operator writing "no investment advice" excludes
    finance (via "investment") without us needing to invent a
    domain.
    """
    out: set[str] = set()
    for item in items:
        item_l = item.lower()
        for kw, tag in kw_map.items():
            if kw in item_l:
                out.add(tag)
    return out


def parse_anti_domains(
    purpose_text: str | None, shared_dir=None
) -> set[str]:
    """Return the set of ``domain:*`` tags the bot has explicitly
    excluded from its scope. Empty set when no explicit markers are
    present — that's the most common case, and the monitors fall back
    to current behavior (neutral / emergent / skip) accordingly.

    Two extraction paths:

      1. Section-header path. Any markdown section whose header
         contains an exclusion phrase (``Out of scope``, ``Excluded``,
         etc.) contributes its bullet items + comma-list items.
      2. Inline-prefix path. Any line containing ``out of scope:``
         (or a recognized variant) contributes the items in its tail.

    Both paths feed the same keyword-vocabulary lookup. False
    positives (treating in-scope as excluded) would silence real RSI
    proposals; false negatives (missing an actual exclusion) just
    keep current behavior. The bias is toward false negatives —
    requires the operator to write the markers explicitly, exactly
    in the documented vocabulary.
    """
    if not purpose_text:
        return set()
    kw_map = _domain_keywords(shared_dir)
    if not kw_map:
        return set()

    items: list[str] = []
    for header, body in _section_iter(purpose_text):
        if _matches_exclusion_header(header):
            items.extend(_extract_items_from_body(body))
    items.extend(_items_from_inline_prefixes(purpose_text))

    return _items_to_domains(items, kw_map)
