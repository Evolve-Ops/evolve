"""tests/test_users_tier_prefs_ui.py — Users-page per-user tier defaults (UI).

G5 of the spec-user-tier-control 2026-05-26 spec's 2026-08-03 addendum: the
Users page gains a read-only "Per-user tier defaults" section over
``GET /api/admin/bots/<bot_id>/users/tier-prefs``.

The admin SPA has no JS test harness; the established pattern is to pin UI
behaviour by asserting on pages/users.js *source strings*. These pin that:

  1. The per-bot panel renders the section container and the fetch is wired
     (after the by_channel data settles, so the name join has data).
  2. Tier values render via role LABELS (Fast/Standard/Power/Max), never raw
     file strings alone — unknown values fall back to the raw string.
  3. Unjoinable entries stay visible as the raw user_key (never hidden).
  4. The empty state is a short muted line telling users about the
     ``evo tier-default`` chat command — not an empty table.
  5. The surface is READ-ONLY: no write call to the tier-prefs endpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_USERS_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "users.js"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _USERS_JS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Section container + fetch wiring ───────────────────────────────────────

def test_bot_panel_renders_tier_prefs_container(js: str):
    code = _strip_comments(js)
    assert 'users-tier-prefs-${escHtml(botId)}' in code
    assert "Per-user tier defaults" in code


def test_fetch_wired_after_by_channel_settles(js: str):
    """The tier-prefs fetch runs from _usersFetchAndRenderByChannel (both the
    success and failure paths), so the display-name join never races the
    identity fetch."""
    code = _strip_comments(js)
    assert code.count("_usersFetchAndRenderTierPrefs(botId)") >= 2
    assert "/users/tier-prefs`" in code


# ── 2. Role labels, not raw file strings ──────────────────────────────────────

def test_roles_render_with_canonical_labels(js: str):
    code = _strip_comments(js)
    m = re.search(r"_USERS_TIER_LABELS\s*=\s*\{([^}]*)\}", code)
    assert m, "expected a local role-id -> label map"
    body = m.group(1)
    for role_id, label in (("fast", "Fast"), ("standard", "Standard"),
                           ("power", "Power"), ("max", "Max")):
        assert f"{role_id}: '{label}'" in body
    # Unknown role values fall back to the raw string (visible, not blank).
    assert "_USERS_TIER_LABELS[pref.default_role] || pref.default_role" in code


# ── 3. Unjoinable entries stay visible ────────────────────────────────────────

def test_raw_user_key_fallback_when_join_misses(js: str):
    code = _strip_comments(js)
    assert "escHtml(pref.user_key)" in code


# ── 4. Empty state ────────────────────────────────────────────────────────────

def test_empty_state_mentions_the_chat_command(js: str):
    code = _strip_comments(js)
    assert "No per-user tier defaults set" in code
    assert "evo tier-default" in code


# ── 5. Read-only surface ──────────────────────────────────────────────────────

def test_no_write_path_to_tier_prefs(js: str):
    """G5 ships read-only: standing defaults are set from the chat thread
    (evo tier-default; G4's bot tool later), never POSTed from this page."""
    code = _strip_comments(js)
    for line in code.splitlines():
        if "tier-prefs" in line:
            assert "POST" not in line and "PUT" not in line and "DELETE" not in line
