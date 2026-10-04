"""tests/test_release_manager.py — 7.2 proof artifacts.

Spec: internal/spec-state-store-and-deploy-resilience-2026-06-10.md §2.9
(roadmap Phase 7 row 7.2 — "a deliberately broken release: the canary
catches it, the fleet never restarts onto it, and rollback restores in
one command").

Real git fixtures (bare origin + fleet clone + worktrees); everything
host-touching beyond git (deploys, hooks, soak probes, signals) is
injected through ReleaseDeps so the state machine itself runs un-mocked.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import release_manager as rm  # noqa: E402
from evolve_admin import soak_probe  # noqa: E402


# ── git fixture plumbing ──────────────────────────────────────────────────────


def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd} failed: {r.stderr}"
    return r.stdout.strip()


def _commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _run(["git", "add", rel], repo)
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
          "commit", "-q", "-m", msg, "--allow-empty"], repo)
    return _run(["git", "rev-parse", "HEAD"], repo)


@pytest.fixture
def pod(tmp_path):
    """origin (bare) + fleet clone with one commit, plus shared_dir and
    a network.json with a canary bot configured."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], tmp_path)

    seed = tmp_path / "seed"
    _run(["git", "clone", "-q", str(origin), str(seed)], tmp_path)
    sha_a = _commit(seed, "A: initial", {
        "packages/admin/evolve_admin/__init__.py": "",
        "packages/admin/evolve_admin/ok.py": "VALUE = 1\n",
    })
    _run(["git", "push", "-q", "origin", "main"], seed)

    fleet = tmp_path / "fleet"
    _run(["git", "clone", "-q", str(origin), str(fleet)], tmp_path)

    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "networkId": "test-pod",
        "sharedDir": str(shared),
        "members": ["canary_bot", "other_bot"],
        "bots": {
            "canary_bot": {"role": "member"},
            "other_bot": {"role": "member"},
        },
        "pod": {"release": {"mode": "canary", "canary_bot": "canary_bot",
                            "soak_minutes": 60}},
    }
    network_path = shared / "network.json"
    network_path.write_text(json.dumps(network))

    return {
        "origin": origin, "seed": seed, "fleet": fleet,
        "shared": shared, "network": network, "network_path": network_path,
        "sha_a": sha_a, "staging": tmp_path / "staging",
    }


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 6, 10, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes: float):
        self.now += dt.timedelta(minutes=minutes)


class Recorder:
    """Injected fakes that record calls."""

    def __init__(self):
        self.deploys: list[tuple[str, str]] = []     # (bot, code_dir)
        self.hooks: list[tuple[str, str]] = []       # (before, after)
        self.signals: list[dict] = []
        self.soak_healthy = True
        self.soak_detail = "ok"
        # Every baseline the soak-health seam was called with (D4) — lets
        # tests assert the captured baseline is threaded through to the check.
        self.soak_baselines: list[list[str] | None] = []
        self.gate1_ok = True
        self.gate1_detail = "ok"
        self.deploy_ok = True
        # Active soak probe (B2). Default ok so candidates with an
        # exercisable diff still soak; per-test flags drive regression /
        # tooling-error paths. `probes` records every plan the probe saw.
        self.probe_status = rm.SOAK_PROBE_OK
        self.probe_detail = "fake probe ok"
        self.probes: list[list[dict]] = []

    def deploy(self, bot, code_dir, network_path):
        self.deploys.append((bot, str(code_dir)))
        return (self.deploy_ok, "fake deploy")

    def soak_probe(self, shared_dir, bot, staging, network_path, plan):
        self.probes.append(plan)
        return (self.probe_status, self.probe_detail)

    def run_hooks(self, repo, before, after):
        self.hooks.append((before, after))
        return (True, "fake hooks")

    def gate1(self, staging, changed, deps):
        return (self.gate1_ok, self.gate1_detail)

    def soak(self, shared_dir, bot, since, baseline=None):
        self.soak_baselines.append(baseline)
        return (self.soak_healthy, self.soak_detail)

    def observe(self, **kw):
        self.signals.append(kw)

    def resolve(self, **kw):
        pass


def _deps(pod, rec: Recorder, clock: Clock, *, real_gate1=False) -> rm.ReleaseDeps:
    return rm.ReleaseDeps(
        gate1_fn=None if real_gate1 else rec.gate1,
        canary_deploy_fn=rec.deploy,
        soak_health_fn=rec.soak,
        soak_probe_fn=rec.soak_probe,
        hooks_fn=rec.run_hooks,
        observe_fn=rec.observe,
        resolve_fn=rec.resolve,
        staging_root=pod["staging"],
        quarantine_root=pod["staging"].parent / "quarantine",
        network_path=pod["network_path"],
        now_fn=clock,
    )


def _cfg(pod) -> rm.ReleaseConfig:
    return rm.resolve_release_config(pod["network"])


def _tick(pod, deps, cfg=None):
    return rm.release_tick(
        pod["fleet"], pod["shared"], cfg=cfg or _cfg(pod), deps=deps,
    )


def _fleet_head(pod) -> str:
    return _run(["git", "rev-parse", "HEAD"], pod["fleet"])


def _push_commit(pod, msg, files) -> str:
    sha = _commit(pod["seed"], msg, files)
    _run(["git", "push", "-q", "origin", "main"], pod["seed"])
    return sha


def _inject_soaking_candidate(pod, sha, *, tier, active_validated=True):
    """Put release.json into the `soaking` state for `sha` — the state an
    in-flight candidate sits in between ticks. Lets the operator-override
    (`release soak`) tests exercise the shorten-guard deterministically: D7
    makes an active-validated short/skip candidate promote in its FIRST tick,
    so there is no inter-tick soaking window to bump via the normal flow."""
    state = rm.load_release_state(pod["shared"])
    state.candidate = {
        "sha": sha,
        "first_seen_at": "2026-06-10T12:00:00+00:00",
        "soak_started_at": "2026-06-10T12:00:00+00:00",
        "state": "soaking",
        "failure": "",
        "soak_tier": tier,
        "soak_active_validated": active_validated,
    }
    rm.save_release_state(state, pod["shared"])
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Initialization + steady state
# ─────────────────────────────────────────────────────────────────────────────


def test_first_tick_initializes_pointer_to_fleet_head(pod):
    rec, clock = Recorder(), Clock()
    res = _tick(pod, _deps(pod, rec, clock))
    assert res.success
    state = rm.load_release_state(pod["shared"])
    assert state is not None
    assert state.stable["sha"] == pod["sha_a"]
    assert state.candidate is None
    # No-op tick: nothing deployed, no hooks, fleet untouched.
    assert rec.deploys == []
    assert rec.hooks == []
    assert _fleet_head(pod) == pod["sha_a"]


def test_corrupt_state_freezes_promotion_and_signals(pod):
    rec, clock = Recorder(), Clock()
    rm.release_state_path(pod["shared"]).write_text("{not json")
    _push_commit(pod, "B: would-be candidate", {"x.py": "X = 1\n"})

    res = _tick(pod, _deps(pod, rec, clock))
    assert not res.success
    assert "corrupt" in res.error
    assert _fleet_head(pod) == pod["sha_a"]
    assert any(s.get("type") == "release_state_corrupt" for s in rec.signals)
    # The corrupt file must NOT be silently replaced.
    assert rm.release_state_path(pod["shared"]).read_text() == "{not json"


def test_pointer_repair_when_fleet_drifts(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init: stable = A

    sha_b = _push_commit(pod, "B", {"y.py": "Y = 1\n"})
    # Out-of-band surgery: fleet manually moved to B.
    _run(["git", "fetch", "-q", "origin"], pod["fleet"])
    _run(["git", "reset", "-q", "--hard", sha_b], pod["fleet"])

    res = _tick(pod, deps)
    assert res.success
    assert _fleet_head(pod) == pod["sha_a"], "fleet must be repaired to the pointer"
    assert any(s.get("type") == "release_pointer_repaired" for s in rec.signals)


# ─────────────────────────────────────────────────────────────────────────────
# PROOF ARTIFACT 1 — a deliberately broken release is caught and never
# reaches the fleet.
# ─────────────────────────────────────────────────────────────────────────────


def test_broken_candidate_fails_real_gate1_and_never_reaches_fleet(pod, monkeypatch):
    rec, clock = Recorder(), Clock()
    # Real Gate 1, scoped to the fixture: the live venv python runs
    # compileall over the staging tree; the import-smoke list is the
    # fixture's own module so the test doesn't depend on repo layout.
    monkeypatch.setattr(rm, "IMPORT_SMOKE_MODULES", ("evolve_admin.ok",))
    deps = _deps(pod, rec, clock, real_gate1=True)
    deps.venv_python = sys.executable
    _tick(pod, deps)  # init

    broken_sha = _push_commit(pod, "B: deliberately broken", {
        "packages/admin/evolve_admin/broken.py": "def oops(:\n    pass\n",
    })

    res = _tick(pod, deps)
    assert res.success  # the tick itself is healthy; the candidate failed
    assert res.candidate_state == "failed"

    state = rm.load_release_state(pod["shared"])
    assert broken_sha in state.skip
    assert state.stable["sha"] == pod["sha_a"]
    assert _fleet_head(pod) == pod["sha_a"], "fleet must never see the broken sha"
    assert rec.hooks == [], "no restart hooks may run for a failed candidate"
    assert rec.deploys == [], "canary must not be deployed when Gate 1 fails"
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)
    # Worktree pruned after resolution.
    assert not rm.staging_dir_for(broken_sha, pod["staging"]).exists()

    # Subsequent ticks skip the known-bad sha without re-gating.
    res2 = _tick(pod, deps)
    assert res2.success
    assert _fleet_head(pod) == pod["sha_a"]


def test_healthy_candidate_passes_real_gate1(pod, monkeypatch):
    rec, clock = Recorder(), Clock()
    monkeypatch.setattr(rm, "IMPORT_SMOKE_MODULES", ("evolve_admin.ok",))
    deps = _deps(pod, rec, clock, real_gate1=True)
    deps.venv_python = sys.executable
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "B: healthy", {
        "packages/admin/evolve_admin/feature.py": "FEATURE = True\n",
    })
    res = _tick(pod, deps)
    assert res.success
    # feature.py is short tier; real Gate 1 (import-smoke) passes, the canary
    # is deployed from staging and actively validated → D7 promotes it at
    # active-pass time, same tick. The first deploy is the staging exercise.
    assert res.promoted_to == sha_b
    assert rec.deploys[0] == ("canary_bot",
                              str(rm.staging_dir_for(sha_b, pod["staging"])))


# ─────────────────────────────────────────────────────────────────────────────
# PROOF ARTIFACT 2 — healthy candidate soaks on the canary, then promotes.
# ─────────────────────────────────────────────────────────────────────────────


def test_healthy_candidate_soaks_then_promotes(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "B: healthy", {"z.py": "Z = 1\n"})

    # Tick 1: gate1 passes, canary deployed FROM STAGING, soak starts.
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"
    assert _fleet_head(pod) == pod["sha_a"], "fleet unchanged during soak"
    assert len(rec.deploys) == 1
    bot, code_dir = rec.deploys[0]
    assert bot == "canary_bot"
    assert code_dir == str(rm.staging_dir_for(sha_b, pod["staging"]))
    # The staging worktree really is the candidate's code.
    assert _run(["git", "rev-parse", "HEAD"], Path(code_dir)) == sha_b

    # Tick 2, mid-soak: still soaking, no promote.
    clock.advance(15)
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]

    # Tick 3, soak elapsed: promote.
    clock.advance(50)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b

    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b
    assert state.previous["sha"] == pod["sha_a"]
    assert state.candidate is None
    # Hook suite ran exactly once, for the A→B move.
    assert rec.hooks == [(pod["sha_a"], sha_b)]
    # Canary brought home: second deploy, from the FLEET checkout.
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    # Worktree pruned after promote.
    assert not rm.staging_dir_for(sha_b, pod["staging"]).exists()
    # Git-native pointer mirror.
    assert _run(["git", "rev-parse", rm.STABLE_TAG], pod["fleet"]) == sha_b
    assert _run(["git", "rev-parse", rm.PREVIOUS_TAG], pod["fleet"]) == pod["sha_a"]
    assert any(s.get("type") == "release_promoted" for s in rec.signals)


# ─────────────────────────────────────────────────────────────────────────────
# Operator override — "Complete soak now" (CLI `release promote` + web button)
# ─────────────────────────────────────────────────────────────────────────────


