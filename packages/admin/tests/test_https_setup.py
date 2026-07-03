"""tests/test_https_setup.py — `evolve-admin enable-https` / `disable-https`.

Covers the Phase 4.1.b CLI surface from
``docs/spec-pwa-phase0-https-2026-05-18.md``. All ``tailscale`` and
``requests`` interactions are mocked so the suite runs against pure
Python state — no live daemon, no network.

The fixture ``fake_tailscale`` patches both ``_tailscale_bin`` (so the
binary-discovery probe always succeeds) and ``_run_tailscale`` (so we
can script per-subcommand responses). Tests configure the subcommand
table they need, then assert on the result + the resulting network.json.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def net_path(tmp_path: Path) -> Path:
    """Per-test network.json on disk so save_network's atomic-rewrite path runs."""
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "networkId": "test-net",
        "primary": "team_bot_a",
        "adminBaseUrl": "http://team_bot_a-mini:5050",
    }))
    return path


def _completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_tailscale(monkeypatch):
    """Patch tailscale subprocess calls with a scripted-response harness.

    Returns a controller object: tests call ``controller.set("version",
    stdout=..., rc=...)`` to script each tailscale subcommand. The
    controller's ``calls`` list captures invocations for assertions.

    Default responses (overridable):
    - ``version`` → rc=0, stdout="1.66.4\\n  tailscale commit: ...\\n"
    - ``status`` → rc=0, JSON for a Running tailnet at team_bot_a.example.ts.net
    - ``serve status`` → rc=0, JSON showing no serve active
    - ``serve --bg ...`` → rc=0
    - ``serve --https=443 off`` → rc=0
    """

    class Controller:
        def __init__(self) -> None:
            self.responses: dict[str, dict] = {}
            self.calls: list[tuple[str, ...]] = []
            self.serve_active = False  # toggled by `set_serve_active(True)`
            self.auto_flip_on_bg = True

        def set(self, key: str, *, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.responses[key] = {"rc": rc, "stdout": stdout, "stderr": stderr}

        def set_serve_active(self, active: bool) -> None:
            self.serve_active = active

        def _serve_status_payload(self) -> str:
            if not self.serve_active:
                return json.dumps({})
            return json.dumps({
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "team_bot_a.example.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:5050"},
                        },
                    },
                },
            })

        def _default_for(self, args: tuple[str, ...]) -> dict:
            if args == ("version",):
                return {"rc": 0, "stdout": "1.66.4\n  tailscale commit: abcd\n", "stderr": ""}
            if args == ("status", "--json"):
                payload = {
                    "BackendState": "Running",
                    "Self": {"DNSName": "team_bot_a.example.ts.net.", "HostName": "team_bot_a"},
                }
                return {"rc": 0, "stdout": json.dumps(payload), "stderr": ""}
            if args == ("serve", "status", "--json"):
                return {"rc": 0, "stdout": self._serve_status_payload(), "stderr": ""}
            return {"rc": 0, "stdout": "", "stderr": ""}

        def respond(self, *args: str) -> subprocess.CompletedProcess:
            key_full = " ".join(args)
            if key_full in self.responses:
                spec = self.responses[key_full]
            elif args and args[0] in self.responses:
                spec = self.responses[args[0]]
            else:
                spec = self._default_for(args)
            result = subprocess.CompletedProcess(
                args=["tailscale", *args],
                returncode=spec["rc"],
                stdout=spec["stdout"],
                stderr=spec["stderr"],
            )
            # Default-flip serve_active on a successful `serve --bg ...`
            # so happy-path tests don't have to choreograph it. Tests
            # that want to model "serve succeeded but daemon didn't bind"
            # set the response explicitly AND skip this auto-flip by
            # setting auto_flip_on_bg=False.
            if (
                self.auto_flip_on_bg
                and result.returncode == 0
                and args[:2] == ("serve", "--bg")
            ):
                self.serve_active = True
            return result

    ctl = Controller()

    def fake_run(*args, **kwargs):
        # args is a single-element tuple containing the list [bin, *subcmd_args]
        argv = args[0] if args else kwargs.get("args", [])
        if not argv or len(argv) < 2:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        ts_args = tuple(argv[1:])
        ctl.calls.append(ts_args)
        return ctl.respond(*ts_args)

    monkeypatch.setattr(
        "evolve_admin.https_setup._tailscale_bin",
        lambda: "/opt/homebrew/bin/tailscale",
    )
    monkeypatch.setattr("evolve_admin.https_setup.subprocess.run", fake_run)

    return ctl


