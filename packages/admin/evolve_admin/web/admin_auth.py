"""admin_auth — device-pairing authentication for the admin server (roadmap 2.1).

The admin server binds 127.0.0.1 only, but loopback does not defend against a
*compromised local process* (a malicious bot dependency, a prompt-injected agent,
the ``evo`` agent itself) — any can ``curl 127.0.0.1:5050`` into root-equivalent
pod control. Only browsers/PWA reach the server over HTTP (the evo proxy and MCP
bridge use subprocess/direct paths), so a device-cookie gate here cannot lock out
internal components.

**On by default** (roadmap 2.6): enforcement is active even on a fresh pod with
no key yet — the operator runs ``evolve-admin pair`` to mint the key and get a
pairing code; from then on the server requires a paired device cookie. The only
ways off are an explicit recorded opt-out (``evolve-admin auth disable`` writes
the ``admin-auth.disabled`` marker) or the test-env escape variable. See
``is_auth_enabled`` below.

**Pairing** uses a short-lived, key-derived code (TOTP-style): ``evolve-admin pair``
computes the current code from the shared key and prints it; the operator enters it
in the browser; the server verifies it against the same key and issues a signed
device token (cookie). No cross-process code file or TTL bookkeeping needed — the
key is the only shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Cookie the browser carries once paired.
DEVICE_COOKIE_NAME = "evolve_device"

# Pairing codes rotate on this window; verification accepts the current window
# ± 1 so a slightly-slow operator (or minor clock skew) still pairs. ~5–10 min.
PAIRING_WINDOW_SECONDS = 300

# Device tokens are long-lived (re-pair to rotate). The signature binds them to
# the key, so rotating/deleting the key invalidates every device at once.
_TOKEN_VERSION = "v1"


def _key_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "keystore" / "admin-auth.key"


def _load_key(shared_dir: Path) -> bytes | None:
    """Load the admin-auth HMAC key, or None if it doesn't exist yet."""
    try:
        raw = _key_path(shared_dir).read_text().strip()
        return bytes.fromhex(raw)
    except (OSError, ValueError):
        return None


def ensure_key(shared_dir: Path) -> bytes:
    """Return the admin-auth key, generating + persisting it (0600) if absent.

    Called by ``evolve-admin pair`` — generating the key is what *enables*
    enforcement, so auth turns on exactly when the operator opts in.
    """
    existing = _load_key(shared_dir)
    if existing is not None:
        return existing
    path = _key_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    key_hex = secrets.token_hex(32)
    tmp = path.with_suffix(".key.tmp")
    tmp.write_text(key_hex)
    tmp.chmod(0o600)
    tmp.rename(path)
    path.chmod(0o600)
    return bytes.fromhex(key_hex)


# ── Opt-out marker (roadmap 2.6 / decision D1) ──────────────────────────────
#
# Auth is ON BY DEFAULT: a pod enforces the device-cookie gate unless the
# operator has explicitly opted out by recording an acceptance. This inverts
# the prior opt-in model ("open until you run ``pair``") so a fresh or
# upgraded pod never ships its control plane open. The setup wizard mints the
# key + prints a pairing code at install, so first-run is forced pairing, not
# lockout-discovery; ``evolve-admin pair`` over SSH is the recovery path for
# an already-deployed pod.

# Env escape — forces auth OFF regardless of the marker. Used by the test
# suites (the admin conftest sets it) so the hundreds of create_app-based
# tests run open without per-test pairing. Operators use the recorded
# marker, not this env var.
_AUTH_DISABLED_ENV = "EVOLVE_ADMIN_AUTH_DISABLED"


def _optout_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "keystore" / "admin-auth.disabled"


def is_optout(shared_dir: Path) -> bool:
    """True if the operator recorded an explicit auth opt-out."""
    return _optout_path(shared_dir).exists()


def record_optout(shared_dir: Path, *, by: str, reason: str) -> None:
    """Write the opt-out marker (acceptance record). Auth stops enforcing."""
    import json as _json
    path = _optout_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json.dumps({"disabled": True, "by": by, "reason": reason})
    tmp = path.with_suffix(".disabled.tmp")
    tmp.write_text(payload)
    tmp.rename(path)


