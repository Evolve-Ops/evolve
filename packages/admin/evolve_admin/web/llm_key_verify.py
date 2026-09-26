"""Post-rotation liveness check for an LLM provider API key.

One call per rotation, against the provider's model-LIST endpoint rather
than an inference endpoint. The brief asked for "one cheap call on the
provider's smallest model"; the list endpoint is strictly cheaper (every
provider here bills it at zero) and answers the same question more
precisely — an inference call conflates "key rejected" with "that model is
not enabled for this account", which is the exact ambiguity
[[feedback_shared_fallback_erases_broken_vs_unavailable]] warns about.

What a result means:
  ``ok``           — the provider accepted the credential (HTTP 2xx).
  ``not ok`` + 401/403 — the key is wrong, revoked, or lacks scope. This is
                     the one the operator needs after a bad paste.
  ``not ok`` + other  — network / provider-side; the key is UNPROVEN, not
                     disproven, and the copy says so. A rotation is never
                     rolled back on this: the write already landed where the
                     runtime reads, and undoing it on a transient 503 would
                     put the stale key back.

Nothing here logs, echoes or returns the key. Unknown providers return a
``skipped`` result rather than a failure — a provider Evolve cannot verify
is not a provider whose rotation failed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Seconds. Short: this runs inline in the rotate request, and the operator
#: is watching a spinner.
VERIFY_TIMEOUT_S = 10

_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    skipped: bool
    status: int | None
    detail: str

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "status": self.status,
            "detail": self.detail,
        }


# provider → (url, auth-header-name, header-value-template, extra headers)
# provider-literal-allow-begin
_ENDPOINTS: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "anthropic": (
        "https://api.anthropic.com/v1/models?limit=1",
        "x-api-key", "{key}", {"anthropic-version": _ANTHROPIC_VERSION},
    ),
    "openai": (
        "https://api.openai.com/v1/models",
        "Authorization", "Bearer {key}", {},
    ),
    "google": (
        # Header form, NOT ``?key=`` — a credential in a query string lands in
        # proxy and access logs on both ends.
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        "x-goog-api-key", "{key}", {},
    ),
    "xai": (
        "https://api.x.ai/v1/models", "Authorization", "Bearer {key}", {},
    ),
    "mistral": (
        "https://api.mistral.ai/v1/models", "Authorization", "Bearer {key}", {},
    ),
    "groq": (
        "https://api.groq.com/openai/v1/models",
        "Authorization", "Bearer {key}", {},
    ),
    "perplexity": (
        "https://api.perplexity.ai/models", "Authorization", "Bearer {key}", {},
    ),
    "together": (
        "https://api.together.xyz/v1/models",
        "Authorization", "Bearer {key}", {},
    ),
    "deepseek": (
        "https://api.deepseek.com/models", "Authorization", "Bearer {key}", {},
    ),
    "cohere": (
        "https://api.cohere.com/v1/models", "Authorization", "Bearer {key}", {},
    ),
    "moonshot": (
        "https://api.moonshot.cn/v1/models",
        "Authorization", "Bearer {key}", {},
    ),
}
# provider-literal-allow-end


def verifiable(provider: str) -> bool:
    """True when :func:`verify_key` can actually reach this provider."""
    return provider in _ENDPOINTS


def verify_key(
    provider: str,
    key: str,
    *,
    timeout: float = VERIFY_TIMEOUT_S,
    opener=None,
) -> VerifyResult:
    """Ask *provider* whether *key* is currently accepted.

    *opener* exists for tests: any callable with ``urlopen``'s
    ``(request, timeout=…)`` signature and context-manager result. The real
    path uses :mod:`urllib.request` directly — no session, no retry, no
    redirect handling beyond urllib's own.
    """
    spec = _ENDPOINTS.get(provider)
    if spec is None:
        return VerifyResult(
            ok=False, skipped=True, status=None,
            detail=f"no verification endpoint is known for {provider}",
        )
    url, auth_header, template, extra = spec
    req = urllib.request.Request(url, method="GET")
    req.add_header(auth_header, template.format(key=key))
    for name, value in extra.items():
        req.add_header(name, value)
    urlopen = opener if opener is not None else urllib.request.urlopen
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            # Drain so the connection can be reused / closed cleanly; the
            # body is never inspected and never returned.
            resp.read(1)
        return VerifyResult(
            ok=True, skipped=False, status=status,
            detail=f"{provider} accepted the key",
        )
    except urllib.error.HTTPError as exc:
        detail = _http_detail(provider, exc)
        return VerifyResult(ok=False, skipped=False, status=exc.code, detail=detail)
    except Exception as exc:  # noqa: BLE001 — URLError, socket timeout, TLS, …
        return VerifyResult(
            ok=False, skipped=False, status=None,
            detail=(
                f"could not reach {provider} to verify ({type(exc).__name__}) "
                "— the new key is in place but unproven"
            ),
        )


def _http_detail(provider: str, exc: urllib.error.HTTPError) -> str:
    if exc.code in (401, 403):
        return (
            f"{provider} rejected the key (HTTP {exc.code}) — check the paste, "
            "or use Undo to put the previous key back"
        )
    reason = ""
    try:
        body = exc.read(400).decode("utf-8", errors="replace")
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                reason = str(err.get("message") or "")
            elif isinstance(err, str):
                reason = err
    except Exception:  # noqa: BLE001 — a body we cannot parse is not a failure
        reason = ""
    suffix = f": {reason[:160]}" if reason else ""
    return (
        f"{provider} returned HTTP {exc.code} — the new key is in place but "
        f"unproven{suffix}"
    )


__all__ = ["VERIFY_TIMEOUT_S", "VerifyResult", "verifiable", "verify_key"]
