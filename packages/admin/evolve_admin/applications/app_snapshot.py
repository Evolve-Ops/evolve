"""app_snapshot — AL-3.1: turn a *defined* app into a files-pack.

Brief: ``internal/dispatch/done/al-3-1-app-snapshot.md``. That brief cites
``internal/design-app-suites-2026-08-24.md`` §1 (D-S2) as the program doc;
**that file does not exist in this repo** — not on main, not in history — so
it is named here as the brief's citation and not as something this module
was written against. Prior stage:
``internal/build-AL-1.5-spec-vnext.md`` §9 (AL-1.5c's record), whose §9.7
handed this here in as many words:

    *"Making the pack path the only install path requires every app to HAVE
    a pack; today one artifact across 232 does. The other 231 are scanner
    rows and born-defined apps whose files the bot authored in place — there
    is no pack to install them from, and manufacturing one for each is the
    publish/snapshot surface, which AL-3.1 owns."*

This module is that surface for the born-in-place population.

WHAT IT IS NOT — ``snapshot_engine.snapshot_installed_app`` already exists
and is reused rather than reimplemented (its placeholder detector is called
directly). But it answers a different question: it is **pkg_id-keyed**, so
it can only snapshot an app that was INSTALLED FROM the gallery and whose
manifest carries the package key. A born-defined app has no ``pkg_id`` at
all — that is precisely what makes it born-in-place — so on the live mini
that entrypoint is reachable for a handful of artifacts and unreachable for
the rest. This module is **app_id-keyed**, via ``resolve_app_id``, which is
the identity design §3 confers and the one the Spec is filed under.

THE THREE OUTPUTS, in the order they are produced:

  1. **A files-pack** at ``{shared_dir}/apps/packs/<app_id>/`` — the source
     files plus a ``manifest.json`` in ``FILES_PACK_FORMAT_VERSION`` shape.
     It is verified against ``verify_files_pack_integrity`` before this
     function returns success, so a pack that would refuse to install never
     reads as a successful snapshot.
  2. **An updated Spec** at ``{shared_dir}/apps/specs/<app_id>.json`` whose
     ``package.files[].sha256`` are the pack's SOURCE digests — see §9.2
     below.
  3. **A shared-surface report** — what this app changed in surfaces it does
     not own (AGENTS.md / HEARTBEAT.md sections, config keys). Report-only;
     see "the operator addition" below.

WHY THE PACK LIVES THERE. The Spec already lives at
``{shared_dir}/apps/specs/<app_id>.json`` (``app_spec_store``), keyed on the
one identity design §3 confers. The pack is keyed the same way and sits
beside it, so a reader that can name the app can find its pack with no
pointer to follow and no new field on the Spec to carry one. That is not a
convenience: ``package.ref`` is the only free-looking sub-key on
``package{}`` and it is spoken for — design §5 pairs it with
``package.standard`` as the reference INTO an inner Agent Plugins 1.0 /
ClawHub package. Repurposing it for an Evolve-native pack path would be the
quiet field redefinition the brief's STOP rule exists to prevent. Deriving
the location from ``app_id`` needs no field at all.

MODE, OWNERSHIP, ACL — both pods, pinned rather than inherited:

  | | macOS | Linux |
  |---|---|---|
  | ``{shared_dir}`` | ``/Users/Shared/evolve`` | ``/var/lib/evolve`` |
  | ``apps/`` (already enforced) | ``evolve:wheel`` 0755 | ``evolve:root`` 0755 |
  | ``apps/packs/`` + ``apps/packs/<app_id>/`` | ``evolve:wheel`` **0700** | ``evolve:evolve`` **0700** |
  | pack files + ``manifest.json`` | ``evolve`` **0600** | ``evolve`` **0600** |

  **0700/0600 and not the Spec's 0755/0644, deliberately.** A Spec is the
  app's declared *intent* and is read across the user boundary (bots, a
  future ``evo``-side reader) — world-read is the correct posture for it. A
  pack is the app's *content*, copied out of a bot workspace that is
  deliberately bot-private (CLAUDE.md's Linux bot-private clamp: real
  ``group::``/``other::`` are ``---`` on a bot's ``.openclaw``). Landing that
  content pod-wide at 0644 would re-widen, through a new directory, exactly
  what the clamp closed — every bot on the box could read every other bot's
  app source. The only reader a pack needs is the admin daemon, which runs
  as ``evolve`` and is also its only writer, so evolve-only is both
  sufficient and minimal. This is the ``chmod -R a+rX {shared_dir}``
  re-exposer class approached from the other side: do not create the
  exposure in the first place.

  ``ensure_pod_perms`` normalizes ``apps`` itself to 0755 and chowns its
  tree to ``evolve``; its chmod is on the named directory only, not ``-R``,
  so the 0700 children below survive it. The chowns are harmless — this
  module writes as ``evolve`` already.

§9.2 — THE TWO-DIGEST AMBIGUITY, AND WHAT THIS CHIP DOES ABOUT IT.
AL-1.5c §9.2 recorded that ``package.files[].sha256`` carries a *source*
digest when it came from a files-pack and a *realized* digest when it came
from a manifest's inline entry or from ``resolve_workspace_package_files``,
with nothing on the artifact saying which — and §9.8 named settling it as a
precondition for design §6's verify-on-install. The resolution implemented
here, and why it needs no sixteenth field:

  * **The field's meaning is already settled, by operator ratification.**
    design §9's determinism wording (ratified 2026-08-19) reads
    ``package.files[].sha256`` as the source digest, *pre-substitution*, in
    those words. There was never a decision left to make about what the
    field means; what was missing was a writer that honours it and a way to
    tell an honouring row from a legacy one.
  * **This module writes source digests, by construction.** The digest
    recorded for each entry is taken over the bytes written INTO the pack —
    after reverse-substitution, before any install-time substitution — which
    is the same side of the substitution boundary
    ``verify_files_pack_integrity`` checks. So for any app that has been
    snapshotted, Spec and pack agree and verify-on-install can proceed.
  * **A pack is addressable without a field**, because its directory is
    derivable: ``pack_dir(shared_dir, app_id)``, keyed exactly as
    ``app_spec_store.spec_path`` keys the Spec. **The pack, not the Spec, is
    the authority for source digests.** A verify-on-install that reads the
    pack is correct by construction; one that trusts the Spec's mirror is
    not, and that is the rule AL-3.2 has to be built on.
  * **AND THAT IS NOT YET A COMPLETE ANSWER — stated plainly, because an
    earlier draft of this docstring claimed it was.** ``pack_dir(...).exists()``
    answers *"is there a pack?"*, which is the same question as *"are these
    Spec digests source or realized?"* only while this module is the sole
    writer of ``package.files[].sha256``. It is not. The Spec is re-derived
    on every ``record_application`` reflex write, and that path
    (``spec_from_artifact_with_notes``) consults the pkg_id-keyed gallery
    pack only — so a re-record after a snapshot replaces these source
    digests with fresh workspace digests while the pack still exists. In
    that window the presence test says "source" and the Spec holds realized
    digests, which is §9.2's failure relocated rather than removed. It is
    harmless today because nothing consumes either yet.

    So the resolution is **conditional**: it holds for a consumer that
    verifies against the pack, and it requires the reflex writer to learn an
    app_id-keyed third carrier before anything verifies against the Spec.
    That carrier is not built here because it changes what the census
    computes for every artifact at once and moves §9.5's baselines;
    re-baselining is not this chip. Named, measured, handed forward — and
    NOT described as settled.
  * **One pack per ``app_id``, with no bot component**, which is the right
    key for a portable artifact and a real limitation for a pod where two
    bots run the same app from different workspaces: the second snapshot
    overwrites the first, and the metadata records no source bot (by design
    — it is a reserved token). Whoever adds per-bot packs or a provenance
    field should know the overwrite is current behaviour, not an accident.

  A ``spec_version`` bump WAS deliberately not taken here, on three grounds:
  nothing consumed a pack, a no-op re-snapshot must not move the version, and
  "what updates pin" was the install surface's decision to make when there was
  an install to pin. **AL-3.2 settled all three** — ``app_install`` consumes
  the pack, ``changed`` already separates a real re-snapshot from a no-op, and
  it is that install surface. So the bump is taken now, gated on ``changed``,
  in ``_spec_for``; the idempotence promise is unaffected, and without it
  ``Update to vN`` would be unreachable by construction.

THE OPERATOR ADDITION (2026-08-24) — SHARED SURFACES. An app's description
must also cover what it changed in surfaces it does not own: AGENTS.md and
HEARTBEAT.md sections, config keys, memory seeds. A snapshot that captures
only ``files[]`` installs HOLLOW for a conversation-born app that keeps half
its behaviour in an AGENTS.md block. This module DETECTS AND REPORTS those
deltas (``SharedSurfaceDelta``); it does not carry them in the pack or the
Spec, because a ``footprint`` / ``shared_edits`` field is a §5-freeze
decision and is proposed in the PR body, not taken here.

  What detection can and cannot see, stated rather than implied:

    ``marker``   — the section carries ``<!-- evolve-managed: pkg=… -->``
                   and the pkg matches this app. Attribution is a fact
                   written into the file by the installer.
    ``declared`` — the artifact's ``scheduled_actions[]`` /
                   ``heartbeat_evidence`` names the section, but the file
                   carries no marker. Attribution rests on the manifest's
                   own claim, which is why it gets its own value instead of
                   being folded into ``marker``.
    ``config``   — an ``installed_artifact`` pointing at a config file
                   fragment (``openclaw.json#hooks.heartbeat[2]``). Reported
                   with no content digest: locating an arbitrary key path
                   inside a JSON document is not the same operation as
                   locating a markdown heading, and guessing would be worse
                   than declining.

  And the limit that matters most: **an unmarked, undeclared edit is not
  detectable.** A bot that appended a paragraph to AGENTS.md during a
  conversation left no marker and no manifest row; nothing here can
  attribute it to an app, and nothing here pretends to. What the report DOES
  carry is the size of that blind spot — ``unattributed_sections``, the
  sections in the bot's shared markdown that carry no marker and are claimed
  by no manifest on that bot. It is bot-scoped context, not this app's
  footprint, and it is labelled as such.

REPORT, NEVER SILENTLY DROP. A declared file that is missing, unreadable or
a directory produces a ``PackageFileNote`` (AL-1.5c's channel, reused) and is
left out of the pack. It is never written as an empty digest and never
vanishes: an empty sha is indistinguishable from "nobody has looked yet",
which is how the deterministic-install gap survived from design §6 to
AL-1.5c unnoticed.

REFUSAL IS NOT FAILURE. A draft has no ``app_id`` and cannot be snapshotted;
an artifact that is still ``discovered`` is not a defined app. Both come back
with ``ok=False`` **and ``refused=True``**, so a healthy decline does not read
like an outage on a status page (AL-1.5b's review found that exact confusion).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .app_identity import draft_id_of, is_canonical_app_id, resolve_app_id
from .app_spec import AppSpec
from .app_spec_store import (
    PackageFileNote,
    load_spec,
    spec_from_artifact,
    write_spec,
)
from .files_pack import (
    FILES_PACK_FORMAT_VERSION,
    load_files_pack_metadata,
    verify_files_pack_integrity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PACK_DIR_MODE",
    "PACK_FILE_MODE",
    "SNAPSHOT_BY",
    "SharedSurfaceDelta",
    "pack_dir",
    "packs_dir",
    "snapshot_app",
]

# Pinned, never inherited from the umask — see the module docstring for why
# these are tighter than the Spec's 0755/0644.
PACK_DIR_MODE = 0o700
PACK_FILE_MODE = 0o600

SNAPSHOT_BY = "app_snapshot.snapshot_app"

#: The workspace markdown files an app can plant a section in. Mirrors
#: ``install_helpers._DEFAULT_HEADER_BY_FILE`` — the same two files the
#: managed-section installer knows how to create.
SHARED_MARKDOWN_FILES: tuple[str, ...] = ("AGENTS.md", "HEARTBEAT.md")

#: ``<!-- evolve-managed: pkg=… job=… -->``. Same shape as
#: ``install_helpers._MANAGED_MARKER_RE``; repeated rather than imported
#: because ``install_helpers`` is a 100KB module that pulls in the whole
#: install stack, and this one only ever reads.
_MANAGED_MARKER_RE = re.compile(
    r"<!--\s*evolve-managed(?::\s*(?P<kv>[^>]*))?\s*-->", re.IGNORECASE,
)

#: Cap on ``unattributed_sections`` so one sprawling AGENTS.md cannot drown
#: the report. The count is reported in full; only the list is trimmed.
_MAX_UNATTRIBUTED_LISTED = 25

# Attribution values for SharedSurfaceDelta.attribution.
ATTR_MARKER = "marker"
ATTR_DECLARED = "declared"
ATTR_CONFIG = "config"


@dataclass(frozen=True)
class SharedSurfaceDelta:
    """One change this app made to a surface it does not own.

    ``sha256`` is over the section's text as found (heading line included,
    trailing whitespace stripped), and is "" when the delta could not be
    located or is not a markdown section at all — a ``config`` delta always
    carries "". ``found`` says which of those two it is, so an empty digest
    is never ambiguous between "not there" and "not hashable".
    """

    file: str
    section: str
    attribution: str
    sha256: str = ""
    found: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "section": self.section,
            "attribution": self.attribution,
            "sha256": self.sha256,
            "found": self.found,
            "detail": self.detail,
        }


@dataclass
class _PackEntry:
    """One file on its way into the pack, before the manifest is written."""

    path: str
    mode: str
    sha256: str
    size_bytes: int
    placeholders: list[str] = field(default_factory=list)
    bare_token_count: int = 0
    role: str = ""
    path_tokens: list[str] = field(default_factory=list)
    payload: bytes = b""

    def manifest_entry(self) -> dict[str, Any]:
        """The ``files[]`` row — install contract only, no review metadata."""
        return {
            "path": self.path,
            "mode": self.mode,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "placeholders": list(self.placeholders),
        }


# ── Locations ────────────────────────────────────────────────────────────────


def packs_dir(shared_dir: Path) -> Path:
    """``{shared_dir}/apps/packs`` — where snapshotted files-packs live."""
    return Path(shared_dir) / "apps" / "packs"


def pack_dir(shared_dir: Path, app_id: str) -> Path:
    """The pack directory for ``app_id``. Raises on a non-conferrable id.

    Same gate as ``app_spec_store.spec_path``, and for the same two reasons:
    a draft has no identity to file a pack under (design §3), and the value
    becomes a path component, so ``APP_ID_PATTERN`` has to vouch for it
    before it is joined onto a directory.
    """
    value = app_id.strip() if isinstance(app_id, str) else ""
    if not value:
        raise ValueError(
            "refusing to resolve a pack directory with no app_id — a "
            "discovered draft carries a draft_id and no portable identity "
            "(design §3)"
        )
    if not is_canonical_app_id(value):
        raise ValueError(
            f"refusing to resolve a pack directory for non-conforming "
            f"app_id {value!r} — it must match app_identity.APP_ID_PATTERN "
            f"before it can become a path component"
        )
    return packs_dir(shared_dir) / value


# ── Envelopes ────────────────────────────────────────────────────────────────


def _err(error: str, *, refused: bool = False, **extra: Any) -> dict:
    """Failure (or refusal) envelope. Callers map to HTTP status / exit code.

    ``refused`` separates "this is not a thing that can be snapshotted" from
    "the snapshot broke". Both are ``ok=False``; only one is a problem.
    """
    return {"ok": False, "error": error, "refused": refused, "pack_dir": "", **extra}


def _ok(**extra: Any) -> dict:
    return {"ok": True, "error": "", "refused": False, **extra}


# ── Artifact lookup ──────────────────────────────────────────────────────────


def _iter_manifest_paths(workspace: Path) -> tuple[list[Path], str]:
    """``(paths, unreadable_reason)`` for ``workspace/manifests``.

    TWO REASONS THIS IS NOT A BARE GLOB, both measured on the Linux pod
    2026-08-27 as the real ``evolve`` user:

    1. **``os.listdir`` rather than ``Path.glob``.** ``/home/<bot>`` there is
       ``drwxr-xr-x+`` with an ACL leaving ``evolve`` traverse-only
       (``X_OK`` true, ``R_OK`` false), and pathlib's glob scandirs each
       parent — so a glob across bot homes returns ``[]`` while the identical
       paths read fine addressed directly. Listing the one directory we mean
       does not go through that.
    2. **An unlistable directory is NOT an empty one.** Returning ``[]`` for
       an EACCES would make "this bot has no manifests" and "this bot's
       manifests could not be read" the same answer — a flat negative only as
       wide as its search, and the caller would go on to report the app
       missing. The reason comes back so it can be reported as what it is.
    """
    d = workspace / "manifests"
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return [], ""
    except OSError as exc:
        return [], f"{d}: {exc}"
    return sorted(
        d / n for n in names
        if n.endswith(".json") and not n.startswith((".", "_"))
    ), ""


def _find_artifact(
    workspace: Path, app_id: str,
) -> tuple[tuple[dict, Path] | None, str]:
    """``(found, unreadable_reason)`` — the bot's manifest for ``app_id``.

    Keyed on ``resolve_app_id`` — never a local ``spec_id or id`` chain
    (AL-1.5's standing guardrail). Feeds the RAW manifest to the resolver,
    because v7-arc hydration rewrites ``id``/``pkg_id`` and would resolve to
    a different key than the one the Spec is filed under.
    """
    paths, unreadable = _iter_manifest_paths(workspace)
    if unreadable:
        return None, unreadable
    for cand in paths:
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if resolve_app_id(data) == app_id:
            return (data, cand), ""
    return None, ""


def _declared_files(data: dict) -> list[tuple[str, dict]]:
    """``(rel_path, source_entry)`` for every file the artifact declares.

    The same carriers, in the same order, that
    ``app_spec_store.resolve_workspace_package_files`` reads —
    ``realized_files`` then ``files`` — so the pack covers exactly the set
    the Spec's ``package.files[]`` already names and the two cannot disagree
    about what the app consists of.
    """
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for raw in (data.get("realized_files") or data.get("files") or []):
        if isinstance(raw, dict):
            rel = raw.get("path")
            source = raw
        elif isinstance(raw, str):
            rel, source = raw, {}
        else:
            continue
        if not isinstance(rel, str) or not rel.strip():
            continue
        rel = rel.strip().lstrip("/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append((rel, source))
    return out


def _role_of(source: dict) -> str:
    """The entry's role, reading the same three keys ``_package_role`` does.

    ``role`` / ``purpose`` / ``marker_state``, in that precedence. Checking
    only ``role`` returns a clean zero on the live mini and hides 333 rows
    carrying ``marker_state`` — AL-1.5c §9.3a's regression, which nearly
    shipped because the narrow query found nothing and the nothing was wrong.
    """
    for key in ("role", "purpose", "marker_state"):
        val = source.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _path_tokens(rel: str, *, bot_id: str, bot_user: str) -> list[str]:
    """Source-bot tokens that survive in the file's PATH, not its content.

    Reverse-substitution rewrites CONTENT. A file the bot named after itself
    — ``scripts/atlas-cron.sh``, ``plist-templates/com.atlas.digest.plist`` —
    keeps that name in the pack and installs to the same literal path on the
    next bot. The files-pack format has a slot for this
    (``placeholders_in_path``), but spec §6 Rule 2 keeps it off by default
    and nothing on this pod emits it, so the honest move is to REPORT the
    tokens rather than either rewriting paths unasked or staying silent about
    a pack that will land a second bot's file under the first bot's name.
    """
    found: list[str] = []
    for token in (t for t in (bot_id, bot_user) if t and len(t) >= 3):
        if token in rel and token not in found:
            found.append(token)
    return found


def _contained(workspace: Path, rel: str) -> Path | None:
    """``workspace/rel`` when it stays inside the workspace, else None.

    ``rel`` comes off a manifest, which a bot writes, so an absolute path,
    a ``..`` segment, a symlinked parent and a symlinked LEAF are all
    reachable inputs.

    THE LEAF IS RESOLVED HERE, AND THAT IS THE DIFFERENCE FROM THE WRITE
    SIDE. ``install_heartbeat_instruction`` resolves the parent chain and
    deliberately not the final component, and this function was first
    written to match it. The shape transferred; the reasoning did not.
    ``install_helpers`` states its own licence for the exemption — *unlink
    removes a leaf symlink itself, which is safe* — and that argument is
    about removing a link in the bot's own tree. This function guards a
    PRIVILEGED READ: the ``evolve`` user holds ACL read on every bot's
    ``.openclaw``, including token-bearing 0600 files the bot itself cannot
    read, and whatever it returns gets copied into a pack that exists to be
    installed onto other bots. A bot could point ``workspace/notes.md`` at
    another bot's ``openclaw.json``, declare it in ``files[]``, and have the
    token packed. So:

      * the leaf is resolved along with the parents, and the whole resolved
        path must be inside the workspace — a symlink pointing back INTO
        the workspace is still fine, because its target is a file the bot
        already owns;
      * a symlinked leaf is refused outright even so. Resolving proves where
        the link points *now*, and the workspace is bot-writable, so a bot
        can swap the target between the check and the read. Refusing the
        link closes that race, and a pack should carry real files anyway.

    Returns None for anything that fails either test; the caller reports it
    as a note and never reads it.
    """
    target = workspace / rel
    try:
        if target.is_symlink():
            return None
        ws_real = workspace.resolve()
        target_real = target.resolve()
    except OSError:
        return None
    if ws_real != target_real and ws_real not in target_real.parents:
        return None
    return target


# ── Pack assembly ────────────────────────────────────────────────────────────


def _collect_entries(
    data: dict,
    workspace: Path,
    *,
    bot_id: str,
    bot_user: str,
    home_dir: Path,
    auto_detect: bool,
) -> tuple[list[_PackEntry], list[PackageFileNote]]:
    """Read + reverse-substitute every declared file. Report what could not be.

    Nothing is written here — the caller compares the result against the
    pack already on disk before deciding whether a write is needed at all,
    which is what makes a re-snapshot of unchanged content a no-op.
    """
    from .snapshot_engine import detect_and_substitute

    entries: list[_PackEntry] = []
    notes: list[PackageFileNote] = []

    for rel, source in _declared_files(data):
        target = _contained(workspace, rel)
        if target is None:
            # NOT ``unreadable``: this is a fact about the declaration, not a
            # permission failure, and the caller refuses the whole snapshot on
            # ``unreadable``. Conflating them would make a bad manifest row
            # look like an ACL regression.
            notes.append(PackageFileNote(
                rel, "outside_workspace",
                "declared path resolves outside the bot workspace",
            ))
            continue
        try:
            if target.is_dir():
                notes.append(PackageFileNote(
                    rel, "directory", "declared path is a directory"))
                continue
            if not target.is_file():
                notes.append(PackageFileNote(
                    rel, "missing", "declared but not on disk"))
                continue
            raw = target.read_bytes()
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError as exc:
            notes.append(PackageFileNote(rel, "unreadable", str(exc)))
            continue

        placeholders: list[str] = []
        bare_count = 0
        payload = raw
        if auto_detect:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # A binary file has no tokens to reverse-substitute; it
                # round-trips byte-for-byte, which is what the install path
                # does with an entry that declares no placeholders.
                text = None
            if text is not None:
                changed, placeholders, bare_count = detect_and_substitute(
                    text, bot_user, bot_id, home_dir=home_dir,
                )
                if changed != text:
                    payload = changed.encode("utf-8")

        entries.append(_PackEntry(
            path=rel,
            path_tokens=_path_tokens(rel, bot_id=bot_id, bot_user=bot_user),
            # Zero-padded so an exotic mode still matches the files-pack
            # loader's ``^0[0-7]{3,4}$`` — an unpadded ``0o0`` would emit
            # ``"00"`` and be rejected at load time, turning a snapshot into
            # a pack nothing can read.
            mode=f"0{mode:03o}",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            placeholders=placeholders,
            bare_token_count=bare_count,
            role=_role_of(source),
            payload=payload,
        ))
    return entries, notes


def _pack_is_current(dest: Path, entries: list[_PackEntry]) -> bool:
    """True when the pack on disk already IS this snapshot, byte for byte.

    Compares the declared rows AND re-verifies the on-disk content, because
    a manifest that still matches over a file that has drifted is exactly
    the state ``verify_files_pack_integrity`` exists to catch. Any
    disagreement — a new file, a dropped file, a changed mode, a changed
    placeholder list, drifted bytes — returns False and the pack is
    rewritten.
    """
    try:
        meta = load_files_pack_metadata(dest)
    except Exception:  # noqa: BLE001 — a malformed pack is simply not current
        return False
    if meta is None:
        return False
    have = [
        (f.path, f.mode, f.sha256, f.size_bytes, tuple(sorted(f.placeholders)))
        for f in meta.files
    ]
    want = [
        (e.path, e.mode, e.sha256, e.size_bytes, tuple(sorted(e.placeholders)))
        for e in entries
    ]
    if have != want:
        return False
    return not verify_files_pack_integrity(dest, meta)


def _existing_snapshot_at(dest: Path) -> str:
    """The ``snapshot_at`` already recorded in the pack, or "".

    Preserved across a no-op re-snapshot. Restamping it would move the
    pack's top-level sha on every run and make "nothing changed" indis-
    tinguishable from "everything changed", which is the whole point of the
    idempotence check above.
    """
    try:
        raw = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        value = (raw.get("snapshot_source") or {}).get("snapshot_at")
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _sweep_unaccounted(
    dest: Path, entries: list[_PackEntry], *, remove: bool,
) -> list[str]:
    """Pack files no manifest row accounts for. Optionally delete them.

    Two populations, and neither is visible to ``verify_files_pack_integrity``
    — that function walks ``metadata.files`` and has no concept of an on-disk
    file the manifest does not name, so a pack carrying junk reports intact
    forever:

      * a path a previous snapshot declared and this one does not;
      * a ``*.snapshot-tmp`` orphan, left when a write died between
        ``write_bytes`` and ``os.replace``.

    Reported on EVERY apply, not only when the content changed, because a
    pack that is byte-correct and carrying an orphan is not a pack anyone
    should call current. Empty directories left behind by a removal are
    pruned too, deepest first.
    """
    keep = {e.path for e in entries}
    found: list[str] = []
    try:
        for existing in sorted(dest.rglob("*")):
            if not existing.is_file() or existing.name == "manifest.json":
                continue
            rel = existing.relative_to(dest).as_posix()
            if rel in keep:
                continue
            found.append(rel)
            if remove:
                existing.unlink()
        if remove and found:
            for d in sorted((p for p in dest.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError as exc:
                    # ENOTEMPTY is the ordinary outcome — most directories
                    # still hold packed files and are meant to. Anything
                    # else is a fact about the pack worth a line in the log
                    # rather than a swallow.
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        logger.info(
                            "app_snapshot: could not prune empty dir %s (%s)",
                            d, exc,
                        )
    except OSError as exc:
        logger.info("app_snapshot: could not sweep %s (%s)", dest, exc)
    return found


def _reassert_modes(dest: Path, entries: list[_PackEntry]) -> list[str]:
    """Re-pin the pack's modes. Returns the paths that had to be repaired.

    THE SECURITY POSTURE HAS TO BE RE-ASSERTED, NOT JUST SET ONCE. The
    module docstring argues 0700/0600 because a pack holds bot-private
    content; that argument is one-shot if the modes are only applied on the
    write path. ``_ensure_pod_perms`` chmods ``apps`` and not ``-R``, so the
    0700 children survive it — and the corollary is that nothing repairs
    them either: ``apps/packs`` appears in neither
    ``deploy.EVOLVE_OWNED_SHARED_SUBDIRS``' per-file checks, nor
    ``secret_config_perms``, nor ``pod_perms_drift_monitor``. A pack that
    ever became 0644 — say via the ``chmod -R a+rX {shared_dir}`` re-exposer
    this repo has hit more than once — would stay 0644 forever, and a
    re-snapshot would report ``changed: False, verified: True`` and repair
    nothing.

    So this runs on every apply, including the unchanged path, and makes
    ``changed: False`` mean "the pack is correct" rather than "the bytes
    are correct". Compare-then-chmod, never unconditional: a chmod on a
    Linux directory carrying a POSIX ACL recalculates the mask from the new
    group bits.
    """
    repaired: list[str] = []
    targets = [dest / "manifest.json"] + [dest / e.path for e in entries]
    for target in targets:
        try:
            if not target.is_file():
                continue
            if stat.S_IMODE(target.stat().st_mode) != PACK_FILE_MODE:
                os.chmod(target, PACK_FILE_MODE)
                repaired.append(target.relative_to(dest).as_posix())
        except OSError as exc:
            logger.info("app_snapshot: could not re-pin %s (%s)", target, exc)
    for d in {(dest / e.path).parent for e in entries} | {dest, dest.parent}:
        _pin_dir_mode(d)
    return repaired


def _pin_dir_mode(component: Path) -> None:
    """Set ``component`` to ``PACK_DIR_MODE`` — compare first, chmod only on a
    mismatch, log rather than swallow when the chmod itself fails.

    The compare-first half is load-bearing on Linux: ``chmod`` on a directory
    carrying a POSIX ACL recalculates the mask from the new group bits and
    silently caps any named ACE on it. The log-rather-than-swallow half is
    the ordinary reason — a directory left at the wrong mode is a real fact,
    and a pack that is readable by more of the box than intended must not be
    the thing nobody was told about.
    """
    try:
        if stat.S_IMODE(component.stat().st_mode) != PACK_DIR_MODE:
            os.chmod(component, PACK_DIR_MODE)
    except OSError as exc:
        logger.info(
            "app_snapshot: could not set %s to %s (%s) — the pack is still "
            "evolve-owned; deploy.ensure_pod_perms owns the durable repair",
            component, oct(PACK_DIR_MODE), exc,
        )


def _ensure_pack_dirs(shared_dir: Path, app_id: str) -> Path:
    """``mkdir -p`` the pack directory chain and pin each level's mode.

    Compare-then-chmod, never chmod unconditionally: on Linux a ``chmod`` on
    a directory carrying a POSIX ACL recalculates the mask from the new
    group bits and silently caps any named ACE on it — the trap that has
    locked ``evolve`` out of shared directories before. ``{shared_dir}/apps``
    has no ACL on either pod today, but it sits beside ``signals/`` and
    ``proposals/``, which do, so an unconditional chmod here would be a
    standing invitation. Same guard, same reason, as
    ``app_spec_store.ensure_specs_dir``.
    """
    dest = pack_dir(shared_dir, app_id)
    dest.mkdir(parents=True, exist_ok=True)
    for component in (dest.parent, dest):
        _pin_dir_mode(component)
    return dest


def _write_pack(
    dest: Path, entries: list[_PackEntry], manifest: dict,
) -> tuple[list[str], str]:
    """Write the pack. Returns (removed_paths, error).

    Order is: remove what this snapshot does not declare, write the content,
    then ``manifest.json`` last. **A crash is survivable at every point but
    not harmless at all of them**, and the honest statement is worth more
    than the reassuring one an earlier version of this docstring made: a
    crash after the content write and before the manifest rewrite leaves a
    manifest that UNDER-claims, while a crash after the removals and before
    the manifest rewrite leaves one that OVER-claims — it still names files
    that were just deleted. Both self-heal on the next run, because
    ``_pack_is_current`` re-verifies the on-disk content and any mismatch
    rewrites the pack; neither is silently absorbed.

    Stale files are removed and reported rather than left to accumulate as
    content no manifest accounts for.
    """
    removed = _sweep_unaccounted(dest, entries, remove=True)
    try:
        for entry in entries:
            target = dest / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            _pin_dir_mode(target.parent)
            tmp = target.with_name(target.name + ".snapshot-tmp")
            tmp.write_bytes(entry.payload)
            os.chmod(tmp, PACK_FILE_MODE)
            os.replace(tmp, target)

        mf = dest / "manifest.json"
        tmp_mf = mf.with_name("manifest.json.snapshot-tmp")
        tmp_mf.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_mf, PACK_FILE_MODE)
        os.replace(tmp_mf, mf)
    except OSError as exc:
        return removed, f"pack_write_failed: {exc}"
    return removed, ""


# ── Shared-surface detection ─────────────────────────────────────────────────
#
# THE HEADING GRAMMAR IS SHARED, AND THAT IS THE POINT. Three questions get
# asked of the same markdown — "where does the section the manifest names
# start and end", "which sections carry a marker for this app", and "which
# sections are claimed by nobody" — and the first version of this module
# answered them with three separate regex walks. They disagreed, and every
# disagreement was a wrong number in an operator-facing report. They now all
# run off ``_headings``.


@dataclass(frozen=True)
class _Heading:
    """One ATX heading, with the two spans a reader needs.

    ``span`` runs to the next heading at the SAME or a HIGHER level — the
    section, and the right thing to digest. ``own`` stops at the next
    heading of ANY level — the scope marker attribution is allowed to see,
    so a marker on a ``###`` is not read as marking its ``##`` ancestor.
    """

    level: int
    text: str          # the heading line, verbatim: "## Task Manager"
    start: int
    span_end: int
    own_end: int


#: An ATX heading at column 0. Setext headings (``Title`` over ``=====``)
#: are NOT understood — see the ``limits`` list in the report.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$", re.MULTILINE)

#: A fenced code block delimiter — ``` or ~~~, optionally indented.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})", re.MULTILINE)


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks.

    Without this a ``# comment`` at column 0 inside a shell block is read as
    a heading: it mints a phantom section in the blind-spot count AND, worse,
    terminates the enclosing section early, so the content digest is taken
    over a truncated span. That digest is what the proposed ``footprint``
    field would key a revert on, so a silently-short span is not cosmetic.
    """
    out: list[tuple[int, int]] = []
    open_at: int | None = None
    marker = ""
    for m in _FENCE_RE.finditer(text):
        token = m.group(1)
        if open_at is None:
            open_at, marker = m.start(), token[0]
        elif token[0] == marker:
            out.append((open_at, m.end()))
            open_at = None
    if open_at is not None:          # unterminated fence runs to EOF
        out.append((open_at, len(text)))
    return out


def _headings(text: str) -> list[_Heading]:
    """Every ATX heading outside a fenced code block, with both spans."""
    fences = _fenced_ranges(text)
    raw = [
        m for m in _HEADING_RE.finditer(text)
        if not any(a <= m.start() < b for a, b in fences)
    ]
    out: list[_Heading] = []
    for i, m in enumerate(raw):
        level = len(m.group(1))
        span_end = len(text)
        for later in raw[i + 1:]:
            if len(later.group(1)) <= level:
                span_end = later.start()
                break
        own_end = raw[i + 1].start() if i + 1 < len(raw) else len(text)
        out.append(_Heading(level=level, text=m.group(0).strip(),
                            start=m.start(), span_end=span_end,
                            own_end=own_end))
    return out


def _anchor_text(anchor: str) -> str:
    """``## Foo`` and ``Foo`` collapse to the same key."""
    return anchor.lstrip("#").strip()


def _file_key(file: str) -> str:
    """The identity of a shared-surface FILE, normalized.

    The anchor axis was deduped and this one was not, so one section
    reported twice whenever two carriers spelled its file differently —
    ``HEARTBEAT.md`` from an install recipe, ``heartbeat.md`` from
    ``heartbeat_evidence`` (the scanner validates that field with
    ``.upper()``, so a lowercase spelling is well-formed by its own
    contract), ``./AGENTS.md`` from an LLM-written install block. The two
    deltas carried the same digest, which is what proved they were one
    section, and ``unattributed_sections`` counted the file's sections once
    per spelling.

    Casefolded because the mini's APFS is case-insensitive and the VPS's
    ext4 is not: without this the SAME manifest yields a doubled report on
    one pod and a false ``not_found`` on the other.
    """
    name = (file or "").strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return PurePosixPath(name).name.casefold()


def _section_bounds(
    text: str, anchor: str, *, prefer_pkg: str = "",
) -> tuple[int, int, int] | None:
    """``(start, span_end, own_end)`` for the section ``anchor`` names.

    Accepts both forms the carriers use: the install-config heading
    (``"## Task Manager — Check"``) and the artifact's bare-text form
    (``"Task Manager — Check"``, from ``installed_artifact``'s
    ``FILE#anchor``).

    ``prefer_pkg`` decides between DUPLICATE heading texts. Taking the first
    match unconditionally is wrong in a specific, silent way: a file with an
    unmarked ``## Notes`` above a marked ``## Notes`` reported the first —
    wrong attribution (``declared`` for a section that carries a marker),
    a digest over the wrong body, and the genuinely-unowned first section
    hidden from the blind-spot count, all from one input. Preferring an
    occurrence that carries this app's marker fixes all three.
    """
    want = _anchor_text(anchor)
    if not want:
        return None
    matches = [h for h in _headings(text) if _anchor_text(h.text) == want]
    if not matches:
        return None
    if prefer_pkg:
        for h in matches:
            if prefer_pkg in _marker_pkgs(text[h.start:h.own_end]):
                return h.start, h.span_end, h.own_end
    h = matches[0]
    return h.start, h.span_end, h.own_end


def _declared_sections(data: dict) -> list[tuple[str, str]]:
    """``(file, anchor)`` pairs the artifact itself claims, deduped.

    Three carriers, all of which exist on the live pod:
      * ``scheduled_actions[].install`` — ``{file, section_anchor}``, the
        v17 managed-section install recipe;
      * ``scheduled_actions[].installed_artifact`` — ``FILE#Anchor``, what
        the installer stamped back;
      * ``heartbeat_evidence`` — ``{file_path, section_anchors[]}``, what
        the scanner found for an app whose behaviour is instructions only.

    Deduped on ``(_file_key, _anchor_text)`` — BOTH axes. The carriers spell
    one section several ways on each axis independently, and deduping only
    the anchor left the file axis producing the very duplicate the anchor
    dedupe was added to remove.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(file: Any, anchor: Any) -> None:
        f = file.strip() if isinstance(file, str) else ""
        a = anchor.strip() if isinstance(anchor, str) else ""
        if not f or not a or not f.lower().endswith(".md"):
            return
        key = (_file_key(f), _anchor_text(a))
        if key not in seen:
            seen.add(key)
            out.append((f, a))

    for action in (data.get("scheduled_actions") or []):
        if not isinstance(action, dict):
            continue
        install = action.get("install")
        if isinstance(install, dict):
            _add(install.get("file"), install.get("section_anchor"))
        artifact = action.get("installed_artifact")
        if isinstance(artifact, str) and "#" in artifact:
            file_part, _, anchor_part = artifact.partition("#")
            _add(file_part, anchor_part)

    hb = data.get("heartbeat_evidence")
    if isinstance(hb, dict):
        for anchor in (hb.get("section_anchors") or []):
            _add(hb.get("file_path") or "HEARTBEAT.md", anchor)
    return out


def _config_deltas(data: dict) -> list[SharedSurfaceDelta]:
    """``installed_artifact`` values that point into a config file, not a doc.

    ``openclaw.json#hooks.heartbeat[2]`` is the documented shape. Reported
    with no digest: resolving an arbitrary key path inside a JSON document
    is a different operation from locating a markdown heading, and a guess
    here would be worse than an honest blank.
    """
    out: list[SharedSurfaceDelta] = []
    seen: set[str] = set()
    for action in (data.get("scheduled_actions") or []):
        if not isinstance(action, dict):
            continue
        artifact = action.get("installed_artifact")
        if not isinstance(artifact, str) or "#" not in artifact:
            continue
        file_part, _, key_part = artifact.partition("#")
        file_part, key_part = file_part.strip(), key_part.strip()
        if not file_part.lower().endswith(".json") or not key_part:
            continue
        if artifact in seen:
            continue
        seen.add(artifact)
        out.append(SharedSurfaceDelta(
            file=file_part,
            section=key_part,
            attribution=ATTR_CONFIG,
            sha256="",
            found=True,
            detail=(
                "config key declared by the manifest; content not digested — "
                "locating a JSON key path is not the markdown-heading "
                "operation and is not attempted here"
            ),
        ))
    return out


def _read_markdown(workspace: Path, rel: str) -> str | None:
    target = _contained(workspace, rel)
    if target is None:
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _marker_pkgs(section_text: str) -> list[str]:
    """The ``pkg=`` values in any evolve-managed marker inside ``section_text``."""
    pkgs: list[str] = []
    for m in _MANAGED_MARKER_RE.finditer(section_text):
        for token in (m.group("kv") or "").split():
            if token.startswith("pkg="):
                value = token[4:].strip()
                if value:
                    pkgs.append(value)
    return pkgs


def _detect_shared_surface(
    data: dict, workspace: Path, *, app_id: str,
) -> dict[str, Any]:
    """The report described in the module docstring. Read-only throughout."""
    # identity: see resolve_app_id — MARKER VOCABULARY, not an identity read.
    # ``install_helpers.install_heartbeat_instruction`` writes ``pkg=<pkg_id>``
    # into the ``<!-- evolve-managed: … -->`` marker and
    # ``remove_heartbeat_instruction`` parses it back out and compares it for
    # equality to decide whether teardown owns a section. This function is the
    # third reader of that same string, so it must read the SAME field those
    # two write and parse. Resolving the app's identity here instead would
    # compare an app_id against a marker that never contains one, and every
    # marked section would silently read as unattributed. The app's identity
    # arrives as the ``app_id`` argument, on its own axis.
    pkg_id = data.get("pkg_id")
    pkg_id = pkg_id.strip() if isinstance(pkg_id, str) else ""

    # One entry per FILE KEY, so a file spelled two ways is read, walked and
    # counted once. The spelling kept for display is whichever one actually
    # resolves on disk — on a case-sensitive pod only one of them will.
    spellings: dict[str, list[str]] = {}
    for candidate in list(SHARED_MARKDOWN_FILES) + [
        f for f, _ in _declared_sections(data)
    ]:
        spellings.setdefault(_file_key(candidate), []).append(candidate)

    texts: dict[str, str | None] = {}
    display: dict[str, str] = {}

    def _text(key: str) -> str | None:
        if key not in texts:
            texts[key] = None
            display[key] = (spellings.get(key) or [key])[0]
            for spelling in spellings.get(key, []):
                content = _read_markdown(workspace, spelling)
                if content is not None:
                    texts[key], display[key] = content, spelling
                    break
        return texts[key]

    deltas: list[SharedSurfaceDelta] = []
    #: (file_key -> [(start, span_end)]) for every section attributed to this
    #: app. A heading inside one of these spans is OWNED, not unattributed —
    #: attribution must not climb to an ancestor (which is why ``own`` exists)
    #: but it must descend, or every ``###`` inside a marked ``##`` is counted
    #: as content no app can be shown to own.
    claimed_spans: dict[str, list[tuple[int, int]]] = {}
    claimed_starts: set[tuple[str, int]] = set()

    def _claim(key: str, start: int, end: int) -> None:
        claimed_spans.setdefault(key, []).append((start, end))
        claimed_starts.add((key, start))

    # 1. Sections the artifact declares. Attribution upgrades to ``marker``
    #    when the file itself carries a matching evolve-managed marker.
    for file, anchor in _declared_sections(data):
        key = _file_key(file)
        text = _text(key)
        shown = display.get(key, file)
        if text is None:
            deltas.append(SharedSurfaceDelta(
                file=shown, section=anchor, attribution=ATTR_DECLARED,
                sha256="", found=False,
                detail="declared by the manifest; file absent or unreadable",
            ))
            continue
        bounds = _section_bounds(text, anchor, prefer_pkg=pkg_id)
        if bounds is None:
            deltas.append(SharedSurfaceDelta(
                file=shown, section=anchor, attribution=ATTR_DECLARED,
                sha256="", found=False,
                detail="declared by the manifest; section not present in the file",
            ))
            continue
        start, span_end, own_end = bounds
        body = text[start:span_end].rstrip()
        marked = _marker_pkgs(text[start:own_end])
        attribution = (
            ATTR_MARKER if (pkg_id and pkg_id in marked) else ATTR_DECLARED
        )
        detail = (
            "" if attribution == ATTR_MARKER else
            "no matching evolve-managed marker in the file — attribution "
            "rests on the manifest's claim, not on the file"
        )
        deltas.append(SharedSurfaceDelta(
            file=shown, section=anchor, attribution=attribution,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            found=True, detail=detail,
        ))
        _claim(key, start, span_end)

    # 2. Marked sections the artifact does NOT declare. A manifest can go
    #    stale; the marker in the file cannot, so a pkg-matching section the
    #    manifest forgot is still this app's footprint.
    if pkg_id:
        for key in list(spellings):
            text = _text(key)
            if text is None:
                continue
            for h in _headings(text):
                if (key, h.start) in claimed_starts:
                    continue
                if pkg_id not in _marker_pkgs(text[h.start:h.own_end]):
                    continue
                _claim(key, h.start, h.span_end)
                deltas.append(SharedSurfaceDelta(
                    file=display.get(key, key), section=h.text,
                    attribution=ATTR_MARKER,
                    sha256=hashlib.sha256(
                        text[h.start:h.span_end].rstrip().encode("utf-8")
                    ).hexdigest(),
                    found=True,
                    detail="marked for this app but not declared by the manifest",
                ))

    deltas.extend(_config_deltas(data))

    unattributed = _unattributed_sections(
        _text, display, list(spellings), claimed_spans,
    )
    return {
        "app_id": app_id,
        "pkg_id": pkg_id,
        "deltas": [d.to_dict() for d in deltas],
        # ``marker`` / ``declared`` / ``config`` partition the deltas by
        # attribution and sum to ``total``. ``not_found`` CUTS ACROSS them —
        # a declared section that is not in the file is counted in BOTH
        # ``declared`` and ``not_found`` — so ``located`` is given rather
        # than left to be derived, because a reader subtracting the wrong
        # pair gets a number that looks plausible and is not.
        "counts": {
            "marker": sum(1 for d in deltas if d.attribution == ATTR_MARKER),
            "declared": sum(1 for d in deltas if d.attribution == ATTR_DECLARED),
            "config": sum(1 for d in deltas if d.attribution == ATTR_CONFIG),
            "not_found": sum(1 for d in deltas if not d.found),
            "located": sum(1 for d in deltas if d.found),
            "total": len(deltas),
        },
        "carried_in_pack": False,
        "limits": [
            "an unmarked, undeclared edit is NOT detectable — a bot that "
            "appended to AGENTS.md in conversation left no marker and no "
            "manifest row, and nothing here attributes it to an app",
            "config-key deltas are reported without a content digest",
            "only ATX headings (``## Title``) are understood; a setext "
            "heading (``Title`` over ``=====``) is invisible to the walk, so "
            "a section written that way reads as not present rather than as "
            "found. The managed-section installer only ever writes ATX, so "
            "this can only hide a human- or bot-authored section",
            "detection is report-only: no delta is carried in the pack or "
            "the Spec, because a footprint/shared_edits field is a §5-freeze "
            "decision for the operator",
        ],
        "unattributed_sections": unattributed,
    }


def _unattributed_sections(
    read: Any, display: dict[str, str], keys: list[str],
    claimed_spans: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    """Size the blind spot: sections carrying no marker and claimed by nobody.

    BOT-SCOPED CONTEXT, not this app's footprint, and labelled as such in
    the payload. It is here because the §5-freeze proposal in the PR body
    needs a number for "how much of a bot's shared surface is content no
    app can be shown to own", and a number nobody measured is the kind of
    thing that gets guessed at instead. Which is also why the two ways it
    over-counted had to go: a number that double-counts is worse than an
    unmeasured one, because it is believed.

    A heading is excluded when it falls INSIDE a span this app was
    attributed (so subsections of an owned block are not counted as
    unowned), or when its own text carries any evolve-managed marker (so a
    block owned by a DIFFERENT app is not counted against this bot either).
    """
    listed: list[dict[str, str]] = []
    total = 0
    for key in keys:
        text = read(key)
        if text is None:
            continue
        spans = claimed_spans.get(key, [])
        for h in _headings(text):
            if any(a <= h.start < b for a, b in spans):
                continue
            if _MANAGED_MARKER_RE.search(text[h.start:h.own_end]):
                continue
            total += 1
            if len(listed) < _MAX_UNATTRIBUTED_LISTED:
                listed.append({"file": display.get(key, key),
                               "section": h.text})
    return {
        "count": total,
        "listed": listed,
        "truncated": total > len(listed),
        "note": (
            "sections in this BOT's shared markdown that carry no "
            "evolve-managed marker and are claimed by no manifest row. Not "
            "this app's footprint — the size of what attribution cannot see."
        ),
    }


# ── The Spec side ────────────────────────────────────────────────────────────


def _bumped_spec_version(current: int) -> int:
    """The next ``spec_version`` for a pack whose CONTENT moved.

    design §5 fixes ``spec_version`` as a monotonic int per ``app_id`` and
    ``app_spec.derive_spec_version`` packs it as ``YYYYMMDD * 100 + major*10 +
    minor``, so today's v1.0 is the natural next value — and ``max`` with
    ``current + 1`` keeps it monotonic on a pod where the existing value was
    already minted ahead (or twice in one day).
    """
    today = int(datetime.now(timezone.utc).strftime("%Y%m%d")) * 100 + 10
    return max(today, int(current) + 1)


def _spec_for(
    data: dict, shared_dir: Path, app_id: str, entries: list[_PackEntry],
    *, bump: bool = False,
) -> AppSpec:
    """The Spec to write: whatever the app already has, with SOURCE digests.

    An existing Spec wins for every field but ``package`` — it may carry
    operator edits — and for ``spec_version``, which moves only when ``bump``
    says the pack's CONTENT changed. Only when none exists is one derived from
    the artifact, through ``spec_from_artifact`` so the derivation stays the
    single one the census and the reflex writer share.

    ``package.files[]`` is then replaced wholesale by the pack's rows. Not
    merged: a file the pack could not include is a file a deterministic
    install cannot materialize, and leaving its stale workspace digest in
    place would claim otherwise.

    WHY ``bump`` EXISTS NOW AND DID NOT BEFORE (AL-3.2). This module's own
    record says a version bump is *"the install surface's decision to make
    when there is an install to pin"*, and declined to take it on three
    grounds: nothing consumed a pack, a no-op re-snapshot must not move the
    version, and the decision was not this chip's. All three are settled —
    ``app_install`` consumes the pack, ``changed`` already separates a real
    re-snapshot from a no-op, and AL-3.2 is that install surface. Without the
    bump, ``Update to vN`` is unreachable by construction: a re-snapshot after
    a change would leave every bot's pinned version equal to the app's, and the
    button that exists to move them would never appear.

    The caller passes ``changed``, so an idempotent re-snapshot still moves
    nothing — the property this module promises is intact.
    """
    base = load_spec(shared_dir, app_id)
    if base is None:
        base = spec_from_artifact(data)
    payload = base.to_dict()
    payload["app_id"] = app_id
    if bump:
        payload["spec_version"] = _bumped_spec_version(base.spec_version)
    payload["package"] = {
        **payload.get("package", {}),
        "files": [
            {"path": e.path, "sha256": e.sha256, "role": e.role}
            for e in entries
        ],
    }
    return AppSpec.from_dict(payload)


# ── The entrypoint ───────────────────────────────────────────────────────────


def snapshot_app(
    bot_id: str,
    app_id: str,
    *,
    shared_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    network: dict | None = None,
    auto_detect: bool = True,
    update_spec: bool = True,
    dry_run: bool = False,
) -> dict:
    """Snapshot the defined app ``app_id`` on ``bot_id`` into a files-pack.

    Args:
      bot_id: the bot whose workspace holds the app's files.
      app_id: the app's conferred identity (``resolve_app_id``). A draft has
        none and is refused.
      shared_dir: pod shared dir; resolved from ``network.json`` when omitted.
      workspace: the bot's workspace; resolved from the bot when omitted.
        Injectable so a test does not need a real account on the box.
      network: pre-loaded ``network.json``, else loaded.
      auto_detect: run reverse-substitution on each file's content. When
        False the files are packed verbatim and every ``placeholders`` list
        stays empty — an honest pack that installs only onto the same bot.
      update_spec: write the Spec's ``package{}`` back. False leaves the
        Spec untouched while the pack is still written.
      dry_run: do the whole read half — resolve, hash, reverse-substitute,
        detect shared surfaces, decide whether anything CHANGED — and write
        neither the pack nor the Spec. Not a separate code path: the same
        computation runs, only the two writes are skipped, so a dry run
        cannot report something the apply would not do.

    Returns an envelope. On success:
      ``{ok, error: "", refused: False, app_id, bot_id, pack_dir, files_count,
          format_version, snapshot_at, changed, removed[], per_file[],
          notes[], spec_path, spec_written, verified, dry_run,
          shared_surface{}}``

    ``verified`` says whether ``verify_files_pack_integrity`` actually ran
    clean against a pack on disk. It is False on a dry run that would write
    a new pack — there is nothing to verify yet, and claiming otherwise
    would be the vacuous pass this arc has caught twice already.

    ``changed`` is False when the pack on disk already was this exact
    snapshot: no content is rewritten and ``snapshot_at`` is the ORIGINAL
    stamp, so a re-snapshot of unchanged content leaves the pack's
    top-level digest where it was. It is NOT a no-op in the wider sense,
    deliberately — the modes are re-pinned and unaccounted files are swept
    on every apply (``repaired_modes``, ``removed``), because a pack whose
    bytes are right and whose permissions are wrong is not a pack anyone
    should call current, and the top-level digest is the digest of
    ``manifest.json`` alone and so is blind to both by construction.

    ``partial`` mirrors "some declared file did not make it into the pack",
    and is written into the pack metadata as well as returned.

    On refusal or failure ``ok`` is False and ``error`` names the class;
    ``refused`` separates the two (see the module docstring).
    """
    from ..config import DEFAULT_SHARED_DIR, bot_home, get_bot_user, load_network

    app_id = app_id.strip() if isinstance(app_id, str) else ""
    bot_id = bot_id.strip() if isinstance(bot_id, str) else ""
    if not bot_id:
        return _err("missing_bot_id", refused=True)
    if not app_id:
        return _err(
            "draft_refused: no app_id — a discovered draft carries a "
            "draft_id and no portable identity to file a pack under "
            "(design §3); identity is conferred at promotion",
            refused=True,
        )
    if not is_canonical_app_id(app_id):
        return _err(
            f"invalid_app_id: {app_id!r} does not match "
            f"app_identity.APP_ID_PATTERN",
            refused=True,
        )

    if network is None:
        try:
            network = load_network()
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            return _err(f"network_load_failed: {exc}")

    if shared_dir is None:
        shared_dir = Path(network.get("sharedDir") or DEFAULT_SHARED_DIR)
    shared_dir = Path(shared_dir)

    try:
        bot_user = get_bot_user(bot_id, network)
    except Exception as exc:  # noqa: BLE001
        return _err(f"bot_user_resolve_failed: {exc}")
    try:
        home = bot_home(bot_id, network)
    except Exception as exc:  # noqa: BLE001
        return _err(f"bot_home_resolve_failed: {exc}")

    ws = Path(workspace) if workspace else (home / ".openclaw" / "workspace")
    # ``exists_or_unreachable`` rather than ``is_dir()``: on Linux a
    # ``.openclaw`` the OC gateway hardened to 0700 clamps evolve's inherited
    # traverse ACE and a plain existence check RAISES. Unreachable classifies
    # as present, and the listing below is what reports it — which is the
    # right split, because "not deployed" and "deployed but walled off" need
    # different operator sentences.
    from ..secret_config_perms import exists_or_unreachable
    if not exists_or_unreachable(ws):
        return _err(f"workspace_missing: {ws} not found. Has {bot_id} been deployed?")

    found, unreadable = _find_artifact(ws, app_id)
    if unreadable:
        return _err(
            f"workspace_unreadable: could not list {unreadable} — the "
            f"evolve user's read ACL on this bot's workspace is not in "
            f"place. This is NOT 'the app is not here'; nothing was ruled "
            f"out. Re-run the bot deploy to re-grant, then retry."
        )
    if found is None:
        return _err(
            f"app_not_found: no manifest under {ws}/manifests/ resolves to "
            f"app_id={app_id!r} on {bot_id}",
            refused=True,
        )
    data, manifest_path = found

    if draft_id_of(data):
        return _err(
            f"draft_refused: {manifest_path.name} carries a draft_id; a "
            f"draft's identity is explicitly unstable (design §3) and must "
            f"not be filed under an app_id",
            refused=True,
        )
    from .pod_apps import DEFINED, definition_status_of
    if definition_status_of(data) != DEFINED:
        return _err(
            f"not_a_defined_app: {manifest_path.name} is "
            f"{definition_status_of(data)!r}, not {DEFINED!r} — promote it "
            f"first; a snapshot packages a defined app's files, it does not "
            f"confer definition",
            refused=True,
        )

    entries, notes = _collect_entries(
        data, ws, bot_id=bot_id, bot_user=bot_user, home_dir=home,
        auto_detect=auto_detect,
    )
    shared_surface = _detect_shared_surface(data, ws, app_id=app_id)

    # READ-DENIED IS NOT WRITE-ALLOWED. ``missing`` is a fact — stat says the
    # file is not there. ``unreadable`` is the ABSENCE of a fact: an ACL that
    # regressed looks exactly like a file that was deleted, and writing a Spec
    # whose package.files[] silently lost those entries would turn "we could
    # not look" into "the app does not have them". Refuse the whole snapshot
    # and say which files could not be read.
    denied = [n for n in notes if n.kind == "unreadable"]
    if denied:
        return _err(
            "read_denied: could not read "
            + ", ".join(n.path for n in denied[:5])
            + (f" (+{len(denied) - 5} more)" if len(denied) > 5 else "")
            + " — an unreadable file is not a file that is gone, and a pack "
              "written without it would claim the app is smaller than it is. "
              "Re-grant the evolve read ACL (re-run the bot deploy) and retry.",
            app_id=app_id, bot_id=bot_id,
            notes=[n.__dict__ for n in notes],
            shared_surface=shared_surface,
        )

    declared_count = len(_declared_files(data))
    if not entries:
        if declared_count == 0:
            # An instruction-only app — behaviour in an AGENTS.md section, no
            # files at all — is a HEALTHY state, and 9 of the 11 non-clean
            # rows in this chip's live measure are exactly that. Reporting it
            # as a failure is the "a design-§3 decline reported as FAILED
            # makes a healthy path read like an outage" mistake AL-1.5b's
            # review caught, and it is also precisely the population the
            # operator addition is about: everything such an app does lives
            # in the shared surface, which is reported below and which no
            # pack can carry today.
            return _err(
                f"nothing_to_pack: {app_id} declares no files — its behaviour "
                f"lives in shared surfaces, not in a package. The "
                f"shared-surface report is still attached.",
                refused=True,
                app_id=app_id, bot_id=bot_id,
                notes=[n.__dict__ for n in notes],
                shared_surface=shared_surface,
            )
        return _err(
            f"no_packable_files: {app_id} declares {declared_count} file(s), "
            f"none of which could be read from {ws}",
            refused=False,
            app_id=app_id, bot_id=bot_id,
            notes=[n.__dict__ for n in notes],
            shared_surface=shared_surface,
        )

    try:
        dest = (pack_dir(shared_dir, app_id) if dry_run
                else _ensure_pack_dirs(shared_dir, app_id))
    except (OSError, ValueError) as exc:
        return _err(f"pack_dir_failed: {exc}", app_id=app_id, bot_id=bot_id)

    changed = not _pack_is_current(dest, entries)
    removed: list[str] = []
    repaired_modes: list[str] = []
    snapshot_at = "" if changed else _existing_snapshot_at(dest)
    if changed and not dry_run:
        snapshot_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "format_version": FILES_PACK_FORMAT_VERSION,
            # The SOURCE bot id is deliberately absent — it is a reserved
            # token, and a pack exists to be installed somewhere else.
            "snapshot_source": {
                "app_id": app_id,
                "snapshot_at": snapshot_at,
                "snapshot_by": SNAPSHOT_BY,
            },
            # DURABLE, because the envelope is not. ``notes`` says which
            # declared files did not make it in, and it lives exactly as long
            # as the CLI process or the HTTP response. On disk the pack would
            # otherwise be indistinguishable from one that is complete —
            # which is the module's own "report, never silently drop" rule
            # broken at the only layer that outlives the call.
            # ``FilesPackMetadata.partial`` was built for this
            # ("deliberately partial: the manifest's files[] may declare paths
            # not present here"), and the integrity sweep already downgrades
            # orphan declarations to warnings when it is set.
            "partial": bool(notes),
            **({"coverage_intent": "workspace_snapshot_with_gaps"}
               if notes else {}),
            "files": [e.manifest_entry() for e in entries],
        }
        removed, write_err = _write_pack(dest, entries, manifest)
        if write_err:
            return _err(write_err, app_id=app_id, bot_id=bot_id,
                        notes=[n.__dict__ for n in notes],
                        shared_surface=shared_surface)
    elif not dry_run:
        # The unchanged path still owns the pack's POSTURE. Content equality
        # is not correctness: a widened mode or an orphan tmp file makes a
        # byte-identical pack the wrong pack, and neither moves any digest.
        removed = _sweep_unaccounted(dest, entries, remove=True)
    if not dry_run:
        repaired_modes = _reassert_modes(dest, entries)

    # The pack must verify before this call reports success. A pack that
    # would refuse to install is not a snapshot that worked. On a dry run
    # that WOULD write, there is no pack yet: ``verified`` comes back False
    # rather than being asserted over nothing.
    verified = False
    if not (dry_run and changed):
        meta = load_files_pack_metadata(dest)
        if meta is None:
            return _err(
                "pack_verify_failed: no manifest.json at " + str(dest),
                app_id=app_id, bot_id=bot_id,
                notes=[n.__dict__ for n in notes],
                shared_surface=shared_surface,
            )
        findings = verify_files_pack_integrity(dest, meta)
        if findings:
            return _err(
                "pack_verify_failed: " + "; ".join(
                    f"{f.path}: {f.kind} ({f.detail})" for f in findings[:5]
                ),
                app_id=app_id, bot_id=bot_id,
                notes=[n.__dict__ for n in notes],
                shared_surface=shared_surface,
            )
        verified = True

    spec_path_str = ""
    spec_written = False
    if update_spec and not dry_run:
        try:
            spec = _spec_for(data, shared_dir, app_id, entries, bump=changed)
            spec_path_str = str(write_spec(spec, shared_dir))
            spec_written = True
        except (OSError, ValueError) as exc:
            return _err(
                f"spec_write_failed: {exc}", app_id=app_id, bot_id=bot_id,
                pack_dir=str(dest), notes=[n.__dict__ for n in notes],
                shared_surface=shared_surface,
            )

    return _ok(
        app_id=app_id,
        bot_id=bot_id,
        pack_dir=str(dest),
        manifest_source=str(manifest_path),
        files_count=len(entries),
        format_version=FILES_PACK_FORMAT_VERSION,
        snapshot_at=snapshot_at,
        changed=changed,
        removed=removed,
        repaired_modes=repaired_modes,
        partial=bool(notes),
        verified=verified,
        dry_run=dry_run,
        per_file=[{
            "path": e.path, "mode": e.mode, "sha256": e.sha256,
            "size_bytes": e.size_bytes, "placeholders": list(e.placeholders),
            "bare_token_count": e.bare_token_count, "role": e.role,
            "path_tokens": list(e.path_tokens),
        } for e in entries],
        notes=[n.__dict__ for n in notes],
        spec_path=spec_path_str,
        spec_written=spec_written,
        shared_surface=shared_surface,
    )
