"""Unit tests for tools/meta_dispatch_headless — the D-PM6' launch-path decision.

The module answers three questions for two callers (`tools/meta-dispatch-move launch
--headless` and `tools/meta-dispatch-eligible`), and every one of them has a direction it
must fail in:

  * the prerequisite check fails toward the CARD (a degraded lane, never a broken launch);
  * the route fails toward `prepared` for a privileged brief, ALWAYS, prerequisites or not;
  * liveness fails toward `alive` (an unknown stamp must never authorise a relaunch, which
    would mint a second session against the same brief).

These are the pins for that. The invocation form is pinned too: the 2026-08-28 experiment
found the argv form starts a session that silently discards its prompt, so a regression to
it would log successful launches forever while producing nothing.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD = Path(__file__).resolve().parents[3] / "tools" / "meta_dispatch_headless.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("meta_dispatch_headless", str(_MOD))
    spec = importlib.util.spec_from_loader("meta_dispatch_headless", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_dispatch_headless"] = mod
    loader.exec_module(mod)
    return mod


mdh = _load()


# ── fixtures ─────────────────────────────────────────────────────────────────


def home_with(tmp_path, *, allow=(), deny=(), disclaimer=False, repo_allow=None):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": list(allow), "deny": list(deny)}}))
    marker = {"numStartups": 3}
    if disclaimer:
        marker["bypassPermissionsModeAccepted"] = True
    (home / ".claude.json").write_text(json.dumps(marker))
    repo = None
    if repo_allow is not None:
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": list(repo_allow)}}))
    return home, repo


def ready_home(tmp_path):
    return home_with(tmp_path, allow=[mdh.GRANT], disclaimer=True)


# ── prerequisite (a): the grant ──────────────────────────────────────────────


def test_both_prerequisites_present_is_available(tmp_path):
    home, repo = ready_home(tmp_path)
    pre = mdh.check_prereqs(home=home, repo=repo)
    assert pre.ok is True
    assert pre.missing == ()
    assert pre.note() == ""


def test_missing_grant_falls_back_and_names_which_half(tmp_path):
    home, repo = home_with(tmp_path, allow=["Bash(git:*)"], disclaimer=True)
    pre = mdh.check_prereqs(home=home, repo=repo)
    assert pre.ok is False
    assert pre.missing == (mdh.MISSING_GRANT,)
    assert pre.note() == "headless: unavailable (grant)"
    assert mdh.GRANT in pre.detail


def test_missing_disclaimer_falls_back_and_names_which_half(tmp_path):
    home, repo = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    pre = mdh.check_prereqs(home=home, repo=repo)
    assert pre.missing == (mdh.MISSING_DISCLAIMER,)
    assert pre.note() == "headless: unavailable (disclaimer)"


def test_both_missing_names_both(tmp_path):
    home, repo = home_with(tmp_path, allow=[], disclaimer=False)
    pre = mdh.check_prereqs(home=home, repo=repo)
    assert pre.missing == (mdh.MISSING_GRANT, mdh.MISSING_DISCLAIMER)
    assert pre.note() == "headless: unavailable (grant, disclaimer)"


def test_a_deny_rule_outranks_an_allow_rule(tmp_path):
    """Deny precedence, copied from the permission layer rather than approximated. A lane
    that read a DENIED command as granted would start a session that dies on its first
    Bash call — with the brief already moved out of `queued/`."""
    home, repo = home_with(tmp_path, allow=[mdh.GRANT], deny=["Bash(claude:*)"],
                           disclaimer=True)
    pre = mdh.check_prereqs(home=home, repo=repo)
    assert pre.missing == (mdh.MISSING_GRANT,)
    assert "deny" in pre.detail


def test_a_repo_level_settings_file_can_carry_the_grant(tmp_path):
    home, repo = home_with(tmp_path, allow=[], disclaimer=True, repo_allow=[mdh.GRANT])
    assert mdh.check_prereqs(home=home, repo=repo).ok is True


@pytest.mark.parametrize("rule", ["Bash(claude:*)", "Bash(claude *)", "Bash(claude)",
                                  "Bash(claude -p:*)", "Bash(*)", "Bash"])
def test_grant_shapes_that_cover_the_claude_command(tmp_path, rule):
    home, repo = home_with(tmp_path, allow=[rule], disclaimer=True)
    assert mdh.check_prereqs(home=home, repo=repo).ok is True, rule


@pytest.mark.parametrize("rule", ["Bash(claudia:*)", "Bash(gh claude:*)", "Read(claude)",
                                  "Bash(git claude:*)", ""])
def test_grant_shapes_that_do_not(tmp_path, rule):
    home, repo = home_with(tmp_path, allow=[rule], disclaimer=True)
    assert mdh.check_prereqs(home=home, repo=repo).ok is False, rule


def test_an_unreadable_settings_file_cannot_grant_and_cannot_revoke(tmp_path):
    """A truncated settings file must not read as a deny — that would be a half-written
    file silently revoking a grant the operator did add somewhere else."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=True)
    (home / ".claude" / "settings.local.json").write_text("{ this is not json")
    assert mdh.check_prereqs(home=home).ok is True


