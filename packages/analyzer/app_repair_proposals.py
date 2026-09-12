"""app_repair_proposals — shared parser for app-repair proposal blocks.

The block format is the same across Channel A (admin-UI chat,
``evolve_admin.applications.repair_chat``) and Channel B (the bot's
own LLM session, ``analyzer.app_repair_prompt``):

    <<<repair_proposal action="ACTION_NAME">>>
    {"key": "value", ...}
    <<<end>>>

Channel A's apply path lives in ``repair_chat.apply_proposal``;
Channel B routes through ``evo file-proposal`` which writes a
Proposal to the arbiter store for operator approval. Both channels
must parse identical block shapes — that's enforced by both importing
``extract_proposals`` from this module.

Design discipline: this module is pure (no I/O, no logging side
effects, no LLM). Keep it deterministic — operators replay the parse
on the chat-log when applying, and a non-deterministic id would break
that.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Tag format. Deliberately verbose so it doesn't collide with anything the
# LLM might produce organically (code fences, markdown headers, etc.). The
# LLM is taught this exact shape in build_app_repair_system_prompt.
_PROPOSAL_OPEN_RE = re.compile(
    r"<<<repair_proposal\s+action=\"([a-z_]+)\">>>",
    re.IGNORECASE,
)
_PROPOSAL_CLOSE = "<<<end>>>"


# The set of actions both channels recognize. Keep aligned with
# ``evolve_admin.applications.repair_chat.VALID_ACTIONS`` — Channel A's
# apply path uses the same names. If we ever add a new action, add it
# here first, then to repair_chat's apply switch.
VALID_ACTIONS: frozenset[str] = frozenset({
    "propose_field_edit",
    "propose_file_edit",
    "propose_test_exemption",
    "mark_resolved",
    "done",
})


@dataclass
class ParsedProposal:
    """One proposal extracted from an LLM response.

    ``id`` is server-assigned and short (8 hex) so it round-trips
    through the apply route without growing the URL. The randomness
    source is :mod:`uuid` so a re-parse of the same response yields
    DIFFERENT ids — callers must persist the parsed proposals (the
    chat log for Channel A, the cross-bot dispatch envelope for
    Channel B) before consulting them by id.
    """
    id: str
    action: str
    payload: dict
    raw: str  # the original block as the LLM emitted it (for log)

    def to_dict(self) -> dict:
        return {"id": self.id, "action": self.action, "payload": self.payload}


def extract_proposals(text: str) -> tuple[str, list[ParsedProposal]]:
    """Strip ``<<<repair_proposal action="X">>>...<<<end>>>`` blocks
    from ``text``. Returns ``(cleaned_text, proposals)``.

    Blocks with unknown actions or malformed JSON are dropped from the
    cleaned text but logged so the caller (or operator) can see the bot
    misbehaved without seeing raw tags.

    Identical output shape to
    ``evolve_admin.applications.repair_chat.extract_proposals`` — both
    channels' apply infrastructure expects the same tuple shape and
    proposal dict layout.
    """
    if not text:
        return "", []

    proposals: list[ParsedProposal] = []
    out_parts: list[str] = []
    cursor = 0

    while True:
        m = _PROPOSAL_OPEN_RE.search(text, cursor)
        if not m:
            out_parts.append(text[cursor:])
            break

        out_parts.append(text[cursor:m.start()])
        close_at = text.find(_PROPOSAL_CLOSE, m.end())
        if close_at == -1:
            # Unterminated block — emit the tail as text so the
            # operator notices the bot misbehaved.
            out_parts.append(text[m.start():])
            break

        action = m.group(1).strip().lower()
        body = text[m.end():close_at].strip()
        cursor = close_at + len(_PROPOSAL_CLOSE)

        if action not in VALID_ACTIONS:
            log.warning(
                "app_repair_proposals: dropping proposal with unknown "
                "action %r", action,
            )
            continue

        try:
            payload: Any = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            log.warning(
                "app_repair_proposals: malformed proposal JSON (%s): %s",
                action, exc,
            )
            continue
        if not isinstance(payload, dict):
            log.warning(
                "app_repair_proposals: proposal payload not an object: %r",
                payload,
            )
            continue

        pid = uuid.uuid4().hex[:8]
        proposals.append(ParsedProposal(
            id=pid, action=action, payload=payload,
            raw=text[m.start():cursor],
        ))

    cleaned = "".join(out_parts).strip()
    return cleaned, proposals


def render_proposal_block(action: str, payload: dict) -> str:
    """Inverse of :func:`extract_proposals` — render a payload dict as
    a tagged block. Useful for tests that round-trip a known payload
    through the parser, and for any future call site that wants to
    construct a block deterministically.

    Raises ValueError on unknown action so the caller doesn't ship a
    block the parser will silently drop.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"unknown app-repair action {action!r}; valid actions: "
            f"{sorted(VALID_ACTIONS)}"
        )
    body = json.dumps(payload, sort_keys=True)
    return (
        f'<<<repair_proposal action="{action}">>>\n{body}\n{_PROPOSAL_CLOSE}'
    )
