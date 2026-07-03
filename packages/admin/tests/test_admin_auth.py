"""Device-pairing admin auth core (roadmap 2.1 + 2.6).

Auth is ON BY DEFAULT (roadmap 2.6): a pod enforces unless the operator
records an explicit opt-out marker. ``pair`` mints the key + a code; device
tokens are signed and stateless; pairing codes are key-derived (TOTP-style).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.web import admin_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _enforce_real_auth(monkeypatch):
    """This file tests the real enforcement logic — clear the suite-wide env
    escape the conftest sets so is_auth_enabled reflects the marker, not the
    test convenience flag."""
    monkeypatch.delenv(admin_auth._AUTH_DISABLED_ENV, raising=False)


# ── on-by-default + opt-out ─────────────────────────────────────────────────────


def test_enforced_by_default_on_fresh_pod(tmp_path):
    # No key, no opt-out marker → ENFORCED (the 2.6 inversion).
    assert admin_auth.is_auth_enabled(tmp_path) is True


def test_optout_marker_disables_enforcement(tmp_path):
    admin_auth.record_optout(tmp_path, by="pod-admin", reason="kiosk")
    assert admin_auth.is_optout(tmp_path) is True
    assert admin_auth.is_auth_enabled(tmp_path) is False
    # …and clearing it re-enables.
    assert admin_auth.clear_optout(tmp_path) is True
    assert admin_auth.is_auth_enabled(tmp_path) is True


def test_env_escape_disables_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv(admin_auth._AUTH_DISABLED_ENV, "1")
    assert admin_auth.is_auth_enabled(tmp_path) is False


def test_pair_generates_key(tmp_path):
    admin_auth.current_pairing_code(tmp_path)  # `evolve-admin pair` path
    assert admin_auth._key_path(tmp_path).exists()
    assert (admin_auth._key_path(tmp_path).stat().st_mode & 0o777) == 0o600
    # Enforcement is on regardless (default), key or not.
    assert admin_auth.is_auth_enabled(tmp_path) is True


def test_ensure_key_is_idempotent(tmp_path):
    k1 = admin_auth.ensure_key(tmp_path)
    k2 = admin_auth.ensure_key(tmp_path)
    assert k1 == k2  # second call returns the same key, doesn't regenerate


# ── pairing codes ───────────────────────────────────────────────────────────────


def _a_wrong_code(shared) -> str:
    """A 6-digit string guaranteed not to match any accepted window — picked
    deterministically so the negative tests can't flake on a 1-in-a-million
    derived-code collision."""
    key = admin_auth._load_key(shared)
    now = int(time.time() // admin_auth.PAIRING_WINDOW_SECONDS)
    valid = {admin_auth._code_for_window(key, now + d) for d in (-1, 0, 1)}
    for i in range(len(valid) + 1):
        candidate = f"{i:06d}"
        if candidate not in valid:
            return candidate
    raise AssertionError("unreachable")


def test_current_code_verifies(tmp_path):
    code = admin_auth.current_pairing_code(tmp_path)
    assert admin_auth.verify_pairing_code(tmp_path, code) is True


def test_code_is_six_digits(tmp_path):
    code = admin_auth.current_pairing_code(tmp_path)
    assert len(code) == 6
    assert code.isdigit()  # numeric only — no hex letters to fat-finger


def test_wrong_code_rejected(tmp_path):
    admin_auth.current_pairing_code(tmp_path)
    assert admin_auth.verify_pairing_code(tmp_path, _a_wrong_code(tmp_path)) is False
    assert admin_auth.verify_pairing_code(tmp_path, "") is False


def test_code_verification_is_case_insensitive(tmp_path):
    # Digits have no case, but the normalisation contract still holds — a code
    # round-trips through .upper() unchanged and still verifies.
    code = admin_auth.current_pairing_code(tmp_path)
    assert admin_auth.verify_pairing_code(tmp_path, code.upper()) is True


def test_pairing_code_fails_when_no_key(tmp_path):
    # verify must never accept anything before the operator has paired
    assert admin_auth.verify_pairing_code(tmp_path, "123456") is False


# ── brute-force throttle ────────────────────────────────────────────────────────


class _FakeClock:
    """Monotonic clock stub — advance it by hand to test cooldown expiry."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_throttle_allows_grace_attempts():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    # The first FREE_ATTEMPTS failures arm no cooldown — a fumbling operator
    # pays nothing.
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS):
        th.record_failure()
        assert th.allowed() is True
        assert th.retry_after() == 0


