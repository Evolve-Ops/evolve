"""tests/test_al_1_4b_area2_identity_reads.py — AL-1.4b, area 2 (evo + wizard).

Brief: internal/build-AL-1.4-app-id-canonical.md §3 (sweep contract), §5
(guardrails), §8 (this area's implementation record). Design:
internal/design-app-spec-and-discovery-2026-08-15.md §3.

AL-1.4b sweeps identity readers onto ``applications.app_identity.resolve_app_id``.
Area 2 sweeps **nothing** — every ``pkg_id`` read in the eight evo/wizard files
is a gallery PACKAGE key or a ForgeJob/QueuedApp field, and two independent
divergences make routing them through the resolver a behavior change rather than
a mechanical swap. Those two divergences are the load-bearing justification for
every ``# identity:`` annotation in this area, so they are pinned here:

  D1  ``app_id`` is an already-occupied field name in the gallery subsystem.
      ``gallery/index.json`` has carried ``app_id`` since #3413, denormalizing
      each package manifest's ``id`` — the app SCRIPT name (``app_task_manager``)
      — not the package key (``p-9bfa1c84``). ``resolve_app_id`` prefers the
      canonical field over the whole legacy chain, so it answers with the script
      name for every builtin package.

  D2  ``pkg_id`` leads the legacy chain, so a chat-forged manifest — which
      carries BOTH an ``id`` slug and a synthetic ``pkg_id`` — resolves to the
      pkg_id, while ``persist_manifest`` deliberately keys the filename on
      ``id``.

If either test starts failing, the collision has been resolved (or has moved)
and the area-2 annotations should be revisited — that is the signal 1.4c needs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications.app_identity import (  # noqa: E402
    is_canonical_app_id,
    normalize_app_id,
    resolve_app_id,
)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_INDEX = _REPO_ROOT / "gallery" / "index.json"

_EVO = _ADMIN_DIR / "evolve_admin" / "evo"

# The eight area-2 files, with the number of grep-gate lines each still carries.
# The gate pattern §3 greps. Kept because the surviving test below uses it to
# ask "does this file still name a legacy id field at all?".
#
# The per-file CENSUS that used to live here — exact grep-hit counts for all
# eight area-2 files — was REMOVED 2026-08-18. It hard-coded line counts and
# #3704 broke it twice in one day by annotating three of the same files
# repo-wide; a ratchet that re-breaks whenever anyone adds a `pkg_id` line
# measures churn, not correctness. What it was really guarding — "every
# matching file carries an identity annotation" — is now enforced repo-wide by
# packages/admin/tests/test_al_1_4b_identity_gate.py, which walks rather than
# lists, so it cannot go stale the same way. (That was #3704's separate
# grep_gate_repo_wide file when this was written; the two gates were merged
# into one on 2026-08-18.)
_GATE = re.compile(
    r"get\(\"(spec_id|instance_id|pkg_id)\"\)|\.(spec_id|instance_id|pkg_id)\b"
)


# ─────────────────────────────────────────────────────────────────────────────
# D1 — the gallery `app_id` field-name collision
# ─────────────────────────────────────────────────────────────────────────────


def _builtin_index() -> list[dict]:
    if not _GALLERY_INDEX.exists():  # pragma: no cover - checkout without gallery
        pytest.skip("gallery/index.json not present in this checkout")
    return json.loads(_GALLERY_INDEX.read_text(encoding="utf-8"))


def test_gallery_index_rows_carry_a_nonconforming_app_id():
    """D1's first half, and the half that is still TRUE.

    Every builtin ``gallery/index.json`` row carries an ``app_id`` of its own
    holding the app SCRIPT name (``app_task_manager``), not the package key —
    a pre-1.4a denormalization of the package manifest's ``id``. That is the
    shape the area-2 annotations exist for, and it is unchanged.

    REWRITTEN 2026-08-18. As originally written this asserted that
    ``resolve_app_id`` DISAGREES with ``pkg_id`` for every row, and it went red
    when #3681 landed. The chip pre-registered the outcome in its own failure
    message — "if this shrank to 0 the collision is gone and the area-2
    annotations should be revisited" — and that is exactly what happened, so
    this file now pins the post-#3681 contract instead. See
    ``test_a_nonconforming_app_id_falls_through_to_the_package_key`` for the
    half that changed.
    """
    entries = _builtin_index()
    assert entries, "gallery index is empty — the divergence cannot be pinned"

    for entry in entries:
        raw = entry.get("app_id")
        pkg_id = entry.get("pkg_id")
        assert isinstance(raw, str) and raw, f"{pkg_id}: row carries no app_id"
        assert str(pkg_id).startswith("p-"), pkg_id
        assert raw != pkg_id, (
            f"{pkg_id}: row's app_id now EQUALS the package key. The "
            "denormalization D1 describes is gone; re-read brief §11 D1 before "
            "trusting the area-2 annotations."
        )
        assert raw.startswith("app_"), (pkg_id, raw)
        assert not is_canonical_app_id(raw), (
            f"{raw!r} is a CONFORMING slug. That is the dangerous direction: "
            "resolve_app_id honors a conforming app_id, so this row would stop "
            "falling through to pkg_id and every gallery reader swapped onto "
            "the resolver would silently re-key. See #3681."
        )


def test_a_nonconforming_app_id_falls_through_to_the_package_key():
    """D1's second half, INVERTED by #3681 — and this is what closes the hazard.

    Because the row's ``app_id`` is not a conforming slug, ``resolve_app_id``
    declines it and falls through the legacy chain to ``pkg_id``. So resolver
    and catalog key now AGREE for every builtin row, and the silent-re-key
    hazard the area-2 annotations were written against is closed at the
    resolver rather than only by convention.

    This test fails in both directions that matter: revert #3681 and the
    resolver starts returning ``app_task_manager`` again; give the index rows
    conforming ``app_id``s and it returns those. Either way a gallery reader
    swapped onto the resolver would re-key, which is the thing worth catching.
    """
    for entry in _builtin_index():
        pkg_id = entry.get("pkg_id")
        resolved = resolve_app_id(entry)
        assert resolved == pkg_id, (
            f"{pkg_id}: resolve_app_id returned {resolved!r}. Post-#3681 a "
            "non-conforming app_id must fall through to the package key; a "
            "divergence here re-opens the D1 re-key hazard."
        )
        # …and the value it returns is one canonical_app_id would accept,
        # which is the property that makes it safe to use as a key at all.
        assert normalize_app_id(resolved) == resolved


def test_a_raw_gallery_package_json_still_resolves_to_its_pkg_id():
    """The contrast that makes D1 a *shape* problem, not a global one: the
    per-package JSON has no top-level ``app_id``, so the resolver agrees with
    ``pkg_id`` there. The divergence is specific to the denormalized index
    record — which is exactly what ``list_gallery_packages`` hands the wizard."""
    pkg_path = _REPO_ROOT / "gallery" / "task-manager" / "p-9bfa1c84.json"
    if not pkg_path.exists():  # pragma: no cover
        pytest.skip("builtin task-manager package not present")
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert "app_id" not in pkg
    assert resolve_app_id(pkg) == pkg["pkg_id"] == "p-9bfa1c84"
    # ...and its `id` IS the string the index republishes as `app_id`.
    assert pkg["id"] == "app_task_manager"


# ─────────────────────────────────────────────────────────────────────────────
# D2 — pkg_id leads the legacy chain, so forged manifests resolve to it
# ─────────────────────────────────────────────────────────────────────────────


def test_a_chat_forged_manifest_resolves_to_pkg_id_not_its_slug():
    """D2. ``build_manifest_from_state`` emits both ``id`` and a synthetic
    ``pkg_id``; the 1.4a chain prefers ``pkg_id``."""
    from evolve_admin.evo.wizard import forge_handlers as fh

    manifest = fh.build_manifest_from_state(
        "botty",
        {"forge_app_name": "Ledger Watcher", "forge_description": "watches"},
        "user-key",
    )
    assert manifest["id"] == "ledger-watcher"
    assert manifest["pkg_id"].startswith("p-")
    assert resolve_app_id(manifest) == manifest["pkg_id"]
    assert resolve_app_id(manifest) != manifest["id"], (
        "resolve_app_id now agrees with the forged manifest's slug — brief "
        "§8 D2 no longer holds; the forge_handlers annotations should be "
        "revisited (and the sweep may finally be safe there)."
    )


def test_persist_manifest_keys_the_filename_on_the_slug(tmp_path: Path):
    """D2, as behavior: the file lands at ``<slug>.json``. Swapping in
    ``resolve_app_id`` would move every chat-forged app to ``p-<8hex>.json``
    and orphan everything that already points at the slug."""
    from evolve_admin.evo.wizard import forge_handlers as fh

    manifest = fh.build_manifest_from_state(
        "botty",
        {"forge_app_name": "Ledger Watcher", "forge_description": "watches"},
        "user-key",
    )
    fh.persist_manifest(tmp_path, manifest)
    written = sorted(p.name for p in (tmp_path / "applications" / "botty").iterdir())
    assert written == ["ledger-watcher.json"]
    assert f"{manifest['pkg_id']}.json" not in written


# ─────────────────────────────────────────────────────────────────────────────
# The one file the repo-wide gate does not really cover
# ─────────────────────────────────────────────────────────────────────────────


def test_prompts_py_identity_reads_are_annotated_in_a_comment():
    """`wizard/prompts.py` used to pass the repo-wide gate for the WRONG REASON.

    #3704's per-file gate asked whether a matching file contained the substring
    ``identity:`` anywhere. `prompts.py` contained it at one place only — inside
    an LLM prompt template, ``f"Bot identity: {bot_id} …"`` — which is an English
    sentence sent to a model, not a classification of anything. Its two real
    ``pkg_id`` reads were unannotated and the gate still passed.

    FIXED UPSTREAM (2026-08-18): the consolidated gate detects reads by AST and
    requires the annotation to NAME the resolver, so a prompt sentence no longer
    counts and both reads are checked in their own block. This test is kept
    anyway — it pins the annotations themselves, which the gate only requires to
    exist, and it is the record of how a per-file text gate fails.

    Measured on origin/main at the time this was written: of the 105 files
    matching the gate's pattern, `prompts.py` was the ONLY one passing on an
    incidental string. The other 16 without a ``#``-prefixed marker carry their
    annotation in a module docstring, which is a real annotation.

    So this is a narrow hole, not a broad one — and annotating the file closes
    it. Tightening the gate's matcher itself is deliberately NOT done here: the
    obvious tightening (require ``identity: see``) fails 12 legitimately
    annotated files that use other wording, and doing it properly means
    distinguishing comments and docstrings from string literals at AST level.
    That belongs in its own change against the gate, not smuggled into this
    one. Recorded in §11 so it is not rediscovered.
    """
    text = (_EVO / "wizard" / "prompts.py").read_text(encoding="utf-8")
    assert _GATE.search(text), (
        "prompts.py no longer names a legacy id field — if the reads were "
        "swept, delete this test with them."
    )
    annotated = [
        ln for ln in text.splitlines()
        if ln.lstrip().startswith("#") and "identity:" in ln
    ]
    assert annotated, (
        "prompts.py has identity reads but no COMMENT-form identity "
        "annotation. The repo-wide gate would still pass on the "
        'f"Bot identity: …" prompt string, so this file is only covered here.'
    )
    assert any("resolve_app_id" in ln for ln in annotated)