def test_release_promote_skips_remaining_soak(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "B: healthy", {"z.py": "Z = 1\n"})

    # Gate 1 passes, soak starts; the fleet stays on A.
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"
    assert _fleet_head(pod) == pod["sha_a"]

    # Mid-soak a normal tick must NOT promote (only 5 of ~60 min elapsed).
    clock.advance(5)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]

    # Operator override: promote NOW, without advancing the clock past the soak.
    res = rm.release_promote(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b

    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b
    assert state.previous["sha"] == pod["sha_a"]
    assert state.candidate is None
    # Same shared promote path as the timer firing: hooks ran, signal emitted.
    assert rec.hooks == [(pod["sha_a"], sha_b)]
    assert any(s.get("type") == "release_promoted" for s in rec.signals)


def test_release_promote_refuses_with_no_candidate(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init → stable only, nothing in flight

    res = rm.release_promote(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert not res.success
    assert res.promoted_to == ""
    assert "nothing to promote" in res.error
    assert _fleet_head(pod) == pod["sha_a"]  # fleet untouched


def test_release_promote_promotes_current_candidate_even_when_origin_advanced(pod):
    """REGRESSION (the bug "Make live now" had no effect): a promote must move
    the fleet to the candidate the operator is LOOKING AT — the on-disk soaking
    sha — even when origin/main has advanced past it.

    The old mechanism (fast-forward the soak clock, then run a full tick)
    re-fetched origin in the tick's candidate-selection step; whenever origin
    had moved past the soaking sha it SUPERSEDED the operator's candidate with
    the newer tip and restarted Gate 1 + soak — so promote moved nothing and
    the pointer stayed at stable. This test fails against that code (stable
    stays S, candidate becomes N) and passes once promote calls `_promote`
    directly. It also proves the newer origin commit is not lost: the next
    normal tick picks it up as the next candidate.
    """
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init → stable = A (S)
    stable_s = pod["sha_a"]

    # Candidate C lands and soaks (Gate 1 passes, fleet stays on S).
    sha_c = _push_commit(pod, "C: candidate", {"c.py": "C = 1\n"})
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"
    assert _fleet_head(pod) == stable_s

    # Origin advances PAST the soaking candidate: N is a descendant of C.
    sha_n = _push_commit(pod, "N: newer origin tip", {"n.py": "N = 1\n"})
    assert sha_n != sha_c

    # Operator clicks "Make live now" mid-soak.
    clock.advance(5)
    res = rm.release_promote(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)

    # It promotes C — NOT N — and the pointer actually moves off S.
    assert res.promoted_to == sha_c, "promote must move the fleet to the soaking candidate C"
    assert _fleet_head(pod) == sha_c
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_c
    assert state.stable["sha"] != stable_s, "pointer must not stay at stable"
    assert state.stable["sha"] != sha_n, "promote must not supersede C with the newer origin tip N"
    assert state.candidate is None
    # Shared promote path: hooks ran for the C move, signal emitted.
    assert rec.hooks == [(stable_s, sha_c)]
    assert any(s.get("type") == "release_promoted" for s in rec.signals)

    # The newer origin commit N is not lost — the next normal tick rides it.
    _tick(pod, deps)
    state2 = rm.load_release_state(pod["shared"])
    assert state2.candidate is not None
    assert state2.candidate["sha"] == sha_n, "newer origin commit becomes the next candidate"


def test_release_promote_refuses_non_descendant_candidate(pod):
    """A candidate that does NOT descend from stable (a history rewrite, or
    stable force-moved ahead under it) must never be promoted by the operator
    override — the shared `_promote` ancestry guard fails it closed, the fleet
    stays on stable, and the candidate is skip-listed."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init → stable = A
    stable_a = pod["sha_a"]

    # Build a sha on an UNRELATED root so it can't be an ancestor of stable.
    orphan = pod["origin"].parent / "orphan"
    _run(["git", "clone", "-q", str(pod["origin"]), str(orphan)], pod["origin"].parent)
    _run(["git", "checkout", "-q", "--orphan", "rogue"], orphan)
    _run(["git", "rm", "-rfq", "."], orphan)
    rogue_sha = _commit(orphan, "rogue: unrelated root", {"rogue.py": "R = 1\n"})
    _run(["git", "fetch", "-q", str(orphan), "rogue"], pod["fleet"])

    # Force a soaking candidate at the non-descendant sha.
    _inject_soaking_candidate(pod, rogue_sha, tier=rm.SOAK_TIER_FULL)

    res = rm.release_promote(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == stable_a, "fleet must not move to a non-descendant"
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == stable_a
    assert rogue_sha in state.skip
    assert rec.hooks == [], "no restart hooks for a refused promote"
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)


def test_release_promote_held_by_pin_set_mid_soak(pod):
    """A `release pin` written to disk after the candidate began soaking must
    win over the operator override: `_promote` re-reads the pin from disk and
    holds. The fleet stays on stable and the candidate survives for resume."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init → stable = A
    sha_b = _push_commit(pod, "B", {"p.py": "P = 1\n"})
    _tick(pod, deps)  # → soaking
    assert _fleet_head(pod) == pod["sha_a"]

    # Operator pins from another process while the candidate soaks.
    rm.release_pin(pod["shared"], repo=pod["fleet"], deps=deps)

    res = rm.release_promote(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert res.promoted_to == "", "a mid-soak pin must block the operator promote"
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == pod["sha_a"]
    assert state.pin is not None, "the operator's pin must survive"
    assert state.candidate is not None, "the candidate is held, not discarded"
    assert state.candidate["sha"] == sha_b
    assert rec.hooks == []


def test_soak_failure_restores_canary_and_blocks_promotion(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    sha_b = _push_commit(pod, "B", {"w.py": "W = 1\n"})
    _tick(pod, deps)  # → soaking

    rec.soak_healthy = False
    rec.soak_detail = "new firing signals on canary: heal:gateway_crash_loop"
    clock.advance(15)
    res = _tick(pod, deps)
    assert res.candidate_state == "failed"
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert sha_b in state.skip
    # Restore deploy: last deploy call is from the FLEET checkout.
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)
    assert rec.hooks == []


def test_pin_holds_promotion_after_soak(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    sha_b = _push_commit(pod, "B", {"v.py": "V = 1\n"})
    _tick(pod, deps)  # → soaking

    rm.release_pin(pod["shared"], repo=pod["fleet"], deps=deps)
    clock.advance(120)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]

    # Unpin → next tick promotes.
    rm.release_unpin(pod["shared"])
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b


def test_superseded_candidate_resets_clock_and_restores_canary(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    sha_b = _push_commit(pod, "B", {"u.py": "U = 1\n"})
    _tick(pod, deps)  # B soaking
    clock.advance(45)

    sha_c = _push_commit(pod, "C", {"u.py": "U = 2\n"})
    res = _tick(pod, deps)
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["sha"] == sha_c
    # B's worktree pruned; canary restored to fleet, then redeployed
    # from C's staging within the same tick (gate1 ran for C).
    assert not rm.staging_dir_for(sha_b, pod["staging"]).exists()
    fleet_restores = [d for d in rec.deploys if d == ("canary_bot", str(pod["fleet"]))]
    assert fleet_restores, "superseding a soaking candidate must restore the canary"
    assert res.candidate_state == "soaking"
    assert rec.deploys[-1] == ("canary_bot", str(rm.staging_dir_for(sha_c, pod["staging"])))

    # The 45 minutes spent soaking B must not count for C.
    clock.advance(30)
    res = _tick(pod, deps)
    assert res.promoted_to == "", "C's soak clock must have reset"
    clock.advance(31)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_c


# ─────────────────────────────────────────────────────────────────────────────
# PROOF ARTIFACT 3 — rollback restores the prior release in one command.
# ─────────────────────────────────────────────────────────────────────────────


def test_rollback_one_command_restores_previous_and_pins(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"t.py": "T = 1\n"})
    _tick(pod, deps)
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b

    rec.hooks.clear()
    res = rm.release_rollback(pod["fleet"], pod["shared"],
                              cfg=_cfg(pod), deps=deps)
    assert res.success, res.error
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == pod["sha_a"]
    assert sha_b in state.skip, "the fled sha must be skip-listed"
    assert state.pin is not None and state.pin["sha"] == pod["sha_a"]
    # Hook suite ran for the backward move.
    assert rec.hooks == [(sha_b, pod["sha_a"])]
    # Canary restored onto the rolled-back fleet code.
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    assert any(s.get("type") == "release_rolled_back" for s in rec.signals)

    # Post-rollback ticks: pinned — the bad sha cannot re-promote even
    # though origin/main still points at it.
    clock.advance(120)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]

    # Even after unpin, the fled sha stays skip-listed.
    rm.release_unpin(pod["shared"])
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]


def test_rollback_cancels_inflight_soak(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"s.py": "S = 1\n"})
    _tick(pod, deps)  # B soaking
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    sha_c = _push_commit(pod, "C", {"s.py": "S = 2\n"})
    _tick(pod, deps)  # C soaking
    state = rm.load_release_state(pod["shared"])
    assert state.candidate is not None

    res = rm.release_rollback(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert res.success
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert state.candidate is None
    assert not rm.staging_dir_for(sha_c, pod["staging"]).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Degraded mode + config + gates
# ─────────────────────────────────────────────────────────────────────────────


def test_degraded_mode_without_canary_uses_timer_only(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    cfg = rm.ReleaseConfig(mode="canary", canary_bot="", soak_minutes=30)
    _tick(pod, deps, cfg)
    sha_b = _push_commit(pod, "B", {"r.py": "R = 1\n"})

    res = _tick(pod, deps, cfg)
    assert res.degraded_no_canary
    assert res.candidate_state == "soaking"
    assert rec.deploys == [], "no canary bot — nothing to deploy"

    clock.advance(31)
    res = _tick(pod, deps, cfg)
    assert res.promoted_to == sha_b
    assert rec.deploys == []


def test_resolve_release_config_env_override(pod, monkeypatch):
    cfg = rm.resolve_release_config(pod["network"])
    assert cfg.mode == "canary"
    assert cfg.canary_bot == "canary_bot"
    assert cfg.soak_minutes == 60

    monkeypatch.setenv(rm.RELEASE_MODE_ENV, "direct")
    cfg = rm.resolve_release_config(pod["network"])
    assert cfg.mode == "direct"

    monkeypatch.delenv(rm.RELEASE_MODE_ENV)
    assert rm.resolve_release_config({}).mode == rm.DEFAULT_RELEASE_MODE
    assert rm.resolve_release_config(None).mode == rm.DEFAULT_RELEASE_MODE


def test_gate1_dep_bump_routes_through_staging_venv(pod, monkeypatch):
    """A candidate touching pyproject.toml must build/use the staging
    venv (review finding #2: without this, any dep-adding PR fails
    import-smoke forever and freezes the pipeline)."""
    calls = []

    def fake_venv(staging, deps):
        calls.append(str(staging))
        # Short-circuit failure so the rest of gate1 doesn't run real
        # subprocesses in this unit test.
        return False, "stub"

    monkeypatch.setattr(rm, "_ensure_staging_venv", fake_venv)
    ok, detail = rm.gate1_static_checks(
        pod["fleet"], ["packages/admin/pyproject.toml"], rm.ReleaseDeps())
    assert calls, "staging venv must be built when pyproject.toml changed"
    assert not ok and "stub" in detail

    calls.clear()
    monkeypatch.setattr(
        rm, "_ensure_staging_venv",
        lambda *a, **k: pytest.fail("staging venv must not build for non-dep changes"),
    )
    monkeypatch.setattr(
        rm.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    ok, _ = rm.gate1_static_checks(pod["fleet"], ["packages/admin/evolve_admin/x.py"],
                                   rm.ReleaseDeps())
    assert ok


def _recording_run(calls: list[list[str]]):
    """A subprocess.run fake that records argv and returns rc=0."""
    def _run(argv, *a, **k):
        calls.append(list(argv))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return _run


def test_gate1_plugin_build_binds_local_compiler(tmp_path, monkeypatch):
    """The canary static-check type-check must resolve the LOCAL tsc, never
    the registry. `npm exec --no -- tsc --noEmit` binds
    packages/plugin/node_modules/.bin/tsc (installed by the npm install that
    runs first) and forbids any install — the lower-risk sibling of the
    `npx --yes tsc` -> squatted tsc@2.0.4 bug PR #2993 fixed in deploy.py. A
    bare `npx tsc` could still cold-cache-flake on a minimal canary host, so
    it must not reappear here."""
    staging = tmp_path / "staging"
    (staging / "packages" / "plugin").mkdir(parents=True)

    calls: list[tuple[list[str], str]] = []

    def _run(argv, *a, **k):
        calls.append((list(argv), str(k.get("cwd"))))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(rm.subprocess, "run", _run)

    ok, info = rm._gate1_plugin_build_check(staging)
    assert ok, info

    argvs = [a for a, _ in calls]
    # 1) install runs first, in the plugin dir.
    assert argvs[0][:2] == ["npm", "install"]
    # 2) the type-check binds the local compiler deterministically.
    assert argvs[1] == ["npm", "exec", "--no", "--", "tsc", "--noEmit"]
    # 3) npx never appears — that was the cold-cache-flaky resolution path.
    assert not any("npx" in a for a in argvs)
    # 4) both legs run with cwd = the staging plugin dir.
    plugin_dir = str(staging / "packages" / "plugin")
    assert all(cwd == plugin_dir for _, cwd in calls)


def test_staging_venv_builds_via_uv(tmp_path, monkeypatch):
    """A dep-bump staging-venv refresh builds via ``uv venv --seed`` when uv
    is present — uv is ensurepip-free, so a Linux canary pod doesn't hit the
    stock-Ubuntu "ensurepip is not available" failure that ``python3 -m venv``
    raises (W7c parity with installer.ensure_evolve_venv). ``--seed`` populates
    pip so the downstream ``pip install -e`` runs through the venv's own pip;
    ``--python`` pins the live deploy interpreter."""
    deps = rm.ReleaseDeps(
        staging_venv=tmp_path / "sv",  # absent → triggers the build
        venv_python="/Users/Shared/evolve-venv/bin/python3",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_find_uv", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(rm.subprocess, "run", _recording_run(calls))

    ok, detail = rm._ensure_staging_venv(tmp_path / "staging", deps)
    assert ok, detail
    # 1) create via uv: --clear (idempotent retry) + --seed (own pip), pinned
    #    to the live deploy interpreter, at the staging venv path.
    assert calls[0] == [
        "/usr/local/bin/uv", "venv", "--clear", "--seed",
        "--python", "/Users/Shared/evolve-venv/bin/python3",
        str(tmp_path / "sv"),
    ]
    # 2) pip install -e the staging admin pkg through the seeded venv's pip.
    assert calls[1][0] == str(tmp_path / "sv" / "bin" / "pip")
    assert "-e" in calls[1]


def test_staging_venv_falls_back_to_stdlib_when_uv_absent(tmp_path, monkeypatch):
    """Host with python3-venv but no uv: ``_find_uv`` → None → fall back to the
    stdlib ``python -m venv`` builder (byte-identical to the pre-W7c shape that
    already works on macOS), so neither prerequisite alone blocks the build."""
    deps = rm.ReleaseDeps(
        staging_venv=tmp_path / "sv",
        venv_python="/Users/Shared/evolve-venv/bin/python3",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_find_uv", lambda: None)
    monkeypatch.setattr(rm.subprocess, "run", _recording_run(calls))

    ok, detail = rm._ensure_staging_venv(tmp_path / "staging", deps)
    assert ok, detail
    assert calls[0] == [
        "/Users/Shared/evolve-venv/bin/python3", "-m", "venv", str(tmp_path / "sv"),
    ]
    # the pip-install leg is identical regardless of the create path.
    assert calls[1][0] == str(tmp_path / "sv" / "bin" / "pip")


def test_staging_venv_skips_create_when_python_present(tmp_path, monkeypatch):
    """Idempotent refresh: an existing staging python skips venv-create
    entirely (only the pip install runs), and uv is never even resolved —
    the same guard the pre-W7c code had, preserved through the port."""
    sv = tmp_path / "sv"
    (sv / "bin").mkdir(parents=True)
    (sv / "bin" / "python3").write_text("")  # python present → no create
    deps = rm.ReleaseDeps(staging_venv=sv, venv_python="/x/bin/python3")
    monkeypatch.setattr(
        rm, "_find_uv",
        lambda: pytest.fail("uv must not be resolved when the staging venv exists"))
    calls: list[list[str]] = []
    monkeypatch.setattr(rm.subprocess, "run", _recording_run(calls))

    ok, detail = rm._ensure_staging_venv(tmp_path / "staging", deps)
    assert ok, detail
    assert len(calls) == 1 and calls[0][0] == str(sv / "bin" / "pip")


def test_skip_list_pruned_at_promote(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)

    # B fails, C (which contains B) succeeds → B's skip entry is moot
    # once C is stable, and must be pruned.
    rec.gate1_ok = False
    sha_b = _push_commit(pod, "B: bad", {"q.py": "Q = 1\n"})
    _tick(pod, deps)
    state = rm.load_release_state(pod["shared"])
    assert sha_b in state.skip

    rec.gate1_ok = True
    sha_c = _push_commit(pod, "C: fixed", {"q.py": "Q = 2\n"})
    _tick(pod, deps)
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_c
    state = rm.load_release_state(pod["shared"])
    assert sha_b not in state.skip


def test_version_for_sha_parses_pr_number(pod):
    sha = _push_commit(pod, "fix: something useful (#1234)", {"p.py": "P = 1\n"})
    v = rm.version_for_sha(pod["seed"], sha)
    assert v.endswith(".1234")
    assert len(v.split(".")) == 3


# ─────────────────────────────────────────────────────────────────────────────
# D-2 residual: install.json top-level pod version reconcile (_record_pod_version)
#
# The staleness this guards against: install.json read 2026.0611.2759 while
# every bot ran 2026.0614.2884, because the top-level field is otherwise only
# rewritten by a full install/upgrade — a *promote* would leave it frozen. The
# reconcile is DISPLAY-ONLY: it rewrites the cosmetic top-level `version` and
# never touches bot_versions or the YYYY.MMDD.PR identity used by
# version_for_sha / version-equality (the lagging-bot sweep depends on that).
# ─────────────────────────────────────────────────────────────────────────────


def _write_install_json(shared, payload):
    (shared / "install.json").write_text(json.dumps(payload, indent=2))


def test_record_pod_version_reconciles_stale_top_level(pod):
    shared = pod["shared"]
    _write_install_json(shared, {
        "version": "2026.0611.2759",                 # frozen at last full upgrade
        "bots": ["canary_bot", "other_bot"],
        "bot_versions": {"canary_bot": {"version": "2026.0614.2884"}},
    })
    result = rm.ReleaseTickResult()
    rm._record_pod_version("2026.0614.2884", shared, result)

    data = json.loads((shared / "install.json").read_text())
    # Top-level display version now tracks the promoted release.
    assert data["version"] == "2026.0614.2884"
    assert data.get("version_updated_at")
    # bot_versions (the deploy-truth + equality input) is untouched.
    assert data["bot_versions"] == {"canary_bot": {"version": "2026.0614.2884"}}
    assert data["bots"] == ["canary_bot", "other_bot"]


def test_record_pod_version_noop_when_already_current(pod):
    shared = pod["shared"]
    _write_install_json(shared, {"version": "2026.0614.2884", "bot_versions": {}})
    result = rm.ReleaseTickResult()
    rm._record_pod_version("2026.0614.2884", shared, result)
    data = json.loads((shared / "install.json").read_text())
    # No-op leaves the file as-is — in particular no version_updated_at stamp.
    assert "version_updated_at" not in data


def test_record_pod_version_is_best_effort_on_missing_file(pod):
    shared = pod["shared"]  # no install.json written
    result = rm.ReleaseTickResult()
    # Missing install.json (the first install owns the initial stamp) and a
    # falsy version must both be silent no-ops — a cosmetic stamp never aborts
    # a pointer move, and never raises.
    rm._record_pod_version("2026.0614.2884", shared, result)
    rm._record_pod_version("", shared, result)
    assert not (shared / "install.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Review-finding regressions: the per-tick sweeps must not fight the soak
# ─────────────────────────────────────────────────────────────────────────────


def test_lagging_sweep_exempts_canary_during_soak(pod):
    """Design-review finding #1: the lagging-bot redeploy sweep runs
    every 15-min tick and redeploys any bot whose install.json stamp
    differs from EVOLVE_VERSION. The canary runs the CANDIDATE version
    during a soak by design — without the exemption, the sweep yanks it
    back to stable within one tick and the soak passes on fabricated
    evidence."""
    from evolve_admin import repo_puller as rp

    # install.json: both bots lag the live EVOLVE_VERSION.
    (pod["shared"] / "install.json").write_text(json.dumps({
        "bot_versions": {
            "canary_bot": {"version": "2020.0101.1"},
            "other_bot": {"version": "2020.0101.1"},
        },
    }))
    # An active soak in release.json.
    rm.save_release_state(rm.ReleaseState(
        stable={"sha": pod["sha_a"], "version": "x", "promoted_at": "t"},
        candidate={"sha": "f" * 40, "first_seen_at": "t",
                   "soak_started_at": "t", "state": "soaking", "failure": ""},
    ), pod["shared"])

    deployed: list[str] = []

    def fake_deploy(bot_id, **kw):
        deployed.append(bot_id)
        return type("R", (), {"success": True})()

    succeeded, errors = rp._redeploy_lagging_bots(
        pod["fleet"], pod["shared"],
        deploy_fn=fake_deploy, record_fn=lambda *a, **k: None,
    )
    assert "other_bot" in deployed, f"non-canary lagging bot must still redeploy (errors={errors})"
    assert "canary_bot" not in deployed, "canary must be exempt while soaking"

    # Once the soak resolves, the exemption lifts.
    deployed.clear()
    rm.save_release_state(rm.ReleaseState(
        stable={"sha": pod["sha_a"], "version": "x", "promoted_at": "t"},
        candidate=None,
    ), pod["shared"])
    rp._redeploy_lagging_bots(
        pod["fleet"], pod["shared"],
        deploy_fn=fake_deploy, record_fn=lambda *a, **k: None,
    )
    assert "canary_bot" in deployed, "exemption must lift when no soak is active"


def test_deploy_drift_detector_exempts_canary(pod):
    """Same carve-out for the deploy_drift_monitor Signal producer."""
    import importlib.util
    analyzer = _ADMIN_DIR.parent / "analyzer"
    spec = importlib.util.spec_from_file_location(
        "deploy_drift_monitor_under_test", analyzer / "deploy_drift_monitor.py")
    ddm = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(analyzer))
    try:
        spec.loader.exec_module(ddm)
    finally:
        sys.path.remove(str(analyzer))

    network = pod["network"]
    install = {"bot_versions": {
        "canary_bot": {"version": "2020.0101.1"},
        "other_bot": {"version": "2020.0101.1"},
    }}
    spec_no_exempt = ddm.detect_deploy_drift(network, install, "2026.0610.99")
    assert spec_no_exempt is not None
    assert "canary_bot" in spec_no_exempt["body"]

    spec_exempt = ddm.detect_deploy_drift(
        network, install, "2026.0610.99", exempt_bots={"canary_bot"})
    assert spec_exempt is not None
    assert "canary_bot" not in spec_exempt["body"]
    assert "other_bot" in spec_exempt["body"]


# ─────────────────────────────────────────────────────────────────────────────
# Independent-review regressions (2026-06-10)
# ─────────────────────────────────────────────────────────────────────────────


def test_pointer_persists_even_when_hooks_kill_the_caller(pod):
    """Review BLOCKER: the hook suite kickstarts admin-ui — which IS the
    calling process for web rollbacks. If the pointer isn't persisted
    before the hooks run, the next tick's pointer-repair silently undoes
    the operator's rollback. Simulate the kill with hooks that raise."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"k.py": "K = 1\n"})
    _tick(pod, deps)
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b

    def killed_hooks(repo, before, after):
        raise RuntimeError("simulated admin-ui kickstart killing the caller")

    deps.hooks_fn = killed_hooks
    res = rm.release_rollback(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    # The rollback as a whole reports what it can; the POINTER must be
    # durable regardless.
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == pod["sha_a"], "pointer must persist before hooks"
    assert state.pin is not None
    assert sha_b in state.skip
    assert _fleet_head(pod) == pod["sha_a"]

    # The next tick must NOT repair the fleet back to the fled sha.
    deps.hooks_fn = rec.run_hooks
    res = _tick(pod, deps)
    assert _fleet_head(pod) == pod["sha_a"]
    assert res.promoted_to == ""


def test_promote_persists_pointer_even_when_hooks_raise(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"k2.py": "K = 2\n"})
    _tick(pod, deps)
    clock.advance(61)

    def killed_hooks(repo, before, after):
        raise RuntimeError("simulated kill")

    deps.hooks_fn = killed_hooks
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b
    assert state.candidate is None
    assert _fleet_head(pod) == sha_b


# ─────────────────────────────────────────────────────────────────────────────
# B1 — risk-tier the canary soak (skip / short / full path policy)
# Spec: internal/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D1.
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_soak_tier_table():
    """The pure path→tier table: inert→skip, ordinary runtime→short,
    privileged/irreversible→full, and the fail-safe default→full."""
    c = rm.classify_soak_tier
    # skip — inert, never executed by a daemon
    assert c(["internal/spec-x.md", "README.md"]) == rm.SOAK_TIER_SKIP
    assert c(["packages/admin/tests/test_x.py"]) == rm.SOAK_TIER_SKIP
    assert c(["packages/admin/evolve_admin/web/static/js/app.js"]) == rm.SOAK_TIER_SKIP
    # short — ordinary reversible runtime
    assert c(["packages/analyzer/generators/model_discovery/generator.py"]) == rm.SOAK_TIER_SHORT
    assert c(["packages/admin/evolve_admin/web/routes_arbiter.py"]) == rm.SOAK_TIER_SHORT
    # full — irreversible / privileged
    assert c(["packages/admin/evolve_admin/deploy.py"]) == rm.SOAK_TIER_FULL
    assert c(["packages/admin/evolve_admin/keystore.py"]) == rm.SOAK_TIER_FULL
    assert c(["packages/analyzer/signals/store.py"]) == rm.SOAK_TIER_FULL
    assert c(["packages/analyzer/delivery_monitor.py"]) == rm.SOAK_TIER_FULL
    assert c(["packages/plugin/src/index.ts"]) == rm.SOAK_TIER_FULL
    assert c(["packages/admin/evolve_admin/applications/migrate_v7.py"]) == rm.SOAK_TIER_FULL
    # fail-safe default — empty diff, unknown extension, mixed risk all → full
    assert c([]) == rm.SOAK_TIER_FULL
    assert c(["Makefile"]) == rm.SOAK_TIER_FULL
    assert c(["infra/main.tf"]) == rm.SOAK_TIER_FULL
    assert c(["docs/x.md", "packages/admin/evolve_admin/deploy.py"]) == rm.SOAK_TIER_FULL
    # most-cautious-wins across paths: docs (skip) + generator (short) → short
    assert c(["docs/x.md", "packages/analyzer/generators/g/generator.py"]) == rm.SOAK_TIER_SHORT
    # D7 tier-derived RESIDUAL ceiling: an active-validated skip/short
    # collapses to 0 (promote at active-pass), full keeps the configured
    # dwell. The degraded fallback + clamp are exercised in
    # test_tier_residual_minutes_d7.
    assert rm.tier_residual_minutes(rm.SOAK_TIER_SKIP, 60, active_validated=True) == 0
    assert rm.tier_residual_minutes(rm.SOAK_TIER_SHORT, 60, active_validated=True) == 0
    assert rm.tier_residual_minutes(rm.SOAK_TIER_FULL, 60, active_validated=True) == 60


def test_tier_residual_minutes_d7():
    """D7 residual ceilings (supersede B3's passive windows).

    The timer is no longer the primary promote gate — active validation is.
    `tier_residual_minutes` returns the per-tier *residual* dwell that
    remains AFTER active validation passes:
      - skip  → 0 always.
      - short → 0 when active-validated (promote at active-pass time); the
                legacy B3 window (min(SHORT_SOAK_MINUTES, full)) ONLY when
                NOT validated (degraded / no canary) — never silently
                fast-track a candidate nothing exercised.
      - full  → the configured window regardless of validation (the dwell
                is the point for the irreversible minority)."""
    m = rm.tier_residual_minutes
    # The headline tune: the full-tier residual default dropped 60 → 15;
    # SHORT_SOAK_MINUTES survives only as the degraded-short fallback.
    assert rm.DEFAULT_SOAK_MINUTES == 15
    assert rm.SHORT_SOAK_MINUTES == 15

    # Active-validated: skip + short collapse to 0; full keeps its dwell.
    assert m(rm.SOAK_TIER_SKIP, 60, active_validated=True) == 0
    assert m(rm.SOAK_TIER_SHORT, 60, active_validated=True) == 0
    assert m(rm.SOAK_TIER_SHORT, 0, active_validated=True) == 0
    assert m(rm.SOAK_TIER_FULL, 60, active_validated=True) == 60
    assert m(rm.SOAK_TIER_FULL, 30, active_validated=True) == 30

    # NOT active-validated (degraded / no canary): short falls back to the
    # legacy passive window, clamped ≤ full so it never out-soaks full.
    assert m(rm.SOAK_TIER_SHORT, 60, active_validated=False) == 15
    assert m(rm.SOAK_TIER_SHORT, 10, active_validated=False) == 10   # clamp
    assert m(rm.SOAK_TIER_SHORT, 15, active_validated=False) == 15
    assert m(rm.SOAK_TIER_SHORT, 0, active_validated=False) == 0
    # skip + full are unchanged by validation state (regression guards).
    assert m(rm.SOAK_TIER_SKIP, 60, active_validated=False) == 0
    assert m(rm.SOAK_TIER_FULL, 60, active_validated=False) == 60

    # Invariant across the config range and BOTH validation states: a short
    # residual never exceeds the full residual.
    for cfg_min in (0, 5, 10, 15, 60, 120):
        for av in (True, False):
            assert (m(rm.SOAK_TIER_SHORT, cfg_min, active_validated=av)
                    <= m(rm.SOAK_TIER_FULL, cfg_min, active_validated=av))


def test_runtime_injected_docs_are_not_skip_tier():
    """Regression: docs/system/*.md are read at daemon import and injected
    into every bot's system prompt (session_surface.py), so a change reaches
    the whole fleet's behavior — they are NOT inert and MUST classify full,
    never the skip-tier the bare `.md`/`docs/` rules would otherwise grant.
    The skip-escape this guards: a fleet-wide conduct/prompt change (or a
    marker-breaking edit that silently drops the conduct rules) promoting
    with no soak window."""
    c = rm.classify_soak_tier
    f = rm._soak_tier_for_path
    # The three runtime-injected docs loaded by session_surface.py.
    assert f("docs/system/POD_CONDUCT.md") == rm.SOAK_TIER_FULL
    assert f("docs/system/RUNTIME_NOTES.md") == rm.SOAK_TIER_FULL
    assert f("docs/system/COHERENCE_VOCAB.md") == rm.SOAK_TIER_FULL
    # Lone candidate and docs-only-looking mixed candidate both → full.
    assert c(["docs/system/POD_CONDUCT.md"]) == rm.SOAK_TIER_FULL
    assert c(["docs/spec.md", "docs/system/POD_CONDUCT.md"]) == rm.SOAK_TIER_FULL
    # Ordinary docs stay skip — the carve-out is scoped to docs/system/.
    assert f("internal/spec-x.md") == rm.SOAK_TIER_SKIP
    assert c(["internal/spec-x.md", "README.md"]) == rm.SOAK_TIER_SKIP


def test_docs_only_candidate_skips_soak_and_promotes(pod):
    """Headline friction win + the live #4c68ac50 regression: a docs-only
    candidate is tier `skip` → Gate 1 runs, then promote in the SAME tick
    with NO soak window. No staging canary deploy means the passive soak's
    ambient app-quality signals (app_discoverability_*, app_permission_
    drift) can never be miscounted as a docs-change failure."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "docs(model-tiers): spec addendum", {
        "internal/spec-addendum-9.md": "# Addendum 9\n\nprose only\n",
    })

    # One tick: Gate 1 passes, tier=skip, promote immediately — no clock
    # advance, no soak window.
    res = _tick(pod, deps)
    assert res.soak_tier == "skip"
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b

    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b
    assert state.candidate is None
    # Promote ran the shared path: hooks once, signal emitted.
    assert rec.hooks == [(pod["sha_a"], sha_b)]
    assert any(s.get("type") == "release_promoted" for s in rec.signals)
    # No soak: the candidate was NEVER deployed to the canary from staging.
    # The only deploy is the post-promote restore from the FLEET checkout.
    assert rec.deploys == [("canary_bot", str(pod["fleet"]))]

    # Rollback is still the net — the previous pointer survives and one
    # command restores the fleet.
    assert state.previous["sha"] == pod["sha_a"]
    rb = rm.release_rollback(pod["fleet"], pod["shared"], cfg=_cfg(pod), deps=deps)
    assert rb.fleet_sha == pod["sha_a"]
    assert _fleet_head(pod) == pod["sha_a"]


def test_skip_tier_still_runs_gate1(pod):
    """Gate 1 runs for EVERY tier, including skip: a docs-only candidate
    whose Gate 1 fails must still fail (never promote)."""
    rec, clock = Recorder(), Clock()
    rec.gate1_ok = False
    rec.gate1_detail = "import-smoke crashed"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "docs: but Gate 1 trips", {"docs/x.md": "# x\n"})
    res = _tick(pod, deps)
    assert res.candidate_state == "failed"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert sha_b in state.skip


def test_unknown_path_defaults_to_full_soak(pod):
    """Fail-safe regression: an unmatched / ambiguous path is tier `full`
    — it does NOT promote immediately; it soaks the full window."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "chore: opaque change", {"weird/thing.xyz": "data\n"})
    res = _tick(pod, deps)
    assert res.soak_tier == "full"
    assert res.candidate_state == "soaking"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]
    # The canary WAS deployed from staging (a real soak), unlike skip.
    assert rec.deploys == [("canary_bot", str(rm.staging_dir_for(sha_b, pod["staging"])))]

    # Still soaking partway through the full window.
    clock.advance(30)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    # Full window elapses → promote.
    clock.advance(31)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b


def test_generator_candidate_is_short_tier(pod):
    """The #2796-class re-proof under D7: a generator-copy change
    (coalesce/human_title in the generator's Python) is tier `short`, and a
    `short` candidate is now ACTIVE-GATED — the gateway + generator probe
    pass and the D4 health is clean, so it promotes at active-pass time with
    NO residual window (was the 15-min B3 floor, was the 60-min #2796 hour).
    The timer no longer gates `short`; active validation does — and it all
    happens in the SAME tick, not on the next 15-min puller tick."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "feat(model_discovery): coalesce per-provider", {
        "packages/analyzer/generators/model_discovery/generator.py":
            "HUMAN_TITLE = 'reworded'\n",
    })
    # ONE tick, NO clock advance: gate1 → canary deploy → active probe OK →
    # D4 health clean → residual 0 → promote, all at active-pass time.
    res = _tick(pod, deps)
    assert res.soak_tier == "short"
    assert res.soak_probe == rm.SOAK_PROBE_OK
    assert res.promoted_to == sha_b, "active-validated short promotes same tick"
    assert _fleet_head(pod) == sha_b
    # The residual ceiling for an active-validated short is 0 (the headline
    # #2796 friction win taken to its limit by D7).
    assert rm.tier_residual_minutes(rm.SOAK_TIER_SHORT, 60,
                                    active_validated=True) == 0
    # Promote ran the real path: hooks fired once, canary brought home.
    assert rec.hooks == [(pod["sha_a"], sha_b)]
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))


def test_delivery_auth_path_candidate_is_full_tier(pod):
    """Delivery / auth / sudoers / store paths are tier `full`."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    _push_commit(pod, "fix(delivery): send path", {
        "packages/analyzer/delivery_monitor.py": "SENT = True\n",
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "full"
    assert res.candidate_state == "soaking"


def test_release_soak_bumps_candidate_up_a_tier(pod):
    """`release soak <tier>` bumps a candidate UP (short → full); the minutes
    form sets an explicit longer window. Exercised on a candidate sitting in
    soak — D7 makes an active-validated short auto-promote, so the operator's
    add-caution knob is meaningful for candidates caught mid-dwell."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "feat(generator): x", {
        "packages/analyzer/generators/g/generator.py": "X = 1\n",
    })
    _inject_soaking_candidate(pod, sha_b, tier="short")

    # Bump the tier up: short → full.
    msg = rm.release_set_soak(pod["shared"], "full", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "tier full" in msg
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["soak_tier"] == "full"

    # Minutes form: set an explicit longer window (more caution).
    msg = rm.release_set_soak(pod["shared"], "120", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "120 min" in msg
    state = rm.load_release_state(pod["shared"])
    assert rm._candidate_soak_minutes(state.candidate, _cfg(pod)) == 120
    # The longer window holds: 61 min in (past the default 60) still soaks.
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert res.candidate_state == "soaking"
    # Past 120 → promote.
    clock.advance(60)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b


def test_release_soak_refuses_downbump(pod):
    """The override only ADDS caution: a request that would shorten the
    soak is refused (pointing at `release promote`); `skip` is rejected."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    _push_commit(pod, "B", {"weird/full.xyz": "x\n"})  # → full, 60 min
    res = _tick(pod, deps)
    assert res.soak_tier == "full"

    # Shorter minutes → refused, override unchanged.
    msg = rm.release_set_soak(pod["shared"], "30", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "shorten the soak" in msg
    state = rm.load_release_state(pod["shared"])
    assert "soak_override_minutes" not in state.candidate

    # skip is never accepted here.
    msg = rm.release_set_soak(pod["shared"], "skip", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "not allowed" in msg
    assert "release promote" in msg


def test_release_soak_override_directions_d7(pod):
    """The `release soak` shorten-guard with the D7 residual ceilings: it
    computes both sides via `tier_residual_minutes` against the candidate's
    active-validation verdict, so lengthening adds caution and shortening is
    refused.

    - an active-validated `short`(residual 0) bumped `release soak full`
      (residual 60) → 0→60 ALLOWED.
    - a `full`(60) candidate asked `release soak short`(0) → 60→0 REFUSED
      (the override only adds caution; `release promote` goes faster)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    # An active-validated short candidate → residual 0 (would auto-promote
    # via the tick; injected directly so the shorten-guard is observable).
    sha_b = _push_commit(pod, "feat(generator): x", {
        "packages/analyzer/generators/g/generator.py": "X = 1\n",
    })
    _inject_soaking_candidate(pod, sha_b, tier="short", active_validated=True)
    state = rm.load_release_state(pod["shared"])
    assert rm._candidate_soak_minutes(state.candidate, _cfg(pod)) == 0

    # Lengthening short(0) → full(60) is ALLOWED (adds caution).
    msg = rm.release_set_soak(pod["shared"], "full", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "tier full" in msg
    assert "shorten" not in msg
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["soak_tier"] == "full"
    assert rm._candidate_soak_minutes(state.candidate, _cfg(pod)) == 60

    # Now the full(60) candidate asked back to short(0) is REFUSED — the
    # shorten-guard computes 60 → 0 and blocks it; the tier is untouched.
    msg = rm.release_set_soak(pod["shared"], "short", repo=pod["fleet"],
                              cfg=_cfg(pod), deps=deps)
    assert "shorten the soak" in msg
    assert "release promote" in msg
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["soak_tier"] == "full"


# ─────────────────────────────────────────────────────────────────────────────
# B2 — active canary probe during soak (D2)
#
# Spec: internal/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D2.
# The probe is selected from the SAME diff classification as the tier and
# runs once at soak entry. A regression fails the soak in that tick; a
# tooling fault fails OPEN with a loud degraded Signal; skip-tier never
# probes. The probe NEVER changes which tier a candidate gets (B1 owns
# that).
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_soak_probe_table():
    """The pure path→probe selector. The D5 gateway liveness probe is
    ALWAYS first (not diff-derived); the diff selects the tail: generator
    dir → run that generator, delivery/send token → send round-trip, route
    module → exercise it, everything else → no *extra* probe (passive
    backstop). De-duplicated."""
    c = rm.classify_soak_probe
    GW = {"kind": "gateway", "target": None}
    # Gateway probe is always-on — present even for an empty / opaque diff.
    assert c([]) == [GW]
    assert c(["docs/x.md", "Makefile"]) == [GW]
    # package-level helpers under generators/ (no <id> dir) → gateway only
    assert c(["packages/analyzer/generators/__init__.py"]) == [GW]
    assert c(["packages/analyzer/generators/_signal_consumer.py"]) == [GW]
    # ordinary runtime with no exercisable surface → gateway only
    assert c(["packages/admin/evolve_admin/some_helper.py"]) == [GW]
    # generator dir → gateway + one probe targeting the <id>
    assert c(["packages/analyzer/generators/model_discovery/observe.py"]) == [
        GW, {"kind": "generator", "target": "model_discovery"}]
    # delivery / message-send → gateway + send round-trip (no per-file target)
    assert c(["packages/analyzer/delivery_monitor.py"]) == [
        GW, {"kind": "send", "target": None}]
    # admin route module → gateway + route probe targeting the module stem
    assert c(["packages/admin/evolve_admin/web/routes_arbiter.py"]) == [
        GW, {"kind": "route", "target": "routes_arbiter"}]
    assert c(["packages/admin/evolve_admin/web/release_routes.py"]) == [
        GW, {"kind": "route", "target": "release_routes"}]
    # multi-kind diff → gateway always first, then de-duplicated diff probes
    plan = c([
        "packages/analyzer/generators/model_discovery/observe.py",
        "packages/analyzer/generators/model_discovery/value_line.py",
        "packages/admin/evolve_admin/web/routes_arbiter.py",
    ])
    assert plan[0] == GW, "gateway probe is always first"
    assert {(p["kind"], p["target"]) for p in plan} == {
        ("gateway", None), ("generator", "model_discovery"), ("route", "routes_arbiter")}
    assert len(plan) == 3  # gateway + coalesced model_discovery + route


def test_generator_candidate_active_probe_regression_fails_fast(pod):
    """Generator candidate (short tier) + active generator-run probe:
    a regression fails the soak in the SAME tick — minutes, not the hour —
    canary restored, fleet unchanged, sha skip-listed."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = "generator model_discovery: observe import failed: NameError"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "feat(model_discovery): buggy observe", {
        "packages/analyzer/generators/model_discovery/observe.py":
            "raise RuntimeError('boom')\n",
    })
    # One tick: gate1 pass → tier short → canary deploy → active probe →
    # REGRESSION → fail before the soak window even opens.
    res = _tick(pod, deps)
    assert res.soak_tier == "short"
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"], "fleet unchanged"
    # The probe ran exactly the plan the diff selected — D5 gateway probe
    # always first, then the generator probe.
    assert rec.probes == [[{"kind": "gateway", "target": None},
                           {"kind": "generator", "target": "model_discovery"}]]
    state = rm.load_release_state(pod["shared"])
    assert sha_b in state.skip
    # Canary deployed from staging, then RESTORED from the fleet checkout.
    assert len(rec.deploys) == 2
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)
    assert rec.hooks == [], "a failed candidate never runs the promote hooks"


def test_delivery_candidate_active_send_probe_regression_fails(pod):
    """Delivery-path candidate (full tier) + active send probe: a send
    regression fails the soak; canary restored; fleet unchanged."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = "send: end-to-end FAILED: telegram api error"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "fix(delivery): adjust send path", {
        "packages/analyzer/delivery_monitor.py": "SENT = True\n",
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "full"
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert _fleet_head(pod) == pod["sha_a"]
    assert rec.probes == [[{"kind": "gateway", "target": None},
                           {"kind": "send", "target": None}]]
    state = rm.load_release_state(pod["shared"])
    assert sha_b in state.skip
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))


def test_route_candidate_active_probe_error_fails(pod):
    """Admin-route candidate: the route probe exercises the changed route;
    a 5xx / import error fails the soak."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = "route routes_arbiter: import failed: NameError"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "fix(routes): arbiter handler", {
        "packages/admin/evolve_admin/web/routes_arbiter.py": "BROKEN = 1\n",
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "short"
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert rec.probes == [[{"kind": "gateway", "target": None},
                           {"kind": "route", "target": "routes_arbiter"}]]
    assert sha_b in rm.load_release_state(pod["shared"]).skip
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))


def test_probe_tooling_error_fails_open_with_degraded_signal(pod):
    """A probe TOOLING fault (the probe could not run) must fail OPEN — the
    candidate is NOT failed — but emit a loud degraded Signal. A release is
    never failed on our own fault; the soak then completes via the passive
    backstop + timer."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_ERROR
    rec.probe_detail = "active probe could not launch: FileNotFoundError"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "feat(model_discovery): fine change", {
        "packages/analyzer/generators/model_discovery/observe.py": "OK = 1\n",
    })
    res = _tick(pod, deps)
    assert res.soak_probe == rm.SOAK_PROBE_ERROR
    assert res.candidate_state == "soaking", "fail OPEN — candidate keeps soaking"
    assert res.promoted_to == ""
    # …but the degraded probe is visible, not silent.
    assert any(s.get("type") == "release_soak_probe_degraded" for s in rec.signals)
    # No candidate-failure signal, and the sha is NOT skip-listed.
    assert not any(s.get("type") == "release_canary_failed" for s in rec.signals)
    assert sha_b not in rm.load_release_state(pod["shared"]).skip
    # Passive backstop + timer carry the soak to a normal promote.
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b


def test_skip_tier_runs_no_active_probe(pod):
    """Docs-only candidate → tier skip → promotes immediately; the active
    probe never runs (B1 promotes skip before the soak/probe step)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "docs: prose only", {"docs/x.md": "# x\n"})
    res = _tick(pod, deps)
    assert res.soak_tier == "skip"
    assert res.promoted_to == sha_b
    assert rec.probes == [], "skip never reaches the active probe"
    assert res.soak_probe == ""


def test_short_tier_without_probe_target_promotes_on_gateway_pass(pod):
    """A short-tier candidate whose diff has no exercisable surface (an
    ordinary helper .py, not a generator/route/send path) runs ONLY the D5
    always-on gateway probe. Under D7 a passing gateway probe (+ clean D4
    health) is enough — the candidate is active-validated → residual 0 → it
    promotes at active-pass time, in the same tick, no window."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "refactor: helper tweak", {
        "packages/admin/evolve_admin/some_helper.py": "VALUE = 2\n",
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "short"
    assert rec.probes == [[{"kind": "gateway", "target": None}]], \
        "no exercisable diff surface → gateway probe only"
    assert res.soak_probe == rm.SOAK_PROBE_OK
    # D7: gateway-validated short → residual 0 → promotes same tick.
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b


def test_pin_set_mid_soak_holds_promotion(pod):
    """Review #7: a `release pin` written to disk while the tick is
    mid-pipeline must win over the tick's in-memory snapshot."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    _push_commit(pod, "B", {"k3.py": "K = 3\n"})
    _tick(pod, deps)  # soaking
    clock.advance(61)

    # Operator pins from another process between ticks (the soaking
    # state on disk does not have the pin in the tick's snapshot until
    # the promote-time re-check).
    rm.release_pin(pod["shared"], repo=pod["fleet"], deps=deps)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"]
    state = rm.load_release_state(pod["shared"])
    assert state.pin is not None, "the operator's pin must survive the tick's save"


def test_move_fleet_quarantines_colliding_untracked_files(pod):
    """Review #2: `git reset --hard` silently overwrites an untracked
    file that collides with a tracked path at the target sha. The sweep
    must quarantine divergent content first."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B: adds newfile", {"newfile.py": "CANON = 1\n"})

    # An operator/agent left a divergent untracked file at the colliding
    # path in the fleet checkout.
    (pod["fleet"] / "newfile.py").write_text("PRECIOUS LOCAL CONTENT\n")

    _run(["git", "fetch", "-q", "origin"], pod["fleet"])
    result = rm.ReleaseTickResult()
    assert rm._move_fleet(pod["fleet"], sha_b, deps, result)
    assert (pod["fleet"] / "newfile.py").read_text() == "CANON = 1\n"
    qroot = pod["staging"].parent / "quarantine"
    quarantined = list(qroot.glob("*/newfile.py")) if qroot.exists() else []
    assert any(
        p.read_text() == "PRECIOUS LOCAL CONTENT\n" for p in quarantined
    ), f"divergent untracked file must be quarantined, steps: {result.steps}"


# ─────────────────────────────────────────────────────────────────────────────
# Auditor-grade adversarial review of the soak risk-tier feature
# (B1 #2812 / #2819, B2 #2820). Each test is a crafted attack input with an
# input → expected → actual assertion. A privileged path that classifies
# skip/short, a fail-open-to-skip, or an inverted probe verdict is fatal.
# ─────────────────────────────────────────────────────────────────────────────


def test_runtime_injected_md_outside_docs_system_is_full_tier():
    """FINDING (major): #2819 made docs/system/*.md full-tier, but the bare
    `.md`→skip rule still skips repo .md that ship VERBATIM to a daemon/bot
    at runtime/deploy. Those are the same hazard class — a change reaches
    fleet behavior with no compile gate — yet would promote to all bots in
    ≤15 min with NO soak. The sharpest is proposal_synthesizer/charter.md:
    the synthesizer LLM's charter, re-read every synthesis run on the LIVE
    fleet (synthesizer.py::_load_charter)."""
    f = rm._soak_tier_for_path
    c = rm.classify_soak_tier
    # The live-fleet synthesizer charter (read every synthesis run).
    assert f("packages/analyzer/proposal_synthesizer/charter.md") == rm.SOAK_TIER_FULL
    # The RSI actor's own identity + skills, shipped verbatim to the bot.
    assert f("packages/analyzer/evolve_bot/SOUL.md") == rm.SOAK_TIER_FULL
    assert f("packages/analyzer/evolve_bot/AGENTS.md") == rm.SOAK_TIER_FULL
    assert f("packages/analyzer/evolve_bot/skills/evolve-knowledge/SKILL.md") == rm.SOAK_TIER_FULL
    # Bot-workspace prompt templates seeded into every bot at setup.
    assert f("packages/admin/evolve_admin/templates/bot_workspace/SOUL.md") == rm.SOAK_TIER_FULL
    # An app procedure installed as the app's runbook.
    assert f("packages/analyzer/evolve_apps/security-cve-scan/procedure.md") == rm.SOAK_TIER_FULL
    # A lone charter candidate must NOT skip-promote.
    assert c(["packages/analyzer/proposal_synthesizer/charter.md"]) == rm.SOAK_TIER_FULL


def test_runtime_injected_md_fix_does_not_overbroaden():
    """The carve-out is scoped to `.md` under the runtime-content roots:
    ordinary docs and package READMEs still skip, and `.py` siblings stay
    `short` (the fix must not soak every code change in those dirs)."""
    f = rm._soak_tier_for_path
    # Plain docs + top-level package READMEs remain inert → skip.
    assert f("internal/spec-x.md") == rm.SOAK_TIER_SKIP
    assert f("packages/admin/README.md") == rm.SOAK_TIER_SKIP
    assert f("packages/analyzer/breakers/README.md") == rm.SOAK_TIER_SKIP
    # The synthesizer's CODE is ordinary reversible runtime → short.
    assert f("packages/analyzer/proposal_synthesizer/synthesizer.py") == rm.SOAK_TIER_SHORT


def test_classifier_normalizes_adversarial_path_shapes():
    """Privileged paths must classify full regardless of separator / prefix
    / whitespace noise — none of these shapes may sneak a privileged file
    past the token table into skip/short."""
    f = rm._soak_tier_for_path
    P = "packages/admin/evolve_admin/keystore.py"  # token 'keystore' → full
    assert f(P) == rm.SOAK_TIER_FULL
    assert f("./" + P) == rm.SOAK_TIER_FULL                     # leading ./
    assert f("/" + P) == rm.SOAK_TIER_FULL                      # leading /
    assert f("  " + P + "  ") == rm.SOAK_TIER_FULL              # surrounding ws
    assert f(P.replace("/", "\\")) == rm.SOAK_TIER_FULL         # windows seps
    # Leading ./ must not break the docs/system runtime-injected guard.
    assert f("./docs/system/POD_CONDUCT.md") == rm.SOAK_TIER_FULL
    # A path that is all separators/dots normalizes to empty → fail-safe full.
    assert f("./") == rm.SOAK_TIER_FULL
    assert f("") == rm.SOAK_TIER_FULL


def test_mixed_candidate_scans_all_paths_for_privilege():
    """The most-cautious-wins loop must inspect EVERY path — a privileged
    file is full even when it is not first and is buried among inert/short
    paths (attack: hide peer_auth.py behind a docs change)."""
    c = rm.classify_soak_tier
    assert c(["docs/x.md", "packages/analyzer/peer_auth.py"]) == rm.SOAK_TIER_FULL
    assert c([
        "docs/a.md",
        "packages/analyzer/generators/g/observe.py",   # short
        "README.md",                                    # skip
        "packages/admin/evolve_admin/sudoers_render.py" # token 'sudoer' → full
    ]) == rm.SOAK_TIER_FULL


def test_predates_policy_candidate_soaks_full_window():
    """A candidate written before the tier policy (no `soak_tier` field) is
    treated as full → the configured window, i.e. today's behavior. Empty
    / blank tier must never read as skip (0 minutes)."""
    cfg = rm.ReleaseConfig(soak_minutes=60)
    assert rm._candidate_soak_minutes({}, cfg) == 60                      # no field
    assert rm._candidate_soak_minutes({"soak_tier": ""}, cfg) == 60       # blank
    assert rm._candidate_soak_minutes({"soak_tier": None}, cfg) == 60     # null
    assert rm._candidate_soak_minutes({"soak_tier": "skip"}, cfg) == 0    # sanity
    # D7: a `short` candidate with NO active-validation flag (predates / not
    # validated) falls back to the legacy 15-min passive window — never a
    # silent 0. Only once the flag is stamped True does the residual drop to
    # 0 (promote at active-pass time).
    assert rm._candidate_soak_minutes({"soak_tier": "short"}, cfg) == 15
    assert rm._candidate_soak_minutes(
        {"soak_tier": "short", "soak_active_validated": True}, cfg) == 0


def test_changed_paths_failed_diff_yields_empty_then_full():
    """Highest-stakes fail-safe: if `git diff` exits non-zero (bad base sha,
    repo lock, …) `_changed_paths` returns [] and classify_soak_tier([])
    → full. A failed diff must NEVER under-classify to skip."""
    def failing_git(repo, args):
        return (128, "", "fatal: bad revision 'stable..candidate'")
    assert rm._changed_paths(Path("/repo"), "stable", "cand", failing_git) == []
    assert rm.classify_soak_tier([]) == rm.SOAK_TIER_FULL


def test_changed_paths_raising_git_is_not_swallowed_to_skip():
    """If the git helper RAISES (missing binary / timeout), `_changed_paths`
    must propagate — it must not silently swallow into [] in a way the
    caller could mistake for 'nothing changed → skip'. The tick aborts in
    `checking` (before any promote), so the candidate is never promoted on a
    diff fault."""
    def raising_git(repo, args):
        raise FileNotFoundError("git not on PATH")
    with pytest.raises(FileNotFoundError):
        rm._changed_paths(Path("/repo"), "stable", "cand", raising_git)


def test_renamed_privileged_file_is_caught_as_full(pod):
    """Real-git rename attack: `git diff --name-only` reports the rename
    DESTINATION, so renaming an inert file TO a privileged path must
    classify full and soak — it cannot skip-promote. (Renaming a privileged
    file to an inert name moves the content off the import path, so the
    inert destination is genuinely inert — safe in the other direction.)"""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    # Rename the benign seed module to a privileged name (token 'keystore').
    _run(["git", "mv",
          "packages/admin/evolve_admin/ok.py",
          "packages/admin/evolve_admin/keystore.py"], pod["seed"])
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
          "commit", "-q", "-m", "rename ok→keystore"], pod["seed"])
    _run(["git", "push", "-q", "origin", "main"], pod["seed"])

    res = _tick(pod, deps)
    assert res.soak_tier == "full", "renamed privileged dest must be full"
    assert res.candidate_state == "soaking"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"], "must NOT skip-promote a rename"


def test_full_tier_without_probe_target_still_soaks_full_window(pod):
    """Probe selection #10: a full-tier candidate whose diff has no
    exercisable diff-selected surface (a keystore/secret change) runs only
    the D5 gateway probe but still soaks the full window on the passive
    backstop — a passing active probe never shortens or skips the full-tier
    window (D7 introduces the residual ceiling; D5/D6 keep the full timer)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "fix(secrets): rotate keystore helper", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n",
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "full"
    assert res.candidate_state == "soaking"
    assert rec.probes == [[{"kind": "gateway", "target": None}]], \
        "keystore change has no diff probe surface → gateway probe only"
    # Full window still required — not promoted early.
    clock.advance(30)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    clock.advance(31)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b


# ─────────────────────────────────────────────────────────────────────────────
# D5 — gateway liveness probe (always-on runtime check)
# Spec: internal/spec-delta-active-canary-validation-2026-06-13.md §D5.
# Tick-layer integration: the always-on gateway probe is in EVERY soaking
# candidate's plan and its verdict drives the existing fail-closed / fail-
# open wiring. The verdict LOGIC (round-trip → ok/regression/error) is unit-
# proven in test_soak_probe.py.
# ─────────────────────────────────────────────────────────────────────────────


def test_d5_gateway_regression_fails_soak_closed(pod):
    """D5 proof: a candidate whose gateway does not come up on the new code
    → the always-on gateway probe returns REGRESSION → the soak FAILS
    CLOSED in this tick. The fleet never moves onto it and it's rollback-
    able (canary restored from the fleet checkout, sha skip-listed). An
    opaque full-tier change is used so the ONLY probe in the plan is the
    gateway probe — the failure is unambiguously the gateway's."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = ("gateway: not serving plugin-signed /evolve/status "
                        "on :8787 after 30 attempts over ~58s "
                        "(200 OK but body is not JSON — plugin not loaded)")
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "fix(secrets): keystore helper", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n",  # full tier, no diff probe
    })
    res = _tick(pod, deps)
    assert res.soak_tier == "full"
    # The only probe in the plan was the always-on gateway probe.
    assert rec.probes == [[{"kind": "gateway", "target": None}]]
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"], "fleet never moved onto the broken candidate"
    assert sha_b in rm.load_release_state(pod["shared"]).skip
    # Canary restored from the fleet checkout (the candidate is rollback-able).
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)
    assert rec.hooks == [], "a failed candidate never runs the promote hooks"


def test_d5_gateway_tooling_error_fails_open_with_degraded_signal(pod):
    """D5 proof: a gateway-liveness TOOLING fault (we couldn't reach the
    gateway for our OWN reasons — e.g. the canary's port is unknown) fails
    OPEN: the candidate is NOT failed, a loud release_soak_probe_degraded
    Signal is emitted, and the soak continues on the passive backstop +
    timer. A release is never failed on our own tooling fault."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_ERROR
    rec.probe_detail = "gateway: no gateway port for canary_bot in network.json (cannot probe)"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "fix(secrets): keystore helper", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n",
    })
    res = _tick(pod, deps)
    assert res.soak_probe == rm.SOAK_PROBE_ERROR
    assert res.candidate_state == "soaking", "fail OPEN — candidate keeps soaking"
    assert res.promoted_to == ""
    assert any(s.get("type") == "release_soak_probe_degraded" for s in rec.signals)
    # NOT a candidate failure: no failure signal, sha not skip-listed.
    assert not any(s.get("type") == "release_canary_failed" for s in rec.signals)
    assert sha_b not in rm.load_release_state(pod["shared"]).skip
    # Passive backstop + timer still carry it to a normal promote.
    clock.advance(61)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b


# ─────────────────────────────────────────────────────────────────────────────
# D7 — event-driven promote: gate on active validation, timer → residual ceiling
# Spec: internal/spec-delta-active-canary-validation-2026-06-13.md §D7.
#
# The promote predicate is now: active_validation_passed AND
# tier_residual_elapsed AND soak_health_clean. A residual-0 tier (skip /
# short, active-validated) promotes at active-pass time, IN THE SAME TICK —
# not on the next 15-min puller tick, and never gated by a passive timer. A
# failing active check fails the candidate immediately. The full tier keeps a
# short residual dwell (DEFAULT_SOAK_MINUTES) as the only timer concession.
# ─────────────────────────────────────────────────────────────────────────────


def _cfg_residual15(pod) -> rm.ReleaseConfig:
    """A canary cfg whose full-tier residual is the D7 default (15), so the
    full-tier residual ceiling is observable without the pod fixture's 60."""
    return rm.ReleaseConfig(mode="canary", canary_bot="canary_bot",
                            soak_minutes=rm.DEFAULT_SOAK_MINUTES)


def test_d7_short_promotes_at_active_pass_not_timer(pod):
    """D7 proof #1: a short candidate that passes every active check (the D5
    gateway probe + the D2 generator probe returned OK, the D4 health is
    clean) promotes at ACTIVE-PASS time — the SAME tick, with the clock never
    advanced past soak entry. The timer does not gate it: residual 0."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    t0 = clock.now

    sha_b = _push_commit(pod, "feat(model_discovery): coalesce", {
        "packages/analyzer/generators/model_discovery/observe.py": "OK = 1\n",
    })
    res = _tick(pod, deps)   # NO clock advance — same instant as soak entry
    assert clock.now == t0, "promotion happened without the clock advancing"
    assert res.soak_tier == "short"
    assert res.soak_probe == rm.SOAK_PROBE_OK            # D5+D2 active checks OK
    assert rec.soak_baselines, "D4 soak-health was consulted in the predicate"
    assert res.promoted_to == sha_b                      # …and it promoted, now
    assert _fleet_head(pod) == sha_b
    # The candidate carried the active-validation verdict that earned the 0.
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b and state.candidate is None


def test_d7_failing_active_check_fails_immediately_not_at_timer(pod):
    """D7 proof #2: a candidate failing an active check (the D5 gateway probe
    returns REGRESSION) is failed at the tick it is OBSERVED — soak entry —
    not after any timer. No clock advance is needed to catch it."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = "gateway: not serving /evolve/status after 30 attempts"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    t0 = clock.now

    sha_b = _push_commit(pod, "feat(model_discovery): buggy", {
        "packages/analyzer/generators/model_discovery/observe.py": "BAD = 1\n",
    })
    res = _tick(pod, deps)   # no clock advance
    assert clock.now == t0, "failure observed at soak entry, not at timer-elapse"
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert res.promoted_to == ""
    assert _fleet_head(pod) == pod["sha_a"], "fleet never moved onto it"
    assert sha_b in rm.load_release_state(pod["shared"]).skip


