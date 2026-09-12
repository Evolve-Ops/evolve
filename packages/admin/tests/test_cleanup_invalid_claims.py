"""
Tests for the invalid-claim cleanup sweep (cleanup_invalid_claims.py).

A synthetic tmp workspace carries one v7-arc Instance whose ``realized_files[]``
mixes:
  - INVALID claims (never-ownable paths: a secret ``.bin``, an append-only
    ``*-log.jsonl``, a platform-telemetry ``rec-*.json``) — the recon ledger
    classifies each ``invalid_claim``; these must be removed.
  - An OWNED claim (a real script with a marker, claimed by the Instance) —
    bucket ``owned_ok``; must be RETAINED.
  - A MISSING-MARKER claim (a real ownable file on disk with no marker, claimed
    by the Instance) — bucket ``missing_marker``; must be RETAINED. This is the
    load-bearing over-removal guard: a real file the app legitimately owns is
    NEVER removed.

Asserts:
  - the dry-run plan lists exactly the invalid claims and changes nothing;
  - ``--apply`` removes exactly the invalid claims, retaining every legit claim;
  - the files on disk are never touched (reversibility);
  - the CLI never re-derives its own exclusion list — it reuses the ledger's
    invalid_claim bucket (a fabricated ledger drives the removal set).

Placeholder bot names only (no real bot identities) per the public-launch
scrub guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.provenance import embed_marker
from evolve_admin.applications import cleanup_invalid_claims as ci


# ── Fixtures / builders ───────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A single bot workspace under tmp_path with bot_home patched in BOTH the
    cleanup module and recon_ledger (the cleanup builds the ledger, which loads
    instances through recon_ledger.bot_home)."""
    bot_id = "personal_bot"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(rl, "bot_home", lambda _bid, *a, **k: bot_dir)
    monkeypatch.setattr(ci, "bot_home", lambda _bid, *a, **k: bot_dir)

    return {
        "bot_id": bot_id,
        "shared_dir": tmp_path / "shared",
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "scripts": workspace / "scripts",
    }


def _write(env, relpath: str, content: str) -> Path:
    p = env["workspace"] / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _stamp(p: Path, spec_id: str, file_id: str) -> None:
    embed_marker(
        p, pkg_ids=[spec_id], file_id=file_id,
        pkg_versions={spec_id: "2026.05.20-1.0"}, file_version="2026.05.20-1.0",
        keyword="spec", merge=False,
    )


def _rf(path: str, file_id: str) -> dict:
    return {"logical_name": Path(path).stem, "path": path,
            "file_id": file_id, "marker_state": "OWNED"}


def _make_instance(env, instance_id: str, spec_id: str, realized: list[dict]) -> Path:
    inst = {
        "instance_id": instance_id,
        "bot_id": env["bot_id"],
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": "2026.05.20-1.0",
            "installed_at": "2026-05-20T00:00:00Z",
            "installed_by": "test",
        },
        "realized_files": realized,
        "change_log": [],
        "status": "active",
    }
    ipath = env["manifests"] / f"{instance_id}.json"
    ipath.write_text(json.dumps(inst))
    return ipath


@pytest.fixture
def populated(env):
    """One Instance claiming 3 invalid + 1 owned + 1 missing-marker path."""
    # ── Invalid claims: real files on disk WITHOUT markers (so pass-1 doesn't
    #    see them; pass-2 classifies the claim invalid_claim). ──
    salt = _write(env, "member-hash-salt.bin", content="RAWSALTBYTES")
    log = _write(env, "capture-log.jsonl", content='{"e":1}\n{"e":2}\n')
    telemetry = _write(env, "evolve/audit_outbox/rec-9.json", content='{"x":1}')

    # ── Owned claim: real file WITH a marker (owned_ok). Stored absolute, the
    #    extend_application shape. ──
    owned = _write(env, "scripts/keep.py", content="x = 1\n")
    _stamp(owned, "p-aaaa1111", "f-keep0001")

    # ── Missing-marker claim: real ownable file on disk, NO marker (the
    #    over-removal guard target — a real file the app legitimately owns). ──
    legit_unmarked = _write(env, "scripts/report.py", content="y = 2\n")

    ipath = _make_instance(env, "i-1", "p-aaaa1111", realized=[
        _rf(str(salt.resolve()), "f-salt0001"),            # invalid
        _rf("capture-log.jsonl", "f-log00001"),            # invalid (rel form)
        _rf(str(telemetry.resolve()), "f-tele0001"),       # invalid
        _rf(str(owned.resolve()), "f-keep0001"),           # owned_ok — retain
        _rf("scripts/report.py", "f-rept0001"),            # missing_marker — retain
    ])

    return {
        "instance_path": ipath,
        "salt": salt, "log": log, "telemetry": telemetry,
        "owned": owned, "legit_unmarked": legit_unmarked,
        "invalid_keys": {"member-hash-salt.bin", "capture-log.jsonl",
                         "evolve/audit_outbox/rec-9.json"},
        "retain_keys": {"scripts/keep.py", "scripts/report.py"},
    }


