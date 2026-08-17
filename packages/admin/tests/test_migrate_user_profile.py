"""Tests for the v1 → v2 user-profile migration.

Spec: docs/spec-user-profile-2026-05-07.md (Migration & removals).

Stubs the subprocess runner used by the writer (so no real sudo) and
monkeypatches ``bot_home`` / ``get_bot_user`` to point at tmp_path.
The migration logic itself runs unmodified; the only difference from
production is that file ops land in tmp_path instead of /Users/<bot>.
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

from evolve_admin.migrate_user_profile import migrate_pod  # noqa: E402
from evolve_admin.evo.wizard.user_profile_writer import (  # noqa: E402
    set_subprocess_runner,
)


# ─────────────────────────────────────────────────────────────────────────────
# Stub subprocess runner — same shape as test_evo_user_profile_writer.py
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
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


@pytest.fixture
def stub_subprocess(monkeypatch):
    rec = _SubprocessRecorder()
    set_subprocess_runner(rec)
    yield rec
    set_subprocess_runner(None)


# ─────────────────────────────────────────────────────────────────────────────
# Pod fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """Set up shared_dir + per-bot homes + network.json + monkeypatch the
    bot_home / get_bot_user / load_network helpers used by the migration."""
    shared = tmp_path / "shared"
    (shared / "profiles").mkdir(parents=True)
    (shared / "profiles" / "users").mkdir()

    bot_homes = {
        "team_bot_a": tmp_path / "team_bot_a_home",
        "team_bot_c": tmp_path / "team_bot_c_home",
    }
    for h in bot_homes.values():
        h.mkdir()

    bot_users = {
        "team_bot_a": "team_bot_a_user",
        "team_bot_c": "team_bot_c_user",
    }

    network = {
        "members": list(bot_homes.keys()),
        "sharedDir": str(shared),
        "bots": {
            bot: {"user": user} for bot, user in bot_users.items()
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    import evolve_admin.config as _config
    import evolve_admin.migrate_user_profile as _mig

    monkeypatch.setattr(_config, "bot_home", lambda bot, network=None: bot_homes[bot])
    monkeypatch.setattr(_config, "get_bot_user", lambda bot, network=None: bot_users[bot])
    # The migration module imported these names directly, so patch them
    # there too.
    monkeypatch.setattr(_mig, "bot_home", lambda bot, network=None: bot_homes[bot])
    monkeypatch.setattr(_mig, "get_bot_user", lambda bot, network=None: bot_users[bot])

    return {
        "shared": shared,
        "network_path": network_path,
        "bot_homes": bot_homes,
        "bot_users": bot_users,
    }


def _seed_user_json(shared: Path, user_key: str, payload: dict) -> Path:
    safe = user_key.replace(":", "__").replace("/", "_")
    path = shared / "profiles" / "users" / f"{safe}.json"
    path.write_text(json.dumps(payload))
    return path


def _seed_v1_bot_profile(shared: Path, bot_id: str, sections: dict[str, str] | None = None) -> Path:
    """Write a minimal v1 bot profile at {shared_dir}/profiles/<bot_id>.md."""
    body = [
        "---",
        f"bot_id: {bot_id}",
        "schema_version: 1",
        "created_at: 2026-01-01T00:00:00+00:00",
        "updated_at: 2026-01-01T00:00:00+00:00",
        "---",
        f"# User Profile — {bot_id}",
        "",
    ]
    if sections:
        for name, content in sections.items():
            body.append(f"## {name}")
            body.append(content)
            body.append("")
    path = shared / "profiles" / f"{bot_id}.md"
    path.write_text("\n".join(body))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: per-user JSON migration
# ─────────────────────────────────────────────────────────────────────────────


def test_migrate_user_json_writes_v2_profile_bot_side(pod, stub_subprocess):
    _seed_user_json(
        pod["shared"],
        "ext:telegram:555",
        {
            "user_key": "ext:telegram:555",
            "last_wizard_bot": "team_bot_a",
            "name": "Pod_admin",
            "role": "Tech lead",
            "top_goals": ["Ship the book"],
            "profile_version": 1,
        },
    )

    report = migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    # Profile materialized in the bot's home
    target = pod["bot_homes"]["team_bot_a"] / ".openclaw" / "profiles" / "ext__telegram__555.md"
    assert target.exists()
    text = target.read_text()
    assert "Goes by: Pod_admin" in text
    assert "Tech lead" in text
    assert "Ship the book" in text

    assert "ext__telegram__555" in report.json_profiles_migrated


def test_migrate_archives_user_json_after_writing(pod, stub_subprocess):
    _seed_user_json(
        pod["shared"],
        "alice",
        {
            "user_key": "alice",
            "last_wizard_bot": "team_bot_a",
            "name": "Alice",
            "top_goals": ["x"],
        },
    )

    migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    src = pod["shared"] / "profiles" / "users" / "alice.json"
    archived = pod["shared"] / "profiles" / "_archive" / "t" / "users" / "alice.json"
    assert not src.exists()
    assert archived.exists()


def test_migrate_skips_json_with_no_last_wizard_bot(pod, stub_subprocess):
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "name": "Alice"},  # no last_wizard_bot
    )

    report = migrate_pod(network_path=pod["network_path"], archive_subdir="t")
    assert "alice" in report.json_profiles_skipped
    assert "no last_wizard_bot" in report.json_profiles_skipped["alice"].lower()
    # Source file is left in place — operator decides what to do.
    assert (pod["shared"] / "profiles" / "users" / "alice.json").exists()


def test_migrate_skips_json_for_unknown_bot(pod, stub_subprocess):
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "last_wizard_bot": "no_such_bot", "name": "x"},
    )

    report = migrate_pod(network_path=pod["network_path"], archive_subdir="t")
    assert "alice" in report.json_profiles_skipped
    assert "not in network.json" in report.json_profiles_skipped["alice"]


def test_migrate_archives_empty_jsons_too(pod, stub_subprocess):
    """If a JSON has only metadata fields (no real content), still archive
    the source so we don't keep retrying it on subsequent migration runs."""
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "last_wizard_bot": "team_bot_a", "profile_version": 1},
    )

    report = migrate_pod(network_path=pod["network_path"], archive_subdir="t")
    assert "alice" in report.json_profiles_skipped
    assert "no content" in report.json_profiles_skipped["alice"]
    # But still archived
    assert not (pod["shared"] / "profiles" / "users" / "alice.json").exists()
    assert (pod["shared"] / "profiles" / "_archive" / "t" / "users" / "alice.json").exists()


