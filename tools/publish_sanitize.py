#!/usr/bin/env python3
"""publish_sanitize — publish-time sanitization of the load-bearing dev apparatus.

PD-3 (internal/spec-public-2026-06-18.md §5): Evolve's dev apparatus ships PUBLIC but
SANITIZED — it is a differentiator ("Evolve is built by this agentic SDLC"), not
hidden. Most of the dev apparatus (the META skill bodies, procedure docs, hooks)
carries no reference-deployment detail and is sanitized IN THE PRIVATE REPO. A
handful of files cannot be: they are operationally LOAD-BEARING and must keep their
real values in the dev repo, so they are sanitized at PUBLISH TIME — rewritten in the
PUBLIC OUTPUT tree only, never in the private working tree.

The load-bearing files (why each cannot be edited in place):

  * ``.claude/settings.json`` — the Bash permission-rule patterns must match the
    operator's real ssh target and machine paths VERBATIM, or the local Claude Code
    permission gate is bypassed / broken.
  * ``CLAUDE.md`` — Claude auto-loads it every session and uses its ssh / admin-account
    detail to operate the dev environment.
  * ``.mailmap`` — normalizes real commit author/committer metadata for the PRIVATE
    repo's git display. (The fresh public repo is published as curated, squashed
    commits under a single identity, so it needs no remapping — see below.)
  * ``docs/PLACEHOLDER_NAMING.md`` — its "Allowlisted paths" section documents the
    private-CI scrub allowlist internals (lockfile false-positive substrings, a
    one-shot ops script), which are reference-deployment specifics, not public content.

This module is the content half of the public-output scrub-honesty story (PUBLIC-2).
The GATE half is PUBLIC-3's: the manifest denylist and the public-output exemption set
inside ``tools/publish-public``. This module makes the files above token-clean in
the public output so PUBLIC-3 can drop them from that exemption set; the dry-run
``SCRUB: PASS`` then becomes fully honest for them.

Reconciliation invariant (2026-07-02 forward-sync v1): a path is either a sanitize
target here (sanitized-and-shipped) or on the manifest denylist (withheld) — NEVER
both. ``internal/runbook-public-cutover.md`` was a target until the denylist grew
``internal/runbook-*``; now that it never ships, it left the target set. The invariant is
pinned by a test in ``tools/test_publish_public.py``.

Design constraint — this module must itself be scrub-clean. It feeds the reserved-token
guard (``packages/admin/tests/test_public_launch_scrub.py``), and that guard scans every
tracked file. So this module carries NO reserved-token literal: it derives the tokens
from the guard's own ``RESERVED_TOKENS`` (imported, referenced by index), and verifies
its own output with the guard's own ``_violations_in``. It names only role placeholders
(all of which are scrub-clean) and the non-reserved host word it genericizes.

The transform is fail-closed: ``sanitize_public_text`` asserts the output of any target
contains zero reserved tokens (per the guard's scanner) and raises if not — so a future
edit that introduces an unmapped reserved token blocks the publish loudly rather than
leaking it.

Spec: internal/spec-public-2026-06-18.md §5 (PD-3).
"""
from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

# tools/publish_sanitize.py -> parents[1] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]

# The reserved-token guard is the single source of truth for the token list and the
# per-line scanner. We reuse both so this transform and the guard cannot drift apart.
_SCRUB_SRC = REPO_ROOT / "packages" / "admin" / "tests" / "test_public_launch_scrub.py"

_SCRUB_MOD: ModuleType | None = None


def _scrub() -> ModuleType:
    """Lazily load the reserved-token guard module (it lives under tests/)."""
    global _SCRUB_MOD
    if _SCRUB_MOD is None:
        loader = SourceFileLoader("public_launch_scrub", str(_SCRUB_SRC))
        spec = importlib.util.spec_from_loader("public_launch_scrub", loader)
        assert spec is not None  # spec_from_loader with a real loader never None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _SCRUB_MOD = mod
    return _SCRUB_MOD


