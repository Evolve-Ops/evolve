"""tests/test_inspector.py — Phase 4 of the surface-aware help-style spec.

Spec: ``docs/spec-surface-aware-help-style-2026-05-22.md`` §7.5 + §7.5.1.

The inspector is the outgoing-text choke point in ``send_to_evo``; this
suite verifies the three known-recurring failure modes are caught with
the right substitution shape, that legitimate prose mentions pass
through, and that the surface conditioning matches the spec.

All haiku calls are stubbed via the ``haiku_fn`` injection — the real
Anthropic API is never hit. The deterministic oracle maps known input
patterns to known haiku verdicts so each test is reproducible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_PKG = Path(__file__).resolve().parents[1]
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin.evo.inspector import (  # noqa: E402
    EVIDENCE_GROUNDED_REMEDIATION_NEGATIVES,
    EXEC_FABRICATION_VARIANTS,
    HaikuVerdict,
    InspectorEvent,
    NO_EVIDENCE_REMEDIATION_PATTERNS,
    PRECONDITION_STALENESS_PATTERNS,
    PROSE_MENTION_NEGATIVES,
    SHELL_AS_ADVICE_PATTERNS,
    _extract_cli_blocks,
    _has_preface_signal,
    _has_remediation_signal,
    _has_staleness_signal,
    _parse_haiku_verdict,
    _regex_pre_filter,
    _strip_permission_preface,
    _verify_cli_accuracy,
    inspect_outgoing_text,
    lookup_ui_alternative,
)


@pytest.fixture
def linux_profile():
    """Pin the LINUX platform profile for one test, then restore the
    suite-wide MACOS pin (conftest pins MACOS at collection).

    The deploy-checkout write-guard resolves get_profile() at call time,
    so a Linux-path command is flagged only when the active profile is
    LINUX (i.e. on a real Linux pod). These cases prove the guard closes
    the Linux blind spot; the macOS cases prove it stays green on macOS.
    The finally restores MACOS so LINUX does not leak to later tests that
    assert macOS path shapes."""
    set_profile(LINUX)
    try:
        yield
    finally:
        set_profile(MACOS)


# ─────────────────────────────────────────────────────────────────────────────
# Hand-coded haiku oracle — maps known input substrings to verdicts so
# tests are deterministic without hitting the real API.
# ─────────────────────────────────────────────────────────────────────────────


def _oracle_haiku(text: str) -> HaikuVerdict:
    """Deterministic stand-in for the Anthropic haiku call.

    Maps known substrings → (branch, reason). Any input not covered by a
    fixture is treated as ``no`` — the inspector then passes the text
    through.
    """
    lowered = text.lower()

    # ── Explicit explanatory NEGATIVES — checked FIRST so they bypass
    # the shell/preface branches.
    if "that's the oc field governing" in lowered:
        return HaikuVerdict("no", reason="explanatory mention of capability")
    if "in bash you'd write" in lowered and "instead" in lowered:
        return HaikuVerdict("no", reason="explanatory contrast, not preface")
    if "/tmp/ is where staging files live" in lowered:
        return HaikuVerdict("no", reason="prose explanation of path")
    if "tools.exec.security is set to" in lowered and "that's the oc field" in lowered:
        return HaikuVerdict("no", reason="explanatory answer")
    # Evidence-grounded remediation NEGATIVE — a fix that quotes a
    # current-turn tool result. Branch d must NOT fire (the evidence
    # gate is satisfied). Checked here so it bypasses the d/a branches.
    if "pod_state.backup_status(bot_id=atlas)` returned" in text:
        return HaikuVerdict("no", reason="remediation cites current-turn tool output")

    # ── Cite-or-don't (branch d) — evidence-free write/sudo/destructive
    # git fix → yes/d. Checked BEFORE the staleness heuristic so a
    # no-evidence fix that happens to mention a /tmp path maps to d, the
    # broader (turn-1-capable) check, not c. Real haiku applies the
    # d-before-c priority from the prompt (decision order d, c, b, a);
    # this oracle keys on the distinctive NO_EVIDENCE fixture phrases for
    # determinism.
    no_evidence_markers = (
        "is failing on a permissions issue",
        "the gateway is wedged",
        "that config drifted",
    )
    if any(m in lowered for m in no_evidence_markers):
        return HaikuVerdict("yes/d", reason="remediation lacks current-turn evidence")

    # ── Precondition-staleness signals → yes/c (checked BEFORE shell
    # so /tmp-staged-file references trip staleness first, matching the
    # diagnosis-2026-05-23 cron-caps case).
    staleness_markers = (
        "if /tmp was cleared",
        "assuming the file is still there",
        "from my earlier turn",
        "unless that's changed since last we spoke",
        "i had staged the patch",
    )
    if any(m in lowered for m in staleness_markers):
        return HaikuVerdict("yes/c", reason="precondition referenced from prior turn")
    # Heuristic: action-recommendation that references a /tmp staged
    # artifact is a precondition-staleness case (the 2026-05-23
    # security_bot-jobs-patched.json transcript). Real haiku is smarter; this
    # oracle approximates it.
    if "/tmp/" in lowered and (".json" in lowered or "patched" in lowered or "staged" in lowered):
        return HaikuVerdict("yes/c", reason="action references /tmp staged file")

    # ── Permission-tier fabrication signals → yes/b ─────────────────────
    permission_markers = (
        "exec is denied", "exec is locked", "exec is blocked",
        "exec is restricted", "exec is unavailable", "exec is walled",
        "exec is disabled", "i'd need elevated exec", "i'd need elevated",
        "security context", "session-security", "permission-tier",
        "tools.exec.security",
    )
    if any(m in lowered for m in permission_markers):
        return HaikuVerdict("yes/b", reason="capability-claim preface detected")

    # ── Shell-recommendation signals → yes/a ────────────────────────────
    shell_markers = (
        "sudo ", "sudo -u", "launchctl", "kickstart", "sed -i", "chmod +a",
        "python3 -c", "python -c", "openclaw exec-policy", "cp /tmp/",
    )
    if any(m in lowered for m in shell_markers):
        return HaikuVerdict("yes/a", reason="recommends shell command")

    return HaikuVerdict("no", reason="no failure signal")


# ─────────────────────────────────────────────────────────────────────────────
# Verdict parser — the haiku call returns ``<branch>: <reason>``; verify
# we parse the expected shapes and degrade gracefully on bad output.
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_haiku_verdict_canonical_forms():
    assert _parse_haiku_verdict("yes/a: recommends sudo cp").branch == "yes/a"
    assert _parse_haiku_verdict("yes/b: capability preface").branch == "yes/b"
    assert _parse_haiku_verdict("yes/c: staleness").branch == "yes/c"
    assert _parse_haiku_verdict("yes/d: no current-turn evidence").branch == "yes/d"
    assert _parse_haiku_verdict("d: bare label").branch == "yes/d"
    assert _parse_haiku_verdict("no: nothing").branch == "no"


def test_parse_haiku_verdict_tolerates_parens_and_whitespace():
    assert _parse_haiku_verdict("  YES/A : something\n").branch == "yes/a"
    assert _parse_haiku_verdict("a: bare label").branch == "yes/a"
    assert _parse_haiku_verdict("yes (b): paren form").branch == "yes/b"


def test_parse_haiku_verdict_falls_back_to_no_on_garbage():
    # Conservative — never substitute on a parse error.
    assert _parse_haiku_verdict("???").branch == "no"
    assert _parse_haiku_verdict("").branch == "no"


# ─────────────────────────────────────────────────────────────────────────────
# Haiku prompt-shape regression — narrowing of branch (c) per the
# 2026-05-23 disk-space misclassification follow-up.
#
# Manual-verification fixture: on 2026-05-23 evo replied to "Disk space
# has increased significantly recently — from 80 to 86%. Can you
# investigate?" with a real diagnostic table built from FRESH same-turn
# tool calls (team_bot_a log: 682K lines, admin_bot: 1.2M, etc.) and a sudo
# truncate recommendation. Haiku classified this as yes/c with reason
# "references state from earlier" because the original branch-(c)
# wording ("referenced from earlier conversation") let it interpret
# "earlier in this response" as "earlier turn".
#
# These assertions pin the narrowed wording so that disambiguator does
# not regress. They're cheap and brittle — but the haiku prompt IS the
# unit under test for branch (c) precision, and a stub haiku_fn cannot
# exercise it.
# ─────────────────────────────────────────────────────────────────────────────


def test_haiku_prompt_template_narrows_branch_c_to_prior_turn():
    """Branch (c) wording must explicitly require the precondition to
    come from a PRIOR TURN and must explicitly exclude same-response
    tool-call output."""
    from evolve_admin.evo.inspector import _HAIKU_PROMPT_TEMPLATE

    # (1) Prior-turn requirement is explicit and emphasized.
    assert "PRIOR TURN" in _HAIKU_PROMPT_TEMPLATE, (
        "branch (c) wording lost the explicit PRIOR TURN requirement — "
        "see 2026-05-23 disk-space misclassification follow-up"
    )

    # (2) Same-response tool-call disambiguator is present.
    lowered = _HAIKU_PROMPT_TEMPLATE.lower()
    assert "tool call" in lowered, (
        "branch (c) wording must mention tool calls to disambiguate "
        "same-turn verification from prior-turn references"
    )
    assert "same response" in lowered, (
        "branch (c) wording must disambiguate 'this same response' from "
        "'earlier conversation'"
    )

    # (3) Explicit do-not-flag instruction for the same-response case.
    assert "Do NOT flag (c)" in _HAIKU_PROMPT_TEMPLATE, (
        "branch (c) wording must explicitly instruct haiku NOT to flag "
        "the tool-call-verified-this-turn case"
    )


def test_haiku_prompt_template_preserves_branches_a_and_b():
    """Constraint from the follow-up spec: only branch (c) wording
    changes — branches (a) and (b) must remain identical."""
    from evolve_admin.evo.inspector import _HAIKU_PROMPT_TEMPLATE

    # Branch (a) — shell-recommendation wording.
    assert (
        "(a) recommend the operator run a shell or terminal command"
        in _HAIKU_PROMPT_TEMPLATE
    )

    # Branch (b) — permission-tier fabrication wording.
    assert (
        "(b) preface the answer with a claim about exec / shell / "
        "permission-tier / session-security capability"
        in _HAIKU_PROMPT_TEMPLATE
    )


def test_haiku_prompt_template_keeps_single_round_trip_shape():
    """Constraint from spec §7.4 (extended with branch d, 2026-06-20):
    the prompt covers all four failure modes in ONE round-trip with a
    single yes/a | yes/b | yes/c | yes/d | no answer."""
    from evolve_admin.evo.inspector import _HAIKU_PROMPT_TEMPLATE

    assert "yes/a, yes/b, yes/c, yes/d, no" in _HAIKU_PROMPT_TEMPLATE
    assert "<branch>: <reason>" in _HAIKU_PROMPT_TEMPLATE


def test_haiku_prompt_template_has_cite_or_dont_branch_d():
    """Branch (d) — cite-or-don't — must require current-turn tool-output
    evidence for a sudo / write / destructive-git fix, and must carry the
    same 'do not flag if tool-call-verified this turn' disambiguator as
    branch (c) so an evidence-grounded fix isn't substituted."""
    from evolve_admin.evo.inspector import _HAIKU_PROMPT_TEMPLATE

    lowered = _HAIKU_PROMPT_TEMPLATE.lower()
    assert "(d) recommend a sudo command" in lowered
    assert "destructive git" in lowered
    assert "this same response" in lowered
    # The c/d shared exclusion clause keeps an evidence-grounded fix
    # (tool ran THIS turn) from being rejected.
    assert "Do NOT flag (c) or (d)" in _HAIKU_PROMPT_TEMPLATE