@pytest.fixture
def fake_save_network(monkeypatch):
    """Bypass save_network's sudo /bin/cp fallback in tests.

    The real implementation tries sudo when it can't write the target
    path; in pytest that prompts for a password. Replace it with a
    simple atomic temp+rename that writes wherever the test points.
    """

    def write(data: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    monkeypatch.setattr("evolve_admin.https_setup.save_network", write)
    return write


# ── Pre-flight refusals ────────────────────────────────────────────────────


def test_preflight_refuses_when_tailscale_missing(monkeypatch, net_path: Path):
    from evolve_admin import https_setup

    def boom():
        raise https_setup.TailscaleNotInstalled(
            "Tailscale not installed. Install via 'brew install --cask tailscale' and sign in."
        )

    monkeypatch.setattr("evolve_admin.https_setup._tailscale_bin", boom)
    with pytest.raises(https_setup.TailscaleNotInstalled) as exc_info:
        https_setup.enable_https(network_path=net_path)
    assert "not installed" in str(exc_info.value).lower()


def test_preflight_refuses_on_old_tailscale_version(fake_tailscale, net_path: Path):
    from evolve_admin import https_setup
    fake_tailscale.set("version", rc=0, stdout="1.40.1\n  tailscale commit: abc\n")
    with pytest.raises(https_setup.TailscaleTooOld) as exc_info:
        https_setup.enable_https(network_path=net_path)
    assert "1.40" in str(exc_info.value) or "too old" in str(exc_info.value).lower()
    assert "1.44" in str(exc_info.value) or "v1.44" in str(exc_info.value)


def test_preflight_refuses_when_not_signed_in(fake_tailscale, net_path: Path):
    from evolve_admin import https_setup
    fake_tailscale.set(
        "status --json",
        rc=0,
        stdout=json.dumps({"BackendState": "NeedsLogin"}),
    )
    with pytest.raises(https_setup.TailscaleNotSignedIn) as exc_info:
        https_setup.enable_https(network_path=net_path)
    assert "tailscale up" in str(exc_info.value).lower() or "sign in" in str(exc_info.value).lower()


def test_preflight_defers_when_https_provisioning_disabled(
    fake_tailscale, fake_save_network, net_path: Path
):
    """`tailscale serve` fails with the HTTPS-disabled error → friendly defer."""
    from evolve_admin import https_setup

    fake_tailscale.set(
        "serve --bg --https=443 http://127.0.0.1:5050",
        rc=1,
        stderr=(
            "tailscale serve: HTTPS is not enabled on your tailnet. "
            "Enable it at https://login.tailscale.com/admin/dns"
        ),
    )

    with pytest.raises(https_setup.TailscaleHTTPSNotEnabled) as exc_info:
        https_setup.enable_https(network_path=net_path)
    msg = str(exc_info.value)
    assert "login.tailscale.com/admin/dns" in msg
    # network.json must NOT have been rewritten — serve failed before write
    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "http://team_bot_a-mini:5050"


# ── Happy path ──────────────────────────────────────────────────────────────


def test_happy_path_rewrites_network_json_and_verifies(
    fake_tailscale, fake_save_network, net_path: Path
):
    from evolve_admin import https_setup

    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp) as get:
        result = https_setup.enable_https(network_path=net_path)

    assert result.ok is True
    assert result.changed is True
    assert result.url == "https://team_bot_a.example.ts.net"
    # Health endpoint actually queried
    health_url = get.call_args[0][0]
    assert health_url == "https://team_bot_a.example.ts.net/api/health"
    # No redirects allowed — silent-failure check
    assert get.call_args.kwargs.get("allow_redirects") is False
    assert get.call_args.kwargs.get("verify") is True

    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "https://team_bot_a.example.ts.net"

    # tailscale serve invoked with --bg + 443 + loopback target
    serve_calls = [c for c in fake_tailscale.calls if c and c[0] == "serve" and "--bg" in c]
    assert serve_calls, f"expected `serve --bg ...` call; got {fake_tailscale.calls!r}"
    assert "--https=443" in serve_calls[0]
    assert "http://127.0.0.1:5050" in serve_calls[0]


