"""AL-3.2 — the deterministic install goes live, and the merge that updates it.

Brief: ``internal/dispatch/done/al-3-2-install-to.md``. Engine:
``evolve_admin.applications.app_install``.

WHAT EACH CLAIM IS FOR, in the order the brief asks for them:

  1. ``test_two_bots_realize_the_same_source_shas`` — the ratified determinism
     wording, on a REAL install rather than a fixture: identical SOURCE sha
     sets across two bots, and every realized difference explained by declared
     substitution. This is 1.5c's
     ``test_realized_difference_is_fully_explained_by_substitution`` run
     against what actually landed on disk.
  2. ``test_the_installed_instance_adopts_the_source_app_id`` — the installed
     copy must resolve to the SAME ``app_id``, not mint a new one, or the pod
     grows a second app every time the button is pressed.
  3. ``test_an_unreadable_target_workspace_refuses_loudly`` — read-denied is
     not write-allowed: an unlistable manifests dir must not read as "the app
     is not here" and license an install over a copy nobody could see.
  4. ``test_installability_*`` — the button is enabled only where an install
     can actually happen: a verified pack, or a defined source to snapshot one
     from. A drifted pack is NOT installable, and saying otherwise moves the
     failure from before the click to after it.
  5. ``test_a_collision_refuses_before_anything_is_written`` /
     ``test_a_partial_install_writes_no_instance`` — never a half-install
     without saying so, and never a manifest over one.
  6. The update claims — D-L3: unadapted replaces, adapted refuses, a
     confirmed overwrite is still digest-checked, and a file the new version
     drops is reported rather than deleted.
  7. ``test_helper_*`` — the privileged seam's two guarantees in isolation:
     create-only cannot clobber, and a replace must match the digest it was
     given.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from evolve_admin.applications import app_install, marker_embed_helper
from evolve_admin.applications.app_install import (
    install_app_to_bot,
    installability,
    update_app_on_bot,
)
from evolve_admin.applications.app_identity import resolve_app_id
from evolve_admin.applications.app_snapshot import pack_dir, snapshot_app
from evolve_admin.applications.app_spec_store import load_spec
from evolve_admin.applications.files_pack import substitute_placeholders

APP = "task-manager"
SOURCE = "atlas"
SOURCE_USER = "atlas-user"
TARGET = "beacon"
TARGET_USER = "beacon-user"
#: A third bot exists only so "which copy do we pack?" can be a real question:
#: with two, the target having the app already answers it by elimination.
OTHER = "cinder"
OTHER_USER = "cinder-user"


# ── Fixture pod ──────────────────────────────────────────────────────────────


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """Two bot homes + a shared dir, with the config seams pinned.

    Mirrors ``test_al_3_1_app_snapshot``'s fixture and extends it to two bots,
    because every claim here is about the difference between them. The bots'
    ACCOUNT names differ from their ids deliberately: the pod has such bots,
    and a helper or context that keyed on the id would resolve to a home that
    does not exist.
    """
    homes = {
        SOURCE: tmp_path / "home" / SOURCE_USER,
        TARGET: tmp_path / "home" / TARGET_USER,
        OTHER: tmp_path / "home" / OTHER_USER,
    }
    users = {SOURCE: SOURCE_USER, TARGET: TARGET_USER, OTHER: OTHER_USER}
    for home in homes.values():
        (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setattr("evolve_admin.config.get_bot_user",
                        lambda bot_id, network=None: users[bot_id])
    monkeypatch.setattr("evolve_admin.config.bot_home",
                        lambda bot_id, network=None: homes[bot_id])

    class Pod:
        pass

    p = Pod()
    p.shared = shared
    p.homes = homes
    p.users = users
    p.ws = {b: homes[b] / ".openclaw" / "workspace" for b in homes}
    p.network = {
        "sharedDir": str(shared),
        "bots": {SOURCE: {"user": SOURCE_USER}, TARGET: {"user": TARGET_USER},
                 OTHER: {"user": OTHER_USER}},
    }
    return p


def _seed_source_app(pod, *, files: "dict[str, str] | None" = None) -> dict:
    """A defined app on the SOURCE bot with two files, one bot-specific."""
    ws = pod.ws[SOURCE]
    content = files if files is not None else {
        "scripts/tasks.py": f"WORKSPACE = '{ws}'\nOWNER = '{SOURCE}'\n",
        "docs/notes.md": "# Tasks\n\nNo bot tokens here.\n",
    }
    for rel, text in content.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    data = {
        "app_id": APP,
        "name": "Task Manager",
        "description": "Tracks tasks.",
        "definition_status": "defined",
        "files": [
            {"path": rel, "role": "vital_to_blueprint"} for rel in content
        ],
    }
    (ws / "manifests" / f"{APP}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _snapshot(pod):
    result = snapshot_app(SOURCE, APP, shared_dir=pod.shared, network=pod.network,
                          dry_run=False)
    assert result["ok"], result
    return result


def _install(pod, **kwargs):
    kwargs.setdefault("dry_run", False)
    return install_app_to_bot(APP, TARGET, shared_dir=pod.shared,
                              network=pod.network, **kwargs)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installs_for(pod, bots=(SOURCE,)):
    """``pod_apps``' per-app tuple list, built the way the route builds it."""
    from evolve_admin.applications.app_spec import spec_from_manifest

    out = []
    for bot in bots:
        raw = json.loads(
            (pod.ws[bot] / "manifests" / f"{APP}.json").read_text())
        out.append((bot, APP, raw, raw, spec_from_manifest(raw)))
    return out