# ── The ledger drives the removal set (no re-derived exclusion list) ───────────

def test_plan_selects_exactly_invalid_claims(env, populated):
    plan, recon = ci.build_removal_plan(env["shared_dir"], [env["bot_id"]])
    # The plan removes precisely the ledger's invalid_claim keys — nothing more.
    assert {r.canonical_key for r in plan.removals} == populated["invalid_keys"]
    # And those are exactly the recon ledger's invalid_claim rows.
    from evolve_admin.applications.recon_ledger import Bucket
    assert {row.path for row in recon.rows(Bucket.INVALID_CLAIM)} == populated["invalid_keys"]
    # Every removal is reason=non_ownable_claim and targets our Instance.
    assert all(r.reason == "non_ownable_claim" for r in plan.removals)
    assert all(r.instance_id == "i-1" for r in plan.removals)


def test_plan_reuses_ledger_not_a_local_list(env, populated, monkeypatch):
    """The over-removal guard is membership in the LEDGER's invalid_claim bucket
    — not a re-derived never-ownable list. If we hand build_removal_plan a recon
    result whose invalid_claim bucket is empty, it removes nothing, even though
    the manifest still claims never-ownable paths."""
    from evolve_admin.applications.recon_ledger import ReconResult, BUCKETS
    empty = ReconResult(bots=[env["bot_id"]])
    for b in BUCKETS:
        empty.buckets.setdefault(b, [])
    plan, _ = ci.build_removal_plan(env["shared_dir"], [env["bot_id"]], recon=empty)
    assert plan.total == 0


# ── Dry-run changes nothing ───────────────────────────────────────────────────

def test_dry_run_changes_nothing(env, populated, capsys):
    rc = ci.main(["--shared-dir", str(env["shared_dir"]), "--bot", env["bot_id"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "TOTAL would remove: 3" in out

    # Manifest unchanged: all 5 claims still present.
    inst = json.loads(populated["instance_path"].read_text())
    assert len(inst["realized_files"]) == 5
    assert inst["change_log"] == []


# ── Apply removes exactly the invalid claims ──────────────────────────────────

def test_apply_removes_invalid_retains_legit(env, populated, capsys):
    rc = ci.main(["--shared-dir", str(env["shared_dir"]),
                  "--bot", env["bot_id"], "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed 3" in out
    assert "failed 0" in out

    inst = json.loads(populated["instance_path"].read_text())
    remaining = {Path(rf["path"]).name for rf in inst["realized_files"]}
    # The two legit claims survive; the three invalid claims are gone.
    assert remaining == {"keep.py", "report.py"}
    assert len(inst["realized_files"]) == 2

    # A documenting change_log entry was appended (additive, audit trail).
    assert len(inst["change_log"]) == 1
    entry = inst["change_log"][0]
    assert entry["kind"] == "invalid_claims_removed"
    assert entry["who"] == "evolve"
    assert len(entry["file_changes"]) == 3


def test_apply_never_touches_files_on_disk(env, populated):
    """Reversibility: removing a claim edits only the manifest — the file on
    disk (including a secret) is untouched."""
    ci.main(["--shared-dir", str(env["shared_dir"]),
             "--bot", env["bot_id"], "--apply"])
    assert populated["salt"].exists()
    assert populated["salt"].read_text() == "RAWSALTBYTES"
    assert populated["log"].read_text() == '{"e":1}\n{"e":2}\n'
    assert populated["telemetry"].exists()
    # Retained-claim files also untouched.
    assert populated["owned"].read_text().endswith("x = 1\n")
    assert populated["legit_unmarked"].read_text() == "y = 2\n"


def test_apply_is_idempotent(env, populated):
    plan1, _ = ci.build_removal_plan(env["shared_dir"], [env["bot_id"]])
    r1 = ci.apply_removal_plan(plan1)
    assert r1.removed == 3 and r1.failed == 0

    # Re-build over the now-clean manifest: nothing left to remove.
    plan2, _ = ci.build_removal_plan(env["shared_dir"], [env["bot_id"]])
    assert plan2.total == 0
    r2 = ci.apply_removal_plan(plan2)
    assert r2.removed == 0 and r2.instances_touched == 0


def test_missing_marker_claim_is_never_removed(env, populated):
    """Explicit over-removal assertion: the real, legitimately-claimed file with
    no marker (missing_marker bucket) stays in realized_files[] after apply."""
    ci.apply_removal_plan(ci.build_removal_plan(env["shared_dir"], [env["bot_id"]])[0])
    inst = json.loads(populated["instance_path"].read_text())
    paths = {Path(rf["path"]).name for rf in inst["realized_files"]}
    assert "report.py" in paths      # missing_marker — must survive
    assert "keep.py" in paths        # owned_ok — must survive
