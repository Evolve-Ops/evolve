"""tests/test_session_surface_per_turn.py — session_surface.py --per-turn mode.

Covers the second half of the Home-narrative recall fix specced in
``docs/diagnosis-evo-briefing-context-gap-2026-05-26.md``: move the
narrative block from a one-shot session_start injection to a per-turn
injection so cache regeneration mid-conversation reaches the next
turn.

The TypeScript-side per-turn injection happens in TurnObserver's
``before_prompt_build`` hook (see
``packages/plugin/src/observer/TurnObserver.ts:_renderPerTurnNarrativeBlock``);
this file covers the Python-side equivalent CLI invocation that
shells out to ``session_surface.py --per-turn``. The CLI mode exists
as a stable contract — anything that wants the same render the plugin
emits can use it (operator debugging, future per-bot diagnostics,
tests).

What's covered:
  * ``main()`` with ``--per-turn`` emits ONLY the narrative block
    (no conduct, no firing signals, no role scaffold).
  * The block mutates across invocations as the cache mutates —
    simulates the multi-turn "cache regenerated mid-conversation"
    case.
  * Member role / no role → empty (primary-gated).
  * Missing cache → empty, no exception.
  * Default (non-per-turn) ``main()`` no longer emits the narrative
    block in the session-start prefix — narrative is per-turn-only
    now.

Spec: docs/diagnosis-evo-briefing-context-gap-2026-05-26.md (Option D —
per-turn injection + read tool, the second half of PR #1623's B1).
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _write_narrative_cache(
    shared_dir: Path,
    *,
    text: str,
    generated_at: str | None = None,
) -> Path:
    """Write a home-narrative-cache.json in the shape that
    home_chat.write_narrative_cache produces."""
    if generated_at is None:
        generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    payload = {
        "digest_hash": "abc123",
        "generated_at": generated_at,
        "text": text,
        "cost_usd": 0.0001,
        "model": "claude-haiku-4-5",
        "input_tokens": 200,
        "output_tokens": 80,
    }
    p = shared_dir / "home-narrative-cache.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _run_main(argv: list[str]) -> str:
    """Invoke session_surface.main() with the given argv and return
    captured stdout. Reloads the module each call to reset sys.argv
    parsing state."""
    import session_surface as _ss
    # Patch sys.argv. Use SystemExit(0) tolerance because main() always
    # calls sys.exit(0) on success.
    captured = io.StringIO()
    with patch.object(sys, "argv", ["session_surface.py", *argv]):
        with redirect_stdout(captured):
            try:
                _ss.main()
            except SystemExit as e:
                assert e.code == 0, f"main() exited non-zero: {e.code}"
    return captured.getvalue()


# ─── --per-turn mode ─────────────────────────────────────────────────────────


class TestPerTurnMode:
    def test_per_turn_emits_narrative_block_when_primary_and_cache_present(
        self, shared_dir
    ):
        """Primary role + fresh cache → output is the narrative block
        (and nothing else — no conduct summary, no firing signals, no
        role scaffold). This is the per-turn payload the plugin's
        before_prompt_build hook threads into appendSystemContext."""
        _write_narrative_cache(
            shared_dir,
            text="team-bot-a had an unusually expensive session this morning.",
        )

        out = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])

        assert "[CURRENT POD REPORT" in out
        assert "team-bot-a had an unusually expensive session" in out
        # Per-turn mode emits ONLY the narrative block — no pod conduct,
        # no firing signals header, no [PRIMARY BOT — Evolve interface]
        # primary scaffold.
        assert "[POD CONDUCT" not in out and "POD_CONDUCT" not in out
        assert "[FIRING SIGNALS" not in out
        assert "[PRIMARY BOT" not in out

    def test_per_turn_picks_up_cache_mutation_across_invocations(
        self, shared_dir
    ):
        """Turn 1: cache says X. Turn 2: cache rewritten to Y. Turn 3:
        cache rewritten to Z. Each invocation must surface the cache as
        it stands NOW, not at session_start. This is the exact bug
        Option D fixes — failure mode 3 from the diagnosis."""
        _write_narrative_cache(
            shared_dir,
            text="Turn 1 cache content — three bots online.",
        )
        out_turn_1 = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])
        assert "Turn 1 cache content" in out_turn_1

        # Operator refreshes Home; new narrative lands.
        _write_narrative_cache(
            shared_dir,
            text="Turn 2 cache content — team-bot-a recovered.",
        )
        out_turn_2 = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])
        assert "Turn 2 cache content" in out_turn_2
        assert "Turn 1 cache content" not in out_turn_2

        # Another regen mid-conversation.
        _write_narrative_cache(
            shared_dir,
            text="Turn 3 cache content — npm pin signals all resolved.",
        )
        out_turn_3 = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])
        assert "Turn 3 cache content" in out_turn_3
        assert "Turn 2 cache content" not in out_turn_3

    def test_per_turn_member_role_emits_nothing(self, shared_dir):
        """Member role → empty output. The narrative is admin-UI
        content aimed at the pod operator; member bots get nothing
        here (same primary gate as session_surface.load_primary_block /
        load_home_narrative_block)."""
        _write_narrative_cache(shared_dir, text="report content")

        out = _run_main([
            "--per-turn", "--role", "member",
            "--shared-dir", str(shared_dir),
        ])

        assert out.strip() == ""

    def test_per_turn_no_role_emits_nothing(self, shared_dir):
        """No --role flag → empty output. Legacy / pre-PR-1339 pods
        without a role field on openclaw.json fall through this gate
        without injecting the operator-facing block to a possibly-
        member bot."""
        _write_narrative_cache(shared_dir, text="report content")

        out = _run_main([
            "--per-turn",
            "--shared-dir", str(shared_dir),
        ])

        assert out.strip() == ""

    def test_per_turn_missing_cache_emits_nothing_no_exception(
        self, shared_dir
    ):
        """No cache file (fresh install, daemon not started, operator
        hasn't visited Home) → empty output, exit 0, no exception.
        before_prompt_build is on the LLM hot path — a missing cache
        must never block or slow down a turn."""
        # tmp_path is intentionally empty.

        out = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])

        assert out.strip() == ""

    def test_per_turn_malformed_cache_emits_nothing_no_exception(
        self, shared_dir
    ):
        """Corrupt cache file (mid-write truncation, manual edit gone
        wrong) → soft-fail to empty output. Mirrors the
        load_home_narrative_block contract."""
        (shared_dir / "home-narrative-cache.json").write_text(
            "{not valid json", encoding="utf-8"
        )

        out = _run_main([
            "--per-turn", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])

        assert out.strip() == ""

    def test_per_turn_json_mode_emits_only_narrative_key(self, shared_dir):
        """--per-turn --json → JSON object with just home_narrative key,
        not the full session_start JSON shape. Lets callers that want
        structured output (future tooling) get just the per-turn slice
        without the rest of the prefix."""
        _write_narrative_cache(shared_dir, text="report from cache")

        out = _run_main([
            "--per-turn", "--json", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])
        data = json.loads(out)

        assert set(data.keys()) == {"home_narrative"}
        assert "report from cache" in data["home_narrative"]


# ─── Default (session_start) mode — narrative removed ────────────────────────


class TestSessionStartMode:
    def test_session_start_default_omits_home_narrative_block(self, shared_dir):
        """Default (no --per-turn) main() does NOT emit the home
        narrative block in the session_start prefix. As of 2026-06-01,
        the narrative ships exclusively via --per-turn — the
        session_start path no longer races against per-turn refreshes."""
        _write_narrative_cache(
            shared_dir, text="narrative present in cache",
        )

        out = _run_main([
            "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])

        # The narrative cache IS present but the session_start mode
        # ignores it. Per-turn mode is the only path that emits it.
        assert "[CURRENT POD REPORT" not in out
        assert "narrative present in cache" not in out
        # Sanity check — session_start IS doing the rest of its job
        # (conduct summary is always present).
        assert "POD CONDUCT" in out or "evolve-pod-conduct" in out or len(out) > 0

    def test_session_start_json_omits_home_narrative_key(self, shared_dir):
        """--json (session_start) → JSON object excludes home_narrative.
        The key is gone from the dict, not just empty — anything that
        depended on its presence at session_start should see a KeyError
        and migrate to either the per-turn mode or the new
        pod_state.home_narrative tool."""
        _write_narrative_cache(shared_dir, text="narrative content")

        out = _run_main([
            "--json", "--role", "primary",
            "--shared-dir", str(shared_dir),
        ])
        data = json.loads(out)

        assert "home_narrative" not in data
        # Sanity check — the firing_signals key IS still present
        # (firing signals stay at session_start; only narrative moved).
        assert "firing_signals" in data
