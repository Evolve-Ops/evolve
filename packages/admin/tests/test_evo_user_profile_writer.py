"""Tests for evo.wizard.user_profile_writer.

Spec: docs/spec-user-profile-2026-05-07.md §D7.

Two layers under test:
1. ``build_profile_from_extracted`` — pure mapping from wizard extracted
   dict to v2 UserProfile sections. No I/O.
2. ``write_user_profile_via_sudo`` and ``commit`` — exercises the sudo
   subprocess sequence with a stub runner. Verifies the right commands
   fire in the right order, but does NOT shell out for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# sys.path setup: admin tests run with packages/admin and packages/analyzer
# both on the path; the conftest in this dir handles it.
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from evolve_admin.evo.wizard.user_profile_writer import (  # noqa: E402
    build_profile_from_extracted,
    commit,
    set_subprocess_runner,
    write_user_profile_via_sudo,
)
from user_profile import (  # noqa: E402
    UserProfile,
    UserProfileFrontmatter,
)


# ─────────────────────────────────────────────────────────────────────────────
# build_profile_from_extracted — mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_mapping_name_lands_in_demographics():
    p = build_profile_from_extracted(
        extracted={"name": "Pod_admin"}, user_key="u", bot_id="team_bot_a"
    )
    assert "Goes by: Pod_admin" in p.sections["Demographics"]


def test_mapping_role_environment_tooling_land_in_vocation():
    p = build_profile_from_extracted(
        extracted={
            "role": "Engineering lead",
            "environment": "Small startup",
            "current_tooling": ["Slack", "GitHub", "Notion"],
        },
        user_key="u",
        bot_id="team_bot_a",
    )
    body = p.sections["Vocation"]
    assert "- Role: Engineering lead" in body
    assert "- Environment: Small startup" in body
    assert "- Tools: Slack, GitHub, Notion" in body


def test_mapping_workflow_notes_appended_as_paragraph():
    p = build_profile_from_extracted(
        extracted={
            "role": "Designer",
            "current_workflow_notes": "Mostly works in Figma; uses Linear for tickets.",
        },
        user_key="u",
        bot_id="team_bot_a",
    )
    body = p.sections["Vocation"]
    assert "- Role: Designer" in body
    assert "Mostly works in Figma" in body


def test_mapping_top_goals_and_pain_points_in_goals():
    p = build_profile_from_extracted(
        extracted={
            "top_goals": ["Ship the book", "Stay focused"],
            "pain_points": ["Email pile", "Meeting overload"],
        },
        user_key="u",
        bot_id="team_bot_a",
    )
    body = p.sections["Goals"]
    assert "- Ship the book" in body
    assert "- Stay focused" in body
    assert "Pain points to address:" in body
    assert "- Email pile" in body
    assert "- Meeting overload" in body


def test_mapping_skips_empty_or_whitespace_values():
    p = build_profile_from_extracted(
        extracted={
            "name": "  ",
            "role": "",
            "top_goals": [],
            "current_tooling": [None, "", "  "],
        },
        user_key="u",
        bot_id="team_bot_a",
    )
    # Nothing meaningful was extracted, so no section content was set.
    assert (p.sections.get("Demographics") or "").strip() == ""
    assert (p.sections.get("Vocation") or "").strip() == ""
    assert (p.sections.get("Goals") or "").strip() == ""


def test_mapping_writes_audit_log_entry():
    p = build_profile_from_extracted(
        extracted={"name": "Pod_admin", "role": "TL"},
        user_key="u",
        bot_id="team_bot_a",
    )
    audit = p.sections.get("Audit Log", "")
    assert "wizard" in audit
    assert "name" in audit
    assert "role" in audit


def test_mapping_replaces_owned_prefix_lines_on_rerun():
    """Wizard re-run replaces lines matching a wizard-owned prefix
    (e.g. ``- Role:``) but does NOT touch other lines in the same
    section. This is what keeps inferrer-added content alive across
    wizard re-runs."""
    existing = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="u", bot_id="team_bot_a"),
        sections={"Vocation": "- Role: Old role"},
    )
    p = build_profile_from_extracted(
        extracted={"role": "New role"},
        user_key="u",
        bot_id="team_bot_a",
        existing=existing,
    )
    body = p.sections["Vocation"]
    # Wizard-owned `- Role:` line is replaced
    assert "Old role" not in body
    assert "- Role: New role" in body


def test_mapping_preserves_inferrer_added_lines_on_rerun():
    """Regression guard: an earlier draft did wholesale section replacement,
    which silently nuked facts the inferrer had accumulated. The fix is
    line-level: only wizard-owned prefixes (Goes by:, - Role:, - Environment:,
    - Tools:) get replaced; other lines stay."""
    existing = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="u", bot_id="team_bot_a"),
        sections={
            "Demographics": "Goes by: Pod_admin\n- Lives in Berlin",
            "Vocation": "- Role: Old role\n- Tools: VS Code\n- Inferred: enjoys deep work",
        },
    )
    p = build_profile_from_extracted(
        extracted={
            "name": "Pod_admin",
            "role": "New role",
            "current_tooling": ["VS Code", "Linear"],
        },
        user_key="u",
        bot_id="team_bot_a",
        existing=existing,
    )
    demographics = p.sections["Demographics"]
    vocation = p.sections["Vocation"]

    # Wizard-owned lines updated to new values
    assert "Goes by: Pod_admin" in demographics
    assert "- Role: New role" in vocation
    assert "Old role" not in vocation
    # Wizard-owned `- Tools:` line replaced with the new tool list
    assert "- Tools: VS Code, Linear" in vocation
    # Inferrer-added lines (no owned prefix) survive
    assert "- Lives in Berlin" in demographics
    assert "- Inferred: enjoys deep work" in vocation


def test_mapping_appends_goals_with_dedup():
    """Goals has no wizard-owned prefix — multiple wizard runs append
    new goals with exact-match deduplication so re-running with the
    same answer doesn't produce duplicates."""
    existing = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="u", bot_id="team_bot_a"),
        sections={"Goals": "- Earlier goal\n- Ship the book"},
    )
    p = build_profile_from_extracted(
        extracted={"top_goals": ["Ship the book", "Brand-new goal"]},
        user_key="u",
        bot_id="team_bot_a",
        existing=existing,
    )
    body = p.sections["Goals"]
    # Existing inferrer/wizard goals preserved
    assert "- Earlier goal" in body
    # Duplicate "Ship the book" not added twice
    assert body.count("- Ship the book") == 1
    # New goal landed
    assert "- Brand-new goal" in body


