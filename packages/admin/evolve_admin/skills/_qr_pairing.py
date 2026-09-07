"""evolve_admin.skills._qr_pairing — interactive QR-pairing session manager.

Used by the WhatsApp install wizard (and later Signal). Each pairing session
spawns ``openclaw channels login --channel <name> --account <bot_id>`` as the
target bot user, tails the subprocess's stdout for QR payload events, and
exposes a short-poll API the admin server's routes use to drive the UI:

  start_session(bot_id, channel, ...) -> {session_id, qr_png_data_url, expires_in_s}
  poll_session(session_id)            -> {state, qr_png_data_url?, error?}
  cancel_session(session_id)          -> bool

Session lifetime is 5 minutes hard; new ``start_session`` for the same
(bot_id, channel) cancels any existing session for that pair (one pairing
at a time per bot).

The QR-stream protocol
----------------------
OC's ``openclaw channels login`` writes Baileys QR strings to stdout as
they cycle (Baileys emits a new QR every ~20 seconds until the operator
scans). We recognise lines that look like Baileys QR payloads (start with
``2@`` or contain a ``ref=`` query param) and capture the most recent one
for rendering.

We render the QR PNG ourselves via the ``qrcode`` library because OC's
CLI output is meant for a terminal, not an HTML <img> element — we can
take the same raw string and produce a PNG data URL inline.

Background workers
------------------
Each active session has a worker thread that owns the subprocess and
pushes state updates into a thread-safe ``Session`` record. The admin
server's routes never block on the subprocess — they read the latest
state snapshot and return immediately.

Session state machine
---------------------

    starting              # subprocess spawned, waiting for first QR
        ↓
    waiting               # QR available; operator should scan
        ↓ (operator scans on phone)
    paired                # subprocess exited 0; authDir populated
        ↓
    (session ends)

Failure terminals:

    failed                # subprocess exited non-zero or QR never appeared
    expired               # hit the 5-minute hard ceiling without pairing
    cancelled             # operator clicked Cancel or replaced this session

Per CLAUDE.md, the admin server runs as ``evolve`` and cannot ``sudo -u
<bot>`` without an explicit grant. The pairing subprocess is invoked via
the sudoers rules added by ``setup_wizard.py::_write_evolve_sudoers``
(``evolve ALL = (<bot_user>) NOPASSWD: /opt/homebrew/bin/openclaw
channels login *``). On pods running pre-rollout sudoers, the spawn
fails with ``sudoers_grant_missing`` and the UI surfaces a doc link.

Privacy
-------
Stdout content is treated as opaque — we extract only the QR payload
needed to render the code, and do NOT log the QR string itself (it
contains a pairing-time secret that, if leaked + scanned by an
attacker before the operator, could link their account to a different
device). Logs include only state transitions and timing.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────────────

#: Hard ceiling on a pairing session's lifetime. If the operator hasn't
#: scanned by then we shut down the subprocess and mark expired.
_SESSION_TTL_S = 5 * 60

#: How long a finished session stays pollable after reaching a terminal
#: state, so the UI's in-flight short-poll still observes the outcome.
_TERMINAL_RESIDENCY_S = 30

#: Pattern that identifies a Baileys QR payload line from OC stdout. Baileys
#: emits QR strings as long pseudo-random tokens with commas separating
#: cryptographic fields; the format has been stable across Baileys 5.x/6.x.
#: We accept anything that looks like a single-line token of >= 100 chars
#: containing only base64-url + comma + alphanumeric + `=` characters.
_QR_PAYLOAD_RE = re.compile(r"^([A-Za-z0-9_/+=,@-]{100,})$")

#: When the subprocess exits with this on stdout, OC has completed pairing.
_PAIRED_MARKER_RE = re.compile(
    r"(?i)\b(paired|connected|linked\s+successfully|ready)\b"
)

#: Bytes per QR PNG output (square). Phones scan reliably at 240+ px even
#: from 30 cm away; 280 gives margin without bloating data URLs.
_QR_PNG_PX = 280


# ── Session record ────────────────────────────────────────────────────────────


@dataclass
class Session:
    """One pairing attempt. Mutated by its worker thread; admin routes
    read snapshots via :meth:`to_dict`."""

    session_id: str
    bot_id: str
    channel: str
    started_at: float
    state: str = "starting"  # see module docstring state machine
    qr_payload: str | None = None  # the latest QR string from OC
    qr_png_data_url: str | None = None  # rendered PNG (data: URL)
    error: str | None = None
    # Subprocess handle — None until the spawn succeeds. The worker
    # thread owns lifecycle; the public API never touches this directly.
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > _SESSION_TTL_S

    @property
    def is_terminal(self) -> bool:
        return self.state in ("paired", "failed", "expired", "cancelled")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "bot_id": self.bot_id,
                "channel": self.channel,
                "state": self.state,
                "qr_png_data_url": self.qr_png_data_url,
                "error": self.error,
                "elapsed_s": int(time.monotonic() - self.started_at),
                "expires_in_s": max(
                    0, _SESSION_TTL_S - int(time.monotonic() - self.started_at)
                ),
            }


# ── Session registry ──────────────────────────────────────────────────────────
#
# Single global, in-process registry. Pairing is foreground operator work —
# no persistence needed. The registry is small (bounded by active operator
# attention) and thread-safe.

_REGISTRY_LOCK = threading.Lock()
_SESSIONS: dict[str, Session] = {}
#: Maps (bot_id, channel) → session_id so a new start_session for the same
#: bot+channel pair can evict the previous one.
_ACTIVE_BY_BOT_CHANNEL: dict[tuple[str, str], str] = {}


def _register(session: Session) -> None:
    with _REGISTRY_LOCK:
        # Evict any previous session for the same bot+channel
        key = (session.bot_id, session.channel)
        prev_id = _ACTIVE_BY_BOT_CHANNEL.get(key)
        if prev_id and prev_id in _SESSIONS:
            prev = _SESSIONS[prev_id]
            _terminate_session(prev, "cancelled", reason="superseded")
        _SESSIONS[session.session_id] = session
        _ACTIVE_BY_BOT_CHANNEL[key] = session.session_id


def _lookup(session_id: str) -> Session | None:
    with _REGISTRY_LOCK:
        return _SESSIONS.get(session_id)


def _unregister(session_id: str) -> None:
    with _REGISTRY_LOCK:
        s = _SESSIONS.pop(session_id, None)
        if s is not None:
            key = (s.bot_id, s.channel)
            if _ACTIVE_BY_BOT_CHANNEL.get(key) == session_id:
                del _ACTIVE_BY_BOT_CHANNEL[key]


# ── PNG rendering ─────────────────────────────────────────────────────────────


def render_qr_png_data_url(payload: str) -> str:
    """Render ``payload`` as a base64 PNG data URL suitable for an <img src>.

    Public for testability; the worker thread calls this whenever a new QR
    payload arrives.
    """
    # Lazy import keeps the qrcode + Pillow cost off the admin-server cold
    # path when nobody's pairing.
    import qrcode  # type: ignore[import-untyped]
    from qrcode.constants import ERROR_CORRECT_L  # type: ignore[import-untyped]

    qr = qrcode.QRCode(
        version=None,  # auto-size based on payload length
        error_correction=ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    # Scale to a known pixel size so the UI doesn't have to do layout math
    img = img.resize((_QR_PNG_PX, _QR_PNG_PX))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ── Worker thread ─────────────────────────────────────────────────────────────


def _stdout_reader(session: Session, proc: subprocess.Popen) -> None:
    """Tail the subprocess stdout for QR payloads and pairing markers."""
    try:
        assert proc.stdout is not None  # we created with stdout=PIPE
        for raw in iter(proc.stdout.readline, ""):
            line = raw.strip()
            if not line:
                continue
            with session._lock:
                # Detect a QR payload — the most common emission. We render
                # the PNG immediately so the next poll picks it up.
                if _QR_PAYLOAD_RE.match(line):
                    session.qr_payload = line
                    try:
                        session.qr_png_data_url = render_qr_png_data_url(line)
                    except Exception as exc:
                        session.error = f"qr_render_failed: {exc.__class__.__name__}"
                        # don't fail the session — operator may still scan
                        # an earlier QR; just log
                        log.warning(
                            "qr-pairing: render failed for session %s: %s",
                            session.session_id, exc,
                        )
                    if session.state == "starting":
                        session.state = "waiting"
                    continue
                # Detect a paired marker — OC's CLI says something like
                # "Connected as <handle>" once the device-link completes.
                if _PAIRED_MARKER_RE.search(line):
                    if session.state != "cancelled":
                        session.state = "paired"
                    return
    except Exception as exc:
        with session._lock:
            if not session.is_terminal:
                session.state = "failed"
                session.error = f"stdout_reader: {exc.__class__.__name__}: {exc}"


def _watchdog(session: Session, proc: subprocess.Popen) -> None:
    """Enforce the TTL ceiling + reap the process when stdout closes."""
    end_by = session.started_at + _SESSION_TTL_S
    while True:
        # Bounded sleep so cancel/terminate can interrupt promptly
        time.sleep(1.0)
        with session._lock:
            terminal = session.is_terminal
        if terminal:
            break
        if time.monotonic() >= end_by:
            with session._lock:
                if not session.is_terminal:
                    session.state = "expired"
            break
        # Process exited on its own without setting a terminal state
        if proc.poll() is not None:
            with session._lock:
                if not session.is_terminal:
                    rc = proc.returncode
                    if rc == 0:
                        # Treat clean exit as paired if we never saw a
                        # marker — Baileys sometimes exits silently after
                        # the handshake completes.
                        session.state = "paired"
                    else:
                        session.state = "failed"
                        if not session.error:
                            session.error = f"process_exit_{rc}"
            break

    # Best-effort cleanup — terminate the process if still alive
    if proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    # Leave the session in the registry briefly so a final poll can
    # return the terminal state. Anchored to the monotonic clock (like the
    # TTL check above), not a single sleep(30) — sleeps can come back early
    # (the test suite caps them at 10 ms), and an early wake here would reap
    # the session out from under a poll that should still see the outcome.
    reap_at = time.monotonic() + _TERMINAL_RESIDENCY_S
    while time.monotonic() < reap_at:
        time.sleep(1.0)
    _unregister(session.session_id)


# ── Public API ────────────────────────────────────────────────────────────────


def _build_spawn_command(
    bot_user: str,
    bot_id: str,
    channel: str,
    config_path: Path,
    openclaw_bin: str = "/opt/homebrew/bin/openclaw",
) -> list[str]:
    """Build the argv for spawning ``openclaw channels login`` as bot_user.

    Public-ish (underscore-leading) for testability; tests assert the
    exact shape against the sudoers grants in
    ``setup_wizard._write_evolve_sudoers``.

    Mirrors deploy.py:2811's pattern for openclaw-as-bot-user calls:
    ``sudo --preserve-env=OPENCLAW_CONFIG_PATH -H -u <bot> openclaw …``.
    The ``--preserve-env`` arg matches the ``SETENV`` clause in the
    existing sudoers grant ``evolve ALL=(ALL) NOPASSWD: SETENV: <oc_path>``
    — no new sudoers grants needed.
    """
    return [
        "sudo",
        "--preserve-env=OPENCLAW_CONFIG_PATH",
        "-H", "-u", bot_user, "-n",
        openclaw_bin,
        "channels", "login",
        "--channel", channel,
        "--account", bot_id,
    ]


def start_session(
    bot_id: str,
    channel: str,
    *,
    bot_user: str | None = None,
    config_path: Path | None = None,
    spawn: Callable[..., subprocess.Popen] | None = None,
) -> dict[str, Any]:
    """Start a new pairing session.

    Parameters
    ----------
    bot_id : str
        Logical bot id; becomes the ``--account`` arg.
    channel : str
        OC channel id (``whatsapp``, ``signal``, …); becomes ``--channel``.
    bot_user : str | None
        macOS user the subprocess runs as. Defaults to ``get_bot_user(bot_id)``.
    config_path : Path | None
        Path to the bot's openclaw.json. Defaults to ``bot_home(bot_id)/.openclaw/openclaw.json``.
    spawn : callable | None
        Override for ``subprocess.Popen`` — tests inject a fake.

    Returns
    -------
    dict
        The session record's ``to_dict()`` immediately after start. The
        ``state`` will be ``starting`` until the worker captures the first
        QR (typically <2 s).
    """
    if bot_user is None or config_path is None:
        from ..config import bot_home as _bh, get_bot_user, load_network
        network = load_network()
        if bot_user is None:
            bot_user = get_bot_user(bot_id, network)
        if config_path is None:
            config_path = _bh(bot_id, network) / ".openclaw" / "openclaw.json"

    session = Session(
        session_id=f"qrp_{uuid.uuid4().hex[:12]}",
        bot_id=bot_id,
        channel=channel,
        started_at=time.monotonic(),
    )
    _register(session)

    cmd = _build_spawn_command(bot_user, bot_id, channel, config_path)
    # Match deploy.py:2811 — pass our parent env merged with the OC config
    # path. sudo --preserve-env=OPENCLAW_CONFIG_PATH carries that variable
    # across the sudo boundary so the openclaw CLI targets the right bot.
    import os as _os
    env = {**_os.environ, "OPENCLAW_CONFIG_PATH": str(config_path)}
    # cwd must be the bot's home; the bot user can't read /Users/Shared
    bot_home = str(config_path.parent.parent)

    spawn_fn = spawn or subprocess.Popen
    try:
        proc = spawn_fn(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered so QR lines arrive promptly
            env=env,
            cwd=bot_home,
        )
    except FileNotFoundError:
        with session._lock:
            session.state = "failed"
            session.error = "openclaw_cli_not_found"
        return session.to_dict()
    except PermissionError as exc:
        with session._lock:
            session.state = "failed"
            session.error = f"sudoers_grant_missing: {exc}"
        return session.to_dict()
    except Exception as exc:
        with session._lock:
            session.state = "failed"
            session.error = f"spawn_failed: {exc.__class__.__name__}: {exc}"
        return session.to_dict()

    session._proc = proc

    threading.Thread(
        target=_stdout_reader, args=(session, proc),
        name=f"qr-pair-stdout-{session.session_id}", daemon=True,
    ).start()
    threading.Thread(
        target=_watchdog, args=(session, proc),
        name=f"qr-pair-watchdog-{session.session_id}", daemon=True,
    ).start()

    return session.to_dict()


def poll_session(session_id: str) -> dict[str, Any] | None:
    """Return the current snapshot of ``session_id``, or ``None`` if unknown.

    The admin server's GET route calls this every ~2 s while the wizard is
    on the pairing screen. Never blocks; returns whatever the worker has
    pushed into the session record.
    """
    s = _lookup(session_id)
    return s.to_dict() if s is not None else None


def cancel_session(session_id: str) -> bool:
    """Cancel an in-flight session. Idempotent; returns ``True`` if a
    session was found and torn down, ``False`` if the id was unknown.
    """
    s = _lookup(session_id)
    if s is None:
        return False
    _terminate_session(s, "cancelled", reason="operator_cancel")
    return True


def _terminate_session(s: Session, terminal: str, *, reason: str) -> None:
    """Mark ``s`` as terminated and reap its subprocess. Idempotent."""
    with s._lock:
        if s.is_terminal:
            return
        s.state = terminal
        if reason and not s.error:
            s.error = reason
    proc = s._proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
