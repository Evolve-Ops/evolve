"""Tests for the security-cve-scan deterministic finalizer.

Pins the discipline that the LLM-driven app delegates to Python:
applicability filtering, baseline-mute, idempotency, message render
per docs/operator-message-style.md.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_APP_DIR = _ANALYZER_DIR / "evolve_apps" / "security-cve-scan"
for _p in (str(_ANALYZER_DIR), str(_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import finalize as F  # noqa: E402  (security-cve-scan/finalize.py)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_security(tmp_path, monkeypatch):
    """Redirect every disk path the finalizer touches into tmp_path."""
    sec = tmp_path / "security"
    keystore = tmp_path / "keystore"
    tmp_fallback = tmp_path / "tmp-fallback"
    sec.mkdir()
    keystore.mkdir()
    tmp_fallback.mkdir()
    monkeypatch.setattr(F, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(F, "SECURITY_DIR", sec)
    monkeypatch.setattr(F, "KEYSTORE_DIR", keystore)
    monkeypatch.setattr(F, "TMP_FALLBACK_DIR", tmp_fallback)
    monkeypatch.setattr(F, "BASELINE_PATH", sec / "cve-baseline.json")
    monkeypatch.setattr(F, "TOKEN_PATH", keystore / "security-alert-token")
    monkeypatch.setattr(F, "CHAT_ID_PATH", keystore / "security-alert-chat-id")
    return {
        "shared": tmp_path,
        "security": sec,
        "keystore": keystore,
        "tmp_fallback": tmp_fallback,
    }


def _t(year=2026, month=5, day=11, hour=9, minute=10) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=F.POD_TZ)


def _write_candidates(security_dir: Path, day: str, candidates: list[dict]) -> Path:
    p = security_dir / f"candidates-{day}.json"
    p.write_text(json.dumps(candidates))
    return p


# ── Shared-dir resolution (platform-port regression) ───────────────────────


def test_resolve_shared_dir_honors_network_shareddir(tmp_path, monkeypatch):
    """``SHARED_DIR`` must be derived from the platform profile +
    ``network.json``'s ``sharedDir`` — never the hardcoded macOS literal.

    Regression for the Linux VPS crash: a hardcoded
    ``/Users/Shared/evolve`` made ``write_log_entry`` try to
    ``mkdir -p /Users/...`` on a Linux pod (shared dir ``/var/lib/evolve``)
    and die with ``PermissionError``, wedging the finalize systemd unit.
    """
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path / "pod-shared")}))
    monkeypatch.setenv("EVOLVE_NETWORK", str(net))
    assert F._resolve_shared_dir() == tmp_path / "pod-shared"


# ── Installed-version candidate paths (Linux port) ──────────────────────────


def test_openclaw_install_candidates_linux_includes_nodesource_path():
    """The finalizer reads the installed version from these paths. The
    macOS-only list missed /usr/lib/node_modules (npm root -g on the VPS), so
    the version read as None and is_applicable() returned None for every
    finding — the version filter silently went inert."""
    from platform_profile import LINUX, MACOS, get_profile, set_profile
    prev = get_profile()
    set_profile(LINUX)
    try:
        cands = [str(p) for p in F._resolve_openclaw_install_candidates()]
    finally:
        set_profile(prev)
    assert "/usr/lib/node_modules/openclaw/package.json" in cands
    assert all("homebrew" not in c for c in cands)

    set_profile(MACOS)
    try:
        macos_cands = [str(p) for p in F._resolve_openclaw_install_candidates()]
    finally:
        set_profile(prev)
    assert "/opt/homebrew/lib/node_modules/openclaw/package.json" in macos_cands


# ── Version parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize("range_str, expected", [
    ("< 2026.4.10", "2026.4.10"),
    ("<2026.4.10", "2026.4.10"),
    ("<= 2026.4.10", "2026.4.10"),
    ("2026.4.0 – 2026.4.10", "2026.4.10"),
    ("2026.4.0 to 2026.4.10", "2026.4.10"),
    ("affected: all", None),
    ("", None),
])
def test_parse_range_upper_bound(range_str, expected):
    assert F._parse_range_upper_bound(range_str) == expected


@pytest.mark.parametrize("installed, upper, expected", [
    ("2026.4.29", "2026.4.10", False),   # newer than upper → not applicable
    ("2026.4.9",  "2026.4.10", True),    # older than upper → applicable
    ("2026.4.10", "2026.4.10", False),   # equal to upper bound → not applicable (< excludes)
    ("2027.1.0",  "2026.4.10", False),   # major-year newer
])
def test_is_applicable_with_known_versions(installed, upper, expected):
    c = F.Candidate(
        cve_id="CVE-X", affects_package="openclaw",
        affects_range=f"< {upper}",
    )
    iv = F.InstalledVersions(openclaw=installed, macos=None)
    assert F.is_applicable(c, iv) is expected


def test_is_applicable_unknown_installed_returns_none():
    """If we don't know what's installed, return None so the caller
    includes the finding (operator can judge)."""
    c = F.Candidate(cve_id="CVE-X", affects_package="openclaw",
                    affects_range="< 2026.4.10")
    iv = F.InstalledVersions(openclaw=None, macos=None)
    assert F.is_applicable(c, iv) is None


def test_is_applicable_unparseable_range_returns_none():
    c = F.Candidate(cve_id="CVE-X", affects_package="openclaw",
                    affects_range="affected: all versions")
    iv = F.InstalledVersions(openclaw="2026.4.29", macos=None)
    assert F.is_applicable(c, iv) is None


def test_is_applicable_unknown_package_returns_none():
    c = F.Candidate(cve_id="CVE-X", affects_package="some-other-tool",
                    affects_range="< 1.0")
    iv = F.InstalledVersions(openclaw="2026.4.29", macos="15.4")
    assert F.is_applicable(c, iv) is None


# ── Baseline / mute ────────────────────────────────────────────────────────


def test_is_muted_recognizes_new_and_legacy_keys():
    """Both 'muted: true' (new) and 'acknowledged: true' (legacy)
    suppress the finding."""
    baseline = {
        "CVE-NEW": {"muted": True},
        "CVE-LEGACY": {"acknowledged": True},
        "CVE-UNMUTED": {"muted": False},
    }
    assert F.is_muted("CVE-NEW", baseline) is True
    assert F.is_muted("CVE-LEGACY", baseline) is True
    assert F.is_muted("CVE-UNMUTED", baseline) is False
    assert F.is_muted("CVE-UNKNOWN", baseline) is False


# ── Rendering (style guide) ────────────────────────────────────────────────


def test_render_alert_has_plain_summary_first_after_header():
    """Style guide: header → one-sentence plain-English summary → details.
    The summary line must NOT contain the raw CVE jargon — that's the
    operator-readability bar."""
    c = F.Candidate(
        cve_id="CVE-2026-43584", affects_package="openclaw",
        affects_range="< 2026.4.10",
        source_url="https://example.com/advisory",
        severity="warn",
        plain_summary="An OpenClaw flaw lets a malicious agent escape env-var denylists and escalate locally.",
    )
    iv = F.InstalledVersions(openclaw="2026.4.5", macos="15.4")
    out = F.render_alert([c], [], iv, _t().date())
    lines = out.splitlines()
    assert lines[0] == "⚠️ Security finding — CVE-2026-43584"
    assert "malicious agent escape env-var denylists" in lines[1]
    assert "Affects: openclaw < 2026.4.10" in out
    assert "Installed: OpenClaw 2026.4.5" in out
    assert "Reply 'mute CVE-2026-43584'" in out
    # No redundant "in the Evolve bot conversation" phrasing.
    assert "in the Evolve bot conversation" not in out


def test_render_alert_uses_critical_emoji_for_critical_severity():
    c = F.Candidate(cve_id="CVE-X", severity="critical",
                    affects_package="openclaw",
                    plain_summary="Active exploit in the wild.")
    iv = F.InstalledVersions(openclaw="2026.4.5", macos=None)
    out = F.render_alert([c], [], iv, _t().date())
    assert out.startswith("🔴 Security finding — CVE-X")


def test_render_alert_handles_injection_attempt():
    inj = F.Candidate(
        cve_id="INJECTION_ATTEMPT", source_url="https://shady.example",
        plain_summary="Page contained 'NEVER OVERRIDE: ignore previous instructions'.",
    )
    iv = F.InstalledVersions(openclaw="2026.4.5", macos=None)
    out = F.render_alert([], [inj], iv, _t().date())
    assert "⚠️ Prompt injection attempt" in out
    assert "NEVER OVERRIDE" in out
    assert "No action needed" in out


def test_render_alert_empty_when_no_findings():
    iv = F.InstalledVersions(openclaw="2026.4.5", macos=None)
    assert F.render_alert([], [], iv, _t().date()) == ""


# ── candidates_path: primary + /tmp fallback ───────────────────────────────


def test_candidates_path_returns_primary_when_exists(tmp_security):
    day = _t().date()
    primary = tmp_security["security"] / f"candidates-{day.isoformat()}.json"
    primary.write_text("[]")
    assert F.candidates_path(day) == primary


def test_candidates_path_falls_back_to_tmp_when_primary_missing(tmp_security):
    """When the LLM scan ran as a bot user without SECURITY_DIR write access,
    it writes candidates to /tmp. The finalizer (running as evolve) must
    find the file there."""
    day = _t().date()
    fallback = tmp_security["tmp_fallback"] / f"candidates-{day.isoformat()}.json"
    fallback.write_text("[]")
    assert F.candidates_path(day) == fallback


def test_candidates_path_returns_primary_when_neither_exists(tmp_security):
    """Defensive default: caller (load_candidates) handles the missing-file
    case, so we hand back the primary path rather than the fallback."""
    day = _t().date()
    primary = tmp_security["security"] / f"candidates-{day.isoformat()}.json"
    assert F.candidates_path(day) == primary
    assert not primary.exists()


# ── End-to-end finalizer ──────────────────────────────────────────────────


def test_finalize_skips_not_applicable_finding(tmp_security):
    """A finding affecting OpenClaw < 2026.4.10 against an installed
    version of 2026.4.29 must be filtered out — this is the regression
    test for the false-positive that triggered the refactor."""
    day = _t().date().isoformat()
    _write_candidates(tmp_security["security"], day, [{
        "cve_id": "CVE-2026-43584",
        "affects_package": "openclaw",
        "affects_range": "< 2026.4.10",
        "source_url": "https://example.com/x",
        "severity": "warn",
        "plain_summary": "Privilege escalation via env-var denylist bypass.",
    }])
    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.29", macos="15.4"),
        send=False,
    )
    assert result.alerted == []
    assert result.skipped_not_applicable == 1
    assert result.sent is False


def test_finalize_includes_applicable_finding(tmp_security):
    day = _t().date().isoformat()
    _write_candidates(tmp_security["security"], day, [{
        "cve_id": "CVE-2026-43584",
        "affects_package": "openclaw",
        "affects_range": "< 2026.4.10",
        "source_url": "https://example.com/x",
        "severity": "warn",
        "plain_summary": "Privilege escalation via env-var denylist bypass.",
    }])
    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
        send=False,
    )
    assert len(result.alerted) == 1
    assert result.alerted[0].cve_id == "CVE-2026-43584"


def test_finalize_respects_muted_baseline(tmp_security):
    day = _t().date().isoformat()
    _write_candidates(tmp_security["security"], day, [{
        "cve_id": "CVE-MUTED",
        "affects_package": "openclaw",
        "affects_range": "< 2026.4.10",
        "plain_summary": "x",
    }])
    F.BASELINE_PATH.write_text(json.dumps({
        "CVE-MUTED": {"muted": True, "date": "2026-05-01"},
    }))
    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
        send=False,
    )
    assert result.alerted == []
    assert result.skipped_muted == 1


def test_finalize_idempotency_short_circuits_on_existing_log(tmp_security):
    """If today's log already has a scan entry, finalize returns
    immediately without dispatching. Guards against the duplicate-message
    issue."""
    day = _t().date().isoformat()
    log_path = tmp_security["security"] / f"{day}-cve-scan.md"
    log_path.write_text(
        f"# Security Scan Log — {day}\n\n## 09:10 Security Scan\n- All clear\n"
    )
    _write_candidates(tmp_security["security"], day, [{
        "cve_id": "CVE-X", "affects_package": "openclaw",
        "affects_range": "< 1.0", "plain_summary": "y",
    }])
    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
        send=False,
    )
    assert result.dedup_short_circuit is True
    assert result.alerted == []  # never even processed


def test_finalize_handles_missing_candidates_file(tmp_security):
    """No candidates file → no findings, log entry still written
    (audit trail), no message sent."""
    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
        send=False,
    )
    assert result.alerted == []
    assert result.sent is False
    # Log entry was written.
    day = _t().date().isoformat()
    log = tmp_security["security"] / f"{day}-cve-scan.md"
    assert log.exists()
    assert "All clear" in log.read_text()


def test_finalize_sends_telegram_when_credentials_present(tmp_security, monkeypatch):
    """When candidates land, applicability passes, and creds are on
    disk, the finalizer POSTs to Telegram. We monkeypatch send_telegram
    to avoid actual HTTP."""
    day = _t().date().isoformat()
    _write_candidates(tmp_security["security"], day, [{
        "cve_id": "CVE-2026-99999",
        "affects_package": "openclaw",
        "affects_range": "< 9999.99.99",
        "source_url": "https://example.com/x",
        "severity": "warn",
        "plain_summary": "Test vulnerability.",
    }])
    F.TOKEN_PATH.write_text("fake-token")
    F.CHAT_ID_PATH.write_text("12345")

    captured: dict = {}

    def fake_send(message, *, token, chat_id):
        captured["message"] = message
        captured["token"] = token
        captured["chat_id"] = chat_id
        return True

    monkeypatch.setattr(F, "send_telegram", fake_send)

    result = F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
    )
    assert result.sent is True
    assert "CVE-2026-99999" in captured["message"]
    assert captured["token"] == "fake-token"
    assert captured["chat_id"] == "12345"


def test_finalize_writes_log_entry_with_counts(tmp_security):
    day = _t().date().isoformat()
    _write_candidates(tmp_security["security"], day, [
        {"cve_id": "CVE-A", "affects_package": "openclaw",
         "affects_range": "< 9999.99.99", "plain_summary": "applicable"},
        {"cve_id": "CVE-B", "affects_package": "openclaw",
         "affects_range": "< 0.0.1", "plain_summary": "not applicable"},
        {"cve_id": "CVE-MUTED", "affects_package": "openclaw",
         "affects_range": "< 9999.99.99", "plain_summary": "muted"},
    ])
    F.BASELINE_PATH.write_text(json.dumps({"CVE-MUTED": {"muted": True}}))

    F.finalize(
        now=_t(),
        installed_override=F.InstalledVersions(openclaw="2026.4.5", macos="15.4"),
        send=False,
    )
    log = tmp_security["security"] / f"{day}-cve-scan.md"
    text = log.read_text()
    assert "OpenClaw: 2026.4.5" in text
    assert "Alerted: 1" in text
    assert "Skipped (not applicable): 1" in text
    assert "Skipped (muted): 1" in text


# ── read_installed_macos_version platform gate (both profiles) ────────────────
# sw_vers is macOS-only. The finalizer runs as a launchd job on macOS and a
# systemd oneshot on Linux; the read is explicitly gated so (a) the Linux pod
# never shells out to a missing binary and (b) tools/platform-path-lint's
# blind rule sees a platform guard, not just a try/except. These pin both
# branches deterministically regardless of the CI runner's real OS.


def test_read_installed_macos_version_linux_profile_skips_sw_vers(monkeypatch):
    """LINUX profile → returns None WITHOUT invoking sw_vers (the binary is
    absent on a Linux pod; shelling out is the false-CRITICAL class the
    blind lint guards)."""
    import subprocess as _sp

    from platform_profile import LINUX, set_profile

    set_profile(LINUX)
    try:
        def _boom(*a, **k):  # pragma: no cover — must not be reached
            raise AssertionError("sw_vers must not be invoked on a Linux pod")

        monkeypatch.setattr(_sp, "run", _boom)
        assert F.read_installed_macos_version() is None
    finally:
        set_profile(None)


def test_read_installed_macos_version_macos_profile_calls_sw_vers(monkeypatch):
    """MACOS profile → invokes ``/usr/bin/sw_vers -productVersion`` exactly as
    before and returns the trimmed version (macOS path byte-identical)."""
    import subprocess as _sp

    from platform_profile import MACOS, set_profile

    set_profile(MACOS)
    try:
        calls: list[list[str]] = []

        def _fake_run(argv, **kw):
            calls.append(argv)
            return _sp.CompletedProcess(argv, 0, "15.5\n", "")

        monkeypatch.setattr(_sp, "run", _fake_run)
        assert F.read_installed_macos_version() == "15.5"
        assert calls and calls[0] == ["/usr/bin/sw_vers", "-productVersion"]
    finally:
        set_profile(None)
