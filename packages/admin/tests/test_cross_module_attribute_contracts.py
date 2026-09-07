"""Cross-module attribute contract guard — catches "writer ships, reader doesn't" silent failures.

This bug class has bit us repeatedly in the cascade work:

  - Plugin wrote `cascade.shadow_verdict.escalation_event`; watchdog
    read `cascade.escalation_event` (no prefix) → escalation_storm
    flag dead-on-arrival (BLOCKER from PR #1664)
  - Plugin wrote `usage.input_tokens`; audit_runner read
    `usage.prompt_tokens` → context-token anomaly Signal never fired
    (BLOCKER from PR #1664)
  - Plugin wrote `cascade.consent_source`; labeler treated
    `cascade.tier_chosen_by == "user_request"` as ui_chip proof
    without consulting consent_source → bot-initiated tier1 grants
    polluted Signal-#1 ground truth (HIGH from PR #1664)
  - Plugin wrote `cascade.struggle.payload_drift`; nothing reads it
    (MEDIUM from the cross-system audit, PR #1706 wave)
  - CascadeController.decide() didn't read pressure_flags.json; the
    pressure_watchdog daemon wrote pressure flags into a file with
    no consumer (MEDIUM from same audit, fixed in PR #1706)

Per-module unit tests don't catch any of these — each side passes
its own tests against its own synthetic data, but the writer and
reader live in different languages + processes + test harnesses.

This test closes the gap: walk plugin TS for `cascade.*` writes;
walk analyzer/admin Python for `cascade.*` reads; assert every
writer key has at least one reader (and vice versa).

The check intentionally avoids cleverness — regex over source, not
full TS/Py AST parsing. The cost is small false-positive risk
(e.g., string literals in tests, debug logs), addressed by
allowlists below.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent.parent
_PLUGIN_SRC = _REPO / "packages" / "plugin" / "src"
_ANALYZER = _REPO / "packages" / "analyzer"
_ADMIN = _REPO / "packages" / "admin"


# ── Allowlists ──────────────────────────────────────────────────────────────
# These attributes are intentionally orphan in one direction and are NOT
# bugs. Each entry MUST carry a comment explaining why. If you find
# yourself adding a name here, ask first whether the simpler fix is to
# wire the consumer (or delete the orphan write).

# Writer keys that don't need a reader.
WRITER_ONLY_ALLOW: dict[str, str] = {
    # Computed-at-write-time alongside tier_used. tier_intended IS read
    # implicitly: when readers compare verdicts, they reach tier_intended
    # through the higher-level cascade.tier_divergent boolean (computed
    # at write time as `tier_used != tier_intended`). The field itself
    # is exposed for ad-hoc forensics + future direct comparisons.
    "cascade.tier_intended":
        "Forensic-only; tier_divergent is the computed comparison the readers use",
    # Phase 4 holdout-cohort A/B label. Written today; the Phase 4
    # tuner-to-be is the documented consumer (spec § 2.3 Component 5).
    "cascade.variant":
        "Phase 4 calibration tag; consumer ships with the tuner",
    # Prefixed form mirrored to bare `cascade.escalation_event` (which
    # IS read by pressure_watchdog). The prefixed form preserves the
    # shadow-vs-live distinction for Phase 3 cutover analytics — both
    # written by design (PR #1664), only the bare form is consumed
    # operationally.
    "cascade.shadow_verdict.escalation_event":
        "Mirror of cascade.escalation_event for shadow/live forensics",
}

# Reader keys that don't need a writer. (Should be near-empty —
# reading a never-written key is a clear bug shape.)
READER_ONLY_ALLOW: dict[str, str] = {
}

# Prefixes the controller writes with dynamic keys (the suffix is
# determined at runtime). Treat them as "any key under this prefix".
# E.g., `cascade.struggle.features.X` is written per feature name.
DYNAMIC_KEY_PREFIXES: tuple[str, ...] = (
    "cascade.struggle.features.",
    "cascade.struggle.raw.",
)


# ── Extractors ──────────────────────────────────────────────────────────────


_WRITER_RE = re.compile(
    # Match `attributes["cascade.foo"] = ...` and also string-literal
    # cascade.* values inside object literals: `"cascade.foo": ...`.
    r'["\']cascade\.[a-zA-Z0-9_.]+["\']',
)


def _extract_writer_keys() -> set[str]:
    """Plugin-TS source → set of cascade.* attribute keys it writes.

    Single file scope: `CascadeTelemetry.ts` is the only place that
    constructs the per-turn span attribute bag.  Other plugin files
    referencing `cascade.*` strings (e.g. comments) are intentionally
    excluded — they'd be too noisy to grep over.
    """
    src = _PLUGIN_SRC / "observer" / "CascadeTelemetry.ts"
    text = src.read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in text.splitlines():
        # Skip pure-comment lines + JSDoc bodies.
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for m in _WRITER_RE.findall(line):
            keys.add(m.strip('"\''))
    return keys


_READER_RE = re.compile(
    r'attrs(?:\w*)?\.get\(\s*["\'](cascade\.[a-zA-Z0-9_.]+)["\']',
)


def _extract_reader_keys() -> set[str]:
    """Walk analyzer + admin Python; return set of cascade.* keys read
    via `attrs.get(...)`. Comments + tests excluded."""
    keys: set[str] = set()
    for root in (_ANALYZER, _ADMIN):
        for py in root.rglob("*.py"):
            # Skip tests + pycache + the guard itself.
            sp = str(py)
            if "/tests/" in sp or "/__pycache__/" in sp:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for m in _READER_RE.findall(line):
                    keys.add(m)
    return keys


def _matches_dynamic_prefix(key: str) -> bool:
    return any(key.startswith(p) for p in DYNAMIC_KEY_PREFIXES)


# ── The contract checks ─────────────────────────────────────────────────────


def test_every_cascade_writer_key_has_a_reader():
    """For every `attributes["cascade.X"]` write in CascadeTelemetry.ts,
    require at least one `attrs.get("cascade.X")` somewhere in
    packages/analyzer or packages/admin (excluding tests).

    If you add a new attribute write without a reader, this test fires.
    Two ways to fix:
      1. Add the reader (preferred — that's the contract).
      2. If the attribute is genuinely write-only for forensics, add
         it to WRITER_ONLY_ALLOW with a comment.

    Do NOT silently delete the test or the attribute — both lose
    information.
    """
    writers = _extract_writer_keys()
    readers = _extract_reader_keys()

    orphan: list[str] = []
    for w in writers:
        if w in WRITER_ONLY_ALLOW:
            continue
        if _matches_dynamic_prefix(w):
            continue
        if w not in readers:
            orphan.append(w)

    if orphan:
        msg = (
            "Writer-only cascade attributes (written by the plugin but "
            "read by no analyzer/admin module). Either wire a consumer "
            "or add to WRITER_ONLY_ALLOW with a comment:\n  "
            + "\n  ".join(sorted(orphan))
            + "\n\nThis is the silent-cross-system-contract bug class "
            "the cascade work hit repeatedly. See test docstring."
        )
        raise AssertionError(msg)


def test_every_cascade_reader_key_has_a_writer():
    """Inverse direction: a reader that reads a never-written key is
    dead code (and may be carrying a typo).
    """
    writers = _extract_writer_keys()
    readers = _extract_reader_keys()

    orphan: list[str] = []
    for r in readers:
        if r in READER_ONLY_ALLOW:
            continue
        if _matches_dynamic_prefix(r):
            continue
        if r not in writers:
            orphan.append(r)

    if orphan:
        msg = (
            "Reader-only cascade attributes (read by analyzer/admin but "
            "never written by the plugin). Likely a typo in the reader "
            "or a writer that got renamed without updating consumers:\n  "
            + "\n  ".join(sorted(orphan))
        )
        raise AssertionError(msg)


# ── Meta-test: confirm extractors actually find data ────────────────────────


def test_extractors_find_canonical_known_attributes():
    """Sanity check the extractors. A few canonical keys MUST appear in
    both sets — if they don't, the regex broke and the contract
    checks would silently pass against empty sets."""
    writers = _extract_writer_keys()
    readers = _extract_reader_keys()
    for canonical in ("cascade.tier_used", "cascade.tier_chosen_by"):
        assert canonical in writers, (
            f"Writer extractor didn't find {canonical!r} — regex broken? "
            f"Found {len(writers)} writer keys."
        )
        assert canonical in readers, (
            f"Reader extractor didn't find {canonical!r}. Found "
            f"{len(readers)} reader keys."
        )
