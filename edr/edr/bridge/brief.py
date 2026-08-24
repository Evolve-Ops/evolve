"""edr.bridge.brief — the self-contained dispatch brief (design §5.1).

``brief_for(proposal, *, repo=...)`` is a PURE function: one agent-able
triage Proposal → the complete instruction text for a headless Claude
Code worker session. The spawned session has no memory of EDR, the
store, or this process — the brief must stand alone, so everything the
worker needs (issue reference, proof artifact, branch, workflow,
constraints, report format) is rendered into it.

This module is a TIER INTERFACE (tiered-oversight §4): every constraint
the brief imposes is MODULE-LEVEL DATA below — the contract between
coordinator and worker tiers is inspectable constants, not prose buried
in a template. P1.3b verifies the same constants it dispatched (e.g.
the G3 never-merge clause and the report-back fields).

The load-bearing clauses (why each exists)
─────────────────────────────────────────────────────────────────────────
  proof artifact   — rendered VERBATIM as the falsifiable close
                     condition (§5.2): the work is not done until the
                     proof holds. Bug-shaped work additionally gets the
                     red→green discipline (failing test FIRST).
  branch name      — ``edr/gh-<issue_number>-<slug>``; deterministic so
                     P1.3b can find the work and flag strays. The slug
                     comes from the proposal's human_title (the triage
                     ``"EDR triage: "`` prefix stripped), lowercased,
                     hyphenated, ≤ ``SLUG_MAX_LEN`` chars.
  anti-wedge       — immediate empty-commit push + push-per-chunk:
                     session death costs a relaunch, never the work.
  one-bite scope   — scope growth is reported (``scope-growth``), never
                     improvised: mis-scoped dispatch is triage's error
                     to learn from, not the worker's to paper over.
  G3 never-merge   — the worker's ONLY mutation channel is an OPEN PR
                     (§7 safe-outputs). Contractual in v1 (this text);
                     P1.3b verifies the PR was not merged and flags
                     violations loudly.
  scrub rules      — no operator/bot/host names in anything pushed.
  report-back      — a short TYPED block (``REPORT_FIELDS``); the
                     worker's final message is parse-able, so the
                     result lands on the Proposal as data (P1.3b).
"""

from __future__ import annotations

import re
import textwrap

from schema.proposal import Proposal

from edr.bridge.lifecycle import triage_verdict

# ── DATA: branch naming ──────────────────────────────────────────────────────

BRANCH_TEMPLATE = "edr/gh-{number}-{slug}"
SLUG_MAX_LEN = 40
SLUG_FALLBACK = "work-item"

# Must mirror edr.triage.dev_issue.build_proposal's human_title format
# ("EDR triage: {signal.title}") — stripped so the slug carries the
# issue's own words, not the generator's prefix.
TRIAGE_TITLE_PREFIX = "EDR triage: "

# ── DATA: the constraint clauses (inspectable; P1.3b re-checks these) ────────

G3_NEVER_MERGE = (
    "NEVER merge the PR. Not `gh pr merge`, not `--auto`, not a merge "
    "queue, not 'just this once'. Merging is a HUMAN act (EDR gate G3, "
    "autonomy rung R1): your only mutation channel is an OPEN pull "
    "request. A merged PR from this session is a protocol violation "
    "and will be flagged loudly."
)

ANTI_WEDGE_WORKFLOW = (
    "IMMEDIATELY after branching, before any other work: "
    '`git commit --allow-empty -m "wip: <branch>"` and push with `-u`. '
    "Commit and push after EVERY completed chunk — session death must "
    "cost a relaunch, never the work."
)

ONE_BITE_SCOPE = (
    "One bite only — no scope growth. If the fix outgrows this brief "
    "(new subsystems, design decisions, privileged paths), STOP: push "
    "what you have and report `proof_status: scope-growth` instead of "
    "improvising a bigger change."
)

SCRUB_RULES = (
    "Scrub discipline: no operator names, no bot names, no hostnames, "
    "and no pod-specific paths in code, tests, commit messages, branch "
    "names, or the PR."
)

PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"

# Extra proof discipline per triage classification (data, not branches).
PROOF_DISCIPLINE_BY_CLASSIFICATION: dict[str, str] = {
    "bug": (
        "Bug-shaped work: write the FAILING test FIRST, run it, show "
        "RED, then make it green. A proof that was never red proves "
        "nothing."
    ),
}

# ── DATA: the typed report-back contract (the worker's final message) ────────

REPORT_FIELDS: tuple[str, ...] = ("branch", "pr_url", "proof_status", "notes")
PROOF_STATUS_VALUES: tuple[str, ...] = (
    "green",  # the proof artifact holds
    "red",  # attempted; the proof does not hold
    "not-run",  # could not run the proof (say why in notes)
    "scope-growth",  # stopped per the one-bite rule
)

_WIDTH = 72


class BriefUnavailable(Exception):
    """``brief_for()`` cannot render a brief for this proposal.

    Raised for proposals without an agent-able triage verdict, without
    a proof artifact, or not backed by a GitHub issue — the brief
    contract is GitHub-issue-shaped in v1 (the worker's first action is
    ``gh issue view``).
    """


# ── Pure renderers ───────────────────────────────────────────────────────────

_SLUG_TOKEN_RE = re.compile(r"[a-z0-9]+")


