"""Tests for zoom_s2s.py."""

from __future__ import annotations

import httpx
import pytest

from evolve_zoom_mcp.zoom_s2s import S2sConfig, S2sError, ZoomS2sSession


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


class TestS2sConfigFromEnv:
    def test_returns_none_when_env_missing(self) -> None:
        assert S2sConfig.from_env({}) is None

    def test_returns_none_when_partial(self) -> None:
        assert S2sConfig.from_env({"ZOOM_S2S_CLIENT_ID": "x"}) is None

    def test_builds_when_complete(self) -> None:
        cfg = S2sConfig.from_env(
            {
                "ZOOM_S2S_CLIENT_ID": "cid",
                "ZOOM_S2S_CLIENT_SECRET": "sec",
                "ZOOM_S2S_ACCOUNT_ID": "acc",
            }
        )
        assert cfg is not None
        assert (cfg.client_id, cfg.client_secret, cfg.account_id) == ("cid", "sec", "acc")


class TestGetAccessToken:
    def test_mints_on_first_call(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": "s2s_at_1", "expires_in": 3600}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            assert session.get_access_token() == "s2s_at_1"
        finally:
            session.close()

    def test_caches_within_validity(self, s2s_config: S2sConfig) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(
                200, json={"access_token": "s2s_at", "expires_in": 3600}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            session.get_access_token()
            session.get_access_token()
            session.get_access_token()
            assert call_count["n"] == 1
        finally:
            session.close()

    def test_force_refresh_bypasses_cache(self, s2s_config: S2sConfig) -> None:
        responses = ["s2s_at_v1", "s2s_at_v2"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": responses.pop(0), "expires_in": 3600}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            assert session.get_access_token() == "s2s_at_v1"
            assert session.get_access_token(force_refresh=True) == "s2s_at_v2"
        finally:
            session.close()

    def test_invalidate_then_get_remints(self, s2s_config: S2sConfig) -> None:
        responses = ["s2s_at_v1", "s2s_at_v2"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": responses.pop(0), "expires_in": 3600}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            assert session.get_access_token() == "s2s_at_v1"
            session.invalidate()
            assert session.get_access_token() == "s2s_at_v2"
        finally:
            session.close()

    def test_near_expiry_refreshes_early(self, s2s_config: S2sConfig) -> None:
        responses = ["s2s_at_v1", "s2s_at_v2"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": responses.pop(0), "expires_in": 30}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        # Slack of 60s + expires_in of 30s → never considered fresh.
        session = ZoomS2sSession(s2s_config, client=client, refresh_slack_seconds=60)
        try:
            assert session.get_access_token() == "s2s_at_v1"
            assert session.get_access_token() == "s2s_at_v2"  # re-minted
        finally:
            session.close()


class TestErrorHandling:
    def test_zoom_400_raises_s2s_error(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_client", "reason": "Invalid client_id"}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            with pytest.raises(S2sError) as exc_info:
                session.get_access_token()
            assert exc_info.value.code == "invalid_client"
        finally:
            session.close()

    def test_non_json_response_raises_invalid_response(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="zoom is down right now")

        client = httpx.Client(transport=_mock_transport(handler))
        session = ZoomS2sSession(s2s_config, client=client)
        try:
            with pytest.raises(S2sError) as exc_info:
                session.get_access_token()
            assert exc_info.value.code == "invalid_response"
        finally:
            session.close()