# --------------------------------------------------------------------------
# Reserved-token -> role-placeholder mapping (no token literals in this file)
# --------------------------------------------------------------------------
# Placeholders for the specific reserved tokens that actually occur in the
# regex-rewritten targets, keyed by their INDEX in the guard's RESERVED_TOKENS
# tuple. We key by index (not by spelling the token) so this module stays
# scrub-clean. The indices are pinned by tools/test_publish_sanitize.py, which
# asserts the end-to-end output — an upstream reorder of RESERVED_TOKENS fails
# that test loudly rather than silently mis-mapping.
#
# index -> (role placeholder)        # role described, token never spelled
_NAMED_PLACEHOLDER_BY_INDEX = {
    7: "pod-admin-user",   # reference deploy-host admin macOS account
    9: "dev",              # maintainer's laptop / dev account
    10: "maintainer",      # primary commit-author identity (.mailmap)
    1: "automation",       # automation-bot commit identity (.mailmap)
}

# Any other reserved token that drifts into a target maps to this clean, obviously
# generic placeholder. It should never be hit in practice (the targets above carry
# only the four roles named); the fail-closed check guarantees nothing leaks even if
# the generic path is taken.
_GENERIC_PLACEHOLDER = "redacted-name"

# Non-reserved but reference-deployment-specific: the deploy host is referred to by a
# short personal SSH-alias word throughout the load-bearing docs. It is NOT a reserved
# token (so spelling it here does not trip the guard), but it is operator-specific, so
# the public output genericizes it to a role hostname.
_HOST_ALIAS_RE = re.compile(r"\bmini\b", re.IGNORECASE)
_HOST_PLACEHOLDER = "deploy-host"

# --------------------------------------------------------------------------
# Global publish-time rewrite (applied to EVERY shipped file, not just the
# load-bearing targets below)
# --------------------------------------------------------------------------
# The private dev origin is github.com/evolve-ops/evolve. Public docs/code that
# link back to it would be broken links in the public repo (the dev repo is
# private) and a needless origin-name reference. Runbook Phase 3 (URL sweep)
# rewrites the org segment to the public org. ``cjalden`` is deliberately NOT a
# reserved token (an anonymized GitHub handle, see
# test_public_launch_scrub.RESERVED_TOKENS), so this is link hygiene, not a
# secret scrub — but a public repo should never ship dead links to its own
# private origin. Only the ``github.com/<org>`` URL segment is rewritten;
# bare-prose ``cjalden`` mentions (workspace-path examples, anonymized fixtures)
# are left untouched so explanatory sentences about the private origin stay
# truthful.
_ORIGIN_ORG_URL_RE = re.compile(r"github\.com/cjalden\b")
_PUBLIC_ORG_URL = "github.com/evolve-ops"


def _global_public_rewrites(text: str) -> str:
    """Publish-time rewrites applied to every shipped text file (org URL sweep)."""
    return _ORIGIN_ORG_URL_RE.sub(_PUBLIC_ORG_URL, text)


def _reserved_token_re() -> re.Pattern[str]:
    """Word-boundary, case-insensitive alternation over the guard's RESERVED_TOKENS.

    Built from the imported tuple so no token is spelled in this file.
    """
    tokens = _scrub().RESERVED_TOKENS
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b",
        re.IGNORECASE,
    )


def _placeholder_for(matched: str) -> str:
    """Map a matched reserved token to its role placeholder (index-keyed)."""
    tokens = _scrub().RESERVED_TOKENS
    low = matched.lower()
    for idx, placeholder in _NAMED_PLACEHOLDER_BY_INDEX.items():
        if idx < len(tokens) and low == tokens[idx].lower():
            return placeholder
    return _GENERIC_PLACEHOLDER


def _apply_replacements(text: str) -> str:
    """Replace reserved tokens with role placeholders, then genericize the host alias.

    Reserved tokens go first so a combined ``<admin>@<host>`` form has its admin part
    rewritten before the host part (e.g. the ssh target becomes
    ``pod-admin-user@deploy-host``).
    """
    text = _reserved_token_re().sub(lambda m: _placeholder_for(m.group(0)), text)
    text = _HOST_ALIAS_RE.sub(_HOST_PLACEHOLDER, text)
    return text


