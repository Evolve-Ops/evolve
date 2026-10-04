"""Forge retry workspace cleanup — bot_forge helper + BuildRequest/build_prompt.

Background (2026-06-05): a forge install job for the trip-research app
failed on the first attempt (PR #2197's --max-turns block), the operator
hit Retry, and the second attempt died with sha256 mismatches on
``apps/trip-research/research.py`` and ``apps/trip-research/drafts.py``.
The bot's fresh content didn't match leftover files from the prior
attempts because nothing cleared the workspace between runs.

Fix (this PR):
- ``clean_workspace_for_retry(bot_id, job_id)``: removes the bot's
  ``forge/inbox/<job_id>*.json`` and ``forge/outbox/<job_id>*.json``
  files. evolve owns those dirs so the unlink works without sudo.
- ``BuildRequest.is_retry`` + ``build_prompt(is_retry=True)``: prepends a
  cleanup-instruction paragraph telling the bot's LLM to
  ``rm -rf apps/<app_id>/`` before re-building. That tree is bot-owned —
  the admin user can't reach it directly.

Coverage here:
- helper removes job-matching inbox/outbox files (build + suffixed)
- helper leaves files for OTHER jobs alone
- helper is idempotent (missing files, missing dirs, called twice)
- build_prompt mentions retry cleanup only when is_retry=True
- BuildRequest.to_json serialises is_retry round-trip

Companion integration coverage lives in test_forge_retry.py — that
asserts the retry endpoint sets ``job.is_retry`` + calls the helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge  # noqa: E402


# ── clean_workspace_for_retry ────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Re-route ``bot_forge_dir`` to a tmp tree and pre-create inbox/outbox."""
    monkeypatch.setattr(
        bot_forge, "bot_forge_dir", lambda bot_id: tmp_path / bot_id,
    )
    bot_id = "bot_test"
    inbox = tmp_path / bot_id / "inbox"
    outbox = tmp_path / bot_id / "outbox"
    inbox.mkdir(parents=True)
    outbox.mkdir(parents=True)
    return bot_id, inbox, outbox


def test_clean_removes_job_inbox_and_outbox(workspace):
    bot_id, inbox, outbox = workspace
    job_id = "j-abc12345"
    (inbox / f"{job_id}.json").write_text("{}")
    (outbox / f"{job_id}.json").write_text("{}")

    result = bot_forge.clean_workspace_for_retry(bot_id, job_id)

    assert result == {"inbox_removed": 1, "outbox_removed": 1}
    assert not (inbox / f"{job_id}.json").exists()
    assert not (outbox / f"{job_id}.json").exists()


def test_clean_removes_suffixed_files(workspace):
    """The forge protocol writes critique/refine variants with -c1, -r1,
    -c2, -r2 suffixes (see bot_forge.inbox_path). All must be cleared."""
    bot_id, inbox, outbox = workspace
    job_id = "j-abc12345"
    for suffix in ("", "-c1", "-r1", "-c2", "-r2"):
        (inbox / f"{job_id}{suffix}.json").write_text("{}")
        (outbox / f"{job_id}{suffix}.json").write_text("{}")

    result = bot_forge.clean_workspace_for_retry(bot_id, job_id)

    assert result == {"inbox_removed": 5, "outbox_removed": 5}
    assert list(inbox.iterdir()) == []
    assert list(outbox.iterdir()) == []


def test_clean_preserves_other_jobs(workspace):
    """Files belonging to other job_ids must survive untouched."""
    bot_id, inbox, outbox = workspace
    target = "j-abc12345"
    other = "j-99999999"
    (inbox / f"{target}.json").write_text("{}")
    (inbox / f"{other}.json").write_text("{}")
    (outbox / f"{target}.json").write_text("{}")
    (outbox / f"{other}-c1.json").write_text("{}")

    result = bot_forge.clean_workspace_for_retry(bot_id, target)

    assert result == {"inbox_removed": 1, "outbox_removed": 1}
    assert (inbox / f"{other}.json").exists()
    assert (outbox / f"{other}-c1.json").exists()


