"""
files_pack.py — Files-pack hybrid install foundation.

Spec: docs/spec-files-pack-hybrid-2026-06-03.md.

The files-pack hybrid replaces the default forge install path (LLM
generates files from build_spec, ~$30/install) with a copy + substitute
path (~$0/install) for any gallery package that ships canonical files
alongside its manifest. The build_spec stays first-class — it's the
durable contract; the files-pack is the snapshot the install
mechanically deploys.

This module is the FOUNDATION (F-P.1 of the spec's implementation
plan). It exposes the pure-data primitives the install dispatcher
will compose in F-P.2:

  substitute_placeholders(content, declared, context) -> str
      The substitution engine. Resolves the 7 v1 placeholders
      ({bot_id}, {bot_user}, {workspace}, {shared_dir}, {pkg_id},
      {app_id}, {installed_at}) against the install context. Scoped
      to ``declared`` — placeholders not in the list pass through
      untouched. Honors {{/}} double-brace escapes. Raises
      FilesPackPlaceholderError on empty resolution (spec §7 Q3).

  load_files_pack_metadata(files_pack_dir) -> FilesPackMetadata | None
      Reads ``gallery/<slug>/files/manifest.json`` and validates the
      shape. Returns None when missing; raises FilesPackError on
      malformed metadata so the install path can surface a clear
      error before walking any files.

  verify_files_pack_integrity(files_pack_dir, metadata)
      -> list[FilesPackIntegrityFinding]
      Walks the metadata's files[], confirms each on-disk file
      matches its declared SHA-256 (spec §7 Q2). A non-empty result
      means the files-pack has drifted from what was snapshotted and
      MUST NOT install.

No behaviour change in this PR: the install dispatcher continues to
take the LLM-forge path for every package. F-P.2 wires these
primitives into the install path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# ── Public format constants ──────────────────────────────────────────────────


FILES_PACK_FORMAT_VERSION = "1.0"

# The 7 v1 placeholders the substitution engine resolves. The deriver
# and snapshot tool MUST emit metadata using only these names —
# unrecognised placeholders surface as ``FilesPackPlaceholderError`` at
# install time. New placeholders bump ``FILES_PACK_FORMAT_VERSION``.
KNOWN_PLACEHOLDERS: frozenset[str] = frozenset({
    "bot_id",
    "bot_user",
    "workspace",
    "shared_dir",
    "pkg_id",
    "app_id",
    "installed_at",
})


# ── Exceptions ───────────────────────────────────────────────────────────────
#
# Three distinct error types so the install dispatcher can render
# different operator guidance. ``FilesPackError`` is the base — catch
# this when the dispatcher just wants "anything related to the
# files-pack went wrong".


class FilesPackError(Exception):
    """Base for any files-pack failure (load, integrity, substitution)."""


class FilesPackPlaceholderError(FilesPackError):
    """A declared placeholder resolved to an empty / missing value.

    Per spec §7 Q3: this is a hard error rather than a silent empty
    substitution. The install dispatcher should surface the file path
    + placeholder name so the operator can fix the missing context
    (typically a malformed network.json entry) and retry.
    """


class FilesPackIntegrityError(FilesPackError):
    """The metadata + on-disk content disagree (SHA mismatch, missing
    file, etc.). Install MUST refuse to proceed — the files-pack has
    drifted from what was snapshotted."""


class FilesPackFormatError(FilesPackError):
    """The files-pack manifest doesn't parse or doesn't match the
    expected shape. Usually a stale gallery file from before the
    schema version we understand."""


# ── Substitution engine ─────────────────────────────────────────────────────


# Matches a {placeholder} that is NOT part of a {{escape}}. The
# negative-lookbehind for "{" and the negative-lookahead for "}"
# together ensure we treat ``{{bot_id}}`` as literal `{bot_id}`,
# leaving the placeholder unmolested.
#
# Why a regex and not str.format: str.format would happily error on a
# literal `{` in source content (e.g. a Python dict literal in a
# generated script), but here we WANT literal braces to pass through
# unless they form a recognised placeholder. The regex is also
# directional — it only substitutes patterns we declared — which is
# the spec's safety property (§6 Rule 1).
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")


def substitute_placeholders(
    content: str,
    declared: Iterable[str],
    context: dict[str, str],
) -> str:
    """Substitute declared placeholders in ``content`` using ``context``.

    Args:
        content: file content (read as text). Returned unchanged when
            ``declared`` is empty.
        declared: list of placeholder names this file is allowed to
            substitute. Per spec §6 Rule 1, placeholders not in this
            list pass through untouched — the protection against a
            Python source file's literal `{bot_id}` docstring getting
            accidentally substituted.
        context: ``{name: value}`` for the install. Must contain every
            entry in ``declared``. Missing or empty entries raise
            ``FilesPackPlaceholderError`` (spec §7 Q3).

    Returns: substituted content.

    Raises:
        FilesPackPlaceholderError: a declared placeholder resolves to
            None / empty string, OR a declared placeholder is not in
            ``KNOWN_PLACEHOLDERS``.
    """
    declared_set = frozenset(declared)
    if not declared_set:
        return content

    # Sanity-check up front so a typo in the metadata produces a
    # clear error before we touch the content.
    unknown = declared_set - KNOWN_PLACEHOLDERS
    if unknown:
        raise FilesPackPlaceholderError(
            f"declared placeholder(s) {sorted(unknown)!r} are not in "
            f"the v{FILES_PACK_FORMAT_VERSION} KNOWN_PLACEHOLDERS set; "
            f"either fix the metadata or bump the files-pack format "
            f"version."
        )

    # Verify each declared placeholder has a usable value in context.
    # We check up-front (rather than during the regex pass) so the
    # error surfaces with the placeholder name, not whatever
    # substring the regex happened to land on first.
    missing = [
        name for name in sorted(declared_set)
        if not context.get(name)
    ]
    if missing:
        raise FilesPackPlaceholderError(
            f"declared placeholders {missing!r} resolve to empty / "
            f"missing values in the install context. Hard error per "
            f"spec §7 Q3 — fix the missing context (likely a malformed "
            f"network.json entry) and retry."
        )

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in declared_set:
            return context[name]
        # Not in the declared list — pass through untouched.
        return match.group(0)

    substituted = _PLACEHOLDER_RE.sub(_replace, content)

    # Unescape literal braces last so a `{{bot_id}}` in the source
    # ends up as `{bot_id}` in the output rather than getting
    # re-substituted on a second pass.
    return substituted.replace("{{", "{").replace("}}", "}")


# ── Per-file metadata schema ────────────────────────────────────────────────


@dataclass
class FilesPackFile:
    """One entry in ``gallery/<slug>/files/manifest.json[files]``.

    The shape mirrors the spec's example (§4) but uses snake_case for
    Python ergonomics. ``placeholders_in_path`` is the rare case where
    the FILENAME itself contains a placeholder (e.g.
    ``scripts/{bot_id}-cron.sh``); we keep this off by default per
    spec §6 Rule 2.
    """

    path: str
    mode: str                       # "0644" / "0755" / ...
    sha256: str
    placeholders: list[str] = field(default_factory=list)
    placeholders_in_path: list[str] = field(default_factory=list)
    size_bytes: int = 0


@dataclass
class FilesPackMetadata:
    """The parsed ``gallery/<slug>/files/manifest.json``."""

    format_version: str
    snapshot_source: dict
    files: list[FilesPackFile]
    # F-P.4.x — smart-forge model awareness
    # (docs/note-smart-forge-and-file-provenance-2026-06-04.md).
    # When True, this files-pack is deliberately partial: the package
    # manifest's files[] may declare paths not present here, with
    # provenance="forge" expected on those entries. The integrity
    # sweep treats orphan bundled-files declared in the manifest but
    # missing from the pack as warnings when ``partial=True``, errors
    # when False (the default). Operator can stamp this at snapshot
    # time or hand-edit the metadata.
    partial: bool = False
    # Free-form hint for the review UI (e.g. "stable_scripts",
    # "doc_skeletons", "prompts_only"). Empty when not declared.
    coverage_intent: str = ""
    # F-P.13 — optional Ed25519 signature block. When present, the
    # install dispatcher can verify the metadata against a public key
    # (typically pulled from ``contributor.public_key`` on the
    # package manifest) to confirm the files-pack hasn't been
    # tampered with since the contributor signed it. Empty dict when
    # the pack is unsigned. Shape:
    #   {version, algo, signer_key_id, signed_at, value}
    # See applications.files_pack_signing for the sign/verify
    # primitives.
    signature: dict = field(default_factory=dict)


# Mode strings must match POSIX permission digits. We refuse anything
# else so a stray "rwxr-xr-x" or "0o644" in the metadata surfaces
# loudly.
_MODE_RE = re.compile(r"^0[0-7]{3,4}$")


def _coerce_files(raw_files: Any) -> list[FilesPackFile]:
    if not isinstance(raw_files, list):
        raise FilesPackFormatError(
            f"files-pack manifest 'files' must be a list, got "
            f"{type(raw_files).__name__}"
        )
    out: list[FilesPackFile] = []
    for i, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise FilesPackFormatError(
                f"files[{i}] must be a dict, got {type(entry).__name__}"
            )
        path = (entry.get("path") or "").strip()
        mode = (entry.get("mode") or "").strip()
        sha256 = (entry.get("sha256") or "").strip().lower()
        if not path:
            raise FilesPackFormatError(f"files[{i}] missing 'path'")
        if not _MODE_RE.match(mode):
            raise FilesPackFormatError(
                f"files[{i}] mode {mode!r} is not POSIX octal "
                f"(e.g. '0644' or '0755')"
            )
        if not re.match(r"^[0-9a-f]{64}$", sha256):
            raise FilesPackFormatError(
                f"files[{i}] sha256 {sha256!r} is not a 64-char "
                f"lowercase hex string"
            )
        placeholders = entry.get("placeholders") or []
        if not isinstance(placeholders, list):
            raise FilesPackFormatError(
                f"files[{i}] 'placeholders' must be a list, got "
                f"{type(placeholders).__name__}"
            )
        placeholders_in_path = entry.get("placeholders_in_path") or []
        if not isinstance(placeholders_in_path, list):
            raise FilesPackFormatError(
                f"files[{i}] 'placeholders_in_path' must be a list"
            )
        size = entry.get("size_bytes")
        if size is None:
            size = 0
        if not isinstance(size, int) or size < 0:
            raise FilesPackFormatError(
                f"files[{i}] 'size_bytes' must be a non-negative int; "
                f"got {size!r}"
            )
        out.append(FilesPackFile(
            path=path,
            mode=mode,
            sha256=sha256,
            placeholders=[str(p) for p in placeholders],
            placeholders_in_path=[str(p) for p in placeholders_in_path],
            size_bytes=size,
        ))
    return out


def load_files_pack_metadata(files_pack_dir: Path) -> FilesPackMetadata | None:
    """Load + parse ``<dir>/manifest.json``.

    Returns ``None`` when no manifest.json exists (the gallery
    package simply doesn't have a files-pack; the dispatcher falls
    through to LLM-forge). Raises ``FilesPackFormatError`` when the
    file is present but malformed — that's a real bug the operator
    needs to see.
    """
    target = files_pack_dir / "manifest.json"
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise FilesPackFormatError(
            f"could not read {target}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise FilesPackFormatError(
            f"files-pack manifest at {target} must be a JSON object, "
            f"got {type(raw).__name__}"
        )
    format_version = (raw.get("format_version") or "").strip()
    if not format_version:
        raise FilesPackFormatError(
            f"{target} is missing 'format_version'"
        )
    # We tolerate >= our current version in case a forward-compatible
    # release lands; tighten this if a future format breaks back-compat.
    if format_version != FILES_PACK_FORMAT_VERSION:
        log.warning(
            "files-pack at %s declares format_version=%s; this code "
            "understands %s. Continuing best-effort.",
            target, format_version, FILES_PACK_FORMAT_VERSION,
        )
    snapshot_source = raw.get("snapshot_source") or {}
    if not isinstance(snapshot_source, dict):
        raise FilesPackFormatError(
            f"{target}: 'snapshot_source' must be a dict"
        )
    files = _coerce_files(raw.get("files"))
    # F-P.13 — optional signature block. Loader is permissive: the
    # block is passed through as-is (the verifier in
    # files_pack_signing does the strict checks). When absent or
    # malformed, the field is just {} and downstream verification
    # returns ("no_signature", False).
    raw_sig = raw.get("signature")
    signature = raw_sig if isinstance(raw_sig, dict) else {}
    return FilesPackMetadata(
        format_version=format_version,
        snapshot_source=snapshot_source,
        files=files,
        partial=bool(raw.get("partial", False)),
        coverage_intent=str(raw.get("coverage_intent") or "").strip(),
        signature=signature,
    )


# ── Integrity check ─────────────────────────────────────────────────────────


@dataclass
class FilesPackIntegrityFinding:
    """One issue from a verify_files_pack_integrity() pass."""

    path: str
    kind: str         # "missing" / "sha_mismatch" / "size_mismatch"
    detail: str


def _sha256_file(path: Path) -> str:
    """Stream a file's SHA-256 without loading it all into memory."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_files_pack_integrity(
    files_pack_dir: Path,
    metadata: FilesPackMetadata,
) -> list[FilesPackIntegrityFinding]:
    """Walk the metadata's files[] and check each against the disk.

    Each finding is one of:
      ``missing``        — file on metadata but not on disk
      ``sha_mismatch``   — file on disk but SHA-256 doesn't match the
                           metadata's declared value
      ``size_mismatch``  — declared size_bytes != on-disk size (when
                           size_bytes is non-zero in the metadata; we
                           skip the size check on zero to keep the
                           field optional)

    Empty list = files-pack is intact + safe to install from.

    Note: This runs against the on-disk content BEFORE substitution.
    Substitution happens at write-time on the target bot, so the SHA
    is always over the source-of-truth (gallery) file.
    """
    findings: list[FilesPackIntegrityFinding] = []
    for entry in metadata.files:
        target = files_pack_dir / entry.path
        if not target.is_file():
            findings.append(FilesPackIntegrityFinding(
                path=entry.path,
                kind="missing",
                detail=f"declared in manifest but not present on disk",
            ))
            continue
        actual_sha = _sha256_file(target)
        if actual_sha != entry.sha256:
            findings.append(FilesPackIntegrityFinding(
                path=entry.path,
                kind="sha_mismatch",
                detail=(
                    f"declared sha256={entry.sha256[:12]}…, "
                    f"actual={actual_sha[:12]}…"
                ),
            ))
            continue
        if entry.size_bytes:
            actual_size = target.stat().st_size
            if actual_size != entry.size_bytes:
                findings.append(FilesPackIntegrityFinding(
                    path=entry.path,
                    kind="size_mismatch",
                    detail=(
                        f"declared size_bytes={entry.size_bytes}, "
                        f"actual={actual_size}"
                    ),
                ))
    return findings


