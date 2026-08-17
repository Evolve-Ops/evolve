"""Tests for ``evo profile`` — view + DNT toggle.

Spec: docs/spec-user-profile-2026-05-07.md §D5, §D5b.

Hits the dispatch entry point (``evo.dispatch.dispatch``) so the routing
itself is exercised end-to-end. Subprocess (sudo) is stubbed via the
writer's test seam; ``bot_home`` and ``get_bot_user`` are monkeypatched
to point at tmp_path so reads/writes go to a real local directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, str(path))

from evolve_admin.evo.dispatch import dispatch  # noqa: E402
from evolve_admin.evo.wizard.user_profile_writer import (  # noqa: E402
    set_subprocess_runner,
)
from user_profile import (  # noqa: E402
    UserProfile,
    UserProfileFrontmatter,
)


# ─────────────────────────────────────────────────────────────────────────────
# Stub subprocess runner (same shape as the writer tests)
# ─────────────────────────────────────────────────────────────────────────────


class _SubprocessRecorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, capture_output=True, text=True, **kwargs):
        self.calls.append(list(cmd))
        if len(cmd) >= 2 and cmd[0] == "sudo":
            verb = cmd[1]
            if verb == "/bin/mkdir" and "-p" in cmd:
                Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            elif verb == "/bin/cp":
                Path(cmd[3]).write_bytes(Path(cmd[2]).read_bytes())
            elif verb == "/bin/cat":
                # Used by read_user_profile_from_bot's sudo fallback —
                # only fires when the direct read fails. In tmp_path the
                # direct read works, so this branch shouldn't trigger.
                p = Path(cmd[2])
                if p.exists():
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=p.read_text(), stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="not found"
                )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


@pytest.fixture
def stub_subprocess(monkeypatch):
    rec = _SubprocessRecorder()
    set_subprocess_runner(rec)
    yield rec
    set_subprocess_runner(None)


@pytest.fixture
def network(tmp_path, monkeypatch):
    """Set up a network dict + monkeypatch bot_home/get_bot_user so the
    handler writes into tmp_path / "<bot_user>" instead of /Users/<bot>."""
    bot_home_root = tmp_path / "bothome"
    bot_home_root.mkdir()

    # Network with a primary user already claimed for telegram so the
    # role resolution puts the caller into "primary".
    net = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a_user",
                "primary_user": {
                    "external_ids": {"telegram": "555"},
                },
            },
        },
    }

    import evolve_admin.config as _config
    monkeypatch.setattr(_config, "bot_home", lambda bot_id, network=None: bot_home_root)
    monkeypatch.setattr(_config, "get_bot_user", lambda bot_id, network=None: "team_bot_a_user")
    return net, bot_home_root


def _dispatch_profile(net, args="") -> "DispatchResult":  # noqa: F821
    """Dispatch ``evo profile [args]`` from the primary user on telegram."""
    raw = "evo profile" if not args else f"evo profile {args}"
    return dispatch(
        net,
        bot_id="team_bot_a",
        channel="telegram",
        sender_external_id="555",
        raw_text=raw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# View — no profile yet
# ─────────────────────────────────────────────────────────────────────────────


def test_view_with_no_profile_yet_says_so(network, stub_subprocess):
    net, _ = network
    result = _dispatch_profile(net)
    assert result.subcommand == "profile"
    assert result.mode == "speak"
    assert "No profile yet" in result.system_append
    # Plugin direct-sends the bare body via Telegram; no preamble.
    assert result.direct_send_message is not None
    assert "No profile yet" in result.direct_send_message
    assert "IMPORTANT" not in result.direct_send_message


def test_view_with_empty_profile_says_so(network, stub_subprocess):
    """A profile file with frontmatter but no populated sections."""
    net, home = network
    profiles_dir = home / ".openclaw" / "profiles"
    profiles_dir.mkdir(parents=True)
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm), home)

    result = _dispatch_profile(net)
    assert "no facts yet" in result.system_append.lower()


def test_view_with_populated_profile_renders_sections(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(
        UserProfile(
            frontmatter=fm,
            sections={
                "Goals": "- Ship the book",
                "Vocation": "- Engineer at a startup",
            },
        ),
        home,
    )

    result = _dispatch_profile(net)
    body = result.system_append
    assert "What I've recorded about you" in body
    assert "Ship the book" in body
    assert "Engineer at a startup" in body
    # Section headings rendered in the view
    assert "**Goals**" in body
    assert "**Vocation**" in body


def test_view_when_dnt_on_explains_state(network, stub_subprocess):
    """A profile with tracking_enabled=false (DNT on) should never show
    section content even if any happens to be there. The handler short-
    circuits with a DNT-explanation message."""
    net, home = network
    fm = UserProfileFrontmatter(
        user_key="ext:telegram:555", bot_id="team_bot_a", tracking_enabled=False
    )
    from user_profile import save_profile

    save_profile(
        UserProfile(
            frontmatter=fm,
            sections={"Goals": "- something the user typed under DNT"},
        ),
        home,
    )

    result = _dispatch_profile(net)
    body = result.system_append
    assert "DNT" in body
    # Critically: the section content is NOT echoed back when DNT is on.
    assert "something the user typed" not in body


# ─────────────────────────────────────────────────────────────────────────────
# DNT on
# ─────────────────────────────────────────────────────────────────────────────


def test_dnt_on_creates_file_when_none_exists(network, stub_subprocess):
    """First-time DNT-on creates an empty tracking_enabled=false profile.
    Subsequent inferrer runs will short-circuit on the flag."""
    net, home = network
    result = _dispatch_profile(net, "dnt on")
    assert "DNT is now ON" in result.system_append

    profile_path = home / ".openclaw" / "profiles" / "ext__telegram__555.md"
    assert profile_path.exists()
    text = profile_path.read_text()
    assert "tracking_enabled: false" in text


def test_dnt_on_wipes_existing_content(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(
        UserProfile(
            frontmatter=fm,
            sections={
                "Goals": "- Ship the book",
                "Vocation": "- Engineer",
                "Audit Log": "- 2026-05-01: added Vocation",
            },
        ),
        home,
    )

    _dispatch_profile(net, "dnt on")

    from user_profile import load_profile
    after = load_profile(home, "ext:telegram:555")
    assert after is not None
    assert after.tracking_enabled is False
    # Section content is gone (anything that could reconstruct facts).
    assert (after.sections.get("Goals") or "").strip() == ""
    assert (after.sections.get("Vocation") or "").strip() == ""
    # Audit Log is wiped of any previously-recorded fact, then gets a
    # single new entry recording the DNT toggle itself. Prior entries
    # are gone (no leak); the new entry mentions only the toggle, not
    # any fact that was wiped.
    audit = (after.sections.get("Audit Log") or "").strip()
    assert "added Vocation" not in audit
    assert "tracking_enabled set to false" in audit
    assert audit.count("\n") == 0  # single line


def test_dnt_on_updates_status_to_no_content(network, stub_subprocess):
    """After DNT on (with wipe), .status.json should report no content."""
    net, home = network
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(
        UserProfile(frontmatter=fm, sections={"Goals": "- Ship the book"}),
        home,
    )
    # Sanity check: status currently true
    status_path = home / ".openclaw" / "profiles" / ".status.json"
    assert json.loads(status_path.read_text())["any_has_content"] is True

    _dispatch_profile(net, "dnt on")

    assert json.loads(status_path.read_text())["any_has_content"] is False


def test_dnt_on_when_already_on_is_idempotent_message(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(
        user_key="ext:telegram:555", bot_id="team_bot_a", tracking_enabled=False
    )
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm), home)

    result = _dispatch_profile(net, "dnt on")
    assert "already on" in result.system_append.lower()


# ─────────────────────────────────────────────────────────────────────────────
# DNT off
# ─────────────────────────────────────────────────────────────────────────────


def test_dnt_off_flips_flag_to_true(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(
        user_key="ext:telegram:555", bot_id="team_bot_a", tracking_enabled=False
    )
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm), home)

    result = _dispatch_profile(net, "dnt off")
    assert "DNT is now OFF" in result.system_append

    from user_profile import load_profile
    after = load_profile(home, "ext:telegram:555")
    assert after.tracking_enabled is True


def test_dnt_off_when_already_off_is_idempotent_message(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm), home)

    result = _dispatch_profile(net, "dnt off")
    assert "already off" in result.system_append.lower()


def test_dnt_off_does_not_create_phantom_profile_when_none_exists(network, stub_subprocess):
    """If a user runs `evo profile dnt off` without ever having a profile,
    the handler should NOT write an empty phantom file. Absence-of-file
    already means tracking_enabled=true (the inferrer's default). Writing
    a placeholder costs a sudo round-trip and clutters the bot's home."""
    net, home = network
    profile_path = home / ".openclaw" / "profiles" / "ext__telegram__555.md"
    assert not profile_path.exists()

    result = _dispatch_profile(net, "dnt off")
    assert "already off" in result.system_append.lower()
    # Critical: no file was written.
    assert not profile_path.exists()


def test_dnt_on_writes_audit_log_entry(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(user_key="ext:telegram:555", bot_id="team_bot_a")
    from user_profile import save_profile

    save_profile(
        UserProfile(frontmatter=fm, sections={"Goals": "- old goal"}), home
    )

    _dispatch_profile(net, "dnt on")

    from user_profile import load_profile, AUDIT_LOG_SECTION
    after = load_profile(home, "ext:telegram:555")
    audit = (after.sections.get(AUDIT_LOG_SECTION) or "").strip()
    assert "tracking_enabled set to false" in audit
    assert "(dnt)" in audit


def test_dnt_off_writes_audit_log_entry(network, stub_subprocess):
    net, home = network
    fm = UserProfileFrontmatter(
        user_key="ext:telegram:555", bot_id="team_bot_a", tracking_enabled=False
    )
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm), home)

    _dispatch_profile(net, "dnt off")

    from user_profile import load_profile, AUDIT_LOG_SECTION
    after = load_profile(home, "ext:telegram:555")
    audit = (after.sections.get(AUDIT_LOG_SECTION) or "").strip()
    assert "tracking_enabled set to true" in audit


