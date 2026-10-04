"""Regression tests for forge_jobs lifecycle robustness fixes.

Covers:
- Race-free ``run_id`` allocation in ``create_improvement_job`` (concurrent
  triggers must yield distinct run_ids).
- ``auto_reject_stale_awaiting_approval`` timeout sweep — rejects stale
  jobs, clears manifest install_job, rolls back manifest status.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import forge_jobs  # noqa: E402
from evolve_admin.applications.forge_jobs import (  # noqa: E402
    TERMINAL_JOB_STATES,
    auto_reject_stale_awaiting_approval,
    complete_job,
    create_improvement_job,
    create_install_job,
    list_active_jobs,
    list_recent_jobs,
    load_job,
    mark_awaiting_approval,
    prune_old_jobs,
    reject_job,
    save_job,
)
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    load_manifest,
    save_manifest,
)


# ── Manifest-path redirect ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _route_manifests_to_tmp(tmp_path, monkeypatch):
    """Re-point ``applications_dir`` at a tmp tree so save_manifest doesn't
    try to write to ``/Users/<synthetic-bot>/.openclaw/workspace/manifests/``.

    Post-PR #1176, manifests live bot-side. These tests use synthetic bot
    ids (``bot_a``, ``bot_d``) that don't exist on the test host, so we
    redirect the resolver via the manifest module's ``applications_dir``.
    """
    def _appdir(shared_dir, bot_id):
        out = tmp_path / "workspaces" / bot_id / "manifests"
        out.mkdir(parents=True, exist_ok=True)
        return out
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir)


# ─────────────────────────────────────────────────────────────────────────────
# Fix #1 — race in run_id allocation
# ─────────────────────────────────────────────────────────────────────────────


def test_create_improvement_job_serial_yields_increasing_run_ids(tmp_path: Path):
    """Sanity: sequential calls produce r-00000001, r-00000002, ..."""
    jobs = [
        create_improvement_job(
            pkg_id="p-aaaa1111",
            app_id="my-app",
            bot_id="bot_a",
            shared_dir=tmp_path,
        )
        for _ in range(3)
    ]
    assert [j.run_id for j in jobs] == [
        "r-00000001",
        "r-00000002",
        "r-00000003",
    ]


def test_create_improvement_job_concurrent_yields_distinct_run_ids(tmp_path: Path):
    """Two concurrent threads must NOT both pick the same run_number.

    The bug this guards against: ``len(existing) + 1`` had a TOCTOU race
    where two callers saw the same count and allocated the same run_id.
    """
    pkg_id = "p-bbbb2222"
    bot_id = "bot_b"
    app_id = "concurrent-app"
    n = 8

    def _do() -> str:
        job = create_improvement_job(
            pkg_id=pkg_id,
            app_id=app_id,
            bot_id=bot_id,
            shared_dir=tmp_path,
        )
        return job.run_id

    with ThreadPoolExecutor(max_workers=n) as pool:
        run_ids = list(pool.map(lambda _: _do(), range(n)))

    # All distinct
    assert len(set(run_ids)) == n, f"duplicate run_ids: {sorted(run_ids)}"
    # Form a contiguous block 1..n (the counter increments by 1 each time)
    nums = sorted(int(r.split("-", 1)[1]) for r in run_ids)
    assert nums == list(range(1, n + 1))


def test_run_id_counter_seeds_from_existing_max(tmp_path: Path):
    """If a deployment has pre-existing jobs but no counter file yet, the
    counter must seed from max(existing run numbers) so it doesn't collide."""
    pkg_id = "p-cccc3333"
    bot_id = "bot_c"
    app_id = "seeded-app"

    # Pre-seed three improvement jobs the "old way" by writing them
    # directly with hand-picked run_ids (mimics a deployment that
    # predates the counter file).
    for n in (1, 2, 3):
        job = forge_jobs.ForgeJob(
            job_id=forge_jobs.new_job_id(),
            run_id=forge_jobs.run_id_for(n),
            job_type="improvement",
            pkg_id=pkg_id,
            app_id=app_id,
            bot_id=bot_id,
            pkg_version_before=None,
            gallery_version=None,
            steps=forge_jobs._improvement_steps(),
            created_at=forge_jobs.now_iso(),
            last_updated=forge_jobs.now_iso(),
        )
        save_job(job, tmp_path)

    # Now allocate via the new counter — must continue from 4, not from 1.
    job = create_improvement_job(
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        shared_dir=tmp_path,
    )
    assert job.run_id == "r-00000004"


