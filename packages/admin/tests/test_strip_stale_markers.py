"""
Tests for the stale-marker scrub sweep (strip_stale_markers.py).

A synthetic tmp workspace carries:
  - telemetry-marker files under ``evolve/audit_outbox/_ingested/`` (the
    rec-*.json flood) — scrub candidates, class ``telemetry``.
  - a reserved-file marker (``AGENTS.md``) — scrub candidate, class ``reserved``.
  - an owned file with a marker (claimed + marked by a live Instance) — NOT a
    scrub candidate; its marker must survive.

Asserts:
  - dry-run reports the right counts and changes nothing on disk;
  - ``--apply`` strips exactly the scrub candidates (telemetry + reserved) and
    leaves the owned file's marker intact;
  - a re-run is a no-op (idempotent);
  - the ``--reason`` filter targets a single class.

Placeholder bot names only (no real bot identities) per the public-launch
scrub guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.applications.provenance import parse_marker, embed_marker
from evolve_admin.applications import strip_stale_markers as sm


# ── Fixtures / builders ───────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A single bot workspace under tmp_path with bot_home patched so the
    recon ledger (which strip_stale_markers builds) walks our synthetic tree."""
    bot_id = "personal_bot"
    bot_dir = tmp_path / "bot-homes" / bot_id
    workspace = bot_dir / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    # build_recon_ledger resolves bot homes through recon_ledger.bot_home.
    from evolve_admin.applications import recon_ledger as rl
    monkeypatch.setattr(rl, "bot_home", lambda _bid, *a, **k: bot_dir)

    return {
        "bot_id": bot_id,
        "shared_dir": tmp_path / "shared",
        "workspace": workspace,
        "manifests": workspace / "manifests",
        "scripts": workspace / "scripts",
    }


def _make_instance(env, instance_id: str, spec_id: str, realized: list[dict]) -> None:
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
        "status": "active",
    }
    (env["manifests"] / f"{instance_id}.json").write_text(json.dumps(inst))


def _rf(path: Path, file_id: str) -> dict:
    return {"logical_name": path.stem, "path": str(path.resolve()),
            "file_id": file_id, "marker_state": "OWNED"}


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


@pytest.fixture
def populated(env):
    """Build the canonical mixed tree and return path handles."""
    # ── Telemetry flood: rec-*.json under the platform evolve/ tree. ──────────
    telemetry: list[Path] = []
    for i in range(3):
        p = _write(
            env, f"evolve/audit_outbox/_ingested/rec-{i}.json",
            content=json.dumps({"finding": f"x{i}", "payload": [1, 2, 3]}),
        )
        _stamp(p, "p-cccc3333", f"f-rec0000{i}")
        telemetry.append(p)

    # ── Reserved file: AGENTS.md at the workspace root. ───────────────────────
    agents = _write(env, "AGENTS.md", content="# Standing instructions\n")
    _stamp(agents, "p-dddd4444", "f-agents01")

    # ── Owned file: claimed + marked by a live Instance (must NOT be touched). ─
    owned = _write(env, "scripts/keep.py", content="x = 1\n")
    _stamp(owned, "p-aaaa1111", "f-keep0001")
    _make_instance(env, "i-1", "p-aaaa1111", realized=[_rf(owned, "f-keep0001")])

    return {"telemetry": telemetry, "agents": agents, "owned": owned}


def _has_marker(p: Path) -> bool:
    m = parse_marker(p)
    return m is not None and m.is_valid()


# ── Collection / classification ───────────────────────────────────────────────

def test_collect_finds_only_scrub_candidates(env, populated):
    rows = sm.collect_scrub_rows(env["shared_dir"], [env["bot_id"]])
    paths = sorted(r.path for r in rows)
    # 3 telemetry rec files + AGENTS.md; the owned keep.py is excluded.
    assert any(p.endswith("rec-0.json") for p in paths)
    assert any(p.endswith("AGENTS.md") for p in paths)
    assert not any(p.endswith("keep.py") for p in paths)
    assert len(rows) == 4

    classes = {sm.classify_reason(r) for r in rows}
    assert classes == {sm.TELEMETRY, sm.RESERVED}


