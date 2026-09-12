"""fit_review.playbook_bestpractices — per-archetype best-practice capability corpus.

A bot's *archetype playbook* (``archetypes._PLAYBOOKS``) answers "which activity
*domains* is this archetype for?" — a frozenset of domain tags, the cheap
targeting lens. That is enough to *target* (decide which noun to look at), but it
says nothing about *what a good bot of this kind actually does* with those
domains. This module supplies that second, richer layer: per archetype, a short
list of **best-practice capability patterns** — "a project-manager bot commonly
does standup digests, blocker tracking, weekly status rollups …" — that the L2
reflection runner reads as its "what good looks like" reference.

**Provenance — distilled, not scraped.** The patterns here are curated from the
edr market-intelligence knowledge base (``internal/market-intelligence/``), the
dev-environment corpus a spec author consults before designing a capability
(README there). Each entry carries a ``source`` back to the KB entry it was
distilled from (or to the real gallery app that proves the pattern installable).
This is a **pure-data, KB-fed enrichment**: no LLM, no network, no scraping
inside the RSI loop — that would violate RSI's cost discipline (LLM is
escalation, not baseline) and reinvent an edr capability. When the KB refreshes
(its ~8-week scan cadence), re-distill here; the RSI engine is now a *consumer*
of the edr KB (hand-off noted to edr).

**Where the KB is thin** (home-automation's smart-home coverage, research-analyst
in a consumer-skewed corpus) the entries are curated conservatively from the base
playbook + the one real signal that exists, and the gap is flagged in
``KB_GAPS`` so a future KB refresh knows where to deepen.

**Decoupling note.** This is a *new* module so the corpus does not have to be
inline-edited into ``archetypes.py`` (which a sibling flight also touches for the
reflection runner). ``archetypes.composed_playbook_for`` is the single thin
*compose* call that merges this enrichment onto the base domain playbook. The
``domains`` on each best practice are drawn from the same shared vocabulary as the
observation nouns and gallery ``application_tags`` (see ``archetypes`` module
docstring) so a best practice can be cross-referenced to a real installable app —
but a best practice MAY reference a domain outside the archetype's base
``aligned_domains`` (that is the enrichment: a pattern the base lens omits).

The compose contract is deliberately **additive and base-preserving**: the
composed playbook's ``aligned_domains`` is the base frozenset *verbatim* (targeting
semantics are unchanged — this module never widens the targeting floor); the best
practices ride alongside as context for the reflection step only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BestPractice:
    """One distilled best-practice capability pattern for an archetype.

    ``capability`` is the short name; ``description`` is the one-line
    job-to-be-done; ``domains`` are the shared-vocabulary domain tags the pattern
    touches (cross-referenceable to gallery ``application_tags``); ``source``
    cites the edr-KB entry (or proving gallery app) it was distilled from.
    """

    capability: str
    description: str
    domains: frozenset[str]
    source: str


def _bp(capability: str, description: str, domains: set[str], source: str) -> BestPractice:
    """Terse constructor — freezes ``domains`` so the corpus is immutable."""
    return BestPractice(
        capability=capability,
        description=description,
        domains=frozenset(domains),
        source=source,
    )


# ── The corpus. Keys MUST be members of ``bot_purpose.ARCHETYPES``. ───────────
# Each list is distilled from internal/market-intelligence/ (cited per entry). Order
# is rough value/adoption order (the KB's "rough adoption order" where it gives
# one). ``custom`` is intentionally absent — a freeform-mission bot has no
# canonical lens, so it carries no canonical best practices either (mirrors the
# empty ``custom`` base playbook; back-compat returns a valid empty enrichment).
_BEST_PRACTICES: dict[str, tuple[BestPractice, ...]] = {
    "personal-assistant": (
        _bp(
            "Morning briefing / daily digest",
            "Deliver one morning digest (calendar + tasks + weather + news + health) "
            "to the user's messaging app, replacing 5–6 separate app opens.",
            {"calendar", "task-management", "reminders", "scheduling"},
            "market-demand.md 2026-05-12 — the killer app ('most popular OpenClaw setup by a wide margin')",
        ),
        _bp(
            "Email triage & draft replies",
            "Summarize, triage, unsubscribe from, and draft replies to inbound email.",
            {"email", "task-management"},
            "market-demand.md 2026-05-12 — top use case #2 (email automation); gallery Email Triage Assistant",
        ),
        _bp(
            "Calendar management & proactive scheduling",
            "Hold the user's calendar; schedule, reschedule, and surface conflicts before they bite.",
            {"calendar", "scheduling", "reminders"},
            "market-demand.md 2026-05-12 — top use case #4; gallery Executive Calendar Assistant",
        ),
        _bp(
            "Reminders & reliable follow-through",
            "Capture commitments and nudge until done — bounded automation that delivers daily.",
            {"reminders", "task-management"},
            "market-demand.md 2026-05-12 — emotional drivers + reliability angle ('actually does what it says it did')",
        ),
        _bp(
            "Travel planning",
            "Assemble itineraries, bookings, and trip logistics into one plan.",
            {"travel", "calendar"},
            "market-demand.md 2026-05-12 — personal-lifestyle use; gallery Travel Planner",
        ),
        _bp(
            "Health & fitness logging",
            "Keep a daily health/fitness log and surface trends.",
            {"health-fitness"},
            "gallery Daily Health Tracker / Daily Journal (personal-lifestyle category)",
        ),
        _bp(
            "Household & home management",
            "Track household tasks, finances, and home logistics in one place.",
            {"home-management", "task-management"},
            "gallery Home Manager / Personal Finance Tracker; market-demand.md 2026-05-12 family-coordination use",
        ),
    ),
    "project-manager": (
        _bp(
            "Task & blocker tracking",
            "Hold the project's tasks; capture blockers and surface what is stuck.",
            {"task-management"},
            "spec-fit-reviewer §1.1 (the PM bot's #1 need, uncovered); gallery Project Tracker",
        ),
        _bp(
            "Weekly status rollups",
            "Assemble a recurring status report from the week's activity (the status done by hand today).",
            {"document-generation", "status-reporting", "task-management"},
            "spec-fit-reviewer §3.5 (weekly status assembled ad hoc); gallery Weekly Status Reporter",
        ),
        _bp(
            "Standup digests & meeting agenda prep",
            "Build meeting agendas and turn meeting transcripts into action items.",
            {"calendar", "document-generation", "task-management"},
            "market-demand.md 2026-05-12 — business ops (meeting transcripts → tasks); gallery Meeting Agenda Builder",
        ),
        _bp(
            "Team communication coordination",
            "Route updates and coordinate across the team's Slack / messaging channels.",
            {"slack-comms", "coordination"},
            "positioning-and-differentiators.md 2026-06-11 — coordination state is the moat; gallery Slack Communications Manager",
        ),
        _bp(
            "Client-facing responsiveness & escalation",
            "Let the client message the project bot directly; triage, hold communication memory, "
            "escalate by owner-set boundary.",
            {"email", "slack-comms", "coordination"},
            "positioning-and-differentiators.md 2026-05-12 — service-business persona (client-facing project bots)",
        ),
        _bp(
            "Audit trail / decision memory",
            "Keep a durable, queryable record of project decisions and client communications.",
            {"document-generation", "ops"},
            "positioning-and-differentiators.md 2026-05-12 — service-business persona (audit trail, communication memory)",
        ),
    ),
    "home-automation": (
        _bp(
            "Home device control & routines",
            "Control smart-home devices and run scheduled routines from a messaging app.",
            {"home-management", "scheduling"},
            "market-demand.md 2026-05-12 — top use case #6 (smart home / IoT via WhatsApp/Telegram); Home Assistant integration",
        ),
        _bp(
            "Household task & chore tracking",
            "Track recurring household tasks and chores; nudge on the schedule.",
            {"home-management", "task-management", "reminders"},
            "gallery Home Manager (personal-lifestyle category)",
        ),
        _bp(
            "Shared household calendar & reminders",
            "Hold the household's shared calendar and surface reminders to the right member.",
            {"calendar", "reminders", "scheduling"},
            "archetypes base playbook + market-demand.md 2026-05-12 family-coordination use (#9)",
        ),
        _bp(
            "Family coordination",
            "Coordinate schedules and tasks across household members.",
            {"home-management", "calendar", "coordination"},
            "market-demand.md 2026-05-12 — top use case #9 (family coordination)",
        ),
    ),
    "research-analyst": (
        _bp(
            "Research & source synthesis",
            "Gather sources on a question and synthesize them into a cited brief.",
            {"research", "document-generation", "summarization"},
            "gallery Research Assistant (professional-productivity / creative)",
        ),
        _bp(
            "Document drafting & summarization",
            "Draft and condense long documents into digestible briefs.",
            {"document-generation", "summarization"},
            "market-demand.md 2026-05-12 — 'summarize' is the recurring email/notes verb; gallery Research Assistant",
        ),
        _bp(
            "Knowledge base & semantic recall",
            "Maintain a searchable knowledge base; semantic recall over notes and voice notes.",
            {"research", "summarization"},
            "market-demand.md 2026-05-12 — top use case #3 (personal memory / knowledge)",
        ),
        _bp(
            "Data analysis & recurring reporting",
            "Analyze structured data and produce a recurring report.",
            {"data-analysis", "document-generation"},
            "archetypes base playbook (data-analysis) — KB thin for this archetype; conservative (see KB_GAPS)",
        ),
    ),
    "customer-facing": (
        _bp(
            "Inbound triage & routing",
            "Triage inbound customer messages and route or escalate by boundary.",
            {"email", "slack-comms", "support"},
            "positioning-and-differentiators.md 2026-05-12 — service-business (client-facing responsiveness); market-demand email automation",
        ),
        _bp(
            "Draft replies & response templates",
            "Draft consistent replies and summarize threads for fast, on-brand response.",
            {"email", "document-generation", "support"},
            "market-demand.md 2026-05-12 — email automation (draft replies); gallery Email Triage Assistant",
        ),
        _bp(
            "Customer communication memory",
            "Keep a durable record of each customer's history so context survives across messages.",
            {"document-generation", "support"},
            "positioning-and-differentiators.md 2026-05-12 — service-business (communication memory)",
        ),
        _bp(
            "Escalation boundaries",
            "Answer in-scope requests; escalate out-of-scope ones to a human per owner-set boundaries.",
            {"support", "coordination"},
            "positioning-and-differentiators.md 2026-05-12 — service-business (owner sets boundaries and escalation)",
        ),
        _bp(
            "Task capture from conversations",
            "Turn customer requests into tracked tasks / tickets.",
            {"task-management", "support"},
            "market-demand.md 2026-05-12 — business ops (CRM / lead tracking)",
        ),
    ),
    # "custom" intentionally omitted — no canonical lens, no canonical practices.
}


# Where the edr KB is thin for an archetype: the entries above were curated
# conservatively from the base playbook + the one real signal, and a future KB
# refresh should deepen these. Surfaced as data so a refresh tool (or an operator)
# can see the gaps without re-reading the corpus.
KB_GAPS: dict[str, str] = {
    "home-automation": (
        "Smart-home / IoT is a single bullet in market-demand.md (top use case #6); "
        "no dedicated home-automation entry in the KB. Patterns curated from that bullet "
        "+ the base playbook + gallery Home Manager. Deepen on next KB refresh."
    ),
    "research-analyst": (
        "The consumer-skewed KB under-represents the research-analyst archetype "
        "(closest signal: 'personal memory / knowledge', top use case #3). Patterns "
        "curated from that + the base playbook + gallery Research Assistant. Deepen on next KB refresh."
    ),
}


def best_practices_for(archetype: str | None) -> tuple[BestPractice, ...]:
    """Return the distilled best-practice patterns for an archetype, or empty.

    Lowercases + strips. An archetype with no corpus entry (including ``custom``
    and any not-yet-curated value) yields an empty tuple — the conservative
    default that keeps the membership check total and back-compat safe.
    """
    key = (archetype or "").strip().lower()
    return _BEST_PRACTICES.get(key, ())


@dataclass(frozen=True)
class ComposedPlaybook:
    """A base archetype playbook merged with its best-practice enrichment.

    ``aligned_domains`` is the base domain frozenset *verbatim* (targeting
    semantics unchanged — never widened here). ``best_practices`` is the additive
    "what good looks like" layer the reflection runner reads. A best practice may
    reference a domain outside ``aligned_domains`` (that is the enrichment).
    """

    archetype: str
    aligned_domains: frozenset[str]
    best_practices: tuple[BestPractice, ...]

    def has_enrichment(self) -> bool:
        """True if any best-practice patterns are attached."""
        return bool(self.best_practices)

    @property
    def best_practice_domains(self) -> frozenset[str]:
        """Union of every domain referenced by the attached best practices.

        May extend beyond ``aligned_domains`` — those extra domains are patterns
        the base targeting lens omits. Useful for the reflection step, NOT for
        the targeting floor.
        """
        out: set[str] = set()
        for bp in self.best_practices:
            out |= bp.domains
        return frozenset(out)

    @property
    def all_domains(self) -> frozenset[str]:
        """``aligned_domains`` ∪ ``best_practice_domains`` — the full surface."""
        return self.aligned_domains | self.best_practice_domains


def compose_playbook(
    archetype: str | None,
    aligned_domains: frozenset[str],
) -> ComposedPlaybook:
    """Merge a base domain playbook with this module's best-practice enrichment.

    ``aligned_domains`` is passed in (rather than imported from ``archetypes``) so
    this module has no dependency on ``archetypes`` — the merge is one-directional
    (``archetypes`` reads this), which keeps the compose footprint in
    ``archetypes.py`` a single thin call and avoids a circular import.

    Base-preserving: the returned ``aligned_domains`` is the input frozenset
    verbatim — no base domain is ever dropped or rewritten by composition.
    """
    key = (archetype or "").strip().lower()
    return ComposedPlaybook(
        archetype=key,
        aligned_domains=frozenset(aligned_domains),
        best_practices=best_practices_for(key),
    )
