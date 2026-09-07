"""``evo improve`` / ``/improve`` — conversational front door for issue reporting.

Phase 0b of the Issue Inbox project. Instead of forcing the operator to
type ``evo bug`` or fill out a form, this handler lets them say what
they want to make better in natural language, classifies the concern
into one of four categories, and either:

  - solves the problem in chat (``local_env``), or
  - captures an intake pre-filled with a classifier-drafted body that
    the operator can review and post via ``evo intake promote <id>``.

See ``internal/spec-issue-inbox-2026-05-22.md`` for the design rationale.

This module does NOT auto-post. Filing GitHub issues is gated on
explicit operator approval via the existing intake-promote flow —
matches the spec's "evo solves before it files" + "approve and post"
contract.

The classifier itself lives in ``evolve_admin.intake.classifier`` — see
that module for the prompt, the test seam, and the Verdict shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso

from ..identity import Role
from ._shared import speak
from ...intake import classifier as _classifier
from ...intake import diagnostics as _diag
from ...intake import envelope as _env
from ...intake import promote as _promote
from ...intake import store as _store


# ─── Public entry points ────────────────────────────────────────────────────


def render_improve(
    *,
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
    reported_from: str | None = None,
):
    """Entry point for ``evo improve <description>`` / ``/improve <description>``.

    With no description, asks the operator to say what they'd like to
    improve. With a description, classifies it and either responds with
    in-chat help (``local_env``) or captures an intake with a drafted
    body (other categories) and returns a preview + Approve hint.

    ``reported_from`` is the originating page when the chat came from a
    drawer — passed through to the classifier as a routing signal.
    """
    body = (args or "").strip()
    if not body:
        return speak(
            "improve",
            (
                "**Make Evolve better**\n\n"
                "Tell me what isn't working, what's confusing, or what "
                "you wish it did — in your own words. I'll try to fix it "
                "in this chat if I can. If it really is a bug or feature "
                "request, I'll draft an issue for you to review and post.\n\n"
                "_(Examples: \"team_bot_a's gateway keeps crashing\", \"the alerts "
                "page is showing yesterday's incidents as if they're still "
                "firing\", \"I wish I could filter the cost view by bot\")_"
            ),
            role,
        )

    shared_dir = _shared_dir(network)

    # Phase 0c: run the investigation pass before classifying so the
    # model can weigh real evidence (matching upstream issues, recent
    # firing signals, recent commits in the implicated code area) on
    # top of the operator's narrative. Best-effort and time-bounded;
    # empty evidence falls through to the operator-text-only path.
    cfg = _promote.PromotionConfig.from_network(network)
    repos_to_search: tuple[str, ...] = ()
    if cfg is not None:
        repos_to_search = tuple(
            f"{t.owner}/{t.repo}" for t in cfg.targets
        )
    evidence = _diag.gather_diagnostics(
        body,
        context=_diag.DiagnosticContext(
            shared_dir=shared_dir,
            repos_to_search=repos_to_search,
            reported_from=reported_from,
        ),
    )

    ctx = _build_context(
        network=network, bot_id=bot_id, reported_from=reported_from,
        diagnostic_evidence=evidence,
    )
    verdict = _classifier.classify_issue(body, context=ctx)

    if verdict.category == "local_env":
        return _render_local_env(verdict, role)
    if verdict.confidence < 0.5:
        # Don't capture an intake on a guess — too easy to file noise.
        return _render_low_confidence(verdict, body, role)

    # evolve_code / upstream / mixed: capture an intake, show draft + Approve.
    kind = _kind_from_category(verdict.category, body)
    intake = _capture_intake(
        body=verdict.draft_body or body,
        kind=kind,
        network=network,
        bot_id=bot_id,
        shared_dir=shared_dir,
        role=role,
    )
    if intake is None:
        return speak(
            "improve",
            (
                "**Make Evolve better**\n\n"
                "I drafted a response but couldn't save it locally. Try "
                "again, or capture it directly with `evo bug \"<description>\"`."
            ),
            role,
        )

    return _render_draft_preview(verdict, intake, role)


# ─── Category-specific renderers ────────────────────────────────────────────


def _render_local_env(verdict: _classifier.Verdict, role: Role):
    """Show the operator the classifier's in-chat help and offer to
    escalate to a filed issue if it turns out the local-env diagnosis
    was wrong."""
    help_text = verdict.in_chat_help.strip() or (
        "I think this is something you can fix on your end, but I'm "
        "not sure exactly what. Tell me more about what you're seeing?"
    )
    reasoning = verdict.reasoning.strip()
    parts = ["**Make Evolve better**", "", help_text]
    if reasoning:
        parts += ["", f"_Why I think this is local: {reasoning}_"]
    parts += [
        "",
        "If that turns out to be wrong — if Evolve or OpenClaw really "
        "should be doing something different — say so and I'll draft an "
        "issue instead.",
    ]
    return speak("improve", "\n".join(parts), role)


def _render_low_confidence(verdict: _classifier.Verdict, original: str, role: Role):
    """The classifier wasn't sure. Ask the user to clarify rather than
    pick a category at random."""
    reasoning = verdict.reasoning.strip()
    parts = [
        "**Make Evolve better**",
        "",
        "I'm not sure what category this fits into. A bit more detail "
        "would help me give you the right kind of help:",
        "",
        "- What were you doing when you noticed this?",
        "- Is this a one-time thing or does it keep happening?",
        "- What did you expect to happen instead?",
    ]
    if reasoning:
        parts += ["", f"_Tentative read: {reasoning}_"]
    return speak("improve", "\n".join(parts), role)


def _render_draft_preview(
    verdict: _classifier.Verdict,
    intake: _env.Intake,
    role: Role,
):
    """Show the operator the drafted intake and how to post it.

    Doesn't auto-post — the operator must explicitly invoke
    `evo intake promote <id>` (optionally with `--to <target>`) to file.
    """
    target_hint = verdict.target_name or "default target"
    promote_cmd = f"evo intake promote {intake.id}"
    if verdict.target_name:
        promote_cmd += f" --to {verdict.target_name}"

    category_label = {
        "evolve_code": "Evolve codebase",
        "upstream":    "upstream (OpenClaw / dependency)",
        "mixed":       "Evolve + upstream (mixed)",
    }.get(verdict.category, verdict.category)

    body_preview = _truncate(verdict.draft_body, 600)
    reasoning = verdict.reasoning.strip()

    parts = [
        f"**Drafted** — `{intake.id}` ({intake.kind})",
        "",
        f"I read this as **{category_label}**, routed to **{target_hint}**.",
    ]
    if reasoning:
        parts.append(f"_Why: {reasoning}_")
    parts += [
        "",
        "**Proposed issue body:**",
        "",
        f"> {body_preview.replace(chr(10), chr(10) + '> ')}",
        "",
        f"To file it as-is: `{promote_cmd}`",
        "To skim the queue first: `evo intake list`",
        "If the draft is off, edit by re-running `evo improve` with more "
        "detail — the captured copy stays in your queue either way.",
    ]
    return speak("improve", "\n".join(parts), role)


# ─── Capture ────────────────────────────────────────────────────────────────


def _capture_intake(
    *,
    body: str,
    kind: _env.IntakeKind,
    network: dict[str, Any],
    bot_id: str,
    shared_dir: Path,
    role: Role,
) -> _env.Intake | None:
    """Persist an Intake. Returns None on filesystem failure so the
    caller can surface a non-fatal message."""
    intake = _env.Intake(
        id=_store.new_intake_id(),
        kind=kind,
        body=body,
        context=_env.IntakeContext(
            primary_bot=_primary_bot(network),
            active_bot=bot_id,
            git_commit=_current_git_commit(),
            evolve_version=_current_evolve_version(),
        ),
    )
    try:
        _store.write_intake(intake, shared_dir)
    except (PermissionError, OSError):
        return None
    return intake


def _kind_from_category(category: _classifier.Category, original: str) -> _env.IntakeKind:
    """Pick an intake-kind label for the captured envelope.

    Heuristic: any category other than the explicit local_env path
    defaults to "bug" unless the operator's own words clearly read as
    a feature/request. We don't try to classify bug-vs-feature in the
    LLM verdict — it's a soft label that admins can re-categorize when
    triaging, and bug is the safer default.
    """
    lowered = original.lower()
    feature_markers = (
        "i wish", "could you add", "would be nice if", "feature request",
        "it would help if", "i want", "can we make", "would love",
    )
    if any(m in lowered for m in feature_markers):
        return "feature"
    return "bug"


# ─── Context builder ────────────────────────────────────────────────────────


def _build_context(
    *,
    network: dict[str, Any],
    bot_id: str,
    reported_from: str | None,
    diagnostic_evidence: Any = None,
) -> _classifier.ClassificationContext:
    """Assemble the structured context the classifier sees alongside the
    operator's message."""
    cfg = _promote.PromotionConfig.from_network(network)
    available_targets = tuple(cfg.target_names) if cfg else ()
    return _classifier.ClassificationContext(
        reported_from=reported_from,
        available_targets=available_targets,
        evolve_version=_current_evolve_version(),
        active_bot=bot_id,
        diagnostic_evidence=diagnostic_evidence,
    )


