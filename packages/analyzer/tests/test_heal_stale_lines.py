"""Regression tests for heal._drop_stale_log_lines.

OpenClaw writes multi-line error blocks where only the first line carries
an ISO timestamp prefix. Earlier versions of `_drop_stale_log_lines` kept
every untimed line indefinitely (the "unknown age → keep" rule), which
caused pre-fix error spam to linger in the maintenance UI for days after
the underlying config issue was resolved. The May-9-2026 team_bot_a memorySearch
reload was the smoking-gun: OpenAI 429 blocks from before the reload kept
surfacing in `recent_errors` long after team_bot_a had switched to Gemini.

A first attempt at the fix had a bug: it processed `context_lines` in a
separate pre-pass, which seeded the running anchor to the LAST timestamp
in the buffer (most recent), so every later error line inherited that
fresh anchor regardless of where it actually sat in file order. Result:
the same May-9 stale 429 block kept passing the freshness check on admin_bot
and team_bot_b because the buffer's most recent timed line (a fresh log entry
from a different subsystem at the bottom of the tail) was erroneously
adopted as the block's anchor. The current implementation walks a single
ordered buffer with an optional `keep` predicate so the anchor is always
strictly the most recent timed line *preceding* each entry.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402


@pytest.fixture
def freeze_now(monkeypatch):
    """Pin heal's `now` to a fixed UTC instant for deterministic age math."""
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(heal, "datetime", _DT)
    return fixed


def _iso(dt: datetime) -> str:
    """Format a datetime in the leading-token form heal._parse_log_ts expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")


def _err_only(line: str) -> bool:
    """Mirror the call site's filter: keep only lines containing 'error'."""
    return "error" in line.lower()


def test_fresh_timed_line_kept(freeze_now):
    line = f"{_iso(freeze_now - timedelta(minutes=5))} [agent] real error here"
    kept = heal._drop_stale_log_lines([line], keep=_err_only)
    assert len(kept) == 1
    assert kept[0][0] == line
    assert kept[0][1] is not None


def test_stale_timed_line_dropped(freeze_now):
    line = f"{_iso(freeze_now - timedelta(days=3))} [agent] ancient error"
    assert heal._drop_stale_log_lines([line], keep=_err_only) == []


def test_untimed_line_inherits_recent_anchor(freeze_now):
    """A multi-line error block: timed header + untimed JSON body. The body
    lines inherit the header's timestamp and age out together."""
    header = f"{_iso(freeze_now - timedelta(minutes=2))} [memory] sync failed: error: 429 {{"
    body_1 = '    "error": {'
    body_2 = '        "message": "rate limited"'
    body_3 = "    }"
    body_4 = "}"
    kept = heal._drop_stale_log_lines(
        [header, body_1, body_2, body_3, body_4], keep=_err_only,
    )
    # `header` and `body_1` contain "error"; the others (rate-limited message,
    # closing braces) don't and are filtered out by `_err_only`.
    assert [k[0] for k in kept] == [header, body_1]
    assert all(k[1] for k in kept), "kept lines must carry a timestamp"


def test_untimed_block_with_stale_anchor_dropped(freeze_now):
    """The team_bot_a-May-9 case: an untimed 429 JSON block sitting in the tail
    long after the timed header that produced it. Header is older than 24h,
    so the whole block must age out — not stick around as 'unknown age'."""
    header = f"{_iso(freeze_now - timedelta(days=5))} [memory] sync failed: error: 429 {{"
    body = '    "error": {"message": "quota exceeded"}'
    closing = "}"
    assert heal._drop_stale_log_lines([header, body, closing], keep=_err_only) == []


def test_orphaned_untimed_lines_dropped(freeze_now):
    """Untimed lines at the top of the buffer with no preceding timed anchor
    can't be placed in time — drop them. This is the bug-fix case: the prior
    'keep unknown age' rule let these persist indefinitely."""
    lines = [
        '[memory] sync failed (watch): Error: openai embeddings failed: 429 {',
        '    "error": { "message": "quota" }',
        "}",
    ]
    assert heal._drop_stale_log_lines(lines, keep=_err_only) == []


def test_orphan_block_followed_by_fresh_block(freeze_now):
    """The interesting boundary case: a buffer that begins with an orphaned
    untimed block, then has a properly-headered fresh block after. Only the
    fresh block survives."""
    orphan_body = '    "error": { "message": "quota" }'
    orphan_close = "}"
    fresh_header = f"{_iso(freeze_now - timedelta(minutes=1))} [agent] new error"
    fresh_body = "    error detail line"
    kept = heal._drop_stale_log_lines(
        [orphan_body, orphan_close, fresh_header, fresh_body], keep=_err_only,
    )
    assert [k[0] for k in kept] == [fresh_header, fresh_body]


def test_anchor_is_preceding_timestamp_not_buffer_max(freeze_now):
    """REGRESSION for the admin_bot/team_bot_b bug: a stale 429 block sits early in
    the buffer with an old preceding header; later in the buffer there are
    fresh timestamped lines from unrelated subsystems. The 429 block must
    inherit its OWN preceding header (stale → drop), not the freshest
    timestamp anywhere in the buffer.

    This is the exact shape that caused the first fix to leak: admin_bot's tail
    had a May-9 429 block at the top followed by 70+ lines of fresh May-11
    activity. The old `context_lines` pre-pass adopted the May-11 anchor
    for everything in `error_lines`, so the stale block kept appearing as
    'recent.'"""
    stale_header = (
        f"{_iso(freeze_now - timedelta(days=2))} "
        "[diagnostic] earlier event"
    )
    stale_body_1 = "[memory] embeddings rate limited; retrying in 1004ms"
    stale_body_2 = "[memory] sync failed (watch): Error: openai embeddings failed: 429 {"
    stale_body_3 = '    "error": {"message": "quota"}'
    fresh_header = (
        f"{_iso(freeze_now - timedelta(minutes=10))} "
        "[plugins] task extractor not found — skipping"
    )
    # `fresh_header` doesn't contain 'error' and isn't an error itself —
    # it's just a fresh timestamp later in the buffer. The stale 429 block
    # must NOT inherit fresh_header's timestamp.
    lines = [
        stale_header, stale_body_1, stale_body_2, stale_body_3, fresh_header,
    ]
    kept = heal._drop_stale_log_lines(lines, keep=_err_only)
    assert kept == [], (
        "stale block must be dropped; got: "
        f"{[(l[:60], ts) for l, ts in kept]}"
    )


def test_anchor_refreshed_by_newer_timed_line(freeze_now):
    """Successive timed lines update the anchor. Untimed lines after each
    timed line inherit the freshest anchor in scope (i.e., the immediately
    preceding timed line, not some earlier one)."""
    old_header = f"{_iso(freeze_now - timedelta(days=5))} [old] timed error"
    new_header = f"{_iso(freeze_now - timedelta(minutes=1))} [new] timed error"
    new_body = "    error continuation"
    kept = heal._drop_stale_log_lines(
        [old_header, new_header, new_body], keep=_err_only,
    )
    assert [k[0] for k in kept] == [new_header, new_body]


def test_no_keep_filter_emits_everything_with_anchor(freeze_now):
    """Without a `keep` predicate, every line that has an (own or inherited)
    fresh anchor is emitted — even ones that don't contain 'error'."""
    header = f"{_iso(freeze_now - timedelta(minutes=2))} [agent] heartbeat ok"
    body = "    detail line with no error keyword"
    kept = heal._drop_stale_log_lines([header, body])
    assert [k[0] for k in kept] == [header, body]
