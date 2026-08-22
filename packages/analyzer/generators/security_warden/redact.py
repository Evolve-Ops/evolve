"""generators.security_warden.redact — Patterns for detecting + redacting secrets.

Used by two places:
  - The credential scanner (flagging sensitive content in observations)
  - The charter invariant that forbids Warden's own proposals from quoting
    exposed credentials verbatim

Patterns are intentionally narrow to keep false-positive rate low. Broaden
only when the Warden's false-positive rate is measured as low enough to
support it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Match:
    """One regex hit with position info."""

    pattern_id: str
    start: int
    end: int
    match_text: str  # the matched substring (not stored long-term in proposals)


# ─────────────────────────────────────────────────────────────────────────────
# Pattern set
# ─────────────────────────────────────────────────────────────────────────────
#
# Each pattern has a stable id used to describe the type of match without
# revealing the matched content. The regex must be anchored enough to
# avoid casual matches (e.g., not "password" appearing in unrelated prose).

_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # Anthropic-style API key
    (
        "anthropic_api_key",
        re.compile(r"\bsk-ant-[a-zA-Z0-9\-_]{30,}\b"),
    ),
    # OpenAI-style API key
    (
        "openai_api_key",
        re.compile(r"\bsk-[a-zA-Z0-9]{32,}\b"),
    ),
    # AWS-style access key id
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    # Slack bot/user tokens
    (
        "slack_token",
        re.compile(r"\bxox[bp]-[a-zA-Z0-9\-]{10,}\b"),
    ),
    # SSH private key header
    (
        "ssh_private_key",
        re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    ),
    # GitHub personal access token (new format)
    (
        "github_pat",
        re.compile(r"\bghp_[a-zA-Z0-9]{36}\b"),
    ),
    # Generic password in key=value form (heuristic; narrow enough to avoid
    # matching casual "password" references in prose)
    (
        "password_kv",
        re.compile(
            r"""(?ix)
            \b(?:password|passwd|pwd)\s*[:=]\s*
            ['"]?
            (?P<value>[^\s'";]{8,})
            ['"]?
            """
        ),
    ),
]


def find_matches(text: str) -> list[Match]:
    """Scan ``text`` for credential-like patterns."""
    if not text:
        return []
    matches: list[Match] = []
    for pattern_id, regex in _PATTERNS:
        for m in regex.finditer(text):
            matches.append(
                Match(
                    pattern_id=pattern_id,
                    start=m.start(),
                    end=m.end(),
                    match_text=m.group(0),
                )
            )
    # Sort by position for deterministic ordering
    matches.sort(key=lambda m: (m.start, m.end))
    return matches


def contains_secret(text: str) -> bool:
    """True if ``text`` contains any credential-like pattern."""
    return bool(find_matches(text))


def is_safe(text: str) -> bool:
    """Invariant predicate: ``text`` is safe to include in a proposal.

    A Warden proposal's problem / admin_surface_summary / conversational_pitch
    fields must all pass ``is_safe``; otherwise the proposal is rejected
    before emission. This guards the ``never_reveals_secrets`` invariant
    declared in the Warden's charter.
    """
    return not contains_secret(text)


def describe_match(m: Match) -> str:
    """Return a non-sensitive description of a match.

    Never quotes the matched value verbatim — returns only the pattern id
    and a length indicator.
    """
    return f"[{m.pattern_id}, {m.end - m.start} chars]"


def describe_matches(matches: list[Match]) -> str:
    """Join descriptions of multiple matches, without revealing content."""
    return ", ".join(describe_match(m) for m in matches)