def test_mapping_preserves_tracking_enabled_from_existing():
    """If the user has DNT on, a wizard re-run must NOT silently flip it
    back to true. Frontmatter from existing profile wins."""
    existing = UserProfile(
        frontmatter=UserProfileFrontmatter(
            user_key="u", bot_id="team_bot_a", tracking_enabled=False
        ),
        sections={},
    )
    p = build_profile_from_extracted(
        extracted={"name": "Pod_admin"},
        user_key="u",
        bot_id="team_bot_a",
        existing=existing,
    )
    assert p.tracking_enabled is False


def test_mapping_rejects_cross_bot_write():
    """Defensive: if the existing profile is for a different bot, refuse."""
    existing = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="u", bot_id="team_bot_c"),
        sections={},
    )
    with pytest.raises(ValueError):
        build_profile_from_extracted(
            extracted={"name": "Pod_admin"},
            user_key="u",
            bot_id="team_bot_a",  # mismatch
            existing=existing,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Stub subprocess runner
# ─────────────────────────────────────────────────────────────────────────────


class _SubprocessRecorder:
    """Records every subprocess call; emulates files being written."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, capture_output=True, text=True, **kwargs):
        self.calls.append(list(cmd))
        # Emulate the effect of `mkdir -p`, `cp`, `chmod`, `chown` so the
        # filesystem state matches what the production path produces.
        if len(cmd) >= 4 and cmd[0] == "sudo":
            verb = cmd[1]
            if verb == "/bin/mkdir" and "-p" in cmd:
                target = Path(cmd[-1])
                target.mkdir(parents=True, exist_ok=True)
            elif verb == "/bin/cp":
                src, dst = cmd[2], cmd[3]
                Path(dst).write_bytes(Path(src).read_bytes())
            elif verb == "/bin/chmod":
                # ignore — tmp_path checks don't enforce mode in CI
                pass
            elif verb == "/usr/sbin/chown":
                # ignore — uid mapping isn't reproducible in CI
                pass
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


@pytest.fixture
def stub_subprocess():
    rec = _SubprocessRecorder()
    set_subprocess_runner(rec)
    yield rec
    set_subprocess_runner(None)


# ─────────────────────────────────────────────────────────────────────────────
# write_user_profile_via_sudo
# ─────────────────────────────────────────────────────────────────────────────


def test_write_invokes_mkdir_cp_chown_chmod_in_order(tmp_path, stub_subprocess):
    profile = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a"),
        sections={"Goals": "- Ship the book"},
    )
    write_user_profile_via_sudo(profile, bot_home=tmp_path, bot_user="team_bot_a_user")

    # Each command: ["sudo", "/bin/something", ...args].
    # The verb is at index 1.
    flat = [(cmd[1] if len(cmd) > 1 else cmd[0], list(cmd[2:])) for cmd in stub_subprocess.calls]

    # First three: ensure the profiles directory exists with bot-owned 700.
    assert flat[0][0] == "/bin/mkdir"
    assert flat[1][0] == "/usr/sbin/chown"
    assert flat[2][0] == "/bin/chmod"

    # Profile file: cp + chown + chmod 600 + chmod -N (strip inherited ACL)
    assert flat[3][0] == "/bin/cp"
    assert flat[4][0] == "/usr/sbin/chown"
    assert flat[5] == ("/bin/chmod", ["600", str(tmp_path / ".openclaw/profiles/alice.md")])
    assert flat[6] == ("/bin/chmod", ["-N", str(tmp_path / ".openclaw/profiles/alice.md")])

    # Status file: cp + chown + chmod 644, NO chmod -N (admin needs to read it)
    assert flat[7][0] == "/bin/cp"
    assert flat[8][0] == "/usr/sbin/chown"
    assert flat[9] == ("/bin/chmod", ["644", str(tmp_path / ".openclaw/profiles/.status.json")])
    # No tenth call — status path doesn't strip ACL
    chmod_n_calls = [c for c in flat if c[0] == "/bin/chmod" and c[1] and c[1][0] == "-N"]
    assert len(chmod_n_calls) == 1, "exactly one chmod -N (on profile.md only)"


def test_write_creates_profile_file_at_correct_path(tmp_path, stub_subprocess):
    profile = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a"),
        sections={"Goals": "- Ship the book"},
    )
    write_user_profile_via_sudo(profile, bot_home=tmp_path, bot_user="team_bot_a_user")

    expected = tmp_path / ".openclaw" / "profiles" / "alice.md"
    assert expected.exists()
    text = expected.read_text()
    assert "Ship the book" in text
    assert "alice" in text


def test_write_creates_status_file_with_any_has_content_true(tmp_path, stub_subprocess):
    profile = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a"),
        sections={"Goals": "- Ship the book"},
    )
    write_user_profile_via_sudo(profile, bot_home=tmp_path, bot_user="team_bot_a_user")

    status_file = tmp_path / ".openclaw" / "profiles" / ".status.json"
    assert status_file.exists()
    status = json.loads(status_file.read_text())
    assert status == {"any_has_content": True}


def test_write_aggregates_status_across_multi_user_bot(tmp_path, stub_subprocess):
    """Regression guard for the multi-user status-corruption blocker:
    on a multi-user bot like Team_bot_c, writing Bob's empty profile must
    NOT flip the status bit to False if Alice's profile is still
    populated. The writer aggregates across all profiles in the dir.
    """
    from user_profile import save_profile

    # Seed Alice with content (bot-side path; uses regular save_profile
    # which calls update_status — same aggregation logic).
    alice = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_c"),
        sections={"Goals": "- something Alice cares about"},
    )
    save_profile(alice, tmp_path)

    # Now write Bob's empty profile via the sudo path.
    bob = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="bob", bot_id="team_bot_c"),
    )
    write_user_profile_via_sudo(bob, bot_home=tmp_path, bot_user="team_bot_c_user")

    # Status must reflect Alice's content, NOT Bob's emptiness.
    status_file = tmp_path / ".openclaw" / "profiles" / ".status.json"
    assert json.loads(status_file.read_text()) == {"any_has_content": True}


def test_write_status_false_when_all_users_have_no_content(tmp_path, stub_subprocess):
    """All users' profiles empty (e.g. all DNT'd) → status correctly false."""
    alice = UserProfile(
        frontmatter=UserProfileFrontmatter(
            user_key="alice", bot_id="team_bot_c", tracking_enabled=False
        ),
    )
    bob = UserProfile(
        frontmatter=UserProfileFrontmatter(
            user_key="bob", bot_id="team_bot_c", tracking_enabled=False
        ),
    )
    write_user_profile_via_sudo(alice, bot_home=tmp_path, bot_user="team_bot_c_user")
    write_user_profile_via_sudo(bob, bot_home=tmp_path, bot_user="team_bot_c_user")

    status_file = tmp_path / ".openclaw" / "profiles" / ".status.json"
    assert json.loads(status_file.read_text()) == {"any_has_content": False}


def test_write_chowns_to_bot_user(tmp_path, stub_subprocess):
    """The chown call must use the bot user's name, not evolve."""
    profile = UserProfile(
        frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a"),
        sections={"Goals": "- x"},
    )
    write_user_profile_via_sudo(profile, bot_home=tmp_path, bot_user="team_bot_a_user")

    chowns = [c for c in stub_subprocess.calls if c[1] == "/usr/sbin/chown"]
    # Every chown should target the bot user, never evolve
    for c in chowns:
        assert "team_bot_a_user:" in c[2]
        assert "evolve" not in c[2]


