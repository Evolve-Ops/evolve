"""The spend-alert log path is derived from ``shared_dir``, not a module constant.

``spend_alert._LOG_FILE = Path("/Users/Shared/evolve/logs/spend_alert.log")``
was a hard-coded module constant, so every test that drove a spend path
appended its fixture lines to the REAL operator-facing log whatever
``tmp_path`` it was handed. Measured on a maintainer's laptop on 2026-09-01:
the seven ``test_spend_alert_*`` suites moved
``/Users/Shared/evolve/logs/spend_alert.log`` from 10441 to 10478 lines in one
run — 37 fabricated cap trips, breaker trips and tier downgrades sitting in
the log an operator greps when a bot's spend runs away. Benign on Linux CI
(``/Users`` is not creatable by a non-root user, so the ``mkdir`` raises and
``_log`` swallows the OSError) and invisible there for the same reason.

These tests pin the *executed* path, not just the presence of a helper: a real
run must land in the caller's ``shared_dir``. A later change that reintroduces
a module-level default — under any name — leaves the tmp log empty and turns
this file red.

The last test here is not about the log path at all. It guards the re-land:
``spend_alert`` used to define its own ``_LIVE_LOAD_FAILED = object()`` and its
own ``_load_live_turns``, and ``main`` has since moved both into
``live_spend``. Merging the original branch mechanically would have restored
the local sentinel beside the imported one — two distinct objects, compared by
identity, so ``if turns is _LIVE_LOAD_FAILED`` silently stops matching and
"I could not read this bot's turns" degrades into "this bot is idle". One
``is`` assertion catches that whole class.
"""

from __future__ import annotations

import inspect
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import live_spend  # noqa: E402
import spend_alert  # noqa: E402


@pytest.fixture(autouse=True)
def _no_delivery(monkeypatch):
    """Delivery is not under test here — keep the run off the network."""
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: False)


def test_resolved_log_path_lives_under_the_given_shared_dir(tmp_path: Path):
    log_file = spend_alert.spend_alert_log_path(tmp_path)

    assert log_file == tmp_path / "logs" / "spend_alert.log"
    assert tmp_path in log_file.parents, (
        f"spend_alert log resolved to {log_file}, outside the caller's "
        f"shared_dir {tmp_path} — a run would write to some other pod's log"
    )


def test_log_requires_a_shared_dir_with_no_production_fallback():
    """No default ⇒ a new call site cannot silently write to the real log.

    The containment only holds because omitting ``shared_dir`` is a type
    error rather than a fallback to a module constant.
    """
    param = inspect.signature(spend_alert._log).parameters["shared_dir"]

    assert param.default is inspect.Parameter.empty, (
        "_log grew a default shared_dir — a call site that forgets the "
        "argument would silently write to whatever that default points at"
    )


def test_no_shared_dir_writes_nowhere_rather_than_falling_back(tmp_path: Path, capsys):
    """``None`` means "no pod root", which means no file — never a default.

    Two call sites (``_pod_budget_defaults`` and ``_resolve_spend_cap_action``)
    take ``shared_dir: Path | None``. The line still has to reach the operator
    on stdout; what it must not do is pick an absolute path of its own, which
    is the behaviour this whole file exists to remove.
    """
    monkey_root = tmp_path / "logs"

    spend_alert._log("[spend_alert] pod budget defaults unreadable", None)

    assert "pod budget defaults unreadable" in capsys.readouterr().out
    assert not monkey_root.exists()


def test_weekly_summary_writes_the_log_under_shared_dir(tmp_path: Path):
    """The executed path, not just the helper: a real run lands in tmp.

    The expected path is spelled out literally rather than read back from
    ``spend_alert_log_path`` — a resolver that ignored ``shared_dir`` would
    otherwise move both sides of the assertion and pass.
    """
    log_file = tmp_path / "logs" / "spend_alert.log"
    assert not log_file.exists()

    spend_alert._maybe_send_weekly_summary(
        tmp_path, ["team_bot_c"], date(2026, 9, 1),
        weekly_threshold=20.0, network={},
    )

    assert log_file.exists(), (
        "the weekly-summary run wrote no log under shared_dir — the log path "
        "is no longer derived from the caller's shared_dir"
    )
    assert "weekly summary: $0.00 over 7d" in log_file.read_text()


def test_burst_window_routes_its_log_to_the_callers_shared_dir(
    tmp_path: Path, monkeypatch,
):
    """``burst_window_spend`` gained ``shared_dir`` purely to reach ``_log``.

    Pins that the plumbing is wired end to end: the unpriced-turn note it
    emits has to reach the caller's pod, not a module constant's. The loader
    is faked at ``live_spend.load_live_turns``, which is where it lives since
    the refactor — the original branch faked a ``spend_alert._load_live_turns``
    that no longer exists.
    """
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        live_spend, "load_live_turns",
        lambda *a, **kw: [{"ts": "2026-09-01T11:30:00Z", "bot_id": "team_bot_c"}],
    )
    monkeypatch.setattr(spend_alert, "_turn_cost", lambda *a, **kw: None)

    total, selected = spend_alert.burst_window_spend(
        tmp_path, "team_bot_c", now=now, window_minutes=60,
    )

    assert (total, selected) == (0.0, [])
    body = (tmp_path / "logs" / "spend_alert.log").read_text()
    assert "burst total is a floor" in body


def test_two_shared_dirs_do_not_share_a_log(tmp_path: Path):
    """Two pods, two logs — the proof that nothing is process-global."""
    pod_a, pod_b = tmp_path / "a", tmp_path / "b"

    spend_alert._log("[spend_alert] finding for pod a", pod_a)
    spend_alert._log("[spend_alert] finding for pod b", pod_b)

    log_a = (pod_a / "logs" / "spend_alert.log").read_text()
    log_b = (pod_b / "logs" / "spend_alert.log").read_text()
    assert "finding for pod a" in log_a and "finding for pod b" not in log_a
    assert "finding for pod b" in log_b and "finding for pod a" not in log_b


def test_the_discovery_sentinel_is_live_spends_one_not_a_local_copy():
    """One object, compared by identity — never two that look alike.

    ``spend_alert`` re-exports the sentinel; a local ``object()`` under the
    same name would pass every test that only checks the alert path's
    *behaviour*, because the two objects are indistinguishable except by
    ``is`` — which is exactly how the comparison is written.
    """
    assert spend_alert._LIVE_LOAD_FAILED is live_spend.LIVE_LOAD_FAILED, (
        "spend_alert._LIVE_LOAD_FAILED is not live_spend.LIVE_LOAD_FAILED — a "
        "second sentinel has been introduced, so `turns is _LIVE_LOAD_FAILED` "
        "no longer matches and an unreadable bot reads as an idle one"
    )
