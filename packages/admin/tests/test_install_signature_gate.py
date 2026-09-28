"""F-P.13.c — install-time signature verification tests.

Covers the install dispatcher's signature gate:

  - evolve / operator-local packages skip the check
  - community + valid signature → install proceeds
  - community + missing signature → log + allow (v1 soft policy)
  - community + tampered signature → raise FilesPackSignatureRefused
  - community + signature but no public_key → raise (misconfig)
  - community + malformed public_key → raise

The refusal MUST propagate out of the dispatcher (not get swallowed
by the broad except-and-fall-through-to-LLM logic). Otherwise the
LLM-forge path would silently bypass the signature check.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.files_pack import (  # noqa: E402
    load_files_pack_metadata,
)
from evolve_admin.applications.files_pack_signing import (  # noqa: E402
    FilesPackSignatureRefused,
    sign_files_pack,
)
from evolve_admin.applications.forge_engine import (  # noqa: E402
    _maybe_install_via_files_pack,
)
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402
from evolve_admin.applications.forge_jobs import ForgeJob  # noqa: E402


# ── Fixture setup ───────────────────────────────────────────────────────────


def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _job() -> ForgeJob:
    return ForgeJob(
        job_id="j-sig", run_id="r-00000001", job_type="install",
        pkg_id="p-a1b2c3d4", app_id="comm-app",
        bot_id="personal-bot",
        pkg_version_before=None, gallery_version=None,
    )


def _manifest_for_sig_test() -> ApplicationManifest:
    """Manifest that points at the test gallery package (community)."""
    m = ApplicationManifest(
        id="comm-app", name="Community App", bot_id="personal-bot",
        pkg_id="p-a1b2c3d4",
    )
    m.files_pack = {
        "format_version": "1.0",
        "files_count": 1,
        "snapshot_source_pkg_version": "1.0",
        "sha256": "ignored-in-test",
    }
    return m


def _set_up_gallery_with_pack(
    tmp_path: Path, monkeypatch,
    *,
    package_provenance: str | None = None,
    contributor: dict | None = None,
    sign_with: Ed25519PrivateKey | None = None,
    tamper_after_sign: bool = False,
):
    """Lay out a gallery dir + a workspace dir + (optionally) sign
    the files-pack. Returns (files_pack_dir, workspace) like the
    existing _set_up_files_pack helper does.
    """
    gallery = tmp_path / "gallery"
    app = gallery / "comm-app"
    app.mkdir(parents=True)

    pkg_dict = {"pkg_id": "p-a1b2c3d4", "name": "Community App"}
    if package_provenance is not None:
        pkg_dict["provenance"] = package_provenance
    if contributor is not None:
        pkg_dict["contributor"] = contributor
    (app / "p-a1b2c3d4.json").write_text(json.dumps(pkg_dict))

    # Files-pack itself
    fp = app / "files"
    (fp / "scripts").mkdir(parents=True)
    (fp / "scripts/foo.py").write_text("print('foo')\n")
    os.chmod(fp / "scripts/foo.py", 0o644)
    foo_bytes = (fp / "scripts/foo.py").read_bytes()
    foo_sha = hashlib.sha256(foo_bytes).hexdigest()
    manifest_dict: dict = {
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-a1b2c3d4", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [
            {"path": "scripts/foo.py", "mode": "0644",
             "sha256": foo_sha, "size_bytes": len(foo_bytes),
             "placeholders": []},
        ],
    }
    (fp / "manifest.json").write_text(json.dumps(manifest_dict))

    if sign_with is not None:
        meta = load_files_pack_metadata(fp)
        sig = sign_files_pack(meta, sign_with)
        manifest_dict["signature"] = sig
        if tamper_after_sign:
            # Change a SIGNED metadata field after signing without
            # touching file content. coverage_intent is in the
            # canonical signable bytes; tampering it triggers the
            # signature verifier (NOT the integrity check, which
            # only looks at file content).
            manifest_dict["coverage_intent"] = "tampered_after_sign"
        (fp / "manifest.json").write_text(json.dumps(manifest_dict))

    workspace = tmp_path / "Users/personal-bot/.openclaw/workspace"
    workspace.mkdir(parents=True)

    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, network=None: tmp_path / "Users" / bot_id,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {"personal-bot": {}}},
    )
    return fp, workspace


# ── No-op cases (skip the gate) ─────────────────────────────────────────────


def test_evolve_package_skips_signature_check(
    tmp_path: Path, monkeypatch,
):
    """provenance=evolve → no signature check. Pre-existing builtin
    packages (no provenance field, defaults to evolve) just install."""
    files_dir, workspace = _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance=None,  # no provenance → defaults to evolve
    )
    result = _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert result is not None
    assert result.status == "complete"
    assert (workspace / "scripts/foo.py").is_file()


def test_operator_local_skips_signature_check(
    tmp_path: Path, monkeypatch,
):
    files_dir, workspace = _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="operator-local",
    )
    result = _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert result is not None
    assert result.status == "complete"


def test_community_without_signature_logs_warning_but_allows(
    tmp_path: Path, monkeypatch,
):
    """Soft v1 policy: community + no signature → install proceeds
    with a warning logged. Future flag could tighten this."""
    files_dir, workspace = _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        # No signature, no contributor.
    )
    result = _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert result is not None
    assert result.status == "complete"


# ── Verification success ───────────────────────────────────────────────────


def test_community_with_valid_signature_installs(
    tmp_path: Path, monkeypatch,
):
    """community + signed + valid pubkey → install proceeds."""
    priv = Ed25519PrivateKey.generate()
    files_dir, workspace = _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        contributor={
            "name": "Alex Developer",
            "public_key": _pub_pem(priv),
        },
        sign_with=priv,
    )
    result = _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert result is not None
    assert result.status == "complete"
    assert (workspace / "scripts/foo.py").is_file()


# ── Refusal cases (must raise) ─────────────────────────────────────────────


def test_community_with_tampered_signature_refuses(
    tmp_path: Path, monkeypatch,
):
    """community + signed + content changed after signing → REFUSE
    install. Must raise out, not fall through to LLM-forge."""
    priv = Ed25519PrivateKey.generate()
    _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        contributor={
            "name": "Alex Developer",
            "public_key": _pub_pem(priv),
        },
        sign_with=priv,
        tamper_after_sign=True,
    )
    with pytest.raises(FilesPackSignatureRefused) as exc_info:
        _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert exc_info.value.reason == "tampered"


def test_community_signature_with_wrong_pubkey_refuses(
    tmp_path: Path, monkeypatch,
):
    """signature signed by key A; contributor.public_key is key B's
    public key → key_mismatch refusal."""
    sign_key = Ed25519PrivateKey.generate()
    declared_key = Ed25519PrivateKey.generate()
    _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        contributor={
            "name": "Alex Developer",
            "public_key": _pub_pem(declared_key),
        },
        sign_with=sign_key,
    )
    with pytest.raises(FilesPackSignatureRefused) as exc_info:
        _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert exc_info.value.reason == "key_mismatch"


def test_community_signature_with_missing_pubkey_refuses(
    tmp_path: Path, monkeypatch,
):
    """signature present + no contributor.public_key → can't verify
    against anything → refuse with missing_public_key."""
    priv = Ed25519PrivateKey.generate()
    _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        contributor={"name": "Alex Developer"},  # no public_key
        sign_with=priv,
    )
    with pytest.raises(FilesPackSignatureRefused) as exc_info:
        _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert exc_info.value.reason == "missing_public_key"


def test_community_signature_with_malformed_pubkey_refuses(
    tmp_path: Path, monkeypatch,
):
    """contributor.public_key is garbage → malformed_public_key refusal.
    Operator misconfigured — better than silently allowing install."""
    priv = Ed25519PrivateKey.generate()
    _set_up_gallery_with_pack(
        tmp_path, monkeypatch,
        package_provenance="community",
        contributor={
            "name": "Alex Developer",
            "public_key": "not a valid PEM key",
        },
        sign_with=priv,
    )
    with pytest.raises(FilesPackSignatureRefused) as exc_info:
        _maybe_install_via_files_pack(_job(), _manifest_for_sig_test(), tmp_path)
    assert exc_info.value.reason == "malformed_public_key"
