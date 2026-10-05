"""AL-3.1 — a defined app becomes a files-pack.

Brief: ``internal/dispatch/done/al-3-1-app-snapshot.md``. Engine:
``evolve_admin.applications.app_snapshot``.

WHAT EACH CLAIM IS FOR, in the order the brief asks for them:

  1. ``test_snapshot_round_trips_the_integrity_check`` — the whole point. A
     pack this module writes must satisfy ``verify_files_pack_integrity``,
     because a pack that fails it is one the install path refuses.
  2. ``test_the_spec_carries_the_packs_source_digests`` — and the §9.2
     resolution, made falsifiable: for a placeholder-bearing file the Spec's
     digest must equal the PACK's bytes and must NOT equal the workspace's.
     A test that only asserted equality with the pack would still pass if
     the two happened to be the same file, which is the vacuous shape this
     arc has been caught by twice.
  3. ``test_an_unhashable_file_is_reported_never_silently_dropped`` — an
     empty digest is indistinguishable from "nobody has looked yet".
  4. ``test_re_snapshot_is_a_true_no_op_when_nothing_changed`` — down to the
     pack's top-level sha, which means ``snapshot_at`` must not be restamped.
  5. ``test_a_draft_is_refused`` / ``test_a_discovered_app_is_refused`` — and
     that a refusal reports as a refusal, not as a failure.
  6. The shared-surface claims — detection is real, its attribution is
     honest about which evidence it rests on, and nothing it finds leaks
     into the pack or the Spec (that field is the operator's decision).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from evolve_admin.applications import app_snapshot
from evolve_admin.applications.app_snapshot import pack_dir, snapshot_app
from evolve_admin.applications.app_spec_store import load_spec
from evolve_admin.applications.files_pack import (
    load_files_pack_metadata,
    verify_files_pack_integrity,
)

BOT = "atlas"
BOT_USER = "atlas-user"
APP = "task-manager"
PKG = "p-9bfa1c84"


# ── Fixture pod ──────────────────────────────────────────────────────────────


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A bot home + workspace + shared dir, with the config seams pinned.

    ``get_bot_user`` / ``bot_home`` are patched rather than mocked away
    wholesale: the engine resolves the bot's REAL home and hands it to the
    reverse-substitution pass, so a fixture that did not supply one would
    silently test the ``/Users/`` fallback instead of the code path a Linux
    pod takes.
    """
    home = tmp_path / "home" / BOT_USER
    workspace = home / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setattr("evolve_admin.config.get_bot_user",
                        lambda bot_id, network=None: BOT_USER)
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot_id, network=None: home)

    class Pod:
        pass

    p = Pod()
    p.home, p.workspace, p.shared = home, workspace, shared
    p.network = {"sharedDir": str(shared), "bots": {BOT: {"user": BOT_USER}}}
    return p


def _write(pod, rel: str, text: str) -> Path:
    target = pod.workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _manifest(pod, **overrides) -> dict:
    """A defined app declaring two files, one of which mentions the bot."""
    data = {
        "app_id": APP,
        "pkg_id": PKG,
        "name": "Task Manager",
        "description": "Tracks tasks.",
        "definition_status": "defined",
        "files": [
            {"path": "scripts/tasks.py", "role": "vital_to_blueprint"},
            {"path": "docs/tasks.md", "marker_state": "reference_only"},
        ],
    }
    data.update(overrides)
    (pod.workspace / "manifests" / f"{APP}.json").write_text(
        json.dumps(data), encoding="utf-8")
    return data


def _seed_files(pod) -> None:
    # Mentions the bot's real home + id, so reverse-substitution has
    # something to find and the pack's bytes end up DIFFERENT from disk.
    _write(pod, "scripts/tasks.py",
           f"WORKSPACE = '{pod.workspace}'\nOWNER = '{BOT}'\n")
    _write(pod, "docs/tasks.md", "# Tasks\n\nNo bot tokens here.\n")


def _snapshot(pod, **kwargs):
    return snapshot_app(BOT, APP, network=pod.network, **kwargs)


# ── 1. The pack verifies ─────────────────────────────────────────────────────


def test_snapshot_round_trips_the_integrity_check(pod):
    _seed_files(pod)
    _manifest(pod)

    result = _snapshot(pod)

    assert result["ok"], result
    assert result["verified"] is True
    assert result["files_count"] == 2

    dest = pack_dir(pod.shared, APP)
    meta = load_files_pack_metadata(dest)
    assert meta is not None
    assert verify_files_pack_integrity(dest, meta) == []
    assert {f.path for f in meta.files} == {"scripts/tasks.py", "docs/tasks.md"}


def test_the_pack_location_is_derivable_from_the_app_id(pod):
    """The §9.2 resolution's mechanism, asserted rather than described.

    No pointer field is added to the Spec because the pack sits at a place
    the app_id already determines. If that stopped being true, "does this
    Spec's sha mean source or realized?" would become unanswerable again.
    """
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    assert pack_dir(pod.shared, APP) == pod.shared / "apps" / "packs" / APP
    assert (pack_dir(pod.shared, APP) / "manifest.json").is_file()
    # And the Spec carries no pointer to it — nothing to drift.
    spec = load_spec(pod.shared, APP)
    assert spec is not None
    assert "ref" not in spec.package
    assert "pack" not in spec.package


