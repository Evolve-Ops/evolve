"""files_pack_signing.py — Ed25519 sign/verify for files-pack metadata.

Spec: internal/note-smart-forge-and-file-provenance-2026-06-04.md
(F-P.13 — provenance-axis follow-on).

This is the foundation layer for community-package signing. It
exposes the two primitives the rest of the F-P.13 stack will build
on:

  sign_files_pack(metadata, private_key) -> dict
      Builds a signature block for the metadata (a parsed
      ``files_pack/manifest.json``). Caller is responsible for
      writing the block back into the manifest and persisting.

  verify_files_pack_signature(metadata, public_key) -> (ok, reason)
      Checks whether the signature on the metadata was produced by
      the holder of ``public_key``. Returns a (bool, string) so
      callers can surface a specific reason ("no_signature",
      "tampered", "bad_algo", "ok") to operators.

What's signed
─────────────

The signature covers a CANONICAL byte representation of the
files-pack metadata MINUS the signature itself. Specifically:

  json.dumps({
      "format_version":   metadata.format_version,
      "snapshot_source":  metadata.snapshot_source,
      "files":            [<each entry as dict>],
      "partial":          metadata.partial,
      "coverage_intent":  metadata.coverage_intent,
  }, sort_keys=True, separators=(",", ":")).encode("utf-8")

The deterministic JSON encoding (sorted keys, no whitespace)
ensures the bytes are identical across systems. The per-file
``sha256`` values in files[] are the integrity root for file
content; signing the SHAs (not the file bytes) keeps the signature
constant-size regardless of pack size.

Signature block shape (lives at ``metadata.signature``)
───────────────────────────────────────────────────────

  {
      "version":       "1",
      "algo":          "ed25519",
      "signer_key_id": "sha256:<hex>",   # SHA-256 of raw public-key bytes
      "signed_at":     "2026-06-04T...",  # ISO-8601 UTC
      "value":         "<base64 signature>",
  }

The ``signer_key_id`` lets verifiers confirm "this signature was
made with the key whose fingerprint is X" without needing the full
public key up front. The actual verification still needs the public
key — typically pulled from the package manifest's
``contributor.public_key`` (F-P.12.d's forward-compat slot).

What's NOT in scope here
────────────────────────

  - Key generation / management (F-P.13.b CLI handles that)
  - Persistence (caller writes the block back to manifest.json)
  - Install-path enforcement (F-P.13.c wires the verifier into
    forge_engine's install dispatcher)
  - UI rendering of verification status (F-P.13.d)

Pure data + crypto. Easy to test, easy to vendor into other tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .files_pack import FilesPackMetadata


__all__ = (
    "FILES_PACK_SIGNATURE_VERSION",
    "FILES_PACK_SIGNATURE_ALGO",
    "compute_signable_bytes",
    "compute_key_id",
    "sign_files_pack",
    "verify_files_pack_signature",
    "load_private_key_pem",
    "load_public_key_pem",
    "FilesPackSignatureError",
)


# Signature-block format identifiers. Bump version when the canonical
# signable-byte representation changes; the algo field future-proofs
# for a non-ed25519 signature scheme.
FILES_PACK_SIGNATURE_VERSION = "1"
FILES_PACK_SIGNATURE_ALGO = "ed25519"


class FilesPackSignatureError(Exception):
    """Raised by sign_files_pack for malformed inputs.

    Verifier path returns ``(False, reason)`` instead of raising;
    invalid signatures aren't exceptional — they're the protocol's
    primary failure mode. Only true programmer errors (wrong key
    type passed in, etc.) raise.
    """


class FilesPackSignatureRefused(Exception):
    """Raised by the install dispatcher when a community package's
    signature fails verification (F-P.13.c).

    Distinct from ``FilesPackSignatureError`` (which is for malformed
    inputs to the primitives). Distinct from ``FilesPackError`` /
    ``FilesPackIntegrityError`` (which the dispatcher catches and
    converts to "fall through to LLM-forge"). Signature refusal is
    a SECURITY-RELEVANT install failure — it should propagate out
    of the dispatcher and abort the install, NOT fall through to
    the LLM path (which would silently bypass the security check).

    Carries the verification reason ("tampered", "key_mismatch",
    etc.) so the caller can surface a specific operator message.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"signature refused: {reason}: {detail}".rstrip(": "))


# ── Canonical signable bytes ────────────────────────────────────────────────