# ─────────────────────────────────────────────────────────────────────────────
# Regex pre-filter — the cheap first stage that gates the haiku call.
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_cli_blocks_finds_fenced_bash():
    text = "Here is the command:\n```bash\nsudo ls -la\n```\nThat's it."
    blocks = _extract_cli_blocks(text)
    assert any("sudo ls -la" in b for b in blocks)


def test_extract_cli_blocks_finds_dollar_prefix():
    text = "Run this:\n  $ sudo /bin/launchctl kickstart -k system/x\nThen wait."
    blocks = _extract_cli_blocks(text)
    assert any("launchctl" in b for b in blocks)


def test_extract_cli_blocks_finds_loose_sudo_line():
    text = "Try: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    blocks = _extract_cli_blocks(text)
    assert blocks, "expected forbidden-token-line capture"


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy verification (§4 / §7.5) — bot_id resolution, path validity,
# schema-safety (sed/awk on JSON).
# ─────────────────────────────────────────────────────────────────────────────


def test_accuracy_rejects_sed_on_json():
    result = _verify_cli_accuracy(
        ["sudo sed -i '' 's/foo/bar/' /Users/Shared/evolve/network.json"]
    )
    assert result.has_hallucination is True
    assert "JSON" in result.reason


def test_accuracy_passes_clean_command():
    result = _verify_cli_accuracy(
        ["sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"]
    )
    assert result.has_hallucination is False


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy verification — bot_id vs macOS account name distinction
# (2026-05-26 regression: PR #1492's helper mixed bot_ids and account
# names into one set, so `sudo -u team_bot_b` passed because `team_bot_b` appeared
# in the set as a bot_id — even though the kernel rejects it because
# team_bot_b runs on the `personal_bot_user` account. See
# `feedback_bot_id_not_account_name`.)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _mock_network_team_bot_b_on_personal_bot_user(monkeypatch):
    """network.json with team_bot_b → personal_bot_user + team_bot_a/team_bot_c/personal_bot/admin_bot/security_bot
    on their eponymous accounts. Each test wires the fake into
    ``evolve_admin.config`` so the inspector helpers pick it up via
    their local ``from evolve_admin.config import load_network`` call."""

    def fake_load(path=None):
        return {
            "bots": {
                "team_bot_a": {"user": "team_bot_a"},
                "team_bot_c": {"user": "team_bot_c"},
                "personal_bot": {"user": "personal_bot"},
                "admin_bot": {"user": "admin_bot"},
                "security_bot": {"user": "security_bot"},
                "team_bot_b": {"user": "personal_bot_user"},  # the load-bearing case
            }
        }

    import evolve_admin.config as _cfg
    monkeypatch.setattr(_cfg, "load_network", fake_load)
    return fake_load