# ── 2. Source digests, and the proof they are not realized digests ───────────


def test_the_spec_carries_the_packs_source_digests(pod):
    _seed_files(pod)
    _manifest(pod)

    result = _snapshot(pod)
    assert result["ok"], result

    spec = load_spec(pod.shared, APP)
    assert spec is not None
    by_path = {f["path"]: f["sha256"] for f in spec.package["files"]}

    dest = pack_dir(pod.shared, APP)
    meta = load_files_pack_metadata(dest)
    assert by_path == {f.path: f.sha256 for f in meta.files}

    # The file that carried bot-specific tokens: its SOURCE digest is over
    # the pack's bytes, which are NOT the workspace's bytes. Both halves are
    # asserted — equality alone would pass even if nothing was substituted.
    substituted = (dest / "scripts/tasks.py").read_bytes()
    on_disk = (pod.workspace / "scripts/tasks.py").read_bytes()
    assert substituted != on_disk, "reverse-substitution did not fire"
    assert by_path["scripts/tasks.py"] == hashlib.sha256(substituted).hexdigest()
    assert by_path["scripts/tasks.py"] != hashlib.sha256(on_disk).hexdigest()

    # The placeholder-free file has no such gap, and the pack says so.
    entry = next(f for f in meta.files if f.path == "docs/tasks.md")
    assert entry.placeholders == []
    assert by_path["docs/tasks.md"] == hashlib.sha256(
        (pod.workspace / "docs/tasks.md").read_bytes()).hexdigest()


def test_the_role_survives_the_snapshot(pod):
    """``_package_role`` reads role / purpose / marker_state in that order.

    AL-1.5c §9.3a nearly shipped a regression here because a check written
    against ``role`` alone returned a clean zero while 333 live entries
    carried ``marker_state``. Both carriers are covered.
    """
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    spec = load_spec(pod.shared, APP)
    roles = {f["path"]: f["role"] for f in spec.package["files"]}
    assert roles["scripts/tasks.py"] == "vital_to_blueprint"
    assert roles["docs/tasks.md"] == "reference_only"


# ── 3. Report, never silently drop ───────────────────────────────────────────


def test_an_unhashable_file_is_reported_never_silently_dropped(pod):
    _seed_files(pod)
    (pod.workspace / "data").mkdir()
    _manifest(pod, files=[
        {"path": "scripts/tasks.py"},
        {"path": "scripts/gone.py"},        # declared, never written
        {"path": "data"},                   # a directory, not a file
        {"path": "../escape.py"},           # outside the workspace
    ])

    result = _snapshot(pod)
    assert result["ok"], result

    kinds = {n["path"]: n["kind"] for n in result["notes"]}
    assert kinds["scripts/gone.py"] == "missing"
    assert kinds["data"] == "directory"
    # A declaration that escapes the workspace is a fact about the manifest,
    # NOT a permission failure — it gets its own kind so it cannot trip the
    # read-denied refusal below.
    assert kinds["../escape.py"] == "outside_workspace"

    # Reported AND left out — never written as an empty digest, which is
    # indistinguishable from "nobody has looked yet".
    packed = {f["path"] for f in result["per_file"]}
    assert packed == {"scripts/tasks.py"}
    spec = load_spec(pod.shared, APP)
    assert [f["path"] for f in spec.package["files"]] == ["scripts/tasks.py"]
    assert all(f["sha256"] for f in spec.package["files"])


def test_an_app_whose_files_all_vanished_is_a_failure_with_its_notes_intact(pod):
    _manifest(pod, files=[{"path": "scripts/gone.py"}])

    result = _snapshot(pod)
    assert result["ok"] is False
    assert result["refused"] is False       # a real failure, not a decline
    assert result["error"].startswith("no_packable_files")
    assert [n["kind"] for n in result["notes"]] == ["missing"]


def test_an_instruction_only_app_is_a_refusal_not_a_failure(pod):
    """No files at all is a HEALTHY state, and 9 of 11 non-clean rows in the
    live measure are exactly this shape. Reporting it as a failure is how a
    healthy path starts reading like an outage — and this is precisely the
    population the operator addition is about, so the shared-surface report
    has to survive the refusal."""
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod, files=[])

    result = _snapshot(pod)
    assert result["ok"] is False
    assert result["refused"] is True
    assert result["error"].startswith("nothing_to_pack")
    assert result["shared_surface"]["deltas"], "the report must survive"
    assert not pack_dir(pod.shared, APP).exists()


def test_a_read_denied_file_refuses_the_whole_snapshot(pod):
    """Read-denied is not write-allowed.

    An ACL that regressed looks exactly like a file that was deleted. Writing
    a Spec whose package.files[] silently lost the entry would turn "we could
    not look" into "the app does not have it", so the snapshot declines
    wholesale rather than shipping a pack that under-claims.
    """
    _seed_files(pod)
    _manifest(pod)

    real_read = Path.read_bytes

    def denied(self, *a, **kw):
        if self.name == "tasks.py":
            raise PermissionError(13, "Permission denied")
        return real_read(self, *a, **kw)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(Path, "read_bytes", denied)
    try:
        result = _snapshot(pod)
    finally:
        monkey.undo()

    assert result["ok"] is False
    assert result["refused"] is False       # an ACL gap is a real problem
    assert result["error"].startswith("read_denied")
    assert "not a file that is gone" in result["error"]
    # Nothing written: no half-pack, no Spec claiming one fewer file.
    assert not pack_dir(pod.shared, APP).exists()
    assert load_spec(pod.shared, APP) is None


