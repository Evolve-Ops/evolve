"""Prompt injection scanner.

Two-stage detector mirroring the credential scanner:

  1. Regex stage — load patterns from ``patterns/injection.yaml`` and find
     all matches in the input text.
  2. LLM verification stage — for regex hits, a callable returns a
     ``VerifierResult`` with ``verdict`` (``injection`` | ``legitimate`` |
     ``ambiguous``) and a confidence score. Only ``injection`` with
     confidence ≥ ``confidence_threshold`` is treated as a real exposure.

Spec: docs/archive/specs/spec-security-warden-completion-2026-04-18.md §4.

The default verifier accepts every regex hit (production wires this to
a Haiku call). Tests override via ``set_llm_verifier()``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


logger = logging.getLogger(__name__)


class PatternLoadError(RuntimeError):
    """Raised when injection.yaml cannot be loaded or contains no usable patterns.

    Loud failure is deliberate: a silent empty pattern list would mean
    the scanner appears to run but never fires, which is the worst
    possible failure mode for a security detector.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Pattern loading
# ─────────────────────────────────────────────────────────────────────────────


_PATTERNS_FILE = Path(__file__).parent.parent / "patterns" / "injection.yaml"

# Cached compiled patterns: list of (id, compiled_regex).
_compiled_patterns: list[tuple[str, "re.Pattern[str]"]] | None = None


def _load_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    global _compiled_patterns
    if _compiled_patterns is not None:
        return _compiled_patterns

    if not _PATTERNS_FILE.exists():
        raise PatternLoadError(
            f"injection pattern file missing: {_PATTERNS_FILE}"
        )

    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise PatternLoadError(
            f"PyYAML required to load injection patterns ({_PATTERNS_FILE}): {e}"
        ) from e

    try:
        raw = yaml.safe_load(_PATTERNS_FILE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PatternLoadError(
            f"failed to read/parse {_PATTERNS_FILE}: {e}"
        ) from e

    entries = (raw or {}).get("patterns") or []
    compiled: list[tuple[str, re.Pattern[str]]] = []
    skipped: list[tuple[str, str]] = []
    for entry in entries:
        pid = entry.get("id") if isinstance(entry, dict) else None
        regex = entry.get("regex") if isinstance(entry, dict) else None
        if not pid or not regex:
            skipped.append((str(entry)[:60], "missing id or regex"))
            continue
        try:
            compiled.append((pid, re.compile(regex)))
        except re.error as e:
            skipped.append((pid, f"bad regex: {e}"))

    if skipped:
        for pid, why in skipped:
            logger.error(
                "prompt_injection: skipping pattern %r: %s", pid, why
            )

    if not compiled:
        raise PatternLoadError(
            f"no valid injection patterns loaded from {_PATTERNS_FILE} "
            f"(skipped {len(skipped)})"
        )

    _compiled_patterns = compiled
    return compiled


def reload_patterns() -> None:
    """Drop the pattern cache. Used by tests after editing the YAML."""
    global _compiled_patterns
    _compiled_patterns = None


# ─────────────────────────────────────────────────────────────────────────────
# Match + verifier types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class InjectionMatch:
    """One regex hit. ``match_text`` is held only inside the scanner; it
    must never be propagated into proposal user-facing fields."""

    pattern_id: str
    start: int
    end: int
    match_text: str


Verdict = Literal["injection", "legitimate", "ambiguous"]


@dataclass
class VerifierResult:
    """LLM verifier output."""

    verdict: Verdict
    confidence: float
    rationale: str = ""


@dataclass
class ScanResult:
    """Outcome of scanning one piece of text."""

    has_injection: bool
    matches: list[InjectionMatch] = field(default_factory=list)
    # Pattern ids + lengths only — safe to log / put in proposals.
    summary: str = ""
    verifier: VerifierResult | None = None


# Default confidence threshold from spec §3 (config block).
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


# ─────────────────────────────────────────────────────────────────────────────
# LLM verifier seam
# ─────────────────────────────────────────────────────────────────────────────


LLMVerifier = Callable[[str, list[InjectionMatch]], VerifierResult]


def _default_verifier(
    text: str, matches: list[InjectionMatch]  # noqa: ARG001
) -> VerifierResult:
    """Placeholder verifier — accepts every regex hit as injection.

    Production wiring replaces this with a Haiku call that returns
    ``{verdict, confidence, rationale}`` after reading the snippet in
    context. Tests override via ``set_llm_verifier()`` to exercise
    threshold and verdict-handling branches.
    """
    return VerifierResult(verdict="injection", confidence=1.0, rationale="default")


_verifier: LLMVerifier = _default_verifier


def set_llm_verifier(fn: LLMVerifier) -> None:
    """Install an LLM verifier (tests + production wiring)."""
    global _verifier
    _verifier = fn


def reset_llm_verifier() -> None:
    global _verifier
    _verifier = _default_verifier


# ─────────────────────────────────────────────────────────────────────────────
# Scanning
# ─────────────────────────────────────────────────────────────────────────────


def _describe(matches: list[InjectionMatch]) -> str:
    return ", ".join(f"[{m.pattern_id}, {m.end - m.start} chars]" for m in matches)


def find_matches(text: str) -> list[InjectionMatch]:
    """Stage 1: regex scan. Public for testing the pattern set in isolation."""
    if not text:
        return []
    patterns = _load_patterns()
    matches: list[InjectionMatch] = []
    for pid, regex in patterns:
        for m in regex.finditer(text):
            matches.append(
                InjectionMatch(
                    pattern_id=pid,
                    start=m.start(),
                    end=m.end(),
                    match_text=m.group(0),
                )
            )
    matches.sort(key=lambda m: (m.start, m.end))
    return matches


def scan_text(
    text: str,
    *,
    verify_with_llm: bool = True,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ScanResult:
    """Two-stage scan for prompt injection patterns.

    Stage 1 finds regex matches. Stage 2 (if ``verify_with_llm`` and stage
    1 found anything) calls the LLM verifier. ``has_injection`` is True
    only when the verifier returns ``verdict='injection'`` with
    ``confidence >= threshold`` — anything ambiguous or below threshold is
    treated as a non-event.
    """
    matches = find_matches(text)
    if not matches:
        return ScanResult(has_injection=False, matches=[], summary="")

    summary = _describe(matches)

    if not verify_with_llm:
        return ScanResult(has_injection=True, matches=matches, summary=summary)

    verdict = _verifier(text, matches)
    confirmed = (
        verdict.verdict == "injection" and verdict.confidence >= confidence_threshold
    )
    return ScanResult(
        has_injection=confirmed,
        matches=matches,
        summary=summary if confirmed
        else f"{len(matches)} regex hit(s) {verdict.verdict} (confidence {verdict.confidence:.2f})",
        verifier=verdict,
    )
