"""tests/test_session_surface_capabilities.py — the [INSTALLED CAPABILITIES]
push block (skills + configured-integration tools).

Spec: docs/spec-bot-capability-awareness-2026-06-22.md §3 + §5. The block is
delivered PER TURN via the plugin's before_prompt_build hook (which shells out
to ``session_surface.py --capabilities-only``), NOT at session_start — because
session_start fires once per OC session and never re-fires for existing
long-running Telegram chats, which is exactly why CA-P1 (#3080) shipped
non-functional. These tests pin:

  * load_capabilities_block surfaces skills + integration tools, NOT apps
    (apps ship durably via AGENTS.md), and soft-fails without crashing
  * the ``--capabilities-only`` CLI mode emits ONLY the block (the contract
    the plugin's per-turn hook consumes)
  * the default (session_start) mode does NOT emit the capability block
  * build_session_prefix still slots the block when passed directly (the
    param is kept for backwards-compat / ordering tests)
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _run_main(argv: list[str]) -> str:
    """Invoke session_surface.main() with argv and capture stdout. main()
    always sys.exit(0) on success."""
    import session_surface as _ss
    captured = io.StringIO()
    with patch.object(sys, "argv", ["session_surface.py", *argv]):
        with redirect_stdout(captured):
            try:
                _ss.main()
            except SystemExit as e:
                assert e.code == 0, f"main() exited non-zero: {e.code}"
    return captured.getvalue()


# ── build_session_prefix wiring ───────────────────────────────────────────────


class TestPrefixWiring:
    def test_capabilities_block_included_when_present(self):
        from session_surface import build_session_prefix
        out = build_session_prefix(capabilities_block="[INSTALLED CAPABILITIES] x")
        assert "[INSTALLED CAPABILITIES] x" in out

    def test_absent_when_empty(self):
        from session_surface import build_session_prefix
        out = build_session_prefix(capabilities_block="")
        assert "INSTALLED CAPABILITIES" not in out

    def test_lands_after_apps_before_role_scaffold(self):
        from session_surface import build_session_prefix
        out = build_session_prefix(
            app_posture_block="[APP POSTURE] a",
            capabilities_block="[INSTALLED CAPABILITIES] c",
            member_block="[EVOLVE PLUGIN] m",
        )
        assert out.index("[APP POSTURE]") < out.index("[INSTALLED CAPABILITIES]")
        assert out.index("[INSTALLED CAPABILITIES]") < out.index("[EVOLVE PLUGIN]")

    def test_backwards_compatible_positional_call(self):
        # The first three params are still positional (pinned elsewhere too).
        from session_surface import build_session_prefix
        out = build_session_prefix("[BOT GUIDE] g", "[NOTIF] n", "[APP POSTURE] a")
        assert "[BOT GUIDE] g" in out and "[NOTIF] n" in out


# ── load_capabilities_block ───────────────────────────────────────────────────


class TestLoadCapabilitiesBlock:
    def test_no_bot_id_returns_empty(self, shared_dir):
        from session_surface import load_capabilities_block
        assert load_capabilities_block(None, shared_dir) == ""
        assert load_capabilities_block("", shared_dir) == ""

    def test_no_sources_returns_empty(self, shared_dir, monkeypatch, tmp_path):
        # Home with an empty (or absent) skills dir, no network.json, no apps.
        from session_surface import load_capabilities_block
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert load_capabilities_block("member_bot", shared_dir) == ""

    def test_real_assembly_surfaces_skills_and_google(
        self, shared_dir, monkeypatch, tmp_path
    ):
        from session_surface import load_capabilities_block
        # 1) a skill in the bot's ~/.openclaw/skills
        skills = tmp_path / ".openclaw" / "skills" / "weather"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Local forecasts.\n---\nbody",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # 2) a Google-configured bot in network.json (free_gmail_oauth = supported)
        import json
        (shared_dir / "network.json").write_text(
            json.dumps({"bots": {"member_bot": {"google_integration": {"mode": "free_gmail_oauth"}}}}),
            encoding="utf-8",
        )
        block = load_capabilities_block("member_bot", shared_dir)
        assert "[INSTALLED CAPABILITIES" in block
        assert "weather" in block          # skill
        assert "gmail_send" in block       # google tool from TOOL_SPECS
        assert "do NOT use a shell CLI" in block

    def test_soft_fails_when_assembly_raises(self, shared_dir, monkeypatch, tmp_path):
        from session_surface import load_capabilities_block
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        import capability_block
        monkeypatch.setattr(
            capability_block, "build_capabilities_block",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Must not raise — soft-fails to "".
        assert load_capabilities_block("member_bot", shared_dir) == ""

    def test_malformed_network_json_degrades_to_skills_only(
        self, shared_dir, monkeypatch, tmp_path
    ):
        from session_surface import load_capabilities_block
        skills = tmp_path / ".openclaw" / "skills" / "weather"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Forecasts.\n---\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (shared_dir / "network.json").write_text("{ not json", encoding="utf-8")
        block = load_capabilities_block("member_bot", shared_dir)
        # Skills still surface; Google silently absent.
        assert "weather" in block
        assert "gmail_send" not in block

    def test_block_excludes_apps(self, shared_dir, monkeypatch, tmp_path):
        """The per-turn block is skills + integration tools ONLY — apps ship
        durably via AGENTS.md, so load_capabilities_block must never reach
        into the apps inventory. (Regression guard for the 2026-06-22
        session_start→per-turn migration that dropped apps from this block.)"""
        from session_surface import load_capabilities_block
        skills = tmp_path / ".openclaw" / "skills" / "weather"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Forecasts.\n---\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        block = load_capabilities_block("member_bot", shared_dir)
        assert "weather" in block          # skill present
        assert "Apps (" not in block       # no apps section header
        assert "INSTALLED_APPS.md" not in block  # no apps cross-ref either


# ── --capabilities-only CLI mode (the per-turn contract) ──────────────────────


class TestCapabilitiesOnlyMode:
    def _seed_skill_and_google(self, shared_dir, monkeypatch, tmp_path):
        skills = tmp_path / ".openclaw" / "skills" / "weather"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Local forecasts.\n---\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (shared_dir / "network.json").write_text(
            json.dumps({"bots": {"member_bot": {"google_integration": {"mode": "free_gmail_oauth"}}}}),
            encoding="utf-8",
        )

    def test_emits_only_the_capability_block(self, shared_dir, monkeypatch, tmp_path):
        """--capabilities-only → ONLY the block (no conduct, no role
        scaffold, no firing signals). This is what the plugin's per-turn
        before_prompt_build hook threads into appendSystemContext."""
        self._seed_skill_and_google(shared_dir, monkeypatch, tmp_path)
        out = _run_main([
            "--capabilities-only", "--bot", "member_bot",
            "--role", "member", "--shared-dir", str(shared_dir),
        ])
        assert "[INSTALLED CAPABILITIES" in out
        assert "weather" in out
        assert "gmail_send" in out
        # ONLY the block — none of the session_start scaffolding.
        assert "POD_CONDUCT" not in out and "[POD CONDUCT" not in out
        assert "[FIRING SIGNALS" not in out
        assert "[EVOLVE PLUGIN" not in out  # member role scaffold

    def test_every_bot_not_role_gated(self, shared_dir, monkeypatch, tmp_path):
        """Member role still gets the block — the confabulation this fixes
        was a member/consumer bot. (Contrast the home narrative, which is
        primary-only.)"""
        self._seed_skill_and_google(shared_dir, monkeypatch, tmp_path)
        out = _run_main([
            "--capabilities-only", "--bot", "member_bot",
            "--role", "member", "--shared-dir", str(shared_dir),
        ])
        assert "gmail_send" in out

    def test_no_capabilities_emits_nothing(self, shared_dir, monkeypatch, tmp_path):
        """No skills, no integrations → empty output, exit 0, no exception.
        before_prompt_build is on the hot path; an empty bot must not block."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
        out = _run_main([
            "--capabilities-only", "--bot", "member_bot",
            "--role", "member", "--shared-dir", str(shared_dir),
        ])
        assert out.strip() == ""

    def test_json_mode_emits_only_capabilities_key(self, shared_dir, monkeypatch, tmp_path):
        """--capabilities-only --json → object with just the capabilities key."""
        self._seed_skill_and_google(shared_dir, monkeypatch, tmp_path)
        out = _run_main([
            "--capabilities-only", "--json", "--bot", "member_bot",
            "--role", "member", "--shared-dir", str(shared_dir),
        ])
        data = json.loads(out)
        assert set(data.keys()) == {"capabilities"}
        assert "gmail_send" in data["capabilities"]