# --------------------------------------------------------------------------
# Curated public versions (files whose public content differs in KIND)
# --------------------------------------------------------------------------
# A regex over the private content would leave these incoherent (the private
# .mailmap documents filter-repo cutover options that PD-2 superseded; the private
# PLACEHOLDER_NAMING.md's allowlist table is private-CI internals), so the public
# version is produced directly / by section-trim instead.

_PUBLIC_MAILMAP = """\
# .mailmap — git author/committer identity normalization
#
# Evolve's public repository is published as curated, squashed release
# snapshots, all under a single project maintainer identity, so no
# per-commit identity remapping is required here.
#
# If a future contributor identity ever needs normalizing for `git shortlog
# --use-mailmap` / GitHub display, add it in the git-shortlog(1) form:
#   Canonical Name <canonical@email> <commit@email>
"""

# The public PLACEHOLDER_NAMING.md keeps the convention + the role-placeholder mapping
# (the differentiator content) and drops the "## Allowlisted paths" section onward,
# which documents private-CI scrub internals (lockfile false-positive substrings, a
# one-shot ops script) — the only place reserved tokens appear in that doc.
_PLACEHOLDER_NAMING_TRIM_MARKER = "## Allowlisted paths"


def _public_placeholder_naming(private_text: str) -> str:
    """Trim PLACEHOLDER_NAMING.md to its public-safe prefix."""
    idx = private_text.find(_PLACEHOLDER_NAMING_TRIM_MARKER)
    body = private_text if idx == -1 else private_text[:idx].rstrip() + "\n"
    # Defensive: scrub any residual reference-deployment token from the kept prefix
    # (today it is already clean; this keeps it clean under future edits).
    return _apply_replacements(body)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
# repo-relative POSIX path -> transform function (private_text -> public_text)
_TARGETS: dict[str, Callable[[str], str]] = {
    "CLAUDE.md": _apply_replacements,
    ".claude/settings.json": _apply_replacements,
    ".mailmap": lambda _private: _PUBLIC_MAILMAP,
    "docs/PLACEHOLDER_NAMING.md": _public_placeholder_naming,
}


def is_target(rel_posix: str) -> bool:
    """True if ``rel_posix`` is sanitized at publish time by this module."""
    return rel_posix in _TARGETS


def target_paths() -> tuple[str, ...]:
    """The repo-relative paths this module sanitizes (sorted, stable)."""
    return tuple(sorted(_TARGETS))


def _assert_clean(rel_posix: str, text: str) -> None:
    """Fail-closed: raise if the sanitized output still trips the reserved-token guard."""
    scrub = _scrub()
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits = scrub._violations_in(line)
        if hits:
            raise ValueError(
                f"publish_sanitize: sanitized output for {rel_posix} still contains "
                f"reserved token(s) {hits} at line {lineno}: {line.strip()[:110]!r}. "
                f"The transform is incomplete — add a rule before publishing."
            )


def sanitize_public_text(rel_posix: str, text: str) -> str:
    """Return the public-output content for ``rel_posix``.

    For the load-bearing dev-apparatus targets this returns the sanitized content;
    for every other path it returns ``text`` unchanged. The private working tree is
    never read or written here — callers pass the private content in and write the
    returned content to the PUBLIC output only.

    Fail-closed: raises ``ValueError`` if a target's sanitized output still trips the
    reserved-token guard.
    """
    fn = _TARGETS.get(rel_posix)
    if fn is None:
        # Non-target file: only the global org-URL sweep applies (no-op unless
        # the file links back to the private dev origin).
        return _global_public_rewrites(text)
    # Target files get their curated transform ONLY — NOT the blunt org-URL
    # sweep. A target's curated content may legitimately describe the private
    # cjalden origin (e.g. explaining the private→public publish model); a
    # blunt rewrite would corrupt such prose into self-contradiction. cjalden
    # is not a reserved token, so leaving it in a sanitized target is
    # scrub-clean and contextually correct.
    out = fn(text)
    _assert_clean(rel_posix, out)
    return out
