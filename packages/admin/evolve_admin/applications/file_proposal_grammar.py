"""evo file-proposal command grammar.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9
(bot-side awareness for in-situ app repair, Channel B).

When a non-evo bot's repair conversation produces "user said yes, file
this for review", the bot routes through the cross-bot ``evo`` keyword
mechanism to land a Proposal in the operator's queue:

    evo file-proposal --on-behalf-of <bot_id> --app <app_id>
                      --action <action_kind> --content <json>

The grammar is intentionally flag-based (rather than positional) so the
bot's LLM can emit it deterministically — flags self-document their
slots and the parser can give a precise error when one is missing or
malformed. The parser is JSON-aware so a ``--content {"k":"v"}``
payload doesn't need shell-style escaping — the bot LLM emits a raw
JSON object inline.

This module is pure (no I/O, no subprocess, no LLM); the handler in
``evolve_admin/evo/handlers/file_proposal.py`` owns the side effects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


KIND_FILE_PROPOSAL = "file_proposal"
KIND_USAGE_ERROR = "usage_error"


# Valid id pattern (mirrors app_changes_grammar.py): letters, digits,
# dash, underscore. Used for bot_id + app_id sanity.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


# Action vocabulary aligned with
# ``analyzer.app_repair_proposals.VALID_ACTIONS``. Don't reach across
# packages for the actual set — keep this list explicit so the parser
# stays usable without an analyzer import cycle. Mismatches between
# the two sets are caught by ``tests/test_file_proposal_grammar.py``.
_VALID_ACTIONS = frozenset({
    "propose_field_edit",
    "propose_file_edit",
    "propose_test_exemption",
    "mark_resolved",
    "done",
})


_REQUIRED_FLAGS = ("on-behalf-of", "app", "action", "content")


# Matches `--flag-name` at a token boundary. We accept `--flag value`
# and `--flag=value` shapes; the captured group gives the flag name
# (without leading dashes).
_FLAG_RE = re.compile(r"--([a-z][a-z0-9\-]*)(?:=|\b)")


@dataclass
class FileProposalCommand:
    """Parsed `evo file-proposal` command tuple."""
    kind: str
    on_behalf_of: str = ""     # bot_id the proposal is filed under
    app_id: str = ""
    action: str = ""           # one of _VALID_ACTIONS
    content: dict = None       # type: ignore[assignment]
    usage_error: str = ""

    def __post_init__(self) -> None:
        if self.content is None:
            self.content = {}

    def is_error(self) -> bool:
        return self.kind == KIND_USAGE_ERROR


def parse_file_proposal(args: str) -> FileProposalCommand:
    """Parse `evo file-proposal --on-behalf-of X --app Y --action Z --content {...}`.

    JSON-aware: ``--content`` may be a bare JSON object inline (no
    shell-style quotes required around it). The parser advances flag
    by flag, peeling off the value as everything up to the next
    ``--flag`` token. For ``--content`` specifically we balance braces
    + strings so a flag-looking substring INSIDE the JSON doesn't
    fool us. Single and double quotes wrapping the value are stripped.

    Returns either a complete ``FileProposalCommand`` or one whose
    ``kind`` is ``KIND_USAGE_ERROR`` with a human-readable
    ``usage_error``. Never raises.
    """
    raw = (args or "").strip()
    if not raw:
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=_usage_error("missing required flags"),
        )

    fields: dict[str, str] = {}
    i = 0
    while i < len(raw):
        # Skip whitespace before the next flag.
        while i < len(raw) and raw[i].isspace():
            i += 1
        if i >= len(raw):
            break

        if not raw.startswith("--", i):
            return FileProposalCommand(
                kind=KIND_USAGE_ERROR,
                usage_error=(
                    "unexpected positional token at " f"{raw[i:i+20]!r}; "
                    "all args must use --flag value or --flag=value form"
                ),
            )

        # Find end of flag name.
        j = i + 2
        while j < len(raw) and (raw[j].isalnum() or raw[j] == "-"):
            j += 1
        name = raw[i + 2:j]
        if not name:
            return FileProposalCommand(
                kind=KIND_USAGE_ERROR,
                usage_error="empty flag name",
            )

        # Determine where the value starts (= form vs space form).
        if j < len(raw) and raw[j] == "=":
            value_start = j + 1
        elif j < len(raw) and raw[j].isspace():
            # Skip the space(s).
            value_start = j
            while value_start < len(raw) and raw[value_start].isspace():
                value_start += 1
            if value_start >= len(raw):
                return FileProposalCommand(
                    kind=KIND_USAGE_ERROR,
                    usage_error=f"flag --{name} has no value",
                )
        else:
            # Flag at end of input with no value, OR followed by non-space junk.
            return FileProposalCommand(
                kind=KIND_USAGE_ERROR,
                usage_error=f"flag --{name} has no value",
            )

        value, value_end = _consume_value(raw, value_start,
                                           json_aware=(name == "content"))
        fields[name] = value
        i = value_end

    missing: list[str] = [f"--{f}" for f in _REQUIRED_FLAGS
                          if not fields.get(f)]
    if missing:
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=_usage_error("missing " + ", ".join(missing)),
        )

    bot_id = fields["on-behalf-of"]
    if not _ID_PATTERN.match(bot_id):
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"--on-behalf-of {bot_id!r} doesn't look like a bot id — "
                f"letters, numbers, dash, underscore only."
            ),
        )

    app_id = fields["app"]
    if not _ID_PATTERN.match(app_id):
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"--app {app_id!r} doesn't look like an app id — letters, "
                f"numbers, dash, underscore only."
            ),
        )

    action = fields["action"].strip().lower()
    if action not in _VALID_ACTIONS:
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                f"--action {action!r} is not a recognized repair action; "
                f"valid: {sorted(_VALID_ACTIONS)}"
            ),
        )

    raw_content = fields["content"]
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=f"--content is not valid JSON: {exc}",
        )
    if not isinstance(content, dict):
        return FileProposalCommand(
            kind=KIND_USAGE_ERROR,
            usage_error=(
                "--content must be a JSON object, got "
                f"{type(content).__name__}"
            ),
        )

    return FileProposalCommand(
        kind=KIND_FILE_PROPOSAL,
        on_behalf_of=bot_id,
        app_id=app_id,
        action=action,
        content=content,
    )


def _consume_value(raw: str, start: int, *, json_aware: bool) -> tuple[str, int]:
    """Consume a flag's value starting at ``start``. Returns
    ``(value_text, end_offset)`` where ``end_offset`` is the index just
    past the value.

    Three shapes are recognized:

    * Wrapped in single or double quotes — value runs to the matching
      close quote.
    * ``json_aware=True`` (i.e. --content) AND the value starts with
      ``{`` or ``[`` — we balance braces/brackets respecting JSON
      strings, so a `--flag` inside the JSON doesn't terminate it
      early.
    * Otherwise — value runs up to the next whitespace.
    """
    if start >= len(raw):
        return "", start

    ch = raw[start]
    # Quoted form.
    if ch in ("'", '"'):
        end = raw.find(ch, start + 1)
        if end == -1:
            # Unterminated quote — take rest of string.
            return raw[start + 1:], len(raw)
        return raw[start + 1:end], end + 1

    # JSON object/array form.
    if json_aware and ch in ("{", "["):
        end = _balance_json(raw, start)
        return raw[start:end], end

    # Bare value: up to next whitespace.
    end = start
    while end < len(raw) and not raw[end].isspace():
        end += 1
    return raw[start:end], end


def _balance_json(raw: str, start: int) -> int:
    """Return the index just past the JSON value beginning at
    ``raw[start]``. Walks through {}/[] depth while respecting JSON
    string quoting and escapes. Falls through to end-of-string when
    the JSON is malformed — the json.loads call downstream will then
    produce the actual JSON error message."""
    depth = 0
    i = start
    in_string = False
    escape = False
    while i < len(raw):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return len(raw)


def _usage_error(reason: str) -> str:
    return (
        f"Usage: `evo file-proposal --on-behalf-of <bot_id> --app <app_id> "
        f"--action <kind> --content <json>` ({reason}). Action must be one "
        f"of {sorted(_VALID_ACTIONS)}."
    )