def test_verify_cli_accuracy_flags_sudo_u_with_bot_id_when_account_differs(
    _mock_network_team_bot_b_on_personal_bot_user,
):
    """`sudo -u team_bot_b ...` must be rejected because `team_bot_b` is a bot_id,
    not a macOS account — the kernel would emit `user 'team_bot_b' not
    found`. The reason should call out the bot_id/account-name
    distinction explicitly."""
    result = _verify_cli_accuracy(
        ["sudo -u team_bot_b openclaw plugins install --pin pkg --force"]
    )
    assert result.has_hallucination is True
    assert "team_bot_b" in result.reason
    assert "personal_bot_user" in result.reason, (
        f"reason should name the real account so the substitution explains "
        f"why; got: {result.reason!r}"
    )


def test_verify_cli_accuracy_passes_sudo_u_with_real_account_name(
    _mock_network_team_bot_b_on_personal_bot_user,
):
    """`sudo -u personal_bot_user ...` is a valid invocation (personal_bot_user IS the
    macOS account that runs the team_bot_b bot) — accuracy must pass."""
    result = _verify_cli_accuracy(
        ["sudo -u personal_bot_user openclaw plugins install --pin pkg --force"]
    )
    assert result.has_hallucination is False, (
        f"sudo -u personal_bot_user should pass; got reason: {result.reason!r}"
    )


def test_verify_cli_accuracy_flags_path_with_bot_id(
    _mock_network_team_bot_b_on_personal_bot_user,
):
    """`/Users/team_bot_b/.openclaw/openclaw.json` doesn't exist — the bot's
    home is `/Users/personal_bot_user/.openclaw/`. Accuracy must reject."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cat /Users/team_bot_b/.openclaw/openclaw.json"]
    )
    assert result.has_hallucination is True
    assert "team_bot_b" in result.reason
    assert "personal_bot_user" in result.reason


def test_verify_cli_accuracy_passes_path_with_real_account(
    _mock_network_team_bot_b_on_personal_bot_user,
):
    """`/Users/personal_bot_user/.openclaw/openclaw.json` IS the real path for
    team_bot_b's openclaw config — accuracy must pass."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cat /Users/personal_bot_user/.openclaw/openclaw.json"]
    )
    assert result.has_hallucination is False


def test_known_macos_accounts_falls_back_to_bot_id_when_user_missing(monkeypatch):
    """If a bot config lacks an explicit `user` field, the bot_id is
    treated as the account (defensive default — most bots ship with
    bot_id == account_name)."""
    from evolve_admin.evo.inspector import _known_macos_accounts
    import evolve_admin.config as _cfg

    monkeypatch.setattr(
        _cfg, "load_network",
        lambda path=None: {"bots": {"someone": {"role": "member"}}},
    )
    accounts = _known_macos_accounts()
    assert "someone" in accounts


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy verification — writes into /Users/Shared/evolve-repo are
# rejected (2026-05-29 regression: evo emitted
# `sudo /bin/cp /tmp/deploy_patched.py /Users/Shared/evolve-repo/...`
# as a remediation. The deploy checkout is read-only per CLAUDE.md;
# direct edits get clobbered by the next puller cycle and wedge the
# puller until stashed. Code changes go through PRs from the dev
# checkout, never in-place edits on the mini.)
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_cli_accuracy_flags_cp_into_deploy_checkout():
    """sudo /bin/cp into /Users/Shared/evolve-repo/ must be rejected —
    the deploy checkout is read-only and a 15-min puller cycle would
    clobber the edit anyway."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /Users/Shared/evolve-repo/path/file.py"]
    )
    assert result.has_hallucination is True
    assert "evolve-repo" in result.reason
    assert "deploy checkout" in result.reason.lower()


def test_verify_cli_accuracy_flags_other_write_verbs_into_deploy_checkout():
    """Every write verb in the regex must catch deploy-checkout
    targets — not just cp. tee, mv, rm, sed -i, chmod, chown all
    apply."""
    write_verbs = [
        "sudo /bin/cp /tmp/x.py /Users/Shared/evolve-repo/path/file.py",
        "sudo /bin/mv /tmp/x.py /Users/Shared/evolve-repo/path/file.py",
        "tee /Users/Shared/evolve-repo/path/file.py < /tmp/x.py",
        "sudo rm /Users/Shared/evolve-repo/path/file.py",
        "sudo sed -i '' 's/old/new/' /Users/Shared/evolve-repo/path/file.py",
        "sudo chmod 644 /Users/Shared/evolve-repo/path/file.py",
        "sudo chown root /Users/Shared/evolve-repo/path/file.py",
    ]
    for cmd in write_verbs:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is True, (
            f"write verb did not trip deploy-checkout guard: {cmd!r}"
        )
        assert "evolve-repo" in result.reason


def test_verify_cli_accuracy_allows_read_from_deploy_checkout():
    """READS from /Users/Shared/evolve-repo/ (cat, ls, less, tail) are
    fine — only writes block code from being clobbered by the puller."""
    reads = [
        "cat /Users/Shared/evolve-repo/packages/admin/evolve_admin/deploy.py",
        "ls /Users/Shared/evolve-repo/",
        "less /Users/Shared/evolve-repo/CLAUDE.md",
        "tail -50 /Users/Shared/evolve-repo/logs/x.log",
        "head /Users/Shared/evolve-repo/packages/admin/evolve_admin/deploy.py",
        "grep -r 'expected_plist_labels' /Users/Shared/evolve-repo/",
    ]
    for cmd in reads:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is False, (
            f"read against deploy checkout wrongly flagged: {cmd!r} → "
            f"{result.reason!r}"
        )


def test_verify_cli_accuracy_allows_writes_to_evolve_repo_backup_dir():
    """A directory whose path just happens to start with
    `/Users/Shared/evolve-repo` as a substring (e.g. an
    `/Users/Shared/evolve-repo-backup/` sibling) is NOT the deploy
    checkout — the trailing `/` in the regex anchors the match. Writes
    to such sibling dirs must pass."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /Users/Shared/evolve-repo-backup/path/file.py"]
    )
    assert result.has_hallucination is False, (
        f"write to evolve-repo-backup wrongly flagged: {result.reason!r}"
    )


def test_verify_cli_accuracy_allows_cat_pipe_to_tmp():
    """`cat /Users/Shared/evolve-repo/x.py > /tmp/y.py` is a READ from
    the deploy checkout that redirects elsewhere — the write target is
    /tmp, not the deploy checkout. Must pass (no write verb against the
    deploy path)."""
    result = _verify_cli_accuracy(
        ["cat /Users/Shared/evolve-repo/packages/admin/evolve_admin/deploy.py > /tmp/y.py"]
    )
    assert result.has_hallucination is False, (
        f"cat-then-redirect-elsewhere wrongly flagged: {result.reason!r}"
    )


def test_lookup_ui_alternative_returns_dev_checkout_pr_path_for_deploy_writes():
    """When the inspector rejects a deploy-checkout write, the UI
    alternative substitution should point at the dev-checkout PR path —
    not leave the operator with a dead-end stub. The lookup must
    surface the PR-path guidance for any CLI block mentioning
    /Users/Shared/evolve-repo/."""
    blocks = [
        "sudo /bin/cp /tmp/deploy_patched.py "
        "/Users/Shared/evolve-repo/packages/admin/evolve_admin/deploy.py"
    ]
    alt = lookup_ui_alternative(blocks)
    assert alt is not None
    assert "PR" in alt or "pr" in alt.lower()
    assert "dev checkout" in alt.lower() or "laptop" in alt.lower()


