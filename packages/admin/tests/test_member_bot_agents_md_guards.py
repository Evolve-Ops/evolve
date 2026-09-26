"""tests/test_member_bot_agents_md_guards.py — structural lint on the member-bot
AGENTS.md template.

``packages/admin/evolve_admin/templates/bot_workspace/AGENTS.md`` is the starter
system prompt Evolve installs into every MEMBER bot's workspace (via
``deploy.install_bot_docs(role="member")``). It is a DIFFERENT, smaller doc than
the admin/primary bot's hand-written AGENTS.md (``packages/analyzer/evolve_bot/``,
guarded by test_evo_agents_md_guards.py).

Operator live report 2026-06-25: with the "evo" keyword path down, the VPS member
bot ``darwin`` — asked "evo help" — confabulated a full fake
``/evo status|agents|config|logs|update|restart`` command table. Member bots had
only partial/stale awareness of what Evolve/Evo is, so when dispatch was
unreachable they fabricated instead of failing honestly.

This file pins the concise, accurate "what Evo is" teaching block added to the
member template so a refactor can't silently drop it, and — the load-bearing
guard — asserts the template does NOT itself ship a fabricated ``/evo`` command
table (the exact shape of darwin's confabulation, and the reason a full
subcommand map was deliberately NOT the chosen scope).

These aren't lock-in tests — the wording can change. They check the SUBSTANCE
survives edits.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATE = (
    Path(__file__).parent.parent
    / "evolve_admin" / "templates" / "bot_workspace" / "AGENTS.md"
)


@pytest.fixture(scope="module")
def agents_md() -> str:
    assert _TEMPLATE.exists(), f"{_TEMPLATE} not found"
    return _TEMPLATE.read_text(encoding="utf-8")


def test_template_is_structurally_substantive(agents_md: str):
    """content_scan structurally checks AGENTS.md (>= 1500 bytes). The member
    template must stay above the floor or a freshly-seeded bot red-flags."""
    assert len(agents_md.encode("utf-8")) >= 1500, (
        "member AGENTS.md template dropped below the 1500-byte structural "
        "floor — a freshly-seeded bot would fire content_scan_structural_anomaly."
    )


def test_evolve_teaching_section_present(agents_md: str):
    """The 'what Evolve is' section must remain. Without it the member bot has
    no baseline awareness of the layer that runs it and answers from guesswork."""
    assert 'Evolve and "evo"' in agents_md or "## Evolve" in agents_md, (
        "the Evolve/'evo' teaching section is gone from the member template. "
        "It is the member bot's only baseline awareness of what Evolve/Evo is — "
        "without it the 2026-06-25 darwin confabulation recurs."
    )
    # Evolve described as the pod-management/operations layer the operator drives.
    low = agents_md.lower()
    assert "pod-management" in low or "pod management" in low, (
        "the section no longer describes Evolve as the pod-management layer. "
        "That framing is what keeps the member bot from claiming to BE Evolve "
        "or to drive its admin tools."
    )
    assert "operator" in low, (
        "the section no longer says Evolve is operated by the human operator. "
        "Without it the member bot may offer to run Evolve operations itself."
    )


def test_evo_keyword_routing_taught(agents_md: str):
    """The member bot must learn that 'evo' routes to Evolve's assistant and that
    IT does not answer 'evo …' from its own knowledge."""
    assert "evo help" in agents_md, (
        "the template no longer cites 'evo help' — the exact phrase that "
        "triggered darwin's confabulation. Keep the concrete trigger so the "
        "model pattern-matches the keyword case."
    )
    low = agents_md.lower()
    # Routes to Evolve / Evo answers, not the member bot.
    assert "routed" in low or "routes" in low, (
        "the template no longer says an 'evo …' message is routed to Evolve. "
        "The routing fact is what tells the member bot the question isn't its "
        "to answer."
    )
    assert "your own knowledge" in low or "from your own" in low, (
        "the template no longer tells the member bot NOT to answer 'evo …' "
        "from its own knowledge. That instruction is the load-bearing line "
        "against answering as if it were Evo."
    )


def test_anti_confabulation_rule_present(agents_md: str):
    """The load-bearing anti-confabulation rule: when an 'evo …' request does
    not produce an Evolve-provided answer (path down), the bot must NOT invent
    commands/tables/capabilities — it says it couldn't reach Evolve."""
    low = agents_md.lower()
    # The negative instruction — do not invent / fabricate.
    assert "do not invent" in low or "never fabricate" in low or "do not fabricate" in low, (
        "the anti-confabulation rule no longer forbids inventing a reply when "
        "the evo path is unreachable. This is the rule the whole change exists "
        "to add — the 2026-06-25 darwin confab was exactly an invented reply."
    )
    # Must name the fabricated-command-table failure mode specifically.
    assert "command list" in low or "command table" in low or "subcommand" in low, (
        "the rule no longer names the fabricated command list / subcommand "
        "table as the forbidden output. darwin invented a full "
        "/evo status|agents|config|logs|update|restart table — the rule must "
        "call out that specific shape so the model recognizes it."
    )
    # The positive fallback — couldn't reach Evolve; try again / operator.
    assert "couldn't reach evolve" in low or "could not reach evolve" in low or (
        "reach evolve" in low
    ), (
        "the rule no longer gives the honest fallback ('couldn't reach "
        "Evolve'). A pure ban without the positive alternative leaves the "
        "model nothing to say — which is what pushes it back to fabricating."
    )