def test_d7_full_tier_still_waits_residual_ceiling(pod):
    """D7 proof #3: the full tier keeps a residual dwell even when every
    active check is clean — the one timer concession for the irreversible
    minority. With the D7 default (15) it stays soaking at soak entry and
    just before the ceiling, then promotes once the residual elapses."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    cfg = _cfg_residual15(pod)
    _tick(pod, deps, cfg)  # init

    sha_b = _push_commit(pod, "fix(secrets): rotate keystore", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n",   # full tier
    })
    # Soak entry: active checks clean, but full residual > 0 → still soaking.
    res = _tick(pod, deps, cfg)
    assert res.soak_tier == "full"
    assert res.soak_probe == rm.SOAK_PROBE_OK
    assert res.promoted_to == "", "full tier does not promote at active-pass"
    assert res.candidate_state == "soaking"
    # Just shy of the 15-min residual — still soaking.
    clock.advance(rm.DEFAULT_SOAK_MINUTES - 1)
    res = _tick(pod, deps, cfg)
    assert res.promoted_to == ""
    # Past the residual ceiling → promote.
    clock.advance(2)
    res = _tick(pod, deps, cfg)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b


def test_d7_tooling_fault_does_not_block_promote(pod):
    """D7 proof #4: a TOOLING fault (degraded) on an active probe must NOT
    block the promote — fail OPEN. The candidate is not failed and a loud
    degraded Signal is emitted; because we could not actively validate it,
    it falls back to the legacy passive window (never silently fast-tracked)
    and promotes once that window elapses."""
    rec, clock = Recorder(), Clock()
    rec.probe_status = rm.SOAK_PROBE_ERROR
    rec.probe_detail = "gateway: no gateway port for canary_bot (cannot probe)"
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "refactor: helper tweak", {
        "packages/admin/evolve_admin/some_helper.py": "VALUE = 2\n",   # short
    })
    res = _tick(pod, deps)
    assert res.soak_probe == rm.SOAK_PROBE_ERROR
    assert res.candidate_state == "soaking", "fail OPEN — not blocked, not failed"
    assert res.promoted_to == "", "degraded ≠ active-validated → no residual-0"
    assert any(s.get("type") == "release_soak_probe_degraded" for s in rec.signals)
    assert sha_b not in rm.load_release_state(pod["shared"]).skip
    # The fallback window carries it to a normal promote (not blocked).
    clock.advance(rm.SHORT_SOAK_MINUTES + 1)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b
    assert _fleet_head(pod) == sha_b


def test_d7_clean_import_but_gateway_crashes_on_boot_caught_at_promote(pod):
    """D7 proof #5 + the chip's adversarial self-review on this privileged
    promote-decision change.

    The exact failure D7 exists to catch on the common path: a candidate that
    IMPORTS CLEAN (so Gate 1's import-smoke passes) and is otherwise valid,
    but CRASHES THE GATEWAY ON BOOT — the bot's runtime never comes up on the
    new code. Import-smoke can't see this (import ≠ runtime init); only the
    D5 gateway liveness round-trip against the actual canary process does.

    Construct that failure literally: Gate 1 passes (gate1_ok=True, the real
    import path would too — the module imports), but the gateway probe returns
    REGRESSION with the real liveness-failure string. Under D7 this is caught
    at PROMOTE-TIME, in ~seconds (no clock advance, no window), and the
    candidate is left rollback-able: the fleet never moved, the canary is
    restored from the fleet checkout, and the sha is skip-listed."""
    rec, clock = Recorder(), Clock()
    rec.gate1_ok = True   # imports clean — Gate 1 cannot catch a boot crash
    rec.probe_status = rm.SOAK_PROBE_REGRESSION
    rec.probe_detail = (
        "gateway: 200 OK but body is not plugin-signed JSON on :8787 after "
        "30 attempts over ~58s — /evolve/status never re-mounted (the "
        "candidate crashed the gateway on boot)")
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init
    t0 = clock.now

    sha_b = _push_commit(pod, "feat: importable but crashes the gateway", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 2\n",  # full tier
    })
    res = _tick(pod, deps)
    # Caught in ~seconds at promote-time — NOT after a soak window.
    assert clock.now == t0, "caught at the soak-entry tick, no window waited"
    assert res.soak_probe == rm.SOAK_PROBE_REGRESSION
    assert res.candidate_state == "failed"
    assert res.promoted_to == ""
    # Left rollback-able: fleet untouched, canary restored, sha skip-listed.
    assert _fleet_head(pod) == pod["sha_a"]
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    assert sha_b in rm.load_release_state(pod["shared"]).skip
    assert rec.hooks == [], "a failed candidate never runs the promote hooks"
    assert any(s.get("type") == "release_canary_failed" for s in rec.signals)


# ── Active-probe verdict hardening (generator log classification) ──────────────


def test_generator_probe_log_classifies_candidate_break_as_regression():
    """The generator probe recovers its verdict from run_one_generator's
    captured log. An observe() crash is a candidate regression → fail
    closed."""
    clf = soak_probe._classify_generator_log
    v = clf(["[generator_runner] run_one_generator: 'model_discovery'.observe failed: NameError: x"])
    assert v is not None and v[0] == soak_probe.REGRESSION


def test_generator_probe_log_catches_ingest_failure_without_id():
    """FINDING (major) FIX: run_one_generator logs `ingest failed: <exc>`
    with NO generator id in the line. The old heuristic required
    `generator_id in line`, so this genuine regression class (the candidate
    produced proposals that crash ingest) was silently MISSED → probe
    returned OK. It must now classify REGRESSION."""
    clf = soak_probe._classify_generator_log
    v = clf(["[generator_runner] run_one_generator: ingest failed: KeyError: 'foo'"])
    assert v is not None and v[0] == soak_probe.REGRESSION, (
        "ingest failure (no id in line) must not be swallowed to OK")


def test_generator_probe_log_treats_env_ctx_build_as_fail_open():
    """FINDING (major) FIX: a ctx-build / registry-load failure is an
    ENVIRONMENTAL miss on the canary (missing observation data, etc.), not
    'the candidate is broken'. The old heuristic matched id+'failed' and
    failed the release CLOSED — a false-positive that skip-lists a GOOD
    candidate. It must now be ERROR (fail OPEN + degraded signal)."""
    clf = soak_probe._classify_generator_log
    v = clf(["[generator_runner] run_one_generator: ctx build failed for 'g'/'bot': FileNotFoundError"])
    assert v is not None and v[0] == soak_probe.ERROR, (
        "environmental ctx-build failure must fail OPEN, not fail the release")


def test_generator_probe_log_regression_outranks_error_and_benign_is_ok():
    """REGRESSION outranks ERROR regardless of line order; benign run lines
    (counts, 'not active') yield no verdict → caller returns OK."""
    clf = soak_probe._classify_generator_log
    mixed = clf([
        "[generator_runner] run_one_generator: ctx build failed for 'g'/'b': X",
        "[generator_runner] run_one_generator: 'g'.observe failed: ValueError",
    ])
    assert mixed is not None and mixed[0] == soak_probe.REGRESSION
    assert clf([
        "[generator_runner] run_one_generator: 'g' emitted=3 ingested=2",
        "[generator_runner] run_one_generator: 'g' not active in the registry",
    ]) is None


# ─────────────────────────────────────────────────────────────────────────────
# D4 — soak-health baseline-diff by signature
# Spec: internal/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D4.
#
# Root cause these prove fixed: ambient app-quality debt on the canary's own
# apps re-fires every scan (resolve→reopen re-stamps a `to_state="firing"`
# transition inside the soak window). The old check keyed on that timestamp
# and so failed EVERY candidate on the same standing signals, jamming the
# whole fleet. The fix diffs the canary's firing set against a stable-code
# baseline captured at soak entry and fails only on NEW signatures.
# ─────────────────────────────────────────────────────────────────────────────


class _FakeTransition:
    def __init__(self, to_state, at):
        self.to_state = to_state
        self.at = at


class _FakeSignal:
    """Minimal Signal stand-in exposing exactly what _default_soak_health
    reads: signature, producer, type, state, state_history[].{to_state,at}."""

    def __init__(self, signature, producer, type_, *, state="firing",
                 fired_at=None):
        self.signature = signature
        self.producer = producer
        self.type = type_
        self.state = state
        self.state_history = (
            [_FakeTransition("firing", fired_at)] if fired_at else [])


class _FakeStore:
    """Fake signals.store: iter_active yields the injected signals, honoring
    the bot_id (no-op here) and state= filters the real store applies."""

    def __init__(self, signals):
        self._signals = signals

    def iter_active(self, shared_dir, *, bot_id=None, state=None, **kw):
        for s in self._signals:
            if state is not None and s.state != state:
                continue
            yield s


def _patch_signals_store(monkeypatch, signals):
    store = _FakeStore(signals)
    monkeypatch.setattr(
        "evolve_admin.repo_puller._signals_module", lambda: (store, object()))
    return store


# ── Proof 1: the exact bug scenario now PASSES (ambient re-fire) ──────────────


def test_d4_ambient_signals_refiring_passes_soak(monkeypatch, tmp_path):
    """THE BUG: standing debt sigA + sigB re-fire (fresh to-firing transition
    AFTER soak start) on every candidate. With a baseline of {sigA, sigB},
    the diff finds NO new signature → soak is HEALTHY. Under the old
    timestamp predicate this returned (False, ...) and jammed the fleet."""
    soak_start = "2026-06-12T10:00:00+00:00"
    after_start = "2026-06-12T10:30:00+00:00"  # the standing-debt re-fire
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "app_structural_verifier",
                    "app_discoverability_no_cli", fired_at=after_start),
        _FakeSignal("sigB", "app_manifest_monitor",
                    "app_permission_drift", fired_at=after_start),
    ])
    healthy, detail = rm._default_soak_health(
        tmp_path, "canary_bot", soak_start,
        baseline_signatures=["sigA", "sigB"])
    assert healthy is True, detail
    assert detail == "no new canary signals"


# ── Proof 2: a genuinely-new regression still FAILS ───────────────────────────


def test_d4_genuinely_new_signature_fails_soak(monkeypatch, tmp_path):
    """baseline = {sigA}; the canary fires sigA (ambient) PLUS sigZ (a NEW
    signature the candidate introduced). The diff fails the soak and names
    sigZ's producer:type — and must NOT name the ambient sigA."""
    soak_start = "2026-06-12T10:00:00+00:00"
    after_start = "2026-06-12T10:30:00+00:00"
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "app_structural_verifier",
                    "app_discoverability_no_cli", fired_at=after_start),
        _FakeSignal("sigZ", "heal", "gateway_crash_loop", fired_at=after_start),
    ])
    healthy, detail = rm._default_soak_health(
        tmp_path, "canary_bot", soak_start, baseline_signatures=["sigA"])
    assert healthy is False
    assert "heal:gateway_crash_loop" in detail
    assert "app_discoverability_no_cli" not in detail, (
        "ambient baseline signal must not be reported as a regression")


