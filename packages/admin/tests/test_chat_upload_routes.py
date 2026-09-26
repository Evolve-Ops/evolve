"""Unit tests for the chat-upload routes (PWA Phase 1.1.B).

Covers the server-side enforcement layer of internal/spec-pwa-2026-05-18.md §5.4:

  * 3-name surface allowlist
  * 7-type mime allowlist
  * 10 MB per-file size cap (mirrored client-side, enforced here)
  * filename sanitization (path components stripped, unicode normalized)
  * GET serving with re-validated path components

These live outside the browser smoke suite because:
(a) Firefox-on-Linux is documented in tests/browser/test_smoke.py to
    cascade after slow requests, and the 10 MB cap test is one such
    slow request — keeping the heavy-payload assertion at the
    test_client layer avoids that;
(b) the server-side guarantees don't depend on any browser engine —
    they're pure Flask/Werkzeug behaviour, so unit-test scope is the
    right home.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (str(_ADMIN), str(_ANALYZER)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def client(tmp_path):
    """Mount only the chat-upload blueprint on a tmp_path shared_dir."""
    from evolve_admin.web.chat_upload_routes import register_chat_upload_routes

    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "networkId": "test-pod",
        "sharedDir": str(shared),
        "bots": {},
        "pod": {},
        "integrations": {},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_chat_upload_routes(app, network_path)
    return app.test_client(), shared


# ── Sanitization helper ─────────────────────────────────────────────────────


def test_sanitize_filename_strips_path_components():
    from evolve_admin.web.chat_upload_routes import _sanitize_filename
    # Both POSIX and Windows separators.
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("..\\..\\Windows\\hosts") == "hosts"
    # Hidden-file leading dots are stripped (they would otherwise become
    # a dotfile in the upload root).
    assert _sanitize_filename(".bashrc") == "bashrc"


def test_sanitize_filename_normalizes_unicode():
    from evolve_admin.web.chat_upload_routes import _sanitize_filename
    # NFKD decomposes; the combining mark drops; ASCII survives.
    assert _sanitize_filename("héllo.txt") == "hello.txt"
    # Path-shape characters in unicode look-alikes can't sneak through.
    assert _sanitize_filename("a∕b.png") == "ab.png"


def test_sanitize_filename_truncates_preserving_extension():
    from evolve_admin.web.chat_upload_routes import (
        _MAX_FILENAME_LEN, _sanitize_filename,
    )
    long_stem = "a" * 200
    out = _sanitize_filename(f"{long_stem}.png")
    assert out.endswith(".png"), f"extension lost in truncation: {out!r}"
    assert len(out) <= _MAX_FILENAME_LEN, f"truncation overflow: {len(out)}"


def test_sanitize_filename_returns_empty_for_garbage():
    from evolve_admin.web.chat_upload_routes import _sanitize_filename
    # Nothing left after stripping path-shape + non-ASCII.
    assert _sanitize_filename("") == ""
    assert _sanitize_filename("...") == ""
    assert _sanitize_filename("///") == ""


# ── Reference-block formatting ──────────────────────────────────────────────


def test_describe_attachments_formats_list():
    from evolve_admin.web.chat_upload_routes import describe_attachments
    out = describe_attachments([
        {"filename": "a.png", "url": "/u/a",
         "mime_type": "image/png", "size_bytes": 12345},
        {"filename": "b.txt", "url": "/u/b",
         "mime_type": "text/plain", "size_bytes": 100},
    ])
    assert "[Operator attached]" in out
    assert "a.png (image/png, 12.1 KB) — /u/a" in out
    assert "b.txt (text/plain, 100 B) — /u/b" in out


def test_describe_attachments_empty_returns_empty_string():
    from evolve_admin.web.chat_upload_routes import describe_attachments
    assert describe_attachments([]) == ""
    # Non-dict entries are filtered defensively.
    assert describe_attachments([None, "str", 42]) == ""


# ── Happy path: PNG round-trip ──────────────────────────────────────────────


def test_chat_upload_happy_path_png(client):
    c, shared = client
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(png), "probe.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    attachments = body["attachments"]
    assert len(attachments) == 1
    a = attachments[0]
    assert a["filename"] == "probe.png"
    assert a["mime_type"] == "image/png"
    assert a["size_bytes"] == len(png)
    assert a["surface"] == "evo-drawer"
    # URL shape — pinned because the GET route depends on it.
    assert re.fullmatch(
        r"/chat-uploads/evo-drawer/\d{4}-\d{2}-\d{2}/[a-f0-9]{12}-probe\.png",
        a["url"],
    ), f"upload URL shape drifted: {a['url']!r}"
    # File is actually on disk under shared/chat-uploads/...
    stored = list((shared / "chat-uploads" / "evo-drawer").rglob("*-probe.png"))
    assert len(stored) == 1, (
        f"expected one stored file; found {stored!r}"
    )
    assert stored[0].read_bytes() == png, "stored content does not match upload"
    # GET round-trip serves it back.
    served = c.get(a["url"])
    assert served.status_code == 200
    assert served.data == png


# ── Cap enforcement — the test that would cascade browser smoke ─────────────


def test_chat_upload_rejects_oversized_file(client):
    """11 MB upload bounces with the canonical 10 MB error.

    This is the heavy-payload test moved out of browser smoke. The
    in-process Flask test client handles 11 MB without saturating an
    event loop or hitting Playwright's 30s default timeout.
    """
    c, _shared = client
    from evolve_admin.web.chat_upload_routes import MAX_BYTES
    big = b"\x00" * (MAX_BYTES + 1024)  # 1 KB over the cap
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(big), "big.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["attachments"] == []
    errors = body.get("errors") or []
    assert any("too large" in (e.get("error") or "") for e in errors), (
        f"expected size-cap error in response; got {errors!r}"
    )


def test_chat_upload_accepts_exactly_max_bytes(client):
    """A file at exactly MAX_BYTES is accepted (off-by-one guard)."""
    c, _shared = client
    from evolve_admin.web.chat_upload_routes import MAX_BYTES
    blob = b"x" * MAX_BYTES
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(blob), "right-at-cap.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["attachments"][0]["size_bytes"] == MAX_BYTES


def test_chat_upload_rejects_empty_file(client):
    c, _shared = client
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(b""), "empty.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    errors = resp.get_json().get("errors") or []
    assert any("empty" in (e.get("error") or "") for e in errors)


# ── Allowlists ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mime",
    [
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "text/plain", "text/markdown", "application/json",
    ],
)
def test_chat_upload_accepts_all_seven_allowed_types(client, mime):
    c, _shared = client
    ext = mime.split("/")[1].replace("jpeg", "jpg").replace("markdown", "md")
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(b"x"), f"probe.{ext}", mime),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, (
        f"mime {mime!r} should be accepted; got {resp.status_code} "
        f"{resp.get_data(as_text=True)}"
    )


def test_chat_upload_rejects_unsupported_type(client):
    c, _shared = client
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": "evo-drawer",
            "file": (io.BytesIO(b"x"), "a.zip", "application/zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    errors = resp.get_json().get("errors") or []
    assert any("unsupported" in (e.get("error") or "").lower() for e in errors)


@pytest.mark.parametrize("surface", ["evo-drawer", "home-chat", "diagnostics"])
def test_chat_upload_accepts_all_three_surfaces(client, surface):
    c, _shared = client
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": surface,
            "file": (io.BytesIO(b"x"), "probe.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["attachments"][0]["surface"] == surface


@pytest.mark.parametrize(
    "bad",
    ["", "..", "../etc", "unknown", "bots/team_bot_a", "evo-drawer/foo"],
)
def test_chat_upload_rejects_unknown_surface(client, bad):
    """The surface string lands on disk; anything outside the allowlist
    must 400 so a client can't escape the upload root via path traversal."""
    c, _shared = client
    resp = c.post(
        "/api/chat-uploads",
        data={
            "surface": bad,
            "file": (io.BytesIO(b"x"), "probe.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400, (
        f"surface={bad!r} should be rejected; got {resp.status_code}"
    )
    body = resp.get_json()
    assert body.get("error") == "invalid surface"


def test_chat_upload_with_no_files_returns_400(client):
    c, _shared = client
    resp = c.post(
        "/api/chat-uploads",
        data={"surface": "evo-drawer"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json().get("error") == "no files supplied"


def test_chat_upload_multiple_files_in_one_request(client):
    """The route accepts repeated ``file`` parts in one multipart body."""
    c, _shared = client
    data = MultiDict()
    data.add("surface", "evo-drawer")
    data.add("file", (io.BytesIO(b"a"), "a.txt", "text/plain"))
    data.add("file", (io.BytesIO(b"b"), "b.txt", "text/plain"))
    resp = c.post(
        "/api/chat-uploads", data=data, content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    names = [a["filename"] for a in resp.get_json()["attachments"]]
    assert names == ["a.txt", "b.txt"]


def test_chat_upload_partial_success_returns_200(client):
    """One good + one bad → 200 with the good attachment and the bad in
    errors[]. Operator's UI gets to surface the per-file rejection
    while the rest of the chips land normally."""
    c, _shared = client
    data = MultiDict()
    data.add("surface", "evo-drawer")
    data.add("file", (io.BytesIO(b"ok"), "ok.txt", "text/plain"))
    data.add("file", (io.BytesIO(b"bad"), "bad.zip", "application/zip"))
    resp = c.post(
        "/api/chat-uploads", data=data, content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert [a["filename"] for a in body["attachments"]] == ["ok.txt"]
    errors = body.get("errors") or []
    assert any(e.get("filename") == "bad.zip" for e in errors)


# ── GET-route hardening ─────────────────────────────────────────────────────


def test_serve_chat_upload_rejects_path_traversal(client):
    """Basename regex prevents ``..`` and other multi-segment shapes."""
    c, _shared = client
    # Unknown surface → 404.
    assert c.get("/chat-uploads/unknown/2026-05-20/a.png").status_code == 404
    # Bad date format → 404.
    assert c.get("/chat-uploads/evo-drawer/13-99-foo/a.png").status_code == 404
    # Werkzeug normalizes ``..`` in the URL before routing, so we can't
    # send a literal ``..``; but the basename regex is the ultimate
    # gate. Probe with a basename character outside [A-Za-z0-9._-].
    assert c.get(
        "/chat-uploads/evo-drawer/2026-05-20/has%20space.png"
    ).status_code == 404
