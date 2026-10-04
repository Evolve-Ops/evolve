"""Integration: bot_forge._dispatch_agent + ForgeRunawayWatcher.

Exercises the dispatcher path that wires Guard B's runaway watcher to a
real Popen lifecycle. Subprocess and OS-side primitives are stubbed —
we control:

  * ``subprocess.Popen`` — returns a ``_FakeProc`` that writes synthetic
    OC trajectory events to disk on a background thread, so the watcher
    observes a real-shaped JSONL file growing in real time.
  * ``os.killpg`` — recorded but no-op (we don't actually want to kill
    anything in the test environment).
  * ``forge_cost_guard.emit_signal`` — captures the payload dict so we
    can assert the Signal shape that lands in the Alerts page.

Two scenarios:

  * **Cap exceeded** — fake writer races past the per-bot
    ``message_cap`` quickly. Watcher triggers the kill callback; fake
    proc transitions to dead state; dispatcher's poll loop sees
    rc != None without outbox, reads ``watcher.verdict()``, emits the
    cap-hit Signal, and raises with the "killed by runaway watcher"
    message.

  * **Under cap** — fake writer stays well under the cap and writes the
    outbox shortly after starting. Dispatcher's poll loop exits on
    outbox arrival, no Signal is emitted, ``dispatch_build`` returns a
    clean BuildResult.

The cap-hit path is the canonical regression guard for the
2026-06-03 runaway shape. The under-cap path locks the
"no false-positive Signal" contract — under the old code, ANY
rc-without-outbox failure emitted
``forge_session_message_cap_exceeded``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge  # noqa: E402


# Real-shape OC trajectory event (sampled from atlas's session log).
# Each call appends one of these. The substring ``"model.completed"`` is
# what the watcher counts.
_MODEL_COMPLETED_LINE = (
    '{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
    '"type":"model.completed","ts":"2026-06-05T20:46:55.000Z",'
    '"sessionId":"fake-test-session","data":{"durationMs":1234}}\n'
)


class _FakeProc:
    """Stand-in for the openclaw subprocess. Background writer thread
    appends ``model.completed`` events to the trajectory file at a
    configurable rate; the proc reports alive until either the writer
    completes or ``kill()`` is invoked.

    Constructor params are passed through ``Popen`` factory below — the
    dispatcher's call to ``Popen(cmd, ...)`` receives these via class-
    level closure variables set by the test.

    Class-level config (set by the test before invoking dispatch_*):
      * ``CFG`` — dict with keys
            trajectory_path : Path to write to.
            n_completed     : int. How many model.completed lines to
                              write before stopping the writer.
            line_interval   : float. Seconds between writes.
            write_outbox    : Path | None. If not None, write a valid
                              outbox JSON at ``write_outbox`` after
                              writing all ``n_completed`` lines (the
                              under-cap happy path).
    """

    CFG: dict = {}

    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd
        self.pid = 12345
        self.stdout = None
        self.stderr = None
        self._alive = True
        self._return_code = None
        self._writer_done = threading.Event()

        cfg = _FakeProc.CFG
        self._traj = Path(cfg["trajectory_path"])
        self._n = int(cfg["n_completed"])
        self._interval = float(cfg["line_interval"])
        self._outbox = cfg.get("write_outbox")

        # Touch the trajectory file so the watcher's tick can open it
        # on its very first poll. Mirrors openclaw writing
        # session.started before any model.completed event.
        self._traj.parent.mkdir(parents=True, exist_ok=True)
        self._traj.write_text("")

        self._writer = threading.Thread(
            target=self._writer_run, daemon=True,
        )
        self._writer.start()

    def _writer_run(self):
        for _ in range(self._n):
            if not self._alive:
                return
            with open(self._traj, "a", encoding="utf-8") as fh:
                fh.write(_MODEL_COMPLETED_LINE)
                fh.flush()
            # Honour cancellation between writes — when the watcher
            # kills us, we should stop writing immediately rather than
            # racing past the cap by more than one extra line.
            if self._interval > 0:
                time.sleep(self._interval)
        if self._alive and self._outbox is not None:
            Path(self._outbox).parent.mkdir(parents=True, exist_ok=True)
            Path(self._outbox).write_text(
                '{"status":"complete","files_written":[],'
                '"test_run":null,"test_exit_code":null,'
                '"test_output":"","notes":""}'
            )
        # Mark ourselves dead so the dispatcher's outbox-or-died poll
        # loop sees the transition (the under-cap path uses this).
        self._return_code = 0
        self._alive = False
        self._writer_done.set()

    # ── subprocess.Popen surface ──────────────────────────────────────────
    @property
    def returncode(self):
        return self._return_code

    def poll(self):
        return self._return_code

    def kill(self):
        # Mimic _kill_forge_pg's effect on the proc — flip rc != None
        # so the dispatcher's poll loop sees the death and reads the
        # watcher verdict.
        self._alive = False
        self._return_code = -9
        self._writer_done.set()

    def terminate(self):
        self.kill()

    def wait(self, timeout=None):
        self._writer_done.wait(timeout=timeout)
        return self._return_code


def _setup_dispatch_stubs(monkeypatch, tmp_path, message_cap: int):
    """Plumbs every external dependency of ``_dispatch_agent`` into
    tmp_path. Returns ``(emitted_signals, killpg_calls)`` lists that
    callers assert against.
    """
    monkeypatch.setattr(
        bot_forge, "_load_network_for_guard",
        lambda: {
            "bots": {
                "atlas": {
                    "forge": {
                        # Generous per-turn cap so Guard A doesn't refuse
                        # before we even start Popen — Guard B is what
                        # we're exercising here.
                        "per_turn_cap_usd": 1000.0,
                        "per_dispatch_cap_usd": 100000.0,
                        "message_cap": message_cap,
                    },
                },
            },
        },
    )
    # Point all path lookups at tmp_path.
    monkeypatch.setattr(
        bot_forge, "bot_forge_dir", lambda bot_id: tmp_path / bot_id,
    )
    monkeypatch.setattr(
        bot_forge, "_bot_home", lambda bot_id: tmp_path / bot_id,
    )

    def _ensure(bot_id):
        (tmp_path / bot_id / "inbox").mkdir(parents=True, exist_ok=True)
        (tmp_path / bot_id / "outbox").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bot_forge, "ensure_forge_dirs", _ensure)
    monkeypatch.setattr(
        bot_forge, "_write_forge_session_annotation", lambda **kw: None,
    )
    monkeypatch.setattr(
        bot_forge, "_operator_confirmed_subkind_for_job", lambda jid: None,
    )

    # Capture the signal-emit calls.
    emitted: list[dict] = []
    monkeypatch.setattr(
        bot_forge.forge_cost_guard, "emit_signal",
        lambda shared_dir, payload: emitted.append(payload) or True,
    )

    # No-op killpg — the test doesn't actually want to kill anything.
    # Record the calls so we can assert the kill path was reached.
    killpg_calls: list[tuple[int, int]] = []

    def _fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(bot_forge.os, "killpg", _fake_killpg)
    monkeypatch.setattr(bot_forge.os, "getpgid", lambda pid: pid)

    return emitted, killpg_calls


def _route_popen(monkeypatch, *, trajectory_path, n_completed,
                  line_interval, write_outbox):
    """Configure ``_FakeProc`` class state, then route
    ``subprocess.Popen`` to it for the duration of the test."""
    _FakeProc.CFG = {
        "trajectory_path": trajectory_path,
        "n_completed": n_completed,
        "line_interval": line_interval,
        "write_outbox": write_outbox,
    }
    monkeypatch.setattr(bot_forge.subprocess, "Popen", _FakeProc)


# ── Cap-exceeded path ────────────────────────────────────────────────────────


def test_dispatch_emits_cap_signal_and_raises_when_watcher_fires(
    monkeypatch, tmp_path,
):
    """End-to-end-ish: tight cap + fake OC that writes many model.completed
    lines forces the watcher to fire. Dispatcher must:
      * NOT write any outbox
      * Catch the dead-without-outbox case
      * Emit a forge_session_message_cap_exceeded Signal with
        ``observed_messages`` recorded
      * Raise RuntimeError naming the cap source

    This is the 2026-06-03 runaway-shape regression guard. Under the
    old `--max-turns` wiring (PR #2036) this scenario hung because OC
    rejected the flag rc=1; under the post-#2197 stub it would fire
    the cap Signal on EVERY rc-without-outbox failure (false-positive).
    The watcher-driven path fires it iff the cap was actually hit.
    """
    emitted, killpg_calls = _setup_dispatch_stubs(
        monkeypatch, tmp_path, message_cap=3,
    )

    # Predict where the trajectory will land. The dispatcher generates
    # a UUID for the session id; we can't predict it, but we know the
    # *directory*. The fake proc will create a single trajectory file
    # in that dir on Popen; the watcher will discover and poll it.
    sessions_dir = (
        tmp_path / "atlas" / ".openclaw" / "agents" / "main" / "sessions"
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # We need to seed CFG with the path AT Popen-time, but since the
    # session id is random, the FakeProc has to derive the path from
    # the argv it receives. Patch _FakeProc.__init__ inline so it
    # extracts --session-id from cmd and writes there.

    original_init = _FakeProc.__init__

    def _init_with_argv(self, cmd, *args, **kwargs):
        # Find --session-id <uuid> in argv and route the writer to it.
        try:
            sid = cmd[cmd.index("--session-id") + 1]
        except (ValueError, IndexError):
            sid = "fallback"
        traj = sessions_dir / f"{sid}.trajectory.jsonl"
        _FakeProc.CFG["trajectory_path"] = traj
        original_init(self, cmd, *args, **kwargs)

    monkeypatch.setattr(_FakeProc, "__init__", _init_with_argv)

    _route_popen(
        monkeypatch,
        trajectory_path=sessions_dir / "placeholder.trajectory.jsonl",
        n_completed=50,   # well past cap=3
        line_interval=0.01,
        write_outbox=None,  # never write an outbox — we want the kill path
    )

    with pytest.raises(RuntimeError) as excinfo:
        bot_forge.dispatch_build(
            "atlas",
            bot_forge.BuildRequest(
                job_id="j-runaway",
                kind="build",
                pkg_id="p",
                pkg_version="1",
                app_id="a",
                app_name="A",
                build_spec="",
            ),
            model="anthropic/claude-sonnet-4-6",
        )

    # Dispatcher's failure message must name the watcher, the observed
    # turn count, and the cap source — what the operator sees in the
    # forge job log.
    assert "killed by runaway watcher" in str(excinfo.value)
    assert "cap=3" in str(excinfo.value)

    # Exactly one cap-hit Signal emitted (de-duped via signature).
    cap_signals = [
        p for p in emitted
        if p.get("type") == "forge_session_message_cap_exceeded"
    ]
    assert len(cap_signals) == 1
    payload = cap_signals[0]
    assert payload["bot_id"] == "atlas"
    assert payload["details"]["message_cap"] == 3
    assert payload["details"]["observed_messages"] >= 3
    # Cap source reflects per-bot override path, not "default".
    assert payload["details"]["message_cap_source"] == "bots.atlas.forge.message_cap"


# ── Under-cap path ───────────────────────────────────────────────────────────


def test_dispatch_completes_cleanly_when_under_cap(monkeypatch, tmp_path):
    """Trajectory stays under the cap and the outbox arrives. The
    watcher's verdict ends up ``stopped``; NO Signal is emitted; the
    BuildResult is returned cleanly.

    This pins the "no false-positive Signal" contract — every regular
    forge install must not light up the Alerts page.
    """
    emitted, _ = _setup_dispatch_stubs(
        monkeypatch, tmp_path, message_cap=100,
    )

    sessions_dir = (
        tmp_path / "atlas" / ".openclaw" / "agents" / "main" / "sessions"
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)

    original_init = _FakeProc.__init__

    def _init_with_argv(self, cmd, *args, **kwargs):
        try:
            sid = cmd[cmd.index("--session-id") + 1]
        except (ValueError, IndexError):
            sid = "fallback"
        traj = sessions_dir / f"{sid}.trajectory.jsonl"
        _FakeProc.CFG["trajectory_path"] = traj
        original_init(self, cmd, *args, **kwargs)

    monkeypatch.setattr(_FakeProc, "__init__", _init_with_argv)

    outbox = tmp_path / "atlas" / "outbox" / "j-clean.json"
    _route_popen(
        monkeypatch,
        trajectory_path=sessions_dir / "placeholder.trajectory.jsonl",
        n_completed=2,
        line_interval=0.0,
        write_outbox=outbox,
    )

    result = bot_forge.dispatch_build(
        "atlas",
        bot_forge.BuildRequest(
            job_id="j-clean",
            kind="build",
            pkg_id="p",
            pkg_version="1",
            app_id="a",
            app_name="A",
            build_spec="",
        ),
        model="anthropic/claude-sonnet-4-6",
    )

    assert result.status == "complete"
    # No cap-hit Signal must be emitted on the happy path. The old
    # rc-without-outbox trigger would have emitted under any failure
    # mode; the watcher-verdict trigger only fires on real cap hits.
    cap_signals = [
        p for p in emitted
        if p.get("type") == "forge_session_message_cap_exceeded"
    ]
    assert cap_signals == []
