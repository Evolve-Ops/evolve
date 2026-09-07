"""Per-pod VAPID keypair — PWA Phase 1.2 §6.1.

Web Push servers require a VAPID (Voluntary Application Server
Identification) keypair so push services can attribute outbound pushes
to a known publisher. The private key signs each push; the public key
goes to the browser when it subscribes.

The keypair is **per-pod and stable for the life of the pod**. Rotating
it invalidates every existing push subscription (the browser stored the
old public key when it subscribed), so we generate once and persist.

On-disk shape (``{shared_dir}/push/vapid.json``)::

    {
      "version": 1,
      "private_key_pem": "-----BEGIN PRIVATE KEY-----\\n...",
      "public_key_b64url": "BNbi...",
      "subject": "mailto:evolve@pod.local",
      "created_at": "2026-05-19T..."
    }

Mode 0600 — only the file owner (the ``evolve`` user) reads the private
key. The directory is under ``{shared_dir}`` which CLAUDE.md says is
evolve-owned, so writes are plain ``Path.write_*`` with no sudo.

Lazy-init: callers use :func:`get_or_create` and never have to know
whether the file already exists. Writes use a temp-file + rename + an
exclusive O_CREAT|O_EXCL race-loser check so two concurrent first-time
reads cannot silently overwrite each other (key rotation by accident
would invalidate every browser subscription on the pod).
"""

from __future__ import annotations

import base64
import errno
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


SCHEMA_VERSION = 1
DEFAULT_SUBJECT = "mailto:evolve@pod.local"


@dataclass(frozen=True)
class VapidKeypair:
    """Resolved VAPID keypair for the running pod.

    ``private_key_pem`` is the PKCS#8 PEM-encoded P-256 private key
    (the format ``pywebpush.webpush(vapid_private_key=...)`` accepts as
    a string). ``public_key_b64url`` is the base64url-encoded
    uncompressed public point (the format the browser's
    ``pushManager.subscribe(applicationServerKey)`` accepts).
    """

    private_key_pem: str
    public_key_b64url: str
    subject: str
    created_at: str


# ── Public API ──────────────────────────────────────────────────────────────


def vapid_path(shared_dir: Path) -> Path:
    return shared_dir / "push" / "vapid.json"


def get_or_create(shared_dir: Path, *, subject: str = DEFAULT_SUBJECT) -> VapidKeypair:
    """Return the pod's VAPID keypair, generating one if none exists.

    Idempotent — repeated calls return the same key. The exclusive-create
    write means concurrent first-time callers settle on the first one to
    win the race; the loser re-reads the persisted file rather than
    generating a second key (which would orphan every subscription that
    used the first one).
    """
    existing = _try_read(shared_dir)
    if existing is not None:
        return existing

    return _generate_and_persist(shared_dir, subject=subject)


def get_public_key_b64url(shared_dir: Path) -> str:
    """Convenience: return just the public key for the
    ``GET /api/push/vapid-public-key`` route. Generates the keypair on
    first call so the frontend never sees an empty response."""
    return get_or_create(shared_dir).public_key_b64url


# ── Read ────────────────────────────────────────────────────────────────────


def _try_read(shared_dir: Path) -> VapidKeypair | None:
    """Return the persisted keypair, or ``None`` if genuinely absent /
    malformed.

    Malformed (parse error) → None so callers fall through to the
    generate path; an unparseable file mid-generation (race) is treated
    as "not there yet" and the next read will see the completed file.

    PermissionError, by contrast, **raises** — a perfectly-fine vapid.json
    that we just can't read (wrong UID, ACL drift) must NOT silently
    regenerate, because regenerating invalidates every existing browser
    subscription. Failing loud here lets the operator fix the
    ownership/ACL rather than wake up to "all my devices stopped
    receiving push."
    """
    p = vapid_path(shared_dir)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError:
        # Re-raise so the caller (and any HTTP route above it) surfaces
        # the access problem instead of silently rotating the keypair.
        raise
    except OSError:
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    pem = body.get("private_key_pem")
    pub = body.get("public_key_b64url")
    if not pem or not pub:
        return None
    return VapidKeypair(
        private_key_pem=str(pem),
        public_key_b64url=str(pub),
        subject=str(body.get("subject") or DEFAULT_SUBJECT),
        created_at=str(body.get("created_at") or ""),
    )


# ── Generate + persist ──────────────────────────────────────────────────────


def _generate_and_persist(shared_dir: Path, *, subject: str) -> VapidKeypair:
    """Generate a new P-256 keypair and persist it atomically.

    The exclusive-create step is the race guard: if a parallel caller
    already created the file between our ``_try_read`` and the create,
    we lose the race, re-read the persisted file, and return *that*
    keypair. Without this, two concurrent first-time reads would
    generate two different keys and the second overwrite would silently
    invalidate every browser subscription tied to the first key.
    """
    priv = ec.generate_private_key(ec.SECP256R1(), backend=default_backend())
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    pub = priv.public_key()
    raw_point = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = _b64url_nopad(raw_point)
    created = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    body = {
        "version": SCHEMA_VERSION,
        "private_key_pem": pem,
        "public_key_b64url": pub_b64,
        "subject": subject,
        "created_at": created,
    }

    p = vapid_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file, then atomically rename into place via O_EXCL.
    # If another process wins the race, fall back to reading their copy.
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        os.chmod(tmp, 0o600)
        try:
            # link() is the POSIX atomic "create if not exists" — unlike
            # os.replace() which overwrites, link() fails with EEXIST when
            # the target already exists. Whichever caller wins the race
            # gets the file; the rest see EEXIST and re-read.
            os.link(tmp, p)
        except FileExistsError:
            existing = _try_read(shared_dir)
            if existing is not None:
                return existing
            # Race-winner's file is malformed mid-write — this is the
            # corner case where the winner crashed between create and
            # write-complete. Treat it as "use ours" and overwrite.
            os.replace(tmp, p)
            os.chmod(p, 0o600)
        else:
            # link() succeeded — our file is the canonical one. The temp
            # path still exists as a hard link; unlink it to clean up.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            os.chmod(p, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                pass
        raise

    return VapidKeypair(
        private_key_pem=pem,
        public_key_b64url=pub_b64,
        subject=subject,
        created_at=created,
    )


# ── Encoding helper ─────────────────────────────────────────────────────────


def _b64url_nopad(raw: bytes) -> str:
    """Base64url-encode without trailing ``=`` padding.

    Web Push (RFC 8291 + JWK) uses unpadded base64url for everything —
    the browser's ``applicationServerKey`` parser rejects the padded
    form. urlsafe_b64encode emits ``+``-free ``-``/``_`` chars; we just
    strip the padding.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


__all__ = (
    "VapidKeypair",
    "vapid_path",
    "get_or_create",
    "get_public_key_b64url",
    "DEFAULT_SUBJECT",
)