# ── 1. determinism, on a real install ───────────────────────────────────────


def test_two_bots_realize_the_same_source_shas(pod):
    """The ratified wording: identical SOURCE shas; realized diffs explained.

    Both halves are asserted, and the second one is asserted the STRONG way —
    re-substituting the pack source under the target's context must reproduce
    the target's realized digest exactly. A test that only compared the two
    bots' digests would pass on an install that wrote nothing at all.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    result = _install(pod)
    assert result["ok"], result

    pack = pack_dir(pod.shared, APP)
    meta = json.loads((pack / "manifest.json").read_text())
    pack_shas = sorted({f["sha256"] for f in meta["files"]})

    # SOURCE shas: read off the pack, so bot-independent by construction —
    # and the proof carries them, so two installs are comparable without
    # re-reading either bot.
    assert result["proof"]["source_shas"] == pack_shas

    # REALIZED: every file's digest reproducible from (pack source + declared
    # placeholders + this bot's context) and from nothing else.
    assert result["proof"]["explained"], result["proof"]
    assert not result["proof"]["unexplained"]
    for row in result["proof"]["files"]:
        assert row["realized_sha"] == row["predicted_sha"]
        assert row["realized_sha"] == _sha(pod.ws[TARGET] / row["rel"])

    # And the difference between the two bots is real where a placeholder was
    # declared: this proof is only meaningful on a substituted corpus.
    varying = [f for f in meta["files"] if f["placeholders"]]
    assert varying, "the fixture must produce at least one substituted file"
    for entry in varying:
        source_bytes = (pod.ws[SOURCE] / entry["path"]).read_bytes()
        target_bytes = (pod.ws[TARGET] / entry["path"]).read_bytes()
        assert source_bytes != target_bytes, (
            f"{entry['path']} declares {entry['placeholders']} but landed "
            f"byte-identical on both bots — the substitution did not happen"
        )


def test_the_realized_difference_is_explained_and_nothing_else_is(pod):
    """1.5c's strong claim, re-derived independently of the engine's own proof.

    The engine reports ``explained``; this recomputes it from the pack and the
    documented context so a bug in the proof cannot certify itself.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    result = _install(pod)
    assert result["ok"], result

    pack = pack_dir(pod.shared, APP)
    meta = json.loads((pack / "manifest.json").read_text())
    instance = json.loads(Path(result["manifest_path"]).read_text())
    context = dict(instance["install"]["context"])
    context["shared_dir"] = str(pod.shared)

    for entry in meta["files"]:
        source_text = (pack / entry["path"]).read_text(encoding="utf-8")
        predicted = hashlib.sha256(substitute_placeholders(
            source_text, entry["placeholders"], context).encode("utf-8")
        ).hexdigest()
        assert predicted == _sha(pod.ws[TARGET] / entry["path"]), entry["path"]


# ── 2. identity ─────────────────────────────────────────────────────────────