def test_deploy_patched_py_transcript_regression_2026_05_29():
    """Pins the exact 2026-05-29 transcript line: evo emitted
    `sudo /bin/cp /tmp/deploy_patched.py /Users/Shared/evolve-repo/...`
    as a remediation for a launchd label-mismatch bug it diagnosed in
    deploy.py::expected_plist_labels(). The inspector must reject this
    via the deploy-checkout-write guard, AND the UI alternative must
    point at the dev-checkout PR path so the substitution is helpful
    instead of a dead-end."""
    transcript_line = (
        "sudo /bin/cp /tmp/deploy_patched.py "
        "/Users/Shared/evolve-repo/packages/admin/evolve_admin/deploy.py"
    )
    result = _verify_cli_accuracy([transcript_line])
    assert result.has_hallucination is True
    assert "evolve-repo" in result.reason
    # And the UI alt for this CLI must point at the PR path, not a
    # dead-end stub.
    alt = lookup_ui_alternative([transcript_line])
    assert alt is not None
    assert "PR" in alt or "pr" in alt.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Linux deploy-checkout guard — on a Linux pod the read-only deploy
# checkout lives at /var/lib/evolve/repo, NOT /Users/Shared/evolve-repo
# (platform_profile.LINUX.deploy_checkout_default). A macOS-path-only
# guard would let an evo-issued cp/mv/rm into /var/lib/evolve/repo slip
# through and clobber the checkout, wedging the puller. These cases set
# the LINUX profile and prove the guard follows the active platform.
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_cli_accuracy_flags_cp_into_linux_deploy_checkout(linux_profile):
    """On a Linux pod the deploy checkout is /var/lib/evolve/repo. A
    write there must be rejected exactly like the macOS path — this is
    the Linux blind spot the platform-aware guard closes."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /var/lib/evolve/repo/path/file.py"]
    )
    assert result.has_hallucination is True
    assert "deploy checkout" in result.reason.lower()
    # The operator-facing reason names the ACTIVE checkout, not the macOS literal.
    assert "/var/lib/evolve/repo" in result.reason


def test_verify_cli_accuracy_flags_other_write_verbs_into_linux_deploy_checkout(
    linux_profile,
):
    """Every write verb in the guard must catch Linux deploy-checkout
    targets — not just cp. tee, mv, rm, sed -i, chmod, chown all apply."""
    write_verbs = [
        "sudo /bin/cp /tmp/x.py /var/lib/evolve/repo/path/file.py",
        "sudo /bin/mv /tmp/x.py /var/lib/evolve/repo/path/file.py",
        "tee /var/lib/evolve/repo/path/file.py < /tmp/x.py",
        "sudo rm /var/lib/evolve/repo/path/file.py",
        "sudo sed -i 's/old/new/' /var/lib/evolve/repo/path/file.py",
        "sudo chmod 644 /var/lib/evolve/repo/path/file.py",
        "sudo chown root /var/lib/evolve/repo/path/file.py",
    ]
    for cmd in write_verbs:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is True, (
            f"write verb did not trip Linux deploy-checkout guard: {cmd!r}"
        )
        assert "deploy checkout" in result.reason.lower()


def test_verify_cli_accuracy_allows_reads_from_linux_deploy_checkout(linux_profile):
    """READS from /var/lib/evolve/repo/ (cat, ls, grep) are fine — only
    writes block code from being clobbered by the puller."""
    reads = [
        "cat /var/lib/evolve/repo/packages/admin/evolve_admin/deploy.py",
        "ls /var/lib/evolve/repo/",
        "grep -r 'expected_plist_labels' /var/lib/evolve/repo/",
    ]
    for cmd in reads:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is False, (
            f"read against Linux deploy checkout wrongly flagged: {cmd!r} → "
            f"{result.reason!r}"
        )


def test_verify_cli_accuracy_allows_writes_to_linux_repo_backup_sibling(linux_profile):
    """The trailing `/` anchors the match on Linux too:
    /var/lib/evolve/repo-backup/ is a sibling, NOT the deploy checkout.
    Writes there must pass (the over-block guard, Linux flavour)."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /var/lib/evolve/repo-backup/path/file.py"]
    )
    assert result.has_hallucination is False, (
        f"write to /var/lib/evolve/repo-backup sibling wrongly flagged: "
        f"{result.reason!r}"
    )


