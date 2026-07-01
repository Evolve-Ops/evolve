"""Tests for the auto-responder + undo path (Phase 5 of Issue Inbox).

The auto-responder takes inbound intakes whose triage verdict matches
the operator's policy and applies a GitHub action (close as duplicate,
post clarifying reply, apply labels). Each fired action records an
undo handle with a 24-hour deadline.

Transport is injected so these tests run with a fake transport that
records requests — no real GitHub calls.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


# ─── Fixtures ────────────────────────────────────────────────────────────────


class FakeTransport:
    """Records each request, returns canned responses by URL pattern."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self._responses: dict[tuple[str, str], tuple[int, dict]] = {}
        self._comment_id_seq = 1000

    def respond(self, method: str, url_substring: str, status: int, body: dict):
        self._responses[(method, url_substring)] = (status, body)

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        for (m, sub), resp in self._responses.items():
            if m == method and sub in url:
                return resp
        # Sensible defaults: comment POST returns an auto-incrementing id.
        if method == "POST" and "/comments" in url:
            self._comment_id_seq += 1
            return 201, {"id": self._comment_id_seq, "body": "ok"}
        if method == "PATCH" and "/issues/" in url:
            return 200, {"state": "closed"}
        if method == "POST" and url.endswith("/labels"):
            return 200, [{"name": "ok"}]
        if method == "DELETE":
            return 204, {}
        return 200, {}


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def shared_dir(tmp_path):
    return tmp_path


def _make_inbound(
    *, id_="in-1", urgency="p1", category="bug",
    recommendation="auto_close_duplicate",
    confidence=0.95,
    duplicate_of=("evolve-ops/evolve#42",),
    draft_reply="Could you share the exact command you ran?",
    draft_labels=("bug",),
    number=7,
    auto_action=None,
):
    from evolve_admin.intake.envelope import (
        Intake, TriageRecord, AutoActionRecord,
    )
    ix = Intake(
        id=id_, kind="bug", body=f"inbound body for {id_}",
    )
    ix.state = "filed"
    ix.inbound = True
    ix.promotion.github_issue_url = (
        f"https://github.com/evolve-ops/evolve/issues/{number}"
    )
    ix.promotion.github_issue_number = number
    ix.triage = TriageRecord(
        category=category, urgency=urgency,
        recommendation=recommendation,
        confidence=confidence,
        duplicate_of=list(duplicate_of),
        draft_reply=draft_reply,
        draft_labels=list(draft_labels),
        inbound_author="some-user", inbound_title=f"Title {id_}",
        inbound_body_short="Issue body",
    )
    if auto_action is not None:
        ix.auto_action = auto_action
    return ix


# ─── decide() ────────────────────────────────────────────────────────────────


def test_decide_returns_none_when_policy_disabled():
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound()
    p = AutoResponsePolicy(enabled=False, close_duplicate_enabled=True)
    assert decide(ix, p) is None


def test_decide_returns_none_when_kind_disabled():
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(recommendation="auto_close_duplicate")
    p = AutoResponsePolicy(enabled=True, close_duplicate_enabled=False)
    assert decide(ix, p) is None


def test_decide_returns_close_when_gates_pass():
    """All three gates met → returns the action kind."""
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(
        recommendation="auto_close_duplicate", confidence=0.95,
    )
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.9,
    )
    assert decide(ix, p) == "close_duplicate"


def test_decide_returns_none_when_confidence_below_floor():
    """Floor is load-bearing — must not fire on a borderline verdict."""
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(confidence=0.85)
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.9,
    )
    assert decide(ix, p) is None


def test_decide_close_requires_duplicate_of():
    """Can't close as duplicate without a citation. The classifier
    might recommend it but forget the ref — must still skip."""
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(
        recommendation="auto_close_duplicate", duplicate_of=(),
    )
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.5,
    )
    assert decide(ix, p) is None


def test_decide_reply_requires_draft_reply():
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(
        recommendation="auto_reply_clarifying", draft_reply="   ",
    )
    p = AutoResponsePolicy(
        enabled=True, reply_clarifying_enabled=True,
        reply_clarifying_min_confidence=0.5,
    )
    assert decide(ix, p) is None