def test_clean_is_idempotent(workspace):
    """Called twice in a row — second call is a no-op, no error."""
    bot_id, inbox, outbox = workspace
    job_id = "j-abc12345"
    (inbox / f"{job_id}.json").write_text("{}")

    first = bot_forge.clean_workspace_for_retry(bot_id, job_id)
    second = bot_forge.clean_workspace_for_retry(bot_id, job_id)

    assert first == {"inbox_removed": 1, "outbox_removed": 0}
    assert second == {"inbox_removed": 0, "outbox_removed": 0}


def test_clean_tolerates_missing_dirs(tmp_path, monkeypatch):
    """If the bot was never dispatched to (forge dirs don't exist yet),
    the helper returns zeros — it must NOT create dirs or error out."""
    monkeypatch.setattr(
        bot_forge, "bot_forge_dir", lambda bot_id: tmp_path / bot_id,
    )

    result = bot_forge.clean_workspace_for_retry("bot_never_used", "j-abc12345")

    assert result == {"inbox_removed": 0, "outbox_removed": 0}
    assert not (tmp_path / "bot_never_used").exists()


def test_clean_tolerates_empty_job_id(workspace):
    bot_id, _, _ = workspace
    # Empty job_id would otherwise glob "*.json" and nuke everyone's files.
    result = bot_forge.clean_workspace_for_retry(bot_id, "")
    assert result == {"inbox_removed": 0, "outbox_removed": 0}


# ── build_prompt with is_retry ───────────────────────────────────────────────


def test_build_prompt_no_retry_paragraph_when_not_retry():
    prompt = bot_forge.build_prompt("j-abc12345", "p-12345678", "build")
    assert "RETRY OF A FAILED BUILD" not in prompt
    assert "rm -rf apps/" not in prompt


def test_build_prompt_includes_retry_paragraph_when_retry():
    prompt = bot_forge.build_prompt(
        "j-abc12345", "p-12345678", "build", is_retry=True,
    )
    assert "RETRY OF A FAILED BUILD" in prompt
    assert "rm -rf apps/" in prompt
    # The bot needs to know WHY — the sha256-mismatch explanation makes
    # the instruction self-documenting if the prompt ever changes hands.
    assert "sha256" in prompt


def test_build_prompt_retry_paragraph_appears_before_partial_files_pack():
    """Order matters — cleanup must happen before the bot decides what to
    build. The PARTIAL FILES-PACK paragraph is the next conditional and
    serves as a stable anchor."""
    prompt = bot_forge.build_prompt(
        "j-abc12345", "p-12345678", "build", is_retry=True,
    )
    retry_idx = prompt.index("RETRY OF A FAILED BUILD")
    partial_idx = prompt.index("PARTIAL FILES-PACK")
    assert retry_idx < partial_idx


# ── BuildRequest.is_retry serialisation ──────────────────────────────────────


def test_build_request_is_retry_defaults_false():
    req = bot_forge.BuildRequest(
        job_id="j-abc12345",
        kind="build",
        pkg_id="p-12345678",
        pkg_version="v1.0.0",
        app_id="trip-research",
        app_name="Trip Research",
        build_spec="build this",
    )
    assert req.is_retry is False
    payload = json.loads(req.to_json())
    assert payload["is_retry"] is False


def test_build_request_serialises_is_retry_true():
    """The bot's LLM reads the inbox JSON to discover is_retry — the
    field must round-trip through to_json so the prompt-driven cleanup
    matches the request payload."""
    req = bot_forge.BuildRequest(
        job_id="j-abc12345",
        kind="build",
        pkg_id="p-12345678",
        pkg_version="v1.0.0",
        app_id="trip-research",
        app_name="Trip Research",
        build_spec="build this",
        is_retry=True,
    )
    payload = json.loads(req.to_json())
    assert payload["is_retry"] is True