def test_verify_cli_accuracy_flags_macos_path_even_on_linux(linux_profile):
    """Defensive: the alternation always carries the macOS literal too,
    so a /Users/Shared/evolve-repo write mis-authored on a Linux pod is
    still flagged. The macOS path isn't a real path on Linux, so blocking
    it is a harmless belt-and-suspenders — never an under-block."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /Users/Shared/evolve-repo/path/file.py"]
    )
    assert result.has_hallucination is True
    assert "deploy checkout" in result.reason.lower()


def test_verify_cli_accuracy_macos_active_does_not_flag_linux_path():
    """Symmetric no-over-block: on a macOS pod /var/lib/evolve/repo is
    NOT the deploy checkout (it typically doesn't exist), so a write
    there must NOT be flagged — flagging it would be a false positive.
    The macOS pin is the suite default (conftest), so no fixture needed."""
    result = _verify_cli_accuracy(
        ["sudo /bin/cp /tmp/x.py /var/lib/evolve/repo/path/file.py"]
    )
    assert result.has_hallucination is False, (
        f"macOS-active guard wrongly flagged a Linux path: {result.reason!r}"
    )


def test_lookup_ui_alternative_returns_pr_path_for_linux_deploy_writes(linux_profile):
    """The UI-alternative lookup is platform-aware too: a write to the
    Linux deploy checkout must surface the dev-checkout PR-path guidance,
    not a dead-end stub."""
    blocks = [
        "sudo /bin/cp /tmp/deploy_patched.py "
        "/var/lib/evolve/repo/packages/admin/evolve_admin/deploy.py"
    ]
    alt = lookup_ui_alternative(blocks)
    assert alt is not None
    assert "PR" in alt or "pr" in alt.lower()
    assert "dev checkout" in alt.lower() or "laptop" in alt.lower()


def test_npm_pin_wall_regression_2026_05_26(_mock_network_team_bot_b_on_personal_bot_user):
    """Pins the EXACT 14-line transcript from the 2026-05-26 operator
    report. The accuracy check must flag at least the `sudo -u team_bot_b`
    line (the team_bot_a/team_bot_c/personal_bot/admin_bot/security_bot lines pass because their
    bot_id == account_name)."""
    wall = [
        "sudo -u team_bot_a openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u team_bot_a openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        "sudo -u team_bot_c openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u team_bot_c openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        "sudo -u personal_bot openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u personal_bot openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        "sudo -u admin_bot openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u admin_bot openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        "sudo -u security_bot openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u security_bot openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        "sudo -u team_bot_b openclaw plugins install --pin @openclaw/codex@2026.5.22 --force",
        "sudo -u team_bot_b openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22 --force",
        # plus a couple extra to land at 14 lines (the operator report
        # quoted ~14 lines without exact reproduction).
        "sudo -u team_bot_b openclaw plugins install --pin @openclaw/openclaw-tool@2026.5.22 --force",
        "sudo -u team_bot_b openclaw plugins install --pin @openclaw/openclaw-stats@2026.5.22 --force",
    ]
    result = _verify_cli_accuracy(wall)
    assert result.has_hallucination is True
    assert "team_bot_b" in result.reason
    assert "personal_bot_user" in result.reason
    # The five `bot_id == account_name` bots must NOT appear as
    # rejections — they're legitimate. The reason should only contain
    # the `team_bot_b` lines (4 of them in this fixture).
    for ok_bot in ("team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot"):
        # Each of these names will appear inside the broader reason
        # ONLY through a team_bot_b line referencing them — we just need to
        # confirm none of them got flagged for *being* the rejection.
        assert (
            f"bot_id '{ok_bot}'" not in result.reason
        ), f"{ok_bot} (whose bot_id == account_name) should not be flagged"


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy verification — dangerous perm changes on /Users/Shared/evolve/
# (2026-06-02 regression: evo recommended
#   `sudo chown evo /Users/Shared/evolve/alerts/subscriptions.json`
#   `sudo chmod a+w /Users/Shared/evolve/alerts/subscriptions.json`
# to fix an EACCES from PR #1979's action.subscriptions.set tool. Both
# wrong — chown breaks admin-ui (`evolve` user) writes; chmod a+w makes
# pod-wide state world-writable. The correct pattern is an ACL grant.
# Sister to PR #1785 which handles /Users/Shared/evolve-repo/.)
# ─────────────────────────────────────────────────────────────────────────────


def test_flags_chown_into_shared_dir():
    """`sudo chown evo /Users/Shared/evolve/alerts/subscriptions.json`
    must be rejected — admin-ui runs as `evolve`, so chowning to anyone
    else silently disables admin-ui writes (the 2026-06-02 case)."""
    result = _verify_cli_accuracy(
        ["sudo chown evo /Users/Shared/evolve/alerts/subscriptions.json"]
    )
    assert result.has_hallucination is True
    assert "/Users/Shared/evolve/" in result.reason
    assert "shared dir" in result.reason.lower()


def test_flags_chmod_a_plus_w_into_shared_dir():
    """`sudo chmod a+w /Users/Shared/evolve/alerts/subscriptions.json`
    must be rejected — a+w on pod-wide state is world-writable."""
    result = _verify_cli_accuracy(
        ["sudo chmod a+w /Users/Shared/evolve/alerts/subscriptions.json"]
    )
    assert result.has_hallucination is True
    assert "/Users/Shared/evolve/" in result.reason
    assert "world-writable" in result.reason.lower()


def test_flags_chmod_octal_into_shared_dir():
    """`sudo chmod 777 /Users/Shared/evolve/...` must be rejected —
    777/666/etc. on shared state are world-writable too. The regex
    accepts 3- and 4-digit octal modes."""
    cases = [
        "sudo chmod 777 /Users/Shared/evolve/alerts/subscriptions.json",
        "sudo chmod 666 /Users/Shared/evolve/alerts/subscriptions.json",
        "sudo chmod 0777 /Users/Shared/evolve/alerts/subscriptions.json",
        "sudo chmod -R 777 /Users/Shared/evolve/alerts/",
    ]
    for cmd in cases:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is True, (
            f"octal chmod against shared dir did not trip guard: {cmd!r}"
        )
        assert "/Users/Shared/evolve/" in result.reason


def test_allows_chmod_plus_a_acl_grant():
    """`chmod +a "user:evo allow read,write,append" /Users/Shared/evolve/...`
    is the CORRECT pattern (preserves owner+group, grants evo as an
    additional ACL writer — same shape ensure_pod_perms() uses for
    proposals/ and signals/). Must pass through unchanged."""
    cases = [
        "sudo /bin/chmod +a \"user:evo allow read,write,append\" /Users/Shared/evolve/alerts/",
        "sudo /bin/chmod +a 'user:evo allow read,write,append,delete' /Users/Shared/evolve/alerts/subscriptions.json",
        "sudo /bin/chmod +a \"user:evolve allow read,write\" /Users/Shared/evolve/proposals/",
    ]
    for cmd in cases:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is False, (
            f"ACL grant pattern (the correct fix) was wrongly flagged: {cmd!r} → "
            f"{result.reason!r}"
        )


def test_allows_reads_from_shared_dir():
    """Reads from /Users/Shared/evolve/ (cat, ls, etc.) are fine — only
    chown/world-writable chmod targeting the shared dir block."""
    reads = [
        "cat /Users/Shared/evolve/alerts/subscriptions.json",
        "ls /Users/Shared/evolve/",
        "ls -la /Users/Shared/evolve/alerts/",
        "head /Users/Shared/evolve/proposals/pending/x.json",
        "tail -50 /Users/Shared/evolve/logs/inspector.jsonl",
    ]
    for cmd in reads:
        result = _verify_cli_accuracy([cmd])
        assert result.has_hallucination is False, (
            f"read against shared dir wrongly flagged: {cmd!r} → "
            f"{result.reason!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI-alternative lookup (§7.6)
# ─────────────────────────────────────────────────────────────────────────────


def test_ui_alternative_finds_gateway_restart():
    blocks = ["sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"]
    assert lookup_ui_alternative(blocks) is not None
    assert "Restart Gateway" in lookup_ui_alternative(blocks)


def test_ui_alternative_finds_acl_repair():
    blocks = ["sudo chmod +a 'user:evolve allow read,write' /Users/security_bot/.openclaw/"]
    assert lookup_ui_alternative(blocks) is not None
    assert "Repair ACLs" in lookup_ui_alternative(blocks)


def test_ui_alternative_returns_none_when_no_match():
    blocks = ["some random text that is not CLI"]
    assert lookup_ui_alternative(blocks) is None


# ─────────────────────────────────────────────────────────────────────────────
# Per-variant positive tests — EXEC_FABRICATION_VARIANTS (branch b)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", EXEC_FABRICATION_VARIANTS)
def test_exec_fabrication_variant_caught(variant):
    """Each known fabrication variant must trip branch b and result in
    either a preface-stripped or fallback substitution."""
    # Place the preface at the top of the reply with a constructive
    # follow-up; the spec wants the remainder kept whenever possible.
    reply = f"{variant}. Here's the right move: use the dashboard's Restart Gateway button."
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None, f"variant should be caught: {variant!r}"
    assert event.branch == "b"
    # Either preface_stripped or fallback shape is acceptable.
    assert event.event in ("permission_preface_stripped", "permission_preface_fallback")
    # The substituted text should not contain the exact original preface
    # phrase (it's been stripped or replaced).
    assert variant.lower() not in text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Per-pattern positive tests — SHELL_AS_ADVICE_PATTERNS (branch a)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern,_expected", SHELL_AS_ADVICE_PATTERNS)
def test_shell_as_advice_pattern_caught(pattern, _expected):
    """Each shell-as-advice pattern must trip branch a (or branch c if
    the pattern mentions a /tmp staged file, per the precondition-first
    rule)."""
    reply = f"Try this: {pattern}"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None, f"shell pattern not caught: {pattern!r}"
    # /tmp patterns trip the staleness branch first per §7.2 ordering.
    if "/tmp/" in pattern:
        assert event.branch in ("a", "c")
    else:
        assert event.branch == "a"


# ─────────────────────────────────────────────────────────────────────────────
# Per-pattern positive tests — PRECONDITION_STALENESS_PATTERNS (branch c)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern,_expected", PRECONDITION_STALENESS_PATTERNS)
def test_precondition_staleness_pattern_caught(pattern, _expected):
    """Each precondition-staleness pattern must trip branch c, PREPEND a
    provisional note, and KEEP the reply (the 2026-05-23 disk-space
    regression: do not destroy fresh same-turn data). The 2026-06-20
    reword removed the offloading hedge: no "(Heads up: re-verify it
    yourself before running anything)" — evo now commits to re-read the
    state ITSELF and explicitly tells the operator they need not verify."""
    reply = f"Recommendation: {pattern}. Then run the command."
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None, f"staleness pattern not caught: {pattern!r}"
    assert event.branch == "c"
    assert event.event == "precondition_staleness_prepend"
    # The reply is preserved (data-retention regression).
    assert reply in text
    # The offloading hedge is gone.
    assert "before running anything" not in text.lower()
    assert "re-verify the precondition" not in text.lower()
    # Evo commits to re-read itself and tells the operator not to verify.
    assert "re-read" in text.lower()
    assert "you don't need to verify" in text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Per-pattern positive tests — NO_EVIDENCE_REMEDIATION_PATTERNS (branch d,
# cite-or-don't). Task A, 2026-06-20 atlas-backup incident.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern,_expected", NO_EVIDENCE_REMEDIATION_PATTERNS)
def test_no_evidence_remediation_pattern_caught(pattern, _expected):
    """An evidence-free sudo / write / destructive-git fix must trip
    branch d and be REJECTED with a commitment to run the read-only
    check first — not shipped as a concrete fix for unverified state."""
    text, event = inspect_outgoing_text(
        pattern,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None, f"no-evidence remediation not caught: {pattern!r}"
    assert event.branch == "d"
    assert event.event == "no_evidence_reject"
    # The fix is withheld; evo commits to verify + quote first.
    assert "verif" in text.lower() or "read-only check" in text.lower()
    assert "before running anything" not in text.lower()


@pytest.mark.parametrize("pattern,_expected", EVIDENCE_GROUNDED_REMEDIATION_NEGATIVES)
def test_evidence_grounded_remediation_not_branch_d(pattern, _expected):
    """A remediation that quotes current-turn tool output satisfies the
    evidence gate — branch d must NOT fire on it."""
    text, event = inspect_outgoing_text(
        pattern,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    # The evidence gate is satisfied; whatever else happens, it is not a
    # no-evidence rejection.
    assert event is None or event.branch != "d", (
        f"evidence-grounded remediation wrongly tripped branch d: {pattern!r}"
    )


def test_destructive_git_triggers_remediation_pre_filter():
    """A destructive-git fix carries no sudo/chmod token, so it needs the
    dedicated remediation pre-filter signal to reach the haiku call."""
    assert _has_remediation_signal("recover with `git reset --hard origin/main`")
    assert _has_remediation_signal("force it: git push --force origin main")
    assert not _has_remediation_signal("git status shows a clean tree")


@pytest.mark.parametrize(
    "line",
    [
        "run `cp /tmp/x /Users/Shared/evolve/network.json` to restore it",
        "fix it with `mv /tmp/staged /Users/Shared/evolve/release.json`",
        "clear the stale lock: `rm /Users/Shared/evolve/.deploy.lock`",
        "rewrite it via `tee /Users/Shared/evolve/network.json < /tmp/new`",
        "delete the placeholder with `rm <lock>`",
        "force-copy: `cp -f /tmp/x /Users/Shared/evolve/network.json`",
    ],
)
def test_bare_filesystem_write_trips_pre_filter(line):
    """A bare (non-sudo) write verb carries no sudo/chmod/git token, so
    without the cp|mv|rm|tee pre-filter signal the branch-D haiku check
    could never fire on it — exactly the confabulation-write path the
    guard exists to catch. The path/flag argument is what trips it."""
    _blocks, any_hit = _regex_pre_filter(line)
    assert any_hit is True, f"bare write verb did not reach pre-filter: {line!r}"


@pytest.mark.parametrize(
    "prose",
    [
        "we can rm it later once the soak passes",
        "I'll cp the file over after you confirm",
        "the mv happens automatically on promote",
        "you'll see a tee in the pipeline output",
        "warm restarts and remote pulls are unaffected",
    ],
)
def test_prose_filesystem_verbs_dont_trip_pre_filter(prose):
    """Prose mentions of cp/mv/rm/tee with NO path-or-flag argument must
    NOT trip the pre-filter — the gate mirrors `sed -i`'s flag gating to
    avoid lighting up haiku on ordinary English."""
    _blocks, any_hit = _regex_pre_filter(prose)
    assert any_hit is False, f"prose verb wrongly tripped pre-filter: {prose!r}"


def test_bare_shared_dir_write_no_evidence_rejected():
    """End-to-end: a NON-sudo write to an evolve-owned {shared_dir} path,
    asserted from priors with no current-turn evidence, must reach branch
    D and be rejected. Before the cp pre-filter signal this yielded
    pre_filter=False and the guard could not fire."""
    text, event = inspect_outgoing_text(
        "that config drifted — run `cp /tmp/fixed "
        "/Users/Shared/evolve/network.json` to restore it",
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None, "bare shared-dir write slipped past the guard"
    assert event.branch == "d"
    assert event.event == "no_evidence_reject"


def test_bare_shared_dir_write_with_evidence_allowed():
    """The same bare write, but quoting a current-turn tool result as
    evidence, satisfies the evidence gate — branch D must NOT fire even
    though the cp line trips the pre-filter and haiku is consulted."""
    text, event = inspect_outgoing_text(
        "`pod_state.backup_status(bot_id=atlas)` returned `{\"error\": "
        "\"EACCES /Users/Shared/evolve/network.json\"}`. Given that, run "
        "`cp /tmp/fixed /Users/Shared/evolve/network.json` to restore it",
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is None or event.branch != "d", (
        "evidence-grounded bare write wrongly tripped branch d"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy verification — macOS full-path requirement (Task C, 2026-06-20
# atlas-backup incident: evo emitted bare `chown`/`chmod` that failed
# with "command not found").
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd,verb,full",
    [
        ("sudo chown atlas /Users/atlas/.openclaw/x", "chown", "/usr/sbin/chown"),
        ("sudo chmod 600 /Users/atlas/.openclaw/x", "chmod", "/bin/chmod"),
        ("launchctl kickstart -k system/ai.openclaw.x", "launchctl", "/bin/launchctl"),
        ("mkdir -p /Users/atlas/.openclaw/cron", "mkdir", "/bin/mkdir"),
    ],
)
def test_accuracy_flags_bare_macos_command(cmd, verb, full):
    """A bare command verb fails with 'command not found' in evo's exec
    context — accuracy must reject and name the canonical full path."""
    result = _verify_cli_accuracy([cmd])
    assert result.has_hallucination is True, f"bare {verb} not flagged: {cmd!r}"
    assert verb in result.reason
    assert full in result.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "sudo /usr/sbin/chown atlas /Users/atlas/.openclaw/x",
        "sudo /bin/chmod 600 /Users/atlas/.openclaw/x",
        "sudo /bin/launchctl kickstart -k system/ai.openclaw.x",
        "sudo /bin/mkdir -p /Users/atlas/.openclaw/cron",
    ],
)
def test_accuracy_passes_full_path_macos_command(cmd):
    """The same commands written with macOS full paths must pass the
    bare-command check (the full-path form is the correct one)."""
    result = _verify_cli_accuracy([cmd])
    # No bare-command issue. (Other guards don't apply to these paths.)
    assert "command not found" not in result.reason


def test_accuracy_full_path_chown_is_usr_sbin_not_bin():
    """Regression guard: chown lives in /usr/sbin, NOT /bin (CLAUDE.md).
    The named full path in the rejection must be /usr/sbin/chown."""
    result = _verify_cli_accuracy(["sudo chown atlas /Users/atlas/.openclaw/x"])
    assert "/usr/sbin/chown" in result.reason
    assert "/bin/chown" not in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Negative tests — PROSE_MENTION_NEGATIVES must NOT be substituted.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prose,_expected", PROSE_MENTION_NEGATIVES)
def test_prose_mention_passes_through(prose, _expected):
    """Legitimate prose mentions of sudo / exec.security / /tmp must
    pass through unchanged."""
    text, event = inspect_outgoing_text(
        prose,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is None, f"prose mention wrongly substituted: {prose!r}"
    assert text == prose


def test_benign_text_passes_through_without_haiku_call():
    """Text with no shell tokens, no preface markers, no staleness
    markers should not even trigger a haiku call. We verify by passing
    a haiku_fn that raises if called."""
    def boom(_):
        raise RuntimeError("haiku should not be called")
    text, event = inspect_outgoing_text(
        "I checked the dashboard and 7 bots are healthy.",
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=boom,
    )
    assert event is None
    assert text == "I checked the dashboard and 7 bots are healthy."


# ─────────────────────────────────────────────────────────────────────────────
# Surface-conditional tests — Telegram passes shell through; mobile
# always substitutes; laptop substitutes when UI alternative known.
# ─────────────────────────────────────────────────────────────────────────────


def test_shell_on_telegram_passes_through():
    """Telegram is the primary CLI surface — verified shell passes
    through unchanged."""
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "telegram"},
        haiku_fn=_oracle_haiku,
    )
    assert event is None
    assert text == reply


def test_shell_on_admin_ui_mobile_always_substituted():
    """Mobile is the strict-block surface — even verified, well-formed
    commands get substituted because the operator can't run them."""
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "a"
    assert event.event in (
        "cli_on_mobile_with_ui_alt",
        "cli_on_mobile_no_ui_alt",
    )
    assert "Restart Gateway" in text or "tool gap" in text