# ─────────────────────────────────────────────────────────────────────────────
# Fix #3 — auto-reject stale awaiting_approval jobs
# ─────────────────────────────────────────────────────────────────────────────


def _seed_manifest(
    tmp_path: Path,
    bot_id: str,
    app_id: str,
    *,
    status: str,
    install_job: dict | None,
) -> ApplicationManifest:
    m = ApplicationManifest(
        id=app_id,
        name=app_id,
        bot_id=bot_id,
        status=status,
        install_job=install_job,
    )
    save_manifest(m, tmp_path)
    return m


def _seed_awaiting_approval_job(
    tmp_path: Path,
    *,
    bot_id: str,
    app_id: str,
    pkg_id: str,
    job_type: str,
    age_days: float,
) -> forge_jobs.ForgeJob:
    """Persist an awaiting_approval job with last_updated rewound by age_days."""
    job = forge_jobs.ForgeJob(
        job_id=forge_jobs.new_job_id(),
        run_id=forge_jobs.run_id_for(1),
        job_type=job_type,
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        pkg_version_before=None,
        gallery_version=None,
        steps=(
            forge_jobs._install_steps() if job_type == "install"
            else forge_jobs._improvement_steps()
        ),
        created_at=forge_jobs.now_iso(),
        last_updated=forge_jobs.now_iso(),
    )
    save_job(job, tmp_path)
    mark_awaiting_approval(job, tmp_path)

    # Rewind last_updated to simulate age.
    rewound = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    job.last_updated = rewound
    save_job(job, tmp_path)
    # save_job overwrites last_updated on every call (it's set to now_iso()
    # inside save_job). So we re-write the JSON directly to pin the time.
    import json as _json

    path = forge_jobs._active_path(job.job_id, tmp_path)
    data = _json.loads(path.read_text())
    data["last_updated"] = rewound
    path.write_text(_json.dumps(data, indent=2))
    return job


def test_auto_reject_skips_fresh_jobs(tmp_path: Path):
    """A job that's only been awaiting_approval for an hour must not be touched."""
    _seed_manifest(
        tmp_path,
        bot_id="bot_d",
        app_id="fresh-app",
        status="updating",
        install_job={"job_id": "placeholder"},
    )
    job = _seed_awaiting_approval_job(
        tmp_path,
        bot_id="bot_d",
        app_id="fresh-app",
        pkg_id="p-dddd4444",
        job_type="improvement",
        age_days=0.04,  # ~1 hour
    )

    rejected = auto_reject_stale_awaiting_approval(tmp_path, threshold_days=7)

    assert rejected == []
    reloaded = load_job(job.job_id, tmp_path)
    assert reloaded is not None
    assert reloaded.status == "awaiting_approval"


def test_auto_reject_rolls_back_improvement_manifest(tmp_path: Path):
    """A stale improvement job: job → rejected, manifest.install_job cleared,
    status rolled back to active."""
    job = _seed_awaiting_approval_job(
        tmp_path,
        bot_id="bot_e",
        app_id="stale-improve",
        pkg_id="p-eeee5555",
        job_type="improvement",
        age_days=10,
    )
    _seed_manifest(
        tmp_path,
        bot_id="bot_e",
        app_id="stale-improve",
        status="updating",
        install_job={"job_id": job.job_id, "phase": "awaiting_approval"},
    )

    rejected = auto_reject_stale_awaiting_approval(tmp_path, threshold_days=7)

    assert rejected == [job.job_id]
    reloaded = load_job(job.job_id, tmp_path)
    assert reloaded is not None
    assert reloaded.status == "rejected"

    manifest = load_manifest("stale-improve", "bot_e", tmp_path)
    assert manifest is not None
    assert manifest.install_job is None
    assert manifest.status == "active"


def test_auto_reject_removes_install_manifest(tmp_path: Path):
    """A stale install job: the manifest only existed for the build, so it
    gets removed (per spec — "removed entirely for never-completed installs")."""
    job = _seed_awaiting_approval_job(
        tmp_path,
        bot_id="bot_f",
        app_id="stale-install",
        pkg_id="p-ffff6666",
        job_type="install",
        age_days=10,
    )
    _seed_manifest(
        tmp_path,
        bot_id="bot_f",
        app_id="stale-install",
        status="updating",
        install_job={"job_id": job.job_id, "phase": "awaiting_approval"},
    )

    rejected = auto_reject_stale_awaiting_approval(tmp_path, threshold_days=7)

    assert rejected == [job.job_id]

    manifest = load_manifest("stale-install", "bot_f", tmp_path)
    assert manifest is None


