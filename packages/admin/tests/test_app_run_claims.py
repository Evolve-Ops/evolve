"""AL-1.2 Lane B — the app-run claim writer (unwired by design) + the
forge-never-claims negative.

``write_app_run_claim`` ships as a tested helper with NO live producer: the
2026-08-15 census found zero wrappers that shell out to ``openclaw agent``,
so wiring it anywhere would be dead code pretending to be coverage. The first
real wrapper (AL-2.x) calls it before passing ``--session-id`` per the AL-0.4
contract; the plugin joins ``{shared}/{bot}/app-runs/<uuid>.json`` at
``before_agent_run``.

The negative half pins the design non-goal: the forge dispatcher
(``bot_forge.py``) also mints session UUIDs and passes ``--session-id``, but
forge turns are Evolve subagent turns and must stay UNATTRIBUTED — if
bot_forge ever grows a claim write, every forge build would masquerade as
scheduled app spend.
"""
from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.install_helpers import (  # noqa: E402
    APP_RUNS_DIR_NAME,
    write_app_run_claim,
)


def test_claim_write_shape_mode_and_location(tmp_path):
    sid = str(uuid.uuid4())
    out = write_app_run_claim(tmp_path, "team_bot_a", sid, "morning-briefing", label="9am digest")
    assert out["ok"] is True
    p = Path(out["path"])
    assert p == tmp_path / "team_bot_a" / APP_RUNS_DIR_NAME / f"{sid}.json"
    claim = json.loads(p.read_text())
    assert claim["app_id"] == "morning-briefing"
    assert claim["label"] == "9am digest"
    assert claim["ts"]  # ISO timestamp present; exact value is wall-clock
    # 0644 pin — the consumer may be a different-user process on future
    # topologies, and a partial-file race is excluded by temp+rename.
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    # Re-claiming the same session id overwrites cleanly (idempotent wrapper retry).
    assert write_app_run_claim(tmp_path, "team_bot_a", sid, "morning-briefing")["ok"] is True


def test_claim_rejects_unsafe_session_ids_and_missing_app_id(tmp_path):
    for bad in ("", "../../etc/passwd", "a/b", ".leading-dot", "x" * 300, None, 42):
        out = write_app_run_claim(tmp_path, "team_bot_a", bad, "some-app")  # type: ignore[arg-type]
        assert out["ok"] is False, f"session_id {bad!r} must be refused"
    assert write_app_run_claim(tmp_path, "team_bot_a", str(uuid.uuid4()), "")["ok"] is False
    assert write_app_run_claim(tmp_path, "team_bot_a", str(uuid.uuid4()), "   ")["ok"] is False
    # Nothing was created by the refused writes.
    assert not (tmp_path / "team_bot_a").exists()


def test_claim_write_failure_returns_error_never_raises(tmp_path):
    (tmp_path / "team_bot_a").write_text("not a dir")
    out = write_app_run_claim(tmp_path, "team_bot_a", str(uuid.uuid4()), "some-app")
    assert out["ok"] is False
    assert out["error"]


def test_forge_dispatcher_never_writes_claims():
    """bot_forge passes --session-id but must never claim the session for an
    app (design §2 non-goals: forge turns are Evolve subagent turns). Source
    scan — the durable guard against a future 'helpful' wiring."""
    src = (
        _ADMIN_DIR / "evolve_admin" / "applications" / "bot_forge.py"
    ).read_text()
    assert "--session-id" in src, "forge still pins sessions (AL-0.4 mechanism)"
    for token in ("write_app_run_claim", "app-runs", "app_runs", APP_RUNS_DIR_NAME):
        assert token not in src, (
            f"bot_forge.py references {token!r} — forge dispatches must stay "
            "unattributed (no claim files); see docs/build-AL-1.2-scheduled-"
            "attribution.md Lane B"
        )
