"""App-identity ownership policy — the ONE shared predicate for "may an
application legitimately own this workspace-relative path?"

Background (the bug this foundation closes): a scanner run stamped ``_evolve``
provenance markers onto ~1,000 audit-telemetry files on each of two bots
under ``evolve/audit_outbox/_ingested/.../rec-*.json``, plus
OC-standard files like ``AGENTS.md``. None of those can EVER be owned by a bot
application — yet ownability was decided ad-hoc in several call sites (the
marker-writer had no check at all). This module is the single predicate they
must all share so the answer is identical everywhere.

Spec: docs/spec-app-identity-ledger-2026-06-27.md (§4.A, §6); sub-spec of
docs/spec-apps-meta-2026-06-13.md.

Pure Python — no I/O, no LLM, no filesystem access. It classifies a STRING.

No divergent copy: the never-ownable classes are sourced from ``scanner``'s
existing canonical artifacts (the discovery-phase ``evolve/`` exclusion, the
platform-written glob list + its ``**`` matcher, and the OC identity/system
basename set), imported here rather than re-listed. The only set defined
locally is the OC task-substrate filenames, which scanner does not yet name.

This module is INTENTIONALLY just the shared predicate. Wiring it into
``stamp_per_file`` / ``reflect`` / ``file_index`` is out of scope here — those
land in the ``apps-stamp-writer-policy`` / ``apps-reflect-thin-reader`` /
``apps-recon-ledger`` chips, which consume ``can_app_own``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Reuse scanner's canonical never-ownable artifacts — do NOT re-list them here.
#   * EVOLVE_PLATFORM_TREES        — the discovery-phase exclusion (scanner.py
#     ~L250, applied in the inventory walk ~L1643). Component-based, so it
#     covers the WHOLE ``evolve/`` telemetry tree: audit_outbox, audit_inbox,
#     audits, skill_audits, provider_audits, investigations, signals, the
#     evolve/manifests outputs — every present and future subdir — plus
#     ``evolve-backup``. A superset of the subdirs the spec enumerates.
#   * _is_platform_written_path    — the glob matcher (_path_matches_any, the
#     same ``**``-aware matcher scanner uses; see scanner.py:373 for the
#     embedded-``/`` quirk it handles) over PLATFORM_WRITTEN_FILE_PATTERNS.
#     Catches platform telemetry NOT under ``evolve/`` (memory/turns-*.jsonl)
#     and the manifests scan-status / _history outputs.
#   * _is_oc_identity_system_file  — OpenClaw identity / standing-instruction
#     files (AGENTS.md, HEARTBEAT.md, SOUL.md, USER.md, MEMORY.md,
#     POD_CONDUCT.md, BOOTSTRAP.md, IDENTITY.md, …). The canonical reserved
#     basename set (_OC_IDENTITY_SYSTEM_BASENAMES) is a superset of the spec's
#     list; reusing it keeps us strictly more conservative with zero copy.
#   * _clean_evidence_path         — strips the ``<tag>:`` prefix, ``#anchor``
#     suffix, and surrounding slashes so the spec's path forms normalize the
#     same way scanner normalizes evidence.
from .scanner import (
    EVOLVE_PLATFORM_TREES,
    _clean_evidence_path,
    _is_oc_identity_system_file,
    _is_platform_written_path,
)

# Directory NAMES (matched as any path component) an application may never own.
# EVOLVE_PLATFORM_TREES ({"evolve", "evolve-backup"}) is the canonical
# discovery-phase exclusion; the manifests store + the OC/git control dirs are
# the remaining never-ownable trees the spec calls out. ``manifests`` as ANY
# component mirrors scanner._is_manifest_store_path exactly.
_NEVER_OWNABLE_DIR_COMPONENTS: frozenset[str] = frozenset(
    EVOLVE_PLATFORM_TREES | {"manifests", ".openclaw", ".git"}
)

# OC task-substrate standard files. Not OpenClaw *identity* files (so not in
# scanner's reserved basename set), but pod-standard task tracking the runtime
# manages — they showed up as false orphans on ``atlas``. Matched on basename,
# case-insensitively. NOTE: ``tasks.json`` (the substrate store) is reserved;
# an app's own ``tasks.py`` orchestration script is NOT (different basename).
_OC_TASK_SUBSTRATE_FILENAMES: frozenset[str] = frozenset(
    {"tasks.md", "tags.md", "tasks.json"}
)

# ── Generated-runtime / secret file classes ───────────────────────────────────
# These are files an app's *runtime* writes — cryptographic material, append-only
# event streams, generated indices — never authored source the app "owns" as a
# deliverable. Several cannot even carry a text provenance marker (binary salt,
# append-only JSONL), so a missing-marker "Fix=stamp" on them would corrupt the
# file. They surfaced as wrongly-claimed paths on ``atlas`` (``atlas/.capture-salt``,
# ``atlas/member-hash-salt.bin``, ``atlas/capture-log.jsonl``,
# ``atlas/research-log.jsonl``, ``archive/index.json``).
#
# Each rule is HIGH-PRECISION — anchored so it cannot match an ordinary source
# filename. The over-removal guard is load-bearing here: a rule that over-matched
# a legit app file (a real ``catalog.json``, a ``salt_recipe.py``) would strip a
# genuine claim. The tests pin the precision boundary on both sides.

# Cryptographic salt / key material extensions. A salt/secret is never source.
_SECRET_EXTS: frozenset[str] = frozenset({".bin", ".key", ".pem", ".dat", ".secret"})

# Append-only runtime log streams: ``*-log.jsonl`` / ``*.log.json`` and friends.
# ``log`` must be a DISTINCT token (path-start or after ``- _ .``) immediately
# before a ``.json``/``.jsonl`` extension, so ``catalog.json`` / ``changelog.jsonl``
# / ``dialog.json`` (where ``log`` is mid-word) are NOT matched.
_RUNTIME_LOG_RE = re.compile(r"(?:^|[-_.])log\.jsonl?$")


def _is_secret_material(name_l: str) -> bool:
    """True for cryptographic salt/secret files (``.capture-salt``,
    ``member-hash-salt.bin``). Anchored on a ``salt`` token PLUS a secret shape
    (dotfile or binary/key extension) so a source file like ``salt_recipe.py`` or
    a data file like ``basalt.json`` is NOT caught."""
    if "salt" not in name_l:
        return False
    return name_l.startswith(".") or Path(name_l).suffix in _SECRET_EXTS


def can_app_own(rel_path: str, *, name: str | None = None) -> bool:
    """True if an application MAY own this workspace-relative path. False for
    paths that are platform telemetry, scanner state, or OpenClaw-standard files.

    ``rel_path`` is a workspace-relative path (the same shape the scanner emits
    as evidence — a leading ``<tag>:`` prefix and a trailing ``#anchor`` are
    tolerated and stripped). ``name`` is the candidate application's name, kept
    for forward compatibility with name-scoped rules; it does not affect the
    answer today.
    """
    cleaned = _clean_evidence_path(rel_path)
    if not cleaned:
        # Empty / whitespace / tag-only input is not an ownable file.
        return False

    parts = [p for p in cleaned.split("/") if p and p != "."]

    # 1. Never-ownable directory trees — the whole evolve/ telemetry tree,
    #    evolve-backup, the manifests store, and the OC/git control dirs.
    if any(seg in _NEVER_OWNABLE_DIR_COMPONENTS for seg in parts):
        return False

    # 2. Platform-written glob paths NOT caught by (1): memory/turns-*.jsonl,
    #    manifests/.scan-status.json, manifests/_history/**, etc. Shares
    #    scanner's pattern list + matcher, so behavior is identical.
    if _is_platform_written_path(cleaned):
        return False

    # 3. OpenClaw identity / standing-instruction files at any depth
    #    (AGENTS.md, SOUL.md, MEMORY.md, POD_CONDUCT.md, …).
    if _is_oc_identity_system_file(cleaned):
        return False

    name_l = Path(cleaned).name.lower()

    # 4. OC task-substrate standard files (TASKS.md, TAGS.md, tasks.json).
    if name_l in _OC_TASK_SUBSTRATE_FILENAMES:
        return False

    # 5. Cryptographic salt / key material (secrets) — never authored source,
    #    and a binary file cannot carry a text marker (a Fix=stamp would corrupt
    #    it). e.g. atlas/.capture-salt, atlas/member-hash-salt.bin.
    if _is_secret_material(name_l):
        return False

    # 6. Append-only runtime log streams (JSON/JSONL event logs the runtime
    #    appends to) — runtime state, not source. e.g. atlas/capture-log.jsonl.
    if _RUNTIME_LOG_RE.search(name_l):
        return False

    # 7. Generated archive index — the app-maintained telemetry index of an
    #    ``archive/`` tree (archive/index.json). Narrow: only ``index.json`` whose
    #    IMMEDIATE parent component is ``archive`` (a real ``archive/article.md``
    #    or a non-archive ``data/index.json`` stays ownable).
    if name_l == "index.json" and len(parts) >= 2 and parts[-2] == "archive":
        return False

    return True
