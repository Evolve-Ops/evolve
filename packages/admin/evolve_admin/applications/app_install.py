"""app_install — AL-3.2: the deterministic install, and the merge that updates it.

Brief: ``internal/dispatch/done/al-3-2-install-to.md``. Design:
``internal/design-apps-surface-2026-08-16.md`` §4/§5 (Install to… / Update to
vN on App detail) and ``internal/design-app-spec-and-discovery-2026-08-15.md``
§6, which fixes what an install IS:

    *"Install = materialize ``package.files`` (sha-verified — the existing
    ``files_pack`` fast path becomes the ONLY path), write the instance …
    No LLM in the install path. Same spec → same files on every bot; ``config``
    carries the differences."*

AL-3.1 made that reachable for the born-in-place population by giving every
defined app a pack at ``{shared_dir}/apps/packs/<app_id>/``. This module is the
consumer: pack → target bot, plus the proof that the result is what design §6
claims, plus the update path.

WHAT "DETERMINISTIC" MEANS HERE, IN THE RATIFIED WORDING (1.5 roadmap row +
#3712, and ``tests/test_al_1_5c_determinism.py``'s module docstring at length):
identical SOURCE sha sets; realized shas differ ONLY by declared substitution,
machine-checked. Not "should differ only by" — every install run here re-derives
each file from (pack source bytes + that file's declared placeholders + this
bot's context) and compares the derivation against **what is on the target's
disk afterwards**. A file whose realized digest is not reproducible that way
fails the install rather than being written down as successful, which is the
1.5c property (``test_realized_difference_is_fully_explained_by_substitution``)
run against a real install instead of a fixture.

THE THREE THINGS THAT MAKE THIS NOT A ``cp -r``:

  1. **Nothing is overwritten by an install.** Every file is created with the
     helper's create-only mode (``os.link``, ``EEXIST``). A pre-existing file at
     a pack path is a COLLISION: the whole install refuses and names them. The
     alternative — clobbering a file the app does not own — is the flatten D-L3
     exists to prevent, arriving through the front door.
  2. **A partial install is never written down as an install.** The per-file
     failures come back as their own channel (``failed[]``) and the instance
     manifest is NOT written, because a manifest is the pod's statement that the
     app IS here. Half a manifest's worth of files with a whole manifest over
     them is the state that makes every later reader wrong.
  3. **The installed instance ADOPTS the source app_id.** It does not mint one.
     The manifest written on the target carries the same ``app_id``, so
     ``resolve_app_id`` returns the source's id and ``pod_apps`` groups the two
     installs as ONE app with two bots — which is the whole point of
     install-once-target-many. (ALPHA-3b generalizes adoption to *promotion*;
     this is the same rule at the *install* boundary, and it is not the same
     code path — nothing here consults an equivalence threshold, because there
     is no equivalence question to ask: the target is being handed this app's
     pack, so it is this app.)

WHY THE WRITE GOES THROUGH ``marker_embed_helper --install`` AND NOT THROUGH
``files_pack.install_files_pack_to_workspace``. Two independent reasons, and
the first one is fatal on a real pod:

  * **The daemon cannot write there.** The admin server runs as ``evolve``,
    whose ACL on a bot's ``.openclaw`` is READ, plus write on ``workspace/``
    root FILES (``deploy.WORKSPACE_ROOT_WRITE_ACL_PERMS`` — ``file_inherit``,
    deliberately no ``directory_inherit``) and on ``workspace/evolve`` /
    ``manifests`` / ``evolve-backup``. The pod's apps live in
    ``workspace/scripts/`` (all six entries of ``gallery/ea-pack``), which is
    read-only to the daemon. ``install_files_pack_to_workspace`` writes
    directly, which is correct where it runs — the forge install path executes
    **bot-side** (``packages/analyzer/forge_runner.py`` is invoked by the bot's
    own session) — and cannot work from here. ``marker_embed_helper`` is the
    existing, already-granted privileged seam for exactly this shape ("evolve
    has ACL READ but not write on those paths … a fixed-path helper that
    validates its own args is the right shape"), so this chip adds a mode to it
    rather than a sudoers wildcard.
  * **That primitive overwrites unconditionally.** It has no create-only mode,
    no expected-digest guard, and it reports the digest of what it *wrote*
    rather than of what is on disk. Points 1 and 2 above are not expressible
    through it. The substitution engine is still single-sourced —
    ``files_pack.substitute_placeholders`` is called here and nowhere is a
    second one written.

UPDATE IS A MERGE, NOT A REPLACE (PM amendment 2026-08-24, D-L3). An instance
may carry local adaptations, and applying a newer ``spec_version`` must not
flatten them silently. How adaptation is detected, stated honestly because the
growth log the amendment refers to **does not exist in this repo yet** (no
``growth log`` producer is on disk; ``internal/dispatch/queued/growth-log-
observer.md`` is still queued, and ``internal/design-app-lineage-2026-08-24.md``
is not in the tree either — the amendment's own citation):

    ``recorded_install``  the instance records the digest this installer wrote
                          for that file. On-disk == recorded ⇒ untouched since
                          install. This is the strongest discriminator and the
                          only one that stays right across a version bump.
    ``current_pack``      no recorded digest (an instance this installer did
                          not write). Fall back to 1.5c's property against the
                          CURRENT pack: on-disk == substitute(source, declared,
                          as-installed context) ⇒ unadapted. Weaker — it cannot
                          tell "locally edited" from "the new version changed
                          this file" — so it is reported as the basis it is.
    ``none``              neither is available ⇒ **adapted**, conservatively.

Both bases read a file the BOT can write (its own instance manifest), and
that is stated rather than hidden: a bot that forged either one would change
the answer about ITS OWN copy, in one of two directions — claim "adapted" and
the update stops and asks a human, or claim "unadapted" and the bot loses its
own local change. Neither reaches another bot, and neither produces a silent
wrong answer about someone else's app, which is why the carrier is acceptable
here and would not be if it were pod-wide.

Anything not proven unadapted goes down the conservative path: the update
reports exactly what would be overwritten and refuses without an explicit
``confirm_overwrite``. And a confirmed overwrite still passes the measured
digest to the helper, so a file that changed between the preview and the apply
refuses root-side rather than being flattened by a stale confirmation.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .app_identity import is_canonical_app_id, resolve_app_id
from .app_snapshot import pack_dir
from .app_spec import AppSpec, spec_from_manifest
from .app_spec_store import load_spec, spec_envelope
from .files_pack import (
    FilesPackError,
    FilesPackMetadata,
    compute_files_pack_sha256,
    load_files_pack_metadata,
    resolve_install_context,
    substitute_placeholders,
    verify_files_pack_integrity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "INSTALL_BY",
    "PlannedFile",
    "install_app_to_bot",
    "installability",
    "update_app_on_bot",
]

INSTALL_BY = "app_install.install_app_to_bot"
UPDATE_BY = "app_install.update_app_on_bot"

#: ``install{}`` sub-key on an instance this module wrote. Named rather than
#: reused from ``provenance``: provenance answers "where did this APP come
#: from" (design §5) and is the same on every bot; this answers "how did this
#: BOT get its copy", which is per-install and would be a lie if it travelled
#: with a shared Spec.
INSTALL_BLOCK = "install"

#: Adaptation-detection bases — see the module docstring.
BASIS_RECORDED = "recorded_install"
BASIS_PACK = "current_pack"
BASIS_NONE = "none"

#: Per-file plan states.
STATE_CREATE = "create"          # not on the target; will be created
STATE_COLLISION = "collision"    # already there, and the app did not put it there
STATE_UNADAPTED = "unadapted"    # matches what was installed; safe to replace
STATE_ADAPTED = "adapted"        # differs; a replace would destroy local work


# ── Envelopes ────────────────────────────────────────────────────────────────


def _err(error: str, *, refused: bool = False, **extra: Any) -> dict:
    """Failure (or refusal) envelope — the same split ``app_snapshot`` uses.

    ``refused`` is "this is not a thing that can be installed" (already there,
    no pack, a draft); everything else is "the install broke". Both are
    ``ok=False`` and only one is a problem, which is the distinction AL-1.5b's
    review found being lost on a status page.
    """
    return {"ok": False, "error": error, "refused": refused, **extra}


def _ok(**extra: Any) -> dict:
    return {"ok": True, "error": "", "refused": False, **extra}


def _now() -> str:
    """UTC stamp in the shape ``app_snapshot`` writes, to the second.

    Same format on both sides so a ``{installed_at}`` placeholder realized by
    an install and re-derived by the proof cannot differ by formatting.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ── Per-file plan ────────────────────────────────────────────────────────────


