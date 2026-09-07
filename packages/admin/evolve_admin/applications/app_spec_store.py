"""app_spec_store — where a v-next App Spec lives on disk, and how it is written.

AL-1.5b (internal/build-AL-1.5-spec-vnext.md §3). 1.5a built the reader —
``app_spec.spec_from_manifest`` migrates every artifact shape on the pod into
an ``AppSpec`` on read, and nothing wrote one. This module is the writer half:
the location, the envelope, and one atomic write.

WHERE, AND WHY THERE. ``{shared_dir}/apps/specs/<app_id>.json``.

  * **Pod-wide, not per-bot.** Design §5 splits the model in two: the *Spec* is
    the portable intent (one per app), the *Instance* is the bot-local
    realization (one per ``(app_id, bot_id)`` pair). Manifests are Instances —
    ``manifest.applications_dir`` resolves to the BOT's own workspace
    (``<bot home>/.openclaw/workspace/manifests/``), whatever its ``shared_dir``
    docstrings say. A Spec is not per-bot state and must not live there: two
    bots that record the same app share one Spec and keep two manifests, which
    is the whole point of the split.
  * **Not the gallery.** ``{shared_dir}/gallery/<tier>/<spec_id>/<version>.json``
    (identity: see resolve_app_id — ``<spec_id>`` there is a GALLERY TIER PATH
    component, the on-disk directory name the gallery already uses; this module
    reads no identity from it)
    is the *published* tier (design §4) — a state an app reaches after an
    operator publishes it, with versions accumulating per publish. A
    born-defined app has a Spec the moment it is defined and may never be
    published at all. Writing one into the gallery would mean "defined" and
    "published" stopped being distinguishable, which is two of the five
    lifecycle states collapsing.
  * **Under ``{shared_dir}/apps/``,** which is already the apps-layer pod-wide
    directory: AL-1.4a's ``id-migration.json`` and AL-1.5a's
    ``spec-migration.json`` are its current contents.

ONE FILE PER ``app_id``, holding the current Spec. Versions do not accumulate
here — ``spec_version`` is monotonic per ``app_id`` (design §5) and the place
copies of older versions live is the gallery, at publish. A re-record of the
same app overwrites, atomically.

PERMISSIONS, both pods (the write is the same call on each; only the profile
constants differ):

  | | macOS | Linux |
  |---|---|---|
  | ``{shared_dir}`` | ``/Users/Shared/evolve`` | ``/var/lib/evolve`` |
  | ``apps/`` + ``apps/specs/`` at creation | ``evolve:wheel`` 0755 | ``evolve:evolve`` 0755 |
  | …after ``ensure_pod_perms`` | ``evolve:wheel`` 0755 | ``evolve:root`` 0755 |
  | ``<app_id>.json`` | ``evolve`` 0644 | ``evolve`` 0644 |

  The group differs between the two rows on Linux only: a directory this
  module mkdirs inherits ``evolve``'s primary group, while
  ``deploy._ensure_evolve_owned_dir_perms`` normalizes it to the platform
  profile's admin group (``wheel`` on macOS, ``root`` on Linux — ``wheel`` is
  not gid 0 on Ubuntu). Both are correct; neither affects readability, which
  is what the 0755/0644 pair carries. Verified as the real ``evolve`` user on
  both live pods under a deliberately hostile ``umask 077``.

The mode is PINNED, not inherited: ``atomic_write_json(..., mode=0o644)``
chmods the temp file before the rename, because ``mkstemp`` creates at 0600 and
a mode that rode the umask would be a different answer on a differently
configured box. 0644 and not 0600 because this file is read across a user
boundary and 0600 would be a silent read failure for every reader that is not
``evolve``. Everything that writes here runs as ``evolve`` (the admin server,
the ``manifest-reflex`` runner, the CLI), so there is no ``/tmp``-staging +
``sudo /bin/cp`` dance and no ACL to grant on the write side. On the read side
the file carries no secret — it is the app's declared intent, the same content
the manifest already publishes at 0644 — so world-read is the correct posture
and the one the bot users and a future ``evo``-side reader both need.

``apps`` joins ``deploy.EVOLVE_OWNED_SHARED_SUBDIRS`` in this chip so
``ensure_pod_perms`` enforces that row rather than leaving it to whatever umask
first created the directory, and so ``pod_perms_drift_monitor`` catches a
re-widen. That is the ``chmod -R a+rX {shared_dir}`` re-exposer class: a
directory nothing checks is a directory that drifts.

A DRAFT GETS NO FILE. ``write_spec`` refuses an empty or non-conforming
``app_id`` rather than inventing a filename. Design §3 confers identity at
promotion; a discovered draft has a ``draft_id`` and no portable intent to
share yet, so there is nothing here for it to be written as. The check is also
what keeps ``<app_id>.json`` a safe path join — ``APP_ID_PATTERN`` admits no
``/``, no ``.`` and no ``..``, so a hostile or merely broken id cannot steer
the write out of the directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from evolve_util import atomic_write_json

from .app_identity import is_canonical_app_id
from .app_spec import (
    SPEC_SHAPE_FIELD,
    SPEC_SHAPE_VERSION,
    SPEC_SHAPE_VERSION_FIELD,
    SPEC_SHAPE_VNEXT,
    AppSpec,
    is_vnext_artifact,
    spec_from_manifest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PackageFileNote",
    "SPEC_DIR_MODE",
    "SPEC_FILE_MODE",
    "ensure_specs_dir",
    "iter_spec_paths",
    "load_spec",
    "read_spec",
    "resolve_package_files",
    "resolve_workspace_package_files",
    "spec_envelope",
    "spec_from_artifact",
    "spec_from_artifact_with_notes",
    "spec_path",
    "specs_dir",
    "write_spec",
]

# Pinned, never inherited from the umask. See the module docstring.
SPEC_FILE_MODE = 0o644
SPEC_DIR_MODE = 0o755


def specs_dir(shared_dir: Path) -> Path:
    """``{shared_dir}/apps/specs`` — the v-next Spec directory."""
    return Path(shared_dir) / "apps" / "specs"


def spec_path(shared_dir: Path, app_id: str) -> Path:
    """Where the Spec for ``app_id`` lives. Raises on a non-conferrable id."""
    return specs_dir(shared_dir) / f"{_checked_app_id(app_id)}.json"


def _checked_app_id(app_id: Any) -> str:
    """``app_id`` if it is a conferrable identity; raise otherwise.

    Two jobs in one gate. Design §3: a draft has no ``app_id`` and gets no
    Spec, so an empty id is a refusal and not an error to paper over. And path
    safety: this value becomes a filename, so it has to be one that
    ``APP_ID_PATTERN`` vouches for before it is joined onto a directory.
    """
    value = app_id.strip() if isinstance(app_id, str) else ""
    if not value:
        raise ValueError(
            "refusing to write a v-next Spec with no app_id — a discovered "
            "draft carries a draft_id and no portable identity (design §3); "
            "identity is conferred at promotion, not by writing a file"
        )
    if not is_canonical_app_id(value):
        raise ValueError(
            f"refusing to write a v-next Spec for non-conforming app_id "
            f"{value!r} — it must match app_identity.APP_ID_PATTERN before it "
            f"can become a filename"
        )
    return value


def spec_envelope(spec: AppSpec) -> dict[str, Any]:
    """The on-disk payload: the shape discriminator, then design §5's fifteen.

    The discriminator leads so the first bytes of the file say what the file
    is. ``AppSpec.to_dict()`` supplies the rest unchanged — this function adds
    keys, it never edits them, which is what keeps ``SPEC_FIELDS`` frozen at
    fifteen while the artifact still carries a marker a reader can branch on.
    """
    return {
        SPEC_SHAPE_FIELD: SPEC_SHAPE_VNEXT,
        SPEC_SHAPE_VERSION_FIELD: SPEC_SHAPE_VERSION,
        **spec.to_dict(),
    }


def write_spec(spec: AppSpec, shared_dir: Path) -> Path:
    """Write ``spec`` to ``{shared_dir}/apps/specs/<app_id>.json``. Returns the path.

    Atomic (same-directory temp file + ``os.replace``), so a reader sees either
    the previous Spec or this one and never a torn write. Mode is pinned at
    0644 — see the module docstring for the both-pods ownership table.

    Raises ``ValueError`` for a spec with no conferrable ``app_id`` and lets
    ``OSError`` out: a caller that cannot write the Spec needs to know, and
    swallowing it here would hand the census a pod that silently never became
    v-next. (The reflex runner catches it at ITS boundary, where the manifest
    write has already succeeded and losing the row would be the worse outcome.)
    """
    path = spec_path(shared_dir, spec.app_id)
    ensure_specs_dir(shared_dir)
    atomic_write_json(path, spec_envelope(spec), mode=SPEC_FILE_MODE)
    return path


def ensure_specs_dir(shared_dir: Path) -> Path:
    """``mkdir -p`` the Spec directory at ``SPEC_DIR_MODE``. Returns it.

    ``Path.mkdir(mode=…)`` is masked by the umask and does nothing at all when
    the directory already exists, so the mode is asserted explicitly rather
    than left to whichever process created the directory first.

    ONLY WHEN IT IS ACTUALLY WRONG, though, and that guard is load-bearing on
    Linux: ``chmod`` on a directory carrying a POSIX ACL RECALCULATES the ACL
    mask from the new group bits, which silently caps any named ACE on it —
    the trap that has locked ``evolve`` out of shared directories before.
    ``{shared_dir}/apps`` has no ACL on either pod today, but it sits beside
    ``signals/`` and ``proposals/``, which carry a named ``evo`` grant, so a
    chmod fired on EVERY write is a standing invitation to that bug. Compare
    first, write only on a mismatch, and the steady state touches nothing.

    Best-effort even then: ``deploy.ensure_pod_perms`` is the durable enforcer
    (``apps`` is in ``EVOLVE_OWNED_SHARED_SUBDIRS``) and it repairs the mode
    the right way, re-asserting the ACL mask afterwards via the platform perms
    seam. A Spec write must not fail because the directory was already correct
    and owned by someone this process cannot chmod.
    """
    d = specs_dir(shared_dir)
    d.mkdir(parents=True, exist_ok=True)
    for component in (d.parent, d):
        try:
            if stat.S_IMODE(component.stat().st_mode) != SPEC_DIR_MODE:
                os.chmod(component, SPEC_DIR_MODE)
        except OSError as exc:
            logger.info(
                "app_spec_store: could not set %s to %s (%s) — leaving it to "
                "deploy.ensure_pod_perms, which owns this invariant",
                component, oct(SPEC_DIR_MODE), exc,
            )
    return d


def read_spec(path: Path) -> AppSpec | None:
    """The ``AppSpec`` at ``path``, or None when it is not a v-next Spec.

    None rather than an exception for an unreadable, malformed or
    wrong-shaped file: every caller here is walking a directory, and one bad
    file must not end the walk. A file that IS v-next is rebuilt through
    ``AppSpec.from_dict``, the same normalizer ``write_spec`` round-trips
    against, so ``read_spec(write_spec(s, d)) == s``.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not is_vnext_artifact(data):
        return None
    return AppSpec.from_dict(data)