def test_decide_skips_already_actioned_intakes():
    """Don't re-fire on an intake we've already touched — undo is the
    only path back."""
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.envelope import AutoActionRecord
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound(auto_action=AutoActionRecord(
        kind="close_duplicate", actor="evo-bot",
    ))
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.5,
    )
    assert decide(ix, p) is None


def test_decide_skips_outbound_intakes():
    """The auto-responder is for inbound only. An operator-filed
    intake on the same repo must never be auto-closed."""
    from evolve_admin.intake.auto_responder import decide
    from evolve_admin.intake.policy import AutoResponsePolicy
    ix = _make_inbound()
    ix.inbound = False
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.5,
    )
    assert decide(ix, p) is None


# ─── apply_close_duplicate() ─────────────────────────────────────────────────


def test_close_duplicate_posts_comment_then_closes(transport):
    """Order is load-bearing: post comment first, then close. If we
    closed first and the comment failed, the reporter would see a
    closure with no explanation."""
    from evolve_admin.intake.auto_responder import apply_close_duplicate
    ix = _make_inbound()
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    methods = [(r[0], r[1]) for r in transport.requests]
    # First request: POST a comment.
    assert methods[0][0] == "POST"
    assert "/issues/7/comments" in methods[0][1]
    # Second: PATCH the issue to closed.
    assert methods[1][0] == "PATCH"
    assert "/issues/7" in methods[1][1]


def test_close_duplicate_records_undo_handle(transport):
    """The undo handle must carry the comment_id so undo can delete it."""
    from evolve_admin.intake.auto_responder import apply_close_duplicate
    transport.respond("POST", "/comments", 201, {"id": 12345})
    ix = _make_inbound()
    out = apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    assert out.auto_action is not None
    assert out.auto_action.kind == "close_duplicate"
    assert out.auto_action.undo_handle["comment_id"] == 12345
    assert out.auto_action.undo_handle["owner"] == "evolve-ops"
    assert out.auto_action.undo_handle["repo"] == "evolve"
    assert out.auto_action.undo_handle["number"] == 7


def test_close_duplicate_sets_24h_undo_deadline(transport):
    """Spec pins the window at 24h. The deadline must be ~24h after acted_at."""
    from evolve_admin.intake.auto_responder import apply_close_duplicate
    ix = _make_inbound()
    out = apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    rec = out.auto_action
    acted = datetime.fromisoformat(rec.acted_at.replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(rec.undo_deadline_at.replace("Z", "+00:00"))
    delta = deadline - acted
    assert timedelta(hours=23, minutes=59) <= delta <= timedelta(hours=24, minutes=1)


def test_close_duplicate_comment_contains_duplicate_ref(transport):
    """The auto-comment must cite the duplicate refs so the reporter
    can navigate to the canonical issue."""
    from evolve_admin.intake.auto_responder import apply_close_duplicate
    ix = _make_inbound(duplicate_of=("evolve-ops/evolve#42",))
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    # The first request body should contain the duplicate ref.
    import json as _json
    body = transport.requests[0][3]
    payload = _json.loads(body.decode("utf-8"))
    assert "evolve-ops/evolve#42" in payload["body"]


def test_close_duplicate_cleans_up_comment_if_close_fails(transport):
    """If PATCH fails after the comment is posted, the auto-responder
    must delete the orphan comment so the issue isn't littered with
    a "closing as duplicate" comment on an issue we couldn't close."""
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, AutoResponseError,
    )
    transport.respond("POST", "/comments", 201, {"id": 9999})
    transport.respond("PATCH", "/issues/7", 422, {"message": "validation"})
    ix = _make_inbound()
    with pytest.raises(AutoResponseError):
        apply_close_duplicate(
            ix, token="t", transport=transport, actor_login="evo-bot",
        )
    # Last request must be a DELETE of the orphan comment.
    last = transport.requests[-1]
    assert last[0] == "DELETE"
    assert "/issues/comments/9999" in last[1]


