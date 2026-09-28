"""tests/test_gallery_preflight_telegram_and_optional.py

Two bugs Pod_admin hit installing atlas-daily-digest on 2026-05-29:

1. Telegram preflight returned "missing" even though atlas HAS Telegram
   configured at channels.telegram.botToken (and also has the filesystem
   skill file). The check only walked the manifest's dotted `check_path`
   ("openclaw.json → skills.telegram or .openclaw/skills/telegram.json"),
   which can't parse the "or" alternative; it walked `skills.telegram`,
   found null, and returned missing.

2. GitHub (declared required=False in the manifest) showed up under the
   per-bot install-modal "issues" list with the same yellow tone as
   genuine missing-required items, and the install dialog said
   ready_to_run=false. Optional integrations not being configured isn't a
   gap — that's what "optional" means.

These tests guard the structural fixes:

  • _check_integration recognizes "telegram" as a first-class integration
    with multiple legitimate locations (filesystem skill file OR
    channels.telegram.<token-field>); either satisfies.

  • preflight_check.has_runtime_gap only flips for required+missing items,
    so an app with only optional+missing items still has ready_to_run=true.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))


# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    return tmp_path / "shared"


@pytest.fixture
def patched_oc(monkeypatch):
    """Stand in for /Users/<bot>/.openclaw/openclaw.json.

    The preflight checker reads openclaw.json via Path.read_text() with a
    sudo /bin/cat fallback. We replace _read_oc_json by patching what
    Path.read_text returns when the path matches /Users/atlas/.openclaw/openclaw.json.
    """
    holder: dict[str, dict] = {}

    real_read_text = Path.read_text
    real_exists = Path.exists

    def fake_read_text(self, *args, **kwargs):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            bot = s.split("/")[2]
            if bot in holder:
                return json.dumps(holder[bot])
            raise FileNotFoundError(s)
        return real_read_text(self, *args, **kwargs)

    def fake_exists(self):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            bot = s.split("/")[2]
            return bot in holder
        if s.endswith("/.openclaw/skills/telegram.json"):
            bot = s.split("/")[2]
            return holder.get(f"__skill_file__{bot}", False)
        return real_exists(self)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(Path, "exists", fake_exists)
    return holder


# ─── _check_integration: telegram-aware ──────────────────────────────────────


def _make_telegram_req(setup_doc="docs/skills/telegram-setup.md"):
    return {
        "id": "telegram",
        "display_name": "Telegram",
        "required": True,
        # The "or" alternation in the legacy check_path — should be IGNORED
        # because telegram is now a first-class handler.
        "check_path": "openclaw.json → skills.telegram or .openclaw/skills/telegram.json",
        "setup_doc": setup_doc,
    }


def test_telegram_satisfied_via_channels_botToken(patched_oc, shared_dir):
    """Atlas's real shape on 2026-05-29: channels.telegram.botToken set,
    skills.telegram is null, no filesystem skill file. Must be satisfied."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {
        "channels": {
            "telegram": {
                "enabled": True,
                "botToken": "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "dmPolicy": "pairing",
            }
        },
        "skills": {"telegram": None},
    }

    # Build preflight_check's _check_integration closure by calling
    # preflight_check end-to-end with a single-integration package.
    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {"integrations": [_make_telegram_req()]},
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    reqs = result["requirements"]
    assert len(reqs) == 1
    assert reqs[0]["state"] == "satisfied", reqs[0]["message"]
    assert "channels.telegram.botToken" in reqs[0]["message"]


def test_telegram_satisfied_via_filesystem_skill_file(patched_oc, shared_dir):
    """Filesystem skill file is the canonical/winning location."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {}
    patched_oc["__skill_file__atlas"] = True

    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {"integrations": [_make_telegram_req()]},
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    reqs = result["requirements"]
    assert reqs[0]["state"] == "satisfied"
    assert "skills/telegram.json" in reqs[0]["message"]


def test_telegram_missing_when_neither_location_set(patched_oc, shared_dir):
    """No skill file, no channels.telegram token → missing with helpful message."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {"channels": {}, "skills": {}}

    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {"integrations": [_make_telegram_req()]},
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    reqs = result["requirements"]
    assert reqs[0]["state"] == "missing"
    # The message must name BOTH legitimate locations so the operator
    # knows there's more than one way to satisfy it.
    msg = reqs[0]["message"]
    assert "skills/telegram.json" in msg
    assert "channels.telegram" in msg


def test_telegram_missing_when_channels_explicitly_disabled(patched_oc, shared_dir):
    """Explicit opt-out (channels.telegram.enabled = false) beats presence,
    just like the generic dotted-path check does for plugins."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {
        "channels": {
            "telegram": {
                "enabled": False,  # explicit opt-out
                "botToken": "stale-token",
            }
        }
    }

    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {"integrations": [_make_telegram_req()]},
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    assert result["requirements"][0]["state"] == "missing"
    assert "disabled" in result["requirements"][0]["message"]


# ─── optional+missing doesn't flip ready_to_run ───────────────────────────────


def test_optional_missing_integration_does_not_block_ready_to_run(patched_oc, shared_dir):
    """Bug 2: atlas-daily-digest declares github with required=False but the
    install dialog said ready_to_run=false because optional+missing was
    bumping has_runtime_gap. ready_to_run must stay true."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {
        "channels": {
            "telegram": {"enabled": True, "botToken": "tok"}
        },
        "plugins": {"entries": {"brave": {"enabled": True}}},
    }

    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {
            "integrations": [
                _make_telegram_req(),
                {
                    "id": "brave",
                    "display_name": "Brave",
                    "required": True,
                    "check_path": "openclaw.json → plugins.entries.brave",
                },
                {
                    "id": "github",
                    "display_name": "GitHub",
                    "required": False,  # ← optional
                    "check_path": "openclaw.json → plugins.entries.github",
                },
            ]
        },
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    assert result["ready_to_build"] is True
    # The actual bug: optional+missing was making ready_to_run=false.
    assert result["ready_to_run"] is True, (
        "ready_to_run must stay True when only an OPTIONAL integration is missing; "
        f"requirements={result['requirements']!r}"
    )

    # The optional item is still surfaced in the requirements list (the UI
    # decides how to render it), with severity=info — NOT runtime_warning.
    github_row = next(r for r in result["requirements"] if r["id"] == "github")
    assert github_row["state"] == "missing"
    assert github_row["severity"] == "info"
    assert github_row["required"] is False


def test_required_missing_integration_does_flip_ready_to_run(patched_oc, shared_dir):
    """Regression guard: don't accidentally suppress required+missing too."""
    from evolve_admin.applications import gallery

    patched_oc["atlas"] = {}

    pkg = {
        "pkg_id": "atlas-test",
        "requirements": {"integrations": [_make_telegram_req()]},
    }
    with patch.object(gallery, "load_gallery_package", return_value=pkg):
        result = gallery.preflight_check("atlas-test", "atlas", shared_dir)

    assert result["ready_to_run"] is False
    tg = result["requirements"][0]
    assert tg["required"] is True
    assert tg["severity"] == "runtime_warning"