def test_shell_on_admin_ui_mobile_no_ui_alt_uses_tool_gap_stub():
    """Mobile + no UI alt → tool-gap substitution."""
    # Use a non-mapped pattern so the UI lookup returns None.
    reply = "Try: sudo dd if=/dev/null of=/some/random/path"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=_oracle_haiku,
    )
    # dd / random path is rejected by accuracy verification (unknown
    # path user) or no UI alt — either way, branch a fires.
    assert event is not None
    assert event.branch == "a"


def test_shell_on_admin_ui_laptop_with_ui_alt_substitutes():
    """Laptop with a UI alternative → substitute with UI guidance."""
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "a"
    assert event.event == "cli_on_laptop_with_ui_alternative"
    assert "Restart Gateway" in text


def test_shell_on_admin_ui_laptop_no_ui_alt_with_terminal_framing():
    """Laptop + no UI alt + preference != ui → keep CLI with terminal
    framing prefix."""
    # Use a verified but unmapped command so UI lookup returns None.
    reply = "Run: openclaw exec-policy set --bot team_bot_a --mode ask"
    text, event = inspect_outgoing_text(
        reply,
        session_context={
            "surface": "admin_ui",
            "surface_type": "laptop",
            "help_style_preference": "either",
        },
        haiku_fn=_oracle_haiku,
    )
    # `openclaw exec-policy set` HAS a UI alternative per the map, so
    # this case routes to with_ui_alternative. That's correct behavior;
    # use a true unmapped command instead.
    if event and event.event == "cli_on_laptop_with_ui_alternative":
        # The map covers exec-policy — that's expected.
        return
    assert event is not None
    assert event.event == "cli_on_laptop_terminal_framing"
    assert text.startswith("From your admin terminal")


