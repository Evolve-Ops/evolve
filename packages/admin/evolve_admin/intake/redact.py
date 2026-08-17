"""Redaction policy for intake → GitHub promotion.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §6.5.

Capture is allowed to be raw (the local intake file is private to the
pod). Promotion to GitHub is the boundary where redaction must apply,
because issue bodies are world-readable on public repos.

Defaults:

  - drop ``submitter.user_key`` and ``submitter.channel``
  - drop ``context.recent_turns_excerpt`` entirely (opt in via
    ``include_transcript=True``)
  - reduce ``context.active_signals`` to a count, never ids
  - keep ``primary_bot``, ``active_bot``, ``git_commit``, ``evolve_version``

Output: ``(rendered_body, redactions_applied)`` — the markdown body to
POST to GitHub, plus the list of redaction tags persisted on
``Intake.promotion.redactions_applied``.
"""

from __future__ import annotations

from .envelope import Intake


def build_issue_body(
    intake: Intake,
    *,
    include_transcript: bool = False,
) -> tuple[str, list[str]]:
    """Render the GitHub issue body for ``intake`` and report what was
    redacted.

    The returned redaction tags name the categories of data that were
    stripped (or, in the case of the transcript, the choice to include
    it). Persist them on the intake envelope so admin can see exactly
    what was sent.
    """
    redactions: list[str] = []

    # Header — mirrors §6.4 of the spec.
    header = (
        f"**Intake** — `{intake.id}` ({intake.kind}, captured "
        f"{intake.created_at})"
    )

    lines: list[str] = [header, "", intake.body.strip(), "", "---", "", "### Context"]

    ctx = intake.context
    bullets: list[str] = []
    if ctx.active_bot:
        bullets.append(f"- Active bot at capture: `{ctx.active_bot}`")
    if ctx.primary_bot:
        bullets.append(f"- Primary bot: `{ctx.primary_bot}`")
    if ctx.git_commit or ctx.evolve_version:
        commit = ctx.git_commit or "(unknown)"
        version = ctx.evolve_version or "(unknown)"
        bullets.append(f"- Pod commit: `{commit}` (evolve {version})")
    if ctx.active_signals:
        bullets.append(
            f"- Firing signals at capture: {len(ctx.active_signals)} (signature-redacted)"
        )
        redactions.append("active_signals")

    if bullets:
        lines.extend(bullets)
    else:
        lines.append("- _(no context captured)_")

    # Submitter — always redact identity.
    if intake.submitter_user_key or intake.submitter_channel:
        redactions.append("submitter")

    # Transcript handling.
    if intake.context.recent_turns_excerpt:
        if include_transcript:
            redactions.append("transcript_included")
            lines.append("")
            lines.append("### Recent turns")
            lines.append("")
            for turn in intake.context.recent_turns_excerpt:
                role = str(turn.get("role") or "?")
                text = str(turn.get("text") or "").strip()
                lines.append(f"- **{role}**: {text}")
        else:
            redactions.append("transcript_dropped")
            lines.append("")
            lines.append(
                "_(Recent transcript redacted by default; admin can re-promote "
                "with `--include-transcript` to attach.)_"
            )

    return "\n".join(lines).strip() + "\n", redactions