def test_migrate_routes_correct_user_to_correct_bot(pod, stub_subprocess):
    """In a multi-bot pod, each user JSON's last_wizard_bot determines
    which bot's home receives the profile."""
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "last_wizard_bot": "team_bot_a", "name": "Alice"},
    )
    _seed_user_json(
        pod["shared"],
        "bob",
        {"user_key": "bob", "last_wizard_bot": "team_bot_c", "name": "Bob"},
    )

    migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    team_bot_a_alice = pod["bot_homes"]["team_bot_a"] / ".openclaw" / "profiles" / "alice.md"
    team_bot_c_bob = pod["bot_homes"]["team_bot_c"] / ".openclaw" / "profiles" / "bob.md"
    assert team_bot_a_alice.exists()
    assert team_bot_c_bob.exists()
    # No cross-contamination
    assert not (pod["bot_homes"]["team_bot_a"] / ".openclaw" / "profiles" / "bob.md").exists()
    assert not (pod["bot_homes"]["team_bot_c"] / ".openclaw" / "profiles" / "alice.md").exists()


# ─────────────────────────────────────────────────────────────────────────────
# v1 bot profiles are left in place — they still hold bot-admin config
# (archetype, surfacing_cadence, timezone) read by /api/arbiter/bot-setup.
# ─────────────────────────────────────────────────────────────────────────────


