"""
Tests for the lineage re-point CLI (lineage_repoint.py).

A synthetic tmp workspace mirrors bot ``atlas``'s retired-monolith→split shape:
a monolithic digest app (retired spec_ids ``p-c07ba3b2`` / ``p-6e6c474a``) was
split into three live successors —

  - Capture Pipeline   ``p-555f7671``
  - Daily Digest       ``p-7b26ba5e``
  - Article Capture    ``p-df9d99a3``  (the broad successor — claims most files)

Live files on disk still carry the retired ids in their ``_evolve`` markers but
no ``prior_spec_ids[]`` lineage records the split. The tree carries one file of
each disposition the planner must distinguish:

  - sole-claimant re-point, multi-claimant needs-decision, zero-claimant
    needs-decision, digest-output strip-keep, a healthy marker (skipped), and
    the adversarial "retired id that is ALSO a live spec elsewhere" case.

Placeholder bot name only (no real bot identity) per the public-launch scrub
guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.provenance import embed_marker, parse_marker
from evolve_admin.applications import lineage_repoint as lr

# Retired (monolith) ids — resolve to no live Instance.
RETIRED_A = "p-c07ba3b2"
RETIRED_B = "p-6e6c474a"
# A retired id carried only by the zero-claimant file — no re-point ever binds
# it, so no lineage side-effect resolves it.
LONELY_RETIRED = "p-99999999"
# Live successor ids.
CAPTURE = "p-555f7671"
DIGEST = "p-7b26ba5e"
ARTICLE = "p-df9d99a3"


# ── Builders ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    bot_id = "personal_bot"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)

    # compute_plan / apply_plan resolve bot homes through lineage_repoint.bot_home.
    monkeypatch.setattr(lr, "bot_home", lambda _bid, *a, **k: bot_dir)

    return {
        "bot_id": bot_id,
        "bot_dir": bot_dir,
        "workspace": workspace,
        "manifests": workspace / "manifests",
    }


def _make_instance(env, name: str, spec_id: str, realized_rel: list[str]) -> Path:
    inst = {
        "instance_id": name,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.06.20-1.0",
            "installed_at": "2026-06-20T00:00:00Z",
            "installed_by": "test",
        },
        # Faithful to live atlas: realized_files store workspace-RELATIVE paths.
        "realized_files": [
            {"logical_name": Path(r).stem, "path": r,
             "file_id": f"f-{abs(hash(r)) % 10**8:08d}", "marker_state": "OWNED"}
            for r in realized_rel
        ],
        "status": "active",
    }
    p = env["manifests"] / f"{name}.json"
    p.write_text(json.dumps(inst, indent=2))
    return p


def _write(env, relpath: str, content: str) -> Path:
    p = env["workspace"] / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _stamp(p: Path, spec_ids: list[str], file_id: str) -> None:
    embed_marker(
        p, pkg_ids=spec_ids, file_id=file_id,
        pkg_versions={s: "2026.06.20-1.0" for s in spec_ids},
        file_version="2026.06.20-1.0", keyword="spec", merge=False,
    )


def _marker_ids(p: Path) -> list[str]:
    m = parse_marker(p)
    return list(m.spec_ids) if (m and m.is_valid()) else []


def _has_marker(p: Path) -> bool:
    m = parse_marker(p)
    return m is not None and m.is_valid()


@pytest.fixture
def populated(env):
    """The canonical mixed tree. Returns path handles + the spec → manifest map."""
    # Live successors.
    _make_instance(env, "capture-pipeline", CAPTURE,
                   ["atlas/operator.json", "scripts/atlas_lib/composer.py"])
    _make_instance(env, "daily-digest", DIGEST,
                   ["scripts/atlas_digest.py", "atlas/sources.json",
                    "atlas/operator.json"])
    _make_instance(env, "article-capture", ARTICLE,
                   ["scripts/atlas_capture.py", "atlas/optout.json",
                    "atlas/research-budget.json", "atlas/sources.json",
                    "digest/2026-06-05.md",
                    "digest/source_health-2026-06-05.json"])

    files: dict[str, Path] = {}

    # 1. sole-claimant re-point: only article-capture claims atlas_capture.py.
    p = _write(env, "scripts/atlas_capture.py", "#!/usr/bin/env python3\nx=1\n")
    _stamp(p, [RETIRED_B], "f-cap00001")
    files["capture"] = p

    # 2. sole-claimant re-point, marker carries TWO retired ids.
    p = _write(env, "atlas/optout.json", json.dumps({"opted_out": []}))
    _stamp(p, [RETIRED_A, RETIRED_B], "f-opt00001")
    files["optout"] = p

    # 3. sole-claimant re-point: only article-capture claims research-budget.
    p = _write(env, "atlas/research-budget.json", json.dumps({"budget": 10}))
    _stamp(p, [RETIRED_A], "f-bud00001")
    files["budget"] = p

    # 4. multi-claimant → needs-decision: sources.json claimed by digest+article.
    p = _write(env, "atlas/sources.json", json.dumps({"sources": []}))
    _stamp(p, [RETIRED_A], "f-src00001")
    files["sources"] = p

    # 5. zero-claimant → needs-decision: no successor lists lonely.json, and its
    #    retired id is UNIQUE (shared with no re-pointed file) so no lineage
    #    side-effect resolves it — it stays a genuine operator decision.
    p = _write(env, "atlas/lonely.json", json.dumps({"x": 1}))
    _stamp(p, [LONELY_RETIRED], "f-lon00001")
    files["lonely"] = p

    # 6. digest-output → strip-keep (dated markdown digest).
    p = _write(env, "digest/2026-06-05.md", "# Digest 2026-06-05\nbody\n")
    _stamp(p, [RETIRED_A], "f-dig00001")
    files["digest_md"] = p

    # 7. digest-output → strip-keep (source_health json).
    p = _write(env, "digest/source_health-2026-06-05.json",
               json.dumps({"ok": True}))
    _stamp(p, [RETIRED_A], "f-dig00002")
    files["digest_health"] = p

    # 8. healthy marker (live id) → never in plan.
    p = _write(env, "scripts/atlas_lib/composer.py", "y=2\n")
    _stamp(p, [CAPTURE], "f-comp0001")
    files["healthy"] = p

    # 9. MIXED marker: a live id + a retired extra → already resolves via the
    #    live id; the retired extra is cruft, NOT a re-point target → not in plan.
    p = _write(env, "scripts/atlas_guard.py", "g=3\n")
    _stamp(p, [DIGEST, RETIRED_A], "f-guard001")
    files["mixed"] = p

    # 10. INELIGIBLE path (OC task substrate) carrying a retired marker →
    #     strip_stale_markers' domain, never bound to an app → not in plan.
    p = _write(env, "tasks.json", json.dumps({"tasks": []}))
    _stamp(p, [RETIRED_A], "f-tasks001")
    files["ineligible"] = p

    return files


def _plan_by_path(env) -> dict[str, lr.PlanRow]:
    return {r.path: r for r in lr.compute_plan(env["bot_id"])}


# ── Per-file successor resolution ─────────────────────────────────────────────

def test_sole_claimant_repoints_to_successor(env, populated):
    plan = _plan_by_path(env)
    row = plan["scripts/atlas_capture.py"]
    assert row.action == lr.REPOINT
    assert row.bind_spec_id == ARTICLE
    assert row.bind_manifest == "article-capture.json"
    assert row.orphaned_ids == [RETIRED_B]


def test_two_retired_ids_both_recorded_on_sole_successor(env, populated):
    row = _plan_by_path(env)["atlas/optout.json"]
    assert row.action == lr.REPOINT
    assert row.bind_spec_id == ARTICLE
    assert sorted(row.orphaned_ids) == sorted([RETIRED_A, RETIRED_B])


def test_multi_claimant_is_needs_decision(env, populated):
    row = _plan_by_path(env)["atlas/sources.json"]
    assert row.action == lr.NEEDS_DECISION
    # Both successors that claim it appear as candidates; nothing is guessed.
    blob = "; ".join(row.candidates)
    assert DIGEST in blob and ARTICLE in blob
    assert row.bind_spec_id is None


def test_zero_claimant_is_needs_decision(env, populated):
    row = _plan_by_path(env)["atlas/lonely.json"]
    assert row.action == lr.NEEDS_DECISION
    assert row.candidates == []
    assert row.bind_spec_id is None


def test_digest_output_is_strip_keep(env, populated):
    plan = _plan_by_path(env)
    for rel in ("digest/2026-06-05.md", "digest/source_health-2026-06-05.json"):
        assert plan[rel].action == lr.STRIP_KEEP
        assert plan[rel].bind_spec_id is None


def test_healthy_marker_not_in_plan(env, populated):
    plan = _plan_by_path(env)
    assert "scripts/atlas_lib/composer.py" not in plan


def test_marker_with_a_live_id_not_in_plan(env, populated):
    """A file whose marker carries a live id (plus a retired extra) already
    resolves via the live id — it is NOT a re-point target."""
    assert "scripts/atlas_guard.py" not in _plan_by_path(env)


def test_ineligible_path_not_in_plan(env, populated):
    """A stale marker on a never-ownable path is strip_stale_markers' domain,
    never a lineage re-point candidate."""
    assert "tasks.json" not in _plan_by_path(env)


def test_is_digest_output_predicate():
    assert lr.is_digest_output("digest/2026-06-05.md")
    assert lr.is_digest_output("digest/source_health-2026-06-05.json")
    assert not lr.is_digest_output("scripts/atlas_capture.py")
    assert not lr.is_digest_output("atlas/sources.json")


def test_retired_id_also_live_elsewhere_not_orphaned(env, populated):
    """Adversarial: a marker id that is ALSO some app's LIVE current id must
    resolve (current-id wins) and never be treated as orphaned/re-pointable."""
    from evolve_admin.applications.spec_lineage import build_spec_index
    instances = [i.data for i in lr.load_instances(env["bot_id"])]
    spec_index = build_spec_index(instances)

    class _M:  # minimal marker stand-in
        spec_ids = [CAPTURE, RETIRED_A]

    orphaned = lr._orphaned_marker_ids(_M(), spec_index)
    assert CAPTURE not in orphaned   # live id resolves
    assert RETIRED_A in orphaned     # retired id does not


# ── Dry-run purity ────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(env, populated, capsys):
    before = {p: (p.read_text(), p.stat().st_mtime_ns)
              for p in env["workspace"].rglob("*") if p.is_file()}
    rows = lr.compute_plan(env["bot_id"])
    lr._print_dry_run(env["bot_id"], rows)
    after = {p: (p.read_text(), p.stat().st_mtime_ns)
             for p in env["workspace"].rglob("*") if p.is_file()}
    assert before == after  # byte- and mtime-identical: no write leaked
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "NEEDS-OPERATOR-DECISION" in out


def test_cli_dry_run_default_changes_nothing(env, populated):
    before = {p: p.read_text() for p in env["workspace"].rglob("*") if p.is_file()}
    rc = lr.main(["--bot", env["bot_id"], "--shared-dir", str(env["bot_dir"])])
    assert rc == 0
    after = {p: p.read_text() for p in env["workspace"].rglob("*") if p.is_file()}
    assert before == after


# ── Apply (the mutation path) ─────────────────────────────────────────────────

def test_apply_repoints_marker_and_records_lineage(env, populated):
    rows = lr.compute_plan(env["bot_id"])
    report = lr.apply_plan(rows, env["bot_id"])
    assert report.failed == 0

    # Markers re-pointed to the live successor; retired ids gone.
    assert _marker_ids(populated["capture"]) == [ARTICLE]
    assert _marker_ids(populated["optout"]) == [ARTICLE]
    assert _marker_ids(populated["budget"]) == [ARTICLE]

    # Lineage recorded on the successor manifest (both retired ids).
    art = json.loads((env["manifests"] / "article-capture.json").read_text())
    chain = art["provenance"]["prior_spec_ids"]
    assert set(chain) >= {RETIRED_A, RETIRED_B}


def test_apply_strip_keep_preserves_content(env, populated):
    md = populated["digest_md"]
    body_before = md.read_text()
    rows = lr.compute_plan(env["bot_id"])
    lr.apply_plan(rows, env["bot_id"])
    assert not _has_marker(md)               # marker stripped
    assert "# Digest 2026-06-05" in md.read_text()  # content survives
    # The marker line is the only thing removed.
    assert "body" in md.read_text()


def test_apply_leaves_needs_decision_untouched(env, populated):
    lonely_before = populated["lonely"].read_text()
    rows = lr.compute_plan(env["bot_id"])
    report = lr.apply_plan(rows, env["bot_id"])
    assert report.needs_decision >= 1
    # The zero-claimant file is untouched (still carries its retired marker).
    assert populated["lonely"].read_text() == lonely_before
    assert _marker_ids(populated["lonely"]) == [LONELY_RETIRED]


def test_apply_is_idempotent(env, populated):
    rows = lr.compute_plan(env["bot_id"])
    lr.apply_plan(rows, env["bot_id"])
    # After apply, re-pointed markers resolve, lineage resolves the shared retired
    # ids, and digest markers are gone → the only residue is the zero-claimant
    # file (unique retired id, no lineage side-effect), which still legitimately
    # needs an operator decision.
    second = lr.compute_plan(env["bot_id"])
    residual = {r.path: r.action for r in second}
    assert residual == {"atlas/lonely.json": lr.NEEDS_DECISION}
    # Re-applying the residual plan touches nothing (the lone row is needs-decision).
    rep = lr.apply_plan(second, env["bot_id"])
    assert rep.applied == 0 and rep.failed == 0 and rep.needs_decision == 1


def test_apply_lineage_resolves_shared_file_marker(env, populated):
    """After lineage is recorded (driven by the unambiguous files), the
    shared sources.json retired marker resolves via lineage and is no longer
    flagged — its on-disk marker is never rewritten without an operator."""
    sources_marker_before = _marker_ids(populated["sources"])
    rows = lr.compute_plan(env["bot_id"])
    lr.apply_plan(rows, env["bot_id"])
    # On-disk marker untouched (needs-decision never rewrites it)…
    assert _marker_ids(populated["sources"]) == sources_marker_before == [RETIRED_A]
    # …but it now resolves via the recorded lineage, so it drops out of the plan.
    assert "atlas/sources.json" not in {r.path for r in lr.compute_plan(env["bot_id"])}


# ── Marker transform unit (add-before-remove ordering) ────────────────────────

def test_transform_repoint_add_before_remove(env):
    p = _write(env, "scripts/x.py", "#!/usr/bin/env python3\nz=0\n")
    _stamp(p, [RETIRED_A], "f-x0001")
    changed = lr._transform_marker_in_place(p, repoint_to=ARTICLE,
                                            strip_ids=[RETIRED_A])
    assert changed
    assert _marker_ids(p) == [ARTICLE]  # never transited an empty marker


def test_transform_strip_removes_marker(env):
    p = _write(env, "digest/d.md", "# d\nbody\n")
    _stamp(p, [RETIRED_A], "f-d0001")
    changed = lr._transform_marker_in_place(p, repoint_to=None,
                                            strip_ids=[RETIRED_A])
    assert changed and not _has_marker(p)


def test_repoint_preserves_v7_spec_keyword_form(env):
    """A partial id removal during re-point must not regress a v7 'spec='
    marker to v6 'pkg=' form (provenance.remove_pkg_id_from_marker keyword fix)."""
    p = _write(env, "scripts/k.py", "k=1\n")
    _stamp(p, [RETIRED_A], "f-k0001")  # _stamp uses keyword="spec"
    lr._transform_marker_in_place(p, repoint_to=ARTICLE, strip_ids=[RETIRED_A])
    m = parse_marker(p)
    assert m.keyword == "spec"          # not regressed to "pkg"
    assert m.spec_ids == [ARTICLE]


def test_transform_noop_when_already_clean(env):
    p = _write(env, "scripts/y.py", "z=1\n")
    _stamp(p, [ARTICLE], "f-y0001")
    # Re-pointing to the id it already carries, nothing to strip → no change.
    changed = lr._transform_marker_in_place(p, repoint_to=ARTICLE, strip_ids=[])
    assert not changed
