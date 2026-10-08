"""F-P.13.b — sign-files-pack + gen-signing-key CLI tests.

Cover the operator-facing CLI surface that wraps F-P.13.a's
primitives. Exercises the round-trip: gen-signing-key produces a
keypair, sign-files-pack signs a real on-disk pack, and the
signature verifies against the public key.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import cli  # noqa: E402
from evolve_admin.applications.files_pack import (  # noqa: E402
    load_files_pack_metadata,
)
from evolve_admin.applications.files_pack_signing import (  # noqa: E402
    compute_key_id,
    load_public_key_pem,
    verify_files_pack_signature,
)


def _invoke(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def pack_on_disk(tmp_path: Path):
    """A minimal valid files-pack — manifest + one file."""
    pack = tmp_path / "files"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts/foo.py").write_text("print('foo')\n")
    os.chmod(pack / "scripts/foo.py", 0o644)
    import hashlib
    sha = hashlib.sha256((pack / "scripts/foo.py").read_bytes()).hexdigest()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [
            {"path": "scripts/foo.py", "mode": "0644",
             "sha256": sha, "size_bytes": 12, "placeholders": []},
        ],
    }))
    return pack


# ── gen-signing-key ─────────────────────────────────────────────────────────


def test_gen_signing_key_writes_two_pem_files(tmp_path: Path):
    out = tmp_path / "keys"
    res = _invoke(
        "gen-signing-key",
        "--out-dir", str(out),
    )
    assert res.exit_code == 0, res.output
    priv = out / "files-pack-signing.pem"
    pub = out / "files-pack-signing.pub.pem"
    assert priv.is_file()
    assert pub.is_file()
    assert b"PRIVATE KEY" in priv.read_bytes()
    assert b"PUBLIC KEY" in pub.read_bytes()


def test_gen_signing_key_private_key_is_0600(tmp_path: Path):
    """Private keys leak silently if their mode is wrong. Verify the
    CLI sets 0600 on the private file."""
    out = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(out))
    priv = out / "files-pack-signing.pem"
    mode = priv.stat().st_mode & 0o777
    assert mode == 0o600


def test_gen_signing_key_emits_fingerprint(tmp_path: Path):
    res = _invoke("gen-signing-key", "--out-dir", str(tmp_path / "keys"))
    assert "sha256:" in res.output


def test_gen_signing_key_refuses_to_overwrite_without_force(tmp_path: Path):
    out = tmp_path / "keys"
    res1 = _invoke("gen-signing-key", "--out-dir", str(out))
    assert res1.exit_code == 0
    priv = out / "files-pack-signing.pem"
    original = priv.read_bytes()
    res2 = _invoke("gen-signing-key", "--out-dir", str(out))
    assert res2.exit_code == 2
    assert "already exist" in res2.output
    # Original key NOT clobbered.
    assert priv.read_bytes() == original


def test_gen_signing_key_force_overwrites(tmp_path: Path):
    out = tmp_path / "keys"
    res1 = _invoke("gen-signing-key", "--out-dir", str(out))
    priv = out / "files-pack-signing.pem"
    original = priv.read_bytes()
    res2 = _invoke("gen-signing-key", "--out-dir", str(out), "--force")
    assert res2.exit_code == 0
    # A fresh keypair was written.
    assert priv.read_bytes() != original


def test_gen_signing_key_custom_name(tmp_path: Path):
    out = tmp_path / "keys"
    res = _invoke(
        "gen-signing-key", "--out-dir", str(out), "--name", "alex-2026",
    )
    assert res.exit_code == 0
    assert (out / "alex-2026.pem").is_file()
    assert (out / "alex-2026.pub.pem").is_file()


# ── sign-files-pack ─────────────────────────────────────────────────────────


def test_sign_files_pack_round_trip_against_pubkey(
    tmp_path: Path, pack_on_disk,
):
    """End-to-end: generate a key, sign a pack, verify against pub
    key. The whole F-P.13.b promise in one test."""
    keys = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(keys))
    priv_path = keys / "files-pack-signing.pem"
    pub_path = keys / "files-pack-signing.pub.pem"

    res = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
    )
    assert res.exit_code == 0, res.output
    assert "Signed" in res.output
    assert "signer_key_id" in res.output

    # Read back and verify.
    metadata = load_files_pack_metadata(pack_on_disk)
    assert metadata is not None
    assert metadata.signature
    pubkey = load_public_key_pem(pub_path.read_bytes())
    ok, reason = verify_files_pack_signature(
        metadata, metadata.signature, pubkey,
    )
    assert ok is True
    assert reason == "ok"
    # signer_key_id matches the public key's fingerprint.
    assert metadata.signature["signer_key_id"] == compute_key_id(pubkey)


def test_sign_files_pack_refuses_double_sign_without_force(
    tmp_path: Path, pack_on_disk,
):
    keys = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(keys))
    priv_path = keys / "files-pack-signing.pem"
    res1 = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
    )
    assert res1.exit_code == 0
    res2 = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
    )
    assert res2.exit_code == 2
    # rich wraps long console lines; the tmp pack path is long enough (longer
    # still under xdist's popen-gwN dirs) that the message wraps mid-phrase.
    # Normalize whitespace so the substring check is wrap-position independent.
    assert "already has a signature" in " ".join(res2.output.split())


def test_sign_files_pack_force_replaces_signature(
    tmp_path: Path, pack_on_disk,
):
    keys = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(keys))
    priv_path = keys / "files-pack-signing.pem"
    _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
    )
    sig1 = load_files_pack_metadata(pack_on_disk).signature

    res = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
        "--force",
    )
    assert res.exit_code == 0
    sig2 = load_files_pack_metadata(pack_on_disk).signature
    # Same key + same content → Ed25519 produces the same signature
    # bytes (deterministic per RFC 8032). The signed_at timestamp can
    # differ; what matters is that the second signature still verifies.
    assert sig2["signer_key_id"] == sig1["signer_key_id"]
    pub = load_public_key_pem(
        (keys / "files-pack-signing.pub.pem").read_bytes()
    )
    meta_after = load_files_pack_metadata(pack_on_disk)
    ok, reason = verify_files_pack_signature(meta_after, sig2, pub)
    assert ok is True
    assert reason == "ok"


def test_sign_files_pack_rejects_non_pack_dir(tmp_path: Path):
    """A dir without manifest.json isn't a files-pack — refuse with
    a clear error."""
    empty = tmp_path / "not-a-pack"
    empty.mkdir()
    keys = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(keys))
    res = _invoke(
        "sign-files-pack",
        "--pack-dir", str(empty),
        "--key-file", str(keys / "files-pack-signing.pem"),
    )
    assert res.exit_code == 2
    assert "No manifest.json" in res.output


def test_sign_files_pack_rejects_non_ed25519_key(
    tmp_path: Path, pack_on_disk,
):
    """Operator pointed at an RSA key — clear error, no garbage
    signature."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        generate_private_key,
    )
    rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "rsa.pem"
    key_path.write_bytes(rsa_pem)
    res = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(key_path),
    )
    assert res.exit_code == 2
    assert "Could not load private key" in res.output


def test_sign_files_pack_preserves_unknown_manifest_fields(
    tmp_path: Path, pack_on_disk,
):
    """If the manifest carries fields the FilesPackMetadata dataclass
    doesn't model (forward compat), the CLI shouldn't drop them when
    writing back."""
    manifest_path = pack_on_disk / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["future_field"] = {"experimental": "yes"}
    manifest_path.write_text(json.dumps(raw, indent=2))

    keys = tmp_path / "keys"
    _invoke("gen-signing-key", "--out-dir", str(keys))
    priv_path = keys / "files-pack-signing.pem"
    res = _invoke(
        "sign-files-pack",
        "--pack-dir", str(pack_on_disk),
        "--key-file", str(priv_path),
    )
    assert res.exit_code == 0
    after = json.loads(manifest_path.read_text())
    assert after["future_field"] == {"experimental": "yes"}
    assert "signature" in after