# ── 4. Idempotence ───────────────────────────────────────────────────────────


def test_re_snapshot_is_a_true_no_op_when_nothing_changed(pod):
    _seed_files(pod)
    _manifest(pod)

    first = _snapshot(pod)
    assert first["changed"] is True
    manifest_path = pack_dir(pod.shared, APP) / "manifest.json"
    before = manifest_path.read_bytes()

    second = _snapshot(pod)
    assert second["ok"], second
    assert second["changed"] is False
    assert second["verified"] is True
    # ``snapshot_at`` is preserved, so the pack's top-level digest — which
    # is the digest OF manifest.json — does not move either. Restamping it
    # would make "nothing changed" indistinguishable from "everything did".
    assert second["snapshot_at"] == first["snapshot_at"]
    assert manifest_path.read_bytes() == before


def test_a_changed_file_makes_the_next_snapshot_write_again(pod):
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    _write(pod, "docs/tasks.md", "# Tasks\n\nNow with more tasks.\n")
    second = _snapshot(pod)

    assert second["changed"] is True
    dest = pack_dir(pod.shared, APP)
    assert verify_files_pack_integrity(dest, load_files_pack_metadata(dest)) == []


def test_a_file_the_app_no_longer_declares_is_removed_and_reported(pod):
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)
    assert (pack_dir(pod.shared, APP) / "docs/tasks.md").is_file()

    _manifest(pod, files=[{"path": "scripts/tasks.py"}])
    result = _snapshot(pod)

    assert result["removed"] == ["docs/tasks.md"]
    assert not (pack_dir(pod.shared, APP) / "docs/tasks.md").exists()
    dest = pack_dir(pod.shared, APP)
    assert verify_files_pack_integrity(dest, load_files_pack_metadata(dest)) == []


# ── 5. Refusals, which are not failures ──────────────────────────────────────


def test_a_draft_is_refused(pod):
    """Three shapes of "no conferred identity", all refusals."""
    _seed_files(pod)
    _manifest(pod)

    empty = snapshot_app(BOT, "", network=pod.network)
    assert empty["ok"] is False and empty["refused"] is True
    assert empty["error"].startswith("draft_refused")

    bad = snapshot_app(BOT, "Not A Slug", network=pod.network)
    assert bad["ok"] is False and bad["refused"] is True
    assert bad["error"].startswith("invalid_app_id")

    # An artifact that resolves to the app_id but carries a draft_id: the
    # draft check runs even though the resolver was satisfied.
    _manifest(pod, draft_id="d-1234")
    drafted = _snapshot(pod)
    assert drafted["ok"] is False and drafted["refused"] is True
    assert drafted["error"].startswith("draft_refused")


def test_a_discovered_app_is_refused_rather_than_failed(pod):
    _seed_files(pod)
    _manifest(pod, definition_status="discovered")

    result = _snapshot(pod)
    assert result["ok"] is False
    assert result["refused"] is True
    assert result["error"].startswith("not_a_defined_app")
    assert not pack_dir(pod.shared, APP).exists()


def test_an_app_this_bot_does_not_have_is_refused(pod):
    _seed_files(pod)
    _manifest(pod)

    result = snapshot_app(BOT, "some-other-app", network=pod.network)
    assert result["ok"] is False and result["refused"] is True
    assert result["error"].startswith("app_not_found")


def test_an_unlistable_workspace_is_not_reported_as_a_missing_app(pod):
    """A flat negative must not be only as wide as its search.

    Measured live on the Linux pod: ``/home/<bot>`` is traverse-only for
    ``evolve`` there, so a walk that cannot list still resolves individual
    paths. Reporting "this bot does not have that app" when nothing was
    actually ruled out is the failure mode; the refusal flag matters too —
    an ACL gap is a real problem, not a healthy decline.
    """
    _seed_files(pod)
    _manifest(pod)

    real_listdir = os.listdir

    def denied(path, *a, **kw):
        if str(path).endswith("manifests"):
            raise PermissionError(13, "Permission denied")
        return real_listdir(path, *a, **kw)

    import evolve_admin.applications.app_snapshot as mod
    orig = mod.os.listdir
    mod.os.listdir = denied
    try:
        result = _snapshot(pod)
    finally:
        mod.os.listdir = orig

    assert result["ok"] is False
    assert result["refused"] is False
    assert result["error"].startswith("workspace_unreadable")
    assert "NOT 'the app is not here'" in result["error"]


# ── 6. Dry run, and the modes ────────────────────────────────────────────────


