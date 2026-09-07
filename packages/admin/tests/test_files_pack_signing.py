"""F-P.13.a — files-pack signing primitive tests.

Covers the Ed25519 sign + verify functions, the canonical-bytes
helper, key-id fingerprinting, and PEM key loaders.

These tests intentionally avoid any CLI / install integration —
that's F-P.13.b / .c work. The point here is to lock in the
crypto primitives so the rest of the F-P.13 stack can rely on
them.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.asymmetric.rsa import (
    generate_private_key as generate_rsa_key,
)

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.files_pack import (  # noqa: E402
    FilesPackFile,
    FilesPackMetadata,
    load_files_pack_metadata,
)
from evolve_admin.applications.files_pack_signing import (  # noqa: E402
    FILES_PACK_SIGNATURE_ALGO,
    FILES_PACK_SIGNATURE_VERSION,
    FilesPackSignatureError,
    compute_key_id,
    compute_signable_bytes,
    load_private_key_pem,
    load_public_key_pem,
    sign_files_pack,
    verify_files_pack_signature,
)


# ── Test helpers ────────────────────────────────────────────────────────────


def _make_metadata(**overrides) -> FilesPackMetadata:
    """Tiny metadata factory; tests override specific fields."""
    defaults = dict(
        format_version="1.0",
        snapshot_source={
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        files=[
            FilesPackFile(
                path="scripts/foo.py", mode="0644",
                sha256="0" * 64, size_bytes=10, placeholders=[],
            ),
        ],
        partial=False,
        coverage_intent="",
        signature={},
    )
    defaults.update(overrides)
    return FilesPackMetadata(**defaults)


def _keypair() -> tuple[Ed25519PrivateKey, "Ed25519PublicKey"]:
    """A fresh keypair for each test that needs one."""
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# ── compute_signable_bytes ──────────────────────────────────────────────────


def test_signable_bytes_are_deterministic():
    """Same metadata MUST produce identical bytes across calls and
    Python versions (sort_keys=True + no whitespace)."""
    m = _make_metadata()
    assert compute_signable_bytes(m) == compute_signable_bytes(m)


def test_signable_bytes_change_when_content_changes():
    """Changing any signed field produces different bytes."""
    base = _make_metadata()
    bytes_base = compute_signable_bytes(base)

    # Change format_version
    m1 = _make_metadata(format_version="1.1")
    assert compute_signable_bytes(m1) != bytes_base

    # Change a per-file sha
    m2 = _make_metadata(files=[
        FilesPackFile(
            path="scripts/foo.py", mode="0644",
            sha256="1" * 64, size_bytes=10, placeholders=[],
        ),
    ])
    assert compute_signable_bytes(m2) != bytes_base

    # Change partial flag
    m3 = _make_metadata(partial=True)
    assert compute_signable_bytes(m3) != bytes_base


def test_signable_bytes_ignore_signature_field():
    """A pre-existing signature on the metadata must NOT participate
    in the signable bytes — otherwise re-signing would be circular."""
    m1 = _make_metadata(signature={})
    m2 = _make_metadata(signature={
        "version": "1", "algo": "ed25519",
        "signer_key_id": "sha256:abc", "signed_at": "2026-06-04T00:00:00Z",
        "value": "dummy",
    })
    assert compute_signable_bytes(m1) == compute_signable_bytes(m2)


def test_signable_bytes_are_valid_json():
    """Sanity: callers can debug by decoding."""
    m = _make_metadata()
    decoded = json.loads(compute_signable_bytes(m).decode("utf-8"))
    assert decoded["format_version"] == "1.0"
    assert decoded["files"][0]["path"] == "scripts/foo.py"
    assert "signature" not in decoded


# ── compute_key_id ──────────────────────────────────────────────────────────


def test_key_id_is_stable_for_same_key():
    priv, pub = _keypair()
    assert compute_key_id(pub) == compute_key_id(pub)


def test_key_id_differs_per_key():
    _, pub_a = _keypair()
    _, pub_b = _keypair()
    assert compute_key_id(pub_a) != compute_key_id(pub_b)


def test_key_id_shape_is_sha256_hex():
    _, pub = _keypair()
    kid = compute_key_id(pub)
    assert kid.startswith("sha256:")
    hex_part = kid[len("sha256:"):]
    assert len(hex_part) == 64
    int(hex_part, 16)  # raises ValueError if not hex


# ── sign + verify round trip ────────────────────────────────────────────────


def test_round_trip_sign_then_verify():
    priv, pub = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    m_signed = _make_metadata(signature=sig)
    ok, reason = verify_files_pack_signature(m_signed, sig, pub)
    assert ok is True
    assert reason == "ok"


def test_signature_block_shape():
    priv, _ = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    assert sig["version"] == FILES_PACK_SIGNATURE_VERSION
    assert sig["algo"] == FILES_PACK_SIGNATURE_ALGO
    assert sig["signer_key_id"].startswith("sha256:")
    assert sig["signed_at"].endswith("Z")
    # value is base64-decodable to a 64-byte Ed25519 signature.
    raw = base64.b64decode(sig["value"], validate=True)
    assert len(raw) == 64


def test_two_signatures_of_same_content_both_verify():
    """Ed25519 is deterministic per RFC 8032 — two signatures of the
    same content with the same key have identical ``value`` bytes
    (signed_at differs). Both must verify regardless."""
    priv, pub = _keypair()
    m = _make_metadata()
    sig1 = sign_files_pack(m, priv)
    sig2 = sign_files_pack(m, priv)
    ok1, _ = verify_files_pack_signature(m, sig1, pub)
    ok2, _ = verify_files_pack_signature(m, sig2, pub)
    assert ok1 and ok2
    # Determinism check — same key + same content → same sig value.
    assert sig1["value"] == sig2["value"]


# ── verifier — failure modes ────────────────────────────────────────────────


def test_verify_returns_no_signature_when_absent():
    _, pub = _keypair()
    m = _make_metadata()
    ok, reason = verify_files_pack_signature(m, None, pub)
    assert ok is False
    assert reason == "no_signature"
    ok, reason = verify_files_pack_signature(m, {}, pub)
    assert reason == "no_signature"


def test_verify_returns_bad_version():
    priv, pub = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    sig["version"] = "99"
    ok, reason = verify_files_pack_signature(m, sig, pub)
    assert ok is False
    assert reason == "bad_version"


def test_verify_returns_bad_algo():
    priv, pub = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    sig["algo"] = "rsa-sha256"
    ok, reason = verify_files_pack_signature(m, sig, pub)
    assert ok is False
    assert reason == "bad_algo"


def test_verify_returns_malformed_when_required_field_missing():
    priv, pub = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    sig.pop("value")
    ok, reason = verify_files_pack_signature(m, sig, pub)
    assert ok is False
    assert reason == "malformed"


def test_verify_returns_key_mismatch_for_wrong_key():
    """A signature made by key A doesn't verify against key B — but
    the verifier surfaces the specific "key_mismatch" reason rather
    than the generic "tampered" so operators can diagnose."""
    priv_a, _ = _keypair()
    _, pub_b = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv_a)
    ok, reason = verify_files_pack_signature(m, sig, pub_b)
    assert ok is False
    assert reason == "key_mismatch"


def test_verify_returns_tampered_when_content_changed():
    """Sign metadata A, then alter the metadata after signing —
    signature still has the matching key_id but the bytes no longer
    match, so verify returns 'tampered'."""
    priv, pub = _keypair()
    m_signed = _make_metadata()
    sig = sign_files_pack(m_signed, priv)
    # Now tamper the metadata: change a per-file SHA.
    m_tampered = _make_metadata(files=[
        FilesPackFile(
            path="scripts/foo.py", mode="0644",
            sha256="1" * 64, size_bytes=10, placeholders=[],
        ),
    ])
    ok, reason = verify_files_pack_signature(m_tampered, sig, pub)
    assert ok is False
    assert reason == "tampered"


def test_verify_returns_malformed_for_unparseable_base64():
    priv, pub = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    sig["value"] = "not valid base64 !!!!"
    ok, reason = verify_files_pack_signature(m, sig, pub)
    assert ok is False
    assert reason == "malformed"


# ── PEM key loaders ─────────────────────────────────────────────────────────


def _pem_bytes(priv: Ed25519PrivateKey) -> tuple[bytes, bytes]:
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def test_load_private_key_pem_round_trip():
    priv, _ = _keypair()
    priv_pem, _ = _pem_bytes(priv)
    loaded = load_private_key_pem(priv_pem)
    assert isinstance(loaded, Ed25519PrivateKey)
    # Signing produces a valid signature against the original public key.
    m = _make_metadata()
    sig = sign_files_pack(m, loaded)
    ok, _ = verify_files_pack_signature(m, sig, priv.public_key())
    assert ok is True


def test_load_public_key_pem_round_trip():
    priv, pub = _keypair()
    _, pub_pem = _pem_bytes(priv)
    loaded = load_public_key_pem(pub_pem)
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    ok, _ = verify_files_pack_signature(m, sig, loaded)
    assert ok is True


def test_load_private_key_rejects_non_ed25519():
    """An RSA private key must NOT load — F-P.13 only supports
    Ed25519. Loader raises so operators don't accidentally produce
    an unverifiable signature."""
    rsa_priv = generate_rsa_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(FilesPackSignatureError) as exc_info:
        load_private_key_pem(rsa_pem)
    assert "Ed25519" in str(exc_info.value)


def test_load_garbage_raises():
    with pytest.raises(FilesPackSignatureError):
        load_private_key_pem(b"not a pem key")
    with pytest.raises(FilesPackSignatureError):
        load_public_key_pem(b"not a pem key")


# ── Metadata loader surfaces signature ──────────────────────────────────────


def test_loader_passes_signature_through(tmp_path: Path):
    pack = tmp_path / "files"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [],
        "signature": {
            "version": "1", "algo": "ed25519",
            "signer_key_id": "sha256:abc", "signed_at": "2026-06-04T00:00:00Z",
            "value": "dummy",
        },
    }))
    meta = load_files_pack_metadata(pack)
    assert meta is not None
    assert meta.signature["signer_key_id"] == "sha256:abc"


def test_loader_signature_defaults_to_empty(tmp_path: Path):
    """Pre-PR files-packs (no signature field) load with empty dict
    — backward-compat preserved."""
    pack = tmp_path / "files"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [],
    }))
    meta = load_files_pack_metadata(pack)
    assert meta.signature == {}


def test_loader_malformed_signature_falls_back_to_empty(tmp_path: Path):
    """If `signature` is present but not a dict, the loader
    normalizes it to {} — verifier later returns 'no_signature'."""
    pack = tmp_path / "files"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [],
        "signature": "not a dict",
    }))
    meta = load_files_pack_metadata(pack)
    assert meta.signature == {}


# ── Defensive: sign with wrong key type ─────────────────────────────────────


def test_sign_with_rsa_key_raises():
    """If a caller manages to pass an RSA private key directly, the
    signer raises clearly rather than silently producing garbage."""
    rsa_priv = generate_rsa_key(public_exponent=65537, key_size=2048)
    m = _make_metadata()
    with pytest.raises(FilesPackSignatureError):
        sign_files_pack(m, rsa_priv)  # type: ignore[arg-type]


def test_verify_with_rsa_key_raises():
    """Wrong key type on verify path raises (programmer error) rather
    than returning a protocol failure code."""
    priv, _ = _keypair()
    m = _make_metadata()
    sig = sign_files_pack(m, priv)
    rsa_priv = generate_rsa_key(public_exponent=65537, key_size=2048)
    rsa_pub = rsa_priv.public_key()
    with pytest.raises(FilesPackSignatureError):
        verify_files_pack_signature(m, sig, rsa_pub)  # type: ignore[arg-type]
