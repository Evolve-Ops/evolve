"""tests/test_evo_agents_md_guards.py — structural lint on evo's AGENTS.md.

The evolve_bot AGENTS.md is the system prompt OC loads when evo's
agent runs. It contains hard rules that govern model behavior on the
admin UI surface. This file pins a few of those rules so a refactor
can't silently drop them.

Currently guarded:

  * Reference-resolution rules covering plural pronouns ("all four",
    "those", "both") — added after a 2026-05-20 transcript where evo
    asked "all four of what?" right after rendering a four-row table.
    The rule says check the prior assistant turn FIRST before asking.

  * Chat-page report_text awareness — added in the same PR as the
    home page-context pack. Evo should treat the report banner as
    on-screen context the operator just read.

These aren't lock-in tests — the rules can be reworded as needed.
They just check that the SUBSTANCE survives edits.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

# AGENTS.md lives in the analyzer package — the evo_bot template.
_AGENTS_MD = (
    _ADMIN_PKG.parent / "analyzer" / "evolve_bot" / "AGENTS.md"
)


@pytest.fixture(scope="module")
def agents_md() -> str:
    assert _AGENTS_MD.exists(), f"{_AGENTS_MD} not found"
    return _AGENTS_MD.read_text(encoding="utf-8")


def test_agents_md_is_non_trivial(agents_md: str):
    """Sanity. Catches a refactor that empties or moves the file."""
    assert len(agents_md) > 5_000, (
        "AGENTS.md is suspiciously small — has it moved?"
    )


def test_reference_resolution_covers_plural_pronouns(agents_md: str):
    """Pronoun resolution must explicitly cover plural / collective
    forms ('all four', 'those', 'both'). The 2026-05-20 transcript
    showed evo treating 'all four' as fresh context when the answer
    was in its own prior message. The rule was rephrased to call out
    plural forms explicitly."""
    for phrase in ('"all four"', '"those"', '"both"'):
        assert phrase in agents_md, (
            f"reference-resolution rule no longer mentions {phrase} as a "
            "pronoun form. If you reworded the rule, keep the examples — "
            "the model needs concrete forms to generalize from."
        )


def test_reference_resolution_checks_prior_assistant_turn_first(
    agents_md: str,
):
    """The lookup order must put 'your own previous assistant turn'
    BEFORE 'ask for clarification'. Otherwise the model defaults to
    asking even when the answer is one message above."""
    body = agents_md
    prior_turn_pos = body.lower().find("your own previous assistant turn")
    ask_pos = body.lower().find("only if none of")
    assert prior_turn_pos > 0, (
        "reference-resolution rule no longer mentions 'your own previous "
        "assistant turn' as a lookup source. That's source #1 in the "
        "ordering — without it the rule is missing its load-bearing case."
    )
    assert ask_pos > prior_turn_pos, (
        "the 'ask for clarification' step must come AFTER the 'check "
        "prior assistant turn' step in the AGENTS.md lookup order. The "
        "model reads top-down; reversing this would default to asking."
    )


def test_chat_page_report_text_called_out(agents_md: str):
    """The Page Context section must explicitly tell evo that
    ``report_text`` on the Chat page (page="home") is the Evo's-report
    banner the operator just read — and to use that connection
    instead of asking 'which baseline?' as if from scratch."""
    assert "report_text" in agents_md, (
        "AGENTS.md no longer references report_text. The Chat page "
        "context-pack exposes the Evo's-report banner content under "
        "this field; without the AGENTS.md call-out the model won't "
        "know to read it as on-screen context."
    )
    # The actual scenario this was added to address:
    assert 'personal_bot' in agents_md.lower() or 'baseline' in agents_md.lower(), (
        "the concrete example (personal_bot's baseline / report follow-up) "
        "appears to have been removed from AGENTS.md. The example is "
        "load-bearing — without it the model is more likely to "
        "abstract-away the 'don't ask if the report told you' rule."
    )


def test_never_propose_shell_snippets_rule_present(agents_md: str):
    """The 'NEVER propose shell snippets' rule must remain. It was
    added after a 2026-05-20 transcript where evo, lacking
    action.plugin.* tools, fabricated a ``sudo -u team_bot_b python3 -c
    "..."`` snippet for the operator instead of pointing them at
    the Plugins page. The hard rule + the operations→UI map are the
    immediate guard; the action.plugin.* tools are the structural fix.
    Both should be present."""
    # Hard rule itself.
    assert "NEVER propose shell snippets" in agents_md, (
        "the 'NEVER propose shell snippets' hard rule is gone. "
        "Without it, the next operator who hits a missing-tool gap "
        "gets a ``sudo`` / ``python -c`` recommendation again."
    )
    # The forbidden patterns the rule enumerates.
    for forbidden in ("sudo", "python -c", "launchctl"):
        assert forbidden in agents_md, (
            f"the example forbidden snippet pattern {forbidden!r} was "
            f"removed from AGENTS.md. The model needs concrete forms "
            f"to generalize from."
        )
    # And the explicit 'no elevated exec' guard.
    assert "elevated exec access" in agents_md, (
        "AGENTS.md no longer mentions 'elevated exec access' as a "
        "fabricated remedy to refuse. The 2026-05-20 transcript had "
        "evo asking for this directly; the rule should call it out "
        "by name so the model doesn't reach for it again."
    )


def test_common_operations_ui_map_present(agents_md: str):
    """The 'Common operations → admin UI surface' map is the guided
    walk-through evo uses when a tool isn't available. Removing it
    means evo falls back to inventing nav paths or shell snippets."""
    assert "Common operations" in agents_md, (
        "AGENTS.md no longer has the 'Common operations' map. The "
        "fallback path when a tool doesn't exist becomes a guess."
    )
    # The map must cover plugin enable/disable specifically — that's
    # the operation that motivated this work.
    assert "Plugins" in agents_md and (
        "Enable" in agents_md or "enable" in agents_md.lower()
    ), (
        "the operations map no longer covers plugin enable/disable. "
        "That was the worked example from the 2026-05-20 transcript "
        "— the one operators most likely hit."
    )
    # And the 'log a tool gap' fallback for genuinely-unsupported
    # operations.
    assert "action.evo.log_tool_gap" in agents_md, (
        "the log-a-tool-gap fallback was removed. Without it the "
        "model falls back to shell snippets when nothing else fits."
    )


def test_audit_readability_worked_example_present(agents_md: str):
    """The .zshrc-unreadable worked example must remain. It teaches
    evo that audit-readability issues are deploy/ACL problems
    (action.bot.repair_acls), NOT chmod-o+r problems. A second
    2026-05-20 transcript showed evo making exactly this wrong call;
    the worked example is the structural fix."""
    assert "action.bot.repair_acls" in agents_md, (
        "AGENTS.md no longer references action.bot.repair_acls. "
        "Without that signpost, the model falls back to chmod "
        "snippets (the 2026-05-20 transcript's actual failure)."
    )
    # The wrong-answer example must be present — concrete forms
    # teach better than abstract rules.
    assert "chmod o+r" in agents_md or "world-read" in agents_md, (
        "the worked example no longer calls out 'chmod o+r' / "
        "'world-read' as the WRONG fix. The contrast (right vs wrong) "
        "is what makes the rule actionable."
    )
    # The principle should be stated explicitly.
    assert (
        "audit-readability" in agents_md.lower()
        or "deploy-time" in agents_md
    ), (
        "the underlying principle (audit-readability problems are "
        "deploy/ACL problems) appears to have been removed. The "
        "model needs the abstraction, not just the one example."
    )


def test_in_process_proposals_worked_example_present(agents_md: str):
    """The In Process tab worked example must remain. It teaches evo
    that pod_state.proposals.pending isn't the only place proposals
    live — applied-status with manual-completion action_kind sit in
    the In Process tab and need .in_process + .mark_complete. Without
    this, evo falls back to 'I don't see it' when the operator is
    looking at the In Process tab (the 2026-05-20 transcript)."""
    assert "pod_state.proposals.in_process" in agents_md, (
        "AGENTS.md no longer references pod_state.proposals.in_process. "
        "Without this signpost the model falls back to .pending and "
        "comes up empty when the operator's on the In Process tab."
    )
    # Worked-example anchor — mentions the tab name and the
    # complementary tool path.
    assert "In Process" in agents_md, (
        "AGENTS.md no longer mentions the 'In Process' tab by name. "
        "Concrete forms beat abstract rules; keep the example anchored."
    )
    assert "mark_complete" in agents_md, (
        "AGENTS.md no longer references action.proposal.mark_complete. "
        "That's the closing-out tool for in-process proposals — without "
        "it evo can find them but doesn't know how to close them."
    )


def test_diagnostic_shell_ban_present(agents_md: str):
    """The original shell-snippet ban (#1347) focused on ``sudo`` /
    ``python -c`` / ``launchctl`` style commands — the *destructive*
    cases. A 2026-05-20 transcript showed evo defaulting to ``find``
    snippets for *diagnostic* shell, which doesn't change anything
    but is still a shell snippet for the operator to copy-paste.
    The reinforced rule must call out diagnostic shell explicitly so
    the model doesn't read the ban as 'no destructive shell'."""
    assert "Diagnostic shell is shell too" in agents_md, (
        "the 'Diagnostic shell is shell too' rule is missing. Without "
        "it, the model interprets the shell-snippet ban as covering "
        "only destructive cases (sudo / chmod / launchctl) and falls "
        "back to find / ls / cat for diagnosis."
    )
    # Must enumerate at least one diagnostic command type so the
    # model has a concrete form.
    diagnostic_forms = ("find", "ls", "cat", "grep", "stat")
    found_any = [f for f in diagnostic_forms if f"``{f}``" in agents_md]
    assert found_any, (
        f"the diagnostic-shell ban doesn't enumerate any forms "
        f"({diagnostic_forms}). The model needs concrete examples."
    )
    # And the explicit 'exec is denied in my security context' guard.
    assert "exec is denied" in agents_md.lower(), (
        "AGENTS.md no longer mentions 'exec is denied' as a fabricated "
        "remedy. Two 2026-05-19/20 transcripts had evo using variants "
        "of this phrasing — the rule should call it out so the model "
        "doesn't reach for it again."
    )


def test_scan_status_worked_example_in_operations_table(agents_md: str):
    """The operations table must include a row pointing at
    pod_state.app_scan_status + action.bot.rescan_apps for the
    scan_needed chip case. Without it, the model lacks the obvious
    'use this instead of find' signpost."""
    assert "pod_state.app_scan_status" in agents_md, (
        "AGENTS.md no longer references pod_state.app_scan_status. "
        "The model has no signpost for the scan_needed diagnostic "
        "path and may fall back to shell."
    )
    assert "action.bot.rescan_apps" in agents_md, (
        "AGENTS.md no longer references action.bot.rescan_apps. "
        "Without it, evo can diagnose but can't act on the chip."
    )
    # Negative anchor — the forbidden ``find`` snippet should appear
    # as a *don't do this* in the row.
    assert "find /Users/" in agents_md, (
        "the 'NOT find /Users/...' contrast was removed from the "
        "operations table. Without naming the wrong answer explicitly, "
        "the model is more likely to drift back to it."
    )


def test_shell_exec_principle_is_stated(agents_md: str):
    """The shell-exec fabrication rule must be stated as a *principle*,
    not a deny-list of specific phrasings. Three variants have shown up
    in transcripts over five days ('elevated exec access', 'exec is
    denied in my security context', 'exec is denied in this session')
    — each one snuck through a phrase-based ban. The principle ('there
    is no shell-exec permission tier') generalizes across phrasings."""
    # The principle statement itself.
    assert "no shell-exec permission tier" in agents_md, (
        "AGENTS.md no longer states 'no shell-exec permission tier' as "
        "the principle. Without the principle, the model treats the "
        "rule as a list of phrases to avoid — and three transcripts in "
        "five days show that approach failing (new phrasings slip "
        "through). State the underlying claim instead."
    )


def test_shell_exec_principle_enumerates_known_variants(agents_md: str):
    """The principle section must include all four known transcript
    variants as concrete examples. Concrete forms teach better than
    abstract rules — and showing the model that a pattern recurs across
    different surfaces (plugin toggle / recs / backup / cost drawer)
    makes the fabrication-pattern claim load-bearing.

    Updated 2026-05-23 to expect the fourth variant ('locked down') —
    see diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md."""
    for variant in (
        "elevated exec access",
        "Exec is denied in my security context",
        "Exec is denied in this session",
        "Exec is locked down in this session context",
    ):
        assert variant in agents_md, (
            f"the principle section no longer cites the {variant!r} "
            f"transcript variant. All four should remain — they're the "
            f"existence proof that this is a recurring pattern, not a "
            f"one-off phrasing to ban."
        )


def test_exec_locked_down_variant_in_enumeration(agents_md: str):
    """The 2026-05-23 'locked down' synonym is structurally distinct
    from the three prior 'denied' variants — same fabrication shape,
    different lexical token. The enumeration must include it
    explicitly so the model has a concrete same-shape example. See
    diagnosis-exec-locked-down-fabrication-variant-2026-05-23.md
    (PR #1480) for the recurrence-cadence rationale."""
    assert "Exec is locked down in this session context" in agents_md, (
        "the principle section no longer cites the 2026-05-23 'locked "
        "down' variant. This is the fourth recurrence in nine days; "
        "without the enumeration row the model loses the concrete "
        "example tying 'locked down' to the same fabrication pattern."
    )
    # The synonym-drift framing should also be present — otherwise the
    # enumeration reads as exhaustive and the model treats anything
    # outside the list as fine.
    assert "synonym drift" in agents_md or "synonym" in agents_md, (
        "AGENTS.md no longer warns that phrase enumeration cannot keep "
        "up with synonym drift. Without this framing the model treats "
        "the four-row enumeration as exhaustive — and 'gated' / "
        "'walled off' / 'sealed' slip through."
    )


def test_no_shell_exec_principle_acknowledges_real_config(agents_md: str):
    """The original principle (PR #1428) claimed *'that category of
    capability does not exist in the architecture'*. The 2026-05-23
    diagnosis (PR #1480) showed this is literally untrue — the OC
    config field ``tools.exec.security`` IS a real session-security
    gate. The model can verify the field exists, so the literal-claim
    contradiction lets it read the principle as guidance about phrasing
    only.

    The rewritten principle must acknowledge ``tools.exec.security``
    as real backend plumbing AND redirect — *never preface an
    operator-facing reply with the state of that backend config*."""
    # The literal-falsity loophole is gone.
    assert "category of capability does not exist" not in agents_md, (
        "AGENTS.md still claims 'that category of capability does not "
        "exist in the architecture'. PR #1480 showed this is literally "
        "untrue — tools.exec.security is a real OC config field. "
        "Replace the literal claim with an accurate-but-redirecting "
        "framing (the field is real but the operator doesn't need to "
        "hear about it)."
    )
    # And the redirect — `tools.exec.security` must be named so the
    # model knows the principle is acknowledging the real field, not
    # asserting it doesn't exist.
    assert "tools.exec.security" in agents_md, (
        "the principle no longer names ``tools.exec.security`` as the "
        "real backend config. Without that anchor the model can't "
        "tell whether the principle is denying the field's existence "
        "(the old, literally-false framing) or redirecting around it "
        "(the new framing)."
    )
    # The redirect's actionable rule must be present. The Markdown
    # wraps mid-phrase, so normalize whitespace before the substring
    # check.
    normalized = " ".join(agents_md.split())
    assert "Never preface an operator-facing reply" in normalized, (
        "the actionable rule ('Never preface an operator-facing reply "
        "with the state of that backend config') is missing. Without "
        "it, the principle's redirect is decorative — there's no "
        "operator-facing instruction the model can apply."
    )


def test_positive_frame_no_tool_reply_rule_present(agents_md: str):
    """Per PR #1480's recommended fix (Q4 option 3), the principle
    must include a positive-frame rule: 'when you don't have a tool,
    the reply is "this isn't a tool I have", not "this capability is
    denied".' Positive frames fill better than negative bans — the
    model is better at picking from {tool / UI path / log-a-gap} than
    at avoiding an open-ended synonym pool."""
    # The rule's headline phrase.
    assert "Positive-frame rule" in agents_md, (
        "AGENTS.md no longer states the 'Positive-frame rule' by name. "
        "Without the named rule the principle is back to being a "
        "synonym deny-list (which we've already shown doesn't work)."
    )
    # The core dichotomy — what to offer vs what's been taken.
    assert "what you can offer" in agents_md, (
        "the positive-frame rule no longer mentions 'what you can "
        "offer' as the right shape. Concrete forms teach better than "
        "abstract rules — the model needs the slot to fill."
    )
    assert "what's been taken from you" in agents_md, (
        "the positive-frame rule no longer mentions 'what's been "
        "taken from you' as the WRONG shape. The contrast (right vs "
        "wrong shape) is what makes the rule actionable."
    )
    # The fabrication-pattern-adjacent framing — the rule must name
    # why this matters even when the underlying claim is technically
    # accurate.
    assert "fabrication-pattern adjacent" in agents_md, (
        "the rule no longer flags negative-frame replies as "
        "'fabrication-pattern adjacent'. Without this framing the "
        "model treats accurate-but-irrelevant capability reports as "
        "fine — which was exactly the 2026-05-23 failure shape."
    )


def test_verify_preconditions_before_recommending_rule_present(agents_md: str):
    """Per PR #1479's recommended fix (a), AGENTS.md must include a
    hard rule symmetric to 'Post-action verify' — verify preconditions
    BEFORE recommending an action. The 2026-05-23 transcript had evo
    recommending ``sudo cp /tmp/security_bot-jobs-patched.json …`` from a
    2-day-old assertion without verifying the /tmp file still
    existed. The pre-action verify rule closes that gap.

    See diagnosis-evo-recalls-prior-turns-as-ground-truth-2026-05-23.md."""
    # The rule's headline phrase.
    assert "verify preconditions BEFORE recommending" in agents_md, (
        "AGENTS.md no longer states the 'verify preconditions BEFORE "
        "recommending' hard rule. Without it the 'Cite the tool' rule "
        "(which covers claims) and the 'Post-action verify' rule "
        "(which covers AFTER an action) leave a gap at "
        "'recommendation that implicitly depends on a stale "
        "assertion' — exactly the 2026-05-23 failure shape."
    )
    # The smoking-gun phrases the rule names — when the model writes
    # any of these, that's the signal it should stop and call a tool.
    for trigger in ('"unless that changed"', '"if it still exists"',
                    '"assuming it hasn\'t been reverted"'):
        assert trigger in agents_md, (
            f"the rule no longer names {trigger} as a 'stop, the rule "
            f"is firing' trigger phrase. The 2026-05-23 transcript "
            f"opened with exactly this hedge shape ('if /tmp was "
            f"cleared since Wednesday'). Concrete trigger phrases "
            f"teach better than the abstract rule alone."
        )
    # The 'memory of what you said vs proof what you said is still
    # true' distinction — the conceptual hook for the rule.
    assert "memory of what you said" in agents_md, (
        "the rule no longer states the 'memory of what you said, not "
        "proof that what you said is still true' framing. This is the "
        "load-bearing distinction (per the diagnosis) — without it "
        "the rule reads as one more verify-this-before-that and "
        "doesn't generalize to the conversation-history class."
    )
    # The rule should be placed symmetrically with Post-action verify
    # (the temporal pre-action counterpart). The doc should call out
    # the symmetry so a future edit doesn't accidentally split them.
    assert "symmetric pre-action counterpart" in agents_md, (
        "the rule no longer flags itself as 'the symmetric pre-action "
        "counterpart' to Post-action verify. The symmetry is the "
        "structural reason the rule belongs adjacent to it — without "
        "the callout the two rules can drift apart in future edits."
    )


def test_accept_drift_operations_row_present(agents_md: str):
    """A 2026-05-20 transcript showed evo confabulating that 'accept
    as baseline' (backup-drift) maps to per-finding Mute (audit) —
    same Security page, totally different mechanisms. The operations
    map must name action.security.accept_drift + pod_state.config_drift
    explicitly so the model has a signpost away from the Mute path."""
    assert "action.security.accept_drift" in agents_md, (
        "AGENTS.md no longer references action.security.accept_drift. "
        "Without it, the model falls back to either Mute (wrong "
        "mechanism) or 'audit.py --reset-baselines' (wrong baseline) "
        "for the 'accept the drift' operator request."
    )
    assert "pod_state.config_drift" in agents_md, (
        "AGENTS.md no longer references pod_state.config_drift. "
        "Without the enumeration tool, the model can't answer "
        "'accept ALL configs as baseline' without guessing which "
        "bots have drift."
    )


def test_backup_baseline_vs_audit_mute_disambiguation(agents_md: str):
    """The worked example contrasting backup-baseline drift (accept_drift)
    against audit-finding Mute must remain. This was the load-bearing
    teaching for the 2026-05-20 transcript — without the contrast,
    the model treats them as interchangeable on the same Security page."""
    md_lower = agents_md.lower()
    assert "backup-baseline" in md_lower, (
        "AGENTS.md no longer uses 'backup-baseline' as a distinct "
        "concept name. The contrast against audit-Mute needs explicit "
        "vocabulary to be teachable."
    )
    # Both 'Mute' and 'accept_drift' should appear so the model has
    # both sides of the distinction.
    assert "Mute" in agents_md, (
        "AGENTS.md no longer mentions Mute — without the other side "
        "of the contrast, the disambiguation is half-stated."
    )
    # And the third (CLI-only) baseline path must be explicitly ruled
    # out — that's what the 2026-05-20 transcript suggested as a
    # wrong-answer alternative.
    assert "reset-baselines" in agents_md or "reset_baselines" in agents_md, (
        "AGENTS.md no longer rules out 'audit.py --reset-baselines' as "
        "the wrong path. The 2026-05-20 transcript had evo suggesting "
        "exactly this; without naming it as wrong, it'll come back."
    )


def test_backup_operations_row_present(agents_md: str):
    """The 'back up X now' operations-table row must remain. It signposts
    ``action.bot.backup_workspace`` (trigger) and ``pod_state.backup_status``
    (inspect) — added 2026-05-19 in response to a transcript where evo
    fabricated a permission-tier excuse rather than calling either tool
    (because neither existed yet)."""
    assert "action.bot.backup_workspace" in agents_md, (
        "AGENTS.md no longer references action.bot.backup_workspace. "
        "Without that signpost, the model falls back to either "
        "fabricating a shell snippet (the bug this tool was meant to "
        "fix) or claiming the capability doesn't exist."
    )
    assert "pod_state.backup_status" in agents_md, (
        "AGENTS.md no longer references pod_state.backup_status. "
        "It's the verify_via for backup_workspace AND the answer to "
        "'is team_bot_a backed up?' — without the signpost, the model "
        "doesn't know inspection is read-only and tool-shaped."
    )
    # Schedule fact + launchd label — both are concrete details that
    # ground evo's reply in reality.
    assert (
        "02:00" in agents_md
        or "ai.evolve" in agents_md  # the launchd label namespace
    ), (
        "the backup operations row no longer mentions the schedule "
        "(02:00) or the launchd label namespace (ai.evolve.<bot>.backup). "
        "Concrete numbers + identifiers help the model cite the right "
        "facts when an operator asks 'when does the nightly run?'"
    )


def test_chip_teaching_rows_present(agents_md: str):
    """The three high-frequency tile chips — ``unexpected_billing``,
    ``high_correction``, ``cost_spike`` — must each have an operations-
    table row that names at least one concrete tool. Added in response
    to the May 2026 evo coverage audit: these chips fire often on the
    Dashboard and operators ask "why is X firing?" — without the
    teaching row evo defaults to generic chip-glossary explanations
    instead of calling the diagnostic tools.

    Each row must:
      1. Mention the chip name verbatim (so the model pattern-matches
         on it).
      2. Mention at least one tool name (so the model has a concrete
         next step rather than a prose explanation).
    """
    # Tool names that the rows MAY reference. The test passes when each
    # chip name appears AND the surrounding text mentions at least one
    # of these tools. We check for any tool name on the same line as
    # the chip — that pins the row's shape without being brittle to
    # exact phrasing.
    tool_names = (
        "pod_state.bots",
        "pod_state.usage",
        "pod_state.audit",
        "pod_state.proposals.pending",
    )
    for chip in ("unexpected_billing", "high_correction", "cost_spike"):
        assert chip in agents_md, (
            f"the operations table no longer mentions the {chip!r} chip "
            f"by name. Concrete chip names are load-bearing — the "
            f"model needs to pattern-match on the chip id to find the "
            f"right teaching row."
        )
        # Find lines containing this chip and confirm at least one
        # references a tool. Markdown table rows are one line each, so
        # line-level checking matches the row shape.
        chip_lines = [
            line for line in agents_md.splitlines()
            if chip in line
        ]
        assert chip_lines, (
            f"chip {chip!r} found but no full line contains it — "
            f"unexpected structure. Update this test."
        )
        # At least one chip-mentioning line must also reference a tool.
        rows_with_tool = [
            line for line in chip_lines
            if any(tool in line for tool in tool_names)
        ]
        assert rows_with_tool, (
            f"the {chip!r} chip is mentioned but no row about it "
            f"references any of the expected tools "
            f"({tool_names}). Without a tool name on the row, the "
            f"teaching is incomplete — the model has no concrete next "
            f"step and falls back to a glossary-style explanation."
        )


def test_high_correction_row_describes_signal_correctly(agents_md: str):
    """The ``high_correction`` chip teaching row must describe the
    signal accurately — and specifically must NOT reinstate the older,
    inverted framing where evo treated the chip as bot self-revision /
    model hedging and pointed at ``pod_state.audit`` for "correction-
    trigger findings".

    The signal is **user-initiated**: a plain substring match in
    ``packages/plugin/src/observer/TierClassifier.ts`` (CORRECTION_
    PATTERNS) against the user's incoming message, pointed at the
    bot's prior reply. The bot's own output is not inspected. The
    security ``pod_state.audit`` tool has nothing to do with it.

    Without this guard, the row regresses to a sound-alike
    interpretation — "the bot is over-correcting itself, run audit
    for detail" — which sends the model down the wrong diagnostic
    path on a chip that fires often on the Dashboard.
    """
    # Find the operations-table row for this chip. There may be other
    # mentions (e.g. the glossary section appended from glossary.yaml),
    # but the operations table is the line containing "Diagnose" — that's
    # the workflow row this guard pins.
    diagnose_lines = [
        line for line in agents_md.splitlines()
        if "high_correction" in line and "Diagnose" in line
    ]
    assert diagnose_lines, (
        "no operations-table row for high_correction found. The row "
        "must start with 'Diagnose / explain a firing high_correction "
        "chip' — see test_chip_teaching_rows_present for the row's "
        "general shape."
    )
    row = diagnose_lines[0]

    # Positive: the row must say the signal is user-initiated, in some
    # form the model will pattern-match on. We accept either the
    # explicit "user-initiated" phrasing or wording naming the user's
    # message as the source.
    assert "user-initiated" in row or "user's incoming message" in row, (
        "the high_correction row no longer states the signal is "
        "user-initiated. Without that framing the model defaults to "
        "the older 'bot is over-correcting itself' interpretation — "
        "the exact inversion this guard catches."
    )

    # Positive: name at least one of the actual CORRECTION_PATTERNS
    # phrases so the model can ground its explanation in real strings,
    # not paraphrased guesses.
    pattern_examples = (
        "no, i meant", "you misunderstood", "that's wrong",
        "try again", "you didn't", "still not",
    )
    assert any(p in row for p in pattern_examples), (
        "the high_correction row no longer cites any of the actual "
        "CORRECTION_PATTERNS phrases. Citing the literal triggers "
        "keeps the model from inventing plausible-sounding ones."
    )

    # Negative: the row must NOT route the operator at ``pod_state.audit``
    # as a *next step*. ``pod_state.audit`` may still be mentioned as a
    # NEGATIVE example ("NOT pod_state.audit"), but the row should never
    # say to *use* it. We approximate this by requiring that any mention
    # of pod_state.audit is paired with a NOT / negation token on the
    # same row.
    if "pod_state.audit" in row:
        assert ("NOT ``pod_state.audit``" in row
                or "not surface" in row.lower()
                or "does not" in row.lower()), (
            "the high_correction row mentions pod_state.audit but not "
            "as a negative example. The 2026-05 inversion sent the "
            "model into the security audit looking for correction "
            "findings that don't exist there."
        )

    # Negative: the older incorrect framings must NOT reappear.
    forbidden_phrases = (
        "over-correcting itself",
        "bot revises or walks back",
        "model uncertainty",
        "correction-trigger findings",
    )
    for phrase in forbidden_phrases:
        assert phrase not in row, (
            f"high_correction row reintroduces the inverted framing "
            f"({phrase!r}). The chip measures the *user* pushing back, "
            f"not the bot self-revising — see "
            f"packages/plugin/src/observer/TierClassifier.ts:74-86 for "
            f"the CORRECTION_PATTERNS the user's message is matched "
            f"against."
        )


def test_infra_daemon_restart_operations_row_present(agents_md: str):
    """The infra-daemon-restart operations-table row must remain. It
    signposts ``action.infra.daemon_restart`` — added to close the
    May 2026 coverage gap where the ``infra_daemon_down`` chip is
    critical-severity but evo could only say "SSH in" because there
    was no tool to act."""
    assert "action.infra.daemon_restart" in agents_md, (
        "AGENTS.md no longer references action.infra.daemon_restart. "
        "Without that signpost, the model falls back to telling the "
        "operator to SSH when infra_daemon_down fires — the failure "
        "mode this tool was added to close."
    )
    # The chip name must appear as the trigger phrase the model
    # pattern-matches on.
    assert "infra_daemon_down" in agents_md, (
        "AGENTS.md no longer mentions the infra_daemon_down chip by "
        "name. The teaching row must tie the chip to the tool — "
        "without the chip name, the model doesn't know when to reach "
        "for the tool."
    )


def test_reference_resolution_covers_bare_letter_and_option_forms(
    agents_md: str,
):
    """Bare single-token references ("C", "1", "Option B", "first")
    must be enumerated alongside the plural pronouns. The 2026-05-21
    transcript (#1402) showed evo asserting "fresh session" when the
    operator typed "can you do C" referring to an Option A/B/C list
    in evo's prior turn. A model weighting "hard rule" framing
    literally can read the bare letter as outside the rule's scope
    unless it's named explicitly."""
    for phrase in ('"C"', '"Option A"', '"the second one"'):
        assert phrase in agents_md, (
            f"reference-resolution rule no longer mentions {phrase} "
            f"as a bare-token form. The 2026-05-21 transcript hit "
            f"exactly this case — without the explicit examples, the "
            f"model falls back to 'fresh session' disavowal."
        )


def test_fresh_session_claim_explicitly_forbidden(agents_md: str):
    """A hard rule must explicitly forbid the model from asserting
    "this is a fresh session" or "I have no prior context" when the
    conversation history above the prompt shows prior turns. Without
    this prohibition, the model has no taught counter-pressure to
    its strongest evasion phrase when confronted with a bare
    follow-up it can't parse."""
    body = agents_md
    # The literal phrase the model defaults to — must appear as a
    # FORBIDDEN claim, not as a permitted state.
    assert "fresh session" in body, (
        "AGENTS.md no longer mentions 'fresh session' anywhere. The "
        "#1402 follow-up rule turns the model's strongest evasion "
        "phrase into a named-and-forbidden failure mode; dropping "
        "the phrase removes the teaching."
    )
    # The rule must tie the prohibition to the session-context block's
    # user_turn_count / turn-N signal — that's the in-prompt anchor
    # the model is supposed to trust over its own reflex.
    assert "user_turn_count" in body or "turn N" in body, (
        "AGENTS.md no longer references the session-context signal "
        "(user_turn_count / turn N) that grounds the fresh-session "
        "prohibition. Without that anchor the rule is abstract — the "
        "model needs to know WHICH field to read for authoritative "
        "thread-continuity state."
    )
    # The model is told to scroll back rather than disavow.
    assert "scroll back" in body.lower(), (
        "AGENTS.md no longer tells the model to 'scroll back' when "
        "the session-context shows prior turns. That instruction is "
        "the load-bearing positive direction — the rule isn't just "
        "'don't say fresh session' but 'instead, use the context "
        "you have'."
    )


def test_oc_slash_command_rule_present(agents_md: str):
    """Fix 3 of the 2026-05-21 exec-approval-leak diagnosis: AGENTS.md
    must explicitly forbid asking the operator to type ``/approve``,
    ``/grant``, or any OC slash-command into chat. The transcript that
    surfaced this leak had evo asking the operator to type four
    ``/approve <id>`` commands; without an AGENTS.md rule naming the
    failure mode, the future model can rationalize the same pivot
    again."""
    # The literal failure-mode phrasing the future model needs to
    # pattern-match on.
    assert "/approve" in agents_md, (
        "AGENTS.md no longer references ``/approve`` by name. The "
        "rule must use the literal token the model would pattern-match "
        "on to recognize the forbidden ask. See docs/diagnosis-evo-"
        "exec-approval-leak-2026-05-21.md."
    )
    assert "/grant" in agents_md, (
        "AGENTS.md no longer references ``/grant`` — the rule must "
        "enumerate the OC slash-command vocabulary so it generalizes "
        "beyond ``/approve``."
    )
    assert "slash-command" in agents_md, (
        "AGENTS.md no longer uses ``slash-command`` as the structural "
        "category. The principle is 'no OC slash-commands routed "
        "through chat' — phrase-only bans (``/approve``-only) miss "
        "future variants."
    )
    # Must tie the rule to the existing "no shell-exec permission
    # tier" principle so the model treats them as the same forbidden
    # category.
    body = agents_md
    no_tier_pos = body.find("no shell-exec permission tier")
    approve_pos = body.find("/approve")
    assert no_tier_pos > 0 and approve_pos > 0, (
        "either the shell-exec principle or the /approve rule moved; "
        "both must remain so they're co-located teaching."
    )
    # And the corrective: name the three right moves (different tool,
    # admin-UI surface, log_tool_gap) so the rule isn't just "don't
    # do X" but tells the model what to do instead.
    assert "action.evo.log_tool_gap" in agents_md, (
        "the /approve rule must reference action.evo.log_tool_gap as "
        "the fallback when neither a sibling tool nor an admin-UI "
        "surface covers the request — same shape as the other "
        "fabrication-pattern rules."
    )


def test_help_style_decision_tree_present(agents_md: str):
    """Phase 1 of the surface-aware help-style spec adds the four-rung
    escalation as a top-level section (§5a). The rungs must each be
    named so the model can pattern-match on the structure."""
    # Section title — anchor the surgery
    assert "Help style — the four-rung escalation" in agents_md, (
        "AGENTS.md no longer has the 'Help style — the four-rung "
        "escalation' section. Phase 1 of the surface-aware help-style "
        "spec (docs/spec-surface-aware-help-style-2026-05-22.md §5a) "
        "added it as the operator-facing decision framework."
    )
    # Each rung must be named so the model can route by rung number
    for rung in (
        "Rung 1 — Just do it",
        "Rung 2 — Walk to the UI button",
        "Rung 3 — CLI guidance",
        "Rung 4 — Log a tool gap",
    ):
        assert rung in agents_md, (
            f"the rung {rung!r} is missing from the decision tree. "
            f"All four rungs must be named — the model picks by rung "
            f"number, removing any one collapses the framework."
        )
    # The three surface defaults must be enumerated so the model can
    # pick the right rung for the operator's actual surface.
    for surface in ("admin_ui / mobile", "admin_ui / laptop", "telegram"):
        assert surface in agents_md, (
            f"the surface {surface!r} is no longer enumerated in the "
            f"help-style framework. The model needs all three v1 "
            f"surfaces named to pick the right rung."
        )


def test_shell_snippet_rule_is_surface_conditional(agents_md: str):
    """Phase 1 (§5d) rewrites the shell-snippet hard rule as
    surface-conditional. The 'NEVER' wording is preserved (admin_ui
    is still the dominant chat surface and 'never on admin_ui' is the
    default) but the rule must explicitly enumerate the three
    surfaces so the future model picks the right policy per turn."""
    body = agents_md
    # The original rule's lede stays — it's still hard
    assert "NEVER propose shell snippets" in body, (
        "the 'NEVER propose shell snippets' hard rule was renamed away. "
        "Phase 1 keeps the 'NEVER' framing — only the body becomes "
        "surface-conditional (mobile=never, laptop=avoid, telegram=ok+verified)."
    )
    # The shell-snippet rule itself must enumerate the three surface
    # cases. Locate the rule's body and search within it (not the whole
    # file — other sections also mention surface names).
    rule_idx = body.index("NEVER propose shell snippets")
    rule_body = body[rule_idx:rule_idx + 4000]
    for surface_token in (
        "Surface: admin_ui / mobile",
        "Surface: admin_ui / laptop",
        "Surface: telegram",
    ):
        assert surface_token in rule_body, (
            f"the shell-snippet rule no longer mentions {surface_token!r}. "
            f"Each surface needs its policy named explicitly within the "
            f"rule body — the model reads top-down and the rule is wrong "
            f"if the operator's surface isn't one of the named cases."
        )
    # The mobile policy must be the absolute 'NEVER' — locate the
    # mobile clause within the rule body, not the surface-awareness
    # section that also names the surfaces.
    mobile_section_start = rule_body.index("Surface: admin_ui / mobile")
    # Within ~600 chars after the mobile heading, NEVER must appear
    assert "NEVER" in rule_body[mobile_section_start:mobile_section_start + 600], (
        "the mobile policy must contain 'NEVER' near its lede. Mobile "
        "is the surface ceiling — no preference override unlocks it."
    )


def test_command_reference_is_surface_conditioned(agents_md: str):
    """Phase 1 (§5b) puts a 'Surface-conditioned' preface on the
    Command Reference so the model treats it as Telegram-only
    reference material, not as the admin-UI recommendation map."""
    body = agents_md
    cmd_ref_idx = body.find("## Command Reference")
    assert cmd_ref_idx > 0, "Command Reference section is missing"
    # The preface must appear in the first ~800 chars after the heading
    preface_window = body[cmd_ref_idx:cmd_ref_idx + 800]
    assert "Surface-conditioned" in preface_window, (
        "the Command Reference section no longer has the "
        "'Surface-conditioned.' preface block introduced in Phase 1 "
        "(§5b). Without it, the model reads the legacy CLI workflows "
        "as the canonical admin-UI recommendation map."
    )
    # The preface must explicitly name 'admin_ui' and 'Telegram' so
    # the surface gating is unambiguous
    assert "admin_ui" in preface_window or "admin-UI" in preface_window, (
        "the Command Reference preface doesn't mention admin_ui — the "
        "surface it's gating against. The rule reads as toothless."
    )


def test_privilege_boundary_is_surface_conditioned(agents_md: str):
    """Phase 1 (§5c) similarly gates the Privilege Boundary section
    with a 'Surface-conditioned' preface. The bullet content
    (CAN/CANNOT) stays unchanged — it's still the underlying
    capability map — but the framing tells the model not to read it
    as 'things to emit as chat-text shell snippets'."""
    body = agents_md
    pb_idx = body.find("## Privilege Boundary")
    assert pb_idx > 0, "Privilege Boundary section is missing"
    preface_window = body[pb_idx:pb_idx + 800]
    assert "Surface-conditioned" in preface_window, (
        "the Privilege Boundary section no longer has the "
        "'Surface-conditioned.' preface introduced in Phase 1 (§5c). "
        "Without it, the CAN/CANNOT bullets read as a checklist of "
        "shell snippets to emit on the admin UI."
    )


def test_admin_ui_closing_summary_carve_out_present(agents_md: str):
    """Phase 1 (§5e) adds a carve-out to the 'never summarize what you
    just did' rule for admin-UI surfaces. The proxy can't reliably
    surface mid-loop assistant text to the operator, so admin-UI
    turns that emit tool calls MUST close with a text turn naming
    what changed. Without the summary, the operator sees an empty
    reply (the failure the empty-reply diagnosis describes)."""
    body = agents_md
    # The carve-out must reference the admin_ui surface and the
    # tool-batch trigger condition
    assert 'surface == "admin_ui"' in body, (
        "AGENTS.md no longer carves out closing summaries for "
        'surface == "admin_ui"'". Phase 1 of the surface-aware "
        "help-style spec (§5e) adds this — without it the "
        "'never summarize' rule overrides on admin_ui and the loop "
        "exits silently after a tool batch."
    )
    # The diagnosis reference is the breadcrumb back to root cause
    assert "diagnosis-empty-reply" in body, (
        "the carve-out no longer references the empty-reply diagnosis. "
        "The model needs the breadcrumb — without it, a future "
        "revision can plausibly delete the rule as redundant."
    )


def test_cost_measures_operations_row_present(agents_md: str):
    """Phase 1 (§5f) adds a Common operations row for Cost Optimization
    page TTL editing. Without this row the model defaults to either
    a fabricated `sed` snippet (the schema-coupling failure
    documented in PR #1438) or 'I don't know how to do that'."""
    body = agents_md
    # Both the page name and the tool gap must be referenced
    assert "Cost Optimization page" in body, (
        "the Common operations table no longer points at the Cost "
        "Optimization page for TTL / behavior-config edits. The "
        "model has no signpost and falls back to shell."
    )
    # The TTL keyword anchors the operator's likely phrasing
    assert "context-pruning TTL" in body or "contextPruning.ttl" in body, (
        "the row no longer mentions context-pruning TTL — the most "
        "common operator phrasing for the field this row signposts. "
        "Without it, the model doesn't pattern-match on 'raise team_bot_a's "
        "TTL' onto this operations row."
    )


def test_sequential_apply_rule_present(agents_md: str):
    """Phase 1 sidesteps OC's session-jsonl file-lock contention by
    teaching evo to apply proposals one at a time, not in parallel.
    See diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md
    Priority 4 + spec §8."""
    body = agents_md
    assert "Apply proposals one at a time" in body, (
        "AGENTS.md no longer instructs evo to apply proposals "
        "sequentially. Without this rule, the model races OC's "
        "session-jsonl write lock with 9+ parallel tool calls and "
        "the loop terminates mid-flight (the empty-reply diagnosis)."
    )
    # The mechanism must be named so a future model understands WHY
    # the rule exists
    assert "session-jsonl write lock" in body or "file-lock" in body, (
        "the rule doesn't name the underlying mechanism (OC's "
        "session-jsonl write lock). Without the why, a future model "
        "might rationalize parallel applies as 'faster'."
    )


def test_never_invent_prior_turns_rule_present(agents_md: str):
    """Companion to the fresh-session rule (#1402 follow-up): when
    the operator pushes back ("you just gave me three options"), the
    failure mode isn't only disavowal — it's also confabulating
    alternative prior turns to rationalize present confusion. The
    2026-05-21 transcript had evo invent "is evo overloaded?" as the
    supposed prior question. The rule must forbid the inversion."""
    body = agents_md.lower()
    # The rule must mention "invent" / "fabricate" / "make up" prior
    # turns specifically (not just the existing 'never claim an action
    # you can't see' rule, which covers a different surface).
    assert (
        "invent prior turns" in body
        or "fabricate" in body and "prior turns" in body
        or ("invent" in body and "prior turn" in body)
    ), (
        "AGENTS.md no longer forbids inventing prior turns to explain "
        "present confusion. The 2026-05-21 transcript showed evo "
        "fabricating an imagined question ('is evo overloaded?') that "
        "was never asked. The rule must call out the failure mode by "
        "name — vague 'don't fabricate' rules don't generalize to it."
    )


def test_session_context_memory_not_state_counter_rail_present():
    """The 'this is turn N of an ongoing thread … your prior replies
    ARE in your context above' line (added by PR #1402's fresh-session
    fix) needed a counter-rail. Without one, the model reads "your
    prior replies are here" and treats them as state-of-truth —
    exactly the 2026-05-23 transcript shape, where evo recommended
    `cp`ing from a /tmp file based on its own 2-day-old "I staged
    this" assertion.

    Per PR #1479's recommended fix (a), the session-context emission
    must follow the "your prior replies ARE in your context" line
    with a counter-rail: 'treat your own prior replies as memory of
    what you said, not as state-of-truth that is still valid. Any
    file path, staged artifact, or config value you mentioned in an
    earlier turn must be re-verified via a tool call before you act
    or recommend on it.'"""
    from evolve_admin.evo import proxy as P
    # The load-bearing case: a 2-day-old ongoing thread where the
    # model would naturally pull on prior assertions.
    block = P.format_session_context({
        "user_turn_count": 5,
        "session_age_seconds": 2 * 24 * 3600,  # 2 days
    })
    # The original #1402 framing must still be present (we are not
    # un-doing the fresh-session fix — we are balancing it).
    assert "prior replies ARE in your context" in block, (
        "the original #1402 'prior replies ARE in your context' "
        "framing is gone. Don't un-ship the fresh-session fix; the "
        "counter-rail is additive."
    )
    # The counter-rail itself — verbatim phrases from the diagnosis.
    assert "memory of what you said" in block, (
        "the session-context block no longer includes the "
        "'memory of what you said' counter-rail. Without it the "
        "model treats prior replies as state-of-truth (the "
        "2026-05-23 failure shape). Add the counter-rail per "
        "PR #1479's recommended fix (a)."
    )
    assert "not as state-of-truth" in block, (
        "the counter-rail no longer includes the 'not as "
        "state-of-truth that is still valid' clause. This is the "
        "load-bearing distinction — prior replies are *what was "
        "said*, not *what is currently true*."
    )
    # The actionable instruction — re-verify before recommending.
    assert "re-verified via a tool call" in block, (
        "the counter-rail no longer instructs the model to "
        "re-verify via a tool call before acting / recommending. "
        "Without the actionable hook the rule is decorative; the "
        "model needs the 'call a tool' instruction explicitly."
    )
    # And it must name the specific shapes that need re-verification.
    for shape in ("file path", "staged artifact", "config value"):
        assert shape in block, (
            f"the counter-rail no longer names {shape!r} as "
            f"something that needs re-verification. Concrete forms "
            f"teach better than abstract rules — without these "
            f"examples the model may apply the rule only to literal "
            f"facts and not to action-recommendation preconditions."
        )


def test_admin_bot_self_identity_teaching_present(agents_md: str):
    """The surface-awareness section must teach that on
    ``Surface: admin_ui`` the bot IS the evolve/admin bot — the
    most-privileged caller — and must forbid the two confabulations the
    real operator transcript showed: (a) deflecting the operator to "the
    admin UI chat" / "the evolve bot" (the surface they're already on /
    the bot itself), and (b) claiming to be a member bot hitting
    authorization walls.

    Root cause: a bot can't observe its own identity unless told each
    turn (``feedback_bot_cannot_observe_own_routing``). The fix injects
    the identity into <session-context>; this guard keeps the prompt-side
    teaching that interprets it from being silently reworded away."""
    body = agents_md
    # The teaching is anchored in the surface-awareness section.
    sa_idx = body.find("## Surface awareness")
    assert sa_idx > 0, "Surface awareness section is missing"
    # Window covers the section through the help-style heading that
    # follows it, so the teaching can't pass by living elsewhere.
    help_idx = body.find("## Help style", sa_idx)
    assert help_idx > sa_idx, "Help style section moved/renamed"
    window = body[sa_idx:help_idx]

    assert "you ARE the evolve/admin bot" in window, (
        "the surface section no longer teaches that on admin_ui the bot "
        "IS the evolve/admin bot. Without it the model confabulates a "
        "member-bot identity on cross-bot questions (the atlas-backup "
        "transcript). See feedback_bot_cannot_observe_own_routing."
    )
    # Must forbid deflecting to the surface they're already on / itself.
    assert "ask the evolve bot via Telegram" in window, (
        "the anti-deflection rule (don't send the operator to the "
        "evolve bot via Telegram — that's the bot itself) is gone."
    )
    # Must forbid the member-bot / authorization-wall confabulation.
    assert "member bot" in window and "authorization wall" in window, (
        "the rule forbidding the member-bot / authorization-wall "
        "confabulation is gone — this was the exact wrong claim in the "
        "operator transcript."
    )
    # The privilege framing MUST be gated on admin_ui, or it would over-
    # claim on Telegram / non-admin surfaces (adversarial-review concern).
    assert "gated on `admin_ui`" in window, (
        "the admin-bot framing is no longer explicitly gated on "
        "admin_ui. Ungated, the model could assert admin privilege on "
        "Telegram or a non-admin surface the session-context block "
        "never granted."
    )
    # And the example session-context block should show the Bot line, so
    # the model has a concrete instance of what it's reading each turn.
    assert "Bot: evolve (admin)" in body, (
        "the example <session-context> block no longer shows the "
        "'Bot: evolve (admin)' identity line that the proxy now injects."
    )


def _page_tool_map_block(agents_md: str) -> str:
    """Slice out the **Page-tool map** table so 'Terminal' / 'Users' /
    'Settings' mentions elsewhere in the file don't satisfy a row check.
    The block runs from the '**Page-tool map**' header to the
    '**Plugins-page tool gaps to close later**' note that follows it."""
    start = agents_md.find("**Page-tool map**")
    assert start > 0, "the **Page-tool map** block header is missing"
    end = agents_md.find("**Plugins-page tool gaps to close later**", start)
    assert end > start, (
        "the page-tool-map block's trailing anchor "
        "('**Plugins-page tool gaps to close later**') moved or was "
        "removed — re-anchor _page_tool_map_block()."
    )
    return agents_md[start:end]


def test_page_tool_map_covers_all_current_admin_pages(agents_md: str):
    """The page-tool map must carry a row for every current admin page
    so evo has grounding to advise on the page the operator is actually
    on, instead of fabricating navigation. Added after the 2026-06-23
    per-page coverage audit found terminal / model-economics / users /
    skills / getting-started / backup / ai-optimization / settings /
    inbox / integrations-keys had no page-tool-map row.

    Each row must (1) name the page using the label the operator sees in
    the sidebar and (2) ground it in a REAL registered tool or an
    explicit 'no tool — use the on-page button' affordance. The grounding
    anchor below is always a real tool name or an actual on-page string
    — never an aspirational tool name (see the companion negative test
    for the gh_issue_create anti-pattern)."""
    block = _page_tool_map_block(agents_md)

    # (sidebar label that must appear as a row, grounding anchor that
    # must also appear in the block). The anchor is a real registered
    # tool or an explicit on-page affordance — verified against the evo
    # tool registry on 2026-06-23, NOT invented.
    rows = [
        ("Terminal", "do NOT try to drive it"),
        ("Model Economics", "pod_state.usage"),
        ("Users", "action.roster.set_role"),
        ("Skills", "action.plugin.enable"),
        ("Credentials", "action.keys.add"),          # integrations-keys page
        ("Getting Started", "Quick Start"),
        ("Backup", "action.bot.backup_workspace"),
        ("AI Optimization", "action.models.check_freshness"),
        ("Settings", "config.network"),
        ("Issues (inbox)", "action.feedback.file_issue"),
    ]
    for label, anchor in rows:
        assert label in block, (
            f"the page-tool map has no row for the {label!r} admin page. "
            f"Without it evo has no grounding to advise on that page and "
            f"is prone to fabricating navigation (the 2026-06-23 audit)."
        )
        assert anchor in block, (
            f"the {label!r} page-tool-map row lost its grounding anchor "
            f"({anchor!r}). Each row must cite a REAL registered tool or "
            f"an explicit on-page affordance — re-grounding it keeps the "
            f"row honest and anti-fabrication."
        )


def test_help_corpus_grounding_rule_teaches_the_tools(agents_md: str):
    """The Per-page bias / Help-surface rule must name the two help-corpus
    retrieval tools by their registered names so the 'answer, don't file'
    Help-page bias is ACTIONABLE. Before this, the bias said to answer
    how-it-works questions but named no tool — so the model answered from
    generic knowledge (a confabulation risk). The rule now teaches:
    evolve_help_search FIRST, then evolve_help_read the top hit, ground
    the answer in that doc.

    Pins the substance, not the wording — the tool names are the
    load-bearing tokens (the model must pattern-match on them to call the
    right tools), plus the search->read ordering and the index-absent
    graceful-degradation cue."""
    # The two tool names — these are the registered names; without them
    # the rule is decorative and the model has no concrete tool to call.
    assert "evolve_help_search" in agents_md, (
        "AGENTS.md no longer names evolve_help_search. The Help-page "
        "'answer, don't file' bias is only actionable if it names the "
        "tool that grounds the answer — without it the model falls back "
        "to generic knowledge (the confabulation risk this rule fights)."
    )
    assert "evolve_help_read" in agents_md, (
        "AGENTS.md no longer names evolve_help_read. The search->read "
        "handoff is how the model pulls the full doc body to ground on; "
        "naming only search leaves the model with a doc_id and no "
        "instruction to fetch the body."
    )
    # The ordering matters — search must be taught BEFORE read so the
    # model does retrieval-then-ground, not the reverse.
    search_pos = agents_md.find("evolve_help_search")
    read_pos = agents_md.find("evolve_help_read")
    assert 0 < search_pos < read_pos, (
        "evolve_help_search must be taught before evolve_help_read — the "
        "rule is 'search FIRST, then read the top hit'. Reversing the "
        "order teaches the model to read before it knows which doc."
    )
    # The graceful-degradation cue: when the index isn't built, the model
    # must NOT fabricate a citation. This is the anti-confabulation
    # backstop and must survive rewordings.
    assert "available=false" in agents_md or "available=False" in agents_md, (
        "the Help-corpus rule no longer mentions the available=false "
        "(index-not-built) case. Without it the model may treat an empty "
        "search as license to answer from memory while implying it "
        "consulted the corpus — exactly the fabrication this rule guards."
    )


def test_page_tool_map_no_aspirational_issue_tool(agents_md: str):
    """The Issues-page (inbox) row must NOT invent an issue-creation /
    issue-list tool. The 2026-06-23 audit specifically warned against
    ``gh_issue_create`` — it is NOT registered. The row should name the
    real ``action.feedback.file_issue`` (files a fresh issue) and route
    draft promotion through the on-page **Promote to GitHub →** button.

    ``gh_issue_create`` may appear ONLY as a named-and-forbidden
    example, never as a recommended tool."""
    for line in agents_md.splitlines():
        if "gh_issue_create" in line:
            assert "not invent" in line.lower(), (
                "``gh_issue_create`` appears in AGENTS.md but not as a "
                "negative ('do NOT invent') example. It is not a "
                "registered tool — the 2026-06-23 audit flagged it as "
                "the aspirational name the model hallucinates for the "
                "Issues page. Name it only to forbid it."
            )