# ── Proof 3: baseline captured at deploy entry, threaded to the check ─────────


def test_d4_baseline_captured_before_canary_deploy(pod, monkeypatch):
    """Integration through tick(): a non-skip candidate captures the canary's
    pre-deploy active-signal signatures into
    state.candidate['soak_baseline_signatures'], BEFORE the candidate is
    deployed to the canary, and threads that baseline to the soak check.

    Uses a FULL-tier candidate so it stays in soak across ticks for the
    inspection (D7 promotes an active-validated short at active-pass time,
    clearing state.candidate); the D4 baseline logic is identical for any
    non-skip tier."""
    # The canary's standing debt as it stands on STABLE code, pre-candidate.
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "app_structural_verifier",
                    "app_discoverability_no_cli"),
        _FakeSignal("sigB", "app_manifest_monitor", "app_permission_drift",
                    state="snoozed"),  # snoozed ambient must be in baseline
    ])
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    # A `full`-tier candidate (keystore) → soaks on the canary and STAYS in
    # soak (residual > 0) so the persisted baseline is observable.
    _push_commit(pod, "B: secret change", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n"})
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"

    state = rm.load_release_state(pod["shared"])
    baseline = state.candidate["soak_baseline_signatures"]
    # Captured from the canary's pre-candidate active set (firing + snoozed),
    # sorted + JSON-serializable.
    assert baseline == ["sigA", "sigB"]
    # Captured BEFORE the canary deploy: the deploy happened, and the baseline
    # is the PRE-candidate signal set (the fake store is the stable-code view).
    assert rec.deploys[0][0] == "canary_bot"
    # The soak check received that baseline in the same-tick D4 evaluation
    # (the D7 fall-through predicate) and again on the next tick.
    assert rec.soak_baselines[-1] == ["sigA", "sigB"]
    clock.advance(20)
    _tick(pod, deps)
    assert rec.soak_baselines[-1] == ["sigA", "sigB"]


def test_d4_crash_during_soak_entry_reuses_stable_baseline(pod, monkeypatch):
    """A1: the baseline is PERSISTED before the canary deploy and capture is
    GUARDED against re-capture. If the daemon dies after the deploy but before
    the soaking-flip (a window that includes the ≤900s active probe), the next
    tick re-enters `checking` with the canary already on CANDIDATE code.
    Re-capturing there would baseline-IN a candidate-introduced firing signal,
    silently passing a passive-only regression to the whole fleet. The persisted
    baseline + the `not in` guard keep the baseline stable-code-derived.

    Drive: the FIRST tick's canary deploy raises (simulating the daemon dying
    mid-tick, after the persist-before-deploy save, before the soaking-flip),
    so on-disk state stays `checking` with the baseline persisted. The signal
    store is then mutated so the canary also emits a NEW candidate signal sigZ
    (the candidate code now on the canary). The next tick must NOT re-capture."""
    # Stable-code standing debt on the canary, pre-candidate.
    store = _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "app_structural_verifier",
                    "app_discoverability_no_cli"),
    ])
    # Count every capture call across both `checking` entries.
    real_capture = rm._capture_canary_signal_baseline
    calls = {"n": 0}

    def counting_capture(shared_dir, canary_bot, deps):
        calls["n"] += 1
        return real_capture(shared_dir, canary_bot, deps)

    monkeypatch.setattr(rm, "_capture_canary_signal_baseline", counting_capture)

    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    # First soak-entry tick: deploy raises AFTER the persist-before-deploy save,
    # before the soaking-flip — the daemon "crashes" mid-tick.
    def crashing_deploy(bot, code_dir, network_path):
        raise RuntimeError("simulated daemon crash during canary deploy")

    deps.canary_deploy_fn = crashing_deploy
    # FULL-tier candidate (keystore) so the re-entry tick stays in soak
    # (residual > 0) for the post-restart baseline inspection — D7 would
    # promote a short candidate at active-pass time, clearing state.candidate.
    _push_commit(pod, "B: secret change", {
        "packages/admin/evolve_admin/keystore.py": "ROT = 1\n"})
    with pytest.raises(RuntimeError):
        _tick(pod, deps)

    # On-disk: still `checking`, but the baseline is ALREADY persisted (= the
    # stable-code set), captured BEFORE the deploy. Exactly one capture so far.
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["state"] == "checking"
    assert state.candidate["soak_baseline_signatures"] == ["sigA"]
    assert calls["n"] == 1

    # The canary is now on CANDIDATE code, which introduces a NEW firing signal
    # sigZ. (Mutate the fake store in place.)
    store._signals.append(
        _FakeSignal("sigZ", "heal", "gateway_crash_loop"))

    # Re-entry tick: the guard must NOT re-capture (which would baseline-in
    # sigZ). Use the recording deploy + soak-health seams so the tick completes.
    deps.canary_deploy_fn = rec.deploy
    res = _tick(pod, deps)
    assert res.candidate_state == "soaking"

    # Capture was invoked EXACTLY once across the two `checking` entries.
    assert calls["n"] == 1, "guard must not re-capture against candidate code"

    # The persisted baseline is UNCHANGED — sigZ was NOT folded in.
    state = rm.load_release_state(pod["shared"])
    assert state.candidate["soak_baseline_signatures"] == ["sigA"]

    # And the diff would FAIL the soak on sigZ (signature ∉ baseline), proving
    # the candidate regression is still caught after the crash-restart.
    healthy, detail = rm._default_soak_health(
        pod["shared"], "canary_bot",
        state.candidate.get("soak_started_at", ""),
        state.candidate["soak_baseline_signatures"])
    assert healthy is False
    assert "heal:gateway_crash_loop" in detail
    assert "app_discoverability_no_cli" not in detail