def slug_for(human_title: str) -> str:
    """Branch slug from a proposal's human_title.

    Strips the triage generator's ``"EDR triage: "`` prefix, lowercases,
    joins ``[a-z0-9]+`` tokens with hyphens, clamps to ``SLUG_MAX_LEN``
    (no trailing hyphen). Empty input → ``SLUG_FALLBACK``.
    """
    text = human_title or ""
    if text.startswith(TRIAGE_TITLE_PREFIX):
        text = text[len(TRIAGE_TITLE_PREFIX):]
    tokens = _SLUG_TOKEN_RE.findall(text.lower())
    slug = "-".join(tokens)[:SLUG_MAX_LEN].rstrip("-")
    return slug or SLUG_FALLBACK


def branch_for(issue_number: int, human_title: str) -> str:
    """The deterministic work branch: ``edr/gh-<number>-<slug>``."""
    return BRANCH_TEMPLATE.format(
        number=issue_number, slug=slug_for(human_title)
    )


def _para(text: str) -> str:
    return textwrap.fill(text, width=_WIDTH)


def _bullet(text: str) -> str:
    return textwrap.fill(
        text, width=_WIDTH, initial_indent="- ", subsequent_indent="  "
    )


def _heading(text: str) -> str:
    return f"{text}\n{'-' * len(text)}"


def _report_block(branch: str) -> str:
    """The typed report-back block, rendered from REPORT_FIELDS."""
    values = {
        "branch": branch,
        "pr_url": '<the PR URL, or "none">',
        "proof_status": " | ".join(PROOF_STATUS_VALUES),
        "notes": "<one line>",
    }
    return "\n".join(
        f"    {field}: {values[field]}" for field in REPORT_FIELDS
    )


def brief_for(proposal: Proposal, *, repo: str) -> str:
    """Render the self-contained dispatch brief for one work item.

    Pure: reads only the Proposal (its triage verdict + human_title)
    and ``repo``; no IO. Raises ``BriefUnavailable`` when the proposal
    has no agent-able triage verdict, no proof artifact, or no GitHub
    issue number (see class docstring). The proof artifact appears
    VERBATIM (indented, never re-wrapped) — it is the falsifiable close
    condition and must survive copy-paste.
    """
    verdict = triage_verdict(proposal)
    if verdict is None:
        raise BriefUnavailable(
            f"{proposal.id}: no machine-readable triage verdict "
            "(provenance.signals['triage'] missing or malformed)"
        )
    if verdict.get("agent_able") is not True:
        raise BriefUnavailable(
            f"{proposal.id}: human-routed (agent_able is not True; "
            f"triage reason: {verdict.get('reason', '')!r}) — no brief "
            "for human-lane items"
        )
    proof = verdict.get("proof_artifact")
    if not isinstance(proof, str) or not proof.strip():
        raise BriefUnavailable(
            f"{proposal.id}: no falsifiable proof_artifact (§5.2: no "
            "proof, no dispatch)"
        )
    number = verdict.get("issue_number")
    if not isinstance(number, int):
        raise BriefUnavailable(
            f"{proposal.id}: not backed by a GitHub issue "
            "(triage issue_number is missing) — the v1 brief contract "
            "is GitHub-issue-shaped"
        )

    title = proposal.human_title or ""
    if title.startswith(TRIAGE_TITLE_PREFIX):
        title = title[len(TRIAGE_TITLE_PREFIX):]
    branch = branch_for(number, proposal.human_title or "")
    url = verdict.get("issue_url") or "n/a"
    route = verdict.get("route", "")
    discipline = PROOF_DISCIPLINE_BY_CLASSIFICATION.get(
        str(verdict.get("classification", ""))
    )

    sections = [
        f"EDR DISPATCH BRIEF — GitHub issue #{number} "
        f"(proposal {proposal.id})",
        "=" * _WIDTH,
        _para(
            "You are a focused, single-bite worker session dispatched "
            "by EDR (the Evolve Development Rig). This brief is "
            "self-contained — work from it autonomously. Your ONLY "
            "mutation channel is a pushed branch plus an OPEN pull "
            "request."
        ),
        _heading("Work item"),
        "\n".join(
            [
                f"- Repo:  {repo}",
                f"- Issue: #{number} — {title}",
                f"- URL:   {url}",
                f"- Route: {route}",
                "- First action — read the FULL issue (body + comments):",
                f"    gh issue view {number} --repo {repo} --comments",
            ]
        ),
        _heading("Falsifiable close condition (the proof artifact)"),
        f"    {proof}",
        _para(
            "The work is NOT done until this proof holds. Run it and "
            "put the real output in the PR body."
        )
        + (f"\n\n{_para(discipline)}" if discipline else ""),
        _heading("Branch"),
        "Work on exactly this branch, off latest origin/main:\n"
        f"    {branch}",
        _heading("Workflow (anti-wedge — mandatory)"),
        "\n".join([_bullet(ANTI_WEDGE_WORKFLOW), _bullet(ONE_BITE_SCOPE)]),
        _heading("Pull request"),
        "\n".join(
            [
                _bullet(
                    "Open ONE PR against main. The body states what "
                    "changed and shows the proof artifact's results "
                    "(the commands you ran + their output)."
                ),
                "- End the PR body with exactly:",
                f"    {PR_FOOTER}",
                _bullet(G3_NEVER_MERGE),
            ]
        ),
        _heading("Scrub"),
        _bullet(SCRUB_RULES),
        _heading("Report back (your FINAL message — exactly this block)"),
        _report_block(branch),
    ]
    return "\n\n".join(sections) + "\n"
