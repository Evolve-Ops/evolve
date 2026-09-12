"""Pin the Distribute-Key error-guidance affordances into index.html.

Three operator-impactful affordances landed alongside the post-#2366
shared-key model:

  1. classifyDistributeKeyError + renderDistributeKeyGuidanceCard —
     the per-bot github_status row gets an inline guidance card for
     every recognised failure pattern (422 key-in-use, 401, 403, 404,
     skipped:unparseable_url). Pre-fix the operator saw a red ✗ with
     a raw HTTP error and no fix hint.

  2. Run-push-test button + backupRunPushTest() — opt-in probe that
     runs git ls-remote per bot. Surfaces auth/access failures that
     user-key registration silently allows (e.g. team-bot whose repo
     lives in an org where the PAT user can't push).

  3. onboardConfirmReuseFromBanner + onboardScrollToRow — the wizard's
     "Unresolved collisions" banner now exposes clickable [Reuse this
     repo] and [Rename] affordances instead of plain-text instructions.

These tests are JS-grep-based — they don't run the bundle. They guard
against silent refactor regressions (a single edit dropping a wired
identifier reads as "still working" in the diff but breaks the live
UI). Same pattern as test_backup_diagnostic_expand_pinned.py.
"""
from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "index.html"
_BACKUP_JS = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "static" / "js" / "pages" / "backup.js"
_ONBOARD_MODAL_JS = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "static" / "js" / "pages" / "onboard-modal.js"
# classifyDistributeKeyError lives in pages/backup.js (Phase 3h); the
# collision banner + scroll-to-row + reuse-from-banner handlers moved
# to pages/onboard-modal.js (Phase 3ae). Concat all three.
_TEXT = _INDEX_HTML.read_text() + "\n" + _BACKUP_JS.read_text() + "\n" + _ONBOARD_MODAL_JS.read_text()


# ── 1. Distribute-Key error classifier + guidance card ────────────────────

def test_classify_distribute_key_error_exists() -> None:
    """The classifier must be defined — backupDistributeKey() calls it
    inline. If it disappears the call site dies with ReferenceError
    and the per-bot grid renders raw HTTP errors only.
    """
    assert "function classifyDistributeKeyError(" in _TEXT


def test_classify_covers_key_already_in_use() -> None:
    """The 422 key-in-use case is the dominant operator-facing failure
    when one user-key tries to claim a pubkey already bound as a
    per-repo deploy key elsewhere. The classifier must recognise it
    by status code + message phrase so the path can change between
    /repos/keys and /user/keys without breaking the match.
    """
    start = _TEXT.find("function classifyDistributeKeyError(")
    end = _TEXT.find("\nfunction ", start + 1)
    body = _TEXT[start:end]
    assert "key_already_in_use" in body
    assert "key is already in use" in body  # phrase match
    # Deep link to the GitHub keys page must be present so the operator
    # can act without leaving the row.
    assert "github.com/settings/keys" in body


def test_classify_covers_auth_status_codes() -> None:
    """401 (bad PAT), 403 (org policy / scope), 404 (repo missing or
    invisible) each need a dedicated cause so the operator gets the
    right fix step (token vs scope vs URL) instead of one generic
    "registration failed" message.
    """
    start = _TEXT.find("function classifyDistributeKeyError(")
    end = _TEXT.find("\nfunction ", start + 1)
    body = _TEXT[start:end]
    assert "pat_unauthorized" in body
    assert "pat_forbidden" in body
    assert "repo_not_visible" in body
    assert "github.com/settings/tokens" in body


def test_classify_covers_unparseable_url() -> None:
    """skipped:unparseable_url previously rendered as an orange ⚠ with
    no fix hint. The classifier must produce a card so the operator
    knows to check the bot's backupRepoUrl format.
    """
    start = _TEXT.find("function classifyDistributeKeyError(")
    end = _TEXT.find("\nfunction ", start + 1)
    body = _TEXT[start:end]
    assert "unparseable_url" in body
    assert "git@github.com" in body  # expected format hint


