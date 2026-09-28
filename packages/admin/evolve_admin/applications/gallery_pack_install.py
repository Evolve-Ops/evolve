"""gallery_pack_install — a gallery files-pack app onto a bot through the AL-3.2 spine.

Brief: ``internal/dispatch/done/artifact-by-reference-pattern.md`` (part 2:
"installable via AL-3.2 on the PoC bot"). Spine: ``app_install.install_app_to_bot``.

THE GAP THIS CLOSES. AL-3.2's deterministic install reads ONE source — the
pack at ``{shared_dir}/apps/packs/<app_id>/`` beside the Spec at
``{shared_dir}/apps/specs/<app_id>.json`` — and AL-3.1's snapshot is the only
thing that has ever written there, from an app already DEFINED on some bot.
A first-party gallery package that ships its files (``gallery/<slug>/files/``)
has a pack in exactly that format and no bot to snapshot from: on a pod that
has never installed it, ``installability`` says ``unavailable`` and the only
route in is the LLM forge (Path A), which is precisely the ~$30 install the
files-pack hybrid exists to avoid.

So this module SEEDS the pod-side pack + Spec from the gallery, then calls the
spine unchanged:

  1. ``seed_pack_from_gallery`` — copy ``gallery/<slug>/files/`` verbatim to
     ``{shared_dir}/apps/packs/<app_id>/`` (per-file bytes and
     ``manifest.json`` both, so the pack's top-level digest is the gallery's
     own), verify it with ``verify_files_pack_integrity`` before reporting
     success, and write the Spec with ``spec_from_artifact`` — the ONE Spec
     derivation for anything on a real pod, which resolves ``package.files[]``
     shas off the same gallery pack (``app_spec_store.resolve_package_files``).
  2. ``install_app_to_bot(app_id, bot, snapshot_if_needed=False)`` — the
     ratified install: create-only, per-file failures loud, no instance over a
     partial materialization, the 1.5c proof against what landed.
  3. The package's ``scheduled_actions[]`` with an ``oc_session_instruction``
     / ``oc_heartbeat_instruction`` mechanism become evolve-managed sections
     through ``install_heartbeat_instruction`` — the SAME writer forge Phase
     4.5 uses, with the same marker (``pkg=<pkg_id>``), so uninstall's
     ``derive_scheduled_teardown`` recognises them. AL-3.2's spine installs
     FILES only (a Spec carries ``runs``, not sections), which is why this is
     a separate step and why it happens after the files land.
  4. ``regenerate_installed_apps_md`` — best-effort, so the app appears in the
     Tier-1 AGENTS.md menu and ``expand_app`` can attribute turns to it.

WHAT IS NOT DONE HERE. No forge job, no LLM, no ``pkg_id`` minted: the
installed instance adopts the package's ``app_id`` (the manifest's canonical
``app_id`` field; a gallery package whose identity does not resolve to a
conforming slug is refused, because AL-3.2 refuses it too — a draft-shaped id
has no portable identity to install under). ``dry_run`` is the DEFAULT and
writes nothing anywhere, including the pack: a preview cannot report something
the apply would not do, and seeding the pack is a pod-state write.

WHO RUNS THIS, AND WHAT IT OWNS AFTERWARDS. The documented invocation is
``sudo evolve-admin …`` (the CLI's other pod-state writers are root-invoked
the same way), so this process is usually root, while the ONLY reader a pack
needs is the admin daemon, which runs as ``evolve`` — ``app_snapshot``'s
0700/0600 modes assume ``evolve`` is also the writer. A root-created
``apps/packs/<app_id>/`` at 0700 is a directory the daemon can never read
again: ``installability`` would report ``unavailable`` and the Install-to…
button would stay off — the exact gap this module exists to close, re-opened
by the command that closed it. So every directory and file this module
creates under ``{shared_dir}/apps`` is chowned to ``evolve:<admin_group>``
when the process is root (``_restore_evolve_ownership``); as ``evolve`` that
is a no-op, and on a box with no ``evolve`` account (a dev checkout, a test)
there is no ownership question to get wrong.

The rest of the repo stays untouched: no second install path, no second Spec
derivation, no second section writer.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from evolve_config import get_profile

from .app_identity import is_canonical_app_id, resolve_app_id
from .app_install import install_app_to_bot
from .app_snapshot import PACK_DIR_MODE, PACK_FILE_MODE, pack_dir
from .app_spec_store import spec_from_artifact, spec_path, write_spec
from .files_pack import (
    FilesPackError,
    compute_files_pack_sha256,
    load_files_pack_metadata,
    verify_files_pack_integrity,
)
from .gallery import find_files_pack_dir, load_gallery_package

logger = logging.getLogger(__name__)

__all__ = [
    "SECTION_MECHANISMS",
    "install_gallery_pack",
    "seed_pack_from_gallery",
    "section_actions",
]

#: The mechanisms that materialize as an evolve-managed markdown section.
SECTION_MECHANISMS = ("oc_session_instruction", "oc_heartbeat_instruction")


def _err(error: str, *, refused: bool = False, **extra: Any) -> dict:
    out = {"ok": False, "refused": refused, "error": error}
    out.update(extra)
    return out


def _ok(**extra: Any) -> dict:
    out = {"ok": True, "refused": False, "error": ""}
    out.update(extra)
    return out


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def section_actions(pkg: dict) -> list[dict]:
    """The ``scheduled_actions[]`` entries that install a markdown section.

    Returns ``{action_id, mechanism, file, section_anchor, body}`` per entry
    whose three install fields are non-empty strings — the same gate the
    gallery conformance suite applies, applied again here because an imported
    package never passed that suite.
    """
    out: list[dict] = []
    for action in (pkg.get("scheduled_actions") or []):
        if not isinstance(action, dict):
            continue
        mechanism = _s(action.get("mechanism"))
        if mechanism not in SECTION_MECHANISMS:
            continue
        raw_install = action.get("install")
        install: dict = raw_install if isinstance(raw_install, dict) else {}
        file, anchor, body = (_s(install.get("file")), _s(install.get("section_anchor")),
                              _s(install.get("body")))
        if not (file and anchor and body):
            continue
        out.append({
            "action_id": _s(action.get("id")) or "?",
            "mechanism": mechanism,
            "file": file,
            "section_anchor": anchor,
            "body": body,
        })
    return out


def _pin_mode(path: Path, mode: int) -> str:
    """Compare-then-chmod. Returns "" or the error — the caller REFUSES on
    an error rather than reporting an install over a pack it could not
    pin (the ``chmod -R a+rX {shared_dir}`` re-exposer class)."""
    try:
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except OSError as exc:
        return f"could not pin mode {mode:o} on {path}: {exc}"
    return ""


def _contained(dest: Path, rel: str) -> "Path | None":
    """``dest/rel`` when ``rel`` is a plain relative path that stays inside
    ``dest`` after resolution; ``None`` for absolute paths, ``..`` segments,
    or a symlinked ancestor that escapes. ``files_pack._coerce_files`` checks
    mode and digests, not paths — and this is the one pack-consuming writer
    outside ``app_install`` (which has its own ``_contained_rel``)."""
    if not rel or rel.startswith(("/", "\\")) or os.path.isabs(rel):
        return None
    if any(part in ("..", "") for part in Path(rel).parts):
        return None
    target = dest / rel
    try:
        root = dest.resolve()
        real = target.parent.resolve() / target.name
    except OSError:
        return None
    if root != real and root not in real.parents:
        return None
    return target


def _partial_note(created: list[Path]) -> str:
    """Appended to a refusal that happens after something was written, so
    the operator knows a partial pack is on disk (and, under sudo, owned by
    root) rather than discovering it on the next run."""
    if not created:
        return ""
    return (f" — {len(created)} path(s) were already created under the pack and "
            f"are left in place (partial, possibly root-owned); remove "
            f"{created[0]} before retrying")


def _restore_evolve_ownership(paths: list[Path]) -> str:
    """chown ``paths`` to ``evolve:<admin_group>`` when this process is root.

    "" on success or when there is nothing to do (not root, or no ``evolve``
    account on this host); the error otherwise. See the module docstring.
    """
    if os.geteuid() != 0:
        return ""
    import grp
    import pwd

    try:
        uid = pwd.getpwnam("evolve").pw_uid
    except KeyError:
        return ""
    try:
        gid = grp.getgrnam(get_profile().admin_group).gr_gid
    except KeyError:
        gid = -1
    for path in paths:
        try:
            os.lchown(path, uid, gid)
        except OSError as exc:
            return f"could not chown {path} to evolve: {exc}"
    return ""


def seed_pack_from_gallery(
    pkg_id: str,
    *,
    shared_dir: Path | str,
    dry_run: bool = True,
) -> dict:
    """``gallery/<slug>/files/`` → ``{shared_dir}/apps/packs/<app_id>/`` + the Spec.

    Returns ``{ok, app_id, pkg_id, pack_dir, spec_path, files[], pack_sha256,
    dry_run, seeded}``. ``seeded`` is False when a verified pack with the same
    top-level digest is already there (the copy is idempotent and says so
    instead of re-writing).
    """
    pkg_id = _s(pkg_id)
    shared = Path(shared_dir)
    if not pkg_id:
        return _err("missing_pkg_id", refused=True)
    pkg = load_gallery_package(pkg_id, shared)
    if not isinstance(pkg, dict):
        return _err(f"unknown_package: {pkg_id} is not in the gallery", refused=True, pkg_id=pkg_id)

    app_id = resolve_app_id(pkg)
    if not app_id or not is_canonical_app_id(app_id):
        return _err(
            f"no_canonical_app_id: gallery package {pkg_id} resolves to {app_id!r}, "
            f"which is not a conforming app id. AL-3.2 installs under the app's "
            f"portable identity; give the package a canonical `app_id` field.",
            refused=True, pkg_id=pkg_id,
        )

    src = find_files_pack_dir(pkg_id)
    if src is None:
        return _err(
            f"no_files_pack: gallery package {pkg_id} ships no files/ directory — "
            f"there is nothing to install deterministically; it is a build_spec "
            f"(forge) package.",
            refused=True, pkg_id=pkg_id, app_id=app_id,
        )
    try:
        meta = load_files_pack_metadata(src)
    except FilesPackError as exc:
        return _err(f"pack_unreadable: {exc}", pkg_id=pkg_id, app_id=app_id)
    if meta is None:
        return _err(f"no_pack_manifest: {src} has no manifest.json", pkg_id=pkg_id, app_id=app_id)
    findings = verify_files_pack_integrity(src, meta)
    if findings:
        return _err(
            "gallery_pack_verify_failed: " + "; ".join(
                f"{f.path}: {f.kind} ({f.detail})" for f in findings[:5]),
            pkg_id=pkg_id, app_id=app_id,
        )
    if not meta.files:
        return _err(f"empty_pack: {pkg_id} declares no files", refused=True,
                    pkg_id=pkg_id, app_id=app_id)

    try:
        dest = pack_dir(shared, app_id)
    except ValueError as exc:
        return _err(f"invalid_app_id: {exc}", refused=True, pkg_id=pkg_id, app_id=app_id)
    src_sha = compute_files_pack_sha256(src)
    files = [{"path": f.path, "mode": f.mode, "sha256": f.sha256, "size_bytes": f.size_bytes}
             for f in meta.files]
    spec_file = spec_path(shared, app_id)

    already = False
    if (dest / "manifest.json").is_file():
        try:
            if compute_files_pack_sha256(dest) == src_sha and not verify_files_pack_integrity(
                    dest, load_files_pack_metadata(dest)):  # type: ignore[arg-type]
                already = True
        except FilesPackError:
            already = False

    if dry_run:
        return _ok(app_id=app_id, pkg_id=pkg_id, pack_dir=str(dest), spec_path=str(spec_file),
                   files=files, pack_sha256=src_sha, dry_run=True,
                   seeded=False, already_seeded=already)

    # Containment and symlink checks BEFORE any write: a pack entry is
    # manifest-supplied, and ``copyfile`` follows symlinks.
    for entry in meta.files:
        if _contained(dest, entry.path) is None:
            return _err(
                f"pack_path_escapes: {entry.path!r} is not a plain relative path "
                f"inside the pack — refusing to seed",
                refused=True, pkg_id=pkg_id, app_id=app_id,
            )
        source = src / entry.path
        if source.is_symlink() or not source.is_file():
            return _err(
                f"pack_source_not_a_file: {entry.path!r} is a symlink or not a regular "
                f"file in the gallery pack — refusing to seed",
                refused=True, pkg_id=pkg_id, app_id=app_id,
            )

    created: list[Path] = []

    def _mkdirs(path: Path) -> None:
        """``mkdir -p`` that records EVERY level it creates, so the chown
        below covers a grandparent as well as the leaf."""
        missing: list[Path] = []
        cur = path
        while not cur.exists():
            missing.append(cur)
            cur = cur.parent
        for level in reversed(missing):
            level.mkdir(exist_ok=False)
            created.append(level)

    if not already:
        try:
            for level in (shared / "apps", shared / "apps" / "packs", dest):
                _mkdirs(level)
            for level, mode in ((shared / "apps" / "packs", PACK_DIR_MODE), (dest, PACK_DIR_MODE)):
                why = _pin_mode(level, mode)
                if why:
                    return _err(f"pack_mode_failed: {why}{_partial_note(created)}",
                                pkg_id=pkg_id, app_id=app_id, pack_dir=str(dest))
            for entry in meta.files:
                target = _contained(dest, entry.path)
                assert target is not None  # checked before any write, above
                _mkdirs(target.parent)
                why = _pin_mode(target.parent, PACK_DIR_MODE)
                if why:
                    return _err(f"pack_mode_failed: {why}{_partial_note(created)}",
                                pkg_id=pkg_id, app_id=app_id, pack_dir=str(dest))
                tmp = target.with_name(f".{target.name}.seeding")
                shutil.copyfile(src / entry.path, tmp, follow_symlinks=False)
                os.chmod(tmp, PACK_FILE_MODE)
                os.replace(tmp, target)
                created.append(target)
            tmp = dest / ".manifest.json.seeding"
            shutil.copyfile(src / "manifest.json", tmp, follow_symlinks=False)
            os.chmod(tmp, PACK_FILE_MODE)
            os.replace(tmp, dest / "manifest.json")
            created.append(dest / "manifest.json")
        except OSError as exc:
            return _err(f"pack_write_failed: {dest}: {exc}{_partial_note(created)}",
                        pkg_id=pkg_id, app_id=app_id, pack_dir=str(dest))
        try:
            landed = load_files_pack_metadata(dest)
            bad = verify_files_pack_integrity(dest, landed) if landed else []
        except FilesPackError as exc:
            return _err(f"seeded_pack_unreadable: {exc}", pkg_id=pkg_id, app_id=app_id,
                        pack_dir=str(dest))
        if landed is None or bad or compute_files_pack_sha256(dest) != src_sha:
            return _err(
                "seeded_pack_verify_failed: the copy under "
                f"{dest} does not match the gallery pack — nothing was installed",
                pkg_id=pkg_id, app_id=app_id, pack_dir=str(dest),
            )

    # The Spec — derived the one way anything on a real pod derives it, so its
    # package.files[].sha256 are the pack's source digests (AL-1.5c §9.2).
    specs_existed = (shared / "apps" / "specs").exists()
    try:
        spec = spec_from_artifact(pkg)
        written = write_spec(spec, shared)
    except Exception as exc:  # noqa: BLE001 — the spec writer's own refusals are the message
        return _err(f"spec_write_failed: {exc}", pkg_id=pkg_id, app_id=app_id,
                    pack_dir=str(dest))
    if not specs_existed:
        created.append(shared / "apps" / "specs")
    created.append(written)
    why = _restore_evolve_ownership(created)
    if why:
        return _err(
            f"pack_ownership_failed: {why}. The pack is on disk but owned by the "
            f"wrong account, so the admin daemon cannot read it; fix ownership "
            f"(evolve:{get_profile().admin_group}) and re-run.",
            pkg_id=pkg_id, app_id=app_id, pack_dir=str(dest),
        )
    return _ok(app_id=app_id, pkg_id=pkg_id, pack_dir=str(dest), spec_path=str(written),
               files=files, pack_sha256=src_sha, dry_run=False,
               seeded=not already, already_seeded=already, spec_version=spec.spec_version)


def _install_sections(pkg: dict, bot_id: str, network: dict, *, pkg_id: str) -> list[dict]:
    from .install_helpers import install_heartbeat_instruction

    out: list[dict] = []
    for sec in section_actions(pkg):
        result = install_heartbeat_instruction(
            bot_id, sec["file"], sec["section_anchor"], sec["body"],
            pkg_id=pkg_id, network=network,
        )
        out.append({
            "action_id": sec["action_id"],
            "mechanism": sec["mechanism"],
            "file": sec["file"],
            "section_anchor": sec["section_anchor"],
            "ok": bool(result.get("ok")),
            "artifact": result.get("artifact") or "",
            "already_present": bool(result.get("already_present")),
            "error": result.get("error") or "",
            "bytes": len(sec["body"].encode("utf-8")),
        })
    return out


def install_gallery_pack(
    pkg_id: str,
    bot_id: str,
    *,
    shared_dir: Path | str | None = None,
    network: dict | None = None,
    dry_run: bool = True,
) -> dict:
    """Seed the pack + Spec from the gallery, then install through AL-3.2.

    Envelope on success: ``{ok, app_id, pkg_id, bot_id, dry_run, seed{},
    install{}, sections[], installed_apps_md}``. ``install`` is
    ``install_app_to_bot``'s own envelope, unmodified, so its ``planned[]``,
    ``installed[]``, ``failed[]`` and ``proof{}`` read exactly as they do from
    the Install-to… button. A section that fails to land after the files did
    is reported per section with ``ok: False`` on the envelope — the files are
    on disk and the instance is written (the spine wrote it), and saying
    "installed" over a bot whose guidance never arrived would be the
    half-install the spine refuses for files.
    """
    from ..config import DEFAULT_SHARED_DIR, load_network

    pkg_id, bot_id = _s(pkg_id), _s(bot_id)
    if not bot_id:
        return _err("missing_bot_id", refused=True)
    if network is None:
        try:
            network = load_network()
        except Exception as exc:  # noqa: BLE001
            return _err(f"network_load_failed: {exc}")
    shared = Path(shared_dir or network.get("sharedDir") or DEFAULT_SHARED_DIR)

    seed = seed_pack_from_gallery(pkg_id, shared_dir=shared, dry_run=dry_run)
    if not seed.get("ok"):
        return _err(seed["error"], refused=bool(seed.get("refused")), pkg_id=pkg_id,
                    bot_id=bot_id, seed=seed)
    app_id = seed["app_id"]
    pkg = load_gallery_package(pkg_id, shared) or {}
    sections_planned = [
        {k: v for k, v in sec.items() if k != "body"} | {"bytes": len(sec["body"].encode("utf-8"))}
        for sec in section_actions(pkg)
    ]

    if dry_run:
        # No pack on disk yet (seeding is a write), so the spine cannot plan
        # against it; the plan IS the gallery pack's file list plus the
        # sections, and the apply reports what actually landed.
        return _ok(app_id=app_id, pkg_id=pkg_id, bot_id=bot_id, dry_run=True, seed=seed,
                   install={"planned": [f["path"] for f in seed["files"]]},
                   sections=sections_planned, installed_apps_md="")

    install = install_app_to_bot(
        app_id, bot_id, shared_dir=shared, network=network,
        snapshot_if_needed=False, dry_run=False,
    )
    if not install.get("ok"):
        return _err(install.get("error") or "install_failed",
                    refused=bool(install.get("refused")), app_id=app_id, pkg_id=pkg_id,
                    bot_id=bot_id, seed=seed, install=install, sections=[])

    sections = _install_sections(pkg, bot_id, network, pkg_id=pkg_id)
    apps_md = ""
    try:
        from .app_registry import regenerate_installed_apps_md
        written = regenerate_installed_apps_md(bot_id, shared)
        apps_md = str(written) if written else ""
    except Exception as exc:  # noqa: BLE001 — the menu is a convenience; the install is not
        logger.warning("gallery_pack_install: INSTALLED_APPS.md not regenerated for %s: %s",
                       bot_id, exc)

    failed = [s for s in sections if not s["ok"]]
    if failed:
        return _err(
            "sections_failed: the files landed and the instance is written, but "
            + "; ".join(f"{s['file']}#{s['section_anchor']}: {s['error']}" for s in failed)
            + " — the bot has the app without its guidance. Fix the file and re-run; "
              "the section install is idempotent.",
            app_id=app_id, pkg_id=pkg_id, bot_id=bot_id, seed=seed, install=install,
            sections=sections, installed_apps_md=apps_md,
        )
    return _ok(app_id=app_id, pkg_id=pkg_id, bot_id=bot_id, dry_run=False, seed=seed,
               install=install, sections=sections, installed_apps_md=apps_md)
