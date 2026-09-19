"""Cross-page modals must live at body level, not inside a .page div.

Regression pin for the 2026-07-28 inert-button bug: the intake-token
modal markup lived inside ``#page-inbox``. The SPA hides inactive pages
with ``.page { display: none }``, and a ``display:none`` ancestor hides
every descendant — ``position:fixed`` included. Net effect: the
Plugins/POD GitHub CTAs (Set up / Manage / **Set override**) called
``_inboxOpenIntakeTokenModal()`` successfully, the modal "opened"
invisibly, and the buttons read as dead on every page except Issues.

Any modal that more than one page opens must therefore sit AFTER the
last ``.page`` div (direct child of <body>). If you add a new
cross-page modal, append its id to ``_CROSS_PAGE_MODAL_IDS``.
"""
from __future__ import annotations

import re
from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "index.html"

# Modals opened from more than one page. Page-private modals may stay
# inside their page div (they can only be opened while it is active).
_CROSS_PAGE_MODAL_IDS = (
    "inbox-intake-token-modal",  # Issues rows + Plugins/POD GitHub CTAs
)


def test_cross_page_modals_after_last_page_div():
    text = _INDEX_HTML.read_text()
    last_page_open = max(m.start() for m in re.finditer(r'<div class="page"', text))
    for modal_id in _CROSS_PAGE_MODAL_IDS:
        pos = text.find(f'id="{modal_id}"')
        assert pos >= 0, f"modal #{modal_id} missing from index.html"
        assert pos > last_page_open, (
            f"modal #{modal_id} sits before the last .page div — if it is "
            f"nested inside a page, `.page {{ display:none }}` makes it "
            f"invisible when opened from any other page (the 2026-07-28 "
            f"inert Set-override bug). Move it to body level."
        )
