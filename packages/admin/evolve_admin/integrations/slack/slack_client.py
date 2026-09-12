"""Minimal Slack Web API client (urllib-based, no external deps).

Only covers the read endpoints the doctor needs. Every call returns
either ``(data, None)`` on success or ``(None, error)`` on failure —
callers must never treat a missing field as success.

Why urllib and not slack_sdk: the admin server can't take new pip-level
dependencies casually (per the runtime context in CLAUDE.md, redeploys
are git pulls; pip churn on every refresh is hostile). urllib is in
stdlib, the Slack API is plain JSON-over-HTTPS, and we need at most
five endpoints. If Phase 2's UI layer needs a richer client, that can
adopt slack_sdk independently.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_API_BASE = "https://slack.com/api/"
_DEFAULT_TIMEOUT = 10.0
_USER_AGENT = "evolve-slack-doctor/1.0"

log = logging.getLogger(__name__)


@dataclass
class SlackError:
    """Structured failure from a Slack API call.

    ``code`` is one of:

    - ``"http_error"`` — non-200 response or network/socket failure
    - ``"transport_error"`` — urllib threw (DNS, TLS, etc.)
    - ``"json_decode"`` — body wasn't JSON
    - ``"api_error"`` — Slack returned ``ok: false``; ``slack_error`` carries
      the value of the ``error`` field (e.g. ``invalid_auth``, ``not_in_channel``)

    Doctor checks branch on the ``slack_error`` value when present —
    distinguishing ``invalid_auth`` from ``account_inactive`` from
    ``not_authed`` matters for the operator-facing remediation hint.
    """
    code: str
    detail: str
    slack_error: str | None = None
    http_status: int | None = None

    def __str__(self) -> str:
        bits = [self.code]
        if self.slack_error:
            bits.append(self.slack_error)
        if self.http_status is not None:
            bits.append(f"http={self.http_status}")
        if self.detail:
            bits.append(self.detail)
        return ": ".join(bits)


class SlackClient:
    """Thin wrapper around the Slack Web API."""

    def __init__(
        self,
        bot_token: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("bot_token is required")
        self._token = bot_token.strip()
        self._timeout = timeout

    # ── Endpoints used by the doctor ────────────────────────────────────────

    def auth_test(self) -> tuple[dict[str, Any] | None, SlackError | None]:
        """``auth.test`` — confirms token validity, returns identity.

        Successful response carries ``user_id``, ``user``, ``team_id``,
        ``team``, ``bot_id``, ``url``. We also surface ``_scopes`` —
        the parsed ``x-oauth-scopes`` response header, which is the
        only authoritative way to read the bot's granted OAuth scopes
        without the admin-level ``apps.permissions.scopes.list`` API.
        ``_scopes`` is a list (deterministic order: as Slack emits it),
        or empty if the header was absent. The underscore prefix is a
        signal that it's an Evolve-side augmentation, not a literal
        Slack field.
        """
        return self._call("auth.test", {}, capture_scopes=True)

    def users_conversations(
        self,
        *,
        types: str = "public_channel,private_channel",
        exclude_archived: bool = True,
    ) -> tuple[list[dict[str, Any]] | None, SlackError | None]:
        """``users.conversations`` — channels the *authenticated bot* is a member of.

        Per team_bot_a's bug 1 post-mortem: this is the right endpoint for
        discovering private channels (``conversations.list`` does not
        return them) and for verifying the bot's actual membership.

        Returns the merged ``channels`` array across paginated responses.
        On any pagination-page failure the partial result is discarded
        and an error is returned — the doctor must not act on a partial
        channel list (would produce false-positive SLK002s for channels
        that simply weren't in the partial page).
        """
        return self._paginated_get(
            "users.conversations",
            {
                "types": types,
                "exclude_archived": "true" if exclude_archived else "false",
                "limit": "200",
            },
            collection_key="channels",
        )

    def conversations_info(
        self, channel_id: str,
    ) -> tuple[dict[str, Any] | None, SlackError | None]:
        """``conversations.info`` — metadata for one channel by ID.

        Used by the ``--fix`` rewrite path (name→ID) and to surface the
        current name for SLK009. Returns the ``channel`` object.
        """
        data, err = self._call("conversations.info", {"channel": channel_id})
        if err is not None:
            return None, err
        channel = (data or {}).get("channel") or None
        if channel is None:
            return None, SlackError(
                code="api_error",
                detail="response missing 'channel'",
                slack_error="missing_field",
            )
        return channel, None

    def users_list(
        self,
        *,
        include_locale: bool = False,
    ) -> tuple[list[dict[str, Any]] | None, SlackError | None]:
        """``users.list`` — every member of the workspace.

        Paginated; we walk all pages via :meth:`_paginated_get`. Filtering
        (skip bots / deleted users / app accounts) is the caller's job —
        the raw page is returned verbatim so different consumers can
        apply their own policy.
        """
        return self._paginated_get(
            "users.list",
            {
                "include_locale": "true" if include_locale else "false",
                "limit": "200",
            },
            collection_key="members",
        )

    def users_info(
        self, user_id: str,
    ) -> tuple[dict[str, Any] | None, SlackError | None]:
        """``users.info`` — confirms a user ID is recognized by the workspace.

        Used by SLK004 to flag DM-allowlist references to unknown user
        IDs. Returns the ``user`` object on success.
        """
        data, err = self._call("users.info", {"user": user_id})
        if err is not None:
            return None, err
        user = (data or {}).get("user") or None
        if user is None:
            return None, SlackError(
                code="api_error",
                detail="response missing 'user'",
                slack_error="missing_field",
            )
        return user, None

    # ── Low-level helpers ───────────────────────────────────────────────────

    def _call(
        self,
        method: str,
        params: dict[str, str],
        *,
        capture_scopes: bool = False,
    ) -> tuple[dict[str, Any] | None, SlackError | None]:
        """One Slack Web API request. Returns ``(json, None)`` or ``(None, error)``.

        ``capture_scopes`` causes the call to extract the
        ``x-oauth-scopes`` response header and merge the parsed scope
        list into the returned dict under the key ``"_scopes"``. The
        header is Slack's canonical surface for "what OAuth scopes is
        this token granted" — used by the doctor's feature-bundle
        checklist.
        """
        url = _API_BASE + method
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                status = resp.getcode()
                scope_header = resp.headers.get("x-oauth-scopes") if capture_scopes else None
        except urllib.error.HTTPError as exc:
            return None, SlackError(
                code="http_error",
                detail=f"{exc.reason}",
                http_status=exc.code,
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return None, SlackError(
                code="transport_error",
                detail=str(exc),
            )
        if status != 200:
            return None, SlackError(
                code="http_error",
                detail=f"non-200 response",
                http_status=status,
            )
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, SlackError(
                code="json_decode",
                detail=str(exc),
            )
        if not isinstance(data, dict):
            return None, SlackError(
                code="json_decode",
                detail="response was not a JSON object",
            )
        if not data.get("ok"):
            slack_err = str(data.get("error") or "unknown")
            return None, SlackError(
                code="api_error",
                detail=data.get("response_metadata", {}).get("messages", [""])[0]
                       if isinstance(data.get("response_metadata"), dict) else "",
                slack_error=slack_err,
            )
        if capture_scopes:
            data["_scopes"] = _parse_scopes_header(scope_header)
        return data, None

    def _paginated_get(
        self,
        method: str,
        params: dict[str, str],
        *,
        collection_key: str,
        max_pages: int = 100,
    ) -> tuple[list[dict[str, Any]] | None, SlackError | None]:
        """Walk Slack's cursor pagination, returning the concatenated collection.

        ``max_pages`` bounds the loop so a pathological server response
        can't spin forever. 100 × 200 = 20,000 items, which covers
        Slack Enterprise workspaces. A bot in 20k+ channels is
        operationally suspicious and worth surfacing as
        ``too_many_pages`` rather than spinning forever.
        """
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data, err = self._call(method, page_params)
            if err is not None:
                return None, err
            items = (data or {}).get(collection_key) or []
            if not isinstance(items, list):
                return None, SlackError(
                    code="json_decode",
                    detail=f"response key '{collection_key}' was not a list",
                )
            results.extend(items)
            meta = (data or {}).get("response_metadata") or {}
            next_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not next_cursor:
                return results, None
            cursor = next_cursor
        return None, SlackError(
            code="api_error",
            detail=f"exceeded {max_pages} pagination pages",
            slack_error="too_many_pages",
        )


def _parse_scopes_header(value: str | None) -> list[str]:
    """Parse the comma-separated ``x-oauth-scopes`` header into a list.

    Returns ``[]`` for missing or empty header. Trims whitespace per
    entry. Order is preserved — Slack emits scopes in a stable order
    that doubles as a hint at registration sequence.
    """
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


__all__ = ["SlackClient", "SlackError"]
