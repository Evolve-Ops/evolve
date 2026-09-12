"""tests/test_sudoers_log_rotate.py

Regression test: the rendered evolve sudoers file must include the
truncate grants the daily oc-log-rotate cron needs.

Bug history: the §3.1 truncate grants shipped with the oc-log-rotate
rotator, then PR #1956 (commit 34ffba154, a forge fix) accidentally
reverted the grant block out of `_render_evolve_sudoers` via a
stale-branch hunk. Nothing pinned the grants, so the loss was silent:
the ai.evolve.evolve.oc-log-rotate daemon kept running daily, its
`sudo /usr/bin/truncate -s 0 ...` calls failed sudo, and the
launchd-captured gateway.log / gateway.err.log files went back to
growing unbounded (4 bots @ 100M+ was the original incident that
motivated the rotator).

The needles are full grant lines including the trailing newline —
a needle that is a prefix of a longer sibling grant passes spuriously
when the pinned grant itself is dropped (three older pins had exactly
that bug).

Consumer: evolve_admin.oc_log_rotate._truncate — argv
`sudo -n /usr/bin/truncate -s 0 /Users/<bot_user>/.openclaw/logs/<name>`
for name in ("gateway.log", "gateway.err.log"). (`-n` is a sudo flag,
not part of the granted command, so it doesn't affect grant matching;
test_oc_log_rotate.py pins the argv tail to the needles here.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import _render_evolve_sudoers  # noqa: E402


@pytest.fixture(scope="module")
def sudoers_content() -> str:
    content = _render_evolve_sudoers()
    assert content is not None, "openclaw not discoverable at test time"
    return content


def test_truncate_gateway_log_grant_present(sudoers_content: str) -> None:
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/usr/bin/truncate -s 0 /Users/*/.openclaw/logs/gateway.log\n"
    )
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for the daily oc-log-rotate cron. "
        f"Without it the launchd-captured gateway.log grows unbounded. "
        f"Add this line in setup_wizard._render_evolve_sudoers:\n  {needle}"
    )


def test_truncate_gateway_err_log_grant_present(sudoers_content: str) -> None:
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/usr/bin/truncate -s 0 /Users/*/.openclaw/logs/gateway.err.log\n"
    )
    assert needle in sudoers_content, (
        f"Missing required sudoers grant for the daily oc-log-rotate cron. "
        f"Without it the launchd-captured gateway.err.log grows unbounded. "
        f"Add this line in setup_wizard._render_evolve_sudoers:\n  {needle}"
    )


def test_grant_section_documented(sudoers_content: str) -> None:
    """The truncate grants should be annotated with the consuming daemon's
    label so future operators grep'ing the sudoers file can find why."""
    assert "oc-log-rotate" in sudoers_content, (
        "The truncate grant block should reference the oc-log-rotate cron "
        "in its comment so future operators understand why it's there."
    )
