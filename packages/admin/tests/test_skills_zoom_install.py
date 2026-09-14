"""Tests for evolve_admin.skills.zoom_install.

Pure-helpers tested without subprocess or network: status resolver,
build_install_plan, set_oauth_client / complete_oauth (with stub shim),
set_s2s_credentials, enable_in_oc_config, revoke, _build_mcp_server_block,
access panel honesty, registry entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evolve_admin.skills import zoom_install as zi


# ── Helpers / fakes ────────────────────────────────────────────────────────


class FakeKeystore:
    """In-memory keystore with read/write/delete closures."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(initial or {})

    def read(self, key: str) -> str | None:
        return self.store.get(key)

    def write(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        if key in self.store:
            del self.store[key]
            return True
        return False


class FakeOcConfig:
    """In-memory openclaw.json store keyed by bot id."""

    def __init__(self, initial: dict[str, dict] | None = None) -> None:
        self.configs: dict[str, dict] = {k: dict(v) for k, v in (initial or {}).items()}

    def read(self, bot_id: str) -> dict | None:
        return json.loads(json.dumps(self.configs.get(bot_id, {})))  # deep copy

    def write(self, bot_id: str, config: dict) -> None:
        self.configs[bot_id] = json.loads(json.dumps(config))


def fake_bot_home(tmp_path: Path) -> "Callable[[str], str]":  # type: ignore[name-defined]
    def _bot_home_for(bot_id: str) -> str:
        return str(tmp_path / bot_id)
    return _bot_home_for


@pytest.fixture
def keystore() -> FakeKeystore:
    return FakeKeystore()


@pytest.fixture
def oc_config() -> FakeOcConfig:
    return FakeOcConfig({"atlas": {}})


@pytest.fixture
def bot_home(tmp_path: Path):  # type: ignore[no-untyped-def]
    return fake_bot_home(tmp_path)


# ── Authorize URL ──────────────────────────────────────────────────────────


class TestBuildAuthorizeUrl:
    def test_url_is_zoom_anchored(self) -> None:
        url = zi.build_authorize_url("cid", "https://example.test/cb")
        assert url.startswith("https://zoom.us/oauth/authorize?")

    def test_includes_required_params(self) -> None:
        url = zi.build_authorize_url(
            "cid_abc", "https://example.test/cb", scopes=("user:read:user",), state="s1"
        )
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == ["cid_abc"]
        assert qs["redirect_uri"] == ["https://example.test/cb"]
        assert qs["scope"] == ["user:read:user"]
        assert qs["state"] == ["s1"]


# ── set_oauth_client ───────────────────────────────────────────────────────


class TestSetOauthClient:
    def test_happy_path_writes_four_slots(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        ok, err = zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        assert (ok, err) == (True, None)
        assert keystore.read(zi.keystore_slot_oauth_client_id_for("atlas")) == "cid"
        assert keystore.read(zi.keystore_slot_oauth_client_secret_for("atlas")) == "secret"
        assert (
            keystore.read(zi.keystore_slot_oauth_redirect_url_for("atlas"))
            == "https://example.test/cb"
        )
        assert keystore.read(zi.keystore_slot_credentials_dir_for("atlas")).endswith(
            "/atlas/.openclaw/zoom"
        )

    def test_empty_client_id_rejected(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        ok, err = zi.set_oauth_client(
            "atlas",
            "",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        assert ok is False
        assert err == "invalid_client_id"

    def test_redirect_url_must_be_http_or_https(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        ok, err = zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "ftp://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        assert ok is False
        assert err == "invalid_redirect_url"


# ── complete_oauth ─────────────────────────────────────────────────────────


class _StubShim:
    def __init__(self, returncode: int = 0, stderr: str = "", stdout: str = "") -> None:
        self._rc = returncode
        self._stderr = stderr
        self._stdout = stdout
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(
        self, bot_id: str, argv: list[str], env: dict[str, str]
    ) -> zi.ShimResult:
        self.calls.append((bot_id, list(argv), dict(env)))
        return zi.ShimResult(returncode=self._rc, stdout=self._stdout, stderr=self._stderr)


class TestCompleteOauth:
    def _prep_keystore(self, keystore: FakeKeystore, bot_home) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )

    def test_missing_oauth_client_returns_error(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        shim = _StubShim()
        ok, err = zi.complete_oauth(
            "atlas",
            "abc",
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            run_shim=shim,
        )
        assert ok is False
        assert err == "oauth_client_missing"
        assert shim.calls == []  # never invoked

    def test_happy_path_invokes_shim_with_env(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        self._prep_keystore(keystore, bot_home)
        shim = _StubShim(returncode=0)
        ok, err = zi.complete_oauth(
            "atlas",
            "code_abc",
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            run_shim=shim,
        )
        assert (ok, err) == (True, None)
        assert len(shim.calls) == 1
        call_bot, call_argv, call_env = shim.calls[0]
        assert call_bot == "atlas"
        assert call_argv == ["login", "--code", "code_abc"]
        assert call_env["ZOOM_OAUTH_CLIENT_ID"] == "cid"
        assert call_env["ZOOM_OAUTH_CLIENT_SECRET"] == "secret"
        assert call_env["ZOOM_OAUTH_REDIRECT_URL"] == "https://example.test/cb"
        assert call_env["ZOOM_CREDENTIALS_DIR"].endswith("/.openclaw/zoom")

    def test_shim_rejects_code_maps_to_invalid_code(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        self._prep_keystore(keystore, bot_home)
        shim = _StubShim(
            returncode=1, stderr="[error] Zoom rejected the code: invalid_grant"
        )
        ok, err = zi.complete_oauth(
            "atlas",
            "code_abc",
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            run_shim=shim,
        )
        assert ok is False
        assert err == "invalid_code"

    def test_shim_generic_failure_maps_to_shim_failed(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        self._prep_keystore(keystore, bot_home)
        shim = _StubShim(returncode=2, stderr="some unrelated error")
        ok, err = zi.complete_oauth(
            "atlas",
            "code_abc",
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            run_shim=shim,
        )
        assert ok is False
        assert err == "shim_failed"


# ── set_s2s_credentials ────────────────────────────────────────────────────


class TestSetS2sCredentials:
    def test_happy_path_persists(self, keystore: FakeKeystore) -> None:
        ok, err = zi.set_s2s_credentials(
            "atlas",
            "scid",
            "ssec",
            "sacc",
            write_keystore=keystore.write,
        )
        assert (ok, err) == (True, None)
        assert keystore.read(zi.keystore_slot_s2s_client_id_for("atlas")) == "scid"
        assert keystore.read(zi.keystore_slot_s2s_client_secret_for("atlas")) == "ssec"
        assert keystore.read(zi.keystore_slot_s2s_account_id_for("atlas")) == "sacc"

    def test_missing_field_rejected(self, keystore: FakeKeystore) -> None:
        ok, err = zi.set_s2s_credentials(
            "atlas", "scid", "", "sacc", write_keystore=keystore.write
        )
        assert ok is False
        assert err == "missing_field"

    def test_validator_failure_blocks_persist(self, keystore: FakeKeystore) -> None:
        def validate_failure(cid: str, sec: str, acc: str) -> tuple[bool, str | None]:
            return False, "invalid_client_credentials"

        ok, err = zi.set_s2s_credentials(
            "atlas",
            "scid",
            "ssec",
            "sacc",
            write_keystore=keystore.write,
            mint_test_token=validate_failure,
        )
        assert ok is False
        assert err == "invalid_client_credentials"
        # Nothing should have been written.
        assert keystore.read(zi.keystore_slot_s2s_client_id_for("atlas")) is None

    def test_validator_success_persists(self, keystore: FakeKeystore) -> None:
        ok, err = zi.set_s2s_credentials(
            "atlas",
            "scid",
            "ssec",
            "sacc",
            write_keystore=keystore.write,
            mint_test_token=lambda cid, sec, acc: (True, None),
        )
        assert (ok, err) == (True, None)


# ── enable_in_oc_config ────────────────────────────────────────────────────


class TestEnableInOcConfig:
    def test_writes_mcp_server_block_with_read_only_env(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        # Prep: only General App slots.
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        ok, err = zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        assert (ok, err) == (True, None)
        block = oc_config.configs["atlas"]["mcp"]["servers"]["zoom"]
        assert block["transport"] == "stdio"
        assert block["command"] == "uvx"
        assert block["args"] == ["evolve-zoom-mcp"]
        env = block["env_bindings"]
        # Read-side env bindings present.
        assert env["ZOOM_OAUTH_CLIENT_ID"].startswith("keystore:")
        assert env["ZOOM_CREDENTIALS_DIR"].startswith("keystore:")
        # S2S env bindings absent.
        assert "ZOOM_S2S_CLIENT_ID" not in env

    def test_includes_s2s_when_present(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        zi.set_s2s_credentials(
            "atlas", "scid", "ssec", "sacc", write_keystore=keystore.write
        )
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        env = oc_config.configs["atlas"]["mcp"]["servers"]["zoom"]["env_bindings"]
        assert env["ZOOM_S2S_CLIENT_ID"].startswith("keystore:")
        assert env["ZOOM_S2S_ACCOUNT_ID"].startswith("keystore:")

    def test_idempotent_rerun_layers_in_s2s(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        # First run — read-only.
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        env_first = oc_config.configs["atlas"]["mcp"]["servers"]["zoom"]["env_bindings"]
        assert "ZOOM_S2S_CLIENT_ID" not in env_first
        # Add S2S, re-run.
        zi.set_s2s_credentials(
            "atlas", "scid", "ssec", "sacc", write_keystore=keystore.write
        )
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        env_second = oc_config.configs["atlas"]["mcp"]["servers"]["zoom"]["env_bindings"]
        assert "ZOOM_S2S_CLIENT_ID" in env_second


# ── resolve_status ─────────────────────────────────────────────────────────


class TestResolveStatus:
    def test_not_configured_when_no_keystore(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
        )
        assert status.status == "not_configured"

    def test_oauth_pending_when_keystore_but_no_credentials_json(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
        )
        assert status.status == "oauth_pending"
        assert status.has_user_oauth is False

    def _make_credentials_json(
        self, bot_home, bot_id: str, email: str = "atlas-zoom@example.test"
    ) -> Path:
        creds_dir = Path(zi.credentials_dir_for(bot_home(bot_id)))
        creds_dir.mkdir(parents=True, exist_ok=True)
        creds_file = creds_dir / "credentials.json"
        creds_file.write_text(
            json.dumps({"refresh_token": "rt", "user_email": email})
        )
        return creds_file

    def test_mcp_not_installed_when_creds_present_no_mcp_server(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        self._make_credentials_json(bot_home, "atlas")
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
        )
        assert status.status == "mcp_not_installed"
        assert status.has_user_oauth is True

    def test_read_only_configured_when_mcp_wired_no_s2s_no_probe(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        self._make_credentials_json(bot_home, "atlas")
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
        )
        assert status.status == "read_only_configured"
        assert status.user_oauth_email == "atlas-zoom@example.test"

    def test_active_when_everything_wired_with_probe(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        zi.set_s2s_credentials(
            "atlas", "scid", "ssec", "sacc", write_keystore=keystore.write
        )
        self._make_credentials_json(bot_home, "atlas")
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            probe_shim_tools_list=lambda bot_id: True,
        )
        assert status.status == "active"
        assert status.has_s2s is True
        assert status.s2s_account_id == "sacc"

    def test_mcp_unreachable_when_probe_returns_false(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        self._make_credentials_json(bot_home, "atlas")
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            probe_shim_tools_list=lambda bot_id: False,
        )
        assert status.status == "mcp_unreachable"
        assert status.error == "probe_failed"

    def test_never_active_when_probe_fails_or_creds_missing(
        self, oc_config: FakeOcConfig, keystore: FakeKeystore, bot_home
    ) -> None:
        """Negative test required by audit doc F3 — never lie about active."""
        # Keystore but no credentials.json → never active.
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        status = zi.resolve_status(
            "atlas",
            read_oc_config=oc_config.read,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
            probe_shim_tools_list=lambda bot_id: True,
        )
        assert status.status != "active"

    def test_unknown_when_oc_config_unreadable(
        self, keystore: FakeKeystore, bot_home
    ) -> None:
        oc = FakeOcConfig({})  # bot has no entry → read returns None
        # Actually FakeOcConfig.read returns {} for unknown bots; explicitly stub.
        def read_none(bot_id: str) -> dict | None:
            return None

        def write_noop(bot_id: str, cfg: dict) -> None:
            pass

        status = zi.resolve_status(
            "atlas",
            read_oc_config=read_none,
            read_keystore=keystore.read,
            bot_home_for=bot_home,
        )
        assert status.status == "unknown"
        assert status.error == "oc_read_failed"


# ── build_install_plan ─────────────────────────────────────────────────────


class TestBuildInstallPlan:
    def test_active_has_no_steps(self) -> None:
        plan = zi.build_install_plan(zi.InstallStatus("atlas", "active"))
        assert plan == []

    def test_not_configured_has_three_steps(self) -> None:
        plan = zi.build_install_plan(zi.InstallStatus("atlas", "not_configured"))
        assert [s.id for s in plan] == [
            "set_oauth_client",
            "complete_oauth",
            "enable_in_oc_config",
        ]

    def test_oauth_pending_has_two_steps(self) -> None:
        plan = zi.build_install_plan(zi.InstallStatus("atlas", "oauth_pending"))
        assert [s.id for s in plan] == ["complete_oauth", "enable_in_oc_config"]

    def test_mcp_not_installed_has_one_step(self) -> None:
        plan = zi.build_install_plan(zi.InstallStatus("atlas", "mcp_not_installed"))
        assert [s.id for s in plan] == ["enable_in_oc_config"]

    def test_read_only_offers_optional_s2s(self) -> None:
        plan = zi.build_install_plan(zi.InstallStatus("atlas", "read_only_configured"))
        assert [s.id for s in plan] == ["set_s2s_credentials"]
        assert "Optional" in plan[0].label


# ── revoke ─────────────────────────────────────────────────────────────────


class TestRevoke:
    def _full_install(
        self, keystore: FakeKeystore, oc_config: FakeOcConfig, bot_home
    ) -> None:
        zi.set_oauth_client(
            "atlas",
            "cid",
            "secret",
            "https://example.test/cb",
            write_keystore=keystore.write,
            bot_home_for=bot_home,
        )
        zi.set_s2s_credentials(
            "atlas", "scid", "ssec", "sacc", write_keystore=keystore.write
        )
        # Create the credentials.json file.
        creds_dir = Path(zi.credentials_dir_for(bot_home("atlas")))
        creds_dir.mkdir(parents=True, exist_ok=True)
        (creds_dir / "credentials.json").write_text(json.dumps({"refresh_token": "rt"}))
        zi.enable_in_oc_config(
            "atlas",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            read_keystore=keystore.read,
        )

    def test_revoke_all_clears_everything(
        self, keystore: FakeKeystore, oc_config: FakeOcConfig, bot_home
    ) -> None:
        self._full_install(keystore, oc_config, bot_home)
        ok, err = zi.revoke(
            "atlas",
            scope="all",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            delete_keystore=keystore.delete,
            bot_home_for=bot_home,
            read_keystore=keystore.read,
        )
        assert (ok, err) == (True, None)
        # Keystore wiped.
        for key in [
            zi.keystore_slot_oauth_client_id_for("atlas"),
            zi.keystore_slot_oauth_client_secret_for("atlas"),
            zi.keystore_slot_oauth_redirect_url_for("atlas"),
            zi.keystore_slot_credentials_dir_for("atlas"),
            zi.keystore_slot_s2s_client_id_for("atlas"),
            zi.keystore_slot_s2s_client_secret_for("atlas"),
            zi.keystore_slot_s2s_account_id_for("atlas"),
        ]:
            assert keystore.read(key) is None
        # credentials.json deleted.
        assert not (Path(zi.credentials_dir_for(bot_home("atlas"))) / "credentials.json").exists()
        # mcp.servers.zoom removed.
        assert "zoom" not in (
            oc_config.configs["atlas"].get("mcp", {}).get("servers", {})
        )

    def test_revoke_write_only_keeps_oauth(
        self, keystore: FakeKeystore, oc_config: FakeOcConfig, bot_home
    ) -> None:
        self._full_install(keystore, oc_config, bot_home)
        zi.revoke(
            "atlas",
            scope="write",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            delete_keystore=keystore.delete,
            bot_home_for=bot_home,
            read_keystore=keystore.read,
        )
        # OAuth keystore intact.
        assert keystore.read(zi.keystore_slot_oauth_client_id_for("atlas")) == "cid"
        # S2S keystore gone.
        assert keystore.read(zi.keystore_slot_s2s_client_id_for("atlas")) is None
        # mcp.servers.zoom still present, but without S2S env_bindings.
        env = oc_config.configs["atlas"]["mcp"]["servers"]["zoom"]["env_bindings"]
        assert "ZOOM_S2S_CLIENT_ID" not in env
        assert "ZOOM_OAUTH_CLIENT_ID" in env

    def test_revoke_read_only_keeps_s2s(
        self, keystore: FakeKeystore, oc_config: FakeOcConfig, bot_home
    ) -> None:
        self._full_install(keystore, oc_config, bot_home)
        zi.revoke(
            "atlas",
            scope="read",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            delete_keystore=keystore.delete,
            bot_home_for=bot_home,
            read_keystore=keystore.read,
        )
        # OAuth keystore + credentials.json gone.
        assert keystore.read(zi.keystore_slot_oauth_client_id_for("atlas")) is None
        assert not (Path(zi.credentials_dir_for(bot_home("atlas"))) / "credentials.json").exists()
        # S2S keystore intact.
        assert keystore.read(zi.keystore_slot_s2s_client_id_for("atlas")) == "scid"

    def test_invalid_scope_rejected(
        self, keystore: FakeKeystore, oc_config: FakeOcConfig, bot_home
    ) -> None:
        ok, err = zi.revoke(
            "atlas",
            scope="bogus",
            read_oc_config=oc_config.read,
            write_oc_config=oc_config.write,
            delete_keystore=keystore.delete,
            bot_home_for=bot_home,
        )
        assert ok is False
        assert err == "invalid_scope"


# ── Access panel / registry ────────────────────────────────────────────────


class TestAccessPanel:
    def test_no_jargon_in_will_wont_summary(self) -> None:
        forbidden = {"oauth", "bearer", "token_endpoint", "client_secret", "refresh_token"}
        text = (
            zi.ZOOM_ACCESS_PANEL["summary"]
            + " "
            + " ".join(zi.ZOOM_ACCESS_PANEL["will"])
            + " "
            + " ".join(zi.ZOOM_ACCESS_PANEL["wont"])
        ).lower()
        for word in forbidden:
            assert word not in text, f"jargon leaked: {word!r}"

    def test_panel_mentions_write_capability_conditionally(self) -> None:
        # The "create new Zoom meetings" entry is gated on operator setup.
        wills = zi.ZOOM_ACCESS_PANEL["will"]
        assert any("if you set up meeting creation" in w for w in wills)

    def test_registry_entry_shape(self) -> None:
        entry = zi.SKILL_REGISTRY_ENTRY
        assert entry["id"] == "zoom"
        assert entry["display_name"] == "Zoom"
        assert "access_panel" in entry


# ── Catalog entry sanity ────────────────────────────────────────────────────


class TestCatalogEntry:
    def test_catalog_entry_exists_with_right_shape(self) -> None:
        # mcp_admin lives in packages/analyzer; not on the admin venv's
        # sys.path by default. Add it before importing so this test runs
        # locally and in CI.
        import sys

        from pathlib import Path as _Path

        # parents[0]=tests, [1]=admin, [2]=packages → packages/analyzer
        analyzer_dir = _Path(__file__).resolve().parents[2] / "analyzer"
        added = False
        if str(analyzer_dir) not in sys.path:
            sys.path.insert(0, str(analyzer_dir))
            added = True
        try:
            from mcp_admin.catalog import default_entries  # type: ignore[import-not-found]
        finally:
            if added:
                sys.path.remove(str(analyzer_dir))

        entries = {e.id: e for e in default_entries()}
        zoom = entries.get("zoom")
        assert zoom is not None
        assert zoom.transport == "stdio"
        assert zoom.command == "uvx"
        assert zoom.args == ["evolve-zoom-mcp"]
        assert "create_meeting" in zoom.advertised_tools
        assert "search_meetings" in zoom.advertised_tools
        assert zoom.vetting_status == "candidate"
