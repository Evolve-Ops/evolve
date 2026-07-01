"""Tests for zoom_write.py."""

from __future__ import annotations

import json

import httpx
import pytest

from evolve_zoom_mcp.zoom_s2s import S2sConfig, S2sError, ZoomS2sSession
from evolve_zoom_mcp.zoom_write import (
    create_meeting,
    delete_meeting,
    update_meeting,
    write_tool_definitions,
)


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def _s2s_with_mint(client: httpx.Client, s2s_config: S2sConfig) -> ZoomS2sSession:
    """Build a session that uses the shared mock client for both mint + API.

    The handler must answer the /oauth/token mint AND the v2 API calls.
    """
    return ZoomS2sSession(s2s_config, client=client)


# ---------- write_tool_definitions ----------


class TestWriteToolDefinitions:
    def test_three_tools_with_consistent_shape(self) -> None:
        tools = write_tool_definitions()
        names = [t["name"] for t in tools]
        assert names == ["create_meeting", "update_meeting", "delete_meeting"]
        for t in tools:
            assert "description" in t and t["description"]
            assert t["inputSchema"]["type"] == "object"

    def test_create_meeting_requires_topic_only(self) -> None:
        create = [t for t in write_tool_definitions() if t["name"] == "create_meeting"][0]
        assert create["inputSchema"]["required"] == ["topic"]

    def test_include_start_url_defaults_false(self) -> None:
        create = [t for t in write_tool_definitions() if t["name"] == "create_meeting"][0]
        assert (
            create["inputSchema"]["properties"]["include_start_url"]["default"] is False
        )


# ---------- create_meeting ----------


class TestCreateMeeting:
    def test_happy_path_projects_to_stable_shape(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            assert request.method == "POST"
            assert url.endswith("/users/me/meetings")
            body = json.loads(request.read())
            assert body["topic"] == "Sync"
            assert body["type"] == 1  # instant (no start_time)
            return httpx.Response(
                201,
                json={
                    "id": 1234567890,
                    "topic": "Sync",
                    "join_url": "https://us02.zoom.us/j/1234567890",
                    "password": "abc",
                    "start_url": "https://us02.zoom.us/s/sensitive",
                    "duration": 60,
                    "start_time": "2026-06-10T15:00:00Z",
                    "host_email": "owner@example.test",
                },
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = create_meeting(session, topic="Sync", client=client)
        finally:
            session.close()
        assert out["meeting_id"] == 1234567890
        assert out["join_url"] == "https://us02.zoom.us/j/1234567890"
        # start_url omitted by default.
        assert "start_url" not in out
        assert "warning" not in out

    def test_include_start_url_surfaces_warning(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            return httpx.Response(
                201,
                json={
                    "id": 1,
                    "topic": "x",
                    "join_url": "j",
                    "start_url": "s",
                    "duration": 60,
                },
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = create_meeting(session, topic="x", include_start_url=True, client=client)
        finally:
            session.close()
        assert out["start_url"] == "s"
        assert "warning" in out
        assert "sensitive" in out["warning"].lower()

    def test_scheduled_meeting_sets_type_2(self, s2s_config: S2sConfig) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            captured.update(json.loads(request.read()))
            return httpx.Response(
                201, json={"id": 1, "topic": "x", "join_url": "j", "duration": 30}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            create_meeting(
                session,
                topic="x",
                start_time="2026-07-01T12:00:00Z",
                duration_minutes=30,
                client=client,
            )
        finally:
            session.close()
        assert captured["type"] == 2
        assert captured["start_time"] == "2026-07-01T12:00:00Z"
        assert captured["duration"] == 30

    def test_host_email_threads_to_url(self, s2s_config: S2sConfig) -> None:
        captured_url: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            captured_url["url"] = url
            return httpx.Response(201, json={"id": 1, "topic": "x", "join_url": "j", "duration": 60})

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            create_meeting(
                session, topic="x", host_email="other@example.test", client=client
            )
        finally:
            session.close()
        assert "/users/other@example.test/meetings" in captured_url["url"]

    def test_401_triggers_refresh_and_retry(self, s2s_config: S2sConfig) -> None:
        states = {"mint_count": 0, "api_attempts": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                states["mint_count"] += 1
                return httpx.Response(
                    200,
                    json={"access_token": f"s2s_at_{states['mint_count']}", "expires_in": 3600},
                )
            states["api_attempts"] += 1
            if states["api_attempts"] == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(
                201, json={"id": 1, "topic": "x", "join_url": "j", "duration": 60}
            )

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = create_meeting(session, topic="x", client=client)
        finally:
            session.close()
        assert out["meeting_id"] == 1
        assert states["mint_count"] == 2  # initial + refresh
        assert states["api_attempts"] == 2  # 401 + retry


# ---------- update_meeting ----------


class TestUpdateMeeting:
    def test_no_fields_returns_no_op(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("HTTP should not be called with no fields")

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = update_meeting(session, meeting_id=12345, client=client)
        finally:
            session.close()
        assert out == {"meeting_id": 12345, "updated": False, "note": "no fields to update"}

    def test_topic_only_patches_correctly(self, s2s_config: S2sConfig) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            assert request.method == "PATCH"
            assert url.endswith("/meetings/12345")
            captured.update(json.loads(request.read()))
            return httpx.Response(204)

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = update_meeting(session, meeting_id=12345, topic="renamed", client=client)
        finally:
            session.close()
        assert captured == {"topic": "renamed"}
        assert out == {"meeting_id": 12345, "updated": True}


# ---------- delete_meeting ----------


class TestDeleteMeeting:
    def test_sends_delete(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            assert request.method == "DELETE"
            assert url.endswith("/meetings/12345")
            return httpx.Response(204)

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            out = delete_meeting(session, meeting_id=12345, client=client)
        finally:
            session.close()
        assert out == {"meeting_id": 12345, "deleted": True}

    def test_404_raises_s2s_error(self, s2s_config: S2sConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/oauth/token"):
                return httpx.Response(
                    200, json={"access_token": "s2s_at", "expires_in": 3600}
                )
            return httpx.Response(404, json={"message": "Meeting does not exist"})

        client = httpx.Client(transport=_mock_transport(handler))
        session = _s2s_with_mint(client, s2s_config)
        try:
            with pytest.raises(S2sError) as exc_info:
                delete_meeting(session, meeting_id=12345, client=client)
        finally:
            session.close()
        assert "does not exist" in str(exc_info.value)
        assert exc_info.value.code == "http_404"
