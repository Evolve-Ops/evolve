"""chat_upload_routes — chat-surface attachment uploads (PWA Phase 1.1.B).

Backs the drag-and-drop / paste handlers on the three desktop chat
surfaces enumerated in internal/spec-pwa-2026-05-18.md §5.4:

  • Evo chat drawer  (surface = "evo-drawer")
  • Home/Chat page   (surface = "home-chat")
  • Diagnostics card (surface = "diagnostics")

The frontend's pwa-drop primitive POSTs files here as multipart, then
includes the returned metadata in its chat-send body so the chat
endpoint stays JSON. Decoupling the upload step keeps /api/home/chat
unchanged and lets the Diagnostics surface — which has no chat-send —
share the same plumbing.

Endpoints:

    POST /api/chat-uploads
        body: multipart/form-data
          surface = "evo-drawer" | "home-chat" | "diagnostics"
          file    = one or more file parts (form field repeated)
        response: {attachments: [{url, filename, size_bytes, mime_type, ...}]}

    GET  /chat-uploads/<surface>/<date>/<basename>
        Streams a previously-uploaded file. 404 outside the upload root.

Storage layout (mirrors the snap pattern used elsewhere under shared_dir):

    {shared_dir}/chat-uploads/{surface}/{YYYY-MM-DD}/{uuid}-{safe-filename}

The ``evolve`` user owns ``{shared_dir}`` already (CLAUDE.md), so writes
go directly via Path.write_bytes — no /tmp staging or sudo needed. The
``/tmp + sudo /bin/cp`` pattern in CLAUDE.md is for bot-owned files.

Server-side enforcement (defense in depth — the JS path already
validates, but a hand-crafted curl could bypass it):

  * Allowlist of mime types (images + text + JSON, v1 scope).
  * 10 MB per-file cap.
  * Filename sanitized to ``[A-Za-z0-9._-]`` (strips path components
    and unicode quirks).
  * ``surface`` constrained to a fixed three-name allowlist so a
    malicious value can't escape the upload root.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.datastructures import FileStorage

log = logging.getLogger(__name__)


# Per-file cap matches internal/spec-pwa-2026-05-18.md §5.4. Screenshots and
# typical log slices fit comfortably; anything larger is a sign the
# operator should use scp/rsync rather than the chat surface.
MAX_BYTES = 10 * 1024 * 1024

# v1 type allowlist. Matches the JS-side ``PWA_DROP_ALLOWED`` set so a
# hand-crafted multipart upload can't sneak past the client-side guard.
ALLOWED_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "application/json",
})

# Explicit surface allowlist — the path component lands on disk as a
# directory name, so we reject anything outside this set rather than
# trusting client-supplied strings. Adding a new surface = touch one
# line here AND one line in the JS primitive.
ALLOWED_SURFACES = frozenset({"evo-drawer", "home-chat", "diagnostics"})

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_LEN = 96


def register_chat_upload_routes(app: Flask, network_path: Path) -> None:
    """Mount /api/chat-uploads + /chat-uploads/<...>."""
    from ..config import DEFAULT_SHARED_DIR, load_network

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir") or DEFAULT_SHARED_DIR
        )

    def _upload_root() -> Path:
        return _shared_dir() / "chat-uploads"

    @app.post("/api/chat-uploads")
    def api_chat_uploads():
        surface = (request.form.get("surface") or "").strip()
        if surface not in ALLOWED_SURFACES:
            return jsonify({
                "error": "invalid surface",
                "allowed": sorted(ALLOWED_SURFACES),
            }), 400

        # Flask exposes repeated ``name="file"`` parts via getlist; also
        # accept the field literally named ``files`` for clients that
        # use the plural convention.
        files: list[FileStorage] = (
            list(request.files.getlist("file"))
            + list(request.files.getlist("files"))
        )
        files = [f for f in files if f and f.filename]
        if not files:
            return jsonify({"error": "no files supplied"}), 400

        date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dest_dir = _upload_root() / surface / date_dir
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("chat-upload mkdir failed: %s", exc)
            return jsonify({
                "error": "upload directory unavailable",
                "detail": str(exc),
            }), 500

        attachments: list[dict] = []
        errors: list[dict] = []
        for upload in files:
            try:
                meta = _store_one(upload, dest_dir, surface, date_dir)
            except _UploadRejected as rej:
                errors.append({
                    "filename": upload.filename,
                    "error": rej.reason,
                })
                continue
            except OSError as exc:
                log.warning(
                    "chat-upload write failed for %s: %s",
                    upload.filename, exc,
                )
                errors.append({
                    "filename": upload.filename,
                    "error": "write failed",
                })
                continue
            attachments.append(meta)

        payload: dict = {"attachments": attachments}
        if errors:
            payload["errors"] = errors
        # Partial-success returns 200 so the chips that DID upload still
        # land — the JS path reads errors[] to surface per-file toasts.
        # If nothing landed at all, treat the whole call as a 400.
        status = 200 if attachments else 400
        return jsonify(payload), status

    @app.get("/chat-uploads/<surface>/<date>/<basename>")
    def serve_chat_upload(surface: str, date: str, basename: str):
        """Stream a previously-uploaded file.

        Path components are restricted on the JS write path so they
        round-trip safely; we still re-validate here because this
        endpoint is unauthenticated at the surface level (the admin
        server itself sits behind the operator's tailnet auth).
        """
        if surface not in ALLOWED_SURFACES:
            return jsonify({"error": "not found"}), 404
        if not _SAFE_DATE_RE.fullmatch(date):
            return jsonify({"error": "not found"}), 404
        if not _SAFE_BASENAME_RE.fullmatch(basename):
            return jsonify({"error": "not found"}), 404
        directory = _upload_root() / surface / date
        if not directory.is_dir():
            return jsonify({"error": "not found"}), 404
        # send_from_directory itself rejects ``..`` traversal, but the
        # regex checks above already constrain basename to a single
        # segment with no slashes.
        return send_from_directory(directory, basename, as_attachment=False)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


class _UploadRejected(Exception):
    """One file rejected — caller records the per-file error and moves on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_SAFE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


def _store_one(
    upload: FileStorage,
    dest_dir: Path,
    surface: str,
    date_dir: str,
) -> dict:
    """Validate + persist a single upload; return attachment metadata."""
    raw_name = upload.filename or ""
    safe_name = _sanitize_filename(raw_name)
    if not safe_name:
        raise _UploadRejected("filename missing or unsafe")

    mime = (upload.mimetype or "").lower().strip()
    if mime not in ALLOWED_MIME_TYPES:
        raise _UploadRejected(
            f"unsupported file type {mime or '(unknown)'}"
        )

    # ``upload.stream`` is a SpooledTemporaryFile; reading in chunks and
    # short-circuiting at the cap avoids buffering an oversized upload
    # into memory just to reject it.
    blob = _read_capped(upload.stream, MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise _UploadRejected("file too large (10 MB max)")
    if len(blob) == 0:
        raise _UploadRejected("file is empty")

    file_id = f"{uuid.uuid4().hex[:12]}-{safe_name}"
    dest = dest_dir / file_id
    dest.write_bytes(blob)
    try:
        dest.chmod(0o644)
    except OSError:
        # Non-fatal — default umask is fine.
        pass

    return {
        "id": file_id,
        "filename": safe_name,
        "size_bytes": len(blob),
        "mime_type": mime,
        "surface": surface,
        "url": f"/chat-uploads/{surface}/{date_dir}/{file_id}",
        "stored_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _sanitize_filename(name: str) -> str:
    """Strip path components, normalize, restrict to ``[A-Za-z0-9._-]``.

    Werkzeug's ``secure_filename`` is similar, but we want a
    deterministic regex we can mirror in the JS-side sanity check and
    in the GET route's basename regex.
    """
    if not name:
        return ""
    # Strip any directory components a malicious client tries to send.
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Decompose then drop non-ASCII so unicode look-alikes can't sneak
    # path-shape characters in.
    nfkd = unicodedata.normalize("NFKD", base)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = _FILENAME_SAFE_RE.sub("_", ascii_only).strip("._-")
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_FILENAME_LEN:
        # Preserve the extension when truncating so screenshots stay
        # recognisable in the chip preview.
        if "." in cleaned:
            stem, _, ext = cleaned.rpartition(".")
            keep = _MAX_FILENAME_LEN - len(ext) - 1
            if keep > 0:
                cleaned = f"{stem[:keep]}.{ext}"
            else:
                cleaned = cleaned[:_MAX_FILENAME_LEN]
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


def _read_capped(stream, cap: int) -> bytes:
    """Read up to ``cap`` bytes, returning everything available.

    Returning > MAX_BYTES (caller passes cap = MAX_BYTES + 1) is the
    signal for "over the limit"; caller checks length to reject.
    """
    chunks: list[bytes] = []
    remaining = cap
    while remaining > 0:
        chunk = stream.read(min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def describe_attachments(attachments: Iterable[dict]) -> str:
    """Render a short reference block to inline in a chat message.

    Used by /api/home/chat to make evo aware of attachments without
    having to thread binary data through the OC subprocess boundary.
    The chips are stored under shared_dir, so evo's read_file tool can
    inspect them by path if it needs to.
    """
    lines: list[str] = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        url = a.get("url") or ""
        filename = a.get("filename") or "(unnamed)"
        mime = a.get("mime_type") or ""
        size = a.get("size_bytes")
        size_str = _fmt_size(size) if isinstance(size, int) else ""
        bits = [s for s in (mime, size_str) if s]
        meta = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {filename}{meta} — {url}")
    if not lines:
        return ""
    return "[Operator attached]\n" + "\n".join(lines)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
