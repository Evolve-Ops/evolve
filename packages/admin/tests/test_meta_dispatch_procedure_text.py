"""Text pins for the `meta-dispatch` procedure and its setup doc.

`internal/meta-dispatch-procedure.md` is not documentation ABOUT the dispatcher — it IS
the dispatcher. `tools/meta-skills-sync` mirrors it byte-for-byte into
`~/.claude/scheduled-tasks/meta-dispatch/SKILL.md`, and the scheduler executes that copy.
So a clause in it has the standing of code, and the clauses that state a CADENCE or a
THRESHOLD have the standing of a constant — which is exactly the kind of thing that rots
when it is written down in two places and changed in one.

WHAT THESE PIN, and why each is worth a test rather than a review comment:

  * **The cadence.** Moved `*/30 * * * *` -> `0 * * * *` on 2026-09-07. The number
    appears in the procedure's front matter, its CADENCE line, and the setup doc's
    footprint table and install step, and a half-applied change reads as a contradiction
    the run has to resolve mid-tick.
  * **The thresholds.** Every window in the procedure is an ABSOLUTE TIME and keeps the
    value it had at `*/30`; only the number of ticks it spans changed. A threshold
    silently re-expressed as "one tick" would double the next time the cadence moves.
  * **The step-4 contract.** The chip title carries the local `HH:MM`, the poke carries
    the created time and the session id, and the tick report carries the standing
    "cards waiting for a tap" line. Those three sentences are the whole operator-facing
    half of this change, and they live nowhere else.
  * **The one thing the repo CANNOT do:** the cron lives in the operator's
    scheduled-tasks registry. `meta-skills-sync` writes bodies only. A procedure that
    stops saying so invites a future change to "fix" the cadence by editing a file that
    the scheduler never reads.
WHAT THE D-PM6' HALF PINS (added on the headless-launch branch, merged here):

Prose cannot be type-checked and it has already gone stale twice in this lane — the
2026-08-30 mirror was 53 lines behind `origin/main`, and a tick ran a Transport section
its own experiment had falsified. Those pins do not review the prose; they assert that the
handful of sentences a reader could silently REVERSE are still there, and that the retired
ones are gone. Each names the cost of losing it: a pin with no such cost is noise, because
a text ratchet makes every future edit expensive and must be spent only where a wrong
answer produces a duplicate chip, a lost brief, or a session running somewhere it should
not.

D-MT1 (2026-09-23): the procedure became `tools/meta-tick`. Pins on a DECISION were
re-pointed at `internal/meta-tick-spec.md` (the moved text); pins on BEHAVIOUR (step 2c,
4, 4a, 4d, the waiting line, headless caps, the privileged route, the stalled poke) were
rewritten against the runner in `test_meta_tick.py`, which names each one it replaces.

NOTE (rebase 2026-09-08): this file gained its constants twice — `_REPO`/`_PROCEDURE`/
`_SETUP`/`_LANE_README` from the cadence work and `_ROOT`/`PROCEDURE`/`SETUP`/
`LANE_README`/`SPEC_DELTA`/`SUBSTRATE_SPEC`/`LEDGER_SCHEMA` from this branch. They are
kept side by side rather than collapsed so neither side's tests were rewritten during a
rebase; collapsing them to one set is a clean follow-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
# D-MT1 (2026-09-23): the procedure's text moved whole to meta-tick-spec.md and is now the
# runner's specification; these pins follow the DECISIONS there. The short scheduled-task
# body keeps the name `meta-dispatch-procedure.md` (_BODY).
_PROCEDURE = _REPO / "internal" / "meta-tick-spec.md"
_BODY = _REPO / "internal" / "meta-dispatch-procedure.md"
_SETUP = _REPO / "internal" / "meta-system-setup.md"
_LANE_README = _REPO / "internal" / "dispatch" / "README.md"


def _text(path: Path) -> str:
    if not path.is_file():          # synced consumer checkout: nothing to pin
        pytest.skip("%s not present in this checkout" % path)
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def procedure():
    return _text(_PROCEDURE)


@pytest.fixture(scope="module")
def setup():
    return _text(_SETUP)


# ── cadence ──────────────────────────────────────────────────────────────────


def test_procedure_states_the_hourly_cadence(procedure):
    assert "**CADENCE: hourly, on the hour (`0 * * * *`)" in procedure
    body = _text(_BODY)
    assert "**CADENCE: hourly, on the hour (`0 * * * *`).**" in body
    assert body.splitlines()[2].startswith("description: Unattended PM-lane "
                                           "dispatcher — hourly:")


def test_no_surface_still_advertises_the_half_hourly_cadence(procedure, setup):
    """`*/30` may appear only where it is named as HISTORY. A live instruction still
    carrying it is the half-applied change this test exists to catch."""
    for name, text in (("procedure", procedure), ("setup", setup)):
        for line in text.splitlines():
            if "*/30 * * * *" not in line:
                continue
            if "meta-dispatch" not in line and "dispatcher" not in line:
                continue          # pm-landing's own cadence is not this task's
            assert any(marker in line for marker in
                       ("until 2026-09-07", "changed from", "2026-09-07")), \
                "%s still instructs the old cadence: %s" % (name, line)


def test_setup_doc_tells_the_operator_to_set_the_hourly_cron(setup):
    """The cron is registry state; merging the procedure cannot set it. This is the
    line the operator acts on."""
    assert "Create it at **`0 * * * *`** — hourly, on the hour" in setup
    assert "Set it to `0 * * * *`" in setup


def test_setup_doc_says_the_cadence_lives_outside_the_repo(setup):
    assert "The cadence lives HERE, in the registry — not in the repo" in setup
    assert "writes\n   > **bodies only** and never reads or touches the cron " in setup


def test_setup_doc_documents_the_ad_hoc_run(setup):
    """Two ways to fire one, and the fact that it needs no special handling — an
    operator who thinks an ad hoc run is a different kind of tick will not use it."""
    assert '"Run now" on the task in the' in setup
    assert "`fire_trigger` from a Cowork session" in setup
    assert "An ad hoc run is an\n   > ORDINARY TICK" in setup


def test_procedure_treats_an_ad_hoc_run_as_a_normal_tick(procedure):
    assert "**An ad hoc run is a NORMAL TICK and needs no special handling.**" in procedure
    assert "Do not try to detect an ad hoc run" in procedure


# ── thresholds ───────────────────────────────────────────────────────────────


def test_procedure_re_derives_every_cadence_coupled_threshold(procedure):
    assert "## THRESHOLDS UNDER THE HOURLY CADENCE" in procedure
    section = procedure.split("## THRESHOLDS UNDER THE HOURLY CADENCE")[1]
    section = section.split("## THE ONE RULE")[0]
    for expected in (
        "~24 h",                       # step 1 failed-preparation window
        "≤1 h",                        # bind exposure + dirty-checkout window
        "cap 6 fills in ~2 h",         # step 5 up-to-three-per-tick, re-derived
        "**never** — edge-triggered",  # the poke has no interval at all
        "**45 minutes**",              # #4088's stall window, absolute
        "**2 h**",                     # #4088's no-commit window, absolute
        "20 minutes",                  # meta-pm-review's let-the-chip-finish grace
    ):
        assert expected in section, expected


def test_procedure_says_absolute_times_did_not_change(procedure):
    """The load-bearing sentence: a window written as "one tick" silently doubles when
    the tick doubles, so every window is stated as a time and its tick span is derived."""
    assert ("**Every window below is an ABSOLUTE TIME and keeps the value\nit had at "
            "`*/30`; what changed is only how many ticks it now spans.**") in procedure


def test_procedure_derived_intervals_follow_the_hourly_tick(procedure):
    assert "the exposure window is ≤1 hour instead of the reconciler's ≤2h" in procedure
    assert "on the first tick after the merge (≤1 h)" in procedure
    assert "**An hourly cron writes the same index you do.**" in procedure
    assert "The build cap of 6 therefore fills in ~2 hours at the hourly cadence" in procedure


def test_procedure_says_the_repo_cannot_change_the_cron(procedure):
    assert "**The cron itself is NOT in this repo and this file cannot change it.**" \
        in procedure


# ── step 4: the card's timestamp, title, poke, and the standing line ─────────




def test_step_4a_names_the_id_composer(procedure):
    """D-CD1 (2026-09-11): the lane id sits in square brackets right after the time,
    composed in exactly one place, so the tray tile and the poke that announced it can
    never disagree about which id they name."""
    assert "`tools/meta-dispatch-eligible.compose_chip_title_timed`" in procedure
    assert "confusion around which cards have been started and which haven't" in procedure


def test_step_4b_no_longer_hand_writes_the_dispatch_stamps(procedure):
    """The mover writes them. A run that still Edits `dispatched` in by hand would be
    composing a stamp beside one taken from a real clock."""
    assert ("Then Edit the moved file's front matter to add `session: <task_id>`, "
            "`branch: <branch if known, else null>`, and `launch: prepared`.") in procedure
    assert "**`dispatched` and `dispatched_at` are NOT among them any more" in procedure
    assert "dispatched_at: <UTC ISO-8601>" in procedure


def test_step_4c_ledger_row_carries_the_stamp(procedure):
    """`tools/meta-queue` reads the chip row, not the lane entry — a row without the
    stamp is a card the operator can find in the tray but not on the `/queue` surface
    they check first."""
    assert "reversible, dispatched, dispatched_at, why," in procedure
    assert "Copy `dispatched` and `dispatched_at` verbatim out of step 4b" in procedure










def test_lane_readme_documents_dispatched_at():
    text = _text(_LANE_README)
    assert "`dispatched_at` (UTC ISO-8601, `YYYY-MM-DDTHH:MM:SSZ`)" in text
    assert "written by `tools/meta-dispatch-move launch` itself" in text
    assert "**`dispatched_at` exists because a date cannot find a card**" in text

_ROOT = Path(__file__).resolve().parents[3]
PROCEDURE = _ROOT / "internal" / "meta-tick-spec.md"
SETUP = _ROOT / "internal" / "meta-system-setup.md"
LANE_README = _ROOT / "internal" / "dispatch" / "README.md"
SPEC_DELTA = (_ROOT / "internal"
              / "spec-delta-pm-lane-rules-of-engagement-2026-09-03.md")
SUBSTRATE_SPEC = _ROOT / "internal" / "spec-substrate-2026-06-15.md"
LEDGER_SCHEMA = _ROOT / "internal" / "meta-ledger-schema.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── the launch path, by privilege ────────────────────────────────────────────






def test_the_privileged_path_is_still_prepare_and_tap():
    body = text(PROCEDURE)
    assert "`route: \"prepared\"`" in body
    assert "unchanged, verbatim, including the tap" in body


def test_the_ratification_line_is_retired_for_non_privileged_and_kept_for_privileged():
    """The operator ratified a split, not a removal. Losing either half misstates what
    they agreed to."""
    body = text(PROCEDURE)
    assert "RETIRED for non-privileged briefs and stands VERBATIM for privileged ones" in body


@pytest.mark.parametrize("doc", [PROCEDURE, LANE_README, SPEC_DELTA])
def test_a_brief_gets_a_card_or_a_session_never_both(doc):
    """The 2026-08-28 amendment's third open question. Two transports running side by side
    is how one brief acquires two workers."""
    body = text(doc).lower()
    assert "never both" in body or "cannot both fire" in body


# ── the guardrails ───────────────────────────────────────────────────────────




def test_the_hard_rules_forbid_running_a_session_in_the_operators_checkout():
    """A headless session runs with `--dangerously-skip-permissions`; its cwd is the whole
    blast radius."""
    body = text(PROCEDURE)
    assert "never in the operator's dev checkout" in body
    assert "/Users/Shared/evolve-repo" in body


def test_the_hard_rules_forbid_the_run_supplying_its_own_prerequisites():
    """Both prerequisites are the operator's. A run that installs its own grant is a run
    widening its own permissions."""
    body = text(PROCEDURE)
    assert "Never add the `Bash(claude:*)` grant" in body
    assert "widening its own permissions" in body




def test_the_dispatcher_never_relaunches_a_stalled_headless_chip():
    """Two relaunchers would mint two sessions against one brief; the reconciler's inverse
    guard is the one that decides."""
    body = text(PROCEDURE)
    assert "Never relaunch a stalled headless chip" in body
    assert "You never relaunch it" in body


def test_the_pr_stays_the_output_of_record():
    """`claude logs <id>` is a raw TTY buffer that is gone once the session exits, so a
    procedure that started reading logs would be reading nothing."""
    assert "The PR remains the output of record" in text(PROCEDURE)


# ── liveness ─────────────────────────────────────────────────────────────────


def test_step_1_states_both_liveness_windows():
    body = text(PROCEDURE)
    assert "no branch was pushed within 45 minutes of `started`" in body
    assert "no commit within 2 h of its last" in body




# ── caps ─────────────────────────────────────────────────────────────────────


def test_step_5_splits_the_three_ceilings():
    """D-PM13 made step 5 three limits, not two, and they are different KINDS of limit:
    a rate (per tick), a depth (the tray), and a headless start budget. A reader who
    collapses them re-derives the wrong tick."""
    body = text(PROCEDURE)
    assert ("UP TO THREE PREPARED CARDS per run, in queue order, subject to both caps "
            "and the file-set rule (D-PM13); up to TWO headless starts") in body
    assert "--headless-started" in body
    assert "**Rate:** at most 3 cards per tick (`--per-tick`)" in body
    assert "**Depth:** the prepared cap (4)" in body
    assert "**Files:** two candidates whose `touches:` prefixes overlap" in body


def test_prepared_cap_no_longer_stops_the_whole_lane():
    """The 2026-09-06 defect, stated where the run reads it: a full tray blocks the tray,
    not the lane."""
    body = text(PROCEDURE)
    assert "`blocked_by: \"prepared-cap\"` no longer stops the whole lane" in body
    assert "24 eligible briefs" in body


# ── the two operator prerequisites ───────────────────────────────────────────


def test_the_setup_doc_carries_the_grant_line_verbatim():
    """It is copied into `~/.claude/settings.json` by hand. A wrong string is a grant that
    silently does not match."""
    assert '"Bash(claude:*)"' in text(SETUP)


def test_the_setup_doc_carries_the_one_time_disclaimer_command():
    body = text(SETUP)
    assert "claude --dangerously-skip-permissions" in body
    assert "interactively" in body


def test_the_setup_doc_says_neither_prerequisite_is_done_by_a_run():
    assert "Neither is\n   optional and neither can be done by any chip or scheduled run" in text(SETUP)


def test_the_setup_doc_says_the_lane_degrades_rather_than_breaking():
    assert "Nothing breaks while they are missing" in text(SETUP)


@pytest.mark.parametrize("doc", [PROCEDURE, LANE_README, SPEC_DELTA])
def test_the_fallback_log_line_is_the_same_string_everywhere(doc):
    """`Prereqs.note()` produces this string. Three paraphrases of one event is how a log
    stops being greppable."""
    assert "headless: unavailable (<which>)" in text(doc)


# ── the records ──────────────────────────────────────────────────────────────


def test_the_decision_is_recorded_as_d_pm6_prime():
    body = text(SPEC_DELTA)
    assert "**D-PM6′**" in body
    assert "ratified in chat 2026-09-06" in body


def test_d_pm5_is_marked_superseded_rather_than_left_contradicting():
    """D-PM5 says "the click gate stays". Leaving it unqualified would make the spec delta
    argue with itself in the same table."""
    assert "*(superseded 2026-09-06 by D-PM6′ for non-privileged briefs)*" in text(SPEC_DELTA)


def test_initiative_13_records_which_fork_was_taken():
    body = text(SUBSTRATE_SPEC)
    assert "RESOLVED 2026-09-06 — option (a) TAKEN, for non-privileged\nbriefs" in body
    assert "fork pending operator decision" not in body


def test_the_ledger_schema_exempts_a_headless_chip_from_click_pending():
    body = text(LEDGER_SCHEMA)
    assert "**A HEADLESS chip is exempt outright (D-PM6′, 2026-09-06).**" in body
    assert "`launch` on a chip row" in body