def test_auto_reject_only_clears_install_job_if_still_ours(tmp_path: Path):
    """If the manifest's install_job has moved on to a newer job, leave it.

    Defensive: avoids stomping on a run that started after the stale one.
    """
    stale = _seed_awaiting_approval_job(
        tmp_path,
        bot_id="bot_g",
        app_id="reused",
        pkg_id="p-gggg7777",
        job_type="improvement",
        age_days=10,
    )
    # Manifest now references a different (hypothetical) job.
    _seed_manifest(
        tmp_path,
        bot_id="bot_g",
        app_id="reused",
        status="updating",
        install_job={"job_id": "j-newer-job", "phase": "running"},
    )

    rejected = auto_reject_stale_awaiting_approval(tmp_path, threshold_days=7)

    assert rejected == [stale.job_id]
    manifest = load_manifest("reused", "bot_g", tmp_path)
    assert manifest is not None
    # The newer job's reference was preserved.
    assert manifest.install_job == {"job_id": "j-newer-job", "phase": "running"}
    assert manifest.status == "updating"  # left for the newer run


# ─────────────────────────────────────────────────────────────────────────────
# list_recent_jobs — covers the Forge Jobs admin page "successful installs are
# invisible" bug. /api/forge/jobs used to call list_active_jobs alone, which
# skipped completed jobs entirely — so the UI saw a wall of recent failures
# and never the successes that followed.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_completed_job(
    tmp_path: Path,
    *,
    job_id: str,
    bot_id: str,
    app_id: str,
    pkg_id: str,
    status: str = "complete",
    age_days: float = 0.0,
) -> None:
    """Drop a job JSON straight into the completed dir at a controlled age."""
    import json
    completed = tmp_path / "forge" / "jobs" / "completed"
    completed.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    data = {
        "job_id": job_id,
        "run_id": "r-00000001",
        "job_type": "install",
        "pkg_id": pkg_id,
        "app_id": app_id,
        "bot_id": bot_id,
        "pkg_version_before": None,
        "gallery_version": "g-test",
        "steps": [],
        "status": status,
        "created_at": created.isoformat(),
        "last_updated": created.isoformat(),
    }
    (completed / f"{job_id}.json").write_text(json.dumps(data))


def test_list_recent_jobs_includes_active_and_recent_completed(tmp_path: Path):
    """Active jobs always returned; completed jobs returned when within window."""
    # Active queued job
    active = create_install_job(
        pkg_id="p-aaaa1111", app_id="journal", bot_id="team_bot_a",
        gallery_version="g-2026-05-15", shared_dir=tmp_path,
    )
    # Recent completed (within 7 days)
    _seed_completed_job(
        tmp_path, job_id="j-recent-001", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-aaaa1111", age_days=2.0,
    )
    # Old completed (outside 7 days — must be filtered out)
    _seed_completed_job(
        tmp_path, job_id="j-old-001", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-aaaa1111", age_days=30.0,
    )

    jobs = list_recent_jobs(tmp_path, days=7)
    ids = sorted(j.job_id for j in jobs)
    assert active.job_id in ids
    assert "j-recent-001" in ids
    assert "j-old-001" not in ids


def test_list_recent_jobs_returns_active_when_completed_dir_missing(tmp_path: Path):
    """No completed dir on disk yet — must not crash, just return actives."""
    active = create_install_job(
        pkg_id="p-bbbb2222", app_id="journal", bot_id="team_bot_a",
        gallery_version="g-x", shared_dir=tmp_path,
    )
    jobs = list_recent_jobs(tmp_path, days=7)
    assert [j.job_id for j in jobs] == [active.job_id]


def test_list_recent_jobs_window_is_inclusive_of_today(tmp_path: Path):
    """A job completed seconds ago must always be returned."""
    _seed_completed_job(
        tmp_path, job_id="j-just-now", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-cccc3333", age_days=0.0,
    )
    jobs = list_recent_jobs(tmp_path, days=7)
    assert any(j.job_id == "j-just-now" for j in jobs)