def test_a_missing_home_is_unavailable_not_an_exception(tmp_path):
    pre = mdh.check_prereqs(home=tmp_path / "nope")
    assert pre.ok is False
    assert set(pre.missing) == {mdh.MISSING_GRANT, mdh.MISSING_DISCLAIMER}


def test_the_disclaimer_marker_is_matched_by_shape_not_one_literal(tmp_path):
    """The key belongs to the CLI. Matching a family of spellings means an upstream rename
    degrades to a tray card rather than to a stale literal reading as `accepted`."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps({"acceptedBypassPermissionsMode": True}))
    assert mdh.check_prereqs(home=home).ok is True


def test_a_falsy_disclaimer_marker_is_not_acceptance(tmp_path):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps({"bypassPermissionsModeAccepted": False}))
    assert mdh.check_prereqs(home=home).missing == (mdh.MISSING_DISCLAIMER,)


def test_the_skip_prompt_setting_counts_as_acceptance(tmp_path):
    """`skipDangerousModePermissionPrompt: true` makes the CLI never show the disclaimer,
    so no marker is ever written to `~/.claude.json`; the operator accepted by a different
    door and the lane must read it (2026-09-11: 20 hours of `unavailable (disclaimer)`)."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": [mdh.GRANT]}, mdh.SKIP_PROMPT_SETTING: True}))
    assert mdh.disclaimer_state(home)[0] is True
    assert mdh.check_prereqs(home=home).ok is True


def test_a_falsy_skip_prompt_setting_defers_to_the_marker(tmp_path):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": [mdh.GRANT]}, mdh.SKIP_PROMPT_SETTING: False}))
    pre = mdh.check_prereqs(home=home)
    assert pre.missing == (mdh.MISSING_DISCLAIMER,)
    assert mdh.SKIP_PROMPT_SETTING in pre.detail


