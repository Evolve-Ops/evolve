"""Mid-dispatch ACL-clamp tolerance in the forge poll loop (Linux round-8 family).

Live failure this pins (reference VPS pod, 2026-07-28): every Linux forge
dispatch died ~20s in with a bare ``[Errno 13] Permission denied:
'…/workspace/evolve/forge/outbox/<job>.json'``. The OC agent's startup
hardening chmods ``~/.openclaw`` to 0700 *during* the dispatch it was spawned
for; chmod recalculates the POSIX-ACL mask to ``---``, capping the evolve
daemon's inherited ACE — and ``pathlib.Path.exists()`` raises
``PermissionError`` on EACCES (unlike ``os.path.exists``), so the poll loop
blew up instead of waiting.

``_outbox_ready`` is the seam: EACCES heals via
``secret_config_perms.heal_evolve_access`` (injectable, throttled to one heal
per window — OC hardens in BURSTS during agent boot) and always reports
not-ready instead of raising; the dispatch deadline is the fail-loud
backstop for a genuinely broken channel.
"""

from __future__ import annotations

import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.bot_forge import _outbox_ready  # noqa: E402


class _ClampedPath:
    """Stand-in for the outbox Path whose exists() hits a clamped mask."""

    def __init__(self, raises: int, exists_after: bool = True):
        self.raises = raises          # how many exists() calls raise EACCES
        self.exists_after = exists_after
        self.calls = 0
        self.parent = Path("/home/bot/.openclaw/workspace/evolve/forge/outbox")

    def exists(self) -> bool:
        self.calls += 1
        if self.calls <= self.raises:
            raise PermissionError(13, "Permission denied", "outbox.json")
        return self.exists_after


def test_first_clamp_heals_and_reports_not_ready():
    heals: list[tuple] = []
    state: dict = {}
    p = _ClampedPath(raises=1)

    ready = _outbox_ready(p, "team_bot_a", state, heal_fn=lambda b, u: heals.append((b, u)) or True)

    assert ready is False              # not ready this tick — re-probe next tick
    assert heals == [("team_bot_a", "team_bot_a")]
    assert "last_heal" in state

    # Next tick: mask repaired, outbox visible.
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=lambda b, u: True) is True


def test_clamp_burst_never_raises_but_throttles_heals():
    """OC hardens in bursts during agent boot — a heal can land and the clamp
    be back within one poll tick. EACCES must NEVER propagate from the probe
    (the dispatch deadline is the fail-loud backstop); repeat clamps within
    the throttle window skip the heal instead of storming setfacl."""
    heals: list[int] = []
    state: dict = {}
    p = _ClampedPath(raises=5)
    heal = lambda b, u: heals.append(1) or True  # noqa: E731

    for _ in range(5):
        assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False
    assert len(heals) == 1  # throttled: one heal within the window

    # After the throttle window elapses, a persisting clamp heals again.
    state["last_heal"] -= 11.0
    p.raises = 6
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False
    assert len(heals) == 2


def test_heal_failure_does_not_mask_the_poll():
    """A heal that itself blows up must not kill the dispatch — the loop
    keeps polling and only a REPEAT clamp is fatal."""
    state: dict = {}
    p = _ClampedPath(raises=1)

    def broken_heal(b, u):
        raise RuntimeError("sudo grant missing")

    assert _outbox_ready(p, "team_bot_a", state, heal_fn=broken_heal) is False
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=broken_heal) is True


def test_no_clamp_is_a_plain_exists():
    state: dict = {}
    p = _ClampedPath(raises=0, exists_after=False)
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=lambda b, u: True) is False
    assert "last_heal" not in state  # heal never attempted


def test_distinct_clamp_after_clean_probe_heals_again():
    """Long dispatches can be re-clamped by an hourly daemon's own openclaw
    run — a clean probe resets the budget, so each DISTINCT clamp gets one
    heal instead of the second clamp killing the dispatch."""
    heals: list[int] = []
    state: dict = {}

    class _TwoClamps:
        # raise, ok, raise, ok — two separate clamp episodes
        seq = iter([True, False, True, False])
        parent = Path("/x/outbox")

        def exists(self) -> bool:
            if next(self.seq):
                raise PermissionError(13, "Permission denied", "outbox.json")
            return False

    p = _TwoClamps()
    heal = lambda b, u: heals.append(1) or True  # noqa: E731
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False  # clamp 1
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False  # clean
    state["last_heal"] -= 11.0  # throttle window elapses between episodes
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False  # clamp 2
    assert _outbox_ready(p, "team_bot_a", state, heal_fn=heal) is False  # clean
    assert len(heals) == 2


def test_heal_targets_resolved_bot_user():
    """A network.json `user` override makes the unix user differ from
    bot_id — the heal must target the resolved user."""
    heals: list[tuple] = []
    state: dict = {}
    p = _ClampedPath(raises=1)
    _outbox_ready(p, "team_bot_a", state,
                  heal_fn=lambda b, u: heals.append((b, u)) or True,
                  bot_user="tba_svc")
    assert heals == [("team_bot_a", "tba_svc")]