def test_d4_skip_tier_captures_no_baseline(pod, monkeypatch):
    """A skip-tier candidate (docs-only) promotes BEFORE any canary deploy →
    no baseline is captured and the soak check is never consulted."""
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "app_structural_verifier", "x")])
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    sha_b = _push_commit(pod, "B: docs only", {"internal/spec-thing.md": "hi\n"})
    res = _tick(pod, deps)
    # Promoted immediately, no soak window, no soak-health call, no baseline.
    assert res.promoted_to == sha_b
    assert rec.soak_baselines == []
    state = rm.load_release_state(pod["shared"])
    assert state.candidate is None  # nothing in flight after a skip promote


# ── Proof 4: back-compat / fail-safe (no baseline ⇒ legacy since predicate) ───


def test_d4_no_baseline_falls_back_to_since_predicate(monkeypatch, tmp_path):
    """A pre-D4 candidate mid-soak across the upgrade (or a capture fault)
    has no baseline. The check must FALL BACK to the legacy since_iso
    predicate — NEVER silently pass everything."""
    soak_start = "2026-06-12T10:00:00+00:00"
    after_start = "2026-06-12T10:30:00+00:00"

    # A signal fired AFTER soak start, no baseline → unhealthy (legacy path).
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "heal", "gateway_crash_loop", fired_at=after_start)])
    healthy, detail = rm._default_soak_health(
        tmp_path, "canary_bot", soak_start, baseline_signatures=None)
    assert healthy is False
    assert "heal:gateway_crash_loop" in detail

    # Empty-list baseline behaves identically to None (capture-fault case).
    healthy, detail = rm._default_soak_health(
        tmp_path, "canary_bot", soak_start, baseline_signatures=[])
    assert healthy is False

    # A signal that fired BEFORE soak start, no baseline → healthy (legacy
    # path); proves the fallback is the real since-predicate, not pass-all.
    before_start = "2026-06-12T09:00:00+00:00"
    _patch_signals_store(monkeypatch, [
        _FakeSignal("sigA", "heal", "gateway_crash_loop", fired_at=before_start)])
    healthy, detail = rm._default_soak_health(
        tmp_path, "canary_bot", soak_start, baseline_signatures=None)
    assert healthy is True, detail


