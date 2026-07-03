"""Signed-bypass helpers for the Evolve OC plugin install.

Background — [docs/spec-plugin-install-trust-2026-06-06.md](../../docs/spec-plugin-install-trust-2026-06-06.md):
the deploy pipeline passes ``--dangerously-force-unsafe-install`` to OC's
``plugins install`` so OC 2026.6.1's hardcoded dangerous-code scanner
doesn't block our intentional ``spawn("python3", ...)`` in TurnObserver.
The flag is a blanket bypass — it trusts the path
``/Users/Shared/evolve-plugin/``, not the content. Anything writable to
that directory by the ``evolve`` user gets installed into every bot with
the scanner disabled.

This module closes that gap by anchoring the bypass to a content digest.
The build step computes a canonical sha256 over ``dist/`` and stamps it
into the deployed ``openclaw.plugin.json``. The install step recomputes
the digest and compares to the stamp. Mismatch → refuse to install,
clear actionable error ("rebuild the plugin").

The committed manifest at ``packages/plugin/openclaw.plugin.json`` is
NOT stamped — only the deployed copy at
``/Users/Shared/evolve-plugin/openclaw.plugin.json`` is. This keeps
``git status`` clean across builds and avoids the stale-committed-digest
problem (spec open question #1, resolved as "populate at deploy, not in
git").

Defense-in-depth: even after OC ships
[openclaw#89516](https://github.com/openclaw/openclaw/pull/89516) (merged
2026-06-03 — removes the install-time scanner entirely and deprecates
``--dangerously-force-unsafe-install``), the digest check still defends
against tampering of ``/Users/Shared/evolve-plugin/dist/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

DIGEST_ALGORITHM = "sha256-canonical-v1"
"""Canonical algorithm identifier.

``sha256-canonical-v1``:
1. Walk the directory recursively.
2. Filter to files only (no directories, no symlinks — symlinks would be
   a sneak path past content hashing).
3. Sort by POSIX-form relative path (stable across OSes).
4. For each file: compute sha256 of its bytes.
5. Final digest: sha256 over ``"<rel>:<hex>\\n"`` lines joined in order.

Properties:
- Stable across machines (no mtimes, no inode order).
- Order-independent in input — sort is deterministic.
- File-rename detected (relative path is part of the hashed line).
- Symlink replacement detected (symlinks are excluded; replacing a file
  with a symlink to the same content yields a different file count).
"""

_TRUST_KEY = "x-evolve-trust"
"""Manifest key for the stamped digest block.

``x-`` prefix keeps the field out of OC's manifest schema validation —
this is Evolve-private metadata that OC's config-validate ignores.
"""


def canonical_dist_digest(dist_dir: Path) -> str:
    """Compute the canonical content digest over ``dist_dir``.

    Returns ``"sha256:<hex>"``. Raises ``ValueError`` if ``dist_dir``
    is not a directory.

    Symlinks are skipped — they would let a tampered file slip past the
    digest by pointing outside the directory.
    """
    if not dist_dir.is_dir():
        raise ValueError(f"not a directory: {dist_dir}")

    # Sort by POSIX-form relative path so the order is identical on
    # APFS / ext4 / NTFS regardless of inode order.
    files = sorted(
        (
            p for p in dist_dir.rglob("*")
            # ``is_file()`` follows symlinks by default; pair with
            # ``is_symlink()`` check so we explicitly exclude links
            # even if they resolve to a real file inside the tree.
            if p.is_file() and not p.is_symlink()
        ),
        key=lambda p: p.relative_to(dist_dir).as_posix(),
    )

    h = hashlib.sha256()
    for f in files:
        rel = f.relative_to(dist_dir).as_posix()
        file_h = hashlib.sha256(f.read_bytes()).hexdigest()
        h.update(f"{rel}:{file_h}\n".encode("utf-8"))
    return "sha256:" + h.hexdigest()


def stamp_manifest_in_place(
    manifest_path: Path,
    *,
    digest: str,
    built_from_commit: str = "",
) -> None:
    """Write the trust block into ``manifest_path``.

    Uses ``/tmp`` + ``sudo /bin/cp`` so it works when the target is
    root-owned (the common case — ``build_plugin`` chowns
    ``/Users/Shared/evolve-plugin/`` to ``root:wheel`` after the sync).
    Falls back to a direct write when the target is writable by the
    current user (test fixtures, dev setups).

    Idempotent: re-stamping with the same digest is a no-op.

    Does NOT include a ``builtAt`` timestamp — deterministic stamping
    means re-running ``build_plugin()`` on unchanged source produces an
    identical manifest, which keeps any "deployed file diverged from
    expected" sanity check meaningful.
    """
    text = manifest_path.read_text()
    data = json.loads(text)
    new_trust = {
        "distDigest": digest,
        "digestAlgorithm": DIGEST_ALGORITHM,
    }
    if built_from_commit:
        new_trust["builtFromCommit"] = built_from_commit

    if data.get(_TRUST_KEY) == new_trust:
        return  # idempotent: nothing to write

    data[_TRUST_KEY] = new_trust
    new_text = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if os.access(str(manifest_path), os.W_OK):
        # Test fixture or evolve-writable path — direct write.
        manifest_path.write_text(new_text)
        return

    # Production path: target is root-owned. Stage to /tmp + sudo cp.
    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-manifest-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(manifest_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sudo cp of stamped manifest failed "
                f"(exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def verify_plugin_signature(plugin_dir: Path) -> tuple[bool, str]:
    """Verify the manifest's stamped digest matches the live ``dist/``.

    Returns ``(True, "")`` on match, ``(False, <reason>)`` on any
    failure. Never raises — caller is expected to decide whether to
    refuse-to-install or surface a different error.

    Failure modes (any of these returns ok=False):
    - manifest is missing / unreadable / not JSON
    - manifest lacks the ``x-evolve-trust.distDigest`` block (unstamped)
    - ``dist/`` is missing / unreadable
    - computed digest differs from the stamped digest

    The on-disk message is meant for an operator: it names what to do
    next ("rebuild" or "investigate"), not what the code did internally.
    """
    manifest_path = plugin_dir / "openclaw.plugin.json"
    dist_dir = plugin_dir / "dist"

    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return False, f"manifest not found at {manifest_path}"
    except (OSError, ValueError) as e:
        return False, f"could not read manifest {manifest_path}: {e}"

    trust = data.get(_TRUST_KEY) or {}
    stamped = trust.get("distDigest")
    if not stamped:
        return (
            False,
            f"manifest at {manifest_path} is not stamped "
            f"(missing {_TRUST_KEY}.distDigest); rebuild the plugin "
            f"to stamp it",
        )

    algo = trust.get("digestAlgorithm")
    if algo != DIGEST_ALGORITHM:
        return (
            False,
            f"unsupported digest algorithm {algo!r}; "
            f"expected {DIGEST_ALGORITHM!r}",
        )

    try:
        computed = canonical_dist_digest(dist_dir)
    except (OSError, ValueError) as e:
        return False, f"could not compute digest of {dist_dir}: {e}"

    if computed != stamped:
        return (
            False,
            f"digest mismatch: stamped={stamped} computed={computed}; "
            f"something modified {dist_dir} since the last build",
        )

    return True, ""
