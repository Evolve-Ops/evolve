"""gmail_send outbound guards — the below-LLM answer to the send storm.

Field motivation (2026-08-31, PoC PA bot): a model on a weak-schema rung
sent ~16 identical "test" emails in two minutes, each time claiming in
prose it was attaching a PDF while its tool call carried no attachments
parameter, and ignoring the ``"attachments": []`` in every result. Guards
that live in the daemon cannot be argued with by a looping model:

  * **Duplicate breaker** — the exact message (recipients + subject + body
    + attachment set) refuses to send twice inside the window; the error
    text instructs the bot to stop retrying. A CORRECTED resend (the
    attachment now actually present) digests differently and passes —
    without that property the guard would block the fix for the very
    failure it exists to catch.
  * **Rate cap** — per-bot sends per rolling window, file-backed so a
    daemon restart mid-storm does not re-arm the storm.
  * **Say-do warning** — text that claims an attachment + no attachments
    parameter ⇒ the result carries an explicit warning naming the missing
    parameter. Warn, never block: honest emails say "no attachment needed".
  * Failed sends never count against the caps (nothing external happened).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import google_service  # noqa: E402


def _network(tmp_path: Path, bot_id: str = "lex") -> dict:
    return {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            bot_id: {
                "role": "member",
                "primary_user": {"name": "Sam Riley"},
                "google_integration": {
                    "mode": "service_account_dwd",
                    "workspace_domain": "example-corp.com",
                    "subject": f"{bot_id}@example-corp.com",
                    "service_account_secret_ref": "google-sa-example-corp",
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                },
            }
        },
    }


def _svc() -> MagicMock:
    send = MagicMock()
    send.execute = MagicMock(return_value={"id": "m1", "threadId": "t1"})
    svc = MagicMock()
    svc.users = MagicMock(return_value=MagicMock(
        messages=MagicMock(return_value=MagicMock(
            send=MagicMock(return_value=send)))))
    return svc


def _send(tmp_path: Path, args: dict, *, now: float = 1000.0):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service.google_auth.assert_scopes_available",
               return_value=None), \
         patch("evolve_admin.google_service._build_service", return_value=_svc()), \
         patch("evolve_admin.google_service._workspace_root",
               return_value=ws.resolve()), \
         patch("evolve_admin.google_service.time.time", return_value=now):
        return google_service.gmail_send(
            "lex",
            {"to": "dave@example.com", "subject": "Hi", "body": "Plain note.",
             **args},
            network=_network(tmp_path),
        )


def test_duplicate_refused_within_window(tmp_path: Path):
    assert _send(tmp_path, {})["ok"] is True
    with pytest.raises(ValueError, match="duplicate send refused"):
        _send(tmp_path, {}, now=1060.0)


def test_duplicate_allowed_after_window(tmp_path: Path):
    _send(tmp_path, {})
    later = 1000.0 + google_service.SEND_DUPLICATE_WINDOW_SECONDS + 1
    assert _send(tmp_path, {}, now=later)["ok"] is True


def test_changed_content_passes(tmp_path: Path):
    _send(tmp_path, {})
    assert _send(tmp_path, {"body": "Different note."}, now=1060.0)["ok"] is True


def test_corrected_resend_with_attachment_passes(tmp_path: Path):
    """The storm case in reverse: the fix (same text, attachment finally
    present) must NOT be swallowed by the duplicate breaker."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "trip.pdf").write_bytes(b"%PDF")
    _send(tmp_path, {"body": "PDF attached."})
    result = _send(tmp_path, {"body": "PDF attached.",
                              "attachments": ["trip.pdf"]}, now=1060.0)
    assert result["attachments"] == ["trip.pdf"]


def test_rate_cap_trips(tmp_path: Path):
    for i in range(google_service.SEND_RATE_MAX):
        _send(tmp_path, {"subject": f"n{i}"}, now=1000.0 + i)
    with pytest.raises(ValueError, match="rate cap"):
        _send(tmp_path, {"subject": "one-too-many"}, now=1000.0 + 60)


def test_rate_window_rolls(tmp_path: Path):
    for i in range(google_service.SEND_RATE_MAX):
        _send(tmp_path, {"subject": f"n{i}"}, now=1000.0 + i)
    later = 1000.0 + google_service.SEND_RATE_WINDOW_SECONDS + 30
    assert _send(tmp_path, {"subject": "fresh"}, now=later)["ok"] is True


def test_failed_send_does_not_count(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        _send(tmp_path, {"attachments": ["ghost.pdf"]})  # unreadable → raises
    # The failure left no ledger row: the same content then sends fine.
    assert _send(tmp_path, {}, now=1001.0)["ok"] is True


def test_say_do_warning_when_text_claims_attachment(tmp_path: Path):
    r = _send(tmp_path, {"body": "The PDF is attached below."})
    assert "warning" in r and "attachments" in r["warning"]


def test_no_warning_for_plain_email_or_real_attachment(tmp_path: Path):
    assert "warning" not in _send(tmp_path, {})
    ws = tmp_path / "ws"
    (ws / "a.pdf").write_bytes(b"%PDF")
    r = _send(tmp_path, {"body": "Report attached.",
                         "attachments": ["a.pdf"]}, now=2000.0)
    assert "warning" not in r