# ─────────────────────────────────────────────────────────────────────────────
# reject_job — rejection metadata persistence
#
# Before this PR, reject_job accepted ``reason`` + ``rejected_by`` but silently
# dropped both — the Forge Jobs UI ended up showing rows in the "rejected"
# state with no operator-visible explanation. These tests pin the metadata
# write so it doesn't regress.
# ─────────────────────────────────────────────────────────────────────────────


def test_reject_job_persists_reason_and_actor(tmp_path: Path):
    """reject_job must write reason, rejected_by, and rejected_at to disk."""
    job = create_install_job(
        pkg_id="p-aaaa1111", app_id="journal", bot_id="team_bot_a",
        gallery_version="g-2026-05-15", shared_dir=tmp_path,
    )
    mark_awaiting_approval(job, tmp_path)

    reject_job(
        job, tmp_path,
        rejected_by="operator",
        reason="failing tests after refine",
    )

    reloaded = load_job(job.job_id, tmp_path)
    assert reloaded is not None
    assert reloaded.status == "rejected"
    assert reloaded.reject_reason == "failing tests after refine"
    assert reloaded.rejected_by == "operator"
    assert reloaded.rejected_at  # ISO timestamp set
    # Sanity: rejected_at parses as ISO-8601
    datetime.fromisoformat(reloaded.rejected_at.replace("Z", "+00:00"))


def test_reject_job_tolerates_empty_reason(tmp_path: Path):
    """Some callers pass reason="" — the field must remain a string, not None."""
    job = create_install_job(
        pkg_id="p-aaaa1112", app_id="journal", bot_id="team_bot_a",
        gallery_version="g-x", shared_dir=tmp_path,
    )
    mark_awaiting_approval(job, tmp_path)

    reject_job(job, tmp_path, rejected_by="operator", reason="")

    reloaded = load_job(job.job_id, tmp_path)
    assert reloaded is not None
    assert reloaded.reject_reason == ""
    assert reloaded.rejected_by == "operator"


def test_auto_reject_sweep_captures_actor_and_reason(tmp_path: Path):
    """Verify the auto-reject sweep also goes through reject_job and lands
    a populated rejected_by + non-empty reason on disk."""
    _seed_manifest(
        tmp_path, bot_id="bot_h", app_id="autorej",
        status="updating",
        install_job={"job_id": "placeholder"},
    )
    job = _seed_awaiting_approval_job(
        tmp_path, bot_id="bot_h", app_id="autorej",
        pkg_id="p-hhhh8888", job_type="improvement", age_days=10,
    )

    rejected = auto_reject_stale_awaiting_approval(tmp_path, threshold_days=7)
    assert rejected == [job.job_id]

    reloaded = load_job(job.job_id, tmp_path)
    assert reloaded is not None
    assert reloaded.status == "rejected"
    assert reloaded.rejected_by == "forge_sweep"
    # The sweep's reason mentions the threshold so an operator can tell at a
    # glance this was auto-rejected, not a manual call.
    assert reloaded.reject_reason
    assert reloaded.rejected_at


# ─────────────────────────────────────────────────────────────────────────────
# list_recent_jobs sort order + rejected inclusion
#
# Before this PR, the Forge Jobs page rendered in directory-iteration order
# (effectively random) with rejected jobs sticking around forever. The UI
# now expects: actives first, then most-recent terminals, with rejected
# treated like complete/failed for the 7-day cutoff.
# ─────────────────────────────────────────────────────────────────────────────


def test_terminal_states_constant_includes_rejected(tmp_path: Path):
    """Pin the set so we don't accidentally drop 'rejected' again."""
    assert "rejected" in TERMINAL_JOB_STATES
    assert "complete" in TERMINAL_JOB_STATES
    assert "failed" in TERMINAL_JOB_STATES
    assert "cancelled" in TERMINAL_JOB_STATES


def test_list_recent_jobs_filters_rejected_by_age(tmp_path: Path):
    """Rejected jobs older than the window must be filtered out — they
    used to stay visible indefinitely."""
    _seed_completed_job(
        tmp_path, job_id="j-recent-reject", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-rrrr0001", status="rejected", age_days=2.0,
    )
    _seed_completed_job(
        tmp_path, job_id="j-old-reject", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-rrrr0002", status="rejected", age_days=30.0,
    )

    jobs = list_recent_jobs(tmp_path, days=7)
    ids = {j.job_id for j in jobs}
    assert "j-recent-reject" in ids
    assert "j-old-reject" not in ids


