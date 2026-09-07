"""Content-integrity helpers for the Evolve OC plugin install.

Background — [internal/spec-plugin-install-trust-2026-06-06.md](../../internal/spec-plugin-install-trust-2026-06-06.md):
OC installs this plugin from the local path
``/Users/Shared/evolve-plugin/`` and vouches for the *path*, not its
*content*. Whatever is in that directory at install time is what lands in
every bot. **This module is the only content check anywhere on that
path** — it does not backstop, and is not conditioned on, any OC flag or
scanner state.

Historical note, because the framing misled once already: this started as
a "signed bypass" for OC 2026.6.1's install-time dangerous-code scanner,
which flagged TurnObserver's intentional ``spawn("python3", ...)``. OC
2026.7 deleted that scanner and made
``--dangerously-force-unsafe-install`` a no-op; Evolve stopped passing it
in 2026-08. None of that weakens the gate — OC would not vouch for a
mutable local path regardless of what it scans, so the justification here
never expired. See the spec's §1.4 and §4.

This module anchors the install to a content digest.
The build step computes canonical sha256 digests over the staged install
tree and stamps them into the deployed ``openclaw.plugin.json``. The
install step recomputes and compares. Mismatch → refuse to install, clear
actionable error ("rebuild the plugin").

Two digests, for rollout reasons rather than design ones:
``distDigest`` covers ``dist/`` and is what shipped first; ``treeDigest``
covers the whole staged tree — ``dist/`` **and** ``node_modules/`` (which
is executable code the gateway will not load the plugin without),
``package.json`` / ``package-lock.json``, and the manifest's own content
(``main``, ``hooks``, ``contracts``, ``configSchema``). See
:data:`TREE_DIGEST_ALGORITHM` and :data:`REQUIRE_TREE_DIGEST`, and the
spec's §4.1. Once every pod has re-stamped, ``distDigest`` is redundant.

The committed manifest at ``packages/plugin/openclaw.plugin.json`` is
NOT stamped — only the deployed copy at
``/Users/Shared/evolve-plugin/openclaw.plugin.json`` is. This keeps
``git status`` clean across builds and avoids the stale-committed-digest
problem (spec open question #1, resolved as "populate at deploy, not in
git").

[openclaw#89516](https://github.com/openclaw/openclaw/pull/89516) (merged
2026-06-03) removed the install-time scanner and deprecated the flag; the
2026.7 runtime threads ``dangerouslyForceUnsafeInstall`` through the
install call chain without ever reading it. The digest check is
unaffected — it defends the install tree on its own terms.
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

TREE_DIGEST_ALGORITHM = "sha256-canonical-tree-v1"
"""Canonical algorithm identifier for the whole-install-tree digest.

Covers everything ``build_plugin()`` copies into the install directory:
``dist/`` **and** ``node_modules/`` (both recursive), plus
``package.json``, ``package-lock.json``, and the manifest — the last by
*content* rather than bytes, so the stamp write does not invalidate it
(see :data:`MANIFEST_NAME`).

``sha256-canonical-tree-v1`` differs from ``sha256-canonical-v1`` in
three ways, all deliberate:

1. **Relative paths are rooted at the plugin dir**, not at ``dist/`` — so
   ``dist/index.js`` and ``node_modules/x/index.js`` are distinguishable
   and the top-level JSON files have a place in the line set.
