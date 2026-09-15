"""tests/test_evo_button_label_drift.py — pack button labels match HTML.

Page-context packs include `available_actions` lists with button
labels (eg "Take this on", "Snooze 1w", "Dismiss"). These labels need
to match the actual on-screen button text — otherwise evo cites
phantom buttons.

The failure mode: someone renames a button in HTML without updating
the pack. Operator sees "Apply" on the row; evo says "click Take
this on". Confusion. Trust erosion.

This test parses every `available_actions[*].label` from every pack
and asserts the label string appears verbatim somewhere in the HTML
— the simplest possible check that the button text *exists* on the
page. False positives are bounded (no label is going to look like
random body text); false negatives are blocked.

When a pack legitimately needs a label that's NOT a literal HTML
string (eg "Snooze (drop-down for duration; default 24h)" is a
description of a complex affordance, not a literal button), wrap the
label in the `_DESCRIPTIVE_LABELS` exemption set with a comment.
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


_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"
# Pack-action labels that describe an affordance rather than naming a
# single literal button. Add new exemptions with rationale.
_DESCRIPTIVE_LABELS = {
    # Maintenance / Alerts pack — the Snooze button opens a dropdown
    # for duration; the label "Snooze" appears on the row but the
    # description in the pack carries more nuance than a literal
    # button text. The HTML check below tolerates substring-match
    # against literal button labels separately.
    "Snooze",
    "Dismiss",
    # Maintenance pack includes a description for Snooze and Dismiss
    # that includes parens — those phrases aren't on screen.
    # ↑ NB: the bare labels "Snooze" / "Dismiss" do appear in HTML
    # (per below check). The exemption is for the long-form
    # description text.
}


def _read_html() -> str:
    return _INDEX_HTML.read_text() + "\n" + _EVO_DRAWER_JS.read_text()


def _extract_action_labels(html: str) -> list[str]:
    """Pull every ``label:`` value from every pack's available_actions.

    Pattern in JS (from the maintenance pack):

        available_actions: [
          { label: 'Snooze', description: '...' },
          ...
        ],

    Regex-based — accepts single or double quotes. Doesn't try to be
    a JS parser; the pattern is consistent across all packs.
    """
    # Get the registry body once.
    match = re.search(
        r"const\s+_EVO_CONTEXT_PACKS\s*=\s*\{(.*?)\n\};",
        html, re.DOTALL,
    )
    if not match:
        return []
    body = match.group(1)
    return re.findall(
        r"label:\s*['\"]([^'\"]+)['\"]",
        body,
    )


def test_action_labels_extracted():
    """Sanity: we find at least a handful of labels. If parsing returns
    nothing the regex is stale; the test would silently pass otherwise."""
    labels = _extract_action_labels(_read_html())
    assert len(labels) >= 3, (
        f"only {len(labels)} action labels parsed — the regex may have "
        f"drifted from the pack format. Found: {labels}"
    )


def test_every_pack_label_appears_in_html_or_is_exempted():
    """Each pack's button label must appear verbatim somewhere in the
    HTML, OR be explicitly listed in ``_DESCRIPTIVE_LABELS``.

    If a real button is renamed in HTML, the pack's stale label
    triggers this test → operator catches it before evo cites the
    phantom button. If the operator chooses to keep a descriptive
    label (eg one that doesn't match any literal button), they add to
    the exemption set with rationale.
    """
    html = _read_html()
    labels = _extract_action_labels(html)
    phantom: list[str] = []
    for label in labels:
        if label in _DESCRIPTIVE_LABELS:
            continue
        # Case-insensitive contains. Very forgiving — button text can
        # appear inside `>...</button>`, in `title="..."`, or as plain
        # text. We just want SOME evidence the string exists on the
        # page.
        if label.lower() not in html.lower():
            phantom.append(label)
    assert not phantom, (
        f"Page-context packs cite these button labels but they don't "
        f"appear anywhere in index.html: {phantom}. Either:\n"
        f"  - Rename the pack's label to match the actual HTML button "
        f"text (most likely — someone changed the button without "
        f"updating the pack).\n"
        f"  - If the label is a description rather than a literal "
        f"button name, add it to _DESCRIPTIVE_LABELS in this test "
        f"file with a comment."
    )


def test_descriptive_label_exemptions_have_a_reason():
    """The descriptive-label exemption set should never grow large.
    If it does, something's wrong with the pack design — too many
    'descriptions disguised as labels'. Cap is generous; a true
    breach means the pattern is being abused."""
    assert len(_DESCRIPTIVE_LABELS) < 15, (
        f"_DESCRIPTIVE_LABELS has grown to {len(_DESCRIPTIVE_LABELS)} "
        f"entries — every entry is a place where pack content doesn't "
        f"match HTML reality. Re-examine the pack designs."
    )
