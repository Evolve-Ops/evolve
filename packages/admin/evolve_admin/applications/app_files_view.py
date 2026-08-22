"""app_files_view — the Files panel's read model (AL-1.8b, design §4a / D-U6).

The 1.8a shell showed an app's bots and its costs and lost the thing the old
manifest modal did have: *which files are this app*. This module is the read
model that brings it back, in the shape the Spec/Instance split makes
possible — **files belong to the app, bots are realization columns**:

    path · role · the app's recorded digest      ← one row per Spec file
      └── team-bot-a: ok   team-bot-c: missing   ← per-bot realization chips

WHY THIS IS NOT "list the manifest's files[]". A manifest's file list is one
BOT's copy. Two bots running the same app each have one, and reading either
of them as "the app's files" is exactly the bot-centric answer the pod-first
surface exists to replace. So the app-level list comes from the Spec
(``package.files[]``, or the projection ``app_spec.spec_from_manifest``
derives for a legacy manifest), and each bot's workspace is measured against
it with AL-1.5c's ``resolve_workspace_package_files``.

THE SIX STATES, AND WHY THERE ARE SIX (brief §1, principle-tri-state-status):

  ``ok``                  the bot's copy hashes to the app's recorded digest
  ``differs_placeholder`` it differs, and re-substituting the package source
                          under THIS bot's install context reproduces the
                          bot's digest exactly — §9.6's machine-checkable
                          sense, never a guess from "well, it has placeholders"
  ``differs``             it differs and nothing explains the difference
  ``missing``             the app declares it; this bot's workspace has not got it
  ``cant_read``           present but unreadable, or the path is a directory
  ``cant_measure``        no digest to compare against, no workspace to read,
                          or this bot's copy does not list the file at all

``differs`` exists because the brief's rule is that *anything unexplained
renders as its own honest state, never as "ok"*. Collapsing it into
``differs_placeholder`` would claim an explanation the pod does not have;
collapsing it into ``ok`` would be the silent-degradation failure the
tri-state principle is written against.

WHICH DIGEST THE CHIP SHOWS IS LABELLED, ALWAYS (AL-1.5c §9.2). That field
has two carriers and the artifact does not say which one it holds: a
files-pack supplies a **source** digest (pre-substitution), a manifest's own
entry or a workspace hash supplies a **realized** one (post-substitution).
This module does not fix that ambiguity — §9.2 records it as a Spec-shape
decision — it reports which carrier it actually read (``sha_kind``), so the
surface can say "source sha" or "as installed on team-bot-a" rather than
showing a hex string that means either.

BOUNDED BY CONSTRUCTION, AND DELIBERATELY NOT CACHED. One request measures
ONE app: its declared files × the bots that have it, hashed by streaming
(``resolve_workspace_package_files``). That is a handful of files, not the
pod — the live macOS census counted 1019 declared file references across 227
artifacts, so an average app is under five. A TTL cache was written and then
removed: it bought microseconds on a per-app read and cost the operator the
one thing this panel is for, which is *what is on disk right now*. A panel
whose whole job is to catch drift must not answer from a minute ago.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .app_spec import AppSpec, spec_from_manifest
from .app_spec_store import (
    PackageFileNote,
    resolve_package_files,
    resolve_workspace_package_files,
)

log = logging.getLogger(__name__)

__all__ = [
    "SHA_RECORDED",
    "SHA_REALIZED",
    "SHA_SOURCE",
    "STATE_CANT_MEASURE",
    "STATE_CANT_READ",
    "STATE_DIFFERS",
    "STATE_DIFFERS_PLACEHOLDER",
    "STATE_MISSING",
    "STATE_OK",
    "build_package_view",
    "build_uses_view",
]

STATE_OK = "ok"
STATE_DIFFERS_PLACEHOLDER = "differs_placeholder"
STATE_DIFFERS = "differs"
STATE_MISSING = "missing"
STATE_CANT_READ = "cant_read"
STATE_CANT_MEASURE = "cant_measure"

#: The digest carrier the app-level list was read from (§9.2).
SHA_SOURCE = "source"        # a files-pack: pre-substitution source bytes
SHA_REALIZED = "realized"    # one bot's workspace copy, hashed
SHA_RECORDED = "recorded"    # a written Spec / inline entry; carrier unknown

#: Note kinds ``resolve_workspace_package_files`` reports, mapped to states.
_NOTE_STATE = {
    "missing": STATE_MISSING,
    "unreadable": STATE_CANT_READ,
    "directory": STATE_CANT_READ,
    "no_workspace": STATE_CANT_MEASURE,
}

def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _resolve_for_bot(
    raw: dict,
    workspace: Path | None,
) -> "tuple[list[dict] | None, list[PackageFileNote]]":
    """``resolve_workspace_package_files`` for one install.

    ``workspace is None`` short-circuits WITHOUT calling the resolver: its
    own fallback would look the bot up through ``evolve_admin.config``, and a
    route that has already failed to resolve the workspace must not have a
    second, differently-shaped attempt happen underneath it.
    """
    if workspace is None:
        return None, []
    return resolve_workspace_package_files(raw, workspace=workspace)


def _pack_files(raw: dict) -> "list[dict] | None":
    """The files-pack file list for one install, or None."""
    try:
        return resolve_package_files(raw)
    except Exception:  # noqa: BLE001 — a gallery hiccup costs digests, not the panel
        return None


def _entries_from(spec: AppSpec) -> "list[dict]":
    return [dict(f) for f in (spec.package.get("files") or [])]


def _hashed(entries: "Iterable[dict]") -> int:
    return sum(1 for e in entries if _s(e.get("sha256")))


def build_package_view(
    installs: "Sequence[tuple[str, str, dict, dict, AppSpec]]",
    *,
    vnext: AppSpec | None = None,
    workspace_for: Callable[[str], "Path | None"],
    pack_context_for: "Callable[[str, dict], dict | None] | None" = None,
) -> dict[str, Any]:
    """The Files panel payload for one app.

    ``installs`` is ``pod_apps``' per-app tuple list
    ``[(bot_id, stem, raw, display, spec), ...]``. ``workspace_for`` resolves
    a bot's workspace root (None when it cannot be read — which becomes
    ``cant_measure``, never a fabricated ``ok``). ``pack_context_for`` is the
    substitution context used for the placeholder check; omit it and the
    check simply never fires, so a difference reads as ``differs`` rather
    than as an explanation nobody verified.
    """
    ordered = sorted(installs, key=lambda i: i[0])
    if not ordered:
        return {"files": [], "sha_kind": None, "sha_kind_bot": None,
                "declared": 0, "hashed": 0}

    # ── Per-bot realization, measured once per install ──────────────────────
    realized: dict[str, dict[str, str]] = {}
    ws_files: "dict[str, list[dict] | None]" = {}
    note_kinds: dict[str, dict[str, str]] = {}
    note_detail: dict[str, dict[str, str]] = {}
    declared_by_bot: dict[str, set[str]] = {}
    workspaces: dict[str, "Path | None"] = {}
    packs: dict[str, "list[dict] | None"] = {}
    for bot_id, _stem, raw, _display, _spec in ordered:
        try:
            ws = workspace_for(bot_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("app_files_view: workspace lookup failed for %s: %s", bot_id, exc)
            ws = None
        workspaces[bot_id] = ws
        packs[bot_id] = _pack_files(raw)
        files, notes = _resolve_for_bot(raw, ws)
        ws_files[bot_id] = files
        realized[bot_id] = {
            _s(f.get("path")): _s(f.get("sha256")) for f in (files or [])
        }
        declared_by_bot[bot_id] = set(realized[bot_id])
        note_kinds[bot_id] = {n.path: n.kind for n in notes}
        note_detail[bot_id] = {n.path: n.detail for n in notes}

    # ── The app-level file list, and WHICH digest it carries ────────────────
    entries, sha_kind, sha_kind_bot = _source_entries(ordered, vnext, ws_files, packs)

    files_out: list[dict[str, Any]] = []
    for entry in entries:
        path = _s(entry.get("path"))
        if not path:
            continue
        source_sha = _s(entry.get("sha256"))
        row: dict[str, Any] = {
            "path": path,
            "role": _s(entry.get("role")),
            "sha256": source_sha,
            # None rather than the package label when there is no digest:
            # "not hashed" is not a kind of digest.
            "sha_kind": sha_kind if source_sha else None,
            "bots": {},
        }
        for bot_id, _stem, raw, _display, _spec in ordered:
            row["bots"][bot_id] = _bot_state(
                path,
                source_sha=source_sha,
                sha_kind=sha_kind,
                bot_id=bot_id,
                raw=raw,
                workspace=workspaces[bot_id],
                realized=realized[bot_id],
                declared=declared_by_bot[bot_id],
                note_kind=note_kinds[bot_id].get(path, ""),
                note_detail=note_detail[bot_id].get(path, ""),
                pack=packs[bot_id],
                pack_context_for=pack_context_for,
            )
        files_out.append(row)

    return {
        "files": files_out,
        "sha_kind": sha_kind,
        "sha_kind_bot": sha_kind_bot,
        "declared": len(files_out),
        "hashed": _hashed(files_out),
    }


def _source_entries(
    ordered: "Sequence[tuple[str, str, dict, dict, AppSpec]]",
    vnext: AppSpec | None,
    ws_files: "dict[str, list[dict] | None]",
    packs: "dict[str, list[dict] | None]",
) -> "tuple[list[dict], str | None, str | None]":
    """The app's file list plus the carrier its digests came from.

    Order of preference, and the reason for each:

    1. **A written v-next Spec.** It IS the app's portable intent; a bot's
       manifest is one realization of it.
    2. **A files-pack**, whichever install has one — design §6 names it and
       its digests are pre-substitution source (AL-1.5c §9.1).
    3. **The best-hashed workspace read.** "Best" is the install with the
       most digests, not the alphabetically first: picking a bot whose copy
       has gone missing would report the app's own files as unhashed and
       hide the drift the panel exists to show. A TIE goes to the
       alphabetically first bot (the loop keeps a strictly-greater winner
       over an already-sorted list), so which bot is the reference never
       depends on directory iteration order — the same reason
       ``app_promotion_sweep`` tiebreaks its ranking by stem.
    4. **The legacy derivation** — path and role, digests mostly absent,
       which the rows then render as "not hashed".
    """
    pack_bot = next((b for b, _s_, _r, _d, _sp in ordered if packs.get(b)), None)

    if vnext is not None and (vnext.package.get("files") or []):
        entries = _entries_from(vnext)
        if not _hashed(entries):
            return entries, None, None
        # A written Spec does not record which carrier filled its digests
        # (§9.2). Where a pack still resolves for this app the answer is
        # knowable; where it does not, "recorded" is the honest label.
        return entries, (SHA_SOURCE if pack_bot else SHA_RECORDED), None

    if pack_bot is not None:
        _b, _st, _raw, display, _spec = next(i for i in ordered if i[0] == pack_bot)
        entries = _entries_from(
            spec_from_manifest(display, package_files=packs[pack_bot])
        )
        if entries:
            return entries, (SHA_SOURCE if _hashed(entries) else None), None

    best_bot, best_entries, best_hashed = None, [], -1
    for bot_id, _stem, _raw, display, _spec in ordered:
        resolved = ws_files.get(bot_id)
        if not resolved:
            continue
        # The resolver's OWN entries, not a rebuilt {path, sha256} pair:
        # they carry ``role``/``purpose``/``marker_state``, and dropping
        # those is the exact regression AL-1.5c §9.3a caught (333 entries
        # carry a role under a key a narrow check does not look at).
        entries = _entries_from(spec_from_manifest(
            display, package_files=resolved,
        ))
        count = _hashed(entries)
        if count > best_hashed:
            best_bot, best_entries, best_hashed = bot_id, entries, count
    if best_entries:
        return best_entries, (SHA_REALIZED if best_hashed > 0 else None), (
            best_bot if best_hashed > 0 else None
        )

    # Nothing on disk resolved: fall back to whatever the artifact itself
    # declares, digests and all (some forge write paths stamp one inline).
    entries = _entries_from(ordered[0][4])
    return entries, (SHA_RECORDED if _hashed(entries) else None), None


def _bot_state(
    path: str,
    *,
    source_sha: str,
    sha_kind: "str | None",
    bot_id: str,
    raw: dict,
    workspace: "Path | None",
    realized: "dict[str, str]",
    declared: "set[str]",
    note_kind: str,
    note_detail: str,
    pack: "list[dict] | None",
    pack_context_for: "Callable[[str, dict], dict | None] | None",
) -> dict[str, Any]:
    """One (file, bot) cell. Never returns ``ok`` on an unverified equality."""
    if workspace is None:
        return _cell(STATE_CANT_MEASURE,
                     note="this bot's workspace could not be read")
    if path not in declared:
        return _cell(STATE_CANT_MEASURE,
                     note="this bot's copy of the app does not list this file")
    if note_kind:
        return _cell(_NOTE_STATE.get(note_kind, STATE_CANT_MEASURE),
                     note=note_detail or note_kind)
    bot_sha = realized.get(path, "")
    if not bot_sha:
        return _cell(STATE_CANT_MEASURE, note="this bot's copy could not be hashed")
    if not source_sha:
        return _cell(STATE_CANT_MEASURE, realized_sha=bot_sha,
                     note="the app has no recorded digest to compare against")
    if bot_sha == source_sha:
        return _cell(STATE_OK, realized_sha=bot_sha)
    if sha_kind == SHA_SOURCE and _substitution_explains(
        path, bot_id=bot_id, raw=raw, pack=pack,
        realized_sha=bot_sha, pack_context_for=pack_context_for,
    ):
        return _cell(STATE_DIFFERS_PLACEHOLDER, realized_sha=bot_sha,
                     note="differs only by the substitutions the package declares")
    return _cell(STATE_DIFFERS, realized_sha=bot_sha,
                 note="differs from the app's recorded digest, and nothing "
                      "on the pod explains why")


def _cell(state: str, *, realized_sha: str = "", note: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"state": state}
    # Absent, not empty-string: "we did not compute one" is not "the digest
    # is blank" (principle-tri-state-status).
    if realized_sha:
        out["realized_sha"] = realized_sha
    if note:
        out["note"] = note
    return out


def _substitution_explains(
    path: str,
    *,
    bot_id: str,
    raw: dict,
    pack: "list[dict] | None",
    realized_sha: str,
    pack_context_for: "Callable[[str, dict], dict | None] | None",
) -> bool:
    """AL-1.5c §9.6's machine-checkable clause, for one file on one bot.

    Re-substitute the PACKAGE SOURCE under this bot's install context and
    hash the result: if it reproduces the bot's realized digest exactly, the
    difference is fully explained by the declared placeholders and by nothing
    else. Anything the check cannot obtain — the pack directory, the file's
    declared placeholder list, the bot's context — returns False, so the cell
    falls through to ``differs``. A "probably placeholders" answer is exactly
    what §9.6 refused to accept.
    """
    if not pack or pack_context_for is None:
        return False
    try:
        from .files_pack import (
            load_files_pack_metadata,
            substitute_placeholders,
        )
        from .gallery import find_files_pack_dir

        # identity: see resolve_app_id — ``pkg_id`` is read here as the
        # PACKAGE ATTRIBUTION NAMESPACE (the files-pack directory key under
        # ``gallery/<slug>/files/``), never as the app's identity. It is the
        # same read ``app_spec_store.resolve_package_files`` makes and for
        # the same reason: the packs are not named by app_id, so a lookup
        # keyed on the app's identity would find nothing. The app's identity
        # on this artifact comes from ``resolve_app_id``, which the route's
        # install context passes separately. AL-1.4b §3.
        pkg_id = _s(raw.get("pkg_id"))
        if not pkg_id:
            return False
        pack_dir = find_files_pack_dir(pkg_id)
        if pack_dir is None:
            return False
        meta = load_files_pack_metadata(pack_dir)
        if meta is None:
            return False
        entry = next((f for f in meta.files if f.path == path), None)
        if entry is None or not (entry.placeholders or []):
            return False
        context = pack_context_for(bot_id, raw)
        if not context:
            return False
        source_text = (Path(pack_dir) / entry.path).read_text(encoding="utf-8")
        predicted = substitute_placeholders(
            source_text, list(entry.placeholders), context,
        )
        return hashlib.sha256(predicted.encode("utf-8")).hexdigest() == realized_sha
    except Exception as exc:  # noqa: BLE001 — see docstring; unexplained wins
        log.debug("app_files_view: substitution check failed for %s/%s: %s",
                  bot_id, path, exc)
        return False


# ── The Uses panel ───────────────────────────────────────────────────────────

def build_uses_view(spec: AppSpec | None) -> dict[str, Any]:
    """``requires{}`` + ``exclusive_tools`` for the Uses panel.

    Returns the four §5 groups verbatim (``skills`` / ``tools`` /
    ``integrations`` / ``secrets``) plus ``exclusive_tools``, and a
    ``declared`` count so the surface can tell "this app declares nothing"
    from "this app has no Spec" — absent is not empty (tri-state).

    **Secret NAMES only.** ``requires.secrets`` is a list of credential names
    by construction (``app_spec._derive_requires`` reads ``name``/``id`` off
    the credential entries and nothing else), and nothing in this function
    reaches for a value. Said out loud because the panel renders next to
    ``integrations`` and a future edit that "helpfully" joined the two would
    be putting a credential on a web page.
    """
    if spec is None:
        return {"requires": None, "exclusive_tools": [], "declared": 0}
    requires = {
        key: [str(v) for v in (spec.requires.get(key) or [])]
        for key in ("skills", "tools", "integrations", "secrets")
    }
    exclusive = [str(t) for t in (spec.exclusive_tools or [])]
    return {
        "requires": requires,
        "exclusive_tools": exclusive,
        "declared": sum(len(v) for v in requires.values()) + len(exclusive),
    }