def test_write_propagates_subprocess_failure(tmp_path):
    """A non-zero return from sudo cp should raise so the caller can log."""
    def _failing(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="permission denied"
        )

    set_subprocess_runner(_failing)
    try:
        profile = UserProfile(
            frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a"),
            sections={"Goals": "- x"},
        )
        with pytest.raises(RuntimeError) as exc:
            write_user_profile_via_sudo(profile, bot_home=tmp_path, bot_user="team_bot_a_user")
        assert "permission denied" in str(exc.value)
    finally:
        set_subprocess_runner(None)


# ─────────────────────────────────────────────────────────────────────────────
# commit (high-level wizard entry point)
# ─────────────────────────────────────────────────────────────────────────────


def test_commit_returns_none_when_extraction_was_empty(tmp_path, stub_subprocess):
    """If the wizard captured nothing meaningful, commit is a no-op — no
    placeholder file gets created."""
    result = commit(
        extracted={"name": "  "},
        user_key="alice",
        bot_id="team_bot_a",
        bot_home=tmp_path,
        bot_user="team_bot_a_user",
    )
    assert result is None
    profiles_dir = tmp_path / ".openclaw" / "profiles"
    # No file written — but parent dir may have been created during the
    # has_content check. The important assertion is no .md file exists.
    md_files = list(profiles_dir.glob("*.md")) if profiles_dir.exists() else []
    assert md_files == []


