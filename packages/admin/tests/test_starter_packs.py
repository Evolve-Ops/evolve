"""tests/test_starter_packs.py — archetype starter packs (M3, delta §5).

Three layers:

  * Gallery integrity — every ``starter-pack``-tagged bundle is a true
    meta-spec (no scripts, no scheduled actions), carries exactly one
    ``archetype-*`` tag on the effectiveness-layer enum, and its
    ``app_dependencies`` all resolve to builtin gallery packages. A
    bundle that silently points at a missing app would make the wizard
    propose something it can't build.
  * ``pack.load_starter_pack`` default loader — resolves the real
    bundles through the tag index; honest ``None`` + reasons for the
    no-pack archetypes (§10 OQ4: ship honest, don't hold the role).
  * Forge seeding — install jobs carrying ``privacy_seed`` /
    ``audience_scoping_seed`` in their context snapshot (the add-bot
    wizard's consent answers, delta §7 + the v24 update) land on the
    seeded manifest; gallery-declared blocks still win.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
_REPO = _ADMIN.parent.parent
for _p in (str(_ADMIN), str(_ANALYZER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_GALLERY = _REPO / "gallery"


def _tags_index() -> dict:
    return json.loads((_GALLERY / "tags-index.json").read_text())


def _index() -> list[dict]:
    return json.loads((_GALLERY / "index.json").read_text())


def _load_pkg(pkg_id: str) -> dict:
    entry = next(e for e in _index() if e["pkg_id"] == pkg_id)
    return json.loads((_GALLERY / entry["path"]).read_text())


# ── Gallery integrity ─────────────────────────────────────────────────────────


def test_starter_packs_exist_for_decided_archetypes():
    """§5 starter map: personal-assistant (the EA Pack) and
    project-manager ship bundles; the honest-pack roles don't."""
    tags = _tags_index()
    assert tags.get("archetype-personal-assistant")
    assert tags.get("archetype-project-manager")
    assert "archetype-research-analyst" not in tags
    assert "archetype-home-automation" not in tags


def test_starter_pack_bundles_are_honest_meta_specs():
    from evolve_admin.evo.wizard.phases import AB_ARCHETYPES

    tags = _tags_index()
    starter = tags.get("starter-pack") or []
    assert starter, "no starter-pack bundles tagged in the gallery"
    for pkg_id in starter:
        pkg = _load_pkg(pkg_id)
        # Meta-spec: no scripts, no schedules of its own.
        assert pkg.get("files") == [], f"{pkg_id} ships files"
        assert pkg.get("scheduled_actions") == [], f"{pkg_id} schedules actions"
        # Exactly one archetype tag, on the §4 enum.
        arch_tags = [
            t for t in pkg.get("application_tags") or []
            if t.startswith("archetype-")
        ]
        assert len(arch_tags) == 1, f"{pkg_id} archetype tags: {arch_tags}"
        assert arch_tags[0].removeprefix("archetype-") in AB_ARCHETYPES
        # Every declared dependency resolves to a real builtin package —
        # the wizard proposes this list verbatim.
        known = {e["pkg_id"] for e in _index()}
        deps = pkg.get("app_dependencies") or []
        assert deps, f"{pkg_id} declares no apps"
        for dep in deps:
            assert dep["pkg_id"] in known, (
                f"{pkg_id} depends on {dep['pkg_id']} "
                f"({dep.get('display_name')}) which is not in the gallery"
            )


# ── Default loader ────────────────────────────────────────────────────────────


def test_default_loader_resolves_project_pack(tmp_path):
    from evolve_admin.evo.wizard import pack

    sp = pack.load_starter_pack("project-manager", tmp_path)
    assert sp is not None
    assert sp.bundle_name == "Project Pack"
    names = [a.name for a in sp.apps]
    assert names == [
        "Task Manager", "Contacts", "Calendar Sync",
        "Meeting Note-taker", "Commitment Tracker", "Pre-Meeting Brief",
    ]
    assert sp.missing == []
    assert all(a.blurb for a in sp.apps)
    assert all(a.est_usd > 0 for a in sp.apps)


def test_default_loader_resolves_ea_pack(tmp_path):
    from evolve_admin.evo.wizard import pack

    sp = pack.load_starter_pack("personal-assistant", tmp_path)
    assert sp is not None
    assert sp.bundle_name == "EA Pack"
    assert len(sp.apps) == 7
    assert pack.MORNING_BRIEFING_PKG_ID in {a.pkg_id for a in sp.apps}


def test_default_loader_returns_none_for_honest_archetypes(tmp_path):
    from evolve_admin.evo.wizard import pack

    for archetype in ("research-analyst", "home-automation",
                      "customer-facing", "custom"):
        assert pack.load_starter_pack(archetype, tmp_path) is None
        assert archetype in pack.NO_PACK_REASONS