# ── session_start (default) mode — capabilities removed ───────────────────────


class TestSessionStartOmitsCapabilities:
    def test_default_mode_omits_capability_block(self, shared_dir, monkeypatch, tmp_path):
        """Default (no --capabilities-only / --per-turn) main() does NOT emit
        the capability block. As of 2026-06-22 it ships exclusively via the
        per-turn path so it reaches long-running sessions — and so it isn't
        double-injected on fresh sessions (where session_start would persist
        a copy AND per-turn would add one each turn)."""
        skills = tmp_path / ".openclaw" / "skills" / "weather"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Forecasts.\n---\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (shared_dir / "network.json").write_text(
            json.dumps({"bots": {"member_bot": {"google_integration": {"mode": "free_gmail_oauth"}}}}),
            encoding="utf-8",
        )
        out = _run_main([
            "--bot", "member_bot", "--role", "member",
            "--shared-dir", str(shared_dir),
        ])
        assert "[INSTALLED CAPABILITIES" not in out
        assert "gmail_send" not in out
        # Sanity: session_start IS still doing its job (conduct present).
        assert len(out) > 0

    def test_default_json_omits_capabilities_key(self, shared_dir, monkeypatch, tmp_path):
        """--json (session_start) → the 'capabilities' key is GONE from the
        dict (not just empty), mirroring the home_narrative removal."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
        out = _run_main([
            "--json", "--bot", "member_bot", "--role", "member",
            "--shared-dir", str(shared_dir),
        ])
        data = json.loads(out)
        assert "capabilities" not in data
        # Sibling keys that stay at session_start are still present.
        assert "member" in data and "firing_signals" in data
