"""File-layer classifier — rules-first cascade for v20 layer assignment.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §3.1 + §3.5.

The classifier decides which of the nine v20 layer values a file belongs
to. Reconciliation severity (§3.2) depends on the layer — a missing
``code`` file is critical, a missing ``data`` file is info — so wrong
classifications produce wrong reconciliation behavior. Validation against
the production pod surfaced multiple legacy mis-classifications (Marie
content files stamped ``state`` instead of ``content``, ``HEARTBEAT.md``
stamped ``state`` instead of ``behavior_doc``) — PR 3 fixes these by
re-classifying every existing files[*] entry rather than trusting the
legacy stamper.

## The cascade

Rules-first, in priority order (§3.5):

  1. **Path glob match** against ``volatile_paths[*].glob``. Wins
     immediately when matched — operator-declared paths are
     authoritative.
  2. **Filename pattern** — specific filenames like ``README.md``,
     ``AGENTS.md``, ``manifest.json`` map to their canonical layer.
     Globs like ``*.lock``, ``*.log`` also fire here.
  3. **Parent directory** — ``scripts/`` → code, ``data/`` → data,
     ``config/`` → config, etc. The most operator-natural heuristic;
     directories communicate intent.
  4. **Extension** — ``.py/.sh/.js/.ts/.rb`` → code, ``.json/.yaml/
     .toml`` → config (default; rule 5 may demote to ``data``).
  5. **Reference graph** — if the file appears as a string in any
     ``code``-layer file's content, bump to ``config`` (.json/.yaml/
     .toml) or ``content`` (other extensions). Skipped when caller
     doesn't pass ``code_file_contents``.
  6. **LLM fallback** — off by default in PR 3 (the ``use_llm`` flag
     is the hook; no LLM call wired up here). PR 4+ enables for the
     residual cases rules 1-5 don't classify.

## Why pure Python first

Cost discipline (spec §1 goal #6): even at scale, rules 1-4 produce
high-quality classifications for ~95% of files at zero LLM cost. LLM is
the escape hatch for the residual, not the default path.

## Idempotency

The classifier is a pure function of (path, volatile_paths,
code_file_contents). Repeated calls produce the same result. Scanner
Phase 5.5 (§7.1) calls it on every files[*] entry — passing the same
entry twice is safe.

## Module shape

* ``classify(path, ...)`` — the public entrypoint, returns a
  ``ClassificationResult`` with layer + method + confidence + evidence
* ``apply_to_manifest(manifest, ...)`` — re-classifies every files[*]
  entry, capturing the prior layer as ``_legacy_layer`` when changed
* ``VALID_LAYERS``, ``CLASSIFICATION_METHOD_*`` constants for callers
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any


# ── The v20 layer enum (spec §3.1) ──────────────────────────────────────────
#
# Mirrors the docstring at the top of manifest.py. Changes here ripple to
# reconciliation severity (§3.2) and coherence Pass A assertions (§6.1) —
# don't add a layer without updating both.

VALID_LAYERS = frozenset({
    "code",          # scripts the app runs (.py, .sh, .js, .ts, .rb)
    "config",        # declared structured config the code reads
    "contract",      # the manifest itself, schemas, interface contracts
    "procedure",     # LLM-readable runbooks the heartbeat loads (procedures/*.md)
    "behavior_doc",  # AGENTS.md / HEARTBEAT.md / SOUL.md / POD_CONDUCT.md
    "reference",     # README, runbooks, design docs the code doesn't load
    "content",       # articles, prompts, templates the app reads as payload
    "data",          # journals, observations, generated outputs
    "log",           # append-only operational logs
    "state",         # locks, checkpoints, indexes, caches
})


# ── Classification method identifiers ───────────────────────────────────────
#
# Stamped on each files[*] entry as ``classification_method`` so the trail
# can attribute layer decisions to the rule that produced them. Useful for
# debugging classifier drift ("why did this file get tagged code? — rule
# 3 (parent_directory: scripts/)").

CLASSIFICATION_METHOD_VOLATILE_PATH = "volatile_path"
CLASSIFICATION_METHOD_FILENAME      = "filename_pattern"
CLASSIFICATION_METHOD_DIRECTORY     = "parent_directory"
CLASSIFICATION_METHOD_EXTENSION     = "extension"
CLASSIFICATION_METHOD_REFERENCE     = "reference_graph"
CLASSIFICATION_METHOD_LLM           = "llm_fallback"
CLASSIFICATION_METHOD_UNCLASSIFIED  = "unclassified"


# ── Rule data: filename → layer (rule 2 part 1) ─────────────────────────────
#
# Exact filename matches. Order doesn't matter — first match by literal
# basename comparison wins. Both .md and bare names are accepted because
# different bots use different conventions (USER.md vs USER).

_FILENAME_LITERAL_LAYERS: dict[str, str] = {
    # Operator-facing reference docs that code doesn't load.
    "README.md": "reference",
    "README.txt": "reference",
    "README": "reference",
    "LICENSE": "reference",
    "LICENSE.md": "reference",
    "LICENSE.txt": "reference",
    "CHANGELOG.md": "reference",
    "CONTRIBUTING.md": "reference",
    "CODE_OF_CONDUCT.md": "reference",
    "SECURITY.md": "reference",
    # Evolve-managed reference docs (scanner generates these).
    "INSTALLED_APPS.md": "reference",
    "RUNTIME_NOTES.md": "reference",
    # Behavior docs — the bot's standing-instruction surfaces.
    # Validation against personal-bot/team-bot-a/team-bot-c/security-bot/evolve confirmed these
    # are the canonical filenames; some bots use uppercase, some don't.
    "AGENTS.md": "behavior_doc",
    "HEARTBEAT.md": "behavior_doc",
    "SOUL.md": "behavior_doc",
    "POD_CONDUCT.md": "behavior_doc",
    "USER.md": "behavior_doc",
    "MAGIC_COMMANDS.md": "behavior_doc",
    "EMAIL_POLICY.md": "behavior_doc",
    "EMAIL_WHITELIST.md": "behavior_doc",
    "SECURITY_PROTOCOLS.md": "behavior_doc",
    "IDENTITY.md": "behavior_doc",
    "MEMORY.md": "behavior_doc",
    "TOOLS.md": "behavior_doc",
    "COST_OPS.md": "behavior_doc",
    "HEALTH.md": "behavior_doc",
    # Manifest + schema files (contract layer).
    "manifest.json": "contract",
    "schema.json": "contract",
    "schema.yaml": "contract",
    "spec.json": "contract",
}


# ── Rule data: filename glob → layer (rule 2 part 2) ────────────────────────
#
# Pattern matches via fnmatch. Evaluated AFTER literal matches above.

_FILENAME_GLOB_LAYERS: tuple[tuple[str, str], ...] = (
    # Logs by extension or marker.
    ("*.log", "log"),
    ("*.log.*", "log"),    # rotated logs: foo.log.1, foo.log.gz
    # State: locks, pids, caches, checkpoints.
    ("*.lock", "state"),
    ("*.pid", "state"),
    ("*.tmp", "state"),
    ("*.cache", "state"),
    ("*.ckpt", "state"),
    # Daily / dated content (journals, daily notes — common in personal-bot).
    # Pure-numeric-or-date filenames are usually data, not config.
    # Match YYYY-MM-DD.md (journal pattern from personal-bot's memory/ dir).
    ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md", "data"),
)


# ── Rule data: parent-directory → layer (rule 3) ───────────────────────────
#
# Match by the IMMEDIATE parent dir name. ``scripts/util/foo.py`` is
# classified by the ``util`` parent first; if util isn't in this map,
# fall back to ``scripts`` (the grand-parent). The walk happens in
# ``_classify_by_directory_walk``.

_DIRECTORY_LAYERS: dict[str, str] = {
    "scripts":   "code",
    "bin":       "code",
    "src":       "code",
    "lib":       "code",
    "config":    "config",
    "configs":   "config",
    "conf":      "config",
    "settings":  "config",
    "data":      "data",
    "datasets":  "data",
    "journal":   "data",
    "journals":  "data",
    "memory":    "data",
    "observations": "data",
    "logs":      "log",
    "log":       "log",
    "state":     "state",
    "cache":     "state",
    ".cache":    "state",
    "tmp":       "state",
    "content":   "content",
    "articles":  "content",
    "templates": "content",
    "prompts":   "content",
    "drafts":    "content",
    "procedures": "procedure",
    "procedure":  "procedure",
    "runbooks":   "procedure",
    "docs":      "reference",
    "doc":       "reference",
    "documentation": "reference",
    "manifests": "contract",
    # Scanner-internal — files here are framework state, never app payload.
    ".openclaw": "state",
}


# ── Rule data: extension → layer (rule 4) ───────────────────────────────────
#
# Default mapping when filename + directory rules don't fire. Most are
# definitive; ``.json`` is the exception — defaults to config but rule 5
# may demote to data if no code reads it.

_EXTENSION_LAYERS: dict[str, str] = {
    # Code (scripts the app runs).
    ".py":   "code",
    ".sh":   "code",
    ".bash": "code",
    ".zsh":  "code",
    ".js":   "code",
    ".mjs":  "code",
    ".cjs":  "code",
    ".ts":   "code",
    ".tsx":  "code",
    ".rb":   "code",
    ".go":   "code",
    ".rs":   "code",
    ".pl":   "code",
    # Structured config the code reads.
    ".json": "config",  # default; rule 5 may demote to data
    ".yaml": "config",
    ".yml":  "config",
    ".toml": "config",
    ".ini":  "config",
    ".cfg":  "config",
    ".env":  "config",
    # Append-only data and tabular formats — not config.
    ".jsonl": "data",
    ".ndjson": "data",
    ".csv":  "data",
    ".tsv":  "data",
    ".parquet": "data",
    ".sqlite": "data",
    ".sqlite3": "data",
    ".db":   "data",
    # Logs (also caught by filename glob *.log; this fires when the
    # extension alone is .log without a glob match).
    ".log":  "log",
    # Reference / content / behavior — these three share .md / .txt
    # extensions, so the extension rule alone defaults to "reference"
    # (the safest). Filename + directory rules above promote to
    # behavior_doc or content when they fire.
    ".md":   "reference",
    ".markdown": "reference",
    ".txt":  "reference",
    ".rst":  "reference",
    ".html": "reference",
    ".htm":  "reference",
}


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass
class ClassificationResult:
    """One classification decision.

    ``layer`` is ``None`` only when every rule struck out and ``use_llm``
    was False — the caller decides whether to leave the entry's layer
    untouched or queue it for the next LLM pass.

    ``confidence`` is a coarse signal for downstream filters:

      high   — exact filename / directory / extension match
      medium — extension default that rule 5 didn't refine
      low    — fell to LLM fallback

    ``evidence`` is a short human-readable rationale used in scan logs and
    the trail. Format: ``"<method>: <detail>"``, e.g.
    ``"parent_directory: scripts/"`` or ``"filename_pattern: HEARTBEAT.md"``.
    """
    layer: str | None
    method: str
    confidence: str
    evidence: str


# ── The classifier ──────────────────────────────────────────────────────────


def classify(
    path: str,
    *,
    volatile_paths: list[dict] | None = None,
    code_file_contents: dict[str, str] | None = None,
    use_llm: bool = False,
) -> ClassificationResult:
    """Run the rules-first cascade against ``path``.

    Args:
        path: File path relative to the workspace (e.g.
            ``"scripts/check-daily-cost.py"``). Absolute paths work too —
            we operate on the filename and directory components.
        volatile_paths: Manifest's ``volatile_paths[]`` entries (rule 1).
            Each dict has at minimum ``{glob, layer}``. Empty list / None
            means rule 1 doesn't fire.
        code_file_contents: Map of code-layer-file-path → file content
            for the reference-graph check (rule 5). When None or empty,
            rule 5 is skipped — the caller decides whether to read these
            contents (often it's prohibitive on large workspaces).
        use_llm: If True, fall through to rule 6 (LLM) when rules 1-5 all
            struck out. Off by default in PR 3 — the hook for PR 4+.

    Returns:
        A ClassificationResult. ``layer=None`` only when rules 1-5 didn't
        fire AND ``use_llm=False``.
    """
    # Rule 1: volatile_paths glob match. Wins immediately.
    if volatile_paths:
        for vp in volatile_paths:
            if not isinstance(vp, dict):
                continue
            glob = vp.get("glob") or ""
            layer = vp.get("layer") or ""
            if not glob or layer not in VALID_LAYERS:
                continue
            if fnmatch.fnmatch(path, glob):
                return ClassificationResult(
                    layer=layer,
                    method=CLASSIFICATION_METHOD_VOLATILE_PATH,
                    confidence="high",
                    evidence=f"volatile_path: {glob!r}",
                )

    basename = os.path.basename(path)

    # Rule 2a: literal filename match.
    if basename in _FILENAME_LITERAL_LAYERS:
        return ClassificationResult(
            layer=_FILENAME_LITERAL_LAYERS[basename],
            method=CLASSIFICATION_METHOD_FILENAME,
            confidence="high",
            evidence=f"filename_pattern: {basename}",
        )

    # Rule 2b: filename glob match.
    for pattern, layer in _FILENAME_GLOB_LAYERS:
        if fnmatch.fnmatch(basename, pattern):
            return ClassificationResult(
                layer=layer,
                method=CLASSIFICATION_METHOD_FILENAME,
                confidence="high",
                evidence=f"filename_pattern: {pattern}",
            )

    # Rule 3: parent-directory walk. Try the immediate parent first;
    # if it doesn't match, walk up until we find one in our map. Stops
    # at the workspace root (no leading slash component) so we don't
    # accidentally fire on system paths.
    dir_match = _classify_by_directory_walk(path)
    if dir_match is not None:
        layer, dir_name = dir_match
        return ClassificationResult(
            layer=layer,
            method=CLASSIFICATION_METHOD_DIRECTORY,
            confidence="high",
            evidence=f"parent_directory: {dir_name}/",
        )

    # Rule 4: extension. The default mapping. Tracks confidence as
    # medium so rule 5 can refine it.
    _, ext = os.path.splitext(basename.lower())
    if ext in _EXTENSION_LAYERS:
        ext_layer = _EXTENSION_LAYERS[ext]
        # Rule 5: reference-graph refinement of the .json default.
        # If no code file references this path, demote .json/.yaml/
        # .toml from config to data (append-only / lookup tables).
        if ext_layer == "config" and code_file_contents is not None:
            if not _is_referenced_by_code(path, basename, code_file_contents):
                return ClassificationResult(
                    layer="data",
                    method=CLASSIFICATION_METHOD_REFERENCE,
                    confidence="medium",
                    evidence=f"extension: {ext} (demoted to data — no code reference)",
                )
        # Rule 5: reference-graph promotion of other extensions to
        # content (the app reads it as payload, e.g. articles/*.md
        # if scripts read articles/<topic>.md).
        if (ext_layer == "reference"
                and code_file_contents
                and _is_referenced_by_code(path, basename, code_file_contents)):
            return ClassificationResult(
                layer="content",
                method=CLASSIFICATION_METHOD_REFERENCE,
                confidence="medium",
                evidence=f"extension: {ext} (promoted to content — code reads it)",
            )
        return ClassificationResult(
            layer=ext_layer,
            method=CLASSIFICATION_METHOD_EXTENSION,
            confidence="medium",
            evidence=f"extension: {ext}",
        )

    # Rule 6: LLM fallback. Off in PR 3 — the hook only.
    if use_llm:
        # When PR 4+ wires this up, it'll dispatch a tier-1 LLM call to
        # classify the residual. For now, we return unclassified with the
        # llm method so the caller can distinguish "we tried but couldn't"
        # from "we never tried."
        return ClassificationResult(
            layer=None,
            method=CLASSIFICATION_METHOD_LLM,
            confidence="low",
            evidence="llm_fallback: not wired in PR 3",
        )

    return ClassificationResult(
        layer=None,
        method=CLASSIFICATION_METHOD_UNCLASSIFIED,
        confidence="low",
        evidence="no rule fired",
    )


def _classify_by_directory_walk(path: str) -> tuple[str, str] | None:
    """Walk parent directories from immediate to root, returning the
    first one in ``_DIRECTORY_LAYERS``. Returns ``(layer, dir_name)`` or
    None.

    Stops at the workspace root — the empty string (split of leading
    component). Leading slashes are stripped, so ``/Users/x/scripts/foo.py``
    becomes ``Users/x/scripts/foo.py`` for the walk.
    """
    norm = path.lstrip("/").lstrip("./")
    parts = norm.split("/")
    if not parts:
        return None
    # Walk parents from immediate to root.
    for i in range(len(parts) - 2, -1, -1):
        dir_name = parts[i]
        if dir_name in _DIRECTORY_LAYERS:
            return (_DIRECTORY_LAYERS[dir_name], dir_name)
    return None


def _is_referenced_by_code(
    path: str,
    basename: str,
    code_file_contents: dict[str, str],
) -> bool:
    """True if ``path`` or ``basename`` appears as a substring in any
    code-file content. Cheap proxy for "is this file actually loaded by
    the app's code."

    Matches both the full path and the basename — a script might say
    ``read_text("data/journal.json")`` (full) or ``read_text("journal.json")``
    after a ``chdir``. We accept either.
    """
    for code_path, content in code_file_contents.items():
        if not content:
            continue
        if path in content or basename in content:
            return True
    return False


# ── Manifest-level apply ────────────────────────────────────────────────────


def apply_to_manifest(
    manifest: dict,
    *,
    code_file_contents: dict[str, str] | None = None,
    use_llm: bool = False,
) -> list[tuple[str, str | None, str]]:
    """Re-classify every files[*] entry on a manifest dict in place.

    Spec §3.1 + §7.1: "PR 3's classifier runs fresh on every file
    regardless of current layer value; the legacy value is logged in the
    trail but not used."

    When the new classification differs from the entry's current
    ``layer``, the prior value is captured as ``_legacy_layer`` (only if
    not already set — preserves the PR 1 migration's capture of the
    original ``"script"`` → ``"code"`` rename).

    Always sets ``classification_method`` on the entry so the trail can
    attribute the decision to the rule that fired.

    Args:
        manifest: The manifest as a dict (top-level keys ``files``,
            ``volatile_paths``).
        code_file_contents: Optional, for the reference-graph rule.
        use_llm: Pass-through to ``classify``.

    Returns:
        A list of (path, prior_layer, new_layer) tuples for every entry
        whose layer changed. Callers use this to decide whether to log a
        trail entry (no change → no log).
    """
    changes: list[tuple[str, str | None, str]] = []
    volatile_paths = manifest.get("volatile_paths") or []
    files = manifest.get("files") or []
    if not isinstance(files, list):
        return changes
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = (entry.get("path") or "").lstrip("/")
        if not path:
            continue
        result = classify(
            path,
            volatile_paths=volatile_paths,
            code_file_contents=code_file_contents,
            use_llm=use_llm,
        )
        # Stamp method always — even when the classifier struck out,
        # so the trail can show what was tried.
        entry["classification_method"] = result.method
        if result.layer is None:
            continue   # leave entry's layer unchanged
        prior_layer = entry.get("layer")
        if prior_layer == result.layer:
            continue   # no change to record
        # Capture prior value as legacy IFF this is the first override.
        # The PR 1 migration already captures "script" → "code" with
        # _legacy_layer = "script"; don't overwrite that record.
        if prior_layer and "_legacy_layer" not in entry:
            entry["_legacy_layer"] = prior_layer
        entry["layer"] = result.layer
        changes.append((path, prior_layer, result.layer))
    return changes