# ─────────────────────────────────────────────────────────────────────────────
# Forward-bootstrap verb + the `pin` gotcha (release-pipeline self-deploy).
# Spec: internal/spec-state-store-and-deploy-resilience-2026-06-10.md §2.11.
#
# `pin` is a FREEZE at the current stable (never a move); a non-stable ref is
# refused. `release bootstrap <ref>` is the sanctioned forward force-move that
# bypasses the soak gate to deploy a fix to the pipeline itself.
# ─────────────────────────────────────────────────────────────────────────────


def _fetch_fleet(pod):
    """Pull origin objects into the fleet repo (what every tick does), so a
    forward target ref is resolvable without having ticked it in."""
    _run(["git", "fetch", "-q", "origin"], pod["fleet"])


# ── pin: freeze-at-stable semantics + non-stable-ref refusal ──────────────────


def test_pin_no_ref_freezes_at_current_stable(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init: stable = sha_a
    ok, msg = rm.release_pin(pod["shared"], repo=pod["fleet"], deps=deps)
    assert ok, msg
    state = rm.load_release_state(pod["shared"])
    # The invariant: pin.sha always equals stable.sha — never dead data.
    assert state.pin is not None
    assert state.pin["sha"] == state.stable["sha"] == pod["sha_a"]


def test_pin_at_current_stable_ref_is_allowed(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    ok, msg = rm.release_pin(pod["shared"], ref=pod["sha_a"], repo=pod["fleet"], deps=deps)
    assert ok, msg
    state = rm.load_release_state(pod["shared"])
    assert state.pin is not None and state.pin["sha"] == pod["sha_a"]


def test_pin_non_stable_ref_is_refused_and_redirects(pod):
    """The footgun: `pin <future-sha>` used to record a dead pin.sha that
    disagreed with stable and moved nothing. Now it is refused and points
    the operator at the verb that actually moves the fleet."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = sha_a
    sha_b = _push_commit(pod, "B", {"b.py": "B = 1\n"})
    _fetch_fleet(pod)  # so sha_b is resolvable in the fleet repo

    ok, msg = rm.release_pin(pod["shared"], ref=sha_b, repo=pod["fleet"], deps=deps)
    assert ok is False
    assert "bootstrap" in msg and "rollback" in msg
    # Crucially, it did NOT pin (no dead pin.sha recorded) and did not move.
    state = rm.load_release_state(pod["shared"])
    assert state.pin is None
    assert state.stable["sha"] == pod["sha_a"]
    assert _fleet_head(pod) == pod["sha_a"]


def test_pin_unresolvable_ref_is_refused(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    ok, msg = rm.release_pin(pod["shared"], ref="nonsuch", repo=pod["fleet"], deps=deps)
    assert ok is False and "cannot resolve" in msg


# ── bootstrap: forward force-move ─────────────────────────────────────────────


def test_bootstrap_force_moves_stable_forward_and_pins(pod):
    """The sanctioned forward move: stable advances to an un-soaked sha, the
    fleet follows, it auto-pins (pin.sha == new stable), the fled sha is NOT
    skip-listed (contrast rollback), hooks run, the canary is restored, and a
    DISTINCT release_bootstrapped signal fires (never release_rolled_back)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = sha_a
    sha_b = _push_commit(pod, "B: pipeline fix", {"gate.py": "FIX = 1\n"})
    _fetch_fleet(pod)
    rec.deploys.clear()
    rec.hooks.clear()

    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=sha_b,
                               cfg=_cfg(pod), deps=deps)
    assert res.success, res.error
    assert res.fleet_sha == sha_b and res.pinned
    assert _fleet_head(pod) == sha_b
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b
    assert state.previous["sha"] == pod["sha_a"]
    # Auto-pin holds the invariant pin.sha == stable.sha.
    assert state.pin is not None and state.pin["sha"] == sha_b
    # Forward move: the fled sha is an ancestor — NOT skip-listed.
    assert pod["sha_a"] not in state.skip
    # Hook suite ran for the forward move; canary restored from the FLEET.
    assert rec.hooks == [(pod["sha_a"], sha_b)]
    assert rec.deploys[-1] == ("canary_bot", str(pod["fleet"]))
    # Distinct audit signal — Alerts must not read "rolled back".
    types = [s.get("type") for s in rec.signals]
    assert "release_bootstrapped" in types
    assert "release_rolled_back" not in types


def test_bootstrap_dry_run_lists_unsoaked_commits_and_moves_nothing(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B: a fix", {"x.py": "X = 1\n"})
    _fetch_fleet(pod)
    rec.deploys.clear()

    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=sha_b,
                               dry_run=True, cfg=_cfg(pod), deps=deps)
    assert res.success
    assert any("UN-SOAKED" in s for s in res.steps)
    assert any("dry-run" in s for s in res.steps)
    # Nothing moved, nothing deployed, no audit signal.
    assert _fleet_head(pod) == pod["sha_a"]
    assert rm.load_release_state(pod["shared"]).stable["sha"] == pod["sha_a"]
    assert rec.deploys == []
    assert not any(s.get("type") == "release_bootstrapped" for s in rec.signals)


def test_bootstrap_refuses_when_target_is_current_stable(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=pod["sha_a"],
                               cfg=_cfg(pod), deps=deps)
    assert res.success is False
    assert "already the current stable" in res.error


def test_bootstrap_requires_a_target_ref(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref="",
                               cfg=_cfg(pod), deps=deps)
    assert res.success is False and "requires a target ref" in res.error


def test_bootstrap_does_not_skiplist_and_resumes_soak_after_unpin(pod):
    """Forward semantics vs rollback: bootstrapping to an intermediate sha
    must NOT skip-list anything, so after `unpin` the pipeline soaks the
    newer origin/main commit normally (rollback, by contrast, skip-lists the
    fled sha to stop it re-promoting)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = sha_a
    sha_b = _push_commit(pod, "B: gate fix", {"g.py": "G = 1\n"})
    sha_c = _push_commit(pod, "C: later work", {"h.py": "H = 1\n"})
    _fetch_fleet(pod)

    # Bootstrap to the intermediate B (origin tip is C).
    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=sha_b,
                               cfg=_cfg(pod), deps=deps)
    assert res.success and _fleet_head(pod) == sha_b
    state = rm.load_release_state(pod["shared"])
    assert state.skip == [], "forward bootstrap must not skip-list anything"
    assert state.pin is not None  # auto-pinned

    # Pinned: C cannot promote yet.
    clock.advance(121)
    res = _tick(pod, deps)
    assert res.promoted_to == ""
    assert _fleet_head(pod) == sha_b

    # Unpin → C is picked up and soaks→promotes through the (now-fixed) gate.
    rm.release_unpin(pod["shared"])
    res = _tick(pod, deps)             # C → soaking
    assert res.candidate_sha == sha_c
    clock.advance(121)
    res = _tick(pod, deps)             # soak elapsed → promote
    assert res.promoted_to == sha_c
    assert _fleet_head(pod) == sha_c


def test_bootstrap_cancels_inflight_candidate(pod):
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"s.py": "S = 1\n"})
    _tick(pod, deps)  # B soaking (candidate in flight, staging worktree exists)
    state = rm.load_release_state(pod["shared"])
    assert state.candidate is not None

    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=sha_b,
                               cfg=_cfg(pod), deps=deps)
    assert res.success and _fleet_head(pod) == sha_b
    state = rm.load_release_state(pod["shared"])
    assert state.candidate is None
    assert not rm.staging_dir_for(sha_b, pod["staging"]).exists()


def test_bootstrap_pointer_persists_even_when_hooks_raise(pod):
    """Shared force-move invariant (pointer BEFORE hooks): a hook that kills
    the caller must still leave the fleet on the bootstrap target, so the next
    tick's pointer-repair does not yank it back."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_b = _push_commit(pod, "B", {"k.py": "K = 1\n"})
    _fetch_fleet(pod)

    def killed_hooks(repo, before, after):
        raise RuntimeError("simulated admin-ui kickstart killing the caller")

    deps.hooks_fn = killed_hooks
    rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=sha_b,
                         cfg=_cfg(pod), deps=deps)
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_b, "pointer must persist before hooks"
    assert state.pin is not None and state.pin["sha"] == sha_b
    assert _fleet_head(pod) == sha_b

    # Next tick must NOT repair the fleet back to the fled sha.
    deps.hooks_fn = rec.run_hooks
    _tick(pod, deps)
    assert _fleet_head(pod) == sha_b