def compute_signable_bytes(metadata: FilesPackMetadata) -> bytes:
    """Build the deterministic bytes the signature covers.

    Identical for any equivalent metadata across systems / JSON
    formatters thanks to ``sort_keys=True`` + ``separators=(",", ":")``.
    Excludes any pre-existing signature block on the metadata so
    re-signing produces a different output if and only if the
    underlying contents change.
    """
    payload: dict[str, Any] = {
        "format_version": metadata.format_version,
        "snapshot_source": metadata.snapshot_source,
        "files": [
            {
                "path": f.path,
                "mode": f.mode,
                "sha256": f.sha256,
                "size_bytes": f.size_bytes,
                "placeholders": list(f.placeholders),
            }
            for f in metadata.files
        ],
        "partial": bool(metadata.partial),
        "coverage_intent": metadata.coverage_intent or "",
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


# ── Key-id (public-key fingerprint) ─────────────────────────────────────────


def compute_key_id(public_key: Ed25519PublicKey) -> str:
    """Return a ``"sha256:<hex>"`` fingerprint of the public key.

    Lets a verifier confirm a signature came from a specific known
    key without round-tripping the whole key — useful when the
    public key lives elsewhere (e.g. ``contributor.public_key`` in
    the package manifest) and the operator wants quick visual
    confirmation.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ── PEM key loaders ─────────────────────────────────────────────────────────


def load_private_key_pem(pem_bytes: bytes) -> Ed25519PrivateKey:
    """Load an unencrypted Ed25519 private key from PEM bytes.

    Raises ``FilesPackSignatureError`` for any load failure or for
    a non-Ed25519 key (e.g. operator pointed at an RSA file).
    """
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        raise FilesPackSignatureError(
            f"could not load PEM private key: {exc}"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise FilesPackSignatureError(
            f"PEM key is {type(key).__name__}, expected Ed25519PrivateKey. "
            f"F-P.13 only supports Ed25519."
        )
    return key


def load_public_key_pem(pem_bytes: bytes) -> Ed25519PublicKey:
    """Load an Ed25519 public key from PEM bytes.

    Same constraints as ``load_private_key_pem``. Operator-facing
    flows typically read this from the package manifest's
    ``contributor.public_key`` field.
    """
    try:
        key = serialization.load_pem_public_key(pem_bytes)
    except Exception as exc:
        raise FilesPackSignatureError(
            f"could not load PEM public key: {exc}"
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise FilesPackSignatureError(
            f"PEM key is {type(key).__name__}, expected Ed25519PublicKey."
        )
    return key


# ── Sign + verify ───────────────────────────────────────────────────────────


def sign_files_pack(
    metadata: FilesPackMetadata,
    private_key: Ed25519PrivateKey,
) -> dict:
    """Sign the metadata with the given private key.

    Returns the signature block (a dict) — caller is responsible for
    writing it onto the manifest and persisting. Doesn't mutate
    ``metadata``.

    The returned shape:

      {
          "version":       FILES_PACK_SIGNATURE_VERSION,
          "algo":          FILES_PACK_SIGNATURE_ALGO,
          "signer_key_id": "sha256:<hex>",
          "signed_at":     "2026-06-04T...",
          "value":         "<base64>",
      }

    A second call to ``sign_files_pack`` with the same key + same
    metadata produces an IDENTICAL signature value (Ed25519 is
    deterministic per RFC 8032 — the per-message nonce is derived
    from the key + message, not random). The ``signed_at``
    timestamp differs between calls. Test by verifying, not by
    string-comparing the ``value`` field, in case a future algo
    changes that assumption.
    """
    if not isinstance(private_key, Ed25519PrivateKey):
        raise FilesPackSignatureError(
            f"private_key must be Ed25519PrivateKey, got {type(private_key).__name__}"
        )
    payload = compute_signable_bytes(metadata)
    sig_bytes = private_key.sign(payload)
    pubkey = private_key.public_key()
    return {
        "version": FILES_PACK_SIGNATURE_VERSION,
        "algo": FILES_PACK_SIGNATURE_ALGO,
        "signer_key_id": compute_key_id(pubkey),
        "signed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value": base64.b64encode(sig_bytes).decode("ascii"),
    }


def verify_files_pack_signature(
    metadata: FilesPackMetadata,
    signature: dict | None,
    public_key: Ed25519PublicKey,
) -> tuple[bool, str]:
    """Verify the metadata's signature against the given public key.

    Returns ``(ok, reason)`` where reason is one of:

      "ok"             → signature is valid
      "no_signature"   → no signature block provided
      "bad_version"    → unknown signature.version
      "bad_algo"       → signature.algo not in supported set
      "malformed"      → signature block missing required fields
      "key_mismatch"   → signer_key_id doesn't match the given public_key
      "tampered"       → signature is structurally valid but doesn't
                         verify against the metadata bytes (content
                         drift OR wrong key used to sign)

    The verifier never raises for protocol failures — operators see
    a specific reason and can act accordingly. Only programmer
    errors (wrong public_key type) raise.
    """
    if not isinstance(public_key, Ed25519PublicKey):
        raise FilesPackSignatureError(
            f"public_key must be Ed25519PublicKey, got {type(public_key).__name__}"
        )
    if not signature:
        return False, "no_signature"
    if signature.get("version") != FILES_PACK_SIGNATURE_VERSION:
        return False, "bad_version"
    if signature.get("algo") != FILES_PACK_SIGNATURE_ALGO:
        return False, "bad_algo"

    required = ("signer_key_id", "value", "signed_at")
    if any(not signature.get(k) for k in required):
        return False, "malformed"

    expected_key_id = compute_key_id(public_key)
    if signature.get("signer_key_id") != expected_key_id:
        return False, "key_mismatch"

    try:
        sig_bytes = base64.b64decode(signature["value"], validate=True)
    except Exception:
        return False, "malformed"

    payload = compute_signable_bytes(metadata)
    try:
        public_key.verify(sig_bytes, payload)
    except InvalidSignature:
        return False, "tampered"
    except Exception:
        # Anything else is structural — treat as malformed so the
        # operator sees a clear failure mode rather than a stack trace.
        return False, "malformed"
    return True, "ok"
