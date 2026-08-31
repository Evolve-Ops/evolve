"""Redacted baselining for openclaw.json snapshots.

Background (2026-05-25 triage): security_bot's manual security-warden workflow
(documented off-repo in ``SECURITY_BOT_MANUAL.md`` on the mini) writes
snapshots of other bots' ``openclaw.json`` into
``/Users/security_bot/.openclaw/workspace/memory/baselines/<bot>-openclaw.json``
for later config-drift diffing. The snapshots were being written
verbatim — including ``channels.telegram.botToken``,
``channels.slack.botToken``, ``channels.slack.appToken``,
``gateway.auth.token``, and any other secret values present.

That's the wrong shape. Drift detection only needs a stable answer to
"did this value change?", not the value itself. Storing the secret
cross-bot in plaintext makes any read of the baseline a credential
disclosure — including reads by security_bot's own LLM context and by any
defect in baseline distribution.

This module redacts secret values at well-known JSON paths (and by
leaf-key name) before writing, replacing each value with a stable
marker:

    "<REDACTED:sha256:abcd1234>"

The 8-char hex prefix is ``sha256(value)[:8]``. Equality-diff still
works: the marker changes when the value changes; the value itself
never lands on disk.

Use ``redacted_snapshot()`` from any baseline-writing path. The CLI in
``scrub_baselines.py`` scrubs existing on-disk baselines.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from generators.security_warden.redact import find_matches


# Leaf key names whose string values should always be redacted, regardless
# of where they appear in the structure. Matched case-insensitively against
# the exact key.
_SECRET_KEY_NAMES: frozenset[str] = frozenset({
    "token",
    "bottoken",
    "apptoken",
    "usertoken",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "apisecret",
    "clientsecret",
    "webhooksecret",
    "secret",
    "password",
    "passphrase",
    "privatekey",
})


# Fully-qualified dotted paths (case-sensitive, list indices replaced with
# "[]") whose string values should always be redacted. Catches values whose
# leaf key alone wouldn't flag them — e.g. "gateway.auth.token" where the
# key "token" is too generic to add to _SECRET_KEY_NAMES without false
# positives on schema keys like "maxTokens".
_SECRET_PATHS: frozenset[str] = frozenset({
    "gateway.auth.token",
})


# Leaf key names that look like secrets but aren't — exempt these from any
# pattern-based redaction so values like ``maxTokens`` aren't blanked.
_FALSE_POSITIVE_KEY_NAMES: frozenset[str] = frozenset({
    "maxtokens",
    "tokens",
    "tokenfloor",
    "tokenslimit",
    "reservetokensfloor",
    "softthresholdtokens",
    "thresholdtokens",
    "usertokenreadonly",
})


# Telegram bot tokens have a stable shape: <numeric-bot-id>:<35-char-token>.
# The standalone credential scanner in ``redact.py`` covers Anthropic /
# OpenAI / Slack / AWS / GitHub — add Telegram here so leaf-string fallback
# catches it even if the parent key isn't on the lists above.
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{35,}\b")


REDACTION_PREFIX = "<REDACTED:sha256:"
REDACTION_SUFFIX = ">"


def _redact_value(value: Any) -> str:
    """Replace ``value`` with a stable redaction marker.

    The marker embeds the first 8 hex chars of ``sha256(str(value))`` so
    equality-diff still works: same value → same marker, different
    value → different marker.
    """
    raw = str(value).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:8]
    return f"{REDACTION_PREFIX}{digest}{REDACTION_SUFFIX}"


def _is_already_redacted(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(REDACTION_PREFIX)
        and value.endswith(REDACTION_SUFFIX)
    )


def _should_redact_by_key(key: str) -> bool:
    norm = key.lower()
    if norm in _FALSE_POSITIVE_KEY_NAMES:
        return False
    return norm in _SECRET_KEY_NAMES


def _should_redact_by_path(path: str) -> bool:
    return path in _SECRET_PATHS


def _value_looks_like_secret(value: str) -> bool:
    """Leaf-string fallback for values not caught by key/path rules."""
    if len(value) < 20:
        return False
    if find_matches(value):
        return True
    if _TELEGRAM_TOKEN_RE.search(value):
        return True
    return False


def redact_json(data: Any, *, _path: str = "", _parent_key: str = "") -> Any:
    """Return a deep-copied ``data`` with secret values replaced by markers.

    Walks dicts and lists recursively. Non-string leaves (bool, int,
    float, None) pass through unchanged — keys like ``userTokenReadOnly``
    or ``maxTokens`` carry non-string values and aren't credentials.

    A value is redacted when any of:
      - the leaf key name matches ``_SECRET_KEY_NAMES``
      - the dotted path matches ``_SECRET_PATHS``
      - the leaf string itself matches a known credential pattern
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            sub_path = f"{_path}.{k}" if _path else str(k)
            out[k] = redact_json(v, _path=sub_path, _parent_key=str(k))
        return out

    if isinstance(data, list):
        return [
            redact_json(item, _path=f"{_path}[]", _parent_key=_parent_key)
            for item in data
        ]

    if not isinstance(data, str):
        return data

    if _is_already_redacted(data):
        return data

    if _parent_key and _should_redact_by_key(_parent_key):
        return _redact_value(data)

    if _should_redact_by_path(_path):
        return _redact_value(data)

    if _value_looks_like_secret(data):
        return _redact_value(data)

    return data


def redacted_snapshot(source: Any) -> Any:
    """Public entrypoint: return a redacted copy of ``source`` JSON data.

    ``source`` should be already-parsed JSON (dict / list / primitive).
    Callers that have a file path should read + ``json.loads`` first,
    then call this.
    """
    return redact_json(source)