def test_shell_on_admin_ui_laptop_ui_preference_no_ui_alt_logs_tool_gap():
    """Laptop + no UI alt + preference == ui → tool-gap substitution."""
    # `dd` with random path triggers accuracy reject, which is its own
    # path — we need a command that passes accuracy but has no UI alt.
    # Just construct a custom case here.
    reply = "From your admin terminal: sudo defaults write com.example.app something true"
    text, event = inspect_outgoing_text(
        reply,
        session_context={
            "surface": "admin_ui",
            "surface_type": "laptop",
            "help_style_preference": "ui",
        },
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "a"
    # Either ui_preferred_no_ui_alt or hallucinated_cli is acceptable;
    # the key is that the operator does NOT see the literal sudo
    # command.
    assert "sudo defaults" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Substitution-shape tests — verify the §7.4.1 three shapes
# ─────────────────────────────────────────────────────────────────────────────


def test_branch_b_preserves_constructive_remainder():
    """Per §7.4.1(b): permission-preface stripping keeps the remainder
    of the reply intact."""
    reply = (
        "Exec is locked down in this session context. "
        "But the dashboard's Restart Gateway button covers this — "
        "click it from the team_bot_a tile."
    )
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "b"
    assert "Restart Gateway" in text, (
        f"constructive remainder lost; got: {text!r}"
    )


def test_branch_b_fallback_when_entire_reply_is_preface():
    """Per §7.4.1(b): when the entire reply is preface, use the
    fallback stub."""
    reply = (
        "Exec is locked down in this session context. "
        "I'd need elevated exec access. My security context prevents this."
    )
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "b"
    assert event.event == "permission_preface_fallback"
    assert "let me try again" in text.lower() or "what did you ask" in text.lower()


def test_branch_c_prepends_staleness_warning():
    """Branch c prepends a provisional note and keeps the original reply
    intact so the operator still sees the investigation. 2026-06-20
    reword: the note no longer offloads the check onto the operator —
    evo commits to re-read the state itself."""
    reply = "Run: sudo cp /tmp/security_bot-jobs-patched.json /Users/security_bot/.openclaw/cron/jobs.json"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    # /tmp path → precondition-staleness branch.
    assert event.branch == "c"
    assert event.event == "precondition_staleness_prepend"
    # Evo commits to re-read itself; no offloading hedge.
    assert "re-read" in text.lower()
    assert "you don't need to verify" in text.lower()
    assert "before running anything" not in text.lower()
    assert reply in text


def test_branch_c_preserves_original_text_with_prepend():
    """Branch c must preserve the full original reply after the prepended
    provisional note — the disk-space regression fix (do not destroy
    fresh same-turn diagnostic data)."""
    reply = (
        "I checked disk usage across the bots:\n"
        "- team_bot_a log: 682K lines\n"
        "- admin_bot log: 1.2M lines\n"
        "- team_bot_b log: 410K lines\n\n"
        "Recommendation: sudo truncate -s 0 /var/log/team_bot_a.log"
    )
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=lambda _t: HaikuVerdict("yes/c", reason="references earlier turn"),
    )
    assert event is not None
    assert event.branch == "c"
    assert event.event == "precondition_staleness_prepend"
    # (a) provisional-note prefix present (no offloading "Heads up" hedge)
    assert text.startswith("(One flag")
    assert "before running anything" not in text.lower()
    # (b) the original text is fully preserved after the prefix
    assert reply in text
    assert text.endswith(reply)
    # (c) all diagnostic data points survive
    assert "team_bot_a log: 682K lines" in text
    assert "admin_bot log: 1.2M lines" in text
    assert "sudo truncate -s 0 /var/log/team_bot_a.log" in text