def test_list_recent_jobs_sorts_actives_first_then_newest_terminal(tmp_path: Path):
    """Active queued/running jobs render before terminal ones, and within
    terminals the newest sorts to the top."""
    # Two completed jobs of different ages
    _seed_completed_job(
        tmp_path, job_id="j-old-complete", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-aaaa1111", status="complete", age_days=5.0,
    )
    _seed_completed_job(
        tmp_path, job_id="j-newer-complete", bot_id="team_bot_a", app_id="journal",
        pkg_id="p-aaaa1111", status="complete", age_days=1.0,
    )
    # An active job (created fresh)
    active = create_install_job(
        pkg_id="p-bbbb2222", app_id="other", bot_id="team_bot_a",
        gallery_version="g-x", shared_dir=tmp_path,
    )

    jobs = list_recent_jobs(tmp_path, days=7)
    ids = [j.job_id for j in jobs]

    # Active must precede terminals
    assert ids.index(active.job_id) < ids.index("j-newer-complete")
    assert ids.index(active.job_id) < ids.index("j-old-complete")
    # Among terminals, newer is first
    assert ids.index("j-newer-complete") < ids.index("j-old-complete")


# ─────────────────────────────────────────────────────────────────────────────
# prune_old_jobs — disk-side pruning of terminal job files
#
# Wired into forge_sweep at 30-day default so the Forge Jobs disk footprint
# doesn't grow without bound. The UI window is 7 days; the disk window is
# 30, giving operators ~3 weeks of headroom to dig into a recent failure
# that's scrolled off the page.
# ─────────────────────────────────────────────────────────────────────────────


def test_prune_old_jobs_removes_old_terminal_files(tmp_path: Path):
    """Jobs older than the cutoff are deleted from completed/."""
    _seed_completed_job(
        tmp_path, job_id="j-stale", bot_id="team_bot_a", app_id="x",
        pkg_id="p-pppp0001", status="complete", age_days=60.0,
    )
    _seed_completed_job(
        tmp_path, job_id="j-fresh", bot_id="team_bot_a", app_id="x",
        pkg_id="p-pppp0002", status="complete", age_days=5.0,
    )

    pruned = prune_old_jobs(tmp_path, days=30)
    assert pruned == 1

    completed = tmp_path / "forge" / "jobs" / "completed"
    remaining = {p.stem for p in completed.iterdir() if p.suffix == ".json"}
    assert "j-stale" not in remaining
    assert "j-fresh" in remaining


def test_prune_old_jobs_handles_all_terminal_statuses(tmp_path: Path):
    """complete, failed, cancelled, rejected — all eligible once old enough."""
    for i, status in enumerate(("complete", "failed", "cancelled", "rejected")):
        _seed_completed_job(
            tmp_path, job_id=f"j-old-{status}", bot_id="team_bot_a", app_id="x",
            pkg_id=f"p-tttt{i:04d}", status=status, age_days=60.0,
        )

    pruned = prune_old_jobs(tmp_path, days=30)
    assert pruned == 4

    completed = tmp_path / "forge" / "jobs" / "completed"
    assert not list(completed.glob("*.json"))


def test_prune_old_jobs_dry_run_is_noop(tmp_path: Path):
    """dry_run=True returns the count but doesn't delete anything."""
    _seed_completed_job(
        tmp_path, job_id="j-stale", bot_id="team_bot_a", app_id="x",
        pkg_id="p-pppp0001", status="complete", age_days=60.0,
    )

    pruned = prune_old_jobs(tmp_path, days=30, dry_run=True)
    assert pruned == 1

    completed = tmp_path / "forge" / "jobs" / "completed"
    assert (completed / "j-stale.json").exists()


def test_prune_old_jobs_returns_zero_when_no_completed_dir(tmp_path: Path):
    """A fresh pod with no terminal jobs yet must not crash."""
    assert prune_old_jobs(tmp_path, days=30) == 0


def test_prune_old_jobs_skips_jobs_in_window(tmp_path: Path):
    """Nothing inside the cutoff is touched, even with explicit days=30."""
    _seed_completed_job(
        tmp_path, job_id="j-fresh", bot_id="team_bot_a", app_id="x",
        pkg_id="p-pppp0002", status="complete", age_days=10.0,
    )

    assert prune_old_jobs(tmp_path, days=30) == 0
    completed = tmp_path / "forge" / "jobs" / "completed"
    assert (completed / "j-fresh.json").exists()