def test_reason_filter_targets_one_class(env, populated):
    tele = sm.collect_scrub_rows(
        env["shared_dir"], [env["bot_id"]], reason_filter=sm.TELEMETRY
    )
    assert len(tele) == 3
    assert all(sm.classify_reason(r) == sm.TELEMETRY for r in tele)

    reserved = sm.collect_scrub_rows(
        env["shared_dir"], [env["bot_id"]], reason_filter=sm.RESERVED
    )
    assert len(reserved) == 1
    assert reserved[0].path.endswith("AGENTS.md")


# ── Dry-run changes nothing ───────────────────────────────────────────────────

def test_dry_run_changes_nothing(env, populated, capsys):
    rc = sm.main(["--shared-dir", str(env["shared_dir"]),
                  "--bot", env["bot_id"]])
    assert rc == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "TOTAL would strip: 4" in out

    # Every marker is still on disk.
    for p in populated["telemetry"]:
        assert _has_marker(p)
    assert _has_marker(populated["agents"])
    assert _has_marker(populated["owned"])


# ── Apply strips exactly the scrub candidates ─────────────────────────────────

def test_apply_strips_scrub_candidates_only(env, populated, capsys):
    rc = sm.main(["--shared-dir", str(env["shared_dir"]),
                  "--bot", env["bot_id"], "--apply"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "stripped 4" in out
    assert "failed 0" in out

    # Scrub candidates: marker gone, payload preserved.
    for i, p in enumerate(populated["telemetry"]):
        assert not _has_marker(p)
        data = json.loads(p.read_text())
        assert "_evolve" not in data
        assert data["finding"] == f"x{i}"        # payload intact
        assert data["payload"] == [1, 2, 3]
    assert not _has_marker(populated["agents"])
    assert "Standing instructions" in populated["agents"].read_text()

    # Owned file: marker intact — never a scrub candidate.
    assert _has_marker(populated["owned"])
    owned_marker = parse_marker(populated["owned"])
    assert owned_marker.spec_ids == ["p-aaaa1111"]


def test_apply_is_idempotent(env, populated):
    first = sm.collect_scrub_rows(env["shared_dir"], [env["bot_id"]])
    r1 = sm.strip_rows(first)
    assert r1.stripped == 4
    assert r1.failed == 0

    # Re-classify at strip time: a second build finds nothing left to scrub.
    second = sm.collect_scrub_rows(env["shared_dir"], [env["bot_id"]])
    assert second == [] or all(not _has_marker(Path(r.abs_path)) for r in second)
    r2 = sm.strip_rows(second)
    assert r2.stripped == 0

    # Owned marker still present after both passes.
    assert _has_marker(populated["owned"])


def test_reason_filtered_apply_leaves_other_classes(env, populated):
    # Strip only telemetry; AGENTS.md (reserved) must remain.
    rows = sm.collect_scrub_rows(
        env["shared_dir"], [env["bot_id"]], reason_filter=sm.TELEMETRY
    )
    report = sm.strip_rows(rows)
    assert report.stripped == 3

    for p in populated["telemetry"]:
        assert not _has_marker(p)
    assert _has_marker(populated["agents"])      # reserved class untouched
    assert _has_marker(populated["owned"])


def test_owned_file_marker_survives_full_sweep(env, populated):
    """Explicit owned-file safety assertion (the chip's named test)."""
    sm.strip_rows(sm.collect_scrub_rows(env["shared_dir"], [env["bot_id"]]))
    assert _has_marker(populated["owned"])
    assert populated["owned"].read_text() == (
        "# evolve: spec=p-aaaa1111@2026.05.20-1.0 file=f-keep0001@2026.05.20-1.0\n"
        "x = 1\n"
    )