2. **Symlinks are recorded by target rather than skipped.** ``dist``-v1
   drops them, which leaves ``node_modules/.bin/*`` (npm writes those as
   links) as an uncovered rewrite surface. Here a link contributes a
   ``l``-typed line carrying its raw target string, so repointing one at
   something outside the tree is a digest change.
3. **NUL-delimited, type-tagged lines.** ``f\\0<rel>\\0<sha256hex>\\n`` for
   regular files, ``l\\0<rel>\\0<target>\\n`` for symlinks, and
   ``m\\0<rel>\\0<sha256hex>\\n`` for the manifest's stripped content.
   POSIX paths cannot contain NUL, so no (rel, value) pair can be
   re-parsed as a different pair — a colon-joined format admits collisions
   once the value side is attacker-influenced (a symlink target is).

A member that does not exist contributes no lines. That is not a hole:
deleting ``node_modules/`` removes every one of its lines and changes the
digest.

Two known limits, inert today but worth knowing: an unenumerated
top-level path is not hashed (deliberate — hashing "everything in the
directory" would let an OS-dropped ``.DS_Store`` fail every install), and
``rglob`` does not descend symlinked *directories*, so content behind a
symlinked dependency dir is represented only by its link target. Nothing
unhashed is reachable unless something covered points at it, and every
pointer (``main``, ``hooks``, ``contracts``) is covered.
"""

MANIFEST_NAME = "openclaw.plugin.json"
"""The deployed manifest.

It carries the stamp, so it cannot be hashed as raw bytes — the write
would invalidate the value it just wrote. It is covered by *content*
instead: :func:`_manifest_content_digest` hashes a canonical
re-serialization with the ``x-evolve-trust`` key removed, which is stable
across stamping and still pins ``main``, ``hooks``, ``contracts`` and
``configSchema``. Excluding the file outright would leave the plugin's
entrypoint repointable at an attacker-dropped file with both digests
still verifying clean.
"""

INSTALL_TREE_DIRS: tuple[str, ...] = ("dist", "node_modules")
"""Directories under the install dir that the tree digest walks."""

INSTALL_TREE_FILES: tuple[str, ...] = ("package.json", "package-lock.json")
"""Top-level files under the install dir that the tree digest covers.

``deploy.build_plugin()`` imports this to drive its own copy loop, so the
set of files copied into the install tree and the set covered by the
digest cannot drift apart by editing one and forgetting the other.
"""

REQUIRE_TREE_DIGEST = False
"""Staged-rollout switch for the ``treeDigest`` field. **Read before flipping.**

``False`` (this release): a manifest carrying ``distDigest`` but no
``treeDigest`` verifies OK and returns a warning string. ``True``: the
missing field is a verification failure, same as a missing ``distDigest``.

Why this cannot ship as ``True``. Verification is fail-closed, so a newly
required field breaks every pod whose install tree was stamped by an
older build — pod-wide and fleet-wide — unless a rebuild is guaranteed to
precede every install. It is not. Two paths reach
``deploy.install_oc_plugin`` with no ``build_plugin()`` ahead of them:

- ``provisioning._call_deploy_bot`` — the add-bot / onboarding stage-6
  path. ``provisioning.py`` never calls ``build_plugin``.
- ``web.server``'s Upgrade job when ``skipPlugin`` is set: the rebuild
  step is skipped, the per-bot install loop still runs.

(``cli._full_deploy``, the fresh-setup wizard's ``_deploy_evolve``, and
the puller's ``_rebuild_plugin`` all do rebuild first.) The puller only
rebuilds when the pulled diff touches ``packages/plugin/``, so a pod can
run for a long time on an old stamp without one.

**Flip criterion** — not a date: flip once every pod in the fleet has
completed one full ``sudo evolve-admin upgrade`` (or ``deploy``) on a
build at or after the commit that introduced this constant, which is what
re-stamps the manifest. The fleet view is the
``plugin_stamp_legacy`` Signal that ``install_integrity_monitor`` emits per
pod on its daily sweep: **flip only when no pod has that Signal open.**
``builtFromCommit`` in a deployed manifest still answers it for one pod by
hand.

**When you flip it**, exactly two tests in
``tests/test_plugin_signature.py`` go red, and both are meant to:
``test_ships_permissive`` (asserts the constant is ``False`` — delete or
invert it) and ``test_tamper_fails_closed_even_in_stage_one`` (whose
precondition asserts the same — drop the precondition, keep the test).
Do **not** touch ``test_legacy_stamp_passes_with_warning`` /
``test_legacy_stamp_rejected_once_required``: they monkeypatch the
constant, pass in either state, and are the only coverage of the two
stages.

**Note for whoever bumps the tree algorithm to a v2.** The two branches
are asymmetric on purpose but the asymmetry bites on rollback: an
*absent* ``treeDigest`` warns, an *unrecognized* ``treeDigestAlgorithm``
hard-fails. A pod stamped by a v2 build and then rolled back (``release
rollback``/``pin`` reverts ``packages/admin`` without touching
``packages/plugin``, so the puller does not rebuild) would refuse every
install with no re-stamp on the way back. A v2 needs either a
recognize-but-warn window for the newer id, or a forced rebuild on
rollback.
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


def _manifest_content_digest(manifest_path: Path) -> str:
    """sha256 over the manifest's content with the trust block removed.

    Canonicalized by parse-and-re-serialize, so it is invariant to the
    formatting change ``stamp_manifest_in_place`` makes when it rewrites
    the file — the stamp changes the bytes, not this value. That is what
    lets the manifest be inside the digest it carries.

    Raises ``ValueError`` if the manifest is missing or not JSON. The
    caller turns that into a verification failure, which is correct: an
    unreadable manifest is already a fail-closed condition.
    """
    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError as e:
        raise ValueError(f"manifest not found: {manifest_path}") from e
    except (OSError, ValueError) as e:
        raise ValueError(f"could not read manifest {manifest_path}: {e}") from e
    if isinstance(data, dict):
        data.pop(_TRUST_KEY, None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_install_tree_digest(plugin_dir: Path) -> str:
    """Compute the canonical content digest over the whole install tree.

    Covers ``dist/`` and ``node_modules/`` recursively plus the top-level
    ``package.json`` / ``package-lock.json`` — everything ``build_plugin()``
    stages into the install directory except the manifest that carries the
    stamp. See :data:`TREE_DIGEST_ALGORITHM` for the line format and for
    why symlinks are recorded rather than skipped.

    Returns ``"sha256:<hex>"``. Raises ``ValueError`` if ``plugin_dir``
    is not a directory. Absent members contribute no lines.
    """
    if not plugin_dir.is_dir():
        raise ValueError(f"not a directory: {plugin_dir}")

    entries: list[Path] = []
    for name in INSTALL_TREE_DIRS:
        sub = plugin_dir / name
        # ``is_dir()`` follows symlinks — a symlinked ``node_modules`` is
        # recorded as a link below, not walked as a directory.
        if sub.is_dir() and not sub.is_symlink():
            entries.extend(sub.rglob("*"))
        elif sub.is_symlink():
            entries.append(sub)
    for name in INSTALL_TREE_FILES:
        f = plugin_dir / name
        if f.is_symlink() or f.is_file():
            entries.append(f)

    # Directories carry no content of their own; their members are already
    # in the walk. Sort by POSIX-form relative path so the order is identical
    # on APFS / ext4 / NTFS regardless of inode order.
    hashable = sorted(
        (p for p in entries if p.is_symlink() or p.is_file()),
        key=lambda p: p.relative_to(plugin_dir).as_posix(),
    )

    h = hashlib.sha256()
    for p in hashable:
        rel = p.relative_to(plugin_dir).as_posix().encode("utf-8")
        if p.is_symlink():
            # Raw target string, not the resolved path — repointing the
            # link is what we are trying to detect.
            h.update(b"l\x00" + rel + b"\x00"
                     + os.readlink(str(p)).encode("utf-8") + b"\n")
        else:
            h.update(b"f\x00" + rel + b"\x00"
                     + hashlib.sha256(p.read_bytes()).hexdigest().encode("ascii")
                     + b"\n")

    # The manifest, by content rather than by bytes — see MANIFEST_NAME.
    # ``m`` is a third line type, so it cannot collide with an ``f`` line for
    # a file that happens to be named openclaw.plugin.json.
    h.update(b"m\x00" + MANIFEST_NAME.encode("utf-8") + b"\x00"
             + _manifest_content_digest(plugin_dir / MANIFEST_NAME).encode("ascii")
             + b"\n")
    return "sha256:" + h.hexdigest()


def stamp_install_tree(install_dir: Path, *, repo_root: Path | None = None) -> None:
    """Compute both digests over ``install_dir`` and stamp its manifest.

    The build-step entrypoint, called by ``deploy.build_plugin()`` after it
    has synced ``dist/``, ``node_modules/`` and the JSON files into place.
    No-ops when the manifest or ``dist/`` is missing — the caller already
    guards on a successful build, and a half-built tree should not be
    blessed with a stamp.

    Because the hash is taken *after* the copy, the stamp records whatever
    was copied. It establishes the baseline for drift of the install tree
    after the build; it does not attest that the copy was faithful. That is
    ``deploy_verify.verify_plugin_install_matches_source``'s job, and the
    two are complementary — see the spec's §4.

    Raises ``RuntimeError`` if a digest cannot be computed.
    """
    manifest_path = install_dir / MANIFEST_NAME
    if not manifest_path.exists() or not (install_dir / "dist").is_dir():
        return

    try:
        digest = canonical_dist_digest(install_dir / "dist")
        tree_digest = canonical_install_tree_digest(install_dir)
    except Exception as e:
        raise RuntimeError(
            f"could not compute plugin install digests for signature: {e}"
        ) from e

    # Capture the commit the build came from, best-effort. Not load-bearing
    # for verification — the digests alone are the security boundary — but
    # it is how an operator checks whether a given pod has re-stamped since
    # a given commit (see REQUIRE_TREE_DIGEST's flip criterion).
    commit = ""
    if repo_root is not None:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root), capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                commit = r.stdout.strip()
        except Exception as e:
            # Forensic field only — a build must not fail because git is
            # unavailable. Say so rather than swallowing it silently: an
            # absent builtFromCommit is what an operator reads to decide
            # whether a pod has re-stamped (REQUIRE_TREE_DIGEST's criterion).
            print(f"[evolve/plugin-signature] could not resolve builtFromCommit: {e}")

    stamp_manifest_in_place(
        manifest_path,
        digest=digest,
        tree_digest=tree_digest,
        built_from_commit=commit,
    )


def stamp_manifest_in_place(
    manifest_path: Path,
    *,
    digest: str,
    tree_digest: str = "",
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
    if tree_digest:
        new_trust["treeDigest"] = tree_digest
        new_trust["treeDigestAlgorithm"] = TREE_DIGEST_ALGORITHM
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
    """Verify the manifest's stamped digests match the live install tree.

    Returns ``(True, "")`` on a clean match, ``(True, <warning>)`` when the
    install passes but something is worth telling the operator, and
    ``(False, <reason>)`` on any failure. Never raises — caller is expected
    to decide whether to refuse-to-install or surface a different error.

    **Callers must treat a truthy second element as output even when ok is
    True.** The one warning today is the staged-rollout case below.

    Failure modes (any of these returns ok=False):
    - manifest is missing / unreadable / not JSON
    - manifest lacks the ``x-evolve-trust.distDigest`` block (unstamped)
    - ``dist/`` is missing / unreadable
    - computed dist digest differs from the stamped one
    - a ``treeDigest`` is stamped and does not match, or is stamped under
      an algorithm we cannot evaluate

    Absence of a claim is never a passing claim — with one bounded,
    deliberate exception: a manifest stamped before ``treeDigest`` existed
    carries no such claim, and rejecting it would take the whole fleet down
    on rollout. That case warns and passes while
    :data:`REQUIRE_TREE_DIGEST` is ``False``, and fails once it is ``True``.
    Read that constant's docstring before changing it.

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

    # ── Install-tree coverage: node_modules/ + the JSON files ────────────
    # dist/ is verified; node_modules/ is executable code the gateway will
    # not load the plugin without, and until this block existed nothing
    # covered it.
    # The permissive branch is scoped to the key being genuinely ABSENT. A
    # key that is present but empty/null/non-string is a claim we cannot
    # evaluate, which is a failure — routing it here would let a manifest
    # writer downgrade the check to dist-only by blanking the field.
    if "treeDigest" in trust:
        stamped_tree = trust.get("treeDigest")
        if not isinstance(stamped_tree, str) or not stamped_tree:
            return (
                False,
                f"manifest at {manifest_path} has an unevaluatable "
                f"{_TRUST_KEY}.treeDigest ({stamped_tree!r}); rebuild the "
                f"plugin to re-stamp it, and investigate what wrote a blank "
                f"digest claim",
            )
    else:
        legacy = (
            f"manifest at {manifest_path} carries no {_TRUST_KEY}.treeDigest "
            f"— it was stamped by a build that predates install-tree "
            f"coverage, so node_modules/, package.json, package-lock.json "
            f"and the manifest's own content are unverified. Run "
            f"`sudo evolve-admin upgrade` to rebuild and re-stamp"
        )
        if REQUIRE_TREE_DIGEST:
            return False, legacy
        return True, legacy

    tree_algo = trust.get("treeDigestAlgorithm")
    if tree_algo != TREE_DIGEST_ALGORITHM:
        return (
            False,
            f"unsupported tree digest algorithm {tree_algo!r}; "
            f"expected {TREE_DIGEST_ALGORITHM!r}",
        )

    try:
        computed_tree = canonical_install_tree_digest(plugin_dir)
    except (OSError, ValueError) as e:
        return False, f"could not compute install-tree digest of {plugin_dir}: {e}"

    if computed_tree != stamped_tree:
        return (
            False,
            f"install-tree digest mismatch: stamped={stamped_tree} "
            f"computed={computed_tree}; dist/ is intact, so something "
            f"modified node_modules/, package.json, package-lock.json or "
            f"the manifest's own content under {plugin_dir} since the last "
            f"build — investigate there, not in dist/",
        )

    return True, ""
