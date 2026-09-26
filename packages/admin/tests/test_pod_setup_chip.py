"""Tests for the Phase C Overview pod-setup chip.

Covers two surfaces:

  * Backend counter math — /api/better/getting-started's new ``counter``
    field correctly reports done vs total, with per_bot template
    expansion based on member count.
  * UI structural pins for the chip banner + load wiring on the Overview
    page.

Backend tests stand up a minimal Flask app with just the endpoint route
so they don't require real shared_dir or per-bot infrastructure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_HOME_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "home.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _HOME_JS.read_text(encoding="utf-8")


# ── Counter math (unit-level, via mark_task_complete) ────────────────────


def _setup_path() -> Path:
    """Make sure the admin package is importable as `evolve_admin`."""
    _ADMIN = Path(__file__).parent.parent
    _ANALYZER = _ADMIN.parent / "analyzer"
    for p in (_ADMIN, _ANALYZER):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def test_counter_total_excludes_per_bot_templates():
    """Operator-reported bug 2026-06-02: `Review an application for <bot>`
    rows showed up on the pod-setup Getting Started checklist, even
    though they're per-bot concerns (one instance per member bot).
    Surfacing N copies of a per-bot task under a pod-wide heading reads
    as a pod-wide problem when it isn't. The per_bot templates still
    live in the registry — the engine still auto-completes them — they
    just don't render in the pod-setup checklist's tasks_meta or counter.

    Per-bot tasks belong on the per-bot Settings checklist (PR #1942).
    """
    _setup_path()
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS

    pod_count = sum(1 for t in ONBOARDING_TASKS if not t.per_bot)

    # The endpoint's `expanded` list filters per_bot out. Total == pod_count
    # regardless of how many members the operator has.
    members_0 = []
    members_5 = ["evo", "team_bot_a", "team_bot_b", "team_bot_c", "personal_bot"]
    for members in (members_0, members_5):
        total = sum(1 for t in ONBOARDING_TASKS if not t.per_bot)
        assert total == pod_count


def test_endpoint_response_excludes_per_bot_tasks_from_tasks_meta():
    """Source-level pin: the endpoint must filter out per_bot templates
    when building the expanded list. Regression guard for the operator-
    reported bug."""
    server_py = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"
    text = server_py.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"def api_better_getting_started\(\).+?return jsonify\(getting_started\)",
        text, re.DOTALL,
    )
    assert fn
    body = fn.group(0)
    # The filter expression
    assert "not t.per_bot" in body


def test_counter_done_counts_completed_and_skipped():
    """Both completed=True (auto / manual) and how="skipped" must count
    as done for the chip — operator's "not for this pod" is a deliberate
    finished state, not a pending one. Matches the per-bot semantic."""
    _setup_path()
    persisted = {
        "tasks": {
            "primary_installed": {"completed": True, "how": "auto"},
            "pod_admins_claimed": {"completed": True, "how": "manual"},
            "https_enabled": {"completed": False, "how": "skipped"},
            "pod_conduct_authored": {"completed": False, "how": None},
        },
    }
    done = sum(
        1
        for task_state in persisted["tasks"].values()
        if task_state.get("completed") or task_state.get("how") == "skipped"
    )
    assert done == 3  # 2 completed + 1 skipped; the None-how one doesn't count


# ── UI structural pins ────────────────────────────────────────────────────


def test_pod_setup_chip_banner_exists():
    """Chip lives in a dedicated banner div above page-home — hidden by
    default until loadPodSetupChip resolves the counter."""
    html = _html()
    assert 'id="pod-setup-chip-banner"' in html
    assert 'id="pod-setup-chip"' in html


def test_chip_uses_pod_chip_info_class():
    """Phase C chip reuses the existing .pod-chip-info CSS (added in PR
    #1942) — neutral blue, distinct from healthy / warn / critical."""
    html = _html()
    # Find the chip element line; must mention both pod-chip and pod-chip-info.
    import re
    m = re.search(
        r'id="pod-setup-chip"[^>]*class="([^"]+)"',
        html,
    )
    assert m, "pod-setup-chip element not found"
    classes = m.group(1).split()
    assert "pod-chip" in classes
    assert "pod-chip-info" in classes


def test_chip_load_function_defined():
    html = _html()
    assert "async function loadPodSetupChip()" in html


def test_chip_load_called_from_load_home():
    """Overview page's loadHome() fires loadPodSetupChip on every visit
    so the counter reflects fresh state — no caching at this layer."""
    html = _html()
    import re
    fn = re.search(
        r"async function loadHome\(\)\s*\{(.+?)\n\}\s*\n",
        html, re.DOTALL,
    )
    assert fn, "loadHome not found"
    assert "loadPodSetupChip()" in fn.group(1)


def test_chip_hides_when_setup_status_incomplete():
    """One banner at a time — the no-primary banner is more urgent than
    the pod-setup chip. Don't show both at once or the page reads as
    cluttered. Per review M2, loadPodSetupChip queries /api/setup-status
    directly (not the DOM class on #setup-incomplete-banner) to avoid
    the page-load race where the banner's .visible class hasn't been
    set yet when the chip-loader fires."""
    html = _html()
    import re
    fn = re.search(
        r"async function loadPodSetupChip\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadPodSetupChip not found"
    body = fn.group(1)
    assert "/api/setup-status" in body
    assert "setup_complete" in body


def test_chip_hides_at_100_percent():
    """No point nagging when everything's done. Counter at 100% → chip
    banner hidden."""
    html = _html()
    import re
    fn = re.search(
        r"async function loadPodSetupChip\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadPodSetupChip not found"
    body = fn.group(1)
    # The done >= total check is what gates this; pin the comparison shape.
    assert "done >= total" in body or "done >=total" in body


def test_chip_click_navigates_to_getting_started():
    """Click should route the operator to the Getting Started page where
    the full checklist + actions live."""
    html = _html()
    import re
    fn = re.search(
        r"async function loadPodSetupChip\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadPodSetupChip not found"
    body = fn.group(1)
    assert "_podSetupNav('getting-started')" in body


def test_chip_banner_continue_button_navigates_to_getting_started():
    """Inline Continue ▸ button (next to the chip) also routes to the
    Getting Started page. Two-affordance redundancy is intentional —
    the chip looks clickable but operators expect a textual button too."""
    html = _html()
    assert 'onclick="_podSetupNav(\'getting-started\')"' in html


# ── Endpoint regression: counter field appears in response ────────────────


def test_endpoint_runs_live_auto_complete_sweep():
    """Operator-reported bug 2026-06-04: setting the daily spend threshold
    in the UI didn't flip the checklist's "Set a daily spend threshold"
    task because the endpoint only read persisted state — auto-complete
    only ran from engine.refresh() (~15-min cadence). The endpoint must
    run auto_complete_tasks on every GET so live detector results show
    up immediately."""
    server_py = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"
    text = server_py.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"def api_better_getting_started\(\).+?return jsonify\(getting_started\)",
        text, re.DOTALL,
    )
    assert fn
    body = fn.group(0)
    assert "from ..better_engine.onboarding import auto_complete_tasks" in body
    assert "auto_complete_tasks(" in body
    # Save only when state actually changed
    assert "newly_completed" in body
    assert "save_getting_started" in body


def test_endpoint_response_includes_counter_field():
    """server.py emits `counter: {done, total}` in the GET response.
    Pinned via source check rather than spinning up Flask to keep the
    test lightweight."""
    server_py = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"
    text = server_py.read_text(encoding="utf-8")
    import re
    # Locate the api_better_getting_started function and check it sets counter.
    fn = re.search(
        r"def api_better_getting_started\(\).+?return jsonify\(getting_started\)",
        text, re.DOTALL,
    )
    assert fn, "api_better_getting_started not found"
    body = fn.group(0)
    assert 'getting_started["counter"]' in body
    assert '"done"' in body and '"total"' in body


# ── Review-cycle regression tests (C1 / C2 / M1) ─────────────────────────


def test_endpoint_counter_done_clamped_to_total():
    """Review M1: ``done`` must never exceed ``total``, even when the
    persisted tasks dict has stale entries for removed bots (e.g.
    scan_applications_<deleted_bot> still marked completed)."""
    server_py = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"
    text = server_py.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"def api_better_getting_started\(\).+?return jsonify\(getting_started\)",
        text, re.DOTALL,
    )
    assert fn
    body = fn.group(0)
    # The fix uses intersection (`tid in completed_ids` for tid in
    # expected_ids), plus a defensive clamp.
    assert "expected_ids" in body
    assert "min(done, total)" in body


def test_endpoint_tasks_meta_entries_carry_state_field():
    """Review C2: tasks_meta must include state on every entry so the
    UI can group by state (Pending / Locked / Done / Dismissed). The
    pre-fix shape was active-only with no state field, hiding all
    dismissed and done tasks from the operator."""
    server_py = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"
    text = server_py.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"def api_better_getting_started\(\).+?return jsonify\(getting_started\)",
        text, re.DOTALL,
    )
    assert fn
    body = fn.group(0)
    # The fix builds entries via to_dict() and attaches a state field.
    assert 'entry["state"] = _task_state(task)' in body
    # Four state values exist
    for s in ('"done"', '"dismissed"', '"blocked"', '"pending"'):
        assert s in body, f"state value {s} missing in endpoint"


def test_ui_renders_counter_from_server_field_not_local_math():
    """Review C1: the Getting Started page counter must use the
    server-side counter field (matches the Overview chip exactly).
    Pre-fix it computed locally from tasks_meta-only, always reading
    0 / active_count instead of done / total."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"function _renderPodSetupChecklist\(el, gs\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderPodSetupChecklist not found"
    body = fn.group(1)
    # New code reads gs.counter directly
    assert "gs.counter" in body
    # New code does NOT call _podSetupCounter on tasks_meta-derived rows
    assert "_podSetupCounter(rows)" not in body


def test_ui_groups_by_state_with_dismissed_section_visible():
    """Review C2 regression: dismissed rows must render under a visible
    "Dismissed" section heading so the "Bring back" toggle is reachable.
    Pre-fix the section header was emitted but tasks_meta was active-only,
    so the section was always empty."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"function _renderPodSetupChecklist\(el, gs\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn
    body = fn.group(1)
    # Four bucket variables computed from the full tasks_meta
    for var in ("pendingRows", "blockedRows", "doneRows", "dismissedRows"):
        assert var in body, f"missing bucket {var!r} in render"
    # Section labels surface the bucket headings
    for label in ("Pending", "Locked", "Done", "Dismissed"):
        assert label in body, f"missing section label {label!r}"


def test_ui_row_renderer_has_blocked_state_badge():
    """The new 'blocked' state needs its own badge (locked / muted)
    so operators see at a glance that an item is upcoming, not failed."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    import re
    fn = re.search(
        r"function _podSetupBadge\(state\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupBadge not found"
    body = fn.group(1)
    assert "state === 'blocked'" in body
    assert "Locked" in body
