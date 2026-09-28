"""Credential exposure scanner.

Stage 1: regex patterns from ``generators.security_warden.redact``.
Stage 2: (optional) LLM verification pass — in L3 we ship just the regex
stage. The LLM verifier can be plugged in by ``set_llm_verifier()`` as
in the extractor pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from generators.security_warden.redact import (
    Match,
    find_matches,
    describe_matches,
)


@dataclass
class ScanResult:
    """Outcome of scanning one piece of text."""

    has_exposure: bool
    matches: list[Match]
    # A safe-to-log description: pattern ids + lengths, no verbatim content
    summary: str


# LLM verifier: takes (text, matches) and returns True if exposures
# look real after LLM review. Default returns True (accept regex hits).
LLMVerifier = Callable[[str, list[Match]], bool]


def _default_verifier(text: str, matches: list[Match]) -> bool:  # noqa: ARG001
    """Placeholder verifier — accepts all regex hits as real exposures.

    Production wiring replaces this with a cheap Haiku call that asks
    whether the matched substrings are actually credentials vs e.g. code
    examples, documentation, or mock values.
    """
    return True


_verifier: LLMVerifier = _default_verifier


def set_llm_verifier(fn: LLMVerifier) -> None:
    """Install an LLM verifier (tests + production wiring)."""
    global _verifier
    _verifier = fn


def reset_llm_verifier() -> None:
    global _verifier
    _verifier = _default_verifier


def scan_text(text: str, *, verify_with_llm: bool = True) -> ScanResult:
    """Scan text for credential exposure.

    Stage 1: regex match. Stage 2 (if ``verify_with_llm`` and hits found):
    LLM confirmation. Returns a ScanResult with ``has_exposure`` set only
    when stage-2 (or stage-1 alone, if verify disabled) confirms.
    """
    matches = find_matches(text)
    if not matches:
        return ScanResult(has_exposure=False, matches=[], summary="")

    if verify_with_llm:
        confirmed = _verifier(text, matches)
        if not confirmed:
            return ScanResult(
                has_exposure=False,
                matches=matches,
                summary=f"{len(matches)} regex hit(s) rejected by verifier",
            )

    return ScanResult(
        has_exposure=True,
        matches=matches,
        summary=describe_matches(matches),
    )