def test_no_fabricated_evo_command_table(agents_md: str):
    """NEGATIVE GUARD — the load-bearing one. The template must NOT itself ship a
    fabricated ``/evo``-command grammar. The operator deliberately scoped this to
    a concise teaching, NOT a subcommand map, precisely because a stale/invented
    subcommand list is what caused darwin's confabulation. A ``/evo`` token may
    appear ONLY in the negative ('never fabricate a /evo command list') sense.

    Concretely: forbid the exact darwin-style command tokens
    (``/evo status``, ``/evo config`` …) from appearing as if they were real
    commands, and forbid a markdown command table keyed on ``/evo``."""
    # The specific fabricated subcommands darwin invented must not appear as
    # real command syntax anywhere in the template.
    forbidden = (
        "/evo status", "/evo agents", "/evo config",
        "/evo logs", "/evo update", "/evo restart",
    )
    for tok in forbidden:
        assert tok not in agents_md, (
            f"the member template contains {tok!r} — a fabricated /evo "
            f"subcommand. This is exactly the darwin-2026-06-25 confabulation "
            f"the change exists to PREVENT; the template must not enshrine it. "
            f"Teach the SHAPE ('ask in plain language, e.g. \"evo status\"') "
            f"not an invented /evo command grammar."
        )
    # Any bare '/evo' mention (if present at all) must be in a negative context
    # on its own line — i.e. 'never fabricate a /evo command list', not a row of
    # a command-reference table.
    for line in agents_md.splitlines():
        if "/evo" in line:
            low = line.lower()
            assert ("fabricate" in low or "never" in low or "no fixed" in low
                    or "command grammar" in low or "do not" in low), (
                f"the line {line.strip()!r} mentions '/evo' but not as a "
                f"named-and-forbidden example. The template must never present "
                f"a '/evo' command as real — name it only to forbid it."
            )


def test_does_not_claim_admin_tool_access(agents_md: str):
    """A member bot is NOT the admin bot. The teaching must not tell it it can
    drive Evolve's admin tools / answer as Evolve — that would duplicate and
    contradict the admin-bot AGENTS.md and re-introduce the confabulation."""
    low = agents_md.lower()
    # Must explicitly disclaim driving Evolve / being Evolve.
    assert "not by you" in low or "you do not drive" in low or "do not drive" in low, (
        "the teaching no longer disclaims that the member bot drives Evolve. "
        "Without the disclaimer the member bot may offer to run admin "
        "operations it has no access to (a fabrication vector)."
    )
    # Guard against an accidental copy of the admin-bot's privilege framing.
    assert "you ARE the evolve/admin bot" not in agents_md, (
        "the member template contains the admin-bot's 'you ARE the "
        "evolve/admin bot' framing. That line belongs ONLY in the "
        "primary/admin AGENTS.md — a member bot is not the admin bot."
    )