def test_bootstrap_refuses_backward_target_and_redirects_to_rollback(pod):
    """bootstrap is a forward verb; a target at-or-behind stable is refused
    and the operator is pointed at `rollback --to` (which is the backward
    force-move that correctly skip-lists the fled sha)."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = sha_a
    sha_b = _push_commit(pod, "B", {"b.py": "B = 1\n"})
    _tick(pod, deps)  # B soaking
    clock.advance(121)
    res = _tick(pod, deps)
    assert res.promoted_to == sha_b  # stable now = sha_b

    res = rm.release_bootstrap(pod["fleet"], pod["shared"], to_ref=pod["sha_a"],
                               cfg=_cfg(pod), deps=deps)
    assert res.success is False
    assert "not ahead" in res.error and "rollback" in res.error
    assert _fleet_head(pod) == sha_b  # unchanged


# ─────────────────────────────────────────────────────────────────────────────
# D-2 — version/release legibility: recency by ANCESTRY, never PR-number
# arithmetic; and the pod-level version never goes stale relative to stable.
# (Regression for the 2026-06-14 promote incident: an operator read a forward
# promote as a backward move because the new tip's PR number was LOWER.)
# ─────────────────────────────────────────────────────────────────────────────


def _promote_one(pod, deps, clock, msg, files):
    """Push a commit, soak it, and promote it to stable. Returns the sha."""
    sha = _push_commit(pod, msg, files)
    _tick(pod, deps)        # detect + start soak
    clock.advance(70)       # past the 60-min soak
    res = _tick(pod, deps)  # promote
    assert res.promoted_to == sha, f"expected promote of {sha[:12]}"
    return sha


def test_d2_status_recency_uses_ancestry_not_pr_number(pod):
    """THE incident, reproduced: stable (the real tip) carries a LOWER PR
    number than the previous stable. PR-number subtraction (2884 - 2885 = -1)
    would call the forward promote "1 behind / older". release_status must
    instead report stable as AHEAD of previous by commit ancestry."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init: stable = A

    # The PR merged EARLIER carries the HIGHER number (assigned at creation).
    sha_prev = _promote_one(pod, deps, clock,
                            "older PR, merged first (#2885)", {"p.py": "P = 1\n"})
    # The real tip — a descendant — carries a LOWER PR number.
    sha_tip = _promote_one(pod, deps, clock,
                           "newer code, lower PR (#2884)", {"q.py": "Q = 1\n"})

    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_tip
    assert state.previous["sha"] == sha_prev

    # The version strings reproduce the inversion: stable's tail < previous's.
    stable_pr = int(state.stable["version"].split(".")[-1])
    prev_pr = int(state.previous["version"].split(".")[-1])
    assert (stable_pr, prev_pr) == (2884, 2885)
    assert stable_pr < prev_pr, "guard: this is the misleading-tail case"

    # PR-number subtraction would say BEHIND. Ancestry says AHEAD/behind==0.
    st = rm.release_status(pod["fleet"], pod["shared"])
    rel = st["recency"]["stable_vs_previous"]
    assert rel["ahead"] >= 1 and rel["behind"] == 0, \
        "stable must read NEWER (ahead) by ancestry, not behind by PR tail"
    assert st["recency"]["stable_commit_date"], "commit date present for the human"

    # The git-free stored field (written at promote) agrees with the live count.
    assert state.stable.get("commits_ahead") == rel["ahead"]
    assert state.stable.get("commit_date")