def test_commit_writes_when_extraction_has_content(tmp_path, stub_subprocess):
    result = commit(
        extracted={"name": "Pod_admin", "top_goals": ["Ship the book"]},
        user_key="alice",
        bot_id="team_bot_a",
        bot_home=tmp_path,
        bot_user="team_bot_a_user",
    )
    assert result is not None
    assert result.name == "alice.md"
    text = result.read_text()
    assert "Goes by: Pod_admin" in text
    assert "Ship the book" in text


def test_commit_preserves_dnt_state_across_wizard_rerun(tmp_path, stub_subprocess):
    """Regression guard: a user with DNT on (tracking_enabled=false) who
    runs the wizard must NOT have DNT silently flipped back to true.
    ``commit()`` loads existing on-disk state when ``existing`` is None
    so frontmatter (including ``tracking_enabled``) survives across
    re-runs.

    Spec: docs/spec-user-profile-2026-05-07.md §D5b — DNT-on users can
    explicitly volunteer information via the wizard, but the off-switch
    stays off until the user explicitly turns it back on."""
    from user_profile import UserProfile, UserProfileFrontmatter, save_profile

    # Seed an existing DNT-on profile bot-side
    save_profile(
        UserProfile(
            frontmatter=UserProfileFrontmatter(
                user_key="alice", bot_id="team_bot_a", tracking_enabled=False
            ),
        ),
        tmp_path,
    )

    # Wizard fires (commit called without an explicit `existing`)
    commit(
        extracted={"name": "Pod_admin", "top_goals": ["Ship the book"]},
        user_key="alice",
        bot_id="team_bot_a",
        bot_home=tmp_path,
        bot_user="team_bot_a_user",
    )

    from user_profile import load_profile
    after = load_profile(tmp_path, "alice")
    assert after is not None
    # DNT preserved
    assert after.tracking_enabled is False
    # And the wizard's content landed (DNT gates passive observation,
    # not explicit user input)
    assert "Pod_admin" in after.sections.get("Demographics", "")
    assert "Ship the book" in after.sections.get("Goals", "")
