"""metrics.resolvers.plugin — plugin.loaded.

1.0 if the bot's gateway is serving the evolve plugin's status endpoint.
0.0 otherwise.

The signal: ``GET http://127.0.0.1:{port}/evolve/status`` is itself an
endpoint the evolve plugin registers with openclaw. If the gateway
returns HTTP 200 with a JSON body shaped like a plugin response (any
of ``bot_id`` / ``plugin_version`` / ``status`` present), the plugin is
loaded — the endpoint cannot exist without the plugin running. An
earlier revision looked for a ``plugins[]`` array with names containing
"evolve", but openclaw's actual response carries top-level keys instead
of a list, so every probe returned False positive even when the plugin
was healthy. Tests override via ``set_plugin_checker()``; the parser
itself is also exposed as a pure function for direct testing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register
from metrics.resolvers.gateway import _resolve_gateway_url


_Checker = Callable[[str], tuple[bool, str]]


# Keys whose presence in the JSON response indicates we are talking to
# the evolve plugin's status endpoint (not a generic 200 from some
# other handler). The endpoint is registered by the plugin itself, so
# any of these is sufficient evidence.
_PLUGIN_RESPONSE_KEYS = ("bot_id", "plugin_version", "status")


def _response_indicates_loaded(body: str) -> tuple[bool, str]:
    """Pure parser: given a /evolve/status response body, decide whether
    the evolve plugin is loaded. Factored out for direct unit tests."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, "status body is not JSON"
    if not isinstance(data, dict):
        return False, f"status body is not a JSON object (got {type(data).__name__})"
    matched = [k for k in _PLUGIN_RESPONSE_KEYS if k in data]
    if matched:
        version = data.get("plugin_version") or "unknown"
        return True, f"evolve plugin responding (version {version}; keys present: {matched})"
    return False, f"status body lacks any of {_PLUGIN_RESPONSE_KEYS!r}"


def _default_checker(bot_id: str) -> tuple[bool, str]:
    url = _resolve_gateway_url(bot_id)
    if url is None:
        return False, "no gateway port configured"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
            if resp.status != 200:
                return False, f"status returned HTTP {resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"probe failed: {e}"
    return _response_indicates_loaded(body)


_checker: _Checker = _default_checker


def set_plugin_checker(fn: _Checker) -> None:
    global _checker
    _checker = fn


def resolve_plugin_loaded(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    ok, note = _checker(bot_id)
    return MetricValue(
        value=1.0 if ok else 0.0,
        confidence=0.95 if ok else 0.8,
        source_note=note,
    )


register(
    MetricSpec(
        name="plugin.loaded",
        description="1.0 if the bot's status endpoint reports the Evolve plugin loaded.",
        unit="bool",
        source="HTTP GET /evolve/status, checking the plugins list",
    ),
    resolve_plugin_loaded,
)
