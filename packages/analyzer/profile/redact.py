"""profile.redact — Redaction patterns enforced on profile writes.

Spec §6.4. Any inferred profile field that contains a credential-like
pattern, secret, or third-party personal info (by name) is rejected
before the write reaches disk. This is the ``never_writes_secrets``
invariant from the Profile Inferrer charter.

Implementation reuses the Security Warden's credential patterns; adds a
light-touch third-party-name heuristic (capitalized tokens that aren't
in a small safe-word list).
"""

from __future__ import annotations

import re
from typing import Callable

from generators.security_warden import redact as warden_redact


# ─────────────────────────────────────────────────────────────────────────────
# Safety check
# ─────────────────────────────────────────────────────────────────────────────


# Words that commonly appear capitalized in benign prose and aren't names.
_SAFE_CAPITALIZED: frozenset[str] = frozenset(
    {
        "I",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "AM",
        "PM",
        "USA",
        "US",
        "UK",
        "EU",
        "HTTP",
        "API",
        "AI",
        "GPT",
    }
)


_NAME_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b")


def is_safe(text: str, *, reject_names: bool = True) -> bool:
    """True if ``text`` is safe to write to a profile.

    Rules:
      - No credential-like patterns (via Security Warden's redact).
      - If ``reject_names`` is True, no two-or-more-token capitalized
        sequences that look like proper names (best-effort; the first
        token may coincidentally be sentence-initial; we accept the false
        positive cost to favor safety).
    """
    if not warden_redact.is_safe(text):
        return False

    if reject_names:
        for m in _NAME_PATTERN.finditer(text):
            tokens = m.group(0).split()
            # Skip if all tokens are in the safe list
            if all(t in _SAFE_CAPITALIZED for t in tokens):
                continue
            return False

    return True


def unsafe_reason(text: str) -> str:
    """Return a short human-readable reason ``text`` was rejected."""
    if not warden_redact.is_safe(text):
        return "contains credential-like pattern"
    for m in _NAME_PATTERN.finditer(text):
        tokens = m.group(0).split()
        if not all(t in _SAFE_CAPITALIZED for t in tokens):
            return f"contains proper-name pattern: {m.group(0)!r}"
    return ""
