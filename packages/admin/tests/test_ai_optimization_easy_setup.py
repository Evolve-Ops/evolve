"""tests/test_ai_optimization_easy_setup.py — Phase 9b easy-setup wizard (UI).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 6 #2.

The admin SPA has no JS test harness; the established pattern is to pin UI
behaviour by asserting on ai-optimization.js *source strings*. These pin that:

  1. An "Easy setup" button is wired on BOTH the POD-default editor and each
     bot tab (use-defaults AND custom modes).
  2. The wizard modal ranks providers (up/down reorder) sourced from the
     listings' llm_providers (the LLM-capable subset; Addendum 7 item 12) —
     no provider literals.
  3. The flow PREVIEWS the resulting per-tier order (preview=true) before the
     write, and the apply path POSTs without preview (an explicit overwrite).
     The apply path uses NO native window.confirm() — it is a silent no-op in
     the Tauri/wry desktop webview; the modal + Preview + explicit Apply click
     is the confirmation surface.
  4. Both writes route through the single /api/models/easy-setup endpoint —
     pod and bot scope share it; the JS never reorders models itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Easy-setup button on both editors ──────────────────────────────────────

def test_pod_editor_has_easy_setup_button(js: str):
    code = _strip_comments(js)
    assert "_aiOpenEasySetup('pod')" in code


def test_bot_editors_have_easy_setup_button(js: str):
    code = _strip_comments(js)
    # Both the use-defaults header and the custom header wire Easy setup for the
    # bot scope (escaped bot id passed through).
    assert code.count("_aiOpenEasySetup('${escHtml(botId)}')") >= 2


# ── 2. Provider-order ranking sourced from LLM-capable providers ──────────────

def test_provider_order_from_llm_providers_listing(js: str):
    code = _strip_comments(js)
    assert "function _aiOpenEasySetup(" in code
    # Item 12: the wizard ranks the LLM-capable subset (llm_providers), not the
    # raw credentialed set. credentialed_providers remains only as the
    # older-server fallback.
    open_start = code.find("function _aiOpenEasySetup(")
    open_end = code.find("function _aiCloseEasySetup(")
    open_region = code[open_start:open_end]
    assert "llm_providers" in open_region, "wizard must seed from llm_providers"
    # credentialed_providers is the fallback only (still present, but after llm).
    assert open_region.find("llm_providers") < open_region.find("credentialed_providers")
    # Reorder controls exist (up/down) over the ranked provider list.
    assert "function _aiEasyMoveProvider(" in code


def test_no_provider_literals_in_wizard(js: str):
    code = _strip_comments(js)
    # The wizard threads _aiEasyOrder (operator data) only — no bare provider id
    # is hardcoded as LOGIC anywhere in the easy-setup region. The sole allowed
    # mention is the file-wide cosmetic `.replace('anthropic/', '')` display
    # prefix-strip (shortens a model label; not provider logic), so strip that
    # before checking — it must be the ONLY occurrence and never a comparison.
    start = code.find("function _aiOpenEasySetup(")
    end = code.find("function _aiRenderPodEngine(")
    assert start != -1 and end != -1
    region = code[start:end]
    region = region.replace("replace('anthropic/', '')", "")
    region = region.lower()
    for lit in ("anthropic", "openai", "google", "claude"):
        assert lit not in region, f"unexpected provider literal {lit!r} in easy-setup logic"


# ── 3. Preview-before-write, then confirmed overwrite ─────────────────────────

def test_preview_before_write(js: str):
    code = _strip_comments(js)
    assert "function _aiEasyPreviewRun(" in code
    assert "preview: true" in code
    # The preview renders the resulting per-tier model order.
    assert "function _aiEasyPreviewRows(" in code


def test_confirm_does_not_use_native_dialog(js: str):
    # The apply path must NOT gate the write behind a native window.confirm():
    # confirm() is a silent no-op in the Tauri/wry desktop webview (no
    # script-dialog delegate wired), so the button dies on pods reached via the
    # desktop app. The easy-setup modal + Preview + explicit Apply click IS the
    # confirmation surface, so no native dialog is needed (or wanted).
    code = _strip_comments(js)
    assert "function _aiEasyConfirm(" in code
    start = code.find("function _aiEasyConfirm(")
    end = code.find("function _aiRenderPodEngine(")
    region = code[start:end]
    assert "confirm(" not in region


# ── 4. Single shared endpoint; JS never reorders models itself ────────────────

def test_writes_route_through_single_endpoint(js: str):
    code = _strip_comments(js)
    assert "/api/models/easy-setup" in code
    # Both pod and bot scope POST the same endpoint with scope + provider_order.
    assert "provider_order: _aiEasyOrder" in code or "provider_order: order" in code
    assert "scope: _aiEasyScope" in code or "scope," in code


def test_reorder_is_server_side(js: str):
    code = _strip_comments(js)
    # The wizard sends only scope + provider_order; the reordered catalog comes
    # back from the server (it never builds rungs/models in JS).
    start = code.find("function _aiEasyConfirm(")
    end = code.find("function _aiRenderPodEngine(")
    region = code[start:end]
    assert "rungs" not in region or "catalog.rungs" in region  # only reads, never builds


# ── 5. Preview is uniform per-role (the judge special-case died with the
# judge role — design-judge-role-collapse-2026-08-21 §5.4) and display-only.""'"'

def _preview_region(js: str) -> str:
    code = _strip_comments(js)
    start = code.find("function _aiEasyPreviewRows(")
    end = code.find("function _aiEasyPreviewRun(")
    assert start != -1 and end != -1
    return code[start:end]


def test_preview_has_no_judge_special_case(js: str):
    region = _preview_region(js)
    # The judge row special-case died with the judge role — every role
    # renders uniformly from its rung chain.
    assert "=== 'judge'" not in region and '=== "judge"' not in region, \
        "the retired judge special-case must not survive in the preview"
    assert "stdProvider" not in region, \
        "the standard-provider split served only the judge row"


def test_judge_preview_is_display_only(js: str):
    region = _preview_region(js)
    # Display-only transform: it reads the catalog and filters for render; it must
    # not mutate the shared rung models[] (which would corrupt Standard's order).
    for mutator in (".sort(", ".splice(", ".push(", ".reverse(", ".pop("):
        assert mutator not in region, \
            f"preview must not mutate the shared rung config ({mutator})"