# ── Verification-failure rollback ───────────────────────────────────────────


def test_verification_failure_triggers_rollback(
    fake_tailscale, fake_save_network, net_path: Path
):
    """200 fetch fails → network.json reverted, serve cleared, error raised."""
    from evolve_admin import https_setup

    fake_resp = MagicMock(status_code=502, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        with pytest.raises(https_setup.VerificationFailed):
            https_setup.enable_https(network_path=net_path)

    # Rollback restored the original adminBaseUrl. (load_network applies
    # pod-block defaults, so a strict byte-compare against the seed text
    # would include those; we assert on the field we actually flipped.)
    restored = json.loads(net_path.read_text())
    assert restored["adminBaseUrl"] == "http://team_bot_a-mini:5050"

    # serve off invoked to clear partial state
    off_calls = [c for c in fake_tailscale.calls
                 if c[:2] == ("serve", "--https=443") and "off" in c]
    assert off_calls, f"expected `serve --https=443 off` rollback; got {fake_tailscale.calls!r}"


def test_verification_redirect_is_treated_as_failure(
    fake_tailscale, fake_save_network, net_path: Path
):
    """A 301 hand-off to plain HTTP must NOT be mistaken for a healthy 200."""
    from evolve_admin import https_setup

    redir = MagicMock(status_code=301, is_redirect=True, is_permanent_redirect=True)
    redir.headers = {"Location": "http://team_bot_a-mini:5050/api/health"}
    with patch("requests.get", return_value=redir):
        with pytest.raises(https_setup.VerificationFailed) as exc_info:
            https_setup.enable_https(network_path=net_path)
    assert "redirect" in str(exc_info.value).lower()


# ── Idempotency ─────────────────────────────────────────────────────────────


def test_enable_https_idempotent_when_already_configured(
    fake_tailscale, fake_save_network, net_path: Path
):
    """Re-run after a successful enable: no serve, no rewrite, exit 0."""
    from evolve_admin import https_setup

    # Pre-state matches what enable would produce
    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "primary": "team_bot_a",
        "adminBaseUrl": "https://team_bot_a.example.ts.net",
    }))
    fake_tailscale.set_serve_active(True)

    with patch("requests.get") as get:
        result = https_setup.enable_https(network_path=net_path)

    assert result.ok is True
    assert result.changed is False
    assert "already enabled" in result.messages[0].lower()
    # No verification fetch needed when state already matches
    assert get.called is False
    # No tailscale `serve --bg` issued
    serve_bg = [c for c in fake_tailscale.calls if "--bg" in c]
    assert not serve_bg


def test_enable_https_recovers_partial_state(
    fake_tailscale, fake_save_network, net_path: Path
):
    """network.json says HTTPS but serve is off — re-run completes the setup."""
    from evolve_admin import https_setup

    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "adminBaseUrl": "https://team_bot_a.example.ts.net",
    }))
    fake_tailscale.set_serve_active(False)

    # serve auto-flips active=True on a successful --bg call (see fixture)

    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        result = https_setup.enable_https(network_path=net_path)

    assert result.changed is True
    serve_bg = [c for c in fake_tailscale.calls if "--bg" in c]
    assert serve_bg, "expected `serve --bg ...` to be issued during partial-state recovery"


# ── disable-https ───────────────────────────────────────────────────────────


def test_disable_https_reverses_cleanly(
    fake_tailscale, fake_save_network, net_path: Path, monkeypatch
):
    from evolve_admin import https_setup

    monkeypatch.setattr("socket.gethostname", lambda: "team_bot_a-mini.local")
    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "adminBaseUrl": "https://team_bot_a.example.ts.net",
        "mcp_bridge": {
            "enabled": True,
            "port": 5051,
            "tailscale_hostname": "team_bot_a.example.ts.net",
            "url": "https://team_bot_a.example.ts.net/sse",
        },
    }))
    fake_tailscale.set_serve_active(True)

    result = https_setup.disable_https(network_path=net_path)

    assert result.ok is True
    assert result.changed is True
    assert result.url == "http://team_bot_a-mini:5050"

    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "http://team_bot_a-mini:5050"
    assert written["mcp_bridge"]["url"] == "http://team_bot_a-mini:5050/sse"

    off_calls = [c for c in fake_tailscale.calls
                 if c[:2] == ("serve", "--https=443") and "off" in c]
    assert off_calls