# ── Convenience: top-level files-pack SHA (spec §7 Q2) ─────────────────────


# ── Orphan-pattern linter (F-P.4) ───────────────────────────────────────────


@dataclass
class OrphanFinding:
    """One orphan-pattern finding from ``lint_files_pack_for_orphan_tokens``.

    "Orphan" = a bot/operator-specific token that appears in a
    files-pack file's CONTENT but is NOT covered by the file's
    ``placeholders[]`` list. These are the personal-data and
    infrastructure leaks the snapshot tool (F-P.3) is supposed to
    convert into placeholders. The linter catches anything F-P.3's
    auto-detection missed before the files-pack lands in the
    gallery.
    """

    path: str          # file inside the files-pack
    token: str         # the offending substring (e.g. a bot id, /Users/<bot>/)
    line_no: int       # 1-based line where it appears
    snippet: str       # the line, trimmed to ~120 chars
    suggested: str     # human-readable hint, e.g. "add 'bot_id' to placeholders"


def lint_files_pack_for_orphan_tokens(
    files_pack_dir: Path,
    metadata: FilesPackMetadata,
    reserved_tokens: Iterable[str],
    *,
    max_findings_per_file: int = 20,
) -> list[OrphanFinding]:
    """Walk every file in ``metadata`` and flag occurrences of
    ``reserved_tokens`` that aren't already covered by the file's
    declared ``placeholders[]`` list.

    Args:
        files_pack_dir: source ``gallery/<slug>/files/`` directory.
        metadata: parsed per-file manifest (from
            ``load_files_pack_metadata``).
        reserved_tokens: tokens to consider orphan when found in
            content. Typically a union of:
              - the public-launch scrub guard's reserved-token list
                (bot ids, operator names)
              - source-bot literals computed by the caller (e.g.
                "/Users/{source-bot-user}/")
            The linter is intentionally agnostic about WHAT counts
            as orphan — that's a policy decision for the caller.
        max_findings_per_file: cap so a single file with thousands of
            occurrences doesn't drown the output.

    Returns: list of :class:`OrphanFinding`, one per orphan match.

    A file with declared placeholders that COULD cover a token (e.g.
    a file declares ``placeholders=["bot_id"]`` and contains a literal
    ``com.someone.``) is treated as already-handled — the linter
    trusts the metadata. The protection here is for tokens that
    SHOULDN'T be in the files-pack at all.
    """
    findings: list[OrphanFinding] = []
    reserved = sorted(set(reserved_tokens), key=len, reverse=True)
    if not reserved:
        return findings

    # Map each token to the placeholder that, if declared, would
    # cover it. A token outside this map is always orphan — its
    # presence means F-P.3 missed it AND no operator review caught
    # it. The placeholder set is the v1 KNOWN_PLACEHOLDERS list.
    coverage_map: dict[str, str] = {}
    for tok in reserved:
        low = tok.lower()
        if low in {"bot_id", "bot_user"}:
            coverage_map[tok] = low
        elif "/users/" in low or "workspace" in low:
            coverage_map[tok] = "workspace"
        elif low.startswith("com."):
            coverage_map[tok] = "bot_id"
        elif tok.islower() and "/" not in tok and "." not in tok:
            # Bare lowercase token, no separators — most reserved-bot-id
            # entries match this shape (e.g. team-bot-a, team-bot-c).
            # Suggest {bot_id} as the substitution; operator review
            # decides if it's actually a bot id vs. some other
            # identifier.
            coverage_map[tok] = "bot_id"

    for entry in metadata.files:
        target = files_pack_dir / entry.path
        if not target.is_file():
            continue
        # Skip binary files — they're copied byte-for-byte at install
        # time and don't carry meaningful "tokens" for human review.
        if not entry.placeholders and not entry.placeholders_in_path:
            try:
                _ = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        declared = frozenset(entry.placeholders)
        file_count = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            for tok in reserved:
                if tok not in line:
                    continue
                # Is the corresponding placeholder declared? If yes,
                # the substitution will happen at install time —
                # not an orphan.
                covers = coverage_map.get(tok)
                if covers and covers in declared:
                    continue
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "…"
                suggested = (
                    f"add {covers!r} to placeholders[]"
                    if covers
                    else "review whether this should be substituted or removed"
                )
                findings.append(OrphanFinding(
                    path=entry.path,
                    token=tok,
                    line_no=lineno,
                    snippet=snippet,
                    suggested=suggested,
                ))
                file_count += 1
                if file_count >= max_findings_per_file:
                    break
            if file_count >= max_findings_per_file:
                break

    return findings