def load_spec(shared_dir: Path, app_id: str) -> AppSpec | None:
    """The Spec for ``app_id`` on this pod, or None when none is written."""
    try:
        path = spec_path(shared_dir, app_id)
    except ValueError:
        return None
    return read_spec(path)


def iter_spec_paths(shared_dir: Path) -> Iterator[Path]:
    """Every v-next Spec file on the pod, sorted.

    Dotfiles and underscore-prefixed names are skipped, matching
    ``id_migration._iter_manifest_paths`` — the same convention keeps editor
    swap files and any future ``_history`` sibling out of the census.
    """
    d = specs_dir(shared_dir)
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        yield p


# ── Deriving a Spec from an artifact that lives on a real pod ────────────────

def resolve_package_files(data: dict) -> list[dict] | None:
    """The files-pack file list for one artifact, or None when it has no pack.

    ``package.files[].sha256`` is the field the design's deterministic install
    (§6) verifies against, and its primary carrier is the files-pack metadata
    (``gallery/<slug>/files/manifest.json``). It is NOT the only carrier —
    ``_derive_package`` also reads an inline ``sha256`` off a manifest's own
    ``files[]`` entries — but it is the only one that needs a disk lookup, so
    it is the one that lives here. Without it, every packaged artifact reads
    ``partial`` for a missing sha and the census's actual signal drowns in one
    uniform complaint.

    Imported lazily: ``gallery`` is a 75KB module and ``app_spec`` stays pure,
    so the disk dependency lives here, in the module that already owns the
    disk side of a Spec. Any failure downgrades to "no pack" rather than
    raising — a gallery that cannot be read is a fact about the gallery, not
    about the artifact being read.

    AL-1.5b moved this out of ``spec_migration``. It was private to the census
    while the census was the only thing that derived a Spec against a real
    pod; now the WRITER derives one too, and two derivations that disagree
    about ``package.files[].sha256`` would mean a written Spec is strictly
    less installable than the artifact it came from — design §6 makes install
    = materialize sha-verified files, so dropping shas the pack could have
    supplied is not a smaller answer, it is a broken one.
    """
    # identity: see resolve_app_id — ``pkg_id`` is read here as the PACKAGE
    # ATTRIBUTION NAMESPACE (the files-pack directory key under
    # ``gallery/<slug>/files/``), never as the app's identity. The app's
    # identity on this artifact comes from ``resolve_app_id`` inside
    # ``spec_from_manifest``; a files-pack lookup keyed on ``app_id`` would
    # find nothing, because the packs are not named that way. AL-1.4b §3.
    pkg_id = data.get("pkg_id")
    if not isinstance(pkg_id, str) or not pkg_id.strip():
        return None
    try:
        from .files_pack import load_files_pack_metadata
        from .gallery import find_files_pack_dir
        fp_dir = find_files_pack_dir(pkg_id.strip())
        if fp_dir is None:
            return None
        meta = load_files_pack_metadata(fp_dir)
        if meta is None:
            return None
        return [{"path": f.path, "sha256": f.sha256} for f in meta.files]
    except Exception:  # noqa: BLE001 — see docstring
        return None