def test_dry_run_writes_nothing_but_still_answers(pod):
    _seed_files(pod)
    _manifest(pod)

    result = _snapshot(pod, dry_run=True)

    assert result["ok"], result
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert result["files_count"] == 2
    # Nothing to verify yet, and it does not claim otherwise.
    assert result["verified"] is False
    assert result["spec_written"] is False
    assert not pack_dir(pod.shared, APP).exists()
    assert load_spec(pod.shared, APP) is None


def test_pack_modes_are_pinned_tighter_than_the_spec(pod):
    """0700 / 0600 — a pack is bot-private CONTENT, not declared intent.

    Landing it at the Spec's 0644 would re-widen, through a new pod-wide
    directory, exactly what the Linux bot-private clamp closes.
    """
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    dest = pack_dir(pod.shared, APP)
    assert stat.S_IMODE(dest.stat().st_mode) == app_snapshot.PACK_DIR_MODE
    assert stat.S_IMODE(dest.parent.stat().st_mode) == app_snapshot.PACK_DIR_MODE
    for rel in ("manifest.json", "scripts/tasks.py", "docs/tasks.md"):
        mode = stat.S_IMODE((dest / rel).stat().st_mode)
        assert mode == app_snapshot.PACK_FILE_MODE, f"{rel} is {oct(mode)}"


def test_no_auto_detect_packs_verbatim(pod):
    _seed_files(pod)
    _manifest(pod)

    result = _snapshot(pod, auto_detect=False)
    assert result["ok"], result
    assert all(not f["placeholders"] for f in result["per_file"])

    dest = pack_dir(pod.shared, APP)
    assert (dest / "scripts/tasks.py").read_bytes() == (
        pod.workspace / "scripts/tasks.py").read_bytes()
    assert verify_files_pack_integrity(dest, load_files_pack_metadata(dest)) == []


def test_a_bot_named_path_is_reported_because_only_content_is_substituted(pod):
    """The limit reverse-substitution does not reach.

    Content gets rewritten; a FILE NAME carrying the source bot's id does
    not, and would install onto the next bot under the first bot's name.
    ``placeholders_in_path`` is the format's slot for it and nothing on this
    pod emits one, so the pack reports the token rather than silently
    shipping it or rewriting a path nobody asked it to rewrite.
    """
    _write(pod, f"scripts/{BOT}-cron.sh", "#!/bin/sh\necho hi\n")
    _write(pod, "scripts/plain.sh", "#!/bin/sh\necho hi\n")
    _manifest(pod, files=[{"path": f"scripts/{BOT}-cron.sh"},
                          {"path": "scripts/plain.sh"}])

    per_file = {f["path"]: f for f in _snapshot(pod)["per_file"]}
    assert per_file[f"scripts/{BOT}-cron.sh"]["path_tokens"] == [BOT]
    assert per_file["scripts/plain.sh"]["path_tokens"] == []
    # And the path itself is untouched — reporting, not rewriting.
    assert (pack_dir(pod.shared, APP) / f"scripts/{BOT}-cron.sh").is_file()


# ── 7. Shared surfaces — detected, reported, and NOT carried ─────────────────


AGENTS_MD = """# Agent instructions

## Task Manager — Check
<!-- evolve-managed: pkg=p-9bfa1c84 -->

Every session, run `python3 scripts/tasks.py check`.

## Task Manager — Digest

No marker on this one; the manifest is the only thing that claims it.

## Operator notes

Hand-written. Nothing owns this.
"""


def _manifest_with_sections(pod, **overrides):
    return _manifest(pod, scheduled_actions=[
        {
            "id": "check",
            "mechanism": "oc_session_instruction",
            "install": {"file": "AGENTS.md",
                        "section_anchor": "## Task Manager — Check"},
            "installed_artifact": "AGENTS.md#Task Manager — Check",
        },
        {
            "id": "digest",
            "mechanism": "oc_session_instruction",
            "installed_artifact": "AGENTS.md#Task Manager — Digest",
        },
        {
            "id": "legacy-hook",
            "mechanism": "oc_heartbeat_hook",
            "installed_artifact": "openclaw.json#hooks.heartbeat[2]",
        },
    ], **overrides)


def test_shared_surface_deltas_are_detected_with_honest_attribution(pod):
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod)

    surface = _snapshot(pod)["shared_surface"]
    by_section = {d["section"]: d for d in surface["deltas"]}

    marked = by_section["## Task Manager — Check"]
    assert marked["attribution"] == "marker"
    assert marked["found"] is True
    assert marked["sha256"]

    # Declared by the manifest, unmarked in the file: a DIFFERENT value, and
    # the detail says whose word the attribution rests on. Collapsing the two
    # would report a manifest's claim as if the file had confirmed it.
    unmarked = by_section["Task Manager — Digest"]
    assert unmarked["attribution"] == "declared"
    assert unmarked["found"] is True
    assert "rests on the manifest's claim" in unmarked["detail"]

    # A config key is reported, and reported WITHOUT a digest rather than
    # with a guessed one.
    config = by_section["hooks.heartbeat[2]"]
    assert config["attribution"] == "config"
    assert config["file"] == "openclaw.json"
    assert config["sha256"] == ""

    # ``not_found`` cuts across the attribution buckets rather than being a
    # fourth one, so ``located`` is reported instead of left to be derived.
    assert surface["counts"] == {
        "marker": 1, "declared": 1, "config": 1,
        "not_found": 0, "located": 3, "total": 3}