@dataclass
class PlannedFile:
    """One pack entry resolved against one target bot, before anything is written.

    ``predicted_sha`` is the digest the file WILL have once installed, derived
    from (pack source bytes + this entry's declared placeholders + this bot's
    context) and from nothing else. It is computed before the write and checked
    against the disk after it, which is what turns design §9's determinism
    clause into something this module can fail on.
    """

    path: str                 # the pack's path (the app's, pre-substitution)
    rel: str                  # workspace-relative target (path placeholders resolved)
    mode: str
    source_sha: str
    predicted_sha: str
    payload: bytes = field(repr=False, default=b"")
    state: str = STATE_CREATE
    current_sha: str = ""
    basis: str = BASIS_NONE
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rel": self.rel,
            "mode": self.mode,
            "source_sha": self.source_sha,
            "predicted_sha": self.predicted_sha,
            "state": self.state,
            "current_sha": self.current_sha,
            "basis": self.basis,
            "note": self.note,
        }


# ── Bot + pack resolution ────────────────────────────────────────────────────


def _resolve_bot(bot_id: str, network: dict) -> "tuple[str, Path, Path, str]":
    """``(bot_user, home, workspace, error)`` for ``bot_id``."""
    from ..config import bot_home, get_bot_user

    try:
        bot_user = get_bot_user(bot_id, network)
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        return "", Path(), Path(), f"bot_user_resolve_failed: {exc}"
    try:
        home = bot_home(bot_id, network)
    except Exception as exc:  # noqa: BLE001
        return "", Path(), Path(), f"bot_home_resolve_failed: {exc}"
    return bot_user, home, home / ".openclaw" / "workspace", ""