def compute_files_pack_sha256(files_pack_dir: Path) -> str:
    """Compute the spec §7 Q2 "top-level" SHA-256 used in the package
    manifest's ``files_pack.sha256`` field.

    Defined as the SHA-256 of ``files/manifest.json``'s bytes — that
    file's contents include every per-file SHA, so any drift in any
    file changes the top-level SHA transitively, AND a metadata-only
    edit (e.g. a placeholder name change) also moves it. Single
    "did anything in this files-pack change?" digest.
    """
    target = files_pack_dir / "manifest.json"
    if not target.is_file():
        raise FilesPackFormatError(f"no manifest at {target}")
    return _sha256_file(target)


# ── Install-context resolver helper ──────────────────────────────────────────


@dataclass
class InstallResult:
    """Outcome of installing a files-pack into a bot workspace.

    Shape is intentionally close to ``bot_forge.BuildResult.files_written``
    so the forge engine can drop this in where the bot's build output
    would have gone, then route through the existing verification +
    manifest-records pipeline unchanged.
    """

    files_written: list[dict]    # [{path, sha256}, ...] — relative to workspace
    bytes_total: int
    errors: list[str] = field(default_factory=list)


def install_files_pack_to_workspace(
    metadata: "FilesPackMetadata",
    files_pack_dir: Path,
    workspace_dir: Path,
    context: dict[str, str],
    allowed_paths: set[str] | None = None,
) -> InstallResult:
    """Copy every file in ``metadata.files`` to the bot's workspace,
    substituting declared placeholders content-by-content.

    The cost-saving path: this function does NOT call any LLM. It's
    pure-Python file IO + ``substitute_placeholders``.

    Args:
        metadata: the parsed per-file manifest from
            ``load_files_pack_metadata(files_pack_dir)``.
        files_pack_dir: the source ``gallery/<slug>/files/`` directory.
        workspace_dir: the target bot's workspace (typically
            ``/Users/{bot_user}/.openclaw/workspace``).
        context: built by ``resolve_install_context()``.
        allowed_paths: when set, only install files whose ``path``
            (verbatim, as recorded in the metadata) is in the set.
            This is the smart-forge dispatcher hook
            (docs/note-smart-forge-and-file-provenance-2026-06-04.md):
            for partial files-packs, the caller passes the set of
            paths the manifest marks as ``"bundled"`` so the LLM-forge
            phase can fill in the remaining ``"forge"`` files. When
            ``None`` (the default), every file in ``metadata`` is
            installed — preserves pre-existing all-or-nothing
            behaviour.

    Returns: :class:`InstallResult`. ``errors`` is non-empty when one or
        more files failed to write; partial success is recorded in
        ``files_written`` so the caller can decide whether to retry just
        the failures or back out everything.

    Raises:
        FilesPackPlaceholderError: a declared placeholder resolves
            empty for at least one file (propagated up from
            ``substitute_placeholders``).
        FilesPackIntegrityError: a source file in ``metadata`` is
            missing on disk. Run ``verify_files_pack_integrity`` first
            to surface this with a clean error message before write
            attempts begin.
    """
    files_written: list[dict] = []
    bytes_total = 0
    errors: list[str] = []

    for entry in metadata.files:
        # Smart-forge filter — when ``allowed_paths`` is set, skip files
        # whose path isn't in it. Lets the dispatcher copy ONLY the
        # subset of the files-pack the manifest marks as ``bundled``.
        if allowed_paths is not None and entry.path not in allowed_paths:
            continue

        src = files_pack_dir / entry.path
        if not src.is_file():
            raise FilesPackIntegrityError(
                f"files-pack source {src} missing — run "
                f"verify_files_pack_integrity before install"
            )

        # Read raw content. Binary files (placeholders=[]) round-trip
        # via bytes; text files with placeholders need str decoding.
        if entry.placeholders or entry.placeholders_in_path:
            try:
                content = src.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                errors.append(
                    f"{entry.path}: declared placeholders but file "
                    f"is not utf-8 text: {exc}"
                )
                continue
            substituted = substitute_placeholders(
                content, entry.placeholders, context,
            )
            payload: bytes = substituted.encode("utf-8")
        else:
            payload = src.read_bytes()

        # Resolve target path. If the FILENAME has placeholders, scan it
        # too — rare, but covers cases like ``scripts/{bot_id}-cron.sh``.
        rel_path = entry.path
        if entry.placeholders_in_path:
            rel_path = substitute_placeholders(
                rel_path, entry.placeholders_in_path, context,
            )

        target = workspace_dir / rel_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic-ish via temp + os.replace so a half-written file
            # doesn't get exec'd by launchd / the bot mid-install.
            tmp = target.with_suffix(target.suffix + ".tmp.installing")
            tmp.write_bytes(payload)
            os.chmod(tmp, int(entry.mode, 8))
            os.replace(tmp, target)
        except (PermissionError, OSError) as exc:
            errors.append(f"{entry.path}: could not write: {exc}")
            continue

        files_written.append({
            "path": rel_path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "mode": entry.mode,
        })
        bytes_total += len(payload)

    return InstallResult(
        files_written=files_written,
        bytes_total=bytes_total,
        errors=errors,
    )


def resolve_install_context(
    *,
    bot_id: str,
    bot_user: str,
    workspace: str,
    pkg_id: str,
    app_id: str,
    installed_at: str,
    shared_dir: str = "/Users/Shared/evolve",
) -> dict[str, str]:
    """Build the ``context`` dict for ``substitute_placeholders``.

    Centralised so the install dispatcher doesn't sprinkle string
    literals across the codebase. Every value is required (the
    install path resolves them upstream from network.json + the
    forge job + the package manifest). An empty value raises here so
    callers get the error at context-construction time rather than
    deep inside the substitution pass.
    """
    ctx = {
        "bot_id": bot_id,
        "bot_user": bot_user,
        "workspace": workspace,
        "shared_dir": shared_dir,
        "pkg_id": pkg_id,
        "app_id": app_id,
        "installed_at": installed_at,
    }
    missing = [k for k, v in ctx.items() if not v]
    if missing:
        raise FilesPackPlaceholderError(
            f"install context missing: {sorted(missing)!r}. The install "
            f"dispatcher must resolve every placeholder upstream so a "
            f"declared-but-missing case surfaces here, not at write-time."
        )
    return ctx
