"""evo app-changes / app-coherence / app-scan command grammar — PR 13.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.4.

Pure grammar + dispatch model. Parses the token stream from the evo
handler and returns a structured ``Command`` for the handler to
execute. Keeping the parser pure and the handler thin makes the
grammar testable without exercising the whole evo dispatch stack.

Grammar (spec §10.4):

  evo app-changes                          # list this bot's apps with changes
  evo app-changes <app>                    # walk user through one app's changes
  evo app-changes <app> approve            # approve all current changes
  evo app-changes <app> promote            # promote current state to authored
  evo app-changes <app> flag <description> # note as Proposal for operator
  evo app-coherence <app>                  # run Pass A + C3 capability check
  evo app-scan <bot>                       # immediate Tier 1 scan (§7.6.6)

Reserved sub-tokens after ``<app>``: ``approve``, ``promote``, ``flag``.
If a sub-token isn't recognized, that's a usage error (returned to
the user as ``Command.usage_error``) — better than dropping the
arguments silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Command kinds ────────────────────────────────────────────────────

KIND_LIST_CHANGES        = "list_changes"
KIND_SHOW_CHANGES        = "show_changes"
KIND_APPROVE_CHANGES     = "approve_changes"
KIND_PROMOTE_CHANGES     = "promote_changes"
KIND_FLAG_CHANGE         = "flag_change"
KIND_RUN_COHERENCE       = "run_coherence"
KIND_RUN_SCAN            = "run_scan"
KIND_HELP                = "help"
KIND_USAGE_ERROR         = "usage_error"


# Valid app-id pattern. Mirrors the manifest's id rules — letters,
# numbers, dash, underscore. Used to reject obvious junk early.
_APP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


@dataclass
class Command:
    """Parsed command tuple."""
    kind: str
    app_id: str = ""
    bot_id: str = ""
    flag_description: str = ""
    usage_error: str = ""

    def is_error(self) -> bool:
        return self.kind == KIND_USAGE_ERROR


def _looks_like_app_id(token: str) -> bool:
    return bool(_APP_ID_PATTERN.match(token))


# ── Parsers ─────────────────────────────────────────────────────────

def parse_app_changes(args: str) -> Command:
    """Parse ``evo app-changes`` tail arguments.

    Returns ``Command`` with one of the KIND_LIST/SHOW/APPROVE/PROMOTE/
    FLAG kinds, or KIND_USAGE_ERROR with a human-readable message.
    """
    tokens = [t for t in (args or "").strip().split() if t]

    if not tokens:
        return Command(kind=KIND_LIST_CHANGES)

    app_id = tokens[0]
    if not _looks_like_app_id(app_id):
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"app id {app_id!r} doesn't look right — letters, "
                f"numbers, dash, underscore only."
            ),
        )

    if len(tokens) == 1:
        return Command(kind=KIND_SHOW_CHANGES, app_id=app_id)

    sub = tokens[1].lower()
    rest = tokens[2:]

    if sub == "approve":
        if rest:
            return Command(
                kind=KIND_USAGE_ERROR,
                app_id=app_id,
                usage_error=(
                    f"`evo app-changes {app_id} approve` takes no "
                    f"additional arguments."
                ),
            )
        return Command(kind=KIND_APPROVE_CHANGES, app_id=app_id)

    if sub == "promote":
        if rest:
            return Command(
                kind=KIND_USAGE_ERROR,
                app_id=app_id,
                usage_error=(
                    f"`evo app-changes {app_id} promote` takes no "
                    f"additional arguments."
                ),
            )
        return Command(kind=KIND_PROMOTE_CHANGES, app_id=app_id)

    if sub == "flag":
        if not rest:
            return Command(
                kind=KIND_USAGE_ERROR,
                app_id=app_id,
                usage_error=(
                    f"`evo app-changes {app_id} flag <description>` — "
                    f"add a description of your concern."
                ),
            )
        return Command(
            kind=KIND_FLAG_CHANGE,
            app_id=app_id,
            flag_description=" ".join(rest).strip(),
        )

    return Command(
        kind=KIND_USAGE_ERROR,
        app_id=app_id,
        usage_error=(
            f"unknown sub-command {sub!r} — try `approve`, `promote`, "
            f"or `flag <description>`."
        ),
    )


def parse_app_coherence(args: str) -> Command:
    """Parse ``evo app-coherence <app>``."""
    tokens = [t for t in (args or "").strip().split() if t]
    if not tokens:
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                "Usage: `evo app-coherence <app>` — names the app to "
                "run the coherence capability check against."
            ),
        )
    if len(tokens) > 1:
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                "Usage: `evo app-coherence <app>` — exactly one app id."
            ),
        )
    app_id = tokens[0]
    if not _looks_like_app_id(app_id):
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"app id {app_id!r} doesn't look right — letters, "
                f"numbers, dash, underscore only."
            ),
        )
    return Command(kind=KIND_RUN_COHERENCE, app_id=app_id)


def parse_app_scan(args: str, *, default_bot_id: str = "") -> Command:
    """Parse ``evo app-scan [<bot>]``.

    When ``<bot>`` is omitted, defaults to the bot the command is
    running on (``default_bot_id``) — that's the conversational
    convenience case ("scan me").
    """
    tokens = [t for t in (args or "").strip().split() if t]
    if not tokens:
        if not default_bot_id:
            return Command(
                kind=KIND_USAGE_ERROR,
                usage_error="Usage: `evo app-scan <bot>` — names the bot to scan.",
            )
        return Command(kind=KIND_RUN_SCAN, bot_id=default_bot_id)
    if len(tokens) > 1:
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error="Usage: `evo app-scan <bot>` — exactly one bot id.",
        )
    bot_id = tokens[0]
    if not _looks_like_app_id(bot_id):
        return Command(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"bot id {bot_id!r} doesn't look right — letters, "
                f"numbers, dash, underscore only."
            ),
        )
    return Command(kind=KIND_RUN_SCAN, bot_id=bot_id)


# ── Natural-language intent matchers (spec §10.9.4) ────────────────

# Spec §10.9.4: beyond explicit `evo app-repair` and `evo app-changes`
# commands, the bot should recognize natural-language equivalents as
# repair intents. The handler dispatches through these matchers
# AFTER the explicit-grammar parsers fail.

_NL_REPAIR_PATTERNS = (
    re.compile(r"\bfix\s+(?P<app>[A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"\brepair\s+(?P<app>[A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"reinstall\s+(?P<app>[A-Za-z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"(?P<app>[A-Za-z0-9_\-]+)\s+(?:is\s+)?(?:broken|stuck|not\s+working)",
                re.IGNORECASE),
)

_NL_APPROVE_PATTERNS = (
    re.compile(r"\b(?:approve|accept)\s+(?:changes\s+)?(?:for\s+|to\s+)?(?P<app>[A-Za-z0-9_\-]+)",
                re.IGNORECASE),
    re.compile(r"(?P<app>[A-Za-z0-9_\-]+)\s+(?:looks?\s+)?good", re.IGNORECASE),
)

_NL_LIST_PATTERNS = (
    re.compile(r"what(?:'s|\s+is)?\s+(?:changed|new)\s+(?:on|with)?\s+(?:my\s+)?apps?",
                re.IGNORECASE),
    re.compile(r"\bshow\s+(?:me\s+)?(?:app|application)\s+changes", re.IGNORECASE),
)


@dataclass
class NaturalLanguageIntent:
    """Best-effort intent recognition. The handler may use this to
    suggest the equivalent command but should NOT silently execute —
    the bot's prompt-side wiring handles ambiguity better.
    """
    kind: str                       # KIND_* constant
    app_id: str = ""
    confidence: float = 0.0


def detect_natural_language_intent(
    text: str,
) -> NaturalLanguageIntent | None:
    """Return the most likely intent and the inferred app id.

    Confidence is a coarse signal — 0.9 for explicit verb matches,
    0.6 for inference patterns ("foo is broken"). Returns None when
    nothing matches.
    """
    if not text or not text.strip():
        return None
    txt = text.strip()

    # Highest-priority verbs first.
    for pat in _NL_REPAIR_PATTERNS[:3]:   # explicit verbs
        m = pat.search(txt)
        if m:
            app = m.group("app")
            if _looks_like_app_id(app):
                return NaturalLanguageIntent(
                    kind="repair", app_id=app, confidence=0.9,
                )

    # Inferred patterns ("X is broken").
    m = _NL_REPAIR_PATTERNS[3].search(txt)
    if m:
        app = m.group("app")
        if _looks_like_app_id(app) and app.lower() not in {"it", "this",
                                                             "everything"}:
            return NaturalLanguageIntent(
                kind="repair", app_id=app, confidence=0.6,
            )

    for pat in _NL_APPROVE_PATTERNS:
        m = pat.search(txt)
        if m:
            app = m.group("app")
            if _looks_like_app_id(app):
                return NaturalLanguageIntent(
                    kind="approve", app_id=app, confidence=0.7,
                )

    for pat in _NL_LIST_PATTERNS:
        if pat.search(txt):
            return NaturalLanguageIntent(
                kind="list", confidence=0.8,
            )

    return None