def test_estimates_never_claim_zero():
    from evolve_admin.evo.wizard import pack

    assert pack.estimate_usd([{}]) > 0
    assert pack.estimate_usd([{"est_usd": 0.5}, {"est_usd": 0.3}]) == 0.8
    assert pack.estimate_minutes(1) >= 2
    assert pack.estimate_minutes(4) >= 4


# ── Forge seeding: consent answers land on installed apps ─────────────────────


def _make_install_job(context_snapshot: dict):
    from evolve_admin.applications.forge_jobs import ForgeJob, _install_steps
    job = ForgeJob(
        job_id="j-seed1234",
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-test1234",
        app_id="test-app",
        bot_id="team-bot-c",
        pkg_version_before=None,
        gallery_version=None,
        steps=_install_steps(),
        status="queued",
    )
    job.context_snapshot.update(context_snapshot)
    return job


def _run_step1_and_capture_manifest(tmp_path, job, gallery_pkg):
    """Run run_forge_job with everything beyond Step 1 stubbed; return
    the manifest save_manifest received (the seeded manifest)."""
    from evolve_admin.applications import forge_engine

    captured: list = []
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_llm_keys",
                      return_value={"anthropic": "sk-test-not-a-real-key"}), \
         patch.object(forge_engine, "_get_targets", return_value=(None, None)), \
         patch.object(forge_engine, "approve_forge_job"), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest",
                      side_effect=lambda m, sd: captured.append(m)), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=gallery_pkg), \
         patch.object(forge_engine, "assemble_context_package",
                      return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id=job.job_id, shared_dir=tmp_path, bot_id=job.bot_id,
        )
    assert captured, "Step 1 never saved a manifest"
    return captured[0]


def _gallery_pkg(**extra):
    pkg = {
        "pkg_id": "p-test1234",
        "name": "test-app",
        "display_name": "Test App",
        "objective": "test app",
        "build_spec": "n/a",
    }
    pkg.update(extra)
    return pkg


def test_privacy_seed_from_job_lands_on_manifest(tmp_path):
    manifest = _run_step1_and_capture_manifest(
        tmp_path,
        _make_install_job({
            "privacy_seed": {
                "consent_notice": "This bot saves links the group shares.",
                "opt_out_command": "react 🤐",
                "ignored_key": "dropped",  # not in PRIVACY_KEYS
            },
            "audience_scoping_seed": {
                "operator": "open",
                "approved_surfaces": ["telegram_group"],
                "role_capabilities": {
                    "operator_only": ["read", "write", "configure"],
                    "audience": ["read"],
                },
                "operator_bypasses": [],
            },
        }),
        _gallery_pkg(),
    )
    privacy = manifest.privacy
    assert privacy["consent_notice"] == "This bot saves links the group shares."
    assert privacy["opt_out_command"] == "react 🤐"
    assert "ignored_key" not in privacy
    # The conservative defaults still underlie the seed.
    assert privacy["user_data_collected"] == []
    assert privacy["shareable_in_lessons"] is False
    scoping = manifest.audience_scoping
    assert scoping["operator"] == "open"
    assert scoping["approved_surfaces"] == ["telegram_group"]


def test_gallery_declared_privacy_wins_over_seed(tmp_path):
    manifest = _run_step1_and_capture_manifest(
        tmp_path,
        _make_install_job({
            "privacy_seed": {"consent_notice": "wizard text"},
        }),
        _gallery_pkg(privacy={
            "user_data_collected": ["links"],
            "retention_days": 30,
            "shareable_in_lessons": False,
            "consent_notice": "the package's own notice",
        }),
    )
    assert manifest.privacy["consent_notice"] == "the package's own notice"


def test_no_seed_still_gets_conservative_defaults(tmp_path):
    manifest = _run_step1_and_capture_manifest(
        tmp_path, _make_install_job({}), _gallery_pkg(),
    )
    assert manifest.privacy["user_data_collected"] == []
    assert manifest.audience_scoping["operator"] == "operator_only"


# ── Morning Briefing v2.1 — the no-data-mode contract ─────────────────────────


def test_briefing_manifest_keeps_delivery_contract_and_adds_no_data():
    pkg = _load_pkg("p-a9a74bf7")
    assert pkg["pkg_version"] == "2026.07.02-2.5"
    spec = pkg["build_spec"]
    # The new day-one mode is specified…
    assert "No-data (day one) mode" in spec
    assert "compose_no_data" in spec
    assert "Connect a calendar and I'll put your day here" in spec
    # …the old silent skip is gone…
    assert "BRIEFING_SKIPPED: {today} no-data" not in spec
    # …and the load-bearing delivery-monitor contract is intact: run
    # file only after the channel accepted the send (the post-OC-2026.6
    # delivery convention), evidence path unchanged.
    assert "only after the channel accepted the send" in spec
    sched = pkg["scheduled_actions"][0]
    evidence = sched["delivery_contract"]["evidence"]["delivered"]
    assert evidence == {
        "kind": "run_file",
        "path": "memory/briefing-runs/{date}.json",
    }