def test_throttle_arms_cooldown_past_grace():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS + 1):
        th.record_failure()
    # The (grace+1)-th failure arms the base cooldown.
    assert th.allowed() is False
    assert th.retry_after() == int(admin_auth.PairThrottle.BASE_COOLDOWN)


def test_throttle_cooldown_expires():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS + 1):
        th.record_failure()
    assert th.allowed() is False
    clock.advance(admin_auth.PairThrottle.BASE_COOLDOWN)
    # Auto-expires — no admin reset, no persistent lockout to get wedged in.
    assert th.allowed() is True
    assert th.retry_after() == 0


def test_throttle_escalates_and_caps():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS):
        th.record_failure()
    # Each failure past grace doubles the cooldown (measured right after arming,
    # before the clock advances).
    th.record_failure()
    assert th.retry_after() == int(admin_auth.PairThrottle.BASE_COOLDOWN)
    th.record_failure()
    assert th.retry_after() == int(admin_auth.PairThrottle.BASE_COOLDOWN * 2)
    # …and never exceeds the cap, however many failures pile up.
    for _ in range(20):
        th.record_failure()
    assert th.retry_after() == int(admin_auth.PairThrottle.MAX_COOLDOWN)


def test_throttle_success_resets():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS + 5):
        th.record_failure()
    assert th.allowed() is False
    th.record_success()
    # A correct code wipes the counter — the next operator starts fresh.
    assert th.allowed() is True
    assert th.retry_after() == 0


def test_throttle_decays_after_cooldown_expires():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    # Pile up well past grace so the cooldown is at/near the cap.
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS + 8):
        th.record_failure()
    # Wait the whole cooldown out, then mistype once more.
    clock.advance(admin_auth.PairThrottle.MAX_COOLDOWN)
    th.record_failure()
    # Decay → the next failure re-arms at the GENTLE base cooldown, not the cap,
    # so a fumbling operator who waited isn't re-slammed to 5 minutes.
    assert th.retry_after() == int(admin_auth.PairThrottle.BASE_COOLDOWN)


# ── atomic attempt() gate (concurrency-safe path used by /api/pair) ──────────────


def test_attempt_ok_resets_and_bad_records():
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    assert th.attempt(lambda: False) == ("bad", 0)
    assert th.attempt(lambda: True) == ("ok", 0)  # success path
    assert th.allowed() is True


def test_attempt_while_armed_does_not_verify():
    """The core anti-brute-force invariant: once a cooldown is armed, attempt()
    must NOT call verify — an attacker gets zero guesses during the window."""
    clock = _FakeClock()
    th = admin_auth.PairThrottle(clock=clock)
    calls = []

    def verify_false() -> bool:
        calls.append(1)
        return False

    # Burn grace + arm the cooldown; each of these DOES call verify.
    for _ in range(admin_auth.PairThrottle.FREE_ATTEMPTS + 1):
        assert th.attempt(verify_false)[0] == "bad"
    armed_calls = len(calls)

    outcome, wait = th.attempt(verify_false)
    assert outcome == "throttled"
    assert wait > 0
    assert len(calls) == armed_calls  # verify was NOT invoked while throttled


# ── device tokens ───────────────────────────────────────────────────────────────


def test_issued_token_verifies(tmp_path):
    token = admin_auth.issue_device_token(tmp_path)
    assert admin_auth.verify_device_token(tmp_path, token) is True


def test_tampered_token_rejected(tmp_path):
    token = admin_auth.issue_device_token(tmp_path)
    version, device_id, issued_at, _sig = token.split(":")
    forged = f"{version}:{device_id}:{issued_at}:" + "deadbeef" * 8
    assert admin_auth.verify_device_token(tmp_path, forged) is False


def test_malformed_token_rejected(tmp_path):
    admin_auth.ensure_key(tmp_path)
    assert admin_auth.verify_device_token(tmp_path, "garbage") is False
    assert admin_auth.verify_device_token(tmp_path, None) is False
    assert admin_auth.verify_device_token(tmp_path, "a:b:c") is False


def test_token_from_a_different_key_rejected(tmp_path):
    """Rotating/deleting the key invalidates every device."""
    token = admin_auth.issue_device_token(tmp_path)
    admin_auth._key_path(tmp_path).unlink()  # key gone
    assert admin_auth.verify_device_token(tmp_path, token) is False  # no key → no trust