def test_disable_https_idempotent_on_http_pod(
    fake_tailscale, fake_save_network, net_path: Path, monkeypatch
):
    from evolve_admin import https_setup

    monkeypatch.setattr("socket.gethostname", lambda: "team_bot_a-mini")
    fake_tailscale.set_serve_active(False)

    result = https_setup.disable_https(network_path=net_path)

    assert result.ok is True
    assert result.changed is False
    assert "already disabled" in result.messages[0].lower()


# ── mcp_bridge.url handling ────────────────────────────────────────────────


def test_mcp_bridge_url_flips_to_https_with_no_port(
    fake_tailscale, fake_save_network, net_path: Path
):
    """The :5050 port must be stripped — the spec called this out as a
    silent-failure mode worth guarding against."""
    from evolve_admin import https_setup

    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "adminBaseUrl": "http://team_bot_a-mini:5050",
        "mcp_bridge": {
            "enabled": True,
            "port": 5051,
            "tailscale_hostname": "team_bot_a.example.ts.net",
            "url": "http://team_bot_a-mini:5050/sse",
        },
    }))

    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        result = https_setup.enable_https(network_path=net_path)

    written = json.loads(net_path.read_text())
    assert written["mcp_bridge"]["url"] == "https://team_bot_a.example.ts.net/sse"
    # Critical: NO stale :5050 cling-on
    assert ":5050" not in written["mcp_bridge"]["url"]


def test_mcp_bridge_url_absence_is_tolerated(
    fake_tailscale, fake_save_network, net_path: Path
):
    """No mcp_bridge field at all — must not KeyError."""
    from evolve_admin import https_setup
    # net_path fixture already lacks mcp_bridge
    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        result = https_setup.enable_https(network_path=net_path)
    assert result.ok is True
    written = json.loads(net_path.read_text())
    assert "mcp_bridge" not in written  # didn't materialize an empty one


def test_mcp_bridge_present_without_url_field(
    fake_tailscale, fake_save_network, net_path: Path
):
    """mcp_bridge exists but no url field — must not KeyError; url stays absent."""
    from evolve_admin import https_setup

    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "adminBaseUrl": "http://team_bot_a-mini:5050",
        "mcp_bridge": {"enabled": True, "port": 5051,
                       "tailscale_hostname": "team_bot_a.example.ts.net"},
    }))
    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        result = https_setup.enable_https(network_path=net_path)

    assert result.ok is True
    written = json.loads(net_path.read_text())
    assert "url" not in written.get("mcp_bridge", {})


# ── Silent-failure guards ───────────────────────────────────────────────────


def test_serve_rc_zero_but_not_actually_bound_is_caught(
    fake_tailscale, fake_save_network, net_path: Path
):
    """`tailscale serve` returns 0 but post-check shows no 443 mapping.

    The CLAUDE.md two-pass review pattern called this out as a real
    silent-failure mode. Verify the recheck catches it instead of
    happily proceeding to write network.json.
    """
    from evolve_admin import https_setup

    # serve returns 0 but we suppress the auto-flip, so the post-call
    # status query reports no 443 mapping → recheck must fail.
    fake_tailscale.auto_flip_on_bg = False
    fake_tailscale.set("serve --bg --https=443 http://127.0.0.1:5050",
                       rc=0, stdout="", stderr="")

    with patch("requests.get"):
        with pytest.raises(https_setup.TailscaleServeFailed) as exc_info:
            https_setup.enable_https(network_path=net_path)
    assert "443" in str(exc_info.value) or "127.0.0.1:5050" in str(exc_info.value)

    # network.json must remain untouched
    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "http://team_bot_a-mini:5050"


# ── Tailscale CLI binary resolution (follow-up to #1274) ───────────────────
#
# These tests exercise _resolve_tailscale_cli directly — they do NOT use
# the ``fake_tailscale`` fixture (which short-circuits _tailscale_bin to
# a hardcoded path). Instead they mock ``shutil.which`` + the executable
# probe so the probe order + fallback chain are observable.


