"""http_errors.py — client-safe error responses for the admin HTTP layer.

Historically every route handler caught ``Exception`` and returned
``jsonify({"error": str(e), "trace": traceback.format_exc()}), 500`` — which
shipped the full Python traceback (absolute file paths, internal module
structure, config hints) straight to the HTTP client. The admin UI is
single-operator and device-authenticated, so the severity is low, but a stack
trace on the wire is still an information leak and poor practice.

``error_response`` is the single sanctioned replacement. It logs the full
traceback **server-side** (the ``evolve_admin.web.http_errors`` logger, which
lands in the same admin log as every other handler error) and returns a body
that carries only an operator-facing message — never a traceback.

Usage at a call site::

    except Exception as e:
        return error_response(e)                 # -> {"error": str(e)}, 500
        return error_response(e, ok=False)       # -> {"ok": False, "error": ...}, 500
        return error_response(e, 400, ok=False)  # -> 400 with the ok envelope

Handlers whose error body must carry extra keys (so the simple shape above
does not fit) call ``log_request_error`` to emit the same server-side record,
then return their own dict **without** a ``"trace"`` key.
"""
from __future__ import annotations

from flask import Response, jsonify, request

from ..telemetry import get_logger

_log = get_logger("web.http_errors")


def log_request_error(exc: BaseException) -> None:
    """Log ``exc`` (with full traceback) against the active request, server-side.

    The traceback goes to the admin log only — it never reaches the client.
    Use this directly for the few handlers whose JSON error body carries extra
    keys; pair it with a return value that omits any ``"trace"`` key.
    """
    try:
        ctx = f"{request.method} {request.path}"
    except RuntimeError:  # called outside a request context (e.g. a unit test)
        ctx = "<no request context>"
    _log.error("admin request failed (%s): %s", ctx, exc, exc_info=exc)


def error_response(
    exc: BaseException,
    status: int = 500,
    *,
    ok: bool | None = None,
) -> Response:
    """Log ``exc`` server-side and return a client-safe JSON ``Response``.

    The response body is ``{"error": str(exc)}`` — the same operator-facing
    message the UI already shows — optionally wrapped with ``"ok": <bool>`` for
    endpoints that use the ``{"ok": ...}`` envelope. The traceback is logged via
    :func:`log_request_error` and is **never** serialized to the client.

    A ready ``Response`` (with ``status_code`` already set) is returned rather
    than a ``(body, status)`` tuple so call sites annotated ``-> Response`` keep
    that contract.
    """
    log_request_error(exc)
    payload: dict[str, object] = {"error": str(exc)}
    if ok is not None:
        payload["ok"] = ok
    resp = jsonify(payload)
    resp.status_code = status
    return resp