def _iter_manifests(workspace: Path) -> "tuple[list[Path], str]":
    """``(paths, unreadable_reason)`` for ``workspace/manifests``.

    ``os.listdir`` rather than a glob, and an unlistable directory reported as
    unreadable rather than as empty — both for the reasons
    ``app_snapshot._iter_manifest_paths`` records: pathlib's glob scandirs each
    parent (which returns ``[]`` on a Linux pod where ``evolve`` is
    traverse-only on ``/home/<bot>``), and a flat negative from a failed read is
    how "we could not look" becomes "it is not there".
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


def _find_instance(
    workspace: Path, app_id: str,
) -> "tuple[tuple[dict, Path] | None, str]":
    """``(found, unreadable_reason)`` — this bot's manifest for ``app_id``.

    Keyed on ``resolve_app_id`` over the RAW manifest, never a local
    ``spec_id or id`` chain (AL-1.5's standing guardrail): hydration rewrites
    ``id``/``pkg_id`` and would answer for a different key than the one the
    Spec and the pack are filed under.
    """
    import json

    paths, unreadable = _iter_manifests(workspace)
    if unreadable:
        return None, unreadable
    for cand in paths:
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and resolve_app_id(data) == app_id:
            return (data, cand), ""
    return None, ""


def _load_pack(shared_dir: Path, app_id: str) -> "tuple[Path | None, FilesPackMetadata | None, str]":
    """``(dir, metadata, error)`` for the app's pack, verified before it is used.

    ``dir`` is None only when ``app_id`` cannot become a path component at all
    — the same gate ``app_spec_store.spec_path`` applies. Every caller narrows
    on ``meta is None or dest is None`` together, because a pack with metadata
    always has a directory and the pair is one answer, not two.

    ``verify_files_pack_integrity`` runs HERE and not only at snapshot time,
    because the pack lives on disk between the two and a pack that has drifted
    is precisely the thing design §6's "sha-verified" clause is about. The pack
    — never the Spec's mirror of its digests — is the authority for source
    digests (AL-3.1 §9.2's conditional resolution: "a verify-on-install that
    reads the pack is correct by construction; one that trusts the Spec's
    mirror is not, and that is the rule AL-3.2 has to be built on").
    """
    try:
        dest = pack_dir(shared_dir, app_id)
    except ValueError as exc:
        return None, None, f"invalid_app_id: {exc}"
    try:
        meta = load_files_pack_metadata(dest)
    except FilesPackError as exc:
        return dest, None, f"pack_unreadable: {exc}"
    if meta is None:
        return dest, None, (
            f"no_pack: {app_id} has no files-pack at {dest}. A deterministic "
            f"install materializes a pack; snapshot the app on a bot that has "
            f"it first (AL-3.1)."
        )
    findings = verify_files_pack_integrity(dest, meta)
    if findings:
        return dest, None, "pack_verify_failed: " + "; ".join(
            f"{f.path}: {f.kind} ({f.detail})" for f in findings[:5]
        )
    return dest, meta, ""


def _context(
    *, bot_id: str, bot_user: str, workspace: Path, app_id: str,
    pkg_id: str, installed_at: str, shared_dir: Path,
) -> "tuple[dict[str, str], str]":
    """``(context, error)`` for the substitution pass.

    ``pkg_id`` and ``app_id`` are two DISTINCT names in the format-versioned
    substitution vocabulary (``files_pack.KNOWN_PLACEHOLDERS``), not two
    spellings of one id — see ``resolve_app_id``. A snapshot pack has no gallery
    package behind it, so there may be no real ``pkg_id`` to supply; the caller
    guards that case before calling (``_pkg_id_for``) rather than letting the
    app's identity be substituted in place of a package key.
    """
    try:
        return resolve_install_context(
            bot_id=bot_id,
            bot_user=bot_user,
            workspace=str(workspace),
            pkg_id=pkg_id,
            app_id=app_id,
            installed_at=installed_at,
            shared_dir=str(shared_dir),
        ), ""
    except FilesPackError as exc:
        return {}, f"context_unresolved: {exc}"


def _pkg_id_for(meta: FilesPackMetadata, source_pkg_id: str, app_id: str) -> "tuple[str, str]":
    """``(pkg_id, error)`` for the install context.

    ``resolve_install_context`` refuses an empty value for every placeholder,
    including ones no file uses — so a pack with no gallery package behind it
    still needs SOMETHING in the ``pkg_id`` slot. Passing the app_id there is
    only safe while no file can actually consume it, so that is checked rather
    than assumed: an entry that declares ``{pkg_id}`` with no real package key
    to resolve is a refusal, not a quiet substitution of one identity for
    another.
    """
    if source_pkg_id:
        return source_pkg_id, ""
    declares = sorted({
        e.path for e in meta.files
        if "pkg_id" in (e.placeholders or []) or "pkg_id" in (e.placeholders_in_path or [])
    })
    if declares:
        return "", (
            "pkg_id_unresolved: " + ", ".join(declares[:5]) + " declare the "
            "{pkg_id} placeholder, but this app has no gallery package key to "
            "resolve it with. Substituting the app's identity there would "
            "write one id into a slot that means another."
        )
    # Unconsumable filler: no entry can substitute it, and the placeholder
    # vocabulary requires the slot to be non-empty.
    return app_id, ""


# ── Planning ─────────────────────────────────────────────────────────────────


def _plan_entries(
    meta: FilesPackMetadata, pack: Path, workspace: Path, context: dict[str, str],
) -> "tuple[list[PlannedFile], str]":
    """Resolve + substitute every pack entry. ``(planned, error)``.

    Nothing is written and nothing on the target is consulted here: this is the
    "what would this install put on disk, and what digest would each file have"
    half, so a dry run and an apply cannot disagree about the content.
    """
    planned: list[PlannedFile] = []
    for entry in meta.files:
        src = pack / entry.path
        if not src.is_file():
            return [], (
                f"pack_incomplete: {entry.path} is declared in the pack "
                f"manifest but is not on disk at {src}"
            )
        try:
            if entry.placeholders or entry.placeholders_in_path:
                text = src.read_text(encoding="utf-8")
                payload = substitute_placeholders(
                    text, entry.placeholders, context).encode("utf-8")
            else:
                payload = src.read_bytes()
        except UnicodeDecodeError as exc:
            return [], (
                f"pack_unreadable: {entry.path} declares placeholders but is "
                f"not utf-8 text: {exc}"
            )
        except (OSError, FilesPackError) as exc:
            return [], f"pack_unreadable: {entry.path}: {exc}"

        rel = entry.path
        if entry.placeholders_in_path:
            try:
                rel = substitute_placeholders(
                    rel, entry.placeholders_in_path, context)
            except FilesPackError as exc:
                return [], f"pack_unreadable: {entry.path} path placeholders: {exc}"

        contained, why = _contained_rel(workspace, rel)
        if not contained:
            return [], f"unsafe_target: {entry.path} -> {rel}: {why}"

        # The mode is checked HERE and not only inside the privileged helper.
        # The helper refuses setuid/setgid/sticky root-side, which is the
        # boundary that holds — but the direct write path does not go through
        # it, so without this check a pack could mint a setuid file wherever
        # the caller CAN write (bot-side, or the workspace root) and the two
        # paths would disagree about what a pack is allowed to ask for.
        try:
            mode_bits = int(entry.mode, 8)
        except ValueError:
            return [], f"unsafe_target: {entry.path}: mode {entry.mode!r} is not octal"
        if mode_bits & 0o7000:
            return [], (
                f"unsafe_target: {entry.path} declares mode {entry.mode} "
                f"(setuid/setgid/sticky). A pack is built from a bot-writable "
                f"tree; it does not get to ask for one."
            )

        planned.append(PlannedFile(
            path=entry.path,
            rel=rel,
            mode=entry.mode,
            source_sha=entry.sha256,
            predicted_sha=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        ))
    return planned, ""


def _contained_rel(workspace: Path, rel: str) -> "tuple[bool, str]":
    """Is ``workspace/rel`` a plain relative path that stays in the workspace?

    Checked on the STRING before anything touches the filesystem, and again
    root-side by ``marker_embed_helper``'s ``O_NOFOLLOW`` walk. Two layers
    because they answer different questions: this one keeps a malformed pack
    from ever being handed to the privileged helper, and the helper's is the
    boundary that holds when the caller is wrong.
    """
    value = (rel or "").strip()
    if not value:
        return False, "empty target path"
    if value.startswith("/") or os.path.isabs(value):
        return False, "absolute target path"
    parts = [p for p in value.replace("\\", "/").split("/") if p]
    if not parts:
        return False, "empty target path"
    if any(p in (".", "..") for p in parts):
        return False, "'.'/'..' component in target path"
    try:
        from .app_ownership_policy import can_app_own
    except Exception:  # noqa: BLE001 — the helper re-checks; do not fail open here
        return False, "ownership policy unavailable"
    if not can_app_own(value, name=parts[-1]):
        return False, (
            "not an application-ownable workspace path (platform telemetry, "
            "manifest/scanner state, secret, or an OpenClaw-standard file)"
        )
    return True, ""


def _sha_on_disk(path: Path) -> "tuple[str, str]":
    """``(sha256, reason)`` for a file on the target. ``("", "")`` when absent.

    A file that is present but UNREADABLE comes back with a reason and an empty
    digest, and every caller treats that as "not proven unadapted" — read-denied
    is not write-allowed.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        return "", f"cannot stat: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return "", "target is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return "", "target is not a regular file"
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest(), ""
    except OSError as exc:
        return "", f"cannot read: {exc}"


# ── Writing ──────────────────────────────────────────────────────────────────


def _writes_land_bot_owned(bot_user: str) -> bool:
    """Would a plain write here produce a file the BOT owns?

    Only when this process IS the bot. Two cases say yes:

      * the running euid is the bot's — the bot-side execution context
        (``forge_runner`` is invoked by the bot's own session);
      * the account does not exist on this host at all — a test tree or a
        developer checkout, where there is no ownership question to get wrong.

    Everything else says NO, and that deliberately includes **root**. A root
    write lands ``root:wheel`` and locks the bot out of its own app file: the
    #2781 class ``marker_embed_helper`` was built to close, and the reason its
    ``_apply`` chowns rather than trusting the caller's euid. The daemon's
    ``evolve`` euid is the other no — it holds ACL READ on a bot's workspace,
    and where it can nonetheless write (``workspace/`` root carries an
    ``add_file`` grant) the file it creates is evolve's, not the bot's.

    Getting this wrong is silent: the file appears, the digest verifies, the
    install reports success, and the bot cannot edit its own app.
    """
    import pwd

    try:
        uid = pwd.getpwnam(bot_user).pw_uid
    except (KeyError, TypeError):
        return True
    return os.geteuid() == uid


def _write_one(
    target: Path, planned: PlannedFile, *, bot_user: str, expect_sha: str,
) -> "tuple[bool, str]":
    """Materialize one planned file. ``(ok, error)``.

    The path is CHOSEN, not attempted-then-escalated. A try-direct-first shape
    reads like CLAUDE.md's documented read pattern but does not transfer: a
    read that succeeds is finished, whereas a write that succeeds can still be
    wrong — it lands the file under whichever account happened to write it. So
    ownership decides the route (:func:`_writes_land_bot_owned`), and the
    privileged helper is used whenever a plain write would land a file the bot
    does not own, including when this process is root.

    Both routes honour the same two rules: create-only unless ``expect_sha`` is
    supplied, and a replace must match the digest it was given.
    """
    from .marker_embed_helper import install_workspace_file_privileged

    if _writes_land_bot_owned(bot_user):
        ok, why = _write_one_direct(target, planned, expect_sha=expect_sha)
        if ok:
            return True, ""
        return False, why or (
            f"{planned.rel}: could not be written to the bot's workspace"
        )

    got, detail, _refused = install_workspace_file_privileged(
        target,
        content=planned.payload,
        bot_user=bot_user,
        mode=planned.mode,
        expect_sha256=expect_sha,
    )
    if got:
        return True, ""
    return False, (
        f"{planned.rel}: could not write. The admin daemon runs as the evolve "
        f"user, which holds ACL read (not write) on a bot's workspace "
        f"subdirectories, so the write goes through the privileged helper — "
        f"and that failed: {detail}"
    )


def _write_one_direct(
    target: Path, planned: PlannedFile, *, expect_sha: str,
) -> "tuple[bool, str]":
    """The in-process half of :func:`_write_one`, used where this process IS
    the bot. ``(ok, refusal)``; an empty refusal means an I/O failure with
    nothing more specific to say.

    It enforces the same two rules the privileged helper does, in the same
    order, because they are the contract and not an artefact of privilege:
    create-only publishes the name with ``os.link`` (``EEXIST`` is atomic, so
    this cannot clobber), and a replace hashes the destination first and
    refuses on a mismatch.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, ""

    current, why = _sha_on_disk(target)
    if why:
        return False, f"{planned.rel}: {why}"
    if not expect_sha:
        if current:
            return False, (
                f"{planned.rel}: already exists on the target and this install "
                f"creates files, it does not overwrite them"
            )
    else:
        if not current:
            return False, f"{planned.rel}: expected an existing file to replace"
        if current != expect_sha:
            return False, (
                f"{planned.rel}: changed since it was measured "
                f"({current[:12]}… != {expect_sha[:12]}…); refusing to "
                f"overwrite it"
            )

    tmp = target.parent / f".app-install-{os.getpid()}-{os.urandom(6).hex()}"
    try:
        tmp.write_bytes(planned.payload)
        os.chmod(tmp, int(planned.mode, 8))
        if expect_sha:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)
            except FileExistsError:
                return False, (
                    f"{planned.rel}: appeared on the target while the install "
                    f"was running; refusing to overwrite it"
                )
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        return True, ""
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False, ""


# ── The proof (1.5c's property, against the real install) ────────────────────


def _prove(planned: Sequence[PlannedFile], workspace: Path) -> dict[str, Any]:
    """Re-derive every installed file and compare against the target's disk.

    The claim being checked is 1.5c's strong one: a realized digest is
    reproducible from (pack source bytes + declared placeholders + this bot's
    context) and from NOTHING else — no clock, no ordering, no ambient
    filesystem state. ``predicted_sha`` was computed from exactly those three
    inputs before the write; this reads what actually landed.

    ``source_shas`` is carried too, because the other half of the ratified
    wording is that the SOURCE sha set is identical on every bot: it is read off
    the pack, which is bot-independent by construction, and recording it here is
    what lets two installs be compared without re-reading either bot.
    """
    files: list[dict[str, Any]] = []
    unexplained: list[str] = []
    for item in planned:
        realized, why = _sha_on_disk(workspace / item.rel)
        explained = bool(realized) and realized == item.predicted_sha
        if not explained:
            unexplained.append(item.rel)
        files.append({
            "path": item.path,
            "rel": item.rel,
            "source_sha": item.source_sha,
            "predicted_sha": item.predicted_sha,
            "realized_sha": realized,
            "explained": explained,
            "note": why or ("" if explained else
                            "the installed bytes are not reproducible from the "
                            "pack source, the declared placeholders and this "
                            "bot's context"),
        })
    return {
        "files": files,
        "source_shas": sorted({i.source_sha for i in planned}),
        "explained": not unexplained,
        "unexplained": unexplained,
    }


# ── The instance the target gets ─────────────────────────────────────────────


def _instance_manifest(
    spec: AppSpec, *, bot_id: str, planned: Sequence[PlannedFile],
    pack: Path, pack_sha: str, source_bot: str, installed_at: str,
    context: dict[str, str], written_by: str, prior: dict | None = None,
) -> dict[str, Any]:
    """The manifest written on the target bot.

    A v-next artifact (``spec_envelope``) plus the per-bot facts. It carries the
    SAME ``app_id`` as the source, which is the adoption rule: ``resolve_app_id``
    returns the source's id, so ``pod_apps`` groups both installs under one app
    instead of the pod growing a second one every time the button is pressed.

    ``realized_files[]`` carries the digest of what was installed on THIS bot —
    the post-substitution value, which is the carrier
    ``resolve_workspace_package_files`` and the Files panel measure against, and
    the ``recorded_install`` basis a later update's adaptation check uses. The
    Spec's ``package.files[].sha256`` stays the SOURCE digest, unmoved; the two
    are different digests on purpose and the artifact now says which is which by
    where it lives (AL-1.5c §9.2).

    ``prior`` is the instance being replaced by an update: its
    ``install.installed_at`` and ``install.source_bot`` are carried forward so
    "when did this bot first get this app" survives a version bump.
    """
    prior_block = (prior or {}).get(INSTALL_BLOCK)
    prior_block = prior_block if isinstance(prior_block, dict) else {}
    first_at = (_s(prior_block.get("first_installed_at"))
                or _s(prior_block.get("installed_at"))
                or installed_at)
    data = dict(spec_envelope(spec))
    data.update({
        "bot_id": bot_id,
        "definition_status": "defined",
        "installed_at": installed_at,
        "realized_files": [
            {
                "path": item.rel,
                "sha256": item.predicted_sha,
                "mode": item.mode,
                "source_sha256": item.source_sha,
                # ``role`` is the app's, not this bot's — read off the Spec so
                # the Files panel's "what it is" column survives an install.
                "role": _role_for(spec, item.path),
            }
            for item in planned
        ],
        INSTALL_BLOCK: {
            "source": "app_pack",
            "app_id": spec.app_id,
            "spec_version": spec.spec_version,
            "pack_dir": str(pack),
            "pack_sha256": pack_sha,
            "source_bot": source_bot,
            "installed_at": installed_at,
            "first_installed_at": first_at,
            "installed_by": written_by,
            # The context the realized digests were produced under. Recorded
            # because an update's ``current_pack`` fallback has to re-derive
            # under the SAME context to mean anything — a ``{installed_at}``
            # placeholder makes "re-substitute and compare" answer differently
            # on every run otherwise.
            "context": {k: v for k, v in context.items() if k != "shared_dir"},
        },
    })
    return data


def _role_for(spec: AppSpec, path: str) -> str:
    for entry in (spec.package.get("files") or []):
        if isinstance(entry, dict) and _s(entry.get("path")) == path:
            return _s(entry.get("role"))
    return ""


def _resolves_to_other_app(path: Path, app_id: str) -> str:
    """The app id a file at ``path`` already claims, when it is not ``app_id``.

    "" when the path is free, unreadable, or already this app's. Unreadable
    counts as free deliberately and narrowly: the caller is about to write
    through ``_write_manifest_bytes``, which will fail loudly if the path is
    genuinely inaccessible — whereas refusing on an unreadable file would turn
    every unparseable stray JSON in the directory into a permanent block on
    installing an app whose id happens to match its name.
    """
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    other = resolve_app_id(data)
    return other if other and other != app_id else ""


def _write_instance(path: Path, data: dict) -> str:
    """Write the instance manifest. Returns an error string, "" on success.

    Routed through ``manifest._write_manifest_bytes`` — the one writer that
    already carries the ``/tmp`` staging + ``sudo /bin/cp`` fallback for the
    manifests directory, under the grant that names exactly that path. Reusing
    it rather than re-deriving the fallback is the same choice
    ``native_write._write_instance_json`` made.

    NO CHOWN, deliberately, and this caller satisfies the condition that makes
    that safe. #3836 settled the manifest-ownership question: the file is left
    ``evolve:staff``, which is inert because no bot-side writer opens a
    manifest IN PLACE — they all rename over it, which needs write on the
    bot-owned DIRECTORY, not on the file — and the one thing that would
    invalidate it is *a bot-user caller of that function*. This caller is
    admin-side (the route runs in the daemon). The app FILES are a different
    question with a different answer: the bot does edit those in place, which
    is why they go through the helper's chown (see :func:`_write_one`).
    """
    import json

    from .manifest import _write_manifest_bytes

    try:
        _write_manifest_bytes(
            path, json.dumps(data, indent=2).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return f"instance_write_failed: {exc}"
    return ""


# ── Installability (what the button may offer) ───────────────────────────────


def installability(
    app_id: str,
    installs: "Iterable[tuple[str, str, dict, dict, AppSpec]]",
    shared_dir: Path,
) -> dict[str, Any]:
    """Can this app be installed elsewhere, and from what?

    ``installs`` is ``pod_apps``' per-app tuple list. Three states, and the
    surface must be able to tell them apart because they have different
    remediations:

      ``pack``            a verified pack exists; an install can proceed now.
      ``snapshot_needed`` no pack, but a defined install with files exists to
                          snapshot one from. The button is offered, and the
                          preview says a snapshot runs first.
      ``unavailable``     neither. The button stays off and says why — an
                          instruction-only app has nothing to materialize, and
                          saying "install" about it would be a promise the
                          install path cannot keep.

    The pack is loaded and VERIFIED here rather than probed with ``exists()``:
    a drifted pack is one the install refuses, and offering an enabled button
    over it would move the failure from before the click to after it.

    TWO STRINGS, NOT ONE. ``reason`` is the sentence the surface may show —
    plain language, no field names (design §7's Plex test: "packaged copy",
    never "files-pack"; the words on screen are never the ones in the JSON).
    ``detail`` is the engine's own error, which starts with a machine class
    (``no_pack:``, ``pack_verify_failed:``) and belongs in a log or a
    diagnostic, not in a tooltip. Returning one field and letting the caller
    decide was the first shape here, and it put ``no_pack: task-manager has no
    files-pack at /Users/Shared/…`` on an operator's screen.
    """
    dest, meta, error = _load_pack(Path(shared_dir), app_id)
    sources = sorted({
        bot_id for bot_id, _stem, raw, _display, _spec in installs
        if _is_defined(raw) and _declares_files(raw)
    })
    if meta is not None:
        return {
            "state": "pack",
            "pack": True,
            "pack_dir": str(dest),
            "pack_files": len(meta.files),
            "sources": sources,
            "reason": "",
            "detail": "",
        }
    if sources:
        return {
            "state": "snapshot_needed",
            "pack": False,
            "pack_dir": str(dest) if dest else "",
            "pack_files": 0,
            "sources": sources,
            "reason": "",
            "detail": error,
        }
    return {
        "state": "unavailable",
        "pack": False,
        "pack_dir": str(dest) if dest else "",
        "pack_files": 0,
        "sources": [],
        "reason": _unavailable_sentence(error),
        "detail": error,
    }


def _unavailable_sentence(error: str) -> str:
    """Why an install cannot be offered, in words an operator can act on.

    Two causes reach here and they have DIFFERENT remediations, so they get
    different sentences: a packaged copy that no longer matches what it was
    made from (re-package it) versus an app with no files at all (nothing to
    install, and nothing to fix).
    """
    if error.startswith(("pack_verify_failed", "pack_unreadable")):
        return (
            "this app's packaged copy no longer matches what it was made "
            "from, so installing it would not reproduce the app — package it "
            "again from a bot that has it"
        )
    return (
        "no bot on this pod has this app with any files to copy. Some apps "
        "are standing instructions rather than files, and there is nothing "
        "for an install to put on another bot"
    )


def _is_defined(raw: dict) -> bool:
    """``definition_status`` is ``defined``.

    ``pod_apps`` is imported inside the function, not at module scope: it is the
    read model the Apps surface assembles, and it reaches back here for
    ``installability`` — a top-level import in both directions is a cycle. The
    same lazy shape ``app_snapshot`` uses for the same pair.
    """
    from .pod_apps import DEFINED, definition_status_of
    return definition_status_of(raw) == DEFINED


def _declares_files(raw: dict) -> bool:
    for key in ("realized_files", "files"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return True
    return False


# ── Install ──────────────────────────────────────────────────────────────────


def install_app_to_bot(
    app_id: str,
    target_bot: str,
    *,
    shared_dir: Path | str | None = None,
    network: dict | None = None,
    source_bot: str = "",
    snapshot_if_needed: bool = True,
    dry_run: bool = True,
) -> dict:
    """Install ``app_id`` onto ``target_bot`` from its files-pack.

    Args:
      source_bot: the bot to snapshot from when no pack exists yet. Optional
        when exactly one bot has the app defined with files; REQUIRED when more
        than one does — guessing would pack one bot's copy while the operator
        meant the other, and the two are not interchangeable (that is what
        makes them separate installs).
      snapshot_if_needed: run AL-3.1's snapshot when the app has no pack.
        ``False`` refuses instead, for a caller that wants the install to touch
        only ``{shared_dir}/apps/packs``' existing content.
      dry_run: DEFAULT TRUE. Plan, substitute, predict every digest, decide
        every state — and write nothing, on the target OR in the pack. The same
        computation runs either way, so a preview cannot report something the
        apply would not do.

    Returns an envelope; on success
    ``{ok, app_id, bot_id, pack_dir, planned[], installed[], failed[], proof{},
       manifest_path, spec_version, snapshot{}, dry_run}``.
    """
    from ..config import DEFAULT_SHARED_DIR, load_network

    app_id, target_bot = _s(app_id), _s(target_bot)
    if not target_bot:
        return _err("missing_bot_id", refused=True)
    if not app_id or not is_canonical_app_id(app_id):
        return _err(
            f"invalid_app_id: {app_id!r} does not match "
            f"app_identity.APP_ID_PATTERN — a draft carries a draft_id and no "
            f"portable identity to install under (design §3)",
            refused=True,
        )

    if network is None:
        try:
            network = load_network()
        except Exception as exc:  # noqa: BLE001
            return _err(f"network_load_failed: {exc}")
    shared_dir = Path(shared_dir or network.get("sharedDir") or DEFAULT_SHARED_DIR)

    bot_user, _home, workspace, why = _resolve_bot(target_bot, network)
    if why:
        return _err(why)

    from ..secret_config_perms import exists_or_unreachable
    if not exists_or_unreachable(workspace):
        return _err(
            f"workspace_missing: {workspace} not found. Has {target_bot} been "
            f"deployed?"
        )

    # An unreadable manifests directory is NOT an empty one. Installing over a
    # copy we could not see would be the "absence of a file is not a negative
    # fact" inversion, with a whole app on the other end of it.
    existing, unreadable = _find_instance(workspace, app_id)
    if unreadable:
        return _err(
            f"workspace_unreadable: could not list {unreadable} — the evolve "
            f"user's read ACL on this bot's workspace is not in place. Nothing "
            f"was ruled out: this is NOT 'the app is not here'. Re-run the bot "
            f"deploy to re-grant, then retry.",
            app_id=app_id, bot_id=target_bot,
        )
    if existing is not None:
        return _err(
            f"already_installed: {target_bot} already has {app_id} "
            f"({existing[1].name}). Moving it to a newer version is Update, "
            f"which merges rather than replaces.",
            refused=True, app_id=app_id, bot_id=target_bot,
            manifest_path=str(existing[1]),
        )

    snapshot: dict[str, Any] = {}
    pack, meta, pack_error = _load_pack(shared_dir, app_id)
    if meta is None or pack is None:
        if not snapshot_if_needed:
            return _err(pack_error, refused=True, app_id=app_id, bot_id=target_bot)
        snapshot, why = _snapshot_for(
            app_id, source_bot, shared_dir=shared_dir, network=network,
            dry_run=dry_run,
        )
        if why:
            # A snapshot that DECLINED (a draft, an instruction-only app) is a
            # refusal; one that BROKE is a failure. The distinction is the
            # snapshot's own, carried through rather than recomputed here — a
            # healthy decline reported as an outage is the confusion AL-1.5b's
            # review caught, and re-deriving it would let the two answers drift.
            refused = (not why.startswith("snapshot_failed")
                       or bool(snapshot.get("refused")))
            return _err(
                why, refused=refused, app_id=app_id, bot_id=target_bot,
                snapshot=snapshot,
            )
        if dry_run:
            # There is no pack on disk yet, so there is nothing to plan
            # against and nothing to claim about. The snapshot's own dry run
            # says whether one COULD be built and out of what; asserting a
            # file list over a pack that does not exist would be the vacuous
            # pass this arc has been caught by twice.
            return _ok(
                app_id=app_id, bot_id=target_bot, pack_dir=str(pack or ""),
                planned=[], installed=[], failed=[],
                proof={"files": [], "source_shas": [], "explained": False,
                       "unexplained": []},
                manifest_path="", spec_version=0, snapshot=snapshot,
                dry_run=True, needs_snapshot=True,
            )
        pack, meta, pack_error = _load_pack(shared_dir, app_id)
        if meta is None or pack is None:
            return _err(pack_error or "no_pack: the snapshot reported success "
                                      "but left no pack to install from",
                        app_id=app_id, bot_id=target_bot, snapshot=snapshot)

    spec = load_spec(shared_dir, app_id)
    if spec is None:
        return _err(
            f"no_spec: {app_id} has no Spec at "
            f"{Path(shared_dir) / 'apps' / 'specs' / (app_id + '.json')}. The "
            f"install writes the target's instance FROM the Spec — installing "
            f"files with no description of what they are would give the target "
            f"an app the pod cannot describe.",
            refused=True, app_id=app_id, bot_id=target_bot,
        )

    pkg_id, why = _pkg_id_for(meta, _source_pkg_id(app_id, network), app_id)
    if why:
        return _err(why, refused=True, app_id=app_id, bot_id=target_bot)

    installed_at = _now()
    context, why = _context(
        bot_id=target_bot, bot_user=bot_user, workspace=workspace,
        app_id=app_id, pkg_id=pkg_id, installed_at=installed_at,
        shared_dir=shared_dir,
    )
    if why:
        return _err(why, app_id=app_id, bot_id=target_bot)

    planned, why = _plan_entries(meta, pack, workspace, context)
    if why:
        return _err(why, app_id=app_id, bot_id=target_bot, snapshot=snapshot)
    if not planned:
        return _err(
            f"empty_pack: {app_id}'s pack declares no files",
            refused=True, app_id=app_id, bot_id=target_bot,
        )

    # Collisions, decided BEFORE any byte is written. A file already sitting at
    # a pack path was not put there by this app on this bot (there is no
    # instance), so overwriting it is destroying something whose provenance the
    # pod does not know.
    collisions: list[dict] = []
    for item in planned:
        current, note = _sha_on_disk(workspace / item.rel)
        if current or note:
            item.state = STATE_COLLISION
            item.current_sha = current
            item.note = note or "a file is already at this path"
            collisions.append(item.to_dict())
    if collisions:
        return _err(
            "target_collision: " + ", ".join(c["rel"] for c in collisions[:5])
            + (f" (+{len(collisions) - 5} more)" if len(collisions) > 5 else "")
            + f" already exist in {target_bot}'s workspace, and this bot has no "
              f"instance of {app_id} — so the pod cannot say what put them "
              f"there. Nothing was written. Remove or rename them, or install "
              f"under a bot that does not have them.",
            refused=True, app_id=app_id, bot_id=target_bot,
            planned=[p.to_dict() for p in planned], collisions=collisions,
        )

    if dry_run:
        return _ok(
            app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
            planned=[p.to_dict() for p in planned],
            installed=[], failed=[],
            proof={"files": [], "source_shas": sorted({p.source_sha for p in planned}),
                   "explained": False, "unexplained": []},
            manifest_path="", spec_version=spec.spec_version,
            snapshot=snapshot, dry_run=True, needs_snapshot=False,
        )

    installed: list[str] = []
    failed: list[dict] = []
    for item in planned:
        ok, why = _write_one(
            workspace / item.rel, item, bot_user=bot_user, expect_sha="")
        if ok:
            installed.append(item.rel)
        else:
            failed.append({"rel": item.rel, "error": why})
    logger.info(
        "app_install: %s -> %s: %d/%d file(s) written",
        app_id, target_bot, len(installed), len(planned),
    )

    landed = set(installed)
    proof = _prove([p for p in planned if p.rel in landed], workspace)

    if failed:
        # Loud, per-file, and NO instance manifest: a manifest is the pod's
        # statement that the app is here, and writing one over a partial
        # materialization makes every later reader wrong about an app it can
        # neither run nor repair.
        return _err(
            f"partial_install: {len(installed)} of {len(planned)} file(s) "
            f"landed on {target_bot} and {len(failed)} did not. No instance "
            f"was written, so the pod does not claim this app is installed "
            f"here. The files that DID land are listed and are still on disk — "
            f"remove them or retry once the errors below are fixed.",
            app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
            planned=[p.to_dict() for p in planned],
            installed=installed, failed=failed, proof=proof,
            snapshot=snapshot, dry_run=False,
        )

    if not proof["explained"]:
        return _err(
            "install_proof_failed: " + ", ".join(proof["unexplained"][:5])
            + " are on disk but their contents are not reproducible from the "
              "pack source, the declared placeholders and this bot's context. "
              "The install is not deterministic for those files, so no "
              "instance was written.",
            app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
            planned=[p.to_dict() for p in planned],
            installed=installed, failed=[], proof=proof,
            snapshot=snapshot, dry_run=False,
        )

    # The instance is filed under the app's id, and that filename may already
    # belong to something else. It is NOT this app — the manifests directory
    # was listed at the top of this function and nothing in it resolved to
    # ``app_id`` — so writing here would replace another app's record with
    # this one's. Checked immediately before the write rather than at the top,
    # because that is where a same-instant race would land.
    manifest_path = workspace / "manifests" / f"{app_id}.json"
    squatter = _resolves_to_other_app(manifest_path, app_id)
    if squatter:
        return _err(
            f"manifest_stem_taken: {manifest_path.name} on {target_bot} "
            f"already holds {squatter!r}. The app's files are installed and "
            f"listed below; the instance was not written, because writing it "
            f"would have replaced another app's record.",
            app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
            planned=[p.to_dict() for p in planned],
            installed=installed, failed=[], proof=proof, snapshot=snapshot,
            dry_run=False,
        )

    data = _instance_manifest(
        spec, bot_id=target_bot, planned=planned, pack=pack,
        pack_sha=_pack_sha(pack), source_bot=_s(snapshot.get("bot_id")) or source_bot,
        installed_at=installed_at, context=context, written_by=INSTALL_BY,
    )
    why = _write_instance(manifest_path, data)
    if why:
        return _err(
            why, app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
            installed=installed, failed=[], proof=proof,
            planned=[p.to_dict() for p in planned], snapshot=snapshot,
        )

    return _ok(
        app_id=app_id, bot_id=target_bot, pack_dir=str(pack),
        planned=[p.to_dict() for p in planned],
        installed=installed, failed=[], proof=proof,
        manifest_path=str(manifest_path), spec_version=spec.spec_version,
        snapshot=snapshot, dry_run=False, needs_snapshot=False,
    )


def _pack_sha(pack: Path) -> str:
    try:
        return compute_files_pack_sha256(pack)
    except FilesPackError:
        return ""


def _source_pkg_id(app_id: str, network: dict) -> str:
    """The gallery package key of a bot's copy of this app, or "".

    identity: see resolve_app_id — ``pkg_id`` is read here as the files-pack
    SUBSTITUTION-VOCABULARY name (``files_pack.KNOWN_PLACEHOLDERS``), the
    package attribution namespace, and NEVER as the app's identity. The app is
    already known: it is the ``app_id`` argument, resolved by
    ``resolve_app_id`` at the call site. Routing this read through the resolver
    would return the app's own id for an artifact that has no package behind
    it, which is precisely the substitution ``_pkg_id_for`` refuses to make
    silently. Read off any bot that has the app, because the key belongs to the
    APP and is the same wherever its copy came from.
    """
    for bot_id in _pod_bots(network):
        _user, _home, workspace, why = _resolve_bot(bot_id, network)
        if why:
            continue
        found, _unreadable = _find_instance(workspace, app_id)
        if found is not None:
            value = _s(found[0].get("pkg_id"))
            if value:
                return value
    return ""


def _pod_bots(network: dict) -> list[str]:
    bots = network.get("bots")
    if isinstance(bots, dict) and bots:
        return list(bots)
    members = network.get("members")
    return list(members) if isinstance(members, list) else []


def _snapshot_for(
    app_id: str, source_bot: str, *, shared_dir: Path, network: dict, dry_run: bool,
) -> "tuple[dict, str]":
    """Snapshot ``app_id`` so an install has a pack. ``(envelope, error)``.

    The source bot is not guessed. With no explicit ``source_bot`` and more
    than one candidate, this refuses and names them: two bots' copies of an app
    are two different sets of bytes, and picking one silently would install
    whichever the directory happened to yield first.
    """
    from .app_snapshot import snapshot_app

    candidates = []
    for bot_id in _pod_bots(network):
        _user, _home, workspace, why = _resolve_bot(bot_id, network)
        if why:
            continue
        found, _unreadable = _find_instance(workspace, app_id)
        if found is None:
            continue
        raw = found[0]
        if _is_defined(raw) and _declares_files(raw):
            candidates.append(bot_id)

    if source_bot:
        if source_bot not in candidates:
            return {}, (
                f"source_not_eligible: {source_bot} does not have {app_id} as a "
                f"defined app with files, so there is nothing on it to pack. "
                f"Candidates: {', '.join(candidates) or 'none'}"
            )
        chosen = source_bot
    elif not candidates:
        return {}, (
            f"no_source: no bot on this pod has {app_id} defined with files to "
            f"snapshot a pack from"
        )
    elif len(candidates) > 1:
        return {"candidates": candidates}, (
            f"ambiguous_source: {app_id} is defined on {', '.join(candidates)}. "
            f"Two bots' copies are two different sets of bytes — name the one "
            f"to pack from rather than having one picked."
        )
    else:
        chosen = candidates[0]

    result = snapshot_app(
        chosen, app_id, shared_dir=shared_dir, network=network, dry_run=dry_run,
    )
    if not result.get("ok"):
        return result, f"snapshot_failed: {result.get('error') or 'unknown'}"
    return result, ""


# ── Update (a merge, never a silent replace — D-L3) ──────────────────────────


def update_app_on_bot(
    app_id: str,
    bot_id: str,
    *,
    shared_dir: Path | str | None = None,
    network: dict | None = None,
    confirm_overwrite: bool = False,
    dry_run: bool = True,
) -> dict:
    """Move ``bot_id``'s copy of ``app_id`` to the Spec's current version.

    An update is a MERGE. Every file the pack carries is classified against
    what is on the bot (see the module docstring for the three bases), and:

      * ``create``     the new version adds a file this bot has not got.
      * ``unadapted``  proven untouched since it was installed — replaced.
      * ``adapted``    differs, or could not be proven either way. NOT touched
        without ``confirm_overwrite``, and the report says exactly what would
        be lost. Never flattened silently.

    A file the bot has that the new version DROPS is reported
    (``removed_upstream``) and left alone: deleting a bot's file is a different
    act from updating an app, and an update that quietly removed files would be
    indistinguishable from an uninstall the operator did not ask for.
    """
    from ..config import DEFAULT_SHARED_DIR, load_network

    app_id, bot_id = _s(app_id), _s(bot_id)
    if not bot_id:
        return _err("missing_bot_id", refused=True)
    if not app_id or not is_canonical_app_id(app_id):
        return _err(f"invalid_app_id: {app_id!r}", refused=True)

    if network is None:
        try:
            network = load_network()
        except Exception as exc:  # noqa: BLE001
            return _err(f"network_load_failed: {exc}")
    shared_dir = Path(shared_dir or network.get("sharedDir") or DEFAULT_SHARED_DIR)

    bot_user, _home, workspace, why = _resolve_bot(bot_id, network)
    if why:
        return _err(why)

    found, unreadable = _find_instance(workspace, app_id)
    if unreadable:
        return _err(
            f"workspace_unreadable: could not list {unreadable} — nothing was "
            f"ruled out. Re-run the bot deploy to re-grant the read ACL.",
            app_id=app_id, bot_id=bot_id,
        )
    if found is None:
        return _err(
            f"not_installed: {bot_id} has no instance of {app_id}. Putting it "
            f"there for the first time is Install, which creates files rather "
            f"than merging into them.",
            refused=True, app_id=app_id, bot_id=bot_id,
        )
    instance, instance_path = found

    pack, meta, pack_error = _load_pack(shared_dir, app_id)
    if meta is None or pack is None:
        return _err(pack_error, refused=True, app_id=app_id, bot_id=bot_id)

    spec = load_spec(shared_dir, app_id)
    if spec is None:
        return _err(
            f"no_spec: {app_id} has no Spec to update to", refused=True,
            app_id=app_id, bot_id=bot_id,
        )
    from_version = spec_from_manifest(instance).spec_version
    to_version = spec.spec_version

    recorded = _recorded_digests(instance)
    installed_context = _recorded_context(instance)
    # identity: see resolve_app_id — both reads below are the files-pack
    # ``{pkg_id}`` PLACEHOLDER value (the package attribution namespace), not
    # this app's identity: the recorded install context first, because it is
    # the value the bot's files were actually substituted with, then the
    # artifact's own key. The app's identity is ``app_id``, which came from
    # ``resolve_app_id`` and is passed separately — the two are distinct names
    # in the same format-versioned vocabulary, which is why they sit side by
    # side here rather than one standing in for the other.
    pkg_id, why = _pkg_id_for(
        meta, _s(installed_context.get("pkg_id")) or _s(instance.get("pkg_id")), app_id)
    if why:
        return _err(why, refused=True, app_id=app_id, bot_id=bot_id)

    installed_at = _now()
    context, why = _context(
        bot_id=bot_id, bot_user=bot_user, workspace=workspace, app_id=app_id,
        pkg_id=pkg_id, installed_at=installed_at, shared_dir=shared_dir,
    )
    if why:
        return _err(why, app_id=app_id, bot_id=bot_id)

    planned, why = _plan_entries(meta, pack, workspace, context)
    if why:
        return _err(why, app_id=app_id, bot_id=bot_id)

    # The as-installed context differs from the new one wherever a
    # context value moved (``{installed_at}`` always does), and the
    # ``current_pack`` basis has to re-derive under the OLD one or it would
    # report every such file as adapted on every update.
    as_installed = dict(context)
    as_installed.update({k: v for k, v in installed_context.items() if v})
    as_installed["shared_dir"] = context["shared_dir"]
    prior_by_rel = _predicted_under(meta, pack, as_installed)

    conflicts: list[dict] = []
    for item in planned:
        current, note = _sha_on_disk(workspace / item.rel)
        item.current_sha = current
        if not current and not note:
            item.state = STATE_CREATE
            item.basis = BASIS_NONE
            item.note = "this version adds a file the bot has not got"
            continue
        if note:
            item.state = STATE_ADAPTED
            item.basis = BASIS_NONE
            item.note = note
            conflicts.append(item.to_dict())
            continue
        want = recorded.get(item.rel, "")
        if want:
            item.basis = BASIS_RECORDED
            item.state = STATE_UNADAPTED if current == want else STATE_ADAPTED
            item.note = ("" if current == want else
                         "changed on this bot since it was installed")
        elif item.rel in prior_by_rel:
            item.basis = BASIS_PACK
            item.state = (STATE_UNADAPTED if current == prior_by_rel[item.rel]
                          else STATE_ADAPTED)
            item.note = ("" if current == prior_by_rel[item.rel] else
                         "differs from what this app's pack would produce here, "
                         "and this bot's copy records no installed digest, so "
                         "whether the difference is a local change or a change "
                         "in the new version cannot be told apart")
        else:
            item.basis = BASIS_NONE
            item.state = STATE_ADAPTED
            item.note = ("nothing records what this file should be, so it is "
                         "treated as locally adapted")
        if item.state == STATE_ADAPTED:
            conflicts.append(item.to_dict())

    removed_upstream = sorted(
        set(recorded) - {p.rel for p in planned}
    )

    payload = {
        "app_id": app_id, "bot_id": bot_id, "pack_dir": str(pack),
        "from_version": from_version, "to_version": to_version,
        "adapted": bool(conflicts),
        "plan": [p.to_dict() for p in planned],
        "conflicts": conflicts,
        "removed_upstream": removed_upstream,
        "bases": sorted({p.basis for p in planned}),
    }

    if dry_run:
        return _ok(applied=[], failed=[], manifest_path="", dry_run=True, **payload)

    if conflicts and not confirm_overwrite:
        return _err(
            "would_overwrite_local_changes: " + ", ".join(
                c["rel"] for c in conflicts[:5])
            + (f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else "")
            + " on this bot differ from what was installed. An update merges; "
              "it does not flatten. Nothing was written. Re-run with an explicit "
              "confirmation to replace them, and note that the replacement is "
              "checked against the digest above — if the file changes again in "
              "between, the write refuses rather than using a stale answer.",
            refused=True, applied=[], failed=[], manifest_path="",
            dry_run=False, **payload,
        )

    applied: list[str] = []
    failed: list[dict] = []
    for item in planned:
        if item.current_sha and item.current_sha == item.predicted_sha:
            continue                       # already exactly this version
        if item.state != STATE_CREATE and not item.current_sha:
            # Something is at this path that could not be hashed (unreadable,
            # a symlink, a directory). A replace has to prove what it is
            # replacing, and there is no digest to prove it with — so this one
            # is reported rather than written over blind.
            failed.append({"rel": item.rel, "error": (
                f"{item.rel}: {item.note or 'could not be read'} — a replace "
                f"has to match the digest of what it replaces, and this file "
                f"could not be measured")})
            continue
        expect = item.current_sha if item.state != STATE_CREATE else ""
        ok, err = _write_one(
            workspace / item.rel, item, bot_user=bot_user, expect_sha=expect)
        if ok:
            applied.append(item.rel)
        else:
            failed.append({"rel": item.rel, "error": err})

    proof = _prove(planned, workspace)
    if failed:
        return _err(
            f"partial_update: {len(applied)} file(s) moved and {len(failed)} "
            f"did not. The instance was NOT re-pinned, so the bot still reports "
            f"the version it can actually run.",
            applied=applied, failed=failed, proof=proof, manifest_path="",
            dry_run=False, **payload,
        )
    if not proof["explained"]:
        return _err(
            "update_proof_failed: " + ", ".join(proof["unexplained"][:5])
            + " are not reproducible from the pack source, the declared "
              "placeholders and this bot's context after the update. The "
              "instance was not re-pinned.",
            applied=applied, failed=[], proof=proof, manifest_path="",
            dry_run=False, **payload,
        )

    data = _instance_manifest(
        spec, bot_id=bot_id, planned=planned, pack=pack, pack_sha=_pack_sha(pack),
        source_bot=_s(_recorded_install(instance).get("source_bot")),
        installed_at=installed_at, context=context, written_by=UPDATE_BY,
        prior=instance,
    )
    why = _write_instance(instance_path, data)
    if why:
        return _err(why, applied=applied, failed=[], proof=proof,
                    manifest_path="", dry_run=False, **payload)

    return _ok(applied=applied, failed=[], proof=proof,
               manifest_path=str(instance_path), dry_run=False, **payload)


def _recorded_install(instance: dict) -> dict:
    """The ``install{}`` block this installer wrote, or ``{}``."""
    block = instance.get(INSTALL_BLOCK)
    return block if isinstance(block, dict) else {}


def _recorded_digests(instance: dict) -> dict[str, str]:
    """``{rel: sha256}`` this installer wrote for the bot's copy, if it did.

    Read off ``realized_files[]`` — the post-substitution carrier. Absent for an
    instance written by anything else, which is exactly when the update falls
    back to the weaker basis and says so.
    """
    out: dict[str, str] = {}
    for entry in (instance.get("realized_files") or []):
        if not isinstance(entry, dict):
            continue
        rel, sha = _s(entry.get("path")), _s(entry.get("sha256")).lower()
        if rel and sha:
            out[rel] = sha
    return out


def _recorded_context(instance: dict) -> dict[str, str]:
    """The substitution context recorded at install time, or ``{}``."""
    context = _recorded_install(instance).get("context")
    if not isinstance(context, dict):
        return {}
    return {str(k): _s(v) for k, v in context.items() if _s(v)}


def _predicted_under(
    meta: FilesPackMetadata, pack: Path, context: dict[str, str],
) -> dict[str, str]:
    """``{rel: sha256}`` each pack entry would realize under ``context``.

    Best-effort by design: an entry this cannot derive is simply absent from
    the map, and the caller then treats the file as adapted. Failing open here
    would mean guessing "unadapted", which is the one answer that loses data.
    """
    out: dict[str, str] = {}
    for entry in meta.files:
        src = pack / entry.path
        try:
            if entry.placeholders or entry.placeholders_in_path:
                payload = substitute_placeholders(
                    src.read_text(encoding="utf-8"), entry.placeholders, context,
                ).encode("utf-8")
            else:
                payload = src.read_bytes()
            rel = entry.path
            if entry.placeholders_in_path:
                rel = substitute_placeholders(
                    rel, entry.placeholders_in_path, context)
        except (OSError, UnicodeDecodeError, FilesPackError):
            continue
        out[rel] = hashlib.sha256(payload).hexdigest()
    return out