def test_a_declared_section_that_is_not_in_the_file_reads_as_not_found(pod):
    _seed_files(pod)
    _write(pod, "AGENTS.md", "# Agent instructions\n\n## Something else\n\nx\n")
    _manifest_with_sections(pod)

    surface = _snapshot(pod)["shared_surface"]
    missing = [d for d in surface["deltas"] if not d["found"]]
    assert {d["section"] for d in missing} == {
        "## Task Manager — Check", "Task Manager — Digest"}
    assert all(d["sha256"] == "" for d in missing)
    # Both not-found rows are ALSO counted as ``declared`` — the overlap the
    # ``located`` count exists to make unambiguous.
    assert surface["counts"]["not_found"] == 2
    assert surface["counts"]["declared"] == 2
    assert surface["counts"]["located"] == 1   # only the config delta
    assert surface["counts"]["total"] == 3


def test_a_marked_section_the_manifest_forgot_is_still_found(pod):
    """A manifest can go stale; the marker in the file cannot."""
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest(pod)                      # declares NO sections at all

    surface = _snapshot(pod)["shared_surface"]
    sections = {d["section"]: d for d in surface["deltas"]}
    assert "## Task Manager — Check" in sections
    assert sections["## Task Manager — Check"]["attribution"] == "marker"
    assert "not declared by the manifest" in (
        sections["## Task Manager — Check"]["detail"])


def test_the_blind_spot_is_sized_rather_than_glossed(pod):
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod)

    unattr = _snapshot(pod)["shared_surface"]["unattributed_sections"]
    listed = {(s["file"], s["section"]) for s in unattr["listed"]}

    # The top-level heading and the operator's own section: no marker, no
    # manifest row. Both of the app's sections are excluded because they ARE
    # claimed — the count measures what attribution cannot see, not noise.
    assert ("AGENTS.md", "# Agent instructions") in listed
    assert ("AGENTS.md", "## Operator notes") in listed
    assert ("AGENTS.md", "## Task Manager — Check") not in listed
    assert ("AGENTS.md", "## Task Manager — Digest") not in listed
    assert unattr["count"] == len(unattr["listed"])
    assert "not this app's footprint" in unattr["note"].lower()


def test_shared_surface_is_reported_but_never_carried(pod):
    """The §5-freeze half of the operator addition, pinned by a test.

    Detection ships; carrying the deltas in the pack or the Spec is an
    operator decision. If a later chip starts writing a ``footprint`` field
    without that decision, this fails — which is the point.
    """
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod)

    result = _snapshot(pod)
    assert result["shared_surface"]["deltas"], "nothing detected to be carried"
    assert result["shared_surface"]["carried_in_pack"] is False

    pack_manifest = json.loads(
        (pack_dir(pod.shared, APP) / "manifest.json").read_text())
    # Every key must be one the files-pack FORMAT already defines — the
    # property is "nothing outside the format leaks in", not a frozen literal
    # set, which would fail the moment a legitimate format key (``partial``)
    # started being written.
    assert set(pack_manifest) <= {
        "format_version", "snapshot_source", "files", "partial",
        "coverage_intent", "signature",
    }
    assert not any(
        k in json.dumps(pack_manifest) for k in ("footprint", "shared_edits"))

    spec_raw = json.loads(
        (pod.shared / "apps" / "specs" / f"{APP}.json").read_text())
    assert "footprint" not in spec_raw
    assert "shared_edits" not in spec_raw
    assert set(spec_raw["package"]) == {"files"}


def test_an_app_with_no_shared_surface_reports_an_empty_footprint(pod):
    _seed_files(pod)
    _manifest(pod)

    surface = _snapshot(pod)["shared_surface"]
    assert surface["deltas"] == []
    assert surface["counts"]["marker"] == 0
    assert surface["limits"], "the limits must be stated even when empty"


# ── 8. The route ─────────────────────────────────────────────────────────────


def test_the_route_defaults_to_dry_run(pod, monkeypatch):
    """A mutation must not be one omitted field away."""
    from flask import Flask

    from evolve_admin.web.routes_app_snapshot import register_app_snapshot_routes

    _seed_files(pod)
    _manifest(pod)
    monkeypatch.setattr("evolve_admin.config.load_network",
                        lambda path=None: pod.network)

    app = Flask(__name__)
    register_app_snapshot_routes(app, Path("network.json"))
    client = app.test_client()

    resp = client.post(f"/api/apps/{APP}/snapshot", json={"bot_id": BOT})
    assert resp.status_code == 200
    assert resp.get_json()["dry_run"] is True
    assert not pack_dir(pod.shared, APP).exists()

    resp = client.post(f"/api/apps/{APP}/snapshot",
                       json={"bot_id": BOT, "dry_run": False})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert (pack_dir(pod.shared, APP) / "manifest.json").is_file()


