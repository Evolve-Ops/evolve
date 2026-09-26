"""infra_llm.py — provider-agnostic client for Evolve's OWN (engine) LLM calls.

Umbrella: github issue #3466 (provider-agnostic infra; PR-2 of the series).
Principle: docs/principle-llm-provider-agnostic.md — never presume a
provider, never compel one, lean toward a provider only when credentialed.

Evolve's engine background work (proposal synthesis, spec extraction, home
chat, scanners, classifiers, critics, …) historically built bespoke Anthropic
Messages calls keyed by ``primary_bot.read_primary_bot_anthropic_key``. This
module is the single shared replacement those call sites migrate onto (PR-3):

  target = resolve_infra_llm("fast")           # or "standard"
  if target is None:
      ...existing degrade path (no provider credentialed)...
  else:
      text = complete(target, prompt="...", system="...", max_tokens=512)

Design contract:
  * Resolution NEVER falls back to a provider the pod has no key for — the
    "discard a correctly-configured non-Anthropic tier model and call Claude
    anyway" anti-pattern is exactly what this module exists to kill.
  * Transports are stdlib ``urllib`` only (one uniform HTTP path; the
    ``anthropic`` SDK is not used here).
  * API keys are redacted from every error and repr path.
  * ``transport`` is an injectable seam for tests:
    ``callable(url, headers, body_dict) -> (status, body_dict)``.

Provider names live only in the adapter table below and in
``models._PROVIDER_PICKS`` / ``primary_bot._PROVIDER_KEY_ENV_VARS`` (catalog
data / adapter buckets of the three-homes rule — this module is covered by
``tools/provider-literal-lint``).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "InfraLLMTarget",
    "InfraLLMError",
    "resolve_infra_llm",
    "credentialed_target",
    "complete",
]

# Transport seam: callable(url, headers, body_dict) -> (status, body_dict).
Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]

# Backoff before the single retry on 429/5xx/overloaded. Module-level so
# tests can zero it out.
RETRY_BACKOFF_SECONDS: float = 2.0


class InfraLLMError(RuntimeError):
    """HTTP or response-parse failure from an infra LLM call.

    Messages carry the provider and HTTP status plus a redacted body
    snippet — never the API key.
    """


@dataclass(repr=False)
class InfraLLMTarget:
    """A resolved, credentialed (provider, model, key) triple.

    ``model`` is provider-qualified (``"openai/gpt-4o-mini"``); each
    transport strips its own provider prefix before sending. ``base_url``
    is only used for openai-compatible custom endpoints (a provider name
    outside the adapter table).
    """

    provider: str
    model: str
    api_key: str
    base_url: str = ""

    def __repr__(self) -> str:  # never leak the key into logs/tracebacks
        return (
            f"InfraLLMTarget(provider={self.provider!r}, model={self.model!r}, "
            f"api_key='***', base_url={self.base_url!r})"
        )


# ── Redaction helpers ─────────────────────────────────────────────────────────


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _redacted_dump(body: Any, secret: str) -> str:
    try:
        text = json.dumps(body)
    except Exception:
        text = str(body)
    return _redact(text, secret)


def _strip_provider_prefix(model: str, provider: str) -> str:
    prefix = provider + "/"
    return model[len(prefix):] if model.startswith(prefix) else model


# Not a provider/model id — the lint's structural "x/y" matcher can't tell
# a MIME type from a qualified model id, hence the line-level allow.
_CONTENT_TYPE_JSON = "application/json"  # provider-literal-allow


# ── Provider adapters (request builders + response parsers) ───────────────────
#
# The ONLY place provider endpoints / auth-header shapes live. Request shapes
# mirror the existing multi-provider dispatch in the admin help agent
# (packages/admin/evolve_admin/web/server.py::api_help), the template site.

# provider-literal-allow-begin: provider adapter endpoint table (bucket b)
_ANTHROPIC = "anthropic"
_OPENAI = "openai"
_GOOGLE = "google"
_XAI = "xai"
_MOONSHOT = "moonshot"
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_OPENAI_COMPAT_BASE_URLS: dict[str, str] = {
    _OPENAI: "https://api.openai.com/v1",
    _XAI: "https://api.x.ai/v1",
    _MOONSHOT: "https://api.moonshot.ai/v1",
}
_GOOGLE_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
# provider-literal-allow-end


def _build_anthropic(
    target: InfraLLMTarget,
    messages: list[dict[str, Any]],
    system: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    body: dict[str, Any] = {
        "model": _strip_provider_prefix(target.model, target.provider),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        body["system"] = system
    headers = {
        "x-api-key": target.api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": _CONTENT_TYPE_JSON,
    }
    return _ANTHROPIC_MESSAGES_URL, headers, body


def _parse_anthropic(body: dict[str, Any]) -> str:
    blocks = body["content"]
    return "".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type", "text") == "text"
    ).strip()


def _build_openai_compat(
    target: InfraLLMTarget,
    messages: list[dict[str, Any]],
    system: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    base = (target.base_url or _OPENAI_COMPAT_BASE_URLS.get(target.provider, "")).rstrip("/")
    if not base:
        raise InfraLLMError(
            f"no endpoint known for provider '{target.provider}' — "
            "set InfraLLMTarget.base_url for openai-compatible custom endpoints"
        )
    msgs: list[dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    body: dict[str, Any] = {
        "model": _strip_provider_prefix(target.model, target.provider),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": msgs,
    }
    headers = {
        "Authorization": f"Bearer {target.api_key}",
        "content-type": _CONTENT_TYPE_JSON,
    }
    return f"{base}/chat/completions", headers, body


def _parse_openai_compat(body: dict[str, Any]) -> str:
    return body["choices"][0]["message"]["content"].strip()


def _build_google(
    target: InfraLLMTarget,
    messages: list[dict[str, Any]],
    system: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    contents = [
        {
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": m.get("content", "")}],
        }
        for m in messages
    ]
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    model = _strip_provider_prefix(target.model, target.provider)
    url = _GOOGLE_GENERATE_URL.format(model=model) + "?key=" + target.api_key
    return url, {"content-type": _CONTENT_TYPE_JSON}, body


def _parse_google(body: dict[str, Any]) -> str:
    parts = body["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()


_Builder = Callable[
    [InfraLLMTarget, list[dict[str, Any]], str, int, float],
    tuple[str, dict[str, str], dict[str, Any]],
]
_Parser = Callable[[dict[str, Any]], str]

_ADAPTERS: dict[str, tuple[_Builder, _Parser]] = {
    _ANTHROPIC: (_build_anthropic, _parse_anthropic),
    _OPENAI: (_build_openai_compat, _parse_openai_compat),
    _XAI: (_build_openai_compat, _parse_openai_compat),
    _MOONSHOT: (_build_openai_compat, _parse_openai_compat),
    _GOOGLE: (_build_google, _parse_google),
}


# ── Resolution ────────────────────────────────────────────────────────────────


def resolve_infra_llm(
    role: str = "fast",
    *,
    network: dict[str, Any] | None = None,
    model_override: str = "",
) -> InfraLLMTarget | None:
    """Resolve the (provider, model, key) triple for an engine LLM call.

    ``role``: ``"fast"`` (tier3 / haiku-class background work) or
    ``"standard"`` (tier2-class). Any role/tier token ``models``
    understands is accepted.

    Resolution:
      1. The pod's tier config for that role (primary-bot inheritance and
         the ``cascade.engine_default_tier`` cost knob both honored, same
         as ``models.resolve_tier`` engine-side calls): the first model in
         the chain whose provider is credentialed on the primary bot wins.
      2. Otherwise walk the credentialed providers via
         ``models.derive_default_tiers`` (catalog picks).
      3. ``None`` when NO provider is credentialed — callers keep their
         existing degrade paths.

    ``model_override`` carries an operator pin (env knobs like
    ``EVOLVE_INSPECTOR_HAIKU_MODEL``):
      * a QUALIFIED id (``"openai/gpt-4o-mini"``) wins outright when its
        provider is credentialed; when it isn't, the pin is ignored (with
        a debug log) and normal resolution proceeds — an override can
        never produce an uncredentialed target.
      * a BARE id (``"claude-haiku-4-5-20251001"``, the historic form of
        these knobs) pins the MODEL ID on whatever provider resolution
        lands on. On the single-provider pods that set these knobs today
        this reproduces the legacy behavior exactly; operators on
        multi-provider pods should qualify the pin.

    NEVER returns a target whose provider has no key: a tier model from an
    uncredentialed provider is walked past, not "corrected" to some
    presumed default provider.
    """
    from models import (  # analyzer flat-layout sibling  # type: ignore
        _KNOWN_LLM_PROVIDERS,
        derive_default_tiers,
        engine_default_tier_from_network,
        get_tier_models,
        normalize_to_tier,
    )
    from primary_bot import (  # type: ignore
        _load_network_default,
        read_primary_bot_llm_keys,
    )

    if network is None:
        network = _load_network_default()
    keys = {p: k for p, k in read_primary_bot_llm_keys(network).items() if k}
    if not keys:
        return None

    override = (model_override or "").strip()
    if override and "/" in override:
        prov = override.split("/", 1)[0]
        if prov in keys:
            return InfraLLMTarget(provider=prov, model=override, api_key=keys[prov])
        logger.debug(
            "infra_llm: model override %r names an uncredentialed provider; "
            "ignoring the pin",
            override,
        )
        override = ""

    def _pin(target: InfraLLMTarget) -> InfraLLMTarget:
        if override:
            target.model = override
        return target

    tier = normalize_to_tier(role)
    forced = engine_default_tier_from_network(network)
    if forced:
        tier = forced

    try:
        chain = get_tier_models(tier, network)
    except Exception:
        chain = []
    for model in chain:
        if "/" not in model:
            continue  # bare id: provider unknowable — never presume one
        prov = model.split("/", 1)[0]
        if prov in keys:
            return _pin(InfraLLMTarget(provider=prov, model=model, api_key=keys[prov]))

    # No chain model is credentialed — derive from the credentialed set.
    credentialed = {p for p in keys if p in _KNOWN_LLM_PROVIDERS}
    if not credentialed:
        return None
    derived = derive_default_tiers(credentialed)
    for model in derived.get(tier, {}).get("models", []):
        if "/" not in model:
            continue
        prov = model.split("/", 1)[0]
        # derive_default_tiers falls back to provider-pinned DEFAULT_TIERS
        # when nothing is derivable — guard so an uncredentialed provider
        # can never leak through.
        if prov in credentialed:
            return _pin(InfraLLMTarget(provider=prov, model=model, api_key=keys[prov]))
    return None


def credentialed_target(
    model: str, *, network: dict[str, Any] | None = None
) -> InfraLLMTarget | None:
    """Target for an explicitly configured model — IFF its provider is
    credentialed on the primary bot.

    The shared "honor a pinned model without ever presuming a provider"
    helper for call sites that carry their own model configuration (a
    per-bot tier pin, a ``network.json`` override, a CLI ``--model``
    flag). Returns ``None`` — caller falls back to
    :func:`resolve_infra_llm` — when ``model`` is empty, bare
    (unqualified: provider unknowable, never presumed), or qualified
    with a provider the pod has no key for.
    """
    if not model or "/" not in model:
        return None
    from primary_bot import (  # type: ignore
        _load_network_default,
        read_primary_bot_llm_keys,
    )

    if network is None:
        network = _load_network_default()
    prov = model.split("/", 1)[0]
    try:
        key = (read_primary_bot_llm_keys(network) or {}).get(prov, "")
    except Exception:
        key = ""
    if not key:
        return None
    return InfraLLMTarget(provider=prov, model=model, api_key=key)


# ── Completion ────────────────────────────────────────────────────────────────


def _default_transport(timeout: int, secret: str) -> Transport:
    """stdlib urllib POST transport. HTTP errors return (status, parsed_body);
    network-level failures raise :class:`InfraLLMError` (key redacted)."""

    def _post(
        url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https APIs
                return resp.getcode() or 200, json.loads(
                    resp.read().decode("utf-8", errors="replace")
                )
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"error": raw[:500]}
            return e.code, parsed
        except InfraLLMError:
            raise
        except Exception as e:  # URLError / timeout / socket — no response
            raise InfraLLMError(f"transport error: {_redact(str(e), secret)}") from None

    return _post


def complete(
    target: InfraLLMTarget,
    *,
    prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: int = 60,
    transport: Transport | None = None,
) -> str:
    """Single completion against ``target``; returns the assistant text.

    Pass exactly one of ``prompt`` (single user turn) or ``messages``
    (``[{"role": "user"|"assistant", "content": str}, ...]``). ``system``
    rides in each provider's native system slot.

    Retries ONCE (with a short backoff) on 429 / 5xx / an "overloaded"
    error body. Raises :class:`InfraLLMError` on HTTP or parse failure —
    message carries provider + status, never the API key.
    """
    if (prompt is None) == (messages is None):
        raise ValueError("pass exactly one of prompt= or messages=")
    if messages is None:
        messages = [{"role": "user", "content": prompt}]

    adapter = _ADAPTERS.get(target.provider)
    if adapter is None:
        if not target.base_url:
            raise InfraLLMError(
                f"no transport adapter for provider '{target.provider}' "
                "(openai-compatible custom endpoints need base_url)"
            )
        adapter = (_build_openai_compat, _parse_openai_compat)
    build, parse = adapter

    url, headers, body = build(target, messages, system, max_tokens, temperature)
    if transport is None:
        transport = _default_transport(timeout, target.api_key)

    retried = False
    while True:
        status, resp = transport(url, headers, body)
        if 200 <= status < 300:
            try:
                return parse(resp)
            except Exception:
                raise InfraLLMError(
                    f"{target.provider}: unexpected response shape "
                    f"(HTTP {status}): {_redacted_dump(resp, target.api_key)[:300]}"
                ) from None
        text = _redacted_dump(resp, target.api_key)
        retryable = status == 429 or status >= 500 or "overloaded" in text.lower()
        if retryable and not retried:
            retried = True
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue
        raise InfraLLMError(
            f"{target.provider} request failed (HTTP {status}): {text[:300]}"
        )
