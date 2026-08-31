"""arbiter.appliers.agents_append — Apply an AgentsAppend proposal.

``AgentsAppend(bot_id, section, content)`` is the additive-only sibling of
``SoulEdit(target="agents", operation="append_section")``: it appends one
section to the bot's ``AGENTS.md`` and never replaces or removes anything.

This module is a **thin adapter** over :mod:`arbiter.appliers.soul_edit`.
Path resolution (``_target_path``), the append itself (``_apply_operation``),
the exact-restore snapshot and the revert all live there and are reused
verbatim — the only thing that belongs here is the ``AgentsAppend`` →
``SoulEdit`` mapping and the section-heading rendering rule below.

Heading rule. Both shipping producers (``efficiency_hawk``'s streamline
notes and ``persona_tuner``'s tone notes) already embed their own ``##``
heading at the top of ``content``, while ``section`` carries a shorter
label used in the admin UI. Prepending ``## {section}`` unconditionally
would write a doubled, near-duplicate heading into AGENTS.md on every
apply, so a heading is synthesized only when ``content`` does not already
open with one.

Always human-approval (``eligibility._HUMAN_ONLY_ACTION_KINDS``) — free-text
instructions land in the doc the model reads every turn.

Write path. The applier runs as the ``evolve`` user and ``AGENTS.md`` sits
at ``.openclaw/workspace/AGENTS.md``, outside the ``workspace/evolve/``
write-ACL'd subdir. That write is nonetheless granted: ``deploy.py``'s
``set_evolve_read_acl`` grants ``WORKSPACE_ROOT_WRITE_ACL_PERMS`` on
``workspace/`` (file_inherit, no directory_inherit) plus a retro-grant over
existing ``workspace/`` files. Verified live on the pod: evolve can rewrite
an existing ``AGENTS.md`` and create the file when absent. No /tmp staging
or sudoers grant is needed here (and ``os.access(dir, W_OK)`` reports False
against a working write — read the ACEs, not the mode bits).
"""

from __future__ import annotations

from typing import cast

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from arbiter.appliers.soul_edit import SoulEditApplier
from schema.proposal import Action, AgentsAppend, SoulEdit

# Delegate: reuse the shipped SoulEdit path resolution / append / snapshot /
# revert rather than reimplementing any of it.
_SOUL = SoulEditApplier()


def _render_section(section: str, content: str) -> str:
    """Compose the markdown block appended to AGENTS.md.

    Returns ``content`` untouched when it already opens with a markdown
    heading (the shipping producers embed one); otherwise prefixes
    ``## {section}`` when a section label is present.
    """
    body = content.strip("\n")
    first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
    if first_line.lstrip().startswith("#"):
        return body
    if section.strip():
        return f"## {section.strip()}\n\n{body}" if body else f"## {section.strip()}"
    return body


def _as_soul_edit(action: Action, bot_id: str) -> SoulEdit:
    # Dispatch is by ``Action.kind``, so only AgentsAppend reaches this
    # applier; the parameter is typed ``Action`` to match the protocol.
    append = cast(AgentsAppend, action)
    return SoulEdit(
        bot_id=append.bot_id or bot_id,
        target="agents",
        operation="append_section",
        content=_render_section(append.section, append.content),
    )


class AgentsAppendApplier:
    """Append-only AGENTS.md writer, delegating to the SoulEdit applier."""

    def capture_snapshot(self, action: Action, bot_id: str) -> dict:
        snapshot = _SOUL.capture_snapshot(_as_soul_edit(action, bot_id), bot_id)
        # Stamp our own kind so the stored RevertPlan reads truthfully; the
        # rest of the snapshot shape (path / prior_content / existed_before)
        # is soul_edit's and is what its revert() consumes.
        snapshot["action_kind"] = "AgentsAppend"
        return snapshot

    def apply(self, action: Action, bot_id: str) -> ApplyResult:
        return _SOUL.apply(_as_soul_edit(action, bot_id), bot_id)

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        return _SOUL.revert(snapshot, bot_id)


register_applier("AgentsAppend", AgentsAppendApplier())