def test_the_installed_instance_adopts_the_source_app_id(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    result = _install(pod)
    assert result["ok"], result

    instance = json.loads(Path(result["manifest_path"]).read_text())
    assert resolve_app_id(instance) == APP
    source = json.loads((pod.ws[SOURCE] / "manifests" / f"{APP}.json").read_text())
    assert resolve_app_id(instance) == resolve_app_id(source)
    assert instance["definition_status"] == "defined"
    assert instance["bot_id"] == TARGET
    # The realized digests are the TARGET's, not the source's — the two are
    # different digests on purpose (AL-1.5c §9.2) and both are recorded.
    realized = {f["path"]: f["sha256"] for f in instance["realized_files"]}
    assert realized["scripts/tasks.py"] == _sha(pod.ws[TARGET] / "scripts/tasks.py")
    spec = load_spec(pod.shared, APP)
    source_shas = {f["path"]: f["sha256"] for f in spec.package["files"]}
    assert realized["scripts/tasks.py"] != source_shas["scripts/tasks.py"]


def test_a_second_install_is_refused_and_points_at_update(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    again = _install(pod)
    assert not again["ok"] and again["refused"]
    assert again["error"].startswith("already_installed")


# ── 3. read-denied is not write-allowed ─────────────────────────────────────


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the 0000 mode")
def test_an_unreadable_target_workspace_refuses_loudly(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    manifests = pod.ws[TARGET] / "manifests"
    manifests.chmod(0o000)
    try:
        result = _install(pod)
    finally:
        manifests.chmod(0o755)
    assert not result["ok"]
    assert result["error"].startswith("workspace_unreadable")
    assert "NOT 'the app is not here'" in result["error"]
    assert not (pod.ws[TARGET] / "scripts").exists(), (
        "an install refused for an unreadable workspace must not have written "
        "files first"
    )


def test_a_missing_target_workspace_is_a_deploy_problem_not_a_refusal(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    import shutil
    shutil.rmtree(pod.homes[TARGET] / ".openclaw")
    result = _install(pod)
    assert not result["ok"] and result["error"].startswith("workspace_missing")


# ── 4. installability (what the button may offer) ───────────────────────────


def test_installability_is_pack_once_a_pack_exists(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    state = installability(APP, _installs_for(pod), pod.shared)
    assert state["state"] == "pack" and state["pack"] is True
    assert state["pack_files"] == 2


def test_installability_offers_a_snapshot_when_there_is_a_source(pod):
    _seed_source_app(pod)
    state = installability(APP, _installs_for(pod), pod.shared)
    assert state["state"] == "snapshot_needed"
    assert state["sources"] == [SOURCE]
    # The machine class rides in ``detail``; ``reason`` is what a surface may
    # show, and an offered button has nothing to explain.
    assert state["detail"].startswith("no_pack")
    assert state["reason"] == ""


def test_installability_is_unavailable_for_an_instruction_only_app(pod):
    """An app with no files has nothing for a deterministic install to place.

    The button must not offer it: "install" would be a promise the install
    path cannot keep, and the honest answer is why.
    """
    ws = pod.ws[SOURCE]
    (ws / "manifests" / f"{APP}.json").write_text(json.dumps({
        "app_id": APP, "name": "Task Manager", "definition_status": "defined",
    }), encoding="utf-8")
    state = installability(APP, _installs_for(pod), pod.shared)
    assert state["state"] == "unavailable"
    assert state["sources"] == []
    # A sentence an operator can act on — and NOT the engine's error class.
    # design §7: no field name from the JSON reaches the screen.
    assert "standing instructions" in state["reason"]
    assert ":" not in state["reason"].split(".")[0], state["reason"]


def test_a_drifted_pack_with_no_source_says_repackage_not_no_files(pod):
    """The two unavailable causes have different remediations, so different words.

    "There is nothing to install" and "the packaged copy has drifted" are not
    the same sentence, and telling an operator the first when the second is
    true sends them to fix a thing that is not broken.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    (pack_dir(pod.shared, APP) / "docs/notes.md").write_text("tampered\n")
    state = installability(APP, [], pod.shared)
    assert state["state"] == "unavailable"
    assert "no longer matches" in state["reason"]
    assert state["detail"].startswith("pack_verify_failed")


def test_a_drifted_pack_is_not_installable(pod):
    """Verification runs on the way IN, not only at snapshot time.

    A pack that has drifted on disk is one the install refuses, so offering an
    enabled button over it would move the failure to after the click.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    (pack_dir(pod.shared, APP) / "docs/notes.md").write_text("tampered\n")
    state = installability(APP, _installs_for(pod), pod.shared)
    assert state["state"] == "snapshot_needed"
    assert state["detail"].startswith("pack_verify_failed")
    result = _install(pod, snapshot_if_needed=False)
    assert not result["ok"]
    assert result["error"].startswith("pack_verify_failed")


# ── 5. never a half-install ─────────────────────────────────────────────────


def test_a_collision_refuses_before_anything_is_written(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    squatter = pod.ws[TARGET] / "docs" / "notes.md"
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else's file\n", encoding="utf-8")

    result = _install(pod)
    assert not result["ok"] and result["refused"]
    assert result["error"].startswith("target_collision")
    assert squatter.read_text() == "someone else's file\n"
    assert not (pod.ws[TARGET] / "scripts" / "tasks.py").exists(), (
        "the collision is decided before ANY file is written — a refusal that "
        "left half the pack on disk would be the half-install the brief bans"
    )
    assert not (pod.ws[TARGET] / "manifests" / f"{APP}.json").exists()


def test_a_partial_install_writes_no_instance(pod, monkeypatch):
    """One file fails: the failure is per-file and loud, and no manifest lands.

    A manifest is the pod's statement that the app IS here. Writing one over a
    partial materialization makes every later reader wrong about an app the
    bot can neither run nor repair.
    """
    _seed_source_app(pod)
    _snapshot(pod)

    real = app_install._write_one

    def flaky(target, planned, *, bot_user, expect_sha):
        if planned.rel.endswith("notes.md"):
            return False, f"{planned.rel}: disk went away"
        return real(target, planned, bot_user=bot_user, expect_sha=expect_sha)

    monkeypatch.setattr(app_install, "_write_one", flaky)
    result = _install(pod)

    assert not result["ok"]
    assert result["error"].startswith("partial_install")
    assert result["installed"] == ["scripts/tasks.py"]
    assert [f["rel"] for f in result["failed"]] == ["docs/notes.md"]
    assert "disk went away" in result["failed"][0]["error"]
    assert not (pod.ws[TARGET] / "manifests" / f"{APP}.json").exists()
    # The files that DID land are still on disk and are named, so an operator
    # can clean up or retry — silence about them would be the worse failure.
    assert (pod.ws[TARGET] / "scripts" / "tasks.py").is_file()


def test_a_dry_run_writes_nothing_and_predicts_everything(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    plan = _install(pod, dry_run=True)
    assert plan["ok"] and plan["dry_run"] is True
    assert {p["rel"] for p in plan["planned"]} == {
        "scripts/tasks.py", "docs/notes.md"}
    assert all(p["predicted_sha"] for p in plan["planned"])
    assert not (pod.ws[TARGET] / "scripts").exists()
    assert not (pod.ws[TARGET] / "manifests" / f"{APP}.json").exists()

    # …and the prediction is what the apply actually lands.
    predicted = {p["rel"]: p["predicted_sha"] for p in plan["planned"]}
    result = _install(pod)
    assert result["ok"]
    for rel, sha in predicted.items():
        assert _sha(pod.ws[TARGET] / rel) == sha


def test_the_install_refuses_a_pack_path_outside_the_ownable_set(pod):
    """A pack that names ``manifests/…`` must not be materialized there.

    The string check here and the helper's own ``can_app_own`` are two layers
    answering different questions; this is the one that keeps a malformed pack
    from ever reaching the privileged helper.
    """
    _seed_source_app(pod, files={"manifests/evil.json": "{}\n"})
    _snapshot(pod)
    result = _install(pod)
    assert not result["ok"]
    assert result["error"].startswith("unsafe_target")
    assert not (pod.ws[TARGET] / "manifests" / "evil.json").exists()


def test_a_snapshot_runs_first_when_the_app_has_no_pack(pod):
    _seed_source_app(pod)
    assert not (pack_dir(pod.shared, APP) / "manifest.json").exists()
    result = _install(pod)
    assert result["ok"], result
    assert result["snapshot"]["ok"] is True
    assert result["snapshot"]["bot_id"] == SOURCE
    assert (pack_dir(pod.shared, APP) / "manifest.json").is_file()


def _seed_second_copy(pod) -> None:
    """A second bot with its own, DIFFERENT copy of the same app."""
    ws = pod.ws[OTHER]
    (ws / "scripts").mkdir(parents=True, exist_ok=True)
    (ws / "scripts" / "tasks.py").write_text(
        f"WORKSPACE = '{ws}'\nOWNER = '{OTHER}'\nVARIANT = True\n",
        encoding="utf-8")
    (ws / "manifests" / f"{APP}.json").write_text(json.dumps({
        "app_id": APP, "name": "Task Manager", "definition_status": "defined",
        "files": [{"path": "scripts/tasks.py", "role": "vital_to_blueprint"}],
    }), encoding="utf-8")


def test_an_ambiguous_source_is_named_not_guessed(pod):
    """Two bots have the app defined; neither copy is the obvious one to pack.

    Two bots' copies are two different sets of bytes — here they differ in
    both content and file count. Picking one silently would install whichever
    the directory happened to yield first.
    """
    _seed_source_app(pod)
    _seed_second_copy(pod)

    result = _install(pod, dry_run=True)
    assert not result["ok"] and result["refused"]
    assert result["error"].startswith("ambiguous_source")
    assert sorted(result["snapshot"]["candidates"]) == sorted([SOURCE, OTHER])
    assert not (pack_dir(pod.shared, APP) / "manifest.json").exists()


def test_a_named_source_is_the_one_that_gets_packed(pod):
    _seed_source_app(pod)
    _seed_second_copy(pod)
    result = _install(pod, source_bot=OTHER)
    assert result["ok"], result
    assert result["snapshot"]["bot_id"] == OTHER
    # The variant's marker landed, so the pack really came from the named bot
    # and not from whichever the walk reached first.
    assert "VARIANT" in (pod.ws[TARGET] / "scripts" / "tasks.py").read_text()
    assert not (pod.ws[TARGET] / "docs" / "notes.md").exists()


def test_a_source_that_does_not_have_the_app_is_refused(pod):
    _seed_source_app(pod)
    result = _install(pod, source_bot=OTHER)
    assert not result["ok"] and result["refused"]
    assert result["error"].startswith("source_not_eligible")
    assert SOURCE in result["error"]


# ── 6. update is a merge (D-L3) ─────────────────────────────────────────────


def _bump(pod, *, text: str) -> None:
    """Change the app on the source bot and re-snapshot: a new version."""
    (pod.ws[SOURCE] / "scripts" / "tasks.py").write_text(text, encoding="utf-8")
    _snapshot(pod)


def _update(pod, **kwargs):
    kwargs.setdefault("dry_run", False)
    return update_app_on_bot(APP, TARGET, shared_dir=pod.shared,
                             network=pod.network, **kwargs)


def test_an_unadapted_instance_takes_the_new_version(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")

    plan = _update(pod, dry_run=True)
    assert plan["ok"] and plan["adapted"] is False
    assert plan["bases"] == ["recorded_install"]

    result = _update(pod)
    assert result["ok"], result
    assert result["applied"] == ["scripts/tasks.py"]
    assert "V = 2" in (pod.ws[TARGET] / "scripts" / "tasks.py").read_text()
    assert result["proof"]["explained"]


def test_an_adapted_instance_is_never_flattened_silently(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    local = pod.ws[TARGET] / "scripts" / "tasks.py"
    local.write_text(local.read_text() + "# a local adaptation\n", encoding="utf-8")
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")

    plan = _update(pod, dry_run=True)
    assert plan["adapted"] is True
    assert [c["rel"] for c in plan["conflicts"]] == ["scripts/tasks.py"]
    assert plan["conflicts"][0]["basis"] == "recorded_install"

    result = _update(pod)
    assert not result["ok"] and result["refused"]
    assert result["error"].startswith("would_overwrite_local_changes")
    assert "# a local adaptation" in local.read_text(), (
        "the refusal must leave the local work exactly where it was"
    )


def test_a_confirmed_overwrite_still_checks_the_digest_it_measured(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    local = pod.ws[TARGET] / "scripts" / "tasks.py"
    local.write_text(local.read_text() + "# a local adaptation\n", encoding="utf-8")
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")

    result = _update(pod, confirm_overwrite=True)
    assert result["ok"], result
    assert "V = 2" in local.read_text()
    assert "# a local adaptation" not in local.read_text()


def test_a_file_that_changed_after_the_preview_refuses_the_write(pod, monkeypatch):
    """The digest guard is what makes a confirmation non-stale.

    A confirmation is given against a measured state. If the file moves in
    between, the write must refuse rather than apply a decision made about
    bytes that are no longer there.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    local = pod.ws[TARGET] / "scripts" / "tasks.py"
    local.write_text(local.read_text() + "# adaptation A\n", encoding="utf-8")
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")

    real = app_install._write_one

    def racing(target, planned, *, bot_user, expect_sha):
        if planned.rel.endswith("tasks.py"):
            local.write_text("# adaptation B, written after the measurement\n",
                             encoding="utf-8")
        return real(target, planned, bot_user=bot_user, expect_sha=expect_sha)

    monkeypatch.setattr(app_install, "_write_one", racing)
    result = _update(pod, confirm_overwrite=True)
    assert not result["ok"]
    assert result["error"].startswith("partial_update")
    assert "changed since it was measured" in result["failed"][0]["error"]
    assert local.read_text().startswith("# adaptation B")


def test_a_file_the_new_version_drops_is_reported_not_deleted(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    dropped = pod.ws[TARGET] / "docs" / "notes.md"
    assert dropped.is_file()

    # The source loses the file, and the app is re-snapshotted without it.
    (pod.ws[SOURCE] / "docs" / "notes.md").unlink()
    (pod.ws[SOURCE] / "manifests" / f"{APP}.json").write_text(json.dumps({
        "app_id": APP, "name": "Task Manager", "definition_status": "defined",
        "files": [{"path": "scripts/tasks.py", "role": "vital_to_blueprint"}],
    }), encoding="utf-8")
    _snapshot(pod)

    plan = _update(pod, dry_run=True)
    assert plan["removed_upstream"] == ["docs/notes.md"]
    result = _update(pod)
    assert result["ok"], result
    assert dropped.is_file(), (
        "an update that quietly removed files would be indistinguishable from "
        "an uninstall the operator did not ask for"
    )


def test_an_instance_with_no_recorded_digests_falls_back_and_says_so(pod):
    """The weaker basis is reported as the weaker basis.

    An instance this installer did not write records nothing to compare
    against, so the check falls back to 1.5c's property against the current
    pack — which cannot tell a local edit from a change in the new version.
    That is a real limitation and the payload names it rather than presenting
    the answer as if it were the strong one.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    path = pod.ws[TARGET] / "manifests" / f"{APP}.json"
    data = json.loads(path.read_text())
    for entry in data["realized_files"]:
        entry.pop("sha256")
    path.write_text(json.dumps(data), encoding="utf-8")

    plan = _update(pod, dry_run=True)
    assert "current_pack" in plan["bases"]
    assert plan["adapted"] is False, (
        "an untouched file must still read unadapted under the fallback basis"
    )


def test_updating_an_app_the_bot_does_not_have_points_at_install(pod):
    _seed_source_app(pod)
    _snapshot(pod)
    result = _update(pod)
    assert not result["ok"] and result["refused"]
    assert result["error"].startswith("not_installed")


# ── 7. the privileged seam, in isolation ────────────────────────────────────


def _helper(*args) -> int:
    return marker_embed_helper.main(["marker_embed_helper.py", *args])


@pytest.fixture
def helper_pod(tmp_path, monkeypatch):
    """A fake home root so the helper's own containment walk has one."""
    home_root = tmp_path / "Users"
    ws = home_root / SOURCE_USER / ".openclaw" / "workspace"
    ws.mkdir(parents=True)

    class _Profile:
        user_home_root = str(home_root)

    import platform_profile
    monkeypatch.setattr(platform_profile, "get_profile", lambda: _Profile())
    monkeypatch.setattr(marker_embed_helper, "_bot_ids",
                        lambda bot_user: (os.getuid(), os.getgid(), ""))
    return ws


def _stage(tmp_path, text: str) -> str:
    staged = Path("/tmp") / f"evolve-appfile-test-{os.getpid()}-{os.urandom(4).hex()}"
    staged.write_text(text, encoding="utf-8")
    return str(staged)


def test_helper_install_creates_missing_parents(helper_pod, tmp_path):
    staged = _stage(tmp_path, "print('hi')\n")
    dest = helper_pod / "scripts" / "deep" / "tool.py"
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0755") == 0
    finally:
        os.unlink(staged)
    assert dest.read_text() == "print('hi')\n"
    assert dest.stat().st_mode & 0o777 == 0o755
    assert (helper_pod / "scripts").stat().st_mode & 0o777 == 0o755


def test_helper_install_is_create_only_without_a_digest(helper_pod, tmp_path):
    dest = helper_pod / "scripts" / "tool.py"
    dest.parent.mkdir(parents=True)
    dest.write_text("original\n", encoding="utf-8")
    staged = _stage(tmp_path, "replacement\n")
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0644") == 2
    finally:
        os.unlink(staged)
    assert dest.read_text() == "original\n"


def test_helper_install_replace_must_match_the_expected_digest(helper_pod, tmp_path):
    dest = helper_pod / "scripts" / "tool.py"
    dest.parent.mkdir(parents=True)
    dest.write_text("original\n", encoding="utf-8")
    staged = _stage(tmp_path, "replacement\n")
    wrong = hashlib.sha256(b"something else\n").hexdigest()
    right = hashlib.sha256(b"original\n").hexdigest()
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0644", wrong) == 2
        assert dest.read_text() == "original\n"
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0644", right) == 0
    finally:
        os.unlink(staged)
    assert dest.read_text() == "replacement\n"


def test_helper_install_refuses_a_path_outside_the_ownable_set(helper_pod, tmp_path):
    staged = _stage(tmp_path, "{}\n")
    dest = helper_pod / "manifests" / "evil.json"
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0644") == 2
    finally:
        os.unlink(staged)
    assert not dest.exists()


def test_helper_install_refuses_another_bots_workspace(helper_pod, tmp_path):
    staged = _stage(tmp_path, "print('x')\n")
    dest = helper_pod / "scripts" / "tool.py"
    try:
        assert _helper("--install", staged, str(dest), TARGET_USER, "0644") == 2
    finally:
        os.unlink(staged)
    assert not dest.exists()


def test_helper_install_refuses_a_setuid_mode(helper_pod, tmp_path):
    staged = _stage(tmp_path, "print('x')\n")
    dest = helper_pod / "scripts" / "tool.py"
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "04755") == 2
    finally:
        os.unlink(staged)
    assert not dest.exists()


def test_helper_install_refuses_a_symlinked_leaf(helper_pod, tmp_path):
    dest = helper_pod / "scripts" / "tool.py"
    dest.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("secret\n", encoding="utf-8")
    dest.symlink_to(elsewhere)
    staged = _stage(tmp_path, "replacement\n")
    right = hashlib.sha256(b"secret\n").hexdigest()
    try:
        assert _helper("--install", staged, str(dest), SOURCE_USER, "0644", right) == 2
    finally:
        os.unlink(staged)
    assert elsewhere.read_text() == "secret\n"


def test_the_embed_mode_still_requires_a_marker(helper_pod, tmp_path):
    """Relaxing the marker rule for ``--install`` must not relax it for embed.

    The two modes have different exposures — embed overwrites, install cannot
    without a digest — so the check that separates them has to still be there.
    """
    dest = helper_pod / "scripts" / "tool.py"
    dest.parent.mkdir(parents=True)
    dest.write_text("original\n", encoding="utf-8")
    staged = _stage(tmp_path, "no marker here\n")
    try:
        assert _helper(staged, str(dest), SOURCE_USER) == 2
    finally:
        os.unlink(staged)
    assert dest.read_text() == "original\n"


# ── 8. the guards the first review pass added ───────────────────────────────


def test_a_pack_asking_for_a_setuid_file_is_refused(pod):
    """Checked at plan time, so BOTH write paths agree about it.

    The privileged helper refuses setuid root-side and that is the boundary
    that holds — but the direct write path does not go through it, so without
    a plan-time check a pack could mint a setuid file wherever the caller can
    already write, and the two paths would disagree about what a pack may ask
    for.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    pack = pack_dir(pod.shared, APP)
    manifest = json.loads((pack / "manifest.json").read_text())
    for entry in manifest["files"]:
        if entry["path"].endswith("tasks.py"):
            entry["mode"] = "04755"
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = _install(pod, snapshot_if_needed=False)
    assert not result["ok"]
    assert result["error"].startswith("unsafe_target")
    assert "setuid" in result["error"]
    assert not (pod.ws[TARGET] / "scripts").exists()


def test_the_instance_never_replaces_another_apps_manifest(pod, monkeypatch):
    """``manifests/<app_id>.json`` may already belong to something else.

    Nothing in the directory resolved to THIS app (the install checked), so a
    file at that name is a different app's record — and writing over it would
    make the pod forget an app as a side effect of installing another one.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    squatter = pod.ws[TARGET] / "manifests" / f"{APP}.json"
    squatter.write_text(json.dumps({
        "app_id": "something-else", "name": "Something Else",
        "definition_status": "defined",
    }), encoding="utf-8")

    result = _install(pod)
    assert not result["ok"]
    assert result["error"].startswith("manifest_stem_taken")
    assert "something-else" in result["error"]
    assert json.loads(squatter.read_text())["app_id"] == "something-else"


def test_an_unreadable_file_is_not_replaced_blind(pod):
    """A replace has to match a digest, and an unreadable file has none.

    Overwriting it anyway would be a blind clobber of something the pod could
    not even look at — the same read-denied-is-not-write-allowed rule the
    install path applies to a whole workspace, at file scale.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    target = pod.ws[TARGET] / "scripts" / "tasks.py"
    target.unlink()
    target.mkdir()                      # a directory where a file should be
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")

    plan = _update(pod, dry_run=True)
    assert plan["adapted"] is True
    result = _update(pod, confirm_overwrite=True)
    assert not result["ok"]
    assert result["error"].startswith("partial_update")
    assert "could not be measured" in result["failed"][0]["error"]
    assert target.is_dir(), "the unreadable path must be left exactly as it was"


def test_the_write_route_is_chosen_by_who_would_own_the_file(pod, monkeypatch):
    """Not "try direct, escalate on EACCES" — ownership decides, up front.

    A write that succeeds can still be wrong: it lands the file under whichever
    account happened to write it. A root or ``evolve`` write leaves a bot
    locked out of its own app file and the install still reports success, which
    is exactly the silence this routing exists to prevent.
    """
    _seed_source_app(pod)
    _snapshot(pod)

    # The bot account does not exist in a tmp fixture, so the direct path is
    # the honest one — there is no ownership question to get wrong.
    assert app_install._writes_land_bot_owned(TARGET_USER) is True

    # Pretend it does exist, owned by someone who is not us.
    class _Pw:
        pw_uid = os.getuid() + 1

    import pwd as _pwd
    monkeypatch.setattr(_pwd, "getpwnam", lambda name: _Pw())
    assert app_install._writes_land_bot_owned(TARGET_USER) is False

    calls = []

    def _spy(target, *, content, bot_user, mode, expect_sha256=""):
        calls.append((str(target), bot_user, mode, expect_sha256))
        return False, "no sudoers grant on this host", False

    monkeypatch.setattr(marker_embed_helper,
                        "install_workspace_file_privileged", _spy)
    result = _install(pod)

    assert not result["ok"] and result["error"].startswith("partial_install")
    assert len(calls) == 2, calls
    assert all(c[1] == TARGET_USER for c in calls), (
        "the helper is bound to the bot's ACCOUNT name — it compares that "
        "against the destination's home component, so passing the bot id "
        "would refuse every write on a pod where the two differ"
    )
    assert all(c[3] == "" for c in calls), "an install creates; it never replaces"
    assert "no sudoers grant" in result["failed"][0]["error"]


def _tamper_after_write(monkeypatch, workspace, rel: str, text: str):
    """Let the write happen, then change the bytes underneath it.

    Simulates the class the proof exists to catch: something between the
    substitution and the disk did not preserve what was predicted. Without a
    test that actually breaks the property, the proof gate is asserted but
    never executed — it would survive being deleted.
    """
    real = app_install._write_one

    def _wrapped(target, planned, *, bot_user, expect_sha):
        ok, err = real(target, planned, bot_user=bot_user, expect_sha=expect_sha)
        if ok and planned.rel == rel:
            (workspace / rel).write_text(text, encoding="utf-8")
        return ok, err

    monkeypatch.setattr(app_install, "_write_one", _wrapped)


def test_an_install_whose_bytes_are_not_reproducible_writes_no_instance(
    pod, monkeypatch,
):
    """The proof is a GATE, not a report.

    A file on disk that cannot be re-derived from (pack source + declared
    placeholders + this bot's context) means the install was not deterministic
    for that file. Recording an instance over it would tell every later reader
    the app is installed and verified when the second half is false.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    _tamper_after_write(monkeypatch, pod.ws[TARGET], "scripts/tasks.py",
                        "something else entirely\n")

    result = _install(pod)
    assert not result["ok"]
    assert result["error"].startswith("install_proof_failed")
    assert result["proof"]["explained"] is False
    assert result["proof"]["unexplained"] == ["scripts/tasks.py"]
    assert not (pod.ws[TARGET] / "manifests" / f"{APP}.json").exists()

    # …and the per-file report says WHICH file and why, not just that it failed.
    row = next(f for f in result["proof"]["files"]
               if f["rel"] == "scripts/tasks.py")
    assert row["explained"] is False
    assert row["realized_sha"] != row["predicted_sha"]
    assert "not reproducible" in row["note"]


def test_an_update_whose_bytes_are_not_reproducible_does_not_re_pin(pod, monkeypatch):
    """Same gate on the update side: the version pointer must not move.

    A bot whose files are not what the new version says they are must keep
    reporting the version it can actually run.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]
    before = json.loads(
        (pod.ws[TARGET] / "manifests" / f"{APP}.json").read_text())
    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")
    _tamper_after_write(monkeypatch, pod.ws[TARGET], "scripts/tasks.py",
                        "mangled by something in between\n")

    result = _update(pod)
    assert not result["ok"]
    assert result["error"].startswith("update_proof_failed")
    after = json.loads(
        (pod.ws[TARGET] / "manifests" / f"{APP}.json").read_text())
    assert after["spec_version"] == before["spec_version"]
    assert after["realized_files"] == before["realized_files"]


# ── 9. the version that makes Update-to-vN reachable ────────────────────────


def test_a_content_change_moves_the_spec_version_and_a_no_op_does_not(pod):
    """AL-3.1 handed this decision to the install surface; this is it.

    Without a bump on a real re-snapshot, every bot's pinned version stays
    equal to the app's and ``Update to vN`` can never appear — the button
    would be unreachable by construction, not by circumstance. With a bump on
    a NO-OP re-snapshot, AL-3.1's idempotence promise breaks. Both halves are
    asserted here because either one alone is the wrong answer.
    """
    _seed_source_app(pod)
    _snapshot(pod)
    first = load_spec(pod.shared, APP).spec_version

    # A no-op re-snapshot: same bytes, same version.
    _snapshot(pod)
    assert load_spec(pod.shared, APP).spec_version == first

    # A real change: the version moves, and forward.
    (pod.ws[SOURCE] / "scripts" / "tasks.py").write_text(
        f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n",
        encoding="utf-8")
    result = _snapshot(pod)
    assert result["changed"] is True
    second = load_spec(pod.shared, APP).spec_version
    assert second > first


def test_a_bot_behind_the_spec_is_what_makes_update_offerable(pod):
    """The end-to-end shape the button reads: install, change, re-snapshot.

    Asserts the RELATIONSHIP the UI's guard tests (`app.spec_version >
    bot.spec_version`), not the numbers — so a change to the packing scheme
    cannot quietly make the button disappear while this still passes.
    """
    from evolve_admin.applications.app_spec import spec_from_manifest

    _seed_source_app(pod)
    _snapshot(pod)
    assert _install(pod)["ok"]

    instance = json.loads(
        (pod.ws[TARGET] / "manifests" / f"{APP}.json").read_text())
    assert spec_from_manifest(instance).spec_version == \
        load_spec(pod.shared, APP).spec_version, (
            "a fresh install is never behind the app it was installed from"
        )

    _bump(pod, text=f"WORKSPACE = '{pod.ws[SOURCE]}'\nOWNER = '{SOURCE}'\nV = 2\n")
    app_version = load_spec(pod.shared, APP).spec_version
    bot_version = spec_from_manifest(json.loads(
        (pod.ws[TARGET] / "manifests" / f"{APP}.json").read_text())).spec_version
    assert app_version > bot_version

    # …and applying the update closes the gap.
    assert _update(pod)["ok"]
    after = spec_from_manifest(json.loads(
        (pod.ws[TARGET] / "manifests" / f"{APP}.json").read_text())).spec_version
    assert after == app_version


def test_a_malformed_install_call_does_not_fall_through_to_the_embed_form(
    helper_pod, tmp_path,
):
    """Wrong argument count on ``--install`` is a usage error, not a mystery.

    Without the guard, a 4-argument ``--install`` matches the legacy embed
    form's arity, ``--install`` is read as the staged source path, and the
    operator gets "staged source '--install' is not absolute" for what is
    really a wrong call.
    """
    import io
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert _helper("--install", "a", "b") == 2
    assert "--install <staged_tmp>" in err.getvalue()
    assert "is not absolute" not in err.getvalue()
