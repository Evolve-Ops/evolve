"""tests/test_bot_creation_seeds_all_content_scan_docs.py — lockstep gate.

content_scan scans a fixed set of files per bot (``scanned_files_per_bot``) and
fires a red ``content_scan_file_disappeared`` alert for any that's missing. The
member bot ``ledger`` red-flagged on exactly this: it was created via ``openclaw
onboard`` (which seeds SOUL/AGENTS/HEARTBEAT/IDENTITY/TOOLS/USER) but MEMORY.md
and README.md — seeded *only* by Evolve's install_bot_docs — never landed, and
nothing caught it.

A warning that fires because the creation process didn't produce a required file
is an Evolve BUG, not per-bot drift. These tests pin the invariant so it can't
regress: the set content_scan requires and the set the creation flow seeds must
stay in lockstep, and a freshly-created member bot's workspace must contain every
content_scan-required doc — even when onboard only wrote its own six.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin import bot_doc_seeding as _bds  # noqa: E402
from evolve_admin.deploy import (  # noqa: E402
    _MEMBER_BOT_DOCS_TEMPLATE_DIR,
    _MEMBER_BOT_DOC_FILES,
    _render_member_bot_doc,
)
from content_scan.default_patterns import default_catalog  # noqa: E402


# Files `openclaw onboard` writes (provisioning.py stage 4). Mirrors the
# observed onboard output; the point of the gap-fill is that the creation flow
# stays correct even if this set shrinks.
_ONBOARD_DOCS = ("SOUL.md", "AGENTS.md", "HEARTBEAT.md", "IDENTITY.md", "TOOLS.md", "USER.md")


# ── Lockstep invariants (pure) ────────────────────────────────────────────────


def test_required_set_is_the_catalog_scope():
    """bot_doc_seeding is the SSOT — it must read the required set straight from
    the content_scan catalog, never a hand-copied list."""
    assert _bds.required_bot_docs() == tuple(
        default_catalog().scope.scanned_files_per_bot
    )


def test_evolve_seeded_docs_are_all_required():
    """Evolve shouldn't ship a rich 'required' template for a file content_scan
    doesn't scan — that's dead work and a sign of drift."""
    required = set(_bds.required_bot_docs())
    extra = set(_bds.EVOLVE_SEEDED_DOCS) - required
    assert not extra, f"EVOLVE_SEEDED_DOCS not in content_scan scope: {extra}"


def test_deploy_alias_tracks_the_ssot():
    """deploy._MEMBER_BOT_DOC_FILES must be the bot_doc_seeding constant, not a
    second hand-maintained tuple that can drift."""
    assert _MEMBER_BOT_DOC_FILES is _bds.EVOLVE_SEEDED_DOCS


def test_seed_plan_covers_every_required_doc():
    """The no-drift core: rich-template set ∪ stub-fill set == required set.
    Add a file to content_scan's scope and it lands in exactly one bucket."""
    covered = set(_bds.EVOLVE_SEEDED_DOCS) | set(_bds.gap_fill_docs())
    assert covered == set(_bds.required_bot_docs())


def test_structurally_checked_docs_are_evolve_owned():
    """A stub can't clear the structural_emptiness check (≥1500 bytes), so every
    structurally-checked required doc MUST be Evolve-owned with a real template.
    If content_scan adds a structural check on, say, USER.md, this fails until a
    rich USER.md template ships (a stub would just trade one red signal for
    another)."""
    structural = _bds.structural_docs()
    assert structural, "expected at least SOUL.md/AGENTS.md to be structural"
    not_owned = structural - set(_bds.EVOLVE_SEEDED_DOCS)
    assert not not_owned, f"structurally-checked docs need a rich template: {not_owned}"


# ── End-to-end: a freshly-created member workspace is complete ────────────────


def _simulate_creation_flow(workspace: Path, onboard_docs, *, bot_id="testbot", role="member"):
    """Reproduce what a real bot creation produces, using the SAME building
    blocks the production code uses (so the test tracks the code, not a copy):

      1. ``openclaw onboard`` writes ``onboard_docs``.
      2. ``install_bot_docs`` ensures every EVOLVE_SEEDED_DOC exists, writing the
         rendered template for any that's absent (operator-edit skip is moot on a
         fresh bot — the relevant case here).
      3. The gap-fill stubs any remaining required doc the flow missed.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    for fname in onboard_docs:
        (workspace / fname).write_text(f"# {fname} (onboard)\n\nseeded by openclaw onboard\n")

    for fname in _bds.EVOLVE_SEEDED_DOCS:
        dst = workspace / fname
        if not dst.exists():
            dst.write_text(_render_member_bot_doc(_MEMBER_BOT_DOCS_TEMPLATE_DIR / fname, bot_id, role))

    present = lambda f: (workspace / f).exists()  # noqa: E731
    for fname, stub in _bds.plan_gap_fill(bot_id, present):
        (workspace / fname).write_text(stub)


def test_fresh_member_workspace_has_every_required_doc():
    """The falsifiable guarantee: after the full creation flow, every
    content_scan-required doc exists and is non-empty (no file_disappeared)."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        _simulate_creation_flow(ws, _ONBOARD_DOCS)

        missing = [f for f in _bds.required_bot_docs() if not (ws / f).exists()]
        assert not missing, f"required docs missing after creation flow: {missing}"
        empty = [f for f in _bds.required_bot_docs() if (ws / f).stat().st_size == 0]
        assert not empty, f"required docs empty after creation flow: {empty}"


def test_onboarded_bot_gets_memory_and_readme():
    """The exact ledger reproduction: a bot onboarded with only the six onboard
    docs (no MEMORY.md/README.md) must come out of the creation flow with both."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        assert "MEMORY.md" not in _ONBOARD_DOCS and "README.md" not in _ONBOARD_DOCS
        _simulate_creation_flow(ws, _ONBOARD_DOCS)
        assert (ws / "MEMORY.md").exists(), "MEMORY.md not seeded — the ledger bug"
        assert (ws / "README.md").exists(), "README.md not seeded — the ledger bug"


def test_gap_fill_repairs_a_skipped_onboard_doc():
    """Safety net: if onboard fails to write an OpenClaw-owned doc, the gap-fill
    stub still keeps content_scan clean (no file_disappeared for that doc)."""
    partial = tuple(d for d in _ONBOARD_DOCS if d != "HEARTBEAT.md")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        _simulate_creation_flow(ws, partial)
        assert (ws / "HEARTBEAT.md").exists(), "gap-fill didn't repair a missing onboard doc"
        # ... and the placeholder names the file so an operator knows to replace it.
        assert "HEARTBEAT" in (ws / "HEARTBEAT.md").read_text()


def test_gap_fill_is_create_only():
    """The gap-fill must never clobber an existing (e.g. onboard-written) doc —
    even a short one. ``plan_gap_fill`` only proposes writes for absent files."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir(parents=True)
        for fname in _bds.gap_fill_docs():
            (ws / fname).write_text("operator content")
        present = lambda f: (ws / f).exists()  # noqa: E731
        assert _bds.plan_gap_fill("testbot", present) == []
