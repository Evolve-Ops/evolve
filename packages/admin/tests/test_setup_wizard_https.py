"""tests/test_setup_wizard_https.py — Phase 4.1.c wizard HTTPS phase.

Covers the new `_run_https_phase` step inserted into `run_fresh_wizard()`
per `internal/spec-pwa-phase0-https-2026-05-18.md` §3.4. The phase wraps
`enable_https_if_possible` (never-raises) and translates each outcome
into one of four operator-facing lines:

* Skipped (Tailscale not installed / not signed in / version too old)
* Skipped (admin-console HTTPS-cert toggle off — defer-not-block)
* Succeeded
* Failed mid-flow (preflight OK but serve / verification failed)

All Tailscale subprocess calls are mocked via `enable_https_if_possible`
itself; these tests live one layer up and only verify the wizard's
translation step.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import https_setup  # noqa: E402
from evolve_admin.setup_wizard import _run_https_phase  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


def _attempt(
    preflight: https_setup.PreflightResult,
    *,
    result: https_setup.HttpsSetupResult | None = None,
    error: str = "",
) -> https_setup.HttpsSetupAttempt:
    return https_setup.HttpsSetupAttempt(
        preflight=preflight, result=result, error=error
    )


def _capture_phase(attempt: https_setup.HttpsSetupAttempt, capsys, net_path: Path):
    """Run the phase under a wider console so Rich doesn't soft-wrap our
    assertion targets. Returns whitespace-collapsed stdout for easy
    substring matching."""
    import re as _re
    from evolve_admin import setup_wizard as _wizard

    with patch(
        "evolve_admin.https_setup.enable_https_if_possible",
        return_value=attempt,
    ) as patched:
        # Force a wide width so Rich keeps full URLs / commands on one
        # line in the captured output.
        original_width = _wizard.console.width
        try:
            _wizard.console.width = 240
            _run_https_phase(net_path)
        finally:
            _wizard.console.width = original_width
    raw = capsys.readouterr().out
    # Collapse all whitespace runs so substring asserts don't trip on
    # any remaining wrap. Keep the raw too for debugging.
    flat = _re.sub(r"\s+", " ", raw)
    return flat, patched


@pytest.fixture
def net_path(tmp_path: Path) -> Path:
    """A real on-disk path so the wizard signature is honored.

    The mocked `enable_https_if_possible` doesn't actually touch the
    file; we just need the argument to be a valid Path.
    """
    p = tmp_path / "network.json"
    p.write_text('{"networkId": "test-net"}')
    return p


# ── Outcome 1: skipped — Tailscale not in usable state ─────────────────────


def test_phase_prints_skipped_for_need_install(capsys, net_path: Path):
    attempt = _attempt(
        https_setup.PreflightResult.NEED_INSTALL,
        error="Tailscale CLI not found.\nInstall Tailscale: ...",
    )
    out, patched = _capture_phase(attempt, capsys, net_path)
    assert patched.call_args.kwargs["network_path"] == net_path
    assert "HTTPS skipped" in out
    assert "Tailscale not installed" in out
    assert "sudo evolve-admin enable-https" in out


def test_phase_prints_skipped_for_need_login(capsys, net_path: Path):
    attempt = _attempt(
        https_setup.PreflightResult.NEED_LOGIN,
        error="Tailscale backend state is 'NeedsLogin' ...",
    )
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "HTTPS skipped" in out
    assert "not signed in" in out
    assert "sudo evolve-admin enable-https" in out


def test_phase_prints_skipped_for_need_upgrade(capsys, net_path: Path):
    attempt = _attempt(
        https_setup.PreflightResult.NEED_UPGRADE,
        error="Tailscale 1.40.1 is too old ...",
    )
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "HTTPS skipped" in out
    assert "too old" in out
    assert "v1.44" in out
    assert "sudo evolve-admin enable-https" in out


# ── Outcome 2: deferred — admin-console toggle is off ──────────────────────


def test_phase_prints_one_time_instructions_for_need_toggle(capsys, net_path: Path):
    """NEED_TOGGLE prints the one-time tailnet setup block + a warn line."""
    attempt = _attempt(
        https_setup.PreflightResult.NEED_TOGGLE,
        error=(
            "HTTPS cert provisioning is not enabled for this tailnet. "
            "Open https://login.tailscale.com/admin/dns ..."
        ),
    )
    out, _ = _capture_phase(attempt, capsys, net_path)
    # One-time setup block elements
    assert "One-time Tailscale setup needed" in out
    assert "login.tailscale.com/admin/dns" in out
    assert 'Enable HTTPS' in out  # the button label, quoted in the block
    assert "sudo evolve-admin enable-https" in out
    # Warn line — matches the brief's table
    assert "HTTPS skipped — enable the Tailscale admin-console toggle" in out


# ── Outcome 3: succeeded ───────────────────────────────────────────────────


def test_phase_prints_enabled_on_success(capsys, net_path: Path):
    """READY + result.changed=True → "HTTPS enabled at <url>"."""
    result = https_setup.HttpsSetupResult(
        ok=True,
        url="https://team_bot_a.example.ts.net",
        changed=True,
        messages=["tailscale serve --bg --https=443 http://127.0.0.1:5050",
                  "adminBaseUrl → https://team_bot_a.example.ts.net",
                  "Verified https://team_bot_a.example.ts.net/api/health → 200"],
    )
    attempt = _attempt(https_setup.PreflightResult.READY, result=result)
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "HTTPS enabled at https://team_bot_a.example.ts.net" in out
    # The wizard should not surface the operational transcript noise —
    # that's CLI output; the wizard is the operator-facing summary line.
    assert "tailscale serve --bg" not in out
    assert "adminBaseUrl →" not in out


def test_phase_idempotent_succeeded_already_enabled(capsys, net_path: Path):
    """READY + result.changed=False (re-run on already-HTTPS pod) → still
    a "HTTPS enabled at <url>" line, but in the "already done" style."""
    result = https_setup.HttpsSetupResult(
        ok=True,
        url="https://team_bot_a.example.ts.net",
        changed=False,
        messages=["Already enabled at https://team_bot_a.example.ts.net."],
    )
    attempt = _attempt(https_setup.PreflightResult.READY, result=result)
    out, _ = _capture_phase(attempt, capsys, net_path)
    # Still mentions the URL so the operator sees the actual destination
    assert "https://team_bot_a.example.ts.net" in out
    # No duplicate-write warning, no failure line
    assert "failed" not in out.lower()


def test_phase_surfaces_app_store_path_hint_note(capsys, net_path: Path):
    """The "Note: ..." hint from the underlying helper (App Store install
    not on PATH) should ride through to the wizard transcript so the
    operator sees it once."""
    result = https_setup.HttpsSetupResult(
        ok=True,
        url="https://team_bot_a.example.ts.net",
        changed=True,
        messages=[
            "Note: Tailscale CLI was found at /Applications/Tailscale.app/...\n"
            "but not on your shell's PATH. ...",
            "tailscale serve --bg ...",
        ],
    )
    attempt = _attempt(https_setup.PreflightResult.READY, result=result)
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "Note: Tailscale CLI" in out
    # Non-Note messages must NOT appear (the wizard is a summary; CLI is
    # the place for the operational transcript)
    assert "tailscale serve --bg" not in out


# ── Outcome 4: failed mid-flow ─────────────────────────────────────────────


def test_phase_prints_failed_for_attempt_with_ready_preflight_and_no_result(
    capsys, net_path: Path,
):
    """Preflight READY but apply failed → "HTTPS setup failed: <reason>"."""
    attempt = _attempt(
        https_setup.PreflightResult.READY,
        result=None,
        error="Verification fetch to https://team_bot_a.example.ts.net/api/health failed: timeout",
    )
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "HTTPS setup failed:" in out
    assert "Verification fetch" in out or "timeout" in out
    assert "Pod remains on HTTP" in out
    assert "sudo evolve-admin enable-https" in out


def test_phase_prints_failed_with_fallback_reason_when_error_blank(
    capsys, net_path: Path,
):
    """Defensive: if the helper returned a failed attempt but no error
    text, the wizard still prints something useful instead of an empty
    parens."""
    attempt = _attempt(
        https_setup.PreflightResult.READY,
        result=None,
        error="",
    )
    out, _ = _capture_phase(attempt, capsys, net_path)
    assert "HTTPS setup failed:" in out
    assert "unknown error" in out


# ── Defense-in-depth ───────────────────────────────────────────────────────


def test_phase_does_not_propagate_unexpected_exceptions(capsys, net_path: Path):
    """`enable_https_if_possible` is documented as non-raising, but any
    escape (importer error, OSError on save) must not abort setup —
    the wizard should warn and continue with HTTP."""
    import re as _re
    from evolve_admin import setup_wizard as _wizard

    with patch(
        "evolve_admin.https_setup.enable_https_if_possible",
        side_effect=RuntimeError("unexpected"),
    ):
        original_width = _wizard.console.width
        try:
            _wizard.console.width = 240
            # Must not raise — the wizard catches and warns
            _run_https_phase(net_path)
        finally:
            _wizard.console.width = original_width
    out = _re.sub(r"\s+", " ", capsys.readouterr().out)
    assert "HTTPS setup hit an unexpected error" in out
    assert "unexpected" in out
    assert "Pod remains on HTTP" in out


def test_phase_passes_network_path_through(capsys, tmp_path: Path):
    """The wizard's net_path must reach enable_https_if_possible — not
    the function's default DEFAULT_NETWORK_CONFIG (which would write to
    the real install location during a test)."""
    custom = tmp_path / "custom-net.json"
    custom.write_text('{"networkId": "x"}')
    attempt = _attempt(https_setup.PreflightResult.NEED_INSTALL, error="x")
    with patch(
        "evolve_admin.https_setup.enable_https_if_possible",
        return_value=attempt,
    ) as patched:
        _run_https_phase(custom)
    assert patched.call_args.kwargs["network_path"] == custom
