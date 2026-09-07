"""Tests for ``evolve-admin handover`` CLI subcommand.

V2.4-5. Covers the three main UX shapes:
  • First call creates a token + prints a URL.
  • Second call for the same bot returns the **same** token (idempotent).
  • ``--rotate`` replaces the existing token.
  • Unregistered bot exits non-zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (str(_ADMIN), str(_ANALYZER)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def cli_with_pod(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import config as _cfg

    network = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["diana_personal", "admin_bot"],
        "bots": {
            "diana_personal": {"role": "member", "port": 19002, "user": "diana"},
            "admin_bot": {"role": "primary", "port": 19000, "user": "evolve"},
        },
        "pod": {},
    }
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _save(data, path=None):
        Path(path or network_path).write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_cfg, "save_network", _save)
    return CliRunner(), tmp_path, network_path


def _run(runner, network_path, *args):
    from evolve_admin.cli import main
    return runner.invoke(main, ["--network", str(network_path), *args])


def test_handover_creates_token_and_prints_url(cli_with_pod):
    runner, tmp_path, network_path = cli_with_pod
    r = _run(runner, network_path, "handover", "diana_personal",
             "-m", "Hi Diana — your assistant is ready.")
    assert r.exit_code == 0, r.output
    assert "/handover/" in r.output
    # One token file should now exist
    tokens = list((tmp_path / "shared" / "handover-tokens").glob("*.json"))
    assert len(tokens) == 1
    rec = json.loads(tokens[0].read_text())
    assert rec["bot_id"] == "diana_personal"
    assert rec["message"] == "Hi Diana — your assistant is ready."
    assert rec["audience"] == "personal_bot_user"
    assert rec["claimed_at"] is None
    assert len(rec["token"]) == 32


def test_handover_idempotent_returns_same_token(cli_with_pod):
    runner, tmp_path, network_path = cli_with_pod
    r1 = _run(runner, network_path, "handover", "diana_personal")
    assert r1.exit_code == 0, r1.output
    r2 = _run(runner, network_path, "handover", "diana_personal")
    assert r2.exit_code == 0, r2.output
    # Same token file, no proliferation
    tokens = list((tmp_path / "shared" / "handover-tokens").glob("*.json"))
    assert len(tokens) == 1
    # Second call surfaces the existing one, not a fresh one
    assert "Existing" in r2.output or "Existing handover" in r2.output


def test_handover_rotate_replaces_token(cli_with_pod):
    runner, tmp_path, network_path = cli_with_pod
    r1 = _run(runner, network_path, "handover", "diana_personal")
    assert r1.exit_code == 0
    first_token = json.loads(
        next((tmp_path / "shared" / "handover-tokens").glob("*.json")).read_text()
    )["token"]

    r2 = _run(runner, network_path, "handover", "diana_personal", "--rotate")
    assert r2.exit_code == 0, r2.output
    tokens = list((tmp_path / "shared" / "handover-tokens").glob("*.json"))
    assert len(tokens) == 1  # old one deleted
    new_token = json.loads(tokens[0].read_text())["token"]
    assert new_token != first_token


def test_handover_rejects_unknown_bot(cli_with_pod):
    runner, _, network_path = cli_with_pod
    r = _run(runner, network_path, "handover", "ghost_bot")
    assert r.exit_code != 0
    assert "isn't registered" in r.output or "not registered" in r.output


def test_handover_custom_expiry(cli_with_pod):
    runner, tmp_path, network_path = cli_with_pod
    r = _run(runner, network_path, "handover", "diana_personal",
             "--expires-in", "3")
    assert r.exit_code == 0, r.output
    rec = json.loads(
        next((tmp_path / "shared" / "handover-tokens").glob("*.json")).read_text()
    )
    # Created and expires_at exist; expires_at is 3 days after created_at
    from datetime import datetime
    created = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
    delta_days = (expires - created).days
    assert delta_days == 3


def test_handover_url_uses_pod_host(cli_with_pod):
    """The URL should incorporate either localhost (default) or a configured
    pod.public_host."""
    runner, tmp_path, network_path = cli_with_pod
    # Add a public_host so we can verify it's picked up
    net = json.loads(network_path.read_text())
    net["pod"]["public_host"] = "diana.example.com"
    network_path.write_text(json.dumps(net))

    r = _run(runner, network_path, "handover", "diana_personal", "--rotate")
    assert r.exit_code == 0
    assert "diana.example.com" in r.output


# ── scheme preservation (PR #1259 / spec-pwa-phase0-https §3.3) ────────────
#
# build_handover_url must propagate the scheme from adminBaseUrl rather than
# guessing it from host shape — the old "https if . and no :" heuristic
# silently downgraded explicit ``https://pod.local:5050`` to http.


def test_build_handover_url_preserves_https_from_admin_base_url(monkeypatch):
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "https://pod.local:5050"}
    url = handover.build_handover_url(network, "abc123")
    assert url == "https://pod.local:5050/handover/abc123"


def test_build_handover_url_preserves_http_from_admin_base_url(monkeypatch):
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "http://pod:5050"}
    url = handover.build_handover_url(network, "def456")
    assert url == "http://pod:5050/handover/def456"


def test_build_handover_url_preserves_https_for_tailnet_no_port(monkeypatch):
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "https://pod.tail-net.ts.net"}
    url = handover.build_handover_url(network, "ghi789")
    assert url == "https://pod.tail-net.ts.net/handover/ghi789"


def test_build_handover_url_bare_override_inherits_https_scheme(monkeypatch):
    """Bare-hostname ``pod.public_host`` inherits scheme from adminBaseUrl
    rather than reapplying the host-shape heuristic."""
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "https://pod.local:5050",
        "pod": {"public_host": "override.example.com"},
    }
    url = handover.build_handover_url(network, "jkl000")
    assert url == "https://override.example.com/handover/jkl000"


def test_build_handover_url_bare_tunnel_inherits_https_scheme(monkeypatch):
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "https://pod.local:5050",
        "tunnel": {"remote_host": "tunnel.example.com"},
    }
    url = handover.build_handover_url(network, "mno111")
    assert url == "https://tunnel.example.com/handover/mno111"


def test_build_handover_url_full_url_override_keeps_own_scheme(monkeypatch):
    """An override with its own scheme keeps it regardless of adminBaseUrl."""
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "http://pod:5050",
        "pod": {"public_host": "https://override.example.com"},
    }
    url = handover.build_handover_url(network, "pqr222")
    assert url == "https://override.example.com/handover/pqr222"


def test_build_handover_url_localhost_fallback_when_nothing_configured(monkeypatch):
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "")
    url = handover.build_handover_url({}, "stu333")
    assert url == "http://localhost:5050/handover/stu333"


def test_pod_host_returns_tuple(monkeypatch):
    """Smoke test the signature change — pod_host() returns (scheme, host)."""
    from evolve_admin import handover
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    result = handover.pod_host({"adminBaseUrl": "https://pod.local:5050"})
    assert result == ("https", "pod.local:5050")