@pytest.fixture
def stub_executable(monkeypatch):
    """Patch _is_executable_file to consult a configurable allowlist.

    Tests call ``stub_executable({"/some/path"})`` to declare which
    paths the resolver should treat as a usable executable. Anything
    not in the set returns False.
    """

    def configure(usable: set[str]) -> None:
        from evolve_admin import https_setup
        monkeypatch.setattr(
            https_setup, "_is_executable_file", lambda p: p in usable
        )

    return configure


@pytest.fixture
def stub_which(monkeypatch):
    """Patch shutil.which (as seen by https_setup) with a fixed return."""

    def configure(found: str | None) -> None:
        from evolve_admin import https_setup
        monkeypatch.setattr(
            https_setup.shutil, "which",
            lambda name: found if name == "tailscale" else None,
        )

    return configure


def test_resolver_uses_shutil_which_when_path_has_binary(stub_which, stub_executable):
    """PATH wins: shutil.which result is returned without probing fallbacks."""
    from evolve_admin import https_setup

    stub_which("/some/custom/bin/tailscale")
    stub_executable({"/some/custom/bin/tailscale"})

    cli = https_setup._resolve_tailscale_cli()
    assert cli.path == "/some/custom/bin/tailscale"
    assert cli.hints == []  # No fallback fired → no PATH-symlink hint


def test_resolver_falls_back_to_app_store_when_path_empty(stub_which, stub_executable):
    """which() returns None, App Store binary exists → use it + emit hint."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable({https_setup._TAILSCALE_APP_STORE_PATH})

    cli = https_setup._resolve_tailscale_cli()
    assert cli.path == https_setup._TAILSCALE_APP_STORE_PATH
    # PATH-symlink hint should fire — operator can't run `tailscale`
    # from their own shell otherwise.
    assert len(cli.hints) == 1
    hint = cli.hints[0]
    assert https_setup._TAILSCALE_APP_STORE_PATH in hint
    assert "/usr/local/bin/tailscale" in hint
    assert "ln -s" in hint


def test_resolver_falls_back_to_homebrew_intel(stub_which, stub_executable):
    """Neither PATH nor App Store, /usr/local/bin works → that path wins."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable({"/usr/local/bin/tailscale"})

    cli = https_setup._resolve_tailscale_cli()
    assert cli.path == "/usr/local/bin/tailscale"
    # Homebrew path doesn't trigger the App-Store symlink hint
    assert cli.hints == []


def test_resolver_falls_back_to_homebrew_apple_silicon(stub_which, stub_executable):
    """/opt/homebrew/bin probed last after the Intel path."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable({"/opt/homebrew/bin/tailscale"})

    cli = https_setup._resolve_tailscale_cli()
    assert cli.path == "/opt/homebrew/bin/tailscale"
    assert cli.hints == []


def test_resolver_prefers_path_over_app_store(stub_which, stub_executable):
    """Both PATH and App Store have a binary — PATH wins (no hint)."""
    from evolve_admin import https_setup

    stub_which("/usr/local/bin/tailscale")
    stub_executable({
        "/usr/local/bin/tailscale",
        https_setup._TAILSCALE_APP_STORE_PATH,
    })

    cli = https_setup._resolve_tailscale_cli()
    assert cli.path == "/usr/local/bin/tailscale"
    assert cli.hints == []


def test_resolver_raises_install_message_when_nothing_found(
    monkeypatch, stub_which, stub_executable,
):
    """No PATH binary, no fallback paths, no Tailscale.app — clean refusal."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable(set())
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(https_setup.TailscaleNotInstalled) as exc:
        https_setup._resolve_tailscale_cli()
    msg = str(exc.value)
    assert "Tailscale CLI not found" in msg
    assert "tailscale.com/download/mac" in msg
    assert "sign in" in msg.lower()
    # Negative — we should NOT confuse the operator with the
    # "Tailscale.app exists but binary missing" line.
    assert "Found Tailscale.app" not in msg