# ─── Revise (Phase 2 of Issue Inbox) ────────────────────────────────────────
#
# `evo revise <intake_id> <instruction>` lets the operator iterate on
# the classifier-drafted body before posting. Updates intake.body +
# intake.triage_notes title is NOT a separate field on Intake — it's the
# first line of the body, so revising the title means rewriting the
# first line. We track the title separately during the reviser call
# (since the classifier produces draft_title) and concatenate on save.


def render_revise(
    *,
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
    reported_from: str | None = None,
):
    """Entry point for ``evo revise <intake_id> <instruction>``.

    Reads the intake by id, calls the LLM reviser with the existing
    title + body + the operator's instruction, and overwrites the
    intake body with the new draft. The prior version goes into
    ``revision_history`` so the operator can audit + undo.

    Filed intakes are rejected — once an issue is posted to GitHub,
    that thread is the source of truth, not the local draft. Operator
    should reply on GitHub instead.
    """
    intake_id, instruction, undo = _parse_revise_args(args)
    if not intake_id:
        return speak(
            "revise",
            (
                "**Revise**\n\n"
                "Usage: `evo revise <intake_id> <instruction>`.\n\n"
                "Examples:\n"
                "- `evo revise intake-20260523-a3f9 make it more concise`\n"
                "- `evo revise intake-20260523-a3f9 add a section about "
                "the FileHandle stack trace`\n"
                "- `evo revise intake-20260523-a3f9 reframe as a feature "
                "request, not a bug`\n\n"
                "To walk back the last revision: "
                "`evo revise <intake_id> --undo`.\n\n"
                "Copy the intake id from `evo intake list`."
            ),
            role,
        )

    shared_dir = _shared_dir(network)
    located = _store.find_intake(shared_dir, intake_id)
    if located is None:
        return speak(
            "revise",
            f"**Revise**\n\nNo intake with id `{intake_id}`. Try "
            "`evo intake list` to see what's in the queue.",
            role,
        )
    intake, _, _ = located

    # State guards apply to BOTH revise and --undo. The local draft is
    # only mutable while the intake is open / triaged. Once filed, the
    # GitHub thread is source of truth; once closed, the operator
    # explicitly retired it.
    if intake.state == "filed":
        url = intake.promotion.github_issue_url or "(unknown)"
        return speak(
            "revise",
            (
                f"**Revise** — `{intake.id}` is already filed at "
                f"{url}.\n\n"
                "The GitHub thread is now the source of truth, not the "
                "local draft. Reply there directly. If you want to "
                "delete what you posted and refile from scratch, close "
                "the GitHub issue first."
            ),
            role,
        )
    if intake.state == "closed":
        return speak(
            "revise",
            f"**Revise** — `{intake.id}` is closed. To re-work it, "
            "capture a fresh intake with `evo improve <description>`.",
            role,
        )

    # Phase 2b.1: `--undo` walks back one revision from revision_history.
    # No LLM call needed — we have the prior body cached on the intake.
    if undo:
        return _do_undo(intake, shared_dir, role)

    if not instruction:
        return speak(
            "revise",
            (
                f"**Revise** — `{intake_id}`\n\n"
                "Tell me how to change it. Examples:\n"
                "- `evo revise " + intake_id + " make it more concise`\n"
                "- `evo revise " + intake_id + " include a reproduction "
                "steps section`\n"
                "- `evo revise " + intake_id + " --undo` to walk back "
                "the last revision"
            ),
            role,
        )

    # Pull the current title out of the body's first line. The intake
    # store doesn't have a separate title field; the promoter pulls
    # the title from the body's first 72 chars (see intake.promote.
    # _make_title). We mirror that here so the reviser sees both as
    # cleanly separated inputs.
    current_title, current_body = _split_title_body(intake.body)

    ctx = _build_context(
        network=network, bot_id=bot_id, reported_from=reported_from,
        diagnostic_evidence=None,
    )
    verdict = _classifier.revise_draft(
        current_title=current_title,
        current_body=current_body,
        instruction=instruction,
        context=ctx,
    )

    if verdict.confidence < 0.5:
        # The reviser couldn't confidently execute. Don't silently
        # mutate the draft — ask the operator to clarify.
        reasoning = verdict.reasoning.strip()
        parts = [
            f"**Revise** — `{intake.id}`",
            "",
            "I'm not sure how to apply that. A clearer instruction "
            "would help — e.g. \"make the title mention the "
            "FileHandle bug specifically\" or \"remove the section "
            "about Node downgrade\".",
        ]
        if reasoning:
            parts += ["", f"_What I tried to make of it: {reasoning}_"]
        return speak("revise", "\n".join(parts), role)

    # Record the prior version BEFORE mutating, so revision_history
    # captures what was overwritten — and the operator can read
    # `evo intake show <id>` later to audit / undo.
    intake.revision_history.append({
        "at": _utc_now_iso(),
        "instruction": instruction,
        "prior_title": current_title,
        "prior_body": current_body,
        "reasoning": verdict.reasoning,
    })

    # Concatenate the new title + body. Empty body is allowed for the
    # reviser (rare — would mean the instruction asked to clear the
    # body) but Intake's __post_init__ rejects empty body on construct,
    # so re-validate by going through write_intake which doesn't.
    #
    # Defense-in-depth: if the reviser returned an empty new_title,
    # preserve the original. The classifier coerces this for the live
    # Anthropic path, but a test stub or future reviser implementation
    # might not — and silently blanking the title would be worse than
    # any other failure mode here.
    new_title_clean = (verdict.new_title or "").strip()
    new_body_clean = (verdict.new_body or "").strip()
    # A reviser returning BOTH fields empty is a refusal — the operator's
    # instruction couldn't be applied. Revert the optimistic
    # revision_history append and surface the failure. We check the raw
    # verdict fields (not the post-fallback `final_title`) so the title
    # fallback can't mask a genuine "I have nothing to say" response.
    if not new_title_clean and not new_body_clean:
        intake.revision_history.pop()
        return speak(
            "revise",
            (
                f"**Revise** — `{intake.id}`\n\n"
                "The reviser came back with an empty draft. Try a more "
                "specific instruction, or `evo improve` to capture a "
                "fresh intake from scratch."
            ),
            role,
        )
    final_title = new_title_clean or current_title
    new_full = _join_title_body(final_title, new_body_clean)
    intake.body = new_full

    try:
        _store.write_intake(intake, shared_dir)
    except (PermissionError, OSError) as e:
        # Revert the revision_history append since we couldn't persist.
        intake.revision_history.pop()
        return speak(
            "revise",
            f"**Revise** — couldn't save the revision: {e}",
            role,
        )

    return _render_revise_preview(intake, verdict, role)