@dataclass(frozen=True)
class PackageFileNote:
    """One declared file whose source digest could NOT be computed.

    AL-1.5c. The brief's rule, and the reason this type exists rather than a
    bare ``""``: *a file that cannot be hashed must be reported, never
    silently written as an empty sha* — an empty string is
    indistinguishable from "nobody has looked yet", which is exactly how the
    deterministic-install gap survived from design §6 to AL-1.5c unnoticed.

    ``kind`` is one of the four this module produces:
      ``missing``      — declared in the artifact, absent from the workspace
      ``unreadable``   — present but the ``evolve`` user cannot read it
      ``directory``    — the declared path is a directory, not a file
      ``no_workspace`` — the bot's workspace could not be resolved at all

    The vocabulary is OPEN — a producer may add a kind, and readers must not
    assume the four above are exhaustive. ``applications.app_snapshot`` adds
    ``outside_workspace`` (a declared path that escapes the workspace), which
    it keeps distinct from ``unreadable`` on purpose: it refuses a whole
    snapshot on read-denial, and a bad manifest row must not be able to trip
    that by borrowing the permission-failure kind.
    """

    path: str
    kind: str
    detail: str = ""


def resolve_workspace_package_files(
    data: dict,
    *,
    workspace: Path | None = None,
) -> tuple[list[dict] | None, list[PackageFileNote]]:
    """Hash an artifact's declared files where the bot actually wrote them.

    THE SECOND CARRIER, and the one that closes AL-1.5c's gap. A files-pack
    (``resolve_package_files``) covers an app installed FROM the gallery, and
    on both live pods that is one artifact out of 232. Everything else — every
    scanner row, and every ``record_application`` Spec the AL-1.5b reflex
    writer lands — declares real workspace-relative paths and no digest at
    all, so ``package.files[].sha256`` came out ``""`` on every entry.

    The digest computed here is the file's CONTENT digest, taken from the
    bot's workspace. For a born-defined app that is both the source and the
    realized bytes at once: the bot authored the file in place, so no
    substitution pass stands between them. That is NOT the case for a
    files-pack install, which is why the pack carrier wins whole when both
    are available — see ``resolve_package_files`` and §8.2's source/realized
    split.

    Reads are plain ``Path.read_bytes()``: the ``evolve`` user holds an
    inherited macOS ACE (``list,search,readattr,readextattr,readsecurity``
    with ``file_inherit,directory_inherit``) on every bot's
    ``.openclaw/workspace``. Measured on the live mini as the real ``evolve``
    user, 2026-08-19: **336 of 343 declared files hashed, 0 permission
    failures**; all 7 misses were files the manifest still names but the
    workspace no longer has. So ``missing`` is the failure mode that actually
    occurs and ``unreadable`` is the one to keep reporting anyway.

    Returns ``(files, notes)``. ``files`` is None when the artifact declares
    nothing — distinct from ``[]``, which would claim "declared zero files".
    """
    entries = data.get("realized_files") or data.get("files") or []
    # (path -> the source entry) so the ROLE survives. Injecting
    # ``package_files`` makes ``_derive_package`` take its whole-cloth branch
    # and skip the legacy read, so anything this resolver does not carry
    # forward is DROPPED. ``_package_role`` reads three keys —
    # ``role`` / ``purpose`` / ``marker_state`` — and on the live mini
    # 2026-08-19 **333 entries carry ``marker_state``** while ZERO carry
    # ``role``. Checking only for ``role`` therefore reports a clean zero and
    # hides a real regression on every one of them; the sha this chip adds
    # must not cost the role the census already reports.
    declared: list[str] = []
    sources: dict[str, dict] = {}
    for raw in entries:
        if isinstance(raw, dict):
            rel = raw.get("path")
        elif isinstance(raw, str):
            rel = raw
        else:
            continue
        if not isinstance(rel, str) or not rel.strip():
            continue
        rel = rel.strip()
        if rel not in declared:
            declared.append(rel)
            sources[rel] = raw if isinstance(raw, dict) else {}
    if not declared:
        return None, []
    # A blueprint-bearing artifact ALSO contributes entries in the legacy
    # derivation. Hashing here would drop them, so leave the whole artifact to
    # the legacy path rather than return a package that is missing files.
    if _dicts_with_files(data.get("blueprint")):
        return None, []

    if workspace is None:
        workspace = _resolve_bot_workspace(data)
    if workspace is None:
        return None, [
            PackageFileNote(p, "no_workspace", "bot workspace did not resolve")
            for p in declared
        ]

    files: list[dict] = []
    notes: list[PackageFileNote] = []
    for rel in declared:
        target = Path(workspace) / rel
        try:
            if target.is_dir():
                notes.append(PackageFileNote(rel, "directory",
                                             "declared path is a directory"))
                files.append(_entry(rel, "", sources[rel]))
                continue
            if not target.is_file():
                notes.append(PackageFileNote(rel, "missing",
                                             "declared but not on disk"))
                files.append(_entry(rel, "", sources[rel]))
                continue
            files.append(_entry(rel, _sha256_path(target), sources[rel]))
        except OSError as exc:
            notes.append(PackageFileNote(rel, "unreadable", str(exc)))
            files.append(_entry(rel, "", sources[rel]))
    return files, notes


