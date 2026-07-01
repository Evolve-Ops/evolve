"""Tests for tools/publish_sanitize (the publish-time dev-apparatus sanitizer).

PUBLIC-2 (docs/spec-public-2026-06-18.md §5): a few load-bearing dev-apparatus files
keep real reference-deployment detail in the PRIVATE repo and are sanitized only in the
PUBLIC OUTPUT. These tests prove the sanitizer is not vacuous:

  * every target's sanitized output is reserved-token-clean (per the guard's OWN
    scanner — so this test, like the sanitizer, spells no reserved token itself);
  * the sanitizer leaves the PRIVATE working tree byte-for-byte untouched;
  * non-target paths pass through unchanged;
  * the transform is idempotent;
  * the specific role placeholders land where expected (pins the RESERVED_TOKENS
    index mapping so an upstream reorder fails here, not silently in the output);
  * fail-closed: an unmapped reserved token in a target raises rather than leaking.

Run with:
  cd tools && python3 -m pytest test_publish_sanitize.py -v
"""
from __future__ import annotations

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS.parent


def _load(name: str, filename: str):
    loader = SourceFileLoader(name, str(_TOOLS / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


san = _load("publish_sanitize", "publish_sanitize.py")
scrub = san._scrub()


def _violations(text: str) -> list[str]:
    """Reserved-token hits per the guard's own per-line scanner (no literals here)."""
    hits: list[str] = []
    for line in text.splitlines():
        hits.extend(scrub._violations_in(line))
    return hits


# --------------------------------------------------------------------------
# Cleanliness — the core invariant
# --------------------------------------------------------------------------
def test_every_target_exists_and_sanitizes_clean():
    targets = san.target_paths()
    assert targets, "sanitizer must declare at least one target"
    for rel in targets:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"declared target {rel} does not exist"
        private_text = path.read_text(encoding="utf-8")
        # Each target currently DOES carry reserved tokens in the private repo —
        # that is exactly why it is a publish-time target. (If this ever fails, the
        # file was cleaned in-repo and can leave the target set.)
        assert _violations(private_text), (
            f"{rel} carries no reserved tokens privately — it no longer needs a "
            f"publish-time transform; drop it from publish_sanitize._TARGETS."
        )
        public_text = san.sanitize_public_text(rel, private_text)
        assert _violations(public_text) == [], (
            f"{rel} still trips the reserved-token guard after sanitization"
        )


def test_private_working_tree_is_untouched():
    """Sanitizing returns new strings; it must never mutate the files on disk."""
    for rel in san.target_paths():
        path = _REPO_ROOT / rel
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        san.sanitize_public_text(rel, path.read_text(encoding="utf-8"))
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert before == after, f"sanitizing {rel} mutated the private file on disk"


def test_non_target_passes_through_unchanged():
    sample = "arbitrary content with no transform applied\nssh someone@somewhere\n"
    assert san.sanitize_public_text("packages/analyzer/foo.py", sample) == sample
    assert san.is_target("packages/analyzer/foo.py") is False


def test_idempotent():
    for rel in san.target_paths():
        private_text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        once = san.sanitize_public_text(rel, private_text)
        twice = san.sanitize_public_text(rel, once)
        assert once == twice, f"sanitizing {rel} is not idempotent"


# --------------------------------------------------------------------------
# Global org-URL rewrite — applies to EVERY shipped file (runbook Phase 3)
# --------------------------------------------------------------------------
def test_global_origin_url_rewritten_on_non_target():
    """A non-target file linking to the private dev origin has the org segment
    rewritten to the public org — dead-link hygiene, not a secret scrub."""
    sample = "See https://github.com/evolve-ops/evolve/issues/new and cjalden/evolve.\n"
    out = san.sanitize_public_text("docs/some-public-doc.md", sample)
    assert "github.com/evolve-ops/evolve/issues/new" in out
    # Only the github.com/<org> URL form is rewritten; bare prose is left as-is.
    assert out.endswith("and cjalden/evolve.\n")


def test_global_origin_url_rewrite_is_idempotent():
    once = san.sanitize_public_text("x.md", "github.com/evolve-ops/evolve\n")
    twice = san.sanitize_public_text("x.md", once)
    assert once == twice == "github.com/evolve-ops/evolve\n"


def test_global_rewrite_not_applied_to_targets():
    """Targets get their curated transform only — NOT the blunt org-URL sweep.

    The cutover runbook intentionally references the private cjalden origin in
    verification steps (Phase 8: "Expect ... cjalden ... NOT evolve-ops"); a
    blunt rewrite would corrupt those. cjalden is not a reserved token, so the
    sanitized runbook is still scrub-clean with its origin refs intact."""
    rel = "docs/runbook-public-cutover.md"
    private_text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    assert "cjalden" in private_text  # fixture precondition
    out = san.sanitize_public_text(rel, private_text)
    assert "cjalden" in out, "runbook's intentional origin refs must survive"
    assert _violations(out) == []


# --------------------------------------------------------------------------
# Placeholder placement — pins the RESERVED_TOKENS index mapping
# --------------------------------------------------------------------------
def test_settings_json_ssh_target_and_paths_genericized():
    rel = ".claude/settings.json"
    out = san.sanitize_public_text(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8"))
    # The ssh permission-rule target becomes the role form...
    assert "pod-admin-user@deploy-host" in out
    # ...and the machine paths become a generic dev home.
    assert "/Users/dev/" in out
    assert _violations(out) == []


def test_claude_md_admin_account_and_host_genericized():
    rel = "CLAUDE.md"
    out = san.sanitize_public_text(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert "pod-admin-user" in out          # admin account role placeholder
    assert "deploy-host" in out             # host-alias role placeholder
    assert _violations(out) == []


def test_mailmap_public_is_minimal_and_clean():
    rel = ".mailmap"
    out = san.sanitize_public_text(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert "curated, squashed release commits" in out
    assert "no per-commit identity remapping is required" in out
    assert _violations(out) == []


def test_placeholder_naming_keeps_convention_drops_allowlist_internals():
    rel = "docs/PLACEHOLDER_NAMING.md"
    private_text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    out = san.sanitize_public_text(rel, private_text)
    # Convention content is retained...
    assert "Placeholder Naming Convention" in out
    # ...and the private-CI allowlist-internals section is gone.
    assert "## Allowlisted paths" not in out
    assert _violations(out) == []


# --------------------------------------------------------------------------
# Fail-closed
# --------------------------------------------------------------------------
def test_fail_closed_on_unmapped_reserved_token():
    """If a target's output still trips the guard, sanitize_public_text must raise.

    We synthesize a 'dirty' output by registering a temporary passthrough target so the
    fail-closed assertion runs over content the guard rejects — without this test file
    itself spelling a reserved token. We pull one straight from the guard's list.
    """
    a_token = scrub.RESERVED_TOKENS[0]
    dirty = f"a line mentioning {a_token} verbatim\n"
    # Sanity: the guard does flag it.
    assert _violations(dirty)
    san._TARGETS["__test_passthrough__"] = lambda t: t
    try:
        with pytest.raises(ValueError):
            san.sanitize_public_text("__test_passthrough__", dirty)
    finally:
        del san._TARGETS["__test_passthrough__"]