def test_close_duplicate_refuses_when_comment_has_no_id(transport):
    """If GH POST returns no id, we can't undo. Refuse rather than
    close-without-undo-handle."""
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, AutoResponseError,
    )
    transport.respond("POST", "/comments", 201, {})
    ix = _make_inbound()
    with pytest.raises(AutoResponseError):
        apply_close_duplicate(
            ix, token="t", transport=transport, actor_login="evo-bot",
        )


# ─── apply_reply_clarifying() ────────────────────────────────────────────────


def test_reply_clarifying_posts_draft_verbatim(transport):
    from evolve_admin.intake.auto_responder import apply_reply_clarifying
    import json as _json
    ix = _make_inbound(draft_reply="Can you share the OC version?")
    apply_reply_clarifying(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    body = transport.requests[0][3]
    payload = _json.loads(body.decode("utf-8"))
    assert payload["body"] == "Can you share the OC version?"


def test_reply_clarifying_does_not_close_issue(transport):
    """reply_clarifying is comment-only. The issue must stay open."""
    from evolve_admin.intake.auto_responder import apply_reply_clarifying
    ix = _make_inbound()
    apply_reply_clarifying(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    assert not any(r[0] == "PATCH" for r in transport.requests)


def test_reply_clarifying_records_undo_handle(transport):
    from evolve_admin.intake.auto_responder import apply_reply_clarifying
    transport.respond("POST", "/comments", 201, {"id": 7777})
    ix = _make_inbound()
    out = apply_reply_clarifying(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    assert out.auto_action.kind == "reply_clarifying"
    assert out.auto_action.undo_handle["comment_id"] == 7777


# ─── apply_label_only() ──────────────────────────────────────────────────────


def test_label_only_adds_labels(transport):
    from evolve_admin.intake.auto_responder import apply_label_only
    import json as _json
    ix = _make_inbound(draft_labels=("bug", "triaged"))
    apply_label_only(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    assert transport.requests[0][0] == "POST"
    assert transport.requests[0][1].endswith("/labels")
    payload = _json.loads(transport.requests[0][3].decode("utf-8"))
    assert payload["labels"] == ["bug", "triaged"]


def test_label_only_records_labels_added(transport):
    from evolve_admin.intake.auto_responder import apply_label_only
    ix = _make_inbound(draft_labels=("bug",))
    out = apply_label_only(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    assert out.auto_action.undo_handle["labels_added"] == ["bug"]


# ─── undo() ──────────────────────────────────────────────────────────────────


def test_undo_close_duplicate_reopens_then_deletes_comment(transport):
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, undo,
    )
    transport.respond("POST", "/comments", 201, {"id": 55555})
    ix = _make_inbound()
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    transport.requests.clear()
    undo(ix, token="t", transport=transport)
    # Should PATCH the issue back to open first, then DELETE the comment.
    assert transport.requests[0][0] == "PATCH"
    assert transport.requests[0][1].endswith("/issues/7")
    import json as _json
    payload = _json.loads(transport.requests[0][3].decode("utf-8"))
    assert payload["state"] == "open"
    assert transport.requests[1][0] == "DELETE"
    assert "/issues/comments/55555" in transport.requests[1][1]


def test_undo_marks_record_undone(transport):
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, undo,
    )
    ix = _make_inbound()
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    out = undo(ix, token="t", transport=transport)
    assert out.auto_action.undone is True
    assert out.auto_action.undone_at


def test_undo_after_deadline_refused(transport):
    """The undo window is 24h. After that, the row hardens."""
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, undo, AutoResponseError,
    )
    ix = _make_inbound()
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    # Simulate "now" being 25 hours after acted_at.
    acted = datetime.fromisoformat(
        ix.auto_action.acted_at.replace("Z", "+00:00")
    )
    way_later = (acted + timedelta(hours=25)).isoformat(timespec="seconds")
    with pytest.raises(AutoResponseError):
        undo(ix, token="t", transport=transport, now_iso=way_later)


def test_undo_twice_refused(transport):
    from evolve_admin.intake.auto_responder import (
        apply_close_duplicate, undo, AutoResponseError,
    )
    ix = _make_inbound()
    apply_close_duplicate(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    undo(ix, token="t", transport=transport)
    with pytest.raises(AutoResponseError):
        undo(ix, token="t", transport=transport)


def test_undo_with_no_auto_action_refused(transport):
    from evolve_admin.intake.auto_responder import undo, AutoResponseError
    ix = _make_inbound()
    ix.auto_action = None
    with pytest.raises(AutoResponseError):
        undo(ix, token="t", transport=transport)


def test_undo_reply_clarifying_deletes_comment(transport):
    from evolve_admin.intake.auto_responder import (
        apply_reply_clarifying, undo,
    )
    transport.respond("POST", "/comments", 201, {"id": 4242})
    ix = _make_inbound(recommendation="auto_reply_clarifying")
    apply_reply_clarifying(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    transport.requests.clear()
    undo(ix, token="t", transport=transport)
    assert transport.requests[0][0] == "DELETE"
    assert "/issues/comments/4242" in transport.requests[0][1]


def test_undo_label_only_removes_labels(transport):
    from evolve_admin.intake.auto_responder import apply_label_only, undo
    ix = _make_inbound(draft_labels=("bug", "triaged"))
    apply_label_only(
        ix, token="t", transport=transport, actor_login="evo-bot",
    )
    transport.requests.clear()
    undo(ix, token="t", transport=transport)
    # Two DELETE requests, one per label, with the label name URL-encoded.
    deletes = [r for r in transport.requests if r[0] == "DELETE"]
    assert len(deletes) == 2
    urls = [r[1] for r in deletes]
    assert any("/labels/bug" in u for u in urls)
    assert any("/labels/triaged" in u for u in urls)


# ─── Batch runner ────────────────────────────────────────────────────────────


def test_run_auto_responses_skips_when_policy_disabled(shared_dir, transport):
    """The single most important safety property: a disabled policy
    NEVER fires actions, even if matching intakes exist on disk."""
    from evolve_admin.intake.auto_responder import run_auto_responses
    from evolve_admin.intake.policy import AutoResponsePolicy
    from evolve_admin.intake import store
    ix = _make_inbound()
    store.write_intake(ix, shared_dir)
    rows = run_auto_responses(
        shared_dir,
        policy=AutoResponsePolicy(enabled=False),
        token="t", transport=transport, actor_login="evo-bot",
    )
    assert rows == []
    assert transport.requests == []


def test_run_auto_responses_applies_matching(shared_dir, transport):
    from evolve_admin.intake.auto_responder import run_auto_responses
    from evolve_admin.intake.policy import AutoResponsePolicy
    from evolve_admin.intake import store
    ix = _make_inbound(confidence=0.95)
    store.write_intake(ix, shared_dir)
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.9,
    )
    rows = run_auto_responses(
        shared_dir, policy=p,
        token="t", transport=transport, actor_login="evo-bot",
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["kind"] == "close_duplicate"
    # Persisted: re-read should show auto_action set.
    found = store.find_intake(shared_dir, ix.id)
    assert found is not None
    refreshed, _path, _sd = found
    assert refreshed.auto_action is not None


def test_run_auto_responses_error_does_not_block_others(shared_dir, transport):
    """A failure on one intake must not abort the batch."""
    from evolve_admin.intake.auto_responder import run_auto_responses
    from evolve_admin.intake.policy import AutoResponsePolicy
    from evolve_admin.intake import store
    ix_bad = _make_inbound(id_="bad-1", confidence=0.95)
    ix_bad.promotion.github_issue_url = "not a valid url"
    ix_good = _make_inbound(id_="good-1", number=8, confidence=0.95)
    store.write_intake(ix_bad, shared_dir)
    store.write_intake(ix_good, shared_dir)
    p = AutoResponsePolicy(
        enabled=True, close_duplicate_enabled=True,
        close_duplicate_min_confidence=0.9,
    )
    rows = run_auto_responses(
        shared_dir, policy=p,
        token="t", transport=transport, actor_login="evo-bot",
    )
    by_id = {r["id"]: r for r in rows}
    assert by_id["bad-1"]["status"] == "error"
    assert by_id["good-1"]["status"] == "applied"
