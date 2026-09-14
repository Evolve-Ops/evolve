"""gmail_send attachments (google_service) — the capability and its cage.

Field motivation: the PoC personal-assistant bot could not mail a PDF and
mis-diagnosed the gap as an OAuth-scope problem. The scope was never the
issue — ``gmail.send`` covers attachments; the tool simply never built MIME
parts. These tests pin the new capability AND the confinement that makes it
safe to expose to a bot:

  * **Attachments ride the same scope.** No new scope constant appears on
    the send path.
  * **The cage.** The daemon reads files as the evolve user, whose ACLs
    span pod secrets — so a path outside the calling bot's workspace must
    raise, including when it gets there via a symlink INSIDE the workspace.
    Without that check, one prompt-injected send exfiltrates key material.
  * **The cap.** >20MB total raises before any API call.
  * **Back-compat.** No ``attachments`` arg ⇒ the exact old single-part
    message shape.
"""
from __future__ import annotations

import base64
import sys
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import google_service  # noqa: E402


def _network(tmp_path: Path, bot_id: str = "lex") -> dict:
    # `sharedDir` is REQUIRED here, not decoration. `google_service._send_ledger_path`
    # falls back to `config.DEFAULT_SHARED_DIR` when the network dict omits it, so
    # without this line every send in this file appended a row to the REAL
    # /Users/Shared/evolve/google_send_ledger/lex.jsonl on the dev box — and read it
    # back on the next run, where the 15-minute duplicate-send guard then refused three
    # of these tests. That reads as flake rather than as pollution: it depends on when
    # the file was last run, and it is invisible on Linux CI, where /Users/Shared does
    # not exist so the read raises and the guard sees an empty ledger. A *read* fallback
    # to a production path is as host-coupled as a write one. Per-test (`tmp_path`), not
    # per-session (`tmp_path.parent`), so the tests in this file cannot collide with each
    # other either. Same shape as test_gmail_send_guards.py, which has always done this.
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
        }
    }


def _send_service_mock() -> tuple[MagicMock, MagicMock]:
    send = MagicMock()
    send.execute = MagicMock(return_value={"id": "m1", "threadId": "t1"})
    messages = MagicMock(send=MagicMock(return_value=send))
    svc = MagicMock()
    svc.users = MagicMock(
        return_value=MagicMock(messages=MagicMock(return_value=messages)))
    return svc, svc.users.return_value.messages.return_value.send


def _sent_message(send_mock) -> bytes:
    raw = send_mock.call_args.kwargs["body"]["raw"]
    return base64.urlsafe_b64decode(raw.encode())


def _run_send(tmp_workspace: Path, args: dict) -> tuple[dict, MagicMock]:
    svc, send_mock = _send_service_mock()
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service.google_auth.assert_scopes_available",
               return_value=None), \
         patch("evolve_admin.google_service._build_service", return_value=svc), \
         patch("evolve_admin.google_service._workspace_root",
               return_value=tmp_workspace.resolve()):
        result = google_service.gmail_send(
            "lex",
            {"to": "dave@example.com", "subject": "Trip", "body": "PDF attached.",
             **args},
            network=_network(tmp_workspace),
        )
    return result, send_mock


def test_attachment_lands_as_mime_part(tmp_path: Path):
    (tmp_path / "trip.pdf").write_bytes(b"%PDF-1.4 fake")
    result, send_mock = _run_send(tmp_path, {"attachments": ["trip.pdf"]})
    assert result["ok"] is True and result["attachments"] == ["trip.pdf"]
    parsed = message_from_bytes(_sent_message(send_mock))
    parts = [p for p in parsed.walk()
             if p.get_content_disposition() == "attachment"]
    assert [p.get_filename() for p in parts] == ["trip.pdf"]
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 fake"


def test_absolute_path_inside_workspace_ok(tmp_path: Path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    result, _ = _run_send(tmp_path, {"attachments": [str(f)]})
    assert result["attachments"] == ["notes.txt"]


def test_path_outside_workspace_raises(tmp_path: Path):
    outside = tmp_path.parent / "secret.json"
    outside.write_text("{}")
    with pytest.raises(ValueError, match="inside the bot's workspace"):
        _run_send(tmp_path, {"attachments": [str(outside)]})
    with pytest.raises(ValueError, match="inside the bot's workspace"):
        _run_send(tmp_path, {"attachments": ["../secret.json"]})


def test_symlink_escape_raises(tmp_path: Path):
    outside = tmp_path.parent / "dwd-key.json"
    outside.write_text("{}")
    (tmp_path / "innocent.json").symlink_to(outside)
    with pytest.raises(ValueError, match="inside the bot's workspace"):
        _run_send(tmp_path, {"attachments": ["innocent.json"]})


def test_total_size_cap(tmp_path: Path):
    (tmp_path / "big.bin").write_bytes(b"x" * (google_service.ATTACHMENT_TOTAL_CAP_BYTES + 1))
    with pytest.raises(ValueError, match="20MB"):
        _run_send(tmp_path, {"attachments": ["big.bin"]})


def test_missing_file_is_a_value_error(tmp_path: Path):
    with pytest.raises(ValueError, match="unreadable"):
        _run_send(tmp_path, {"attachments": ["ghost.pdf"]})


def test_no_attachments_keeps_single_part_shape(tmp_path: Path):
    result, send_mock = _run_send(tmp_path, {})
    assert result["attachments"] == []
    parsed = message_from_bytes(_sent_message(send_mock))
    assert not parsed.is_multipart()


def test_the_send_ledger_stays_under_tmp_path(tmp_path: Path):
    """Hermeticity, pinned so it cannot silently regress.

    The bug this replaces was invisible in two directions at once: green on Linux
    CI (no /Users/Shared, so the ledger read raises and the guard sees nothing),
    and on macOS a *delayed* failure — the run that polluted passed, and the next
    run inside the 15-minute duplicate window failed with an error about duplicate
    sends that says nothing about test isolation.

    The path is asserted as a LITERAL rather than by calling
    `google_service._send_ledger_path`, deliberately: a resolver that ignored
    `sharedDir` again would move both sides of the assertion and pass vacuously.
    """
    result, _ = _run_send(tmp_path, {})
    assert result["ok"] is True
    assert (tmp_path / "shared" / "google_send_ledger" / "lex.jsonl").is_file()