def test_the_route_reports_a_refusal_as_a_refusal(pod, monkeypatch):
    from flask import Flask

    from evolve_admin.web.routes_app_snapshot import register_app_snapshot_routes

    _seed_files(pod)
    _manifest(pod, definition_status="discovered")
    monkeypatch.setattr("evolve_admin.config.load_network",
                        lambda path=None: pod.network)

    app = Flask(__name__)
    register_app_snapshot_routes(app, Path("network.json"))
    client = app.test_client()

    resp = client.post(f"/api/apps/{APP}/snapshot",
                       json={"bot_id": BOT, "dry_run": False})
    # 422, not 500: a well-formed request about a thing that cannot be
    # snapshotted is not a server error.
    assert resp.status_code == 422
    assert resp.get_json()["refused"] is True

    resp = client.post(f"/api/apps/{APP}/snapshot", json={})
    assert resp.status_code == 400
    assert resp.get_json()["missing"] == ["bot_id"]


def _cli(pod, monkeypatch, *args):
    """Drive ``evolve-admin application snapshot`` through the real click group."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    network_path = pod.shared.parent / "network.json"
    network_path.write_text(json.dumps(pod.network), encoding="utf-8")
    monkeypatch.setattr("evolve_admin.config.load_network",
                        lambda path=None: pod.network)
    return CliRunner().invoke(
        main, ["--network", str(network_path), "application", "snapshot",
               "--bot", BOT, "--app", APP, *args],
    )


def test_the_cli_command_is_registered_and_defaults_to_dry_run(pod, monkeypatch):
    """The command is reached by NAME on the command line and by nothing in
    the repo, so this is the only thing that proves the registration in
    cli.py actually attached it — and that its default writes nothing."""
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod)

    result = _cli(pod, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Would snapshot" in result.output
    assert "Shared-surface deltas" in result.output
    assert not pack_dir(pod.shared, APP).exists()

    result = _cli(pod, monkeypatch, "--apply")
    assert result.exit_code == 0, result.output
    assert "Snapshotted" in result.output
    assert (pack_dir(pod.shared, APP) / "manifest.json").is_file()
    assert load_spec(pod.shared, APP) is not None


def test_the_cli_exits_1_on_a_refusal_and_still_prints_the_surface(pod, monkeypatch):
    """A refusal is exit 1, not 2 — and for an app whose behaviour is ONLY in
    the shared surface, the report is the whole answer, so it must survive."""
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest_with_sections(pod, files=[])

    result = _cli(pod, monkeypatch, "--apply")
    assert result.exit_code == 1, result.output
    assert "nothing_to_pack" in result.output
    assert "Shared-surface deltas" in result.output


# ── 9. Reverse-substitution is home-aware, not /Users-shaped ─────────────────


def test_reverse_substitution_uses_the_bots_real_home(pod):
    """The Linux half of the placeholder detector.

    ``snapshot_engine.detect_and_substitute`` defaulted to a ``/Users/``
    literal, which finds nothing on a pod whose homes are ``/home/<user>``.
    The fixture's home is neither, so a pass here means the detector is
    reading the home it was given rather than a hardcoded prefix.
    """
    _seed_files(pod)
    _manifest(pod)

    _snapshot(pod)
    packed = (pack_dir(pod.shared, APP) / "scripts/tasks.py").read_text()

    assert "{workspace}" in packed
    assert str(pod.workspace) not in packed
    entry = next(f for f in load_files_pack_metadata(pack_dir(pod.shared, APP)).files
                 if f.path == "scripts/tasks.py")
    assert "workspace" in entry.placeholders
    assert "bot_id" in entry.placeholders


def test_the_pkg_id_keyed_engine_keeps_its_original_default(pod):
    """The sibling entrypoint is unchanged: ``home_dir`` omitted still means
    ``/Users/<bot_user>``, so ``snapshot_installed_app`` behaves exactly as
    it did before this chip touched the shared detector."""
    from evolve_admin.applications.snapshot_engine import detect_and_substitute

    text = f"p = '/Users/{BOT_USER}/.openclaw/workspace/x'\n"
    changed, placeholders, _ = detect_and_substitute(text, BOT_USER, BOT)
    assert "{workspace}" in changed
    assert placeholders == ["workspace"]


# ── 10. Findings from the independent review (PR #3833), each pinned ─────────
#
# Every test below corresponds to a numbered finding in that review. They are
# grouped here rather than scattered so a later reader can see what an
# adversarial pass actually caught, and so deleting one is a visible act.


def test_a_leaf_symlink_cannot_pull_a_file_into_the_pack(pod):
    """F1. The workspace and the manifest are BOTH bot-written, and this
    module reads as ``evolve``, which holds ACL read on every bot's
    ``.openclaw`` — including token files the bot itself cannot read. A
    symlink at the leaf passed containment before this fix, so a bot could
    declare ``notes.md -> /Users/<other>/.openclaw/openclaw.json`` and have
    the token copied into a pack built to be installed on other bots.
    """
    secret = pod.home.parent / "otherbot" / ".openclaw" / "openclaw.json"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"token": "sk-MUST-NOT-BE-PACKED"}', encoding="utf-8")
    _write(pod, "scripts/tasks.py", "print('hi')\n")
    (pod.workspace / "notes.md").symlink_to(secret)
    _manifest(pod, files=[{"path": "scripts/tasks.py"}, {"path": "notes.md"}])

    result = _snapshot(pod)

    assert result["ok"], result
    assert [f["path"] for f in result["per_file"]] == ["scripts/tasks.py"]
    kinds = {n["path"]: n["kind"] for n in result["notes"]}
    assert kinds["notes.md"] == "outside_workspace"

    dest = pack_dir(pod.shared, APP)
    packed = b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert b"sk-MUST-NOT-BE-PACKED" not in packed


def test_a_symlink_pointing_inside_the_workspace_is_also_refused(pod):
    """F1, the narrower half. Resolving proves where a link points NOW, and
    the workspace is bot-writable, so the target can be swapped between the
    check and the read. The link is refused even when it currently resolves
    inside — and a pack should carry real files anyway."""
    _write(pod, "scripts/tasks.py", "print('hi')\n")
    (pod.workspace / "alias.py").symlink_to(pod.workspace / "scripts/tasks.py")
    _manifest(pod, files=[{"path": "scripts/tasks.py"}, {"path": "alias.py"}])

    result = _snapshot(pod)
    assert [f["path"] for f in result["per_file"]] == ["scripts/tasks.py"]
    assert {n["kind"] for n in result["notes"]} == {"outside_workspace"}


def test_one_section_spelled_two_ways_is_one_delta(pod):
    """F4 — the third detector bug. The anchor axis was deduped and the FILE
    axis was not, so a section reported twice with the IDENTICAL digest, and
    the blind-spot count was multiplied by the number of spellings. The
    scanner validates ``heartbeat_evidence.file_path`` with ``.upper()``, so
    the lowercase spelling is well-formed by its own contract."""
    _seed_files(pod)
    _write(pod, "AGENTS.md", AGENTS_MD)
    _manifest(pod, scheduled_actions=[
        {"id": "a", "install": {"file": "AGENTS.md",
                                "section_anchor": "## Task Manager — Check"}},
        {"id": "b", "installed_artifact": "agents.md#Task Manager — Check"},
        {"id": "c", "installed_artifact": "./AGENTS.md#Task Manager — Check"},
    ])

    surface = _snapshot(pod)["shared_surface"]

    assert surface["counts"]["total"] == 1, surface["deltas"]
    assert surface["counts"]["marker"] == 1
    # And the blind spot is counted once per FILE, not once per spelling.
    listed = [s["section"] for s in surface["unattributed_sections"]["listed"]]
    assert len(listed) == len(set(listed))


NESTED_AGENTS_MD = (
    "# Agent instructions\n\n"
    "## Task Manager\n<!-- evolve-managed: pkg=p-9bfa1c84 -->\n\n"
    "body\n\n### Sub A\n\nx\n\n### Sub B\n\ny\n\n"
    "## Operator notes\n\nhand written\n"
)


@pytest.mark.parametrize("declared", [False, True], ids=["marker-sweep", "declared"])
def test_subsections_of_an_owned_block_are_not_counted_as_unowned(pod, declared):
    """F5. Fixing marker attribution so it does not CLIMB left it unable to
    DESCEND: every ``###`` inside a marked ``##`` scored as content no app
    can be shown to own, inflating the number the §5 proposal rests on.

    Parametrized because the span is claimed at TWO call sites — the
    declared-section pass and the marker sweep — and a fix applied to one
    leaves the other counting. The first version of this test exercised only
    the sweep, and a mutation of the declared path survived it.
    """
    _seed_files(pod)
    _write(pod, "AGENTS.md", NESTED_AGENTS_MD)
    if declared:
        _manifest(pod, scheduled_actions=[
            {"id": "tm", "install": {"file": "AGENTS.md",
                                     "section_anchor": "## Task Manager"}}])
    else:
        _manifest(pod)

    surface = _snapshot(pod)["shared_surface"]
    listed = {s["section"] for s in surface["unattributed_sections"]["listed"]}

    assert surface["counts"]["marker"] == 1
    assert "### Sub A" not in listed and "### Sub B" not in listed
    assert listed == {"# Agent instructions", "## Operator notes"}


def test_a_heading_inside_a_code_fence_is_not_a_section(pod):
    """F6. A ``#`` at column 0 in a shell block minted a phantom section AND
    truncated the enclosing section's digest at the fence — and that digest
    is what the proposed footprint field would key a revert on."""
    _seed_files(pod)
    body = (
        "# Agent instructions\n\n"
        "## Task Manager\n<!-- evolve-managed: pkg=p-9bfa1c84 -->\n\n"
        "```bash\n# Task Manager runner\necho hi\n```\n\n"
        "TAIL-OF-SECTION\n"
    )
    _write(pod, "AGENTS.md", body)
    _manifest(pod)

    surface = _snapshot(pod)["shared_surface"]
    listed = {s["section"] for s in surface["unattributed_sections"]["listed"]}
    assert "# Task Manager runner" not in listed

    # The digest must cover the whole section, tail included.
    delta = next(d for d in surface["deltas"] if d["attribution"] == "marker")
    start = body.index("## Task Manager")
    assert delta["sha256"] == hashlib.sha256(
        body[start:].rstrip().encode("utf-8")).hexdigest()


def test_a_duplicate_heading_prefers_the_marked_occurrence(pod):
    """F7 — one input, three wrong answers before the fix: the unmarked first
    ``## Notes`` won, so attribution read ``declared`` for a section that
    carries a marker, the digest was over the wrong body, and the genuinely
    unowned first section was hidden from the blind-spot count."""
    _seed_files(pod)
    _write(pod, "AGENTS.md",
           "# Agent instructions\n\n"
           "## Notes\n\nunowned prose\n\n"
           "## Notes\n<!-- evolve-managed: pkg=p-9bfa1c84 -->\n\nowned\n")
    _manifest(pod, scheduled_actions=[
        {"id": "n", "installed_artifact": "AGENTS.md#Notes"}])

    surface = _snapshot(pod)["shared_surface"]
    delta = next(d for d in surface["deltas"] if d["section"] == "Notes")
    assert delta["attribution"] == "marker"

    listed = {s["section"] for s in surface["unattributed_sections"]["listed"]}
    assert "## Notes" in listed, "the unowned occurrence must stay visible"


def test_a_widened_pack_file_is_repaired_on_the_unchanged_path(pod):
    """F9. The 0700/0600 argument is the module's whole security posture and
    it was one-shot: applied only when the content changed. Nothing else
    repairs it either — ``apps/packs`` is unknown to deploy.py,
    secret_config_perms and the drift monitor — so a pack widened by the
    recurring ``chmod -R a+rX {shared_dir}`` re-exposer stayed widened, while
    a re-snapshot reported changed=False / verified=True."""
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    dest = pack_dir(pod.shared, APP)
    for rel in ("manifest.json", "scripts/tasks.py"):
        os.chmod(dest / rel, 0o644)
    os.chmod(dest, 0o755)

    result = _snapshot(pod)

    assert result["changed"] is False
    assert set(result["repaired_modes"]) == {"manifest.json", "scripts/tasks.py"}
    for rel in ("manifest.json", "scripts/tasks.py", "docs/tasks.md"):
        assert stat.S_IMODE((dest / rel).stat().st_mode) == app_snapshot.PACK_FILE_MODE
    assert stat.S_IMODE(dest.stat().st_mode) == app_snapshot.PACK_DIR_MODE


def test_a_crashed_writes_tmp_orphan_is_swept_on_the_unchanged_path(pod):
    """F10. ``verify_files_pack_integrity`` walks manifest rows only, so it
    has no concept of an on-disk file the manifest does not name — a
    ``.snapshot-tmp`` left by a dead write reported intact forever."""
    _seed_files(pod)
    _manifest(pod)
    _snapshot(pod)

    dest = pack_dir(pod.shared, APP)
    orphan = dest / "scripts" / "orphan.snapshot-tmp"
    orphan.write_text("junk")

    result = _snapshot(pod)

    assert result["changed"] is False
    assert result["removed"] == ["scripts/orphan.snapshot-tmp"]
    assert not orphan.exists()


def test_a_partial_pack_says_so_in_its_own_metadata(pod):
    """F11. ``notes`` lives as long as the CLI process; the pack outlives it.
    Without ``partial`` the Spec says the app is one file smaller and
    ``validate()`` goes from flagging the gap to reporting clean — the
    snapshot improving its score by deleting the row that scored badly."""
    _seed_files(pod)
    _manifest(pod, files=[{"path": "scripts/tasks.py"},
                          {"path": "scripts/gone.py"}])

    result = _snapshot(pod)
    assert result["partial"] is True

    meta = load_files_pack_metadata(pack_dir(pod.shared, APP))
    assert meta.partial is True
    assert meta.coverage_intent == "workspace_snapshot_with_gaps"


def test_a_complete_pack_is_not_marked_partial(pod):
    """The other half of F11 — ``partial`` has to mean something."""
    _seed_files(pod)
    _manifest(pod)

    result = _snapshot(pod)
    assert result["partial"] is False
    assert load_files_pack_metadata(pack_dir(pod.shared, APP)).partial is False


def test_a_pathological_home_does_not_mangle_the_pack(pod, monkeypatch):
    """F13. ``Path("/")`` is truthy, so the old guard let ``home`` become ""
    and pattern 2's regex became ``re.escape("/")`` — every separator in
    every packed file rewritten, the digest taken over the mangled bytes, and
    the pack verifying CLEAN while being silently garbage."""
    from evolve_admin.applications.snapshot_engine import detect_and_substitute

    text = "p = '/Users/someone/thing'\nq = '/a/b'\n"
    for home in ("/", "", None):
        out, _placeholders, _bare = detect_and_substitute(
            text, BOT_USER, BOT, home_dir=home)
        assert out.count("/") == text.count("/"), f"{home!r} mangled separators"

    # And end to end: a bot whose home resolves to "/" still packs real bytes.
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot_id, network=None: Path("/"))
    _seed_files(pod)
    _manifest(pod)
    result = snapshot_app(BOT, APP, network=pod.network,
                          workspace=pod.workspace)
    assert result["ok"], result
    packed = (pack_dir(pod.shared, APP) / "docs/tasks.md").read_text()
    assert packed == (pod.workspace / "docs/tasks.md").read_text()
