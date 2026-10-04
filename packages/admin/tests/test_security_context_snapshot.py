"""tests/test_security_context_snapshot.py — guard that the Security
page's context snapshot doesn't get clobbered between subtab data
producers.

The 2026-05-20 hotfix: a fresh-session retry of "accept all configs
as baseline" on the Backups subtab showed evo reporting
``drifted_bots=0`` while the page clearly rendered ⚠ Config drift on
multiple bots. Trace:

  1. Backups subtab's ``loadBackupConfig`` writes
     ``_evoContextSnapshots.security`` with ``{...prev, backup_drift,
     active_subtab}`` (spread to preserve siblings).
  2. The audit panel polls periodically and was writing
     ``_evoContextSnapshots.security = {cached_at, per_bot}`` —
     **no spread** — clobbering ``backup_drift`` to undefined.
  3. The context-pack builder reads ``snap.backup_drift || []`` and
     reports zero drift.

Fix: spread ``_prev`` into the audit-poll write too. This test pins
that spread so a future refactor can't drop it again.

Pattern lifted from test_evo_drawer_robustness — regex on the HTML
source. Same caveats: it tests SHAPE, not behavior. The runtime is
exercised live on the mini.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_POD_CONFIG_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "pod-config.js"
_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"
_BACKUP_JS = _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "backup.js"


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _BACKUP_JS.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8")


def test_security_snapshot_writers_preserve_siblings(html: str):
    """Every assignment to ``window._evoContextSnapshots.security``
    must spread the prior value, so concurrent writers (audit poll +
    backup-config render) don't clobber each other's fields.

    Without this, the 2026-05-20 'drifted_bots=0' false-negative
    returns: the audit poll fires every few seconds, and whichever
    writer ran most recently wins.
    """
    # Find every assignment site. The pattern matches:
    #   window._evoContextSnapshots.security = {  ... }
    # plus its multi-line body until the closing brace.
    pattern = re.compile(
        r"window\._evoContextSnapshots\.security\s*=\s*\{([\s\S]*?)\n\s*\};",
        re.MULTILINE,
    )
    matches = pattern.findall(html)
    assert matches, (
        "no assignments to window._evoContextSnapshots.security found. "
        "If the snapshot mechanism was refactored, update this test "
        "to match the new shape."
    )
    # Sanity: there should be at least 2 writers (audit poll +
    # backup-config render). If there's just one, the test still
    # passes — but we want to know if a writer disappeared.
    assert len(matches) >= 2, (
        f"only {len(matches)} writer(s) to _evoContextSnapshots.security "
        f"found; expected at least 2 (audit poll + loadBackupConfig). "
        f"If a writer was consolidated, that's fine — but verify the "
        f"remaining writer still preserves sibling fields."
    )
    # Every writer's body must include a spread of the prior value.
    # Common patterns: ``..._prev`` or ``...prev``.
    for i, body in enumerate(matches):
        has_spread = bool(re.search(r"\.\.\.\s*_?prev\b", body))
        assert has_spread, (
            f"_evoContextSnapshots.security assignment #{i+1} doesn't "
            f"spread the prior value. Without the spread, sibling "
            f"fields (e.g. backup_drift from the Backups subtab) get "
            f"clobbered by every periodic writer. Body was:\n{body[:300]}"
        )


def test_security_context_pack_reads_backup_drift(html: str):
    """The 'security' entry in _EVO_CONTEXT_PACKS must read
    ``backup_drift`` out of the snapshot AND surface it under a
    model-facing key — otherwise the Backups subtab's drift list
    never reaches evo's context regardless of whether the snapshot
    is preserved correctly.

    We can't match the full builder body with a regex (nested object
    literals break ``\\{...\\}`` matching), so we widen to a window
    after the 'security' key and check that all three markers are
    present within it."""
    # 'security' appears in two places: _EVO_PAGE_PROMPTS (suggestion
    # list) and _EVO_CONTEXT_PACKS (the context builder we care about).
    # Anchor at the actual ``const _EVO_CONTEXT_PACKS = {`` declaration
    # rather than the name alone — the name also appears in earlier
    # explanatory comments.
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    if packs_start < 0:
        packs_start = html.find("_EVO_CONTEXT_PACKS = {")
    assert packs_start > 0, (
        "could not locate the _EVO_CONTEXT_PACKS declaration. If the "
        "registry was renamed, update this test."
    )
    start = html.find("'security':", packs_start)
    if start < 0:
        start = html.find('"security":', packs_start)
    assert start > 0, (
        "could not locate the 'security' entry inside _EVO_CONTEXT_PACKS. "
        "If the key shape was refactored, update this test."
    )
    # Builder bodies are ~50–100 lines. 5000 chars covers any plausible
    # rewrite while staying inside the same builder.
    window = html[start:start + 5000]
    assert "snap.backup_drift" in window, (
        "the 'security' context-pack builder no longer reads "
        "``snap.backup_drift`` from the snapshot. Without it, the "
        "Backups subtab's drift list never reaches evo's context — "
        "and the 2026-05-20 'drifted_bots=0' false-negative comes back."
    )
    assert (
        "backup_drift_items" in window
        or "drifted_bots" in window
    ), (
        "the 'security' context-pack no longer surfaces backup-drift "
        "under a model-facing key (backup_drift_items / drifted_bots). "
        "The data is being read but not exposed."
    )