def test_render_distribute_key_guidance_card_exists() -> None:
    """The renderer must exist — classifyDistributeKeyError returns the
    structured cause, this turns it into HTML. The two are paired and
    used together inside the per-bot grid loop.
    """
    assert "function renderDistributeKeyGuidanceCard(" in _TEXT


def test_distribute_key_grid_invokes_classifier() -> None:
    """The per-bot grid renderer inside backupDistributeKey must call
    the classifier so each failure row gets its guidance card. Without
    this wiring the helpers are dead code.
    """
    start = _TEXT.find("async function backupDistributeKey(")
    end = _TEXT.find("\nasync function ", start + 1)
    if end == -1:
        end = _TEXT.find("\nfunction ", start + 1)
    body = _TEXT[start:end]
    assert "classifyDistributeKeyError(" in body
    assert "renderDistributeKeyGuidanceCard(" in body


# ── 2. Run-push-test button + handler ─────────────────────────────────────

def test_push_test_button_exists() -> None:
    """The "Run push test" button must be wired alongside the
    Distribute Key button on the Backup → Cloud page. The id is
    stable so loadBackupConfig's re-render doesn't lose the handler.
    """
    assert 'id="backup-pushtest-btn"' in _TEXT
    assert 'onclick="backupRunPushTest()"' in _TEXT


def test_push_test_handler_exists() -> None:
    """backupRunPushTest() must POST to the push-test endpoint and
    render per-bot results. The endpoint path is the public contract
    with server.py — keep them aligned.
    """
    assert "async function backupRunPushTest(" in _TEXT
    assert "/api/backup/cloud/keys/push-test" in _TEXT


def test_push_test_renders_stderr_block() -> None:
    """Failed rows must surface stderr verbatim so the operator can
    read GitHub's actual auth error (e.g. "ERROR: Permission to org/X
    denied to user."). Truncating to a one-line chip loses the
    diagnostic value of the probe.
    """
    start = _TEXT.find("async function backupRunPushTest(")
    end = _TEXT.find("\nasync function ", start + 1)
    if end == -1:
        end = _TEXT.find("\n// ", start + 1)
    body = _TEXT[start:end]
    assert "stderr" in body
    assert "<pre" in body  # raw-error display block


# ── 3. Actionable wizard collision banner ─────────────────────────────────

def test_collision_banner_has_clickable_reuse() -> None:
    """The "Unresolved collisions" banner must offer a clickable
    [Reuse this repo] affordance, not the plain-text "set [Reuse]"
    pre-#2366 wording. The handler flips reuse_confirmed inline so
    the operator doesn't have to scroll back to the bot row.
    """
    # Banner phrase must include the clickable reuse handler call.
    assert "onboardConfirmReuseFromBanner(" in _TEXT
    # The pre-fix plain-text phrase must be gone from the inside-template
    # banner-string. Scope check to the banner rendering site so the
    # mention in the explanatory comment block above the helpers doesn't
    # trip the assertion.
    start = _TEXT.find("if (r && r.unresolved && Array.isArray(r.unresolved)")
    end = _TEXT.find("} else {", start)
    banner_block = _TEXT[start:end]
    assert "set [Reuse] or rename." not in banner_block


def test_collision_banner_has_scroll_to_row() -> None:
    """[Rename] in the banner must scroll back to the bot row so the
    operator can edit the repo_name input in place. Without scrolling,
    the operator has to hunt through a potentially long bot list.
    """
    assert "function onboardScrollToRow(" in _TEXT
    # The bot row anchor that onboardScrollToRow targets must exist
    # in _onboardRenderBotsList. The id pattern must match.
    assert 'id="onboard-row-${escHtml(b)}"' in _TEXT


def test_confirm_reuse_from_banner_flips_state() -> None:
    """The reuse handler must set reuse_confirmed=true on perBot state
    and re-render — otherwise the bot's row keeps showing unchecked
    and the submit button stays disabled.
    """
    start = _TEXT.find("function onboardConfirmReuseFromBanner(")
    end = _TEXT.find("\nfunction ", start + 1)
    body = _TEXT[start:end]
    assert "reuse_confirmed" in body
    assert "_onboardRenderBotsList(" in body
    assert "_onboardUpdateSubmitState(" in body