def _render_revise_preview(intake, verdict, role: Role):
    """Operator-facing diff-ish preview of the revision: short head,
    quoted new body, what changed, and the promote command to ship it."""
    promote_cmd = f"evo intake promote {intake.id}"
    reasoning = verdict.reasoning.strip()
    body_preview = _truncate(verdict.new_body, 600)

    parts = [
        f"**Revised** — `{intake.id}` (revision #{len(intake.revision_history)})",
        "",
    ]
    if reasoning:
        parts += [f"_{reasoning}_", ""]
    parts += [
        "**New draft:**",
        "",
        f"> **{verdict.new_title.strip() or '(no title)'}**",
        "",
        f"> {body_preview.replace(chr(10), chr(10) + '> ')}",
        "",
        f"To file it: `{promote_cmd}`",
        "To revise again: `evo revise " + intake.id + " <next instruction>`",
        "Prior versions are in the intake's revision_history (audit + "
        "future undo support).",
    ]
    return speak("revise", "\n".join(parts), role)


def _parse_revise_args(args: str) -> tuple[str, str, bool]:
    """Parse ``<intake_id> [<instruction> | --undo]``.

    Returns ``(intake_id, instruction, undo_flag)``. The id is always
    the FIRST non-flag token (operators can't use intake-style ids in
    their instructions, and the first-token rule is easy to explain).

    ``--undo`` can appear anywhere after the id and consumes the entire
    remaining args — operator can't combine ``--undo`` with an
    instruction; the undo path is its own thing.
    """
    raw = (args or "").strip()
    if not raw:
        return "", "", False
    tokens = raw.split()

    # Look for --undo flag anywhere in the args.
    undo = False
    cleaned: list[str] = []
    for tok in tokens:
        if tok == "--undo":
            undo = True
            continue
        cleaned.append(tok)

    if not cleaned:
        return "", "", undo
    intake_id = cleaned[0].strip()
    instruction = " ".join(cleaned[1:]).strip()
    return intake_id, instruction, undo