def test_dnt_off_no_backfill_from_past_observations(network, stub_subprocess):
    """D5b: DNT off resumes from current state — no reconstruction of
    facts from past observations. Sections that were wiped stay wiped."""
    net, home = network
    # Simulate a past DNT-on state: tracking_enabled=false, sections empty
    fm = UserProfileFrontmatter(
        user_key="ext:telegram:555", bot_id="team_bot_a", tracking_enabled=False
    )
    from user_profile import save_profile

    save_profile(UserProfile(frontmatter=fm, sections={}), home)

    _dispatch_profile(net, "dnt off")

    from user_profile import load_profile
    after = load_profile(home, "ext:telegram:555")
    assert after.tracking_enabled is True
    # Sections are still empty — no resurrection of past facts.
    assert (after.sections.get("Goals") or "").strip() == ""


# ─────────────────────────────────────────────────────────────────────────────
# Routing & arg parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_unknown_subform_explains(network, stub_subprocess):
    net, _ = network
    result = _dispatch_profile(net, "explode")
    body = result.system_append
    assert "Unknown evo profile form" in body
    # Lists supported forms in the response
    assert "evo profile dnt on" in body


def test_dnt_arg_normalizes_case(network, stub_subprocess):
    net, _ = network
    # "DNT ON" (uppercase) should work the same as "dnt on"
    result = _dispatch_profile(net, "DNT ON")
    assert "DNT is now ON" in result.system_append


def test_view_does_not_leak_profile_when_caller_lacks_identity():
    """If channel or sender_external_id is missing (e.g. internal API
    calls), the handler refuses rather than reading some other user's
    profile."""
    net = {"members": ["team_bot_a"], "bots": {"team_bot_a": {"user": "team_bot_a_user"}}}
    result = dispatch(
        net,
        bot_id="team_bot_a",
        channel=None,
        sender_external_id=None,
        raw_text="evo profile",
    )
    assert "I can't tell who you are" in result.system_append