def clear_optout(shared_dir: Path) -> bool:
    """Remove the opt-out marker (re-enable enforcement). Returns True if a
    marker was present."""
    path = _optout_path(shared_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def is_auth_enabled(shared_dir: Path) -> bool:
    """True when the server should enforce the device-cookie gate.

    **On by default** (roadmap 2.6): enforced UNLESS the operator recorded an
    explicit opt-out (``admin-auth.disabled`` marker) or the test env escape
    is set. Key-absent + no opt-out = enforced (the operator runs
    ``evolve-admin pair`` to mint the key and get a code).
    """
    import os as _os
    if _os.environ.get(_AUTH_DISABLED_ENV):
        return False
    return not is_optout(shared_dir)


# ── pairing codes (key-derived, TOTP-style) ─────────────────────────────────────


def _code_for_window(key: bytes, window: int) -> str:
    digest = hmac.new(key, f"pair:{window}".encode(), hashlib.sha256).digest()
    # 6 DECIMAL digits (HOTP-style dynamic truncation, RFC 4226). Numeric — not
    # hex — so the operator types only the keypad: no letter/case ambiguity, no
    # b/d/6 glyph confusion, unambiguous to read aloud or transcribe off a
    # terminal. 6 digits = 1e6 space per window. Entropy is NOT the brute-force
    # wall (an unthrottled local process would exhaust any short code) — the
    # PairThrottle on /api/pair is. This is the 2FA/PIN trade: short + friendly,
    # safe because guessing is rate-limited.
    offset = digest[-1] & 0x0F
    binary = (
        (digest[offset] & 0x7F) << 24
        | digest[offset + 1] << 16
        | digest[offset + 2] << 8
        | digest[offset + 3]
    )
    return f"{binary % 1_000_000:06d}"


def current_pairing_code(shared_dir: Path) -> str:
    """The code to display now. Ensures the key exists (enabling enforcement)."""
    key = ensure_key(shared_dir)
    return _code_for_window(key, int(time.time() // PAIRING_WINDOW_SECONDS))


def verify_pairing_code(shared_dir: Path, code: str) -> bool:
    """True if ``code`` matches the current pairing window (± 1)."""
    key = _load_key(shared_dir)
    if key is None or not code:
        return False
    code = code.strip().lower()
    now_window = int(time.time() // PAIRING_WINDOW_SECONDS)
    for window in (now_window - 1, now_window, now_window + 1):
        if hmac.compare_digest(code, _code_for_window(key, window)):
            return True
    return False


# ── brute-force throttle for /api/pair ──────────────────────────────────────────


class PairThrottle:
    """Escalating, auto-expiring cooldown for the ``/api/pair`` endpoint.

    The pairing code is short by design (usability); this throttle — not the
    code's entropy — is what makes online guessing infeasible. Without it a
    compromised local process could ``curl 127.0.0.1`` the pair endpoint
    unboundedly and brute-force any short code in minutes.

    **Global, not per-IP.** The server binds loopback, so every attempt arrives
    from the same machine — per-IP keying would buy nothing. Pairing is a
    single-operator action that is never legitimately concurrent, so one
    endpoint-wide counter is correct.

    **Behaviour.** ``FREE_ATTEMPTS`` wrong codes are allowed back-to-back (a
    fumbling operator pays nothing). Each failure beyond that arms a cooldown
    that starts at ``BASE_COOLDOWN`` and doubles, capped at ``MAX_COOLDOWN``.
    While a cooldown is armed, ``allowed()`` is False and the caller should NOT
    verify the code at all — so an attacker gets **zero** guesses during the
    window, not a free check. A correct code resets the counter.

    **No wedge.** The cooldown auto-expires (≤ ``MAX_COOLDOWN``); there is no
    persistent lockout to get stuck in and no admin-reset state — a legitimate
    operator is delayed at most ``MAX_COOLDOWN`` even after many fumbles. This
    is the deliberate choice over a hard "locked until admin reset", which in a
    single-operator pod is both a self-DoS vector and unrecoverable (the admin
    UI is the very thing locked).

    State is in-process (one instance per app); pairing attempts are rare so
    there is nothing to persist. Uses a monotonic clock so cooldowns are immune
    to wall-clock/NTP adjustments; injectable for tests.

    **Thread-safe.** Flask serves ``/api/pair`` on multiple threads
    (``app.run(threaded=True)`` by default) and the threat actor — a local
    process — can trivially fire concurrent requests. A lock makes the
    check-verify-record sequence atomic via :meth:`attempt`, so the "zero
    guesses while armed" guarantee holds under concurrency and the failure
    counter can't be lost-updated past the grace count.
    """

    FREE_ATTEMPTS = 10
    BASE_COOLDOWN = 30.0
    MAX_COOLDOWN = 300.0

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0

    def attempt(self, verify: Callable[[], bool]) -> tuple[str, int]:
        """Atomically gate one pairing attempt. Returns ``(outcome, wait)``:

        - ``("throttled", secs)`` — a cooldown is armed; ``verify`` was NOT
          called (the attacker gets no guess during the window).
        - ``("ok", 0)`` — ``verify()`` returned True; throttle reset.
        - ``("bad", 0)`` — ``verify()`` returned False; failure recorded.

        ``verify`` runs under the lock, so the whole gate is serialized — there
        is no window where two threads both pass the cooldown check and both
        guess. Pairing is rare, so serializing it costs nothing.
        """
        with self._lock:
            wait = self._retry_after()
            if wait > 0:
                return "throttled", wait
            if verify():
                self._failures = 0
                self._locked_until = 0.0
                return "ok", 0
            self._record_failure()
            return "bad", 0

    def retry_after(self) -> int:
        """Seconds to wait before the next attempt is allowed, or 0 if allowed
        now (rounded up). Lock-held snapshot for callers/tests."""
        with self._lock:
            return self._retry_after()

    def allowed(self) -> bool:
        """True if a pair attempt may be verified now (no cooldown armed)."""
        return self.retry_after() == 0

    def record_success(self) -> None:
        """Clear all throttle state after a successful pairing."""
        with self._lock:
            self._failures = 0
            self._locked_until = 0.0

    def record_failure(self) -> None:
        """Count a wrong code; arm/escalate the cooldown past the grace count."""
        with self._lock:
            self._record_failure()

    # ── internals (caller must hold the lock) ──

    def _retry_after(self) -> int:
        remaining = self._locked_until - self._clock()
        return math.ceil(remaining) if remaining > 0 else 0

    def _record_failure(self) -> None:
        # A cooldown that has fully elapsed decays the escalation back to the
        # gentle floor, so a legit operator who waited one out isn't re-slammed
        # to the 5-min cap by a single further mistype. An attacker still pays
        # at most one guess per expired cooldown (≤ one per MAX_COOLDOWN) — the
        # steady-state rate is unchanged.
        if self._locked_until and self._clock() >= self._locked_until:
            self._failures = self.FREE_ATTEMPTS
            self._locked_until = 0.0
        self._failures += 1
        over = self._failures - self.FREE_ATTEMPTS
        if over > 0:
            cooldown = min(self.BASE_COOLDOWN * (2 ** (over - 1)), self.MAX_COOLDOWN)
            self._locked_until = self._clock() + cooldown


# ── device tokens (signed cookie) ───────────────────────────────────────────────


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def issue_device_token(shared_dir: Path) -> str:
    """Mint a signed device token (the cookie value) for a freshly-paired device.

    Format ``v1:<device_id>:<issued_at>:<sig>``. The signature binds it to the
    admin-auth key; there is no server-side session store.
    """
    key = ensure_key(shared_dir)
    device_id = secrets.token_hex(8)
    issued_at = str(int(time.time()))
    payload = f"{_TOKEN_VERSION}:{device_id}:{issued_at}"
    return f"{payload}:{_sign(key, payload)}"


def verify_device_token(shared_dir: Path, token: str | None) -> bool:
    """True if ``token`` is a validly-signed device token for this pod's key."""
    key = _load_key(shared_dir)
    if key is None or not token:
        return False
    parts = token.split(":")
    if len(parts) != 4:
        return False
    version, device_id, issued_at, sig = parts
    if version != _TOKEN_VERSION:
        return False
    expected = _sign(key, f"{version}:{device_id}:{issued_at}")
    return hmac.compare_digest(sig, expected)