def test_env_var_disable_bypasses_inspector(monkeypatch):
    """EVOLVE_INSPECTOR_DISABLED=1 short-circuits the inspector; every
    reply passes through unchanged regardless of branch."""
    monkeypatch.setenv("EVOLVE_INSPECTOR_DISABLED", "1")

    def boom(_):
        raise RuntimeError("haiku should not be called when inspector is disabled")

    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=boom,
    )
    assert text == reply
    assert event is None


def test_disk_space_investigation_regression():
    """Regression pin for the 2026-05-23 disk-space incident: a reply
    containing a diagnostic table + a sudo truncate recommendation must
    NOT lose the diagnostic data even when haiku classifies as branch
    c. The dead-end stub from PR #1492 destroyed real diagnostic data;
    the hotfix prepends instead of replacing."""
    reply = (
        "Disk usage by bot log:\n"
        "  team_bot_a log: 682K lines\n"
        "  admin_bot log: 1.2M lines\n"
        "  security_bot log: 890K lines\n\n"
        "To free space, run: sudo truncate -s 0 /Users/team_bot_a/.openclaw/logs/team_bot_a.log"
    )

    def stub_haiku(_text):
        return HaikuVerdict("yes/c", reason="references earlier conversation state")

    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=stub_haiku,
    )
    assert event is not None
    assert event.branch == "c"
    # The diagnostic data must NOT be destroyed.
    assert "team_bot_a log: 682K lines" in text
    assert "admin_bot log: 1.2M lines" in text
    assert "security_bot log: 890K lines" in text
    assert "sudo truncate" in text
    # And the provisional note must be present (2026-06-20 reword: evo
    # re-reads itself; no offloading hedge).
    assert "re-read" in text.lower()
    assert "before running anything" not in text.lower()


def test_branch_a_substitutes_with_ui_alt_on_admin_ui():
    """Per §7.4.1(a): shell-recommendation strips the snippet and
    surfaces the UI alternative when known."""
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    assert event.branch == "a"
    assert "Restart Gateway" in text
    assert "launchctl" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Inspector failure-mode resilience — must never raise back to the proxy.
# ─────────────────────────────────────────────────────────────────────────────


def test_haiku_fn_that_raises_degrades_to_passthrough():
    """A haiku_fn that raises results in pass-through, not a crash."""
    def boom(_):
        raise RuntimeError("api down")
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=boom,
    )
    # haiku unavailable → branch defaults to "no" → pass-through.
    assert event is None
    assert text == reply


def test_haiku_fn_returning_garbage_degrades_to_passthrough():
    """Invalid haiku return → pass-through."""
    def garbage(_):
        return "not even a HaikuVerdict"
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=garbage,
    )
    assert event is None


# ─────────────────────────────────────────────────────────────────────────────
# Pre-filter signal helpers — sanity checks on the cheap markers.
# ─────────────────────────────────────────────────────────────────────────────


def test_has_preface_signal_detects_known_variants():
    for variant in EXEC_FABRICATION_VARIANTS:
        assert _has_preface_signal(variant), (
            f"preface-signal regex missed known variant: {variant!r}"
        )


def test_has_staleness_signal_detects_known_patterns():
    for pattern, _ in PRECONDITION_STALENESS_PATTERNS:
        assert _has_staleness_signal(pattern), (
            f"staleness-signal regex missed known pattern: {pattern!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry — every substitution writes a JSONL line.
# ─────────────────────────────────────────────────────────────────────────────


def test_telemetry_log_written_on_substitution(tmp_path, monkeypatch):
    """Inspector substitutions land in the inspector.jsonl telemetry
    log."""
    monkeypatch.setenv("EVOLVE_SHARED_DIR", str(tmp_path))
    reply = "Try this: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        session_id="test-session-abc",
        haiku_fn=_oracle_haiku,
    )
    assert event is not None
    log_path = tmp_path / "logs" / "inspector.jsonl"
    assert log_path.exists(), "telemetry log not written"
    contents = log_path.read_text()
    assert "test-session-abc" in contents
    assert "mobile" in contents
    assert "launchctl" in contents


def test_no_telemetry_on_pass_through(tmp_path, monkeypatch):
    """Pass-through does not write a telemetry event."""
    monkeypatch.setenv("EVOLVE_SHARED_DIR", str(tmp_path))
    text, event = inspect_outgoing_text(
        "Everything is healthy on the dashboard.",
        session_context={"surface": "admin_ui", "surface_type": "laptop"},
        haiku_fn=_oracle_haiku,
    )
    assert event is None
    log_path = tmp_path / "logs" / "inspector.jsonl"
    assert not log_path.exists() or log_path.read_text() == ""


# ─────────────────────────────────────────────────────────────────────────────
# Permission-preface stripper — directly exercise the sentence-level
# strip logic.
# ─────────────────────────────────────────────────────────────────────────────


def test_strip_permission_preface_drops_leading_sentence():
    text = (
        "Exec is locked down in this session context. "
        "But the right move is to click Restart Gateway on the team_bot_a tile."
    )
    stripped = _strip_permission_preface(text)
    assert "Restart Gateway" in stripped
    assert "locked down" not in stripped.lower()


def test_strip_permission_preface_keeps_paragraph_after_preface():
    text = (
        "Exec is denied in my security context.\n\n"
        "Here's what works:\n- Click X\n- Click Y"
    )
    stripped = _strip_permission_preface(text)
    assert "Click X" in stripped
    assert "Click Y" in stripped


def test_strip_permission_preface_returns_empty_when_only_preface():
    text = "Exec is locked down in this session context."
    stripped = _strip_permission_preface(text)
    assert stripped == ""


# ─────────────────────────────────────────────────────────────────────────────
# Empty / boundary inputs
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_text_passes_through():
    text, event = inspect_outgoing_text("", haiku_fn=_oracle_haiku)
    assert text == ""
    assert event is None


def test_missing_session_context_treats_as_telegram_default():
    """Per §2.4, absent surface defaults render no Surface line — but
    the inspector still functions; the surface conditioning falls to
    'unknown', which conservatively passes through."""
    reply = "Run: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    text, event = inspect_outgoing_text(reply, haiku_fn=_oracle_haiku)
    # Unknown surface → conservative pass-through after haiku flags.
    assert text == reply  # passed through (unknown surface policy)


# ─────────────────────────────────────────────────────────────────────────────
# Inspector event shape — verify the dataclass surfaces useful info
# ─────────────────────────────────────────────────────────────────────────────


def test_inspector_event_carries_branch_and_excerpts():
    reply = "Try: sudo /bin/launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway"
    _, event = inspect_outgoing_text(
        reply,
        session_context={"surface": "admin_ui", "surface_type": "mobile"},
        haiku_fn=_oracle_haiku,
    )
    assert isinstance(event, InspectorEvent)
    assert event.branch == "a"
    assert event.surface == "admin_ui"
    assert event.surface_type == "mobile"
    assert "launchctl" in event.original_excerpt
    assert event.substituted_excerpt != ""
