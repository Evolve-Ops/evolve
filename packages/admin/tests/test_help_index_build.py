"""tests/test_help_index_build.py — Index build + parsing + IO."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: a tiny docs/ tree
# ─────────────────────────────────────────────────────────────────────────────


def _seed_docs(root: Path) -> None:
    """Create a representative subset of in-scope and out-of-scope docs."""
    help_dir = root / "help"
    help_dir.mkdir(parents=True, exist_ok=True)

    (help_dir / "cost.md").write_text(
        "# Help: Cost Page\n\n"
        "Spend by model, channel, and source.\n\n"
        "Use the time-range selector to drill in.\n"
    )
    (help_dir / "security.md").write_text(
        "# Help: Security Page\n\n"
        "Audit findings, posture views, and config-health checks.\n\n"
        "## Sub-tabs\n\nAudit, posture, …\n"
    )
    (help_dir / "fenced.md").write_text(
        "# Title\n\n"
        "```\nignored code block\n```\n\n"
        "Real summary lives here, after the fence.\n"
    )

    # Operator docs
    (root / "operator-runbook.md").write_text(
        "# Operator Runbook\n\nRunbook overview line.\n\n## Sections\n"
    )
    (root / "getting-started.md").write_text(
        "# Getting Started\n\nWalkthrough of the first session.\n"
    )

    # Out-of-scope (should NOT appear in index)
    (root / "spec-something.md").write_text("# Spec\n\nIgnore me.\n")
    (root / "archive").mkdir(exist_ok=True)
    (root / "archive" / "old.md").write_text("# Archived\n\nIgnore.\n")


def test_build_index_picks_up_help_and_operator_docs(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")

    ids = {d.doc_id for d in idx.docs}
    # help/*.md — always namespaced with category prefix
    assert "help/cost" in ids
    assert "help/security" in ids
    assert "help/fenced" in ids
    # operator
    assert "operator/operator-runbook" in ids
    assert "operator/getting-started" in ids
    # Out-of-scope must not be there
    assert not any(i.endswith("spec-something") for i in ids)
    assert not any(i.endswith("old") for i in ids)


def test_help_docs_listed_first_in_alpha_order(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")
    help_ids = [d.doc_id for d in idx.docs if d.category == "help"]
    # alphabetical
    assert help_ids == sorted(help_ids)


def test_categories_set(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")
    cats = {d.doc_id: d.category for d in idx.docs}
    assert cats["help/cost"] == "help"
    assert cats["operator/operator-runbook"] == "operator"
    assert cats["operator/getting-started"] == "operator"


def test_doc_ids_are_namespaced_to_avoid_collision(tmp_path):
    """Real-tree case: docs/help/overview.md and docs/overview.md both
    exist. They must produce distinct doc_ids."""
    from evolve_admin.help_index import build

    docs = tmp_path / "docs"
    (docs / "help").mkdir(parents=True)
    (docs / "help" / "overview.md").write_text("# Help overview\n\nHelp page body.\n")
    (docs / "overview.md").write_text("# Operator overview\n\nOperator body.\n")

    idx = build.build_index(docs)
    ids = {d.doc_id for d in idx.docs}
    assert "help/overview" in ids
    assert "operator/overview" in ids
    # Both addressable by their distinct ids
    assert idx.by_id("help/overview").body.startswith("# Help overview")
    assert idx.by_id("operator/overview").body.startswith("# Operator overview")


def test_title_parsed_from_h1(tmp_path):
    from evolve_admin.help_index import build

    (tmp_path / "docs" / "help").mkdir(parents=True)
    (tmp_path / "docs" / "help" / "x.md").write_text(
        "# Help: Cost Page\n\nBody.\n"
    )
    idx = build.build_index(tmp_path / "docs")
    doc = idx.by_id("help/x")
    assert doc is not None
    assert doc.title == "Help: Cost Page"


def test_summary_skips_fenced_blocks(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")
    doc = idx.by_id("help/fenced")
    assert doc is not None
    assert "Real summary lives here" in doc.summary
    assert "ignored code block" not in doc.summary


def test_summary_capped(tmp_path):
    from evolve_admin.help_index import build

    (tmp_path / "docs" / "help").mkdir(parents=True)
    long = "lorem ipsum " * 200
    (tmp_path / "docs" / "help" / "big.md").write_text(
        f"# Big\n\n{long}\n"
    )
    idx = build.build_index(tmp_path / "docs")
    doc = idx.by_id("help/big")
    assert doc is not None
    assert len(doc.summary) <= 200
    assert doc.summary.endswith("…")


def test_sha_stable_across_rebuilds(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    a = build.build_index(tmp_path / "docs").by_id("help/cost")
    b = build.build_index(tmp_path / "docs").by_id("help/cost")
    assert a is not None and b is not None
    assert a.sha == b.sha


def test_write_then_load_round_trips(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")
    out = build.write_index(idx, tmp_path)
    assert out.exists()

    loaded = build.load_index(tmp_path)
    assert loaded is not None
    assert {d.doc_id for d in loaded.docs} == {d.doc_id for d in idx.docs}


def test_load_index_returns_none_when_missing(tmp_path):
    from evolve_admin.help_index import build

    assert build.load_index(tmp_path) is None


def test_load_index_returns_none_on_corrupt_json(tmp_path):
    """A malformed index file shouldn't crash the server — search
    routes return 503 and the bot says 'no index built'."""
    from evolve_admin.help_index import build

    (tmp_path / "help_index.json").write_text("{not valid json")
    assert build.load_index(tmp_path) is None


def test_index_file_mode_is_world_readable(tmp_path):
    from evolve_admin.help_index import build

    _seed_docs(tmp_path / "docs")
    idx = build.build_index(tmp_path / "docs")
    p = build.write_index(idx, tmp_path)
    mode = p.stat().st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_partial_docs_dir_does_not_crash(tmp_path):
    """If only some docs are present (e.g. cleanup work mid-flight),
    we still produce an index with whatever survives."""
    from evolve_admin.help_index import build

    (tmp_path / "docs" / "help").mkdir(parents=True)
    (tmp_path / "docs" / "help" / "cost.md").write_text("# Cost\n\nBody.\n")
    idx = build.build_index(tmp_path / "docs")
    ids = {d.doc_id for d in idx.docs}
    assert ids == {"help/cost"}


def test_out_of_scope_postmortem_and_claude_md_excluded(tmp_path):
    """Spec §4.1: internal/postmortem-*.md and CLAUDE.md are out-of-scope."""
    from evolve_admin.help_index import build

    docs = tmp_path / "docs"
    (docs / "help").mkdir(parents=True)
    (docs / "help" / "cost.md").write_text("# Cost\n\nBody.\n")
    (docs / "postmortem-ssh-wedge-2026-04-25.md").write_text("# Postmortem\n\nIgnore.\n")
    (docs / "CLAUDE.md").write_text("# Dev guide\n\nIgnore.\n")

    idx = build.build_index(docs)
    ids = {d.doc_id for d in idx.docs}
    assert "help/cost" in ids
    assert not any("postmortem" in i for i in ids)
    assert not any(i.endswith("CLAUDE") for i in ids)