def _do_undo(intake, shared_dir, role: Role):
    """Pop the most recent revision_history entry and restore it.

    Walks one step back: pop the entry, set intake.body to
    ``prior_title + prior_body``, persist. If there's nothing to undo,
    surface a friendly noop.
    """
    if not intake.revision_history:
        return speak(
            "revise",
            (
                f"**Revise** — `{intake.id}`\n\n"
                "Nothing to undo. This intake hasn't been revised since "
                "it was captured. Use `evo revise " + intake.id +
                " <instruction>` to make a first revision."
            ),
            role,
        )

    last = intake.revision_history.pop()
    prior_title = str(last.get("prior_title") or "")
    prior_body = str(last.get("prior_body") or "")
    restored = _join_title_body(prior_title, prior_body)
    if not restored.strip():
        # Defensive: if the prior version was somehow empty too, restore
        # what we popped and bail. Should be impossible — the revise
        # handler rejects empty-output before appending — but worth
        # being explicit about.
        intake.revision_history.append(last)
        return speak(
            "revise",
            (
                f"**Revise** — `{intake.id}`\n\n"
                "Couldn't undo: the prior revision was empty. "
                "Intake's revision_history is inconsistent; "
                "use `evo intake show " + intake.id + "` to inspect."
            ),
            role,
        )
    intake.body = restored

    try:
        _store.write_intake(intake, shared_dir)
    except (PermissionError, OSError) as e:
        # Roll back the pop so the operator can retry.
        intake.revision_history.append(last)
        return speak(
            "revise",
            f"**Revise** — couldn't save the undo: {e}",
            role,
        )

    remaining = len(intake.revision_history)
    parts = [
        f"**Undone** — `{intake.id}` is back to "
        + ("its captured draft" if remaining == 0
           else f"revision #{remaining}"),
        "",
        "**Restored draft:**",
        "",
        f"> **{prior_title.strip() or '(no title)'}**",
        "",
        f"> {_truncate(prior_body, 500).replace(chr(10), chr(10) + '> ')}",
        "",
    ]
    if remaining > 0:
        parts.append(
            f"To walk back further: `evo revise {intake.id} --undo` again."
        )
    parts.append(
        f"To file this version: `evo intake promote {intake.id}`."
    )
    return speak("revise", "\n".join(parts), role)