def test_migrate_leaves_v1_bot_profile_files_in_place(pod, stub_subprocess):
    """Regression guard: an earlier draft archived these files, which
    silently nuked operator settings. The v1 file's frontmatter is
    load-bearing — leave it alone."""
    team_bot_a_v1 = _seed_v1_bot_profile(pod["shared"], "team_bot_a")
    team_bot_c_v1 = _seed_v1_bot_profile(pod["shared"], "team_bot_c")

    migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    # Sources still in place
    assert team_bot_a_v1.exists()
    assert team_bot_c_v1.exists()
    # No archive of bot profile files (only user JSONs may be archived)
    archive_root = pod["shared"] / "profiles" / "_archive" / "t"
    bot_archives = list(archive_root.glob("*.md")) if archive_root.exists() else []
    assert bot_archives == [], f"bot profile files unexpectedly archived: {bot_archives}"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: status file seeding
# ─────────────────────────────────────────────────────────────────────────────


def test_migrate_seeds_status_for_bots_with_no_user_profiles(pod, stub_subprocess):
    """A bot with no user JSON and no v1 profile — migration still seeds
    .status.json so the admin endpoint has a definite answer."""
    report = migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    for bot in ("team_bot_a", "team_bot_c"):
        status_path = (
            pod["bot_homes"][bot] / ".openclaw" / "profiles" / ".status.json"
        )
        assert status_path.exists(), f"{bot} missing .status.json"
        status = json.loads(status_path.read_text())
        assert status == {"any_has_content": False}

    assert not report.write_failures


def test_migrate_status_reflects_migrated_content(pod, stub_subprocess):
    """A bot that received a populated profile during stage 1 should end
    with .status.json saying any_has_content=true."""
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "last_wizard_bot": "team_bot_a", "name": "Alice", "top_goals": ["x"]},
    )

    migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    team_bot_a_status = json.loads(
        (pod["bot_homes"]["team_bot_a"] / ".openclaw" / "profiles" / ".status.json").read_text()
    )
    team_bot_c_status = json.loads(
        (pod["bot_homes"]["team_bot_c"] / ".openclaw" / "profiles" / ".status.json").read_text()
    )
    assert team_bot_a_status["any_has_content"] is True
    assert team_bot_c_status["any_has_content"] is False


def test_migrate_does_not_overwrite_existing_status(pod, stub_subprocess):
    """If a bot already has .status.json (e.g. from an earlier wizard run),
    leave it alone — do NOT clobber with the migration's empty default."""
    profiles = pod["bot_homes"]["team_bot_a"] / ".openclaw" / "profiles"
    profiles.mkdir(parents=True)
    existing = {"any_has_content": True}
    (profiles / ".status.json").write_text(json.dumps(existing))

    migrate_pod(network_path=pod["network_path"], archive_subdir="t")

    after = json.loads((profiles / ".status.json").read_text())
    assert after == existing


# ─────────────────────────────────────────────────────────────────────────────
# Idempotence
# ─────────────────────────────────────────────────────────────────────────────


def test_migrate_is_idempotent_on_clean_run(pod, stub_subprocess):
    """Run the migration on an empty pod (no user JSONs, no v1 bot profiles).
    Should produce a clean report and no write failures."""
    r1 = migrate_pod(network_path=pod["network_path"], archive_subdir="t1")
    r2 = migrate_pod(network_path=pod["network_path"], archive_subdir="t2")
    assert not r1.write_failures
    assert not r2.write_failures
    assert r1.json_profiles_migrated == []
    assert r2.json_profiles_migrated == []


def test_migrate_re_run_does_not_re_migrate(pod, stub_subprocess):
    _seed_user_json(
        pod["shared"],
        "alice",
        {"user_key": "alice", "last_wizard_bot": "team_bot_a", "name": "Alice", "top_goals": ["x"]},
    )

    r1 = migrate_pod(network_path=pod["network_path"], archive_subdir="t")
    assert "alice" in r1.json_profiles_migrated

    r2 = migrate_pod(network_path=pod["network_path"], archive_subdir="t")
    # Source JSON is gone, so nothing to migrate the second time.
    assert r2.json_profiles_migrated == []
    assert not r2.write_failures