def test_the_skip_prompt_setting_in_the_repo_settings_does_not_count(tmp_path):
    """A chip can commit `.claude/settings.json`; a committed key must never accept the
    disclaimer on the operator's behalf (review pr-4216, must-fix 1)."""
    home, repo = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False, repo_allow=[])
    (repo / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": []}, mdh.SKIP_PROMPT_SETTING: True}))
    assert mdh.check_prereqs(home=home, repo=repo).missing == (mdh.MISSING_DISCLAIMER,)


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_only_a_json_true_counts_for_the_skip_prompt_setting(tmp_path, value):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": [mdh.GRANT]}, mdh.SKIP_PROMPT_SETTING: value}))
    assert mdh.check_prereqs(home=home).missing == (mdh.MISSING_DISCLAIMER,)


def test_the_skip_prompt_setting_never_short_circuits_the_grant(tmp_path):
    home, _ = home_with(tmp_path, allow=[], disclaimer=False)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": []}, mdh.SKIP_PROMPT_SETTING: True}))
    assert mdh.check_prereqs(home=home).missing == (mdh.MISSING_GRANT,)


def test_a_project_scoped_marker_counts(tmp_path):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/some/repo": {"bypassPermissionsModeAccepted": True}}}))
    assert mdh.check_prereqs(home=home).ok is True


# ── routing ──────────────────────────────────────────────────────────────────


def test_a_privileged_brief_never_routes_headless_even_when_available():
    """THE guardrail. D-PM1/D-PM2 make the operator the gate at both ends of a privileged
    brief; removing the tap would remove half of a control the operator explicitly kept."""
    assert mdh.route_for(privileged=True, headless_available=True) == mdh.ROUTE_PREPARED
    assert mdh.route_for(privileged=True, headless_available=False) == mdh.ROUTE_PREPARED


def test_a_non_privileged_brief_routes_headless_when_available():
    assert mdh.route_for(privileged=False, headless_available=True) == mdh.ROUTE_HEADLESS


def test_a_non_privileged_brief_falls_back_to_a_card_when_unavailable():
    assert mdh.route_for(privileged=False, headless_available=False) == mdh.ROUTE_PREPARED


# ── per-tick arithmetic ──────────────────────────────────────────────────────


def test_headless_is_bounded_at_two_per_tick_even_with_six_build_slots():
    assert mdh.headless_slots(build_slots=6, started_this_tick=0) == 2


def test_each_launch_this_tick_debits_the_ceiling():
    assert mdh.headless_slots(6, 1) == 1
    assert mdh.headless_slots(6, 2) == 0
    assert mdh.headless_slots(6, 3) == 0


def test_the_build_cap_still_binds_when_it_is_tighter_than_the_tick_ceiling():
    assert mdh.headless_slots(build_slots=1, started_this_tick=0) == 1
    assert mdh.headless_slots(build_slots=0, started_this_tick=0) == 0


def test_negative_inputs_clamp_to_zero_rather_than_inverting():
    assert mdh.headless_slots(-3, 0) == 0
    assert mdh.headless_slots(6, -1) == 2


# ── liveness ─────────────────────────────────────────────────────────────────


def test_no_branch_within_45_minutes_is_stalled():
    live = mdh.classify_liveness("2026-09-06T10:00:00Z", None, None,
                                 "2026-09-06T10:46:00Z")
    assert live.state == mdh.STALLED
    assert "no branch" in live.reason


def test_no_branch_but_still_inside_the_window_is_alive():
    live = mdh.classify_liveness("2026-09-06T10:00:00Z", None, None,
                                 "2026-09-06T10:44:00Z")
    assert live.state == mdh.ALIVE


def test_no_commit_within_two_hours_of_the_last_is_stalled():
    live = mdh.classify_liveness("2026-09-06T10:00:00Z", "claude/meta-x",
                                 "2026-09-06T11:00:00Z", "2026-09-06T13:01:00Z")
    assert live.state == mdh.STALLED
    assert "no commit" in live.reason


def test_a_recent_commit_is_alive_however_old_the_launch_is():
    live = mdh.classify_liveness("2026-09-06T10:00:00Z", "claude/meta-x",
                                 "2026-09-06T18:00:00Z", "2026-09-06T19:00:00Z")
    assert live.state == mdh.ALIVE


def test_a_branch_with_no_commit_clock_is_alive_not_stalled():
    """The chip demonstrably started. Whether it has since gone quiet is a question only
    the reconciler's live pass can answer, and answering it here from an absence would
    relaunch a running session."""
    live = mdh.classify_liveness("2026-09-06T10:00:00Z", "claude/meta-x", None,
                                 "2026-09-07T10:00:00Z")
    assert live.state == mdh.ALIVE


@pytest.mark.parametrize("started", [None, "", "not-a-timestamp"])
def test_an_unusable_started_stamp_reads_alive(started):
    live = mdh.classify_liveness(started, None, None, "2026-09-06T23:00:00Z")
    assert live.state == mdh.ALIVE


def test_a_naive_timestamp_is_read_as_utc_not_local():
    """`feedback_turn_files_are_utc_named_readers_must_not_use_local` — the lane stamps
    UTC everywhere, so a stamp that lost its `Z` must not be re-interpreted in whatever
    zone the reader happens to sit in (up to a 12-hour error, either sign)."""
    live = mdh.classify_liveness("2026-09-06T10:00:00", None, None,
                                 "2026-09-06T10:30:00Z")
    assert live.state == mdh.ALIVE
    assert live.elapsed_s == 30 * 60


# ── the invocation ───────────────────────────────────────────────────────────


def test_the_prompt_is_the_positional_and_print_is_never_passed():
    """2026-09-12, CLI v2.1.268: `--bg` and `--print` CONFLICT and the CLI exits 1 —
    "--print never starts the interactive session that `claude agents` attaches to, so the
    job would be unattachable". Two consecutive dispatcher ticks hit that exit 1 while the
    decider reported headless available.

    This asserts the OPPOSITE of what it asserted until 2026-09-12 (v2.1.193: `-p` was the
    only form that ran, and the bare positional silently never started). Both readings were
    correct on their own CLI, so this test pins the CURRENT one and `build_argv`'s docstring
    keeps the table — a future flip is a CLI change to re-measure, not a regression to
    revert blind."""
    argv = mdh.build_argv("the brief body")
    assert argv[0] == "claude"
    assert "-p" not in argv and "--print" not in argv
    assert argv[-1] == "the brief body", "the prompt must be last — no flag may follow it"
    assert "--bg" in argv and "--dangerously-skip-permissions" in argv


def test_the_prompt_is_passed_verbatim_including_newlines_and_markdown():
    body = "---\nWHY: a thing.\n\n1. **Do it.**\n"
    assert mdh.build_argv(body)[-1] == body


def test_no_stdin_pipe_is_implied_by_the_argv():
    """The lane's granted toolset forbids pipes outright, so the `echo … | claude --bg`
    form is inadmissible here however well it works."""
    assert all("|" not in part for part in mdh.build_argv("x"))


# ── the disclaimer miss must distinguish its own two failure modes ────────────


def test_a_genuine_miss_names_the_file_and_what_it_inspected(tmp_path):
    """Before this, both failure modes rendered as one sentence asserting the second, so a
    rename upstream was indistinguishable from a true 'never accepted' (review pr-4088
    finding 5)."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3, "theme": "dark"}))
    accepted, detail = mdh.disclaimer_state(home)
    assert accepted is False
    assert str(home / ".claude.json") in detail
    assert "numStartups" in detail and "theme" in detail
    assert "genuine" in detail


def test_a_renamed_marker_is_reported_as_a_near_miss_not_a_plain_no(tmp_path):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps({"bypassPermissionsModeAck": True}))
    accepted, detail = mdh.disclaimer_state(home)
    assert accepted is False, "a near miss must NOT be read as acceptance"
    assert "bypassPermissionsModeAck" in detail
    assert "DISCLAIMER_KEY_RE" in detail


def test_a_near_miss_nested_under_a_project_is_surfaced_too(tmp_path):
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/Users/placeholder/repo": {"dangerousSkipMode": True}}}))
    accepted, detail = mdh.disclaimer_state(home)
    assert accepted is False
    assert "dangerousSkipMode" in detail


def test_the_miss_detail_never_prints_project_paths(tmp_path):
    """Project keys are absolute paths on the operator's machine; they are counted, not
    printed. The tick summary is pasted around."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/Users/placeholder/secret-repo": {"numStartups": 1}}}))
    _, detail = mdh.disclaimer_state(home)
    assert "secret-repo" not in detail
    assert "1 project entr" in detail


def test_acceptance_still_wins_over_any_near_miss(tmp_path):
    """The near-miss reporter must never change the verdict in either direction."""
    home, _ = home_with(tmp_path, allow=[mdh.GRANT], disclaimer=False)
    (home / ".claude.json").write_text(json.dumps(
        {"bypassPermissionsModeAck": True, "bypassPermissionsModeAccepted": True}))
    assert mdh.disclaimer_state(home)[0] is True
    assert mdh.check_prereqs(home=home).ok is True
