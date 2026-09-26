"""Security-hygiene guard: no Python traceback leaks to the HTTP client.

Admin route handlers historically returned
``jsonify({"error": str(e), "trace": traceback.format_exc()}), 500`` — which
shipped the full traceback (absolute file paths, internal module structure,
config hints) to the client. The sweep that introduced
``evolve_admin.web.http_errors.error_response`` removed every such site; the
traceback now goes to the server log only.

A second, same-leak-different-shape pattern embeds the traceback into an
operator-triggered job/status payload instead of an HTTP error body — e.g.
``err = f"...\n\nTrace:\n{traceback.format_exc()}"`` written into a job-status
``error`` field, or ``_log(traceback.format_exc())`` appended to a scan-status
``log`` that ``GET /scan/status`` returns. Those go through the same fix: the
traceback is logged via ``log_request_error`` and the operator-visible field
carries only the message.

This module locks that in three ways:

1. ``test_no_trace_key_in_admin_web_responses`` is an executable grep — it
   fails if any non-test source line reintroduces a ``"trace"`` response key,
   so a new endpoint can't copy the old shape back in.
2. ``test_no_format_exc_in_admin_web_source`` forbids materializing a traceback
   string (``format_exc()`` / ``format_exception()``) anywhere in the admin
   *web* layer at all — the precursor common to both leak shapes. The sanctioned
   path (``log_request_error`` / a logger with ``exc_info=``) never builds that
   string, so this can't false-positive on pure server-side logging; legitimate
   ``format_exc`` logging in non-web modules (cli, deploy) is out of scope.
3. The ``error_response`` / ``log_request_error`` unit tests prove the helper
   logs the traceback server-side and never serializes it to the body.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from flask import Flask

from evolve_admin.web.http_errors import error_response, log_request_error

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOT = _REPO_ROOT / "packages" / "admin" / "evolve_admin"
# Traceback-string materialization is only forbidden in the web layer, where
# strings flow to clients; non-web modules (cli, deploy) log server-side and
# are out of scope.
_WEB_ROOT = _SCAN_ROOT / "web"

# The sanctioned helper module documents the retired anti-pattern in its
# docstring, so it is the one file allowed to contain the literal string.
_ALLOWED = {_SCAN_ROOT / "web" / "http_errors.py"}

# A ``"trace"`` (or ``'trace'``) JSON key in a response dict. In this codebase
# that key has only ever meant "leak the traceback"; tracebacks belong in the
# server log via error_response / log_request_error, never in the body.
_TRACE_KEY = re.compile(r"""["']trace["']\s*:""")

# A ``format_exc()`` / ``format_exception()`` call — i.e. a traceback rendered
# to a string. ``format_exception_only`` (type+message, no frames/paths) is
# deliberately not matched: it is not a traceback leak.
_FORMAT_EXC = re.compile(r"\bformat_exc\s*\(|\bformat_exception\s*\(")


def test_no_trace_key_in_admin_web_responses() -> None:
    offenders: list[str] = []
    for path in _SCAN_ROOT.rglob("*.py"):
        if "tests" in path.parts or path in _ALLOWED:
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _TRACE_KEY.search(line):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}"
                )
    assert not offenders, (
        'A "trace" response key was found — do not return tracebacks to the '
        "client. Use evolve_admin.web.http_errors.error_response (logs the "
        "traceback server-side) instead:\n" + "\n".join(offenders)
    )


def test_no_format_exc_in_admin_web_source() -> None:
    """No ``format_exc()`` in the web layer — the precursor to every leak shape.

    Both the HTTP-handler leak (``{"trace": format_exc()}``) and the job/status
    leak (``err = f"...{format_exc()}"`` / ``_log(format_exc())``) start by
    rendering the traceback to a string. The sanctioned path never does that —
    it hands the live exception to ``log_request_error`` / a logger's
    ``exc_info=``, which renders server-side only. So a ``format_exc()`` call in
    ``web/`` is, by construction, only ever the start of a leak; banning it is
    stronger than tracing where the string lands and never flags pure logging.
    """
    offenders: list[str] = []
    for path in _WEB_ROOT.rglob("*.py"):
        if "tests" in path.parts or path in _ALLOWED:
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _FORMAT_EXC.search(line):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}"
                )
    assert not offenders, (
        "A traceback was rendered to a string in the admin web layer. Hand the "
        "live exception to evolve_admin.web.http_errors.log_request_error (or a "
        "logger with exc_info=) so it is recorded server-side only, and keep "
        "the operator-visible error/log field to the message:\n"
        + "\n".join(offenders)
    )


@pytest.fixture()
def _app() -> Flask:
    return Flask(__name__)


def test_error_response_body_has_no_traceback(_app: Flask) -> None:
    with _app.test_request_context("/api/thing", method="POST"):
        resp = error_response(ValueError("boom"))
    assert resp.status_code == 500
    body = resp.get_json()
    assert body == {"error": "boom"}
    assert "trace" not in body


def test_error_response_ok_envelope_and_status(_app: Flask) -> None:
    with _app.test_request_context("/api/thing"):
        resp = error_response(RuntimeError("nope"), 400, ok=False)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "nope", "ok": False}


def test_error_response_logs_traceback_server_side(
    _app: Flask, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="evolve_admin.web.http_errors"):
        with _app.test_request_context("/api/thing", method="GET"):
            error_response(ValueError("kaboom"))
    records = [r for r in caplog.records if r.name == "evolve_admin.web.http_errors"]
    assert records, "expected a server-side error log record"
    rec = records[-1]
    # The traceback rides on exc_info (the log handler renders it); it is the
    # server-side record, not the client body, that carries the detail.
    assert rec.exc_info is not None
    assert "GET /api/thing" in rec.getMessage()


def test_log_request_error_outside_request_context_is_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No request context (e.g. a background path): must not raise.
    with caplog.at_level(logging.ERROR, logger="evolve_admin.web.http_errors"):
        log_request_error(ValueError("ctxless"))
    assert any(
        "no request context" in r.getMessage()
        for r in caplog.records
        if r.name == "evolve_admin.web.http_errors"
    )