def test_resolver_reports_when_app_bundle_present_but_binary_missing(
    monkeypatch, stub_which, stub_executable,
):
    """Edge case: /Applications/Tailscale.app exists but the binary
    inside isn't usable (App Store update transiently clobbered +x,
    or similar). Operator gets a "please report" signal."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable(set())  # nothing usable, including the App Store path

    def fake_exists(self):
        return str(self) == https_setup._TAILSCALE_APP_BUNDLE
    monkeypatch.setattr(Path, "exists", fake_exists)

    with pytest.raises(https_setup.TailscaleNotInstalled) as exc:
        https_setup._resolve_tailscale_cli()
    msg = str(exc.value)
    assert "Found Tailscale.app" in msg
    assert "report" in msg.lower()
    # The install instructions should still be present
    assert "tailscale.com/download/mac" in msg


def test_resolved_path_is_used_in_subprocess_call(
    monkeypatch, stub_which, stub_executable,
):
    """Silent-failure guard: a bare ``tailscale`` must never reach
    subprocess.run. The resolved absolute path is what actually gets
    invoked."""
    from evolve_admin import https_setup

    stub_which(None)
    stub_executable({https_setup._TAILSCALE_APP_STORE_PATH})

    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="1.66.4\n", stderr=""
        )

    monkeypatch.setattr(https_setup.subprocess, "run", fake_run)
    https_setup._run_tailscale("version")

    assert captured, "expected subprocess.run to be invoked"
    argv = captured[0]
    assert argv[0] == https_setup._TAILSCALE_APP_STORE_PATH
    assert argv[0] != "tailscale"  # no bare-name slipped through
    assert argv[1:] == ["version"]


def test_is_executable_file_rejects_directory(tmp_path: Path):
    """Sanity: a directory at the candidate path is not a usable binary
    (Path.is_file() returns False)."""
    from evolve_admin import https_setup

    assert https_setup._is_executable_file(str(tmp_path)) is False


def test_is_executable_file_rejects_nonexecutable(tmp_path: Path):
    """A file that exists but isn't executable is rejected — guards
    against the App-Store-update-clobbered-+x silent-failure mode."""
    from evolve_admin import https_setup
    import os as _os

    f = tmp_path / "tailscale"
    f.write_text("#!/bin/sh\necho 1.66.4\n")
    _os.chmod(f, 0o644)  # no +x

    assert https_setup._is_executable_file(str(f)) is False


def test_is_executable_file_rejects_stale_symlink(tmp_path: Path):
    """A symlink pointing at a deleted target — the kind of state a
    half-uninstalled Homebrew leaves behind — must not register as a
    usable binary."""
    from evolve_admin import https_setup

    target = tmp_path / "gone"
    link = tmp_path / "tailscale"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link.symlink_to(target)
    target.unlink()  # leave the symlink dangling

    assert https_setup._is_executable_file(str(link)) is False


def test_enable_https_surfaces_path_hint_in_messages(
    fake_save_network, net_path, monkeypatch,
):
    """End-to-end: when the App Store fallback fires, the symlink hint
    rides along in HttpsSetupResult.messages so the CLI prints it once
    per run (not once per subprocess call)."""
    from evolve_admin import https_setup

    monkeypatch.setattr(
        https_setup.shutil, "which",
        lambda name: None if name == "tailscale" else shutil_which_real(name),
    )
    monkeypatch.setattr(
        https_setup, "_is_executable_file",
        lambda p: p == https_setup._TAILSCALE_APP_STORE_PATH,
    )

    serve_active = {"v": False}

    def fake_run(argv, **kwargs):
        # Resolver should hand us the App Store path
        assert argv[0] == https_setup._TAILSCALE_APP_STORE_PATH
        sub = tuple(argv[1:])
        if sub == ("version",):
            return _completed(argv, stdout="1.66.4\n  tailscale commit: abc\n")
        if sub == ("status", "--json"):
            return _completed(argv, stdout=json.dumps({
                "BackendState": "Running",
                "Self": {"DNSName": "team_bot_a.example.ts.net.", "HostName": "team_bot_a"},
            }))
        if sub == ("serve", "status", "--json"):
            payload: dict[str, Any] = {}
            if serve_active["v"]:
                payload = {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {"team_bot_a.example.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}},
                    }},
                }
            return _completed(argv, stdout=json.dumps(payload))
        if sub[:2] == ("serve", "--bg"):
            serve_active["v"] = True
            return _completed(argv)
        return _completed(argv)

    monkeypatch.setattr(https_setup.subprocess, "run", fake_run)

    fake_resp = MagicMock(
        status_code=200, is_redirect=False, is_permanent_redirect=False
    )
    with patch("requests.get", return_value=fake_resp):
        result = https_setup.enable_https(network_path=net_path)

    assert result.ok is True
    hint_lines = [m for m in result.messages if "symlink" in m.lower()]
    assert hint_lines, (
        f"expected one symlink hint in messages; got {result.messages!r}"
    )
    # Hint should appear exactly once — even though the resolver is
    # invoked many times during pre-flight + serve + verify.
    assert len(hint_lines) == 1


# Real shutil.which captured at module load so the monkeypatched stub
# above can delegate non-tailscale lookups (e.g. anything the test
# harness itself might call) without recursing.
import shutil as _shutil_real
shutil_which_real = _shutil_real.which


# ── preflight() + enable_https_if_possible() (Phase 4.1.c) ─────────────────


def test_preflight_returns_ready_when_state_healthy(fake_tailscale):
    """All checks pass → READY, empty detail."""
    from evolve_admin import https_setup

    outcome = https_setup.preflight()
    assert outcome.status is https_setup.PreflightResult.READY
    assert outcome.detail == ""


def test_preflight_reports_need_install_when_cli_missing(monkeypatch):
    """No tailscale binary anywhere → NEED_INSTALL with operator-facing detail."""
    from evolve_admin import https_setup

    def boom():
        raise https_setup.TailscaleNotInstalled(
            "Tailscale CLI not found.\nInstall Tailscale: ..."
        )

    monkeypatch.setattr(https_setup, "_resolve_tailscale_cli", boom)
    outcome = https_setup.preflight()
    assert outcome.status is https_setup.PreflightResult.NEED_INSTALL
    assert "Tailscale CLI not found" in outcome.detail


def test_preflight_reports_need_upgrade_on_old_version(fake_tailscale):
    """tailscale present but pre-v1.44 → NEED_UPGRADE."""
    from evolve_admin import https_setup

    fake_tailscale.set("version", rc=0, stdout="1.40.1\n  tailscale commit: x\n")
    outcome = https_setup.preflight()
    assert outcome.status is https_setup.PreflightResult.NEED_UPGRADE
    assert "1.44" in outcome.detail


def test_preflight_reports_need_login_when_not_signed_in(fake_tailscale):
    """BackendState != Running → NEED_LOGIN."""
    from evolve_admin import https_setup

    fake_tailscale.set(
        "status --json",
        rc=0,
        stdout=json.dumps({"BackendState": "NeedsLogin"}),
    )
    outcome = https_setup.preflight()
    assert outcome.status is https_setup.PreflightResult.NEED_LOGIN
    assert "tailscale up" in outcome.detail.lower() or "sign in" in outcome.detail.lower()


def test_preflight_treats_version_command_failure_as_need_install(fake_tailscale):
    """`tailscale version` failing at all reads as broken-install."""
    from evolve_admin import https_setup

    fake_tailscale.set("version", rc=1, stderr="dyld: ...")
    outcome = https_setup.preflight()
    assert outcome.status is https_setup.PreflightResult.NEED_INSTALL


def test_enable_https_if_possible_happy_path(fake_tailscale, fake_save_network, net_path):
    """READY preflight + successful enable → result populated, preflight=READY."""
    from evolve_admin import https_setup

    fake_resp = MagicMock(status_code=200, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        attempt = https_setup.enable_https_if_possible(network_path=net_path)

    assert attempt.preflight is https_setup.PreflightResult.READY
    assert attempt.result is not None
    assert attempt.result.ok is True
    assert attempt.result.url == "https://team_bot_a.example.ts.net"
    assert attempt.error == ""


def test_enable_https_if_possible_idempotent_already_on_https(
    fake_tailscale, fake_save_network, net_path
):
    """Already configured → READY preflight + result.changed=False + no rewrite."""
    from evolve_admin import https_setup

    net_path.write_text(json.dumps({
        "networkId": "test-net",
        "adminBaseUrl": "https://team_bot_a.example.ts.net",
    }))
    fake_tailscale.set_serve_active(True)

    with patch("requests.get") as get:
        attempt = https_setup.enable_https_if_possible(network_path=net_path)

    assert attempt.preflight is https_setup.PreflightResult.READY
    assert attempt.result is not None
    assert attempt.result.changed is False
    # No verification fetch (idempotent path skips it)
    assert get.called is False
    # No duplicate `serve --bg` call — daemon already has the mapping
    serve_bg = [c for c in fake_tailscale.calls if "--bg" in c]
    assert not serve_bg


def test_enable_https_if_possible_skips_on_need_install(monkeypatch, net_path):
    """No Tailscale CLI → return NEED_INSTALL without touching network.json."""
    from evolve_admin import https_setup

    def boom():
        raise https_setup.TailscaleNotInstalled("Tailscale CLI not found. Install ...")
    monkeypatch.setattr(https_setup, "_resolve_tailscale_cli", boom)

    attempt = https_setup.enable_https_if_possible(network_path=net_path)
    assert attempt.preflight is https_setup.PreflightResult.NEED_INSTALL
    assert attempt.result is None
    assert "Tailscale CLI not found" in attempt.error
    # network.json untouched
    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "http://team_bot_a-mini:5050"


def test_enable_https_if_possible_skips_on_need_login(fake_tailscale, net_path):
    """Backend not Running → NEED_LOGIN, no apply attempted."""
    from evolve_admin import https_setup

    fake_tailscale.set(
        "status --json",
        rc=0,
        stdout=json.dumps({"BackendState": "NeedsLogin"}),
    )
    attempt = https_setup.enable_https_if_possible(network_path=net_path)
    assert attempt.preflight is https_setup.PreflightResult.NEED_LOGIN
    assert attempt.result is None
    # No serve invocation
    assert not any("--bg" in c for c in fake_tailscale.calls)


def test_enable_https_if_possible_skips_on_need_upgrade(fake_tailscale, net_path):
    """tailscale too old → NEED_UPGRADE, no apply attempted."""
    from evolve_admin import https_setup

    fake_tailscale.set("version", rc=0, stdout="1.40.1\n")
    attempt = https_setup.enable_https_if_possible(network_path=net_path)
    assert attempt.preflight is https_setup.PreflightResult.NEED_UPGRADE
    assert attempt.result is None


def test_enable_https_if_possible_defers_on_admin_toggle_off(
    fake_tailscale, fake_save_network, net_path
):
    """Preflight READY but `tailscale serve` rejects with the
    HTTPS-disabled stderr → NEED_TOGGLE; no network.json write."""
    from evolve_admin import https_setup

    fake_tailscale.set(
        "serve --bg --https=443 http://127.0.0.1:5050",
        rc=1,
        stderr=(
            "tailscale serve: HTTPS is not enabled on your tailnet. "
            "Enable it at https://login.tailscale.com/admin/dns"
        ),
    )
    attempt = https_setup.enable_https_if_possible(network_path=net_path)

    assert attempt.preflight is https_setup.PreflightResult.NEED_TOGGLE
    assert attempt.result is None
    assert "login.tailscale.com/admin/dns" in attempt.error
    # network.json must NOT have been rewritten — serve failed before write
    written = json.loads(net_path.read_text())
    assert written["adminBaseUrl"] == "http://team_bot_a-mini:5050"


def test_enable_https_if_possible_reports_failed_attempt_with_ready_preflight(
    fake_tailscale, fake_save_network, net_path
):
    """Preflight READY but verification fails mid-flow → result=None,
    preflight=READY, error populated, network.json rolled back."""
    from evolve_admin import https_setup

    fake_resp = MagicMock(status_code=502, is_redirect=False, is_permanent_redirect=False)
    with patch("requests.get", return_value=fake_resp):
        attempt = https_setup.enable_https_if_possible(network_path=net_path)

    # Preflight passed; this is "failed mid-flow", distinct from skipped
    assert attempt.preflight is https_setup.PreflightResult.READY
    assert attempt.result is None
    assert attempt.error  # operator-facing reason populated
    # Rollback restored the original adminBaseUrl
    restored = json.loads(net_path.read_text())
    assert restored["adminBaseUrl"] == "http://team_bot_a-mini:5050"