def _split_title_body(full_body: str) -> tuple[str, str]:
    """Split an intake body into (title, body).

    Convention (mirroring ``intake.promote._make_title``): the first
    non-empty line is the title; the rest is the body. If there's only
    one line, body becomes empty and the line is the title.
    """
    lines = (full_body or "").split("\n")
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return title, body


def _join_title_body(title: str, body: str) -> str:
    """Inverse of :func:`_split_title_body`."""
    t = (title or "").strip()
    b = (body or "").strip()
    if not t and not b:
        return ""
    if not b:
        return t
    if not t:
        return b
    return f"{t}\n\n{b}"


# ─── Small helpers (copies of the ones in handlers/intake.py) ───────────────
# Kept local rather than imported because the existing module uses
# private-prefix names; duplicating these 30 lines is cheaper than
# making everything public and threading new dependencies.


def _truncate(text: str, n: int) -> str:
    flat = (text or "").strip()
    if len(flat) <= n:
        return flat
    return flat[: n - 1].rstrip() + "…"


def _shared_dir(network: dict[str, Any]) -> Path:
    return Path(network.get("sharedDir", "/Users/Shared/evolve"))


def _primary_bot(network: dict[str, Any]) -> str | None:
    pb = network.get("primary")
    if isinstance(pb, str) and pb.strip():
        return pb.strip()
    pb_bot = network.get("primary_bot")
    if isinstance(pb_bot, dict):
        pid = pb_bot.get("primary_bot_id")
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    return None


def _current_git_commit() -> str | None:
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return out.strip() or None


def _current_evolve_version() -> str | None:
    try:
        from evolve_admin import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return None