def _dicts_with_files(blueprint: Any) -> bool:
    """True when ``blueprint.files`` would contribute package entries."""
    if not isinstance(blueprint, dict):
        return False
    files = blueprint.get("files")
    return isinstance(files, list) and any(isinstance(f, dict) for f in files)


def _entry(path: str, sha: str, source: dict) -> dict:
    """One ``package.files[]`` entry, carrying the source entry's ROLE through.

    ``_derive_package`` calls ``_package_role`` on whatever this returns, and
    that helper reads ``role`` / ``purpose`` / ``marker_state`` in order — so
    passing the original keys along is what keeps the injected package
    identical to the legacy one except for the digest this chip adds.
    """
    out = {"path": path, "sha256": sha}
    for key in ("role", "purpose", "marker_state"):
        val = source.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val
            break
    return out


def _sha256_path(path: Path) -> str:
    """Stream a file's SHA-256. Mirrors ``files_pack._sha256_file``.

    Local rather than imported so this module's hot path does not drag in
    ``files_pack`` (and through it ``gallery``) for every artifact the census
    walks — the pack carrier already imports it lazily, only when there is a
    pack to read.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_bot_workspace(data: dict) -> Path | None:
    """The workspace of the bot this artifact belongs to, or None.

    Best-effort and lazily imported: ``evolve_admin.config`` reads
    ``network.json`` and shells out, neither of which a pure derivation
    should require. A pod where this returns None still derives a Spec — it
    just reports every file as ``no_workspace`` instead of inventing a digest.
    """
    bot = data.get("bot_id")
    if not isinstance(bot, str) or not bot.strip():
        return None
    try:
        from ..config import get_bot_workspace
        return get_bot_workspace(bot.strip())
    except Exception:  # noqa: BLE001 — see docstring
        return None


def spec_from_artifact_with_notes(
    data: dict,
    *,
    workspace: Path | None = None,
) -> tuple[AppSpec, list[PackageFileNote]]:
    """``spec_from_artifact``, plus why any digest is still missing.

    The pack carrier wins whole when it exists (design §6 names it, and its
    shas are pre-substitution source digests). Only when there is no pack does
    the workspace carrier run — and only IT can report, because a pack that
    verifies is complete by construction while a workspace read can find a
    file gone.
    """
    pack_files = resolve_package_files(data)
    if pack_files is not None:
        return spec_from_manifest(data, package_files=pack_files), []
    ws_files, notes = resolve_workspace_package_files(data, workspace=workspace)
    return spec_from_manifest(data, package_files=ws_files), notes


def spec_from_artifact(data: dict) -> AppSpec:
    """``spec_from_manifest`` with the disk-resolved files-pack injected.

    THE one derivation for anything reading a real pod. ``app_spec`` is pure
    and cannot look up a files-pack, so its ``package_files`` is an injection
    point; every caller that HAS a disk should inject the same way or they
    disagree about the one field design §6's deterministic install verifies.
    The census and the reflex writer both come through here for exactly that
    reason — the census's own rule against inventing "a second answer to what
    is on this pod", applied to derivation rather than to the walk.

    An already-v-next artifact short-circuits inside ``spec_from_manifest``
    and ignores the injection entirely: its ``package`` is the authoritative
    one that was written.
    """
    return spec_from_artifact_with_notes(data)[0]