def test_d2_recency_reports_behind_after_a_real_rollback(pod):
    """The phrase must be ancestry-true in BOTH directions: a rollback genuinely
    moves the pointer back, and that must read as behind/older — not be masked
    by a coincidentally-higher PR tail on the rolled-to commit."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = A
    sha_a = pod["sha_a"]
    # Seed install.json so the pointer-follows-pod-version stamp is observable
    # on the rollback path (record_pod_version no-ops when it's absent).
    install_path = pod["shared"] / "install.json"
    install_path.write_text(json.dumps({
        "version": "2026.0101.1000", "bots": ["canary_bot", "other_bot"],
        "bot_versions": {"other_bot": {"version": "2026.0101.1000"}},
    }))
    sha_b = _promote_one(pod, deps, clock, "B forward (#10)", {"b.py": "B = 1\n"})
    # promote refreshed the pod version forward to B.
    assert json.loads(install_path.read_text())["version"].endswith(".10")

    # Roll back to A (behind B by one commit).
    res = rm.release_rollback(pod["fleet"], pod["shared"], to_ref=sha_a,
                              cfg=_cfg(pod), deps=deps)
    assert res.success
    state = rm.load_release_state(pod["shared"])
    assert state.stable["sha"] == sha_a
    assert state.previous["sha"] == sha_b

    st = rm.release_status(pod["fleet"], pod["shared"])
    rel = st["recency"]["stable_vs_previous"]
    assert rel["behind"] >= 1 and rel["ahead"] == 0, \
        "a rollback must read as BEHIND the prior stable (older)"

    # The pod-level install.json version follows the pointer BACK too — it
    # records "what the fleet runs now", not a high-water mark. bot_versions
    # must survive the rewrite.
    data = json.loads(install_path.read_text())
    assert data["version"] == state.stable["version"], \
        "install.json pod version must track the rolled-to stable"
    assert "other_bot" in data["bot_versions"]


def test_d5_behind_origin_counts_commits_past_the_pointer(pod):
    """D-5 legibility number: ``release_status(fetch_origin=True)`` refreshes
    origin and reports how many commits ``origin/main`` is ahead of the promoted
    stable — i.e. how far the gated fleet sits *behind* tip while candidates
    gate through soak. The fleet tracks the pointer, not tip."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init: stable = A, fleet on A

    # Two commits land on origin/main but are NOT promoted — the fleet stays on
    # the pointer (A). The fleet's local origin/main ref is stale until a fetch.
    _push_commit(pod, "B (#10)", {"b.py": "B = 1\n"})
    _push_commit(pod, "C (#11)", {"c.py": "C = 1\n"})

    # Before any fetch, the stale local origin/main ref still names the promoted
    # commit → 0 behind. This is exactly the staleness fetch_origin closes.
    st_stale = rm.release_status(pod["fleet"], pod["shared"], cfg=_cfg(pod),
                                 fetch_origin=False)
    assert st_stale["behind_origin"] == 0

    # fetch_origin=True refreshes origin/main → now 2 commits ahead of stable.
    st = rm.release_status(pod["fleet"], pod["shared"], cfg=_cfg(pod),
                           fetch_origin=True)
    assert st["behind_origin"] == 2, "fleet is 2 commits behind origin/main tip"
    rel = st["recency"]["stable_vs_origin"]
    assert rel["ahead"] == 2 and rel["behind"] == 0
    # Stable stays exactly where it was — status is read-only, it must not move
    # the fleet toward tip.
    assert rm.load_release_state(pod["shared"]).stable["sha"] == pod["sha_a"]


def test_d2_promote_refreshes_install_json_pod_version(pod):
    """install.json's top-level `version` must track the promoted stable, so no
    surface shows a pod version older than the fleet (the D-2 staleness: pod
    said 2026.0611.2759 while every bot ran 2026.0614.2884). bot_versions must
    be preserved."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # init

    # Seed a STALE install.json: ancient top-level version + a per-bot stamp.
    install_path = pod["shared"] / "install.json"
    install_path.write_text(json.dumps({
        "version": "2026.0101.1000",  # deliberately ancient
        "bots": ["canary_bot", "other_bot"],
        "bot_versions": {"other_bot": {"version": "2026.0101.1000"}},
    }))

    sha_b = _promote_one(pod, deps, clock, "B real tip (#2884)", {"z.py": "Z = 1\n"})

    state = rm.load_release_state(pod["shared"])
    data = json.loads(install_path.read_text())
    assert data["version"] == state.stable["version"], \
        "top-level pod version must equal the promoted stable"
    assert data["version"] != "2026.0101.1000", "the stale value must be gone"
    assert data["version"].endswith(".2884")
    assert "other_bot" in data["bot_versions"], "bot_versions must be preserved"
    assert sha_b  # silence unused


def test_d2_release_ui_view_surfaces_recency_git_free(pod):
    """The web banner reads recency from release.json (no per-poll git):
    release_ui_view must surface the stamped stable + candidate recency."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = A
    sha_b = _promote_one(pod, deps, clock, "B (#2884)", {"z.py": "Z = 1\n"})

    state = rm.load_release_state(pod["shared"])
    view = rm.release_ui_view(_cfg(pod), state)
    assert view["stable_version"] == state.stable["version"]
    assert view["stable_short_sha"] == sha_b[:12]
    assert view["stable_commit_date"], "stamped commit date present"
    assert view["stable_commits_ahead"] == state.stable["commits_ahead"]
    assert view["previous_version"] == state.previous["version"]


_D8_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "docs" / "fixtures" / "deploy-updates-cell-states.json"
)

_OVERVIEW_JS = (
    Path(__file__).parent.parent
    / "evolve_admin" / "web" / "static" / "js" / "pages" / "overview.js"
)


def _overview_cell_tokens() -> dict[str, str]:
    """Extract the {state-id -> cell token} map _classifyUpdates() renders.

    Every cell state returns via the quiet()/loud() helpers whose first two
    positional args are the state id and the bold cell token (a single-quoted
    literal). This is the same structural-assertion technique the rest of the
    admin web tests use (see test_canary_sync_banner.py / test_alerts_button
    _contract.py) — parse the source rather than execute the browser module."""
    src = _OVERVIEW_JS.read_text(encoding="utf-8")
    pairs = re.findall(r"\b(?:quiet|loud)\(\s*'([a-z0-9-]+)'\s*,\s*'([^']*)'", src)
    return dict(pairs)


def test_d8_fixture_labels_match_overview_cell_render():
    """Plain-language relabel (internal/spec-delta-updates-vocabulary-and-direct-
    default-2026-06-22.md): the fixture's per-case `label` is the operator-facing
    UPDATES cell token. Assert each label is exactly what _classifyUpdates()
    renders for that state, locking the deploy-owned contract mirror to the ui
    render so a relabel in one without the other fails CI."""
    fixture = json.loads(_D8_FIXTURE.read_text(encoding="utf-8"))
    tokens = _overview_cell_tokens()
    assert tokens, "no quiet()/loud() cell tokens found in overview.js"
    seen_states: set[str] = set()
    for case in fixture["cases"]:
        exp = case["expect"]
        state, label = exp["state"], exp["label"]
        seen_states.add(state)
        assert state in tokens, (
            f"case {case['id']}: state {state!r} is not rendered by "
            f"_classifyUpdates() (no matching quiet()/loud() call)")
        assert tokens[state] == label, (
            f"case {case['id']} ({state}): fixture label {label!r} != "
            f"overview.js cell token {tokens[state]!r}")
    # Every cell state the JS can render is exercised by at least one fixture
    # case — no state silently drifts un-asserted.
    assert seen_states == set(tokens), (
        f"fixture/JS state drift: only-in-JS={set(tokens) - seen_states}, "
        f"only-in-fixture={seen_states - set(tokens)}")


def _d8_cfg() -> rm.ReleaseConfig:
    return rm.ReleaseConfig(mode="canary", canary_bot="canary_bot", soak_minutes=20)


def test_d8_release_ui_view_surfaces_promoted_at_pin_corrupt():
    """D-8 deltas (internal/spec-deploy-meta-2026-06-14.md): the Overview
    `_classifyUpdates` reads rel.stable.promoted_at (nested), rel.pin (the
    object), and rel.corrupt to split transient-redeploy lag (quiet) from
    stuck/halted (loud). Surface all three, matching the field-name oracle
    docs/fixtures/deploy-updates-cell-states.json."""
    state = rm.ReleaseState(
        stable={"sha": "stablesha", "version": "2026.0614.2884",
                "promoted_at": "2026-06-16T10:00:00+00:00"},
        pin={"sha": "e970a96e", "pinned_at": "2026-06-16T09:00:00+00:00",
             "reason": "rollback from f00dbabe"},
    )
    view = rm.release_ui_view(_d8_cfg(), state)
    # promoted_at lives under a NESTED `stable` mirror (rel.stable.promoted_at),
    # alongside the flat stable_* fields which stay.
    assert view["stable"]["promoted_at"] == "2026-06-16T10:00:00+00:00"
    assert view["stable_version"] == "2026.0614.2884"  # flat field untouched
    # pin is surfaced as the object (carries reason); _classifyUpdates derives
    # pinned = !!pin && !corrupt itself.
    assert view["pin"] is not None
    assert view["pin"]["reason"] == "rollback from f00dbabe"
    assert (view["pin"] is not None) == (state.pin is not None)  # canonical pred
    # corrupt defaults False (it is an INPUT, not detected in the view).
    assert view["corrupt"] is False


def test_d8_release_ui_view_deltas_absent_defaults():
    """None-on-absent: no promoted_at stamp → stable.promoted_at None (lag then
    fails loud = stuck, the safe default); no pin → pin None (not frozen);
    corrupt False. Also holds when there is no release.json at all."""
    # State with stable but no promoted_at, no pin.
    state = rm.ReleaseState(stable={"sha": "s", "version": "2026.0614.2884"})
    view = rm.release_ui_view(_d8_cfg(), state)
    assert view["stable"]["promoted_at"] is None
    assert view["pin"] is None
    assert view["corrupt"] is False
    # No release.json yet (state None) → still a canary view, deltas all empty.
    none_view = rm.release_ui_view(_d8_cfg(), None)
    assert none_view["stable"]["promoted_at"] is None
    assert none_view["pin"] is None
    assert none_view["corrupt"] is False


def test_d8_release_ui_view_corrupt_is_an_input_not_detected():
    """corrupt is passed IN (detection stays at the I/O boundary so the view
    stays pure): the flag reflects the argument, independent of state."""
    cfg = _d8_cfg()
    state = rm.ReleaseState(stable={"sha": "s", "version": "2026.0614.2884",
                                    "promoted_at": "2026-06-16T10:00:00+00:00"})
    assert rm.release_ui_view(cfg, state, corrupt=True)["corrupt"] is True
    assert rm.release_ui_view(cfg, None, corrupt=True)["corrupt"] is True
    assert rm.release_ui_view(cfg, state, corrupt=False)["corrupt"] is False
    # Direct mode still opts out entirely even with corrupt=True.
    assert rm.release_ui_view(rm.ReleaseConfig(mode="direct"), None,
                              corrupt=True) is None


def test_d8_release_ui_view_keys_superset_of_fixture_release_shape():
    """Parity guard: every key the #2954 fixture puts on an `inputs.release`
    object must be producible by release_ui_view, so what we emit lines up 1:1
    with the shape `_classifyUpdates` was verified against. Skips until the
    fixture lands on main (it ships in the D-8 contract PR)."""
    if not _D8_FIXTURE.exists():
        pytest.skip("D-8 fixture not present until the contract PR merges")
    fixture = json.loads(_D8_FIXTURE.read_text(encoding="utf-8"))
    union: set[str] = set()
    for case in fixture["cases"]:
        rel = case.get("inputs", {}).get("release")
        if isinstance(rel, dict):
            union |= set(rel.keys())
    # A fully-populated canary view (stable+promoted, pin, soaking candidate).
    state = rm.ReleaseState(
        stable={"sha": "s", "version": "2026.0614.2884",
                "promoted_at": "2026-06-16T10:00:00+00:00"},
        candidate={"sha": "abc123", "state": "soaking",
                   "soak_started_at": "2026-06-16T11:50:00+00:00"},
        pin={"sha": "e970a96e", "reason": "manual freeze"},
    )
    view = rm.release_ui_view(_d8_cfg(), state)
    missing = union - set(view.keys())
    assert not missing, f"view missing fixture release keys: {sorted(missing)}"
    assert "promoted_at" in view["stable"]  # nested mirror present


def test_d2_candidate_recency_stamped_at_detection(pod):
    """A new candidate is stamped with its ancestry vs stable at detection, so
    the soaking banner can say "N commits ahead of the fleet" without git."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)  # stable = A
    sha_b = _push_commit(pod, "B candidate (#2884)", {"z.py": "Z = 1\n"})
    _tick(pod, deps)  # detect + soak

    state = rm.load_release_state(pod["shared"])
    assert state.candidate["sha"] == sha_b
    assert state.candidate.get("commits_ahead") == 1, "one commit ahead of A"
    assert state.candidate.get("commit_date")

    view = rm.release_ui_view(_cfg(pod), state)
    assert view["candidate"]["commits_ahead"] == 1
    assert view["candidate"]["commit_date"]


def test_d2_ancestry_counts_directionality(pod):
    """Unit the ancestry primitive directly: B descends from A by one commit."""
    rec, clock = Recorder(), Clock()
    deps = _deps(pod, rec, clock)
    _tick(pod, deps)
    sha_a = pod["sha_a"]
    sha_b = _push_commit(pod, "B (#1)", {"b.py": "B = 1\n"})
    _run(["git", "fetch", "-q", "origin"], pod["fleet"])

    fwd = rm.ancestry_counts(pod["fleet"], sha_a, sha_b)   # A → B
    assert fwd == {"ahead": 1, "behind": 0}
    back = rm.ancestry_counts(pod["fleet"], sha_b, sha_a)  # B → A
    assert back == {"ahead": 0, "behind": 1}
    same = rm.ancestry_counts(pod["fleet"], sha_a, sha_a)
    assert same == {"ahead": 0, "behind": 0}
    # Unresolvable pair degrades to None, never raises.
    bad = rm.ancestry_counts(pod["fleet"], "", sha_b)
    assert bad == {"ahead": None, "behind": None}


def test_d2_cli_relation_phrase_wording():
    """The CLI renders ancestry counts as plain words so the operator never
    subtracts PR numbers. The incident case (stable ahead) must say 'newer'."""
    from evolve_admin.release_cli import _relation_phrase
    txt, tone = _relation_phrase({"ahead": 5, "behind": 0},
                                 subject="stable", base="previous")
    assert txt == "stable is 5 commits ahead of previous (newer)"
    assert tone == "green"

    txt, tone = _relation_phrase({"ahead": 1, "behind": 0},
                                 subject="stable", base="previous")
    assert "1 commit ahead" in txt and "commits" not in txt  # singular

    txt, tone = _relation_phrase({"ahead": 0, "behind": 3},
                                 subject="stable", base="previous")
    assert txt == "stable is 3 commits behind previous (older)"
    assert tone == "yellow"

    txt, _ = _relation_phrase({"ahead": 2, "behind": 1},
                              subject="candidate", base="stable")
    assert "diverged" in txt

    # Unknown relationship → empty clause (the CLI omits it).
    assert _relation_phrase(None, subject="s", base="p") == ("", "dim")
    assert _relation_phrase({"ahead": None, "behind": None},
                            subject="s", base="p") == ("", "dim")
