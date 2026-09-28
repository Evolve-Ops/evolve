"""LLM classifier for the conversational issue-reporting flow.

Phase 0b of the Issue Inbox project. When the operator describes a
problem via the ``/improve`` chat surface (or any conversational entry
into the issue flow), this module decides:

  1. Which of four categories the problem falls into — ``local_env``,
     ``evolve_code``, ``upstream``, or ``mixed``.
  2. Which configured intake target to route to (e.g. ``evolve``,
     ``openclaw``).
  3. A draft GitHub issue body the operator can edit before posting.
  4. For ``local_env``: an in-chat help response so evo can try to
     resolve without filing anything.

The classifier is fast-tier — small token budget, cheap, called once
per issue-flow turn (not per conversation turn). Per
``feedback_rsi_low_cost_preference``.

Test seam: :func:`set_classifier` / :func:`get_classifier` mirror the
``evo.wizard.extractor`` and ``evo.wizard.intent`` patterns so unit
tests substitute a deterministic stub instead of hitting the API.

See ``internal/spec-issue-inbox-2026-05-22.md`` for the design rationale
(progressive disclosure, evo-chat-as-front-door, four categories,
classification heuristics).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


# ─── Types ──────────────────────────────────────────────────────────────────

Category = Literal["local_env", "evolve_code", "upstream", "mixed"]
VALID_CATEGORIES: tuple[Category, ...] = ("local_env", "evolve_code", "upstream", "mixed")

DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT_S = 30


@dataclass
class Verdict:
    """The classifier's structured output.

    ``category`` decides routing.
    ``target_name`` names which configured intake target to file
    against; the caller resolves the name against the actual
    ``PromotionConfig``. None means "let the caller pick the default."
    ``draft_body`` is the proposed issue body for categories that file;
    empty for ``local_env``. ``in_chat_help`` is the response evo
    should show the user for ``local_env`` (the "try to fix in chat"
    pass); empty for the filing categories.
    ``confidence`` ∈ [0.0, 1.0] — sub-0.5 means "I'm guessing", and
    the caller should ask the user to clarify before acting.
    ``reasoning`` is a short human-readable explanation of the choice,
    shown to the operator so they can challenge the verdict.
    """

    category: Category
    target_name: str | None = None
    draft_title: str = ""
    draft_body: str = ""
    in_chat_help: str = ""
    confidence: float = 0.0
    reasoning: str = ""


@dataclass
class ClassificationContext:
    """Inputs to the classifier beyond the user's free-form message.

    All fields are optional. The classifier degrades gracefully — when
    a field is absent, it just has less to work with.

    ``reported_from`` is the URL path the user was on when they
    triggered the flow (e.g. ``/alerts``, ``/apps/cve-scan``). Reuses
    the existing page-context-pack infrastructure per Phase 1
    surface-aware help work.

    ``available_targets`` is the list of configured intake-target names
    the caller can route to. The classifier picks a name from this
    list; if it picks one not in the list, the caller falls back to
    None (= default target).

    ``conversation_excerpt`` is the most recent turns of the operator's
    evo conversation, if any. Useful when the user's message
    self-references something said earlier ("evo's reply five minutes
    ago wasn't helpful").

    ``diagnostic_evidence`` is the structured output of the
    investigation pass — matching upstream issues, recent firing
    signals, recent commits in the affected code area. When present,
    the classifier weighs it heavily. See
    :mod:`evolve_admin.intake.diagnostics`.
    """

    reported_from: str | None = None
    available_targets: tuple[str, ...] = ()
    conversation_excerpt: tuple[dict[str, str], ...] = ()
    evolve_version: str | None = None
    openclaw_version: str | None = None
    active_bot: str | None = None
    diagnostic_evidence: Any = None  # diagnostics.DiagnosticEvidence — avoid import cycle


# Classifier function signature — exposed via the test seam.
ClassifierFn = Callable[[str, ClassificationContext], Verdict]


# ─── System prompt ──────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are a triage classifier for the Evolve project's issue-reporting
flow. Your job is to read what the operator says is wrong (or what they
want improved) and decide ONE of four categories:

  - local_env   The Evolve / OpenClaw / plugin code is fine. The user's
                setup is the problem (expired token, misconfig, network
                issue, missing permission, etc.). The fix is something
                they can do — not a code change.

  - evolve_code Reveals a real shortcoming in the Evolve codebase — a
                bug, missing feature, design gap, or detection blind
                spot. Filing against the Evolve repo is appropriate.

  - upstream    Reveals a shortcoming in OpenClaw or a third-party
                dependency. Filing against the upstream repo is
                appropriate.

  - mixed       More than one of the above. Common pattern: a local
                symptom uncovers BOTH a missing Evolve detection AND
                an upstream root cause.

Also produce:

  - target_name: which configured intake target should this be filed
    against? Pick from the available_targets list. Use "evolve" for
    evolve_code or local_env (when filing IS appropriate), "openclaw"
    for upstream. For mixed, name the most relevant single target —
    the caller will decide whether to split.

  - draft_title: short (under 72 chars) issue title in the form
    "[bug] X" or "[feature] X". Required for evolve_code / upstream /
    mixed. Empty for local_env.

  - draft_body: the proposed GitHub issue body for evolve_code /
    upstream / mixed. Markdown. Lead with what the user observed; if
    you have a hypothesis about cause, include it as a separate
    section. Empty for local_env.

  - in_chat_help: for local_env ONLY, a short conversational response
    that helps the user understand the issue and how to fix it. Plain
    text, no markdown headers; 1-3 paragraphs maximum. Empty for the
    filing categories.

  - confidence: 0.0 to 1.0. Use the floor when you're guessing because
    the message is too short or ambiguous. The caller will ask the
    user to clarify if confidence < 0.5.

  - reasoning: one or two sentences explaining the call. Shown to the
    operator so they can challenge it.

If a "# Evidence gathered" section appears below, weigh it heavily:

  - A matching open issue on a target repo (especially a close title
    match) usually means the answer is "this is already filed" — your
    draft_body should reference that issue and add new information
    rather than restating the problem from scratch.
  - A recently-firing signal that matches the symptom is a strong hint
    that this is a known incident; the category may still be
    evolve_code or upstream depending on the producer, but you should
    name the signal in your reasoning.
  - Recent commits in the implicated code area suggest a possible
    regression — flag it explicitly in reasoning and bump confidence.
  - Investigation notes describe what couldn't be gathered (timeout, no
    keywords, no configured repos). Treat empty matching_issues as
    "unknown" rather than "no match" when a note says the gatherer
    couldn't run.

Respond with a single JSON object, no prose around it:

  {"category": "...", "target_name": "...", "draft_title": "...",
   "draft_body": "...", "in_chat_help": "...", "confidence": 0.0,
   "reasoning": "..."}
"""


def _format_user_message(message: str, ctx: ClassificationContext) -> str:
    """Render the classifier's user prompt: operator message + context bullets."""
    parts: list[str] = []
    parts.append("# Operator said\n")
    parts.append(message.strip() or "(empty)")
    parts.append("")

    parts.append("# Context\n")
    bullets: list[str] = []
    if ctx.reported_from:
        bullets.append(f"- Reported from: `{ctx.reported_from}` "
                       "(the page they were on when they triggered the flow)")
    if ctx.available_targets:
        bullets.append(
            f"- Configured intake targets: {', '.join(ctx.available_targets)}"
        )
    if ctx.evolve_version:
        bullets.append(f"- Evolve version: {ctx.evolve_version}")
    if ctx.openclaw_version:
        bullets.append(f"- OpenClaw version: {ctx.openclaw_version}")
    if ctx.active_bot:
        bullets.append(f"- Active bot at trigger: `{ctx.active_bot}`")
    if not bullets:
        bullets.append("- (no extra context available)")
    parts.extend(bullets)

    if ctx.conversation_excerpt:
        parts.append("")
        parts.append("# Recent conversation")
        for turn in ctx.conversation_excerpt[-6:]:  # last 6 turns max
            role = str(turn.get("role") or "?")
            text = str(turn.get("text") or "").strip()
            if text:
                parts.append(f"- **{role}**: {text}")

    # Diagnostic evidence — structured output from the investigation
    # pass. Quoted explicitly in the prompt so the model knows to weigh
    # it against the operator's narrative.
    ev = ctx.diagnostic_evidence
    if ev is not None and hasattr(ev, "to_dict"):
        ev_lines = _format_evidence(ev)
        if ev_lines:
            parts.append("")
            parts.append("# Evidence gathered")
            parts.extend(ev_lines)

    return "\n".join(parts)


def _format_evidence(ev: Any) -> list[str]:
    """Render DiagnosticEvidence as prompt-ready bullet lines.

    Kept defensive: any field missing or non-iterable just gets skipped,
    so the classifier never crashes on a malformed evidence object.
    """
    lines: list[str] = []

    matching = getattr(ev, "matching_issues", None) or []
    if matching:
        lines.append("**Matching issues on configured target repos:**")
        for m in matching:
            try:
                lines.append(
                    f"  - {m.repo}#{m.number} ({m.state}): "
                    f"{m.title} — {m.url}"
                )
            except AttributeError:
                continue

    signals = getattr(ev, "recent_signals", None) or []
    if signals:
        lines.append("**Recent firing signals on this pod (warn/alert):**")
        for s in signals:
            try:
                bot = f" bot={s.bot_id}" if s.bot_id else ""
                lines.append(
                    f"  - [{s.severity}] {s.producer}: {s.signature}"
                    f"{bot} (last seen {s.last_observed_at or '?'})"
                )
            except AttributeError:
                continue

    commits = getattr(ev, "recent_commits", None) or []
    if commits:
        lines.append("**Recent commits in the implicated code area:**")
        for c in commits:
            try:
                lines.append(
                    f"  - {c.sha} ({c.relative_date}): {c.subject} "
                    f"[touched {c.path}]"
                )
            except AttributeError:
                continue

    notes = getattr(ev, "notes", None) or []
    skip_notes = [n for n in notes if isinstance(n, str)]
    if skip_notes:
        lines.append("**Investigation notes:** " + "; ".join(skip_notes))

    return lines


# ─── LLM call ───────────────────────────────────────────────────────────────


def _call_llm(
    *,
    system_prompt: str,
    user_message: str,
    target: Any,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> str:
    """One completion against the resolved ``infra_llm`` target (#3466:
    provider-agnostic — whichever provider the pod is credentialed for).
    """
    from infra_llm import complete  # type: ignore

    return complete(
        target,
        prompt=user_message,
        system=system_prompt,
        max_tokens=max_tokens,
        timeout=timeout,
    )


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?")
_END_FENCE_RE = re.compile(r"\n?```$")


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Same shape as ``extractor._parse_json_object`` — tolerate code
    fences and stray prose around the JSON object."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
        text = _END_FENCE_RE.sub("", text.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_target() -> Any | None:
    """Resolve the fast-role ``infra_llm`` target, or ``None`` when no LLM
    provider is credentialed — the caller surfaces a graceful "classifier
    disabled" response instead of crashing. (Per-provider env-var
    overrides and the primary bot's auth store are both honored inside
    ``resolve_infra_llm``.)
    """
    try:
        from infra_llm import resolve_infra_llm  # type: ignore
        return resolve_infra_llm("fast")
    except Exception:  # noqa: BLE001
        return None


# ─── Default classifier (live infra_llm call) ──────────────────────────────


def _coerce_verdict(parsed: dict[str, Any], ctx: ClassificationContext) -> Verdict:
    """Coerce the model's JSON into a Verdict, sanitizing each field.

    Tolerates missing or ill-typed fields. Unknown categories collapse
    to ``local_env`` (the safe default — no issue gets filed without an
    explicit intent we recognized).
    """
    raw_category = str(parsed.get("category") or "").strip().lower()
    if raw_category not in VALID_CATEGORIES:
        # Unknown / missing — bail safely. Better to over-trigger
        # local_env (which just asks for clarification) than to
        # incorrectly file a GitHub issue.
        raw_category = "local_env"
    category: Category = raw_category  # type: ignore[assignment]

    raw_target = str(parsed.get("target_name") or "").strip()
    if raw_target and ctx.available_targets and raw_target not in ctx.available_targets:
        raw_target = ""  # caller falls back to default

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return Verdict(
        category=category,
        target_name=raw_target or None,
        draft_title=str(parsed.get("draft_title") or "").strip(),
        draft_body=str(parsed.get("draft_body") or "").strip(),
        in_chat_help=str(parsed.get("in_chat_help") or "").strip(),
        confidence=confidence,
        reasoning=str(parsed.get("reasoning") or "").strip(),
    )


def _default_classifier(message: str, ctx: ClassificationContext) -> Verdict:
    """Live infra_llm call (any credentialed provider). Falls back to a
    low-confidence ``local_env`` verdict on any failure so the caller can
    degrade gracefully."""
    target = _resolve_target()
    if target is None:
        return Verdict(
            category="local_env",
            confidence=0.0,
            reasoning="classifier disabled — no LLM provider credentialed",
            in_chat_help=(
                "I'd like to help, but I can't reach the classifier right "
                "now. Try `evo bug \"<description>\"` to capture the issue "
                "directly — you can review and file it later."
            ),
        )
    try:
        raw = _call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=_format_user_message(message, ctx),
            target=target,
        )
    except Exception as e:  # noqa: BLE001 — degrade on any transport error
        return Verdict(
            category="local_env",
            confidence=0.0,
            reasoning=f"classifier call failed: {type(e).__name__}",
            in_chat_help=(
                "I couldn't classify the issue right now — the LLM call "
                "didn't go through. Try `evo bug \"<description>\"` and "
                "I'll capture it without classification."
            ),
        )
    parsed = _parse_json_object(raw)
    return _coerce_verdict(parsed, ctx)


# ─── Test seam ──────────────────────────────────────────────────────────────


_active_classifier: ClassifierFn = _default_classifier


def get_classifier() -> ClassifierFn:
    """Return the currently-installed classifier function."""
    return _active_classifier


def set_classifier(fn: ClassifierFn | None) -> None:
    """Replace the active classifier. Pass ``None`` to restore default.

    Mirrors ``evo.wizard.extractor.set_extractor`` and ``intent``
    module's seam.
    """
    global _active_classifier
    _active_classifier = fn if fn is not None else _default_classifier


# ─── Revise (Phase 2 of Issue Inbox) ────────────────────────────────────────


_REVISE_SYSTEM_PROMPT = """\
You are revising an existing draft GitHub issue body to match the
operator's instruction. The operator has already captured an intake
via the conversational front door (`evo improve`); they now want to
adjust the draft before posting.

You will receive:
  - The CURRENT draft title + body
  - The operator's revision instruction
  - The same context bullets the original classifier saw (page, version,
    diagnostic evidence)

Produce a single JSON object with:

  - new_title: revised title (under 72 chars). If the instruction
    doesn't ask to change the title, keep it identical.
  - new_body: revised body. Markdown. This is the FULL replacement —
    not a patch.
  - reasoning: one or two sentences explaining what you changed and why.
    Shown to the operator so they can see your edit at a glance.
  - confidence: 0.0 to 1.0. Drop below 0.5 if the instruction is
    ambiguous or seems to contradict the original draft's premise.

Honor these constraints:

  - Don't invent facts. If the operator's instruction implies adding
    information you don't have (e.g. "include the FileHandle stack
    trace"), include a placeholder like `<your stack trace here>` and
    say so in reasoning.
  - Preserve diagnostic context the original draft carries. The body
    typically opens with what the operator observed; that opener
    should survive unless the instruction explicitly removes it.
  - Don't change the underlying claim. "Make it more concise" means
    shorter, not different. "Frame this as a feature request" is a
    valid frame change; "argue this isn't a bug" is suspicious — ask
    for clarification by returning a low-confidence verdict.
  - Match the tone of the original draft. The operator filed it; this
    is their voice.

Respond with the JSON object, no prose around it:

  {"new_title": "...", "new_body": "...",
   "reasoning": "...", "confidence": 0.0}
"""


@dataclass
class ReviseVerdict:
    """Output of a revision pass.

    ``new_title`` may equal the original — the prompt instructs the
    model to preserve it when the instruction doesn't ask for a title
    change. ``new_body`` is always the full replacement, never a patch.
    ``confidence < 0.5`` means the model couldn't confidently execute
    the instruction; caller should ask the operator to clarify.
    """

    new_title: str
    new_body: str
    reasoning: str = ""
    confidence: float = 0.0


# Reviser function signature, mirrors :data:`ClassifierFn`.
ReviserFn = Callable[[str, str, str, ClassificationContext], ReviseVerdict]


def _format_revise_user_message(
    *,
    current_title: str,
    current_body: str,
    instruction: str,
    ctx: ClassificationContext,
) -> str:
    """Render the reviser's user prompt. Mirrors the classifier's
    :func:`_format_user_message` so context bullets are consistent
    across the two passes."""
    parts: list[str] = []
    parts.append("# Current draft\n")
    parts.append(f"**Title:** {current_title.strip() or '(empty)'}")
    parts.append("")
    parts.append("**Body:**")
    parts.append(current_body.strip() or "(empty)")
    parts.append("")

    parts.append("# Operator's revision instruction\n")
    parts.append(instruction.strip() or "(empty)")
    parts.append("")

    parts.append("# Context\n")
    bullets: list[str] = []
    if ctx.reported_from:
        bullets.append(f"- Reported from: `{ctx.reported_from}`")
    if ctx.evolve_version:
        bullets.append(f"- Evolve version: {ctx.evolve_version}")
    if ctx.openclaw_version:
        bullets.append(f"- OpenClaw version: {ctx.openclaw_version}")
    if ctx.active_bot:
        bullets.append(f"- Active bot: `{ctx.active_bot}`")
    if not bullets:
        bullets.append("- (no extra context)")
    parts.extend(bullets)

    # Reuse the diagnostic-evidence renderer from the classifier so the
    # reviser sees the same evidence bullets the original verdict was
    # produced from. Keeps the two passes interpretively aligned.
    ev = ctx.diagnostic_evidence
    if ev is not None and hasattr(ev, "to_dict"):
        ev_lines = _format_evidence(ev)
        if ev_lines:
            parts.append("")
            parts.append("# Evidence gathered (when original was classified)")
            parts.extend(ev_lines)

    return "\n".join(parts)


def _coerce_revise_verdict(parsed: dict[str, Any], current_title: str) -> ReviseVerdict:
    """Defensive coercion. Missing new_title falls back to the original
    so a malformed response can't silently blank the title."""
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    new_title = str(parsed.get("new_title") or "").strip()
    if not new_title:
        new_title = current_title
    return ReviseVerdict(
        new_title=new_title,
        new_body=str(parsed.get("new_body") or "").strip(),
        reasoning=str(parsed.get("reasoning") or "").strip(),
        confidence=confidence,
    )


def _default_reviser(
    current_title: str,
    current_body: str,
    instruction: str,
    ctx: ClassificationContext,
) -> ReviseVerdict:
    """Live infra_llm call (any credentialed provider). Falls back to a
    low-confidence "I couldn't revise" verdict on any failure so the
    caller can degrade gracefully."""
    target = _resolve_target()
    if target is None:
        return ReviseVerdict(
            new_title=current_title, new_body=current_body,
            confidence=0.0,
            reasoning="reviser disabled — no LLM provider credentialed",
        )
    user_msg = _format_revise_user_message(
        current_title=current_title,
        current_body=current_body,
        instruction=instruction,
        ctx=ctx,
    )
    try:
        raw = _call_llm(
            system_prompt=_REVISE_SYSTEM_PROMPT,
            user_message=user_msg,
            target=target,
        )
    except Exception as e:  # noqa: BLE001 — degrade on any transport error
        return ReviseVerdict(
            new_title=current_title, new_body=current_body,
            confidence=0.0,
            reasoning=f"reviser call failed: {type(e).__name__}",
        )
    parsed = _parse_json_object(raw)
    return _coerce_revise_verdict(parsed, current_title)


_active_reviser: ReviserFn = _default_reviser


def get_reviser() -> ReviserFn:
    return _active_reviser


def set_reviser(fn: ReviserFn | None) -> None:
    """Replace the active reviser. ``None`` restores the default."""
    global _active_reviser
    _active_reviser = fn if fn is not None else _default_reviser


def revise_draft(
    *,
    current_title: str,
    current_body: str,
    instruction: str,
    context: ClassificationContext | None = None,
) -> ReviseVerdict:
    """Apply ``instruction`` to ``(current_title, current_body)``.

    Returns a :class:`ReviseVerdict` with the rewritten title + body,
    plus a one-line reasoning the caller surfaces to the operator. Never
    raises; on any failure returns a confidence=0 verdict that preserves
    the originals.
    """
    ctx = context or ClassificationContext()
    try:
        return get_reviser()(current_title, current_body, instruction, ctx)
    except Exception as e:  # noqa: BLE001
        return ReviseVerdict(
            new_title=current_title, new_body=current_body,
            confidence=0.0,
            reasoning=f"reviser raised: {type(e).__name__}: {e}",
        )


# ─── Triage (Phase 4 of Issue Inbox) ────────────────────────────────────────


_TRIAGE_SYSTEM_PROMPT = """\
You are an issue-triage assistant. An issue has been filed by SOMEONE
ELSE on a repo the operator maintains. Your job is to classify it so
the operator can decide what to do next quickly.

You will receive:
  - The inbound issue's title, body, author login, repo
  - A small set of context bullets (recent matching issues, recent
    commits in the area, recent signals)

Produce a single JSON object with:

  - category: one of
      "bug"            — real defect, needs a fix
      "feature_request" — net-new capability ask
      "question"       — usage question; can probably be answered
                         without code change
      "duplicate"      — matches an existing open issue; closing as
                         duplicate is the right move (cite the original
                         in duplicate_of)
      "spam"           — drive-by, off-topic, obvious junk
      "docs"           — gap in documentation rather than the code

  - merit: how seriously should we take this?
      "real"     — concrete report with enough detail to act on
      "unclear"  — plausible but needs more info (ask the reporter)
      "low"      — vague or unlikely to be a real issue

  - urgency: how soon does the operator need to look at it?
      "p0"  — security, data loss, or production-down severity
      "p1"  — meaningful breakage that affects operators today
      "p2"  — annoying but workable; default for most bugs
      "p3"  — nice-to-have, polish, future improvement

  - duplicate_of: when category=duplicate, list the existing open
    issues this matches (format: "owner/repo#42"). Empty otherwise.

  - recommendation: what the operator should do, summarized to one
    of these so Phase 5 auto-response rules can dispatch on it:
      "auto_close_duplicate"   — close + comment linking the original
      "auto_reply_clarifying"  — ask the reporter for more info
      "route_to_admin"         — needs the operator's judgment
      "needs_investigation"    — looks real but requires reproduction
                                 or code-reading before action

  - draft_reply: optional 1-3 paragraph comment to post if the
    recommendation calls for one. Empty for recommendations that
    don't post a reply.

  - draft_labels: optional list of GH labels to apply. Pick from the
    repo's normal label set; suggest "duplicate", "needs-info", or
    severity tags like "p0"/"p1" when they fit. Empty list is fine.

  - estimated_effort: how big is the fix, if any? trivial / small /
    medium / large. Used by the operator to plan; doesn't drive
    automation.

  - confidence: 0.0 to 1.0. Sub-0.5 means "I'm guessing"; the operator
    is asked to re-triage manually before any automated action runs.

  - reasoning: one or two sentences explaining the verdict. Shown in
    the triage queue so the operator can challenge.

Respond with a single JSON object, no prose around it.
"""


def _format_triage_user_message(
    *,
    title: str,
    body: str,
    repo: str,
    author: str,
    ctx: ClassificationContext,
) -> str:
    """Render the triage user prompt: inbound issue + context bullets."""
    parts: list[str] = []
    parts.append(f"# Inbound issue ({repo})\n")
    parts.append(f"**Filed by:** @{author or '(unknown)'}")
    parts.append(f"**Title:** {title.strip() or '(no title)'}")
    parts.append("")
    parts.append("**Body:**")
    parts.append(body.strip() or "(empty)")
    parts.append("")

    parts.append("# Context\n")
    bullets: list[str] = []
    if ctx.evolve_version:
        bullets.append(f"- Evolve version: {ctx.evolve_version}")
    if ctx.openclaw_version:
        bullets.append(f"- OpenClaw version: {ctx.openclaw_version}")
    if not bullets:
        bullets.append("- (no extra context)")
    parts.extend(bullets)

    # Reuse the diagnostic-evidence renderer so the triage classifier
    # sees the same evidence shape downstream tools already produce.
    ev = ctx.diagnostic_evidence
    if ev is not None and hasattr(ev, "to_dict"):
        ev_lines = _format_evidence(ev)
        if ev_lines:
            parts.append("")
            parts.append("# Evidence gathered")
            parts.extend(ev_lines)

    return "\n".join(parts)


@dataclass
class TriageVerdict:
    """Classifier output for an inbound issue. Mirrors the
    :class:`evolve_admin.intake.envelope.TriageRecord` schema so the
    caller can write it through directly."""

    category: str = "unknown"
    merit: str = "unknown"
    urgency: str = "unknown"
    duplicate_of: list[str] = field(default_factory=list)
    recommendation: str = "unknown"
    draft_reply: str = ""
    draft_labels: list[str] = field(default_factory=list)
    estimated_effort: str = "unknown"
    confidence: float = 0.0
    reasoning: str = ""


TriagerFn = Callable[[str, str, str, str, ClassificationContext], TriageVerdict]


_TRIAGE_CATEGORIES = ("bug", "feature_request", "question", "duplicate",
                      "spam", "docs", "unknown")
_TRIAGE_MERITS = ("real", "unclear", "low", "unknown")
_TRIAGE_URGENCIES = ("p0", "p1", "p2", "p3", "unknown")
_TRIAGE_RECOMMENDATIONS = ("auto_close_duplicate", "auto_reply_clarifying",
                           "route_to_admin", "needs_investigation", "unknown")
_TRIAGE_EFFORTS = ("trivial", "small", "medium", "large", "unknown")


def _coerce_triage_verdict(parsed: dict[str, Any]) -> TriageVerdict:
    """Defensive coercion. Unknown literal values collapse to 'unknown'
    so a malformed response never silently picks the wrong category."""
    def _norm(v: Any, allowed: tuple[str, ...]) -> str:
        s = str(v or "").strip().lower()
        return s if s in allowed else "unknown"

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    dup_raw = parsed.get("duplicate_of") or []
    dup_list = [str(x) for x in dup_raw if isinstance(x, str)] \
        if isinstance(dup_raw, list) else []

    label_raw = parsed.get("draft_labels") or []
    label_list = [str(x) for x in label_raw if isinstance(x, str)] \
        if isinstance(label_raw, list) else []

    return TriageVerdict(
        category=_norm(parsed.get("category"), _TRIAGE_CATEGORIES),
        merit=_norm(parsed.get("merit"), _TRIAGE_MERITS),
        urgency=_norm(parsed.get("urgency"), _TRIAGE_URGENCIES),
        duplicate_of=dup_list,
        recommendation=_norm(parsed.get("recommendation"),
                             _TRIAGE_RECOMMENDATIONS),
        draft_reply=str(parsed.get("draft_reply") or "").strip(),
        draft_labels=label_list,
        estimated_effort=_norm(parsed.get("estimated_effort"),
                               _TRIAGE_EFFORTS),
        confidence=confidence,
        reasoning=str(parsed.get("reasoning") or "").strip(),
    )


def _default_triager(
    title: str,
    body: str,
    repo: str,
    author: str,
    ctx: ClassificationContext,
) -> TriageVerdict:
    """Live infra_llm call (any credentialed provider). Falls back to a
    low-confidence "unknown" verdict on any failure so callers can
    degrade gracefully."""
    target = _resolve_target()
    if target is None:
        return TriageVerdict(
            confidence=0.0,
            reasoning="triager disabled — no LLM provider credentialed",
        )
    user_msg = _format_triage_user_message(
        title=title, body=body, repo=repo, author=author, ctx=ctx,
    )
    try:
        raw = _call_llm(
            system_prompt=_TRIAGE_SYSTEM_PROMPT,
            user_message=user_msg,
            target=target,
        )
    except Exception as e:  # noqa: BLE001 — degrade on any transport error
        return TriageVerdict(
            confidence=0.0,
            reasoning=f"triager call failed: {type(e).__name__}",
        )
    parsed = _parse_json_object(raw)
    return _coerce_triage_verdict(parsed)


_active_triager: TriagerFn = _default_triager


def get_triager() -> TriagerFn:
    return _active_triager


def set_triager(fn: TriagerFn | None) -> None:
    """Replace the active triager (test seam)."""
    global _active_triager
    _active_triager = fn if fn is not None else _default_triager


def triage_inbound(
    *,
    title: str,
    body: str,
    repo: str,
    author: str,
    context: ClassificationContext | None = None,
) -> TriageVerdict:
    """Classify an inbound issue. Never raises.

    On any unhandled exception, returns a confidence=0 ``unknown``
    verdict so the calling watcher can capture the intake anyway and
    let the operator re-triage by hand.
    """
    ctx = context or ClassificationContext()
    try:
        return get_triager()(title, body, repo, author, ctx)
    except Exception as e:  # noqa: BLE001
        return TriageVerdict(
            confidence=0.0,
            reasoning=f"triager raised: {type(e).__name__}: {e}",
        )


# ─── Public entry point ─────────────────────────────────────────────────────


def classify_issue(
    message: str,
    *,
    context: ClassificationContext | None = None,
) -> Verdict:
    """Classify ``message`` into a :class:`Verdict`. Never raises.

    Internally delegates to the active classifier (default: infra_llm —
    any credentialed provider; swappable via :func:`set_classifier` for
    tests). Any unhandled
    exception from the classifier yields a low-confidence ``local_env``
    verdict so the caller can degrade gracefully.
    """
    ctx = context or ClassificationContext()
    try:
        return get_classifier()(message, ctx)
    except Exception as e:  # noqa: BLE001
        return Verdict(
            category="local_env",
            confidence=0.0,
            reasoning=f"classifier raised: {type(e).__name__}: {e}",
        )
