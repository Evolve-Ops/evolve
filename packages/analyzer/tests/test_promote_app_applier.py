"""PromoteApp applier tests (AL-1.7) — the server-side half of design §7.2.

**This file is the MECHANISM proof the brief's §7.1 asks for, and it is not the
P1 demo.** Everything below runs against a temp-dir manifest this test wrote.
It proves the promotion path works end to end when a promotable draft exists; it
proves nothing about whether one exists on the operator's pod. See
``docs/build-AL-1.7-promotion.md`` §8 for what the P1 demo still needs.

Every guard here was mutation-checked — see each docstring.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schema.proposal import PromoteApp


NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def bot_manifests(tmp_path, monkeypatch):
    """A fake bot home with one discovered manifest. Returns (write, read)."""
    import arbiter.appliers.promote_app as mod

    home = tmp_path / "Users" / "team-bot-a"
    manifests = home / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    monkeypatch.setattr(mod, "_bot_home", lambda bot_id: tmp_path / "Users" / bot_id)

    def write(stem: str, data: dict) -> Path:
        path = manifests / f"{stem}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def read(stem: str) -> dict:
        return json.loads((manifests / f"{stem}.json").read_text(encoding="utf-8"))

    return write, read


def _applier():
    from arbiter.appliers.base import get_applier

    return get_applier("PromoteApp")


def test_the_applier_is_registered():
    """MUTATION CHECKED: removing ``promote_app`` from
    ``arbiter/appliers/__init__.py``'s import list makes this go red — and an
    unregistered applier means an approved promotion silently never applies."""
    import arbiter.appliers  # noqa: F401 — the import IS the registration

    assert _applier() is not None


def test_promotion_confers_the_id_and_flips_the_status(bot_manifests):
    """The whole mechanism, on a STAGED draft. Not the P1 demo.

    MUTATION CHECKED: removing the ``promote_to_defined`` call leaves
    ``definition_status == "discovered"`` and this goes red; removing the
    ``manifest[APP_ID_FIELD] = action.app_id`` line drops the id and it goes red
    on the other assertion.
    """
    write, read = bot_manifests
    write(
        "morning-brief",
        {
            "name": "Morning Brief",
            "definition_status": "discovered",
            "draft_id": "draft-abc123456789",
        },
    )
    result = _applier().apply(
        PromoteApp(
            app_id="morning-brief",
            manifest_stem="morning-brief",
            identity_mode="conferred",
            app_audience="everyone",
        ),
        "team-bot-a",
    )
    assert result.ok, result.message
    after = read("morning-brief")
    assert after["app_id"] == "morning-brief"
    assert after["definition_status"] == "defined"
    assert after["audience"] == "everyone"
    # A promoted app is no longer a draft — see the module docstring on why a
    # lingering draft_id would make every later backfill treat the id as
    # provisional.
    assert "draft_id" not in after


def test_adoption_keeps_the_existing_id(bot_manifests):
    """§7.3a ADOPT, at the write side.

    MUTATION CHECKED: making the applier re-slug from ``name`` makes this red.
    """
    write, read = bot_manifests
    write(
        "atlas-article-capture",
        {"name": "Atlas Article Capture", "app_id": "atlas-capture",
         "definition_status": "discovered"},
    )
    result = _applier().apply(
        PromoteApp(
            app_id="atlas-capture",
            manifest_stem="atlas-article-capture",
            identity_mode="adopted",
        ),
        "team-bot-a",
    )
    assert result.ok, result.message
    assert read("atlas-article-capture")["app_id"] == "atlas-capture"


def test_the_stem_and_the_app_id_may_diverge(bot_manifests):
    """Read and write must hit the SAME file — the finding
    ``routes_app_definition._resolve_manifest_stem`` exists for.

    MUTATION CHECKED: changing ``_manifest_path`` to use ``action.app_id``
    instead of ``action.manifest_stem`` makes this go red with
    ``manifest_missing``.
    """
    write, read = bot_manifests
    write("atlas-article-capture", {"name": "Atlas", "definition_status": "discovered"})
    result = _applier().apply(
        PromoteApp(app_id="atlas-capture", manifest_stem="atlas-article-capture"),
        "team-bot-a",
    )
    assert result.ok, result.message
    assert read("atlas-article-capture")["definition_status"] == "defined"


def test_a_conflicting_app_id_is_refused_not_overwritten(bot_manifests):
    """Identity is immutable once conferred (design §3).

    MUTATION CHECKED: deleting the ``app_id_conflict`` branch lets the applier
    re-identify the app and this goes red — which is the exact failure §7.3a's
    ADOPT decision exists to prevent.
    """
    write, read = bot_manifests
    write("brief", {"app_id": "already-here", "definition_status": "discovered"})
    result = _applier().apply(
        PromoteApp(app_id="something-else", manifest_stem="brief"), "team-bot-a"
    )
    assert not result.ok
    assert result.details["reason"] == "app_id_conflict"
    # And nothing was written.
    assert read("brief") == {"app_id": "already-here", "definition_status": "discovered"}


def test_an_empty_app_id_is_refused(bot_manifests):
    """MUTATION CHECKED: deleting the ``no_app_id`` guard lets a blocked
    identity decision write an app with an empty id and this goes red."""
    write, read = bot_manifests
    write("brief", {"definition_status": "discovered"})
    result = _applier().apply(PromoteApp(app_id="", manifest_stem="brief"), "team-bot-a")
    assert not result.ok
    assert result.details["reason"] == "no_app_id"
    assert "app_id" not in read("brief")


def test_a_missing_manifest_is_refused_not_created(bot_manifests):
    _write, _read = bot_manifests
    result = _applier().apply(
        PromoteApp(app_id="ghost", manifest_stem="ghost"), "team-bot-a"
    )
    assert not result.ok
    assert result.details["reason"] == "manifest_missing"


def test_promotion_is_revertible(bot_manifests):
    """MUTATION CHECKED: making ``capture_snapshot`` return ``{}`` makes the
    revert delete the manifest instead of restoring it, and this goes red."""
    write, read = bot_manifests
    prior = {"name": "Morning Brief", "definition_status": "discovered",
             "draft_id": "draft-abc123456789"}
    write("morning-brief", dict(prior))
    applier = _applier()
    action = PromoteApp(app_id="morning-brief", manifest_stem="morning-brief")
    snapshot = applier.capture_snapshot(action, "team-bot-a")
    assert applier.apply(action, "team-bot-a").ok
    assert read("morning-brief")["definition_status"] == "defined"

    revert = applier.revert(snapshot, "team-bot-a")
    assert revert.ok, revert.message
    assert read("morning-brief") == prior


def test_promotion_is_idempotent(bot_manifests):
    """Applying twice must not corrupt or re-identify."""
    write, read = bot_manifests
    write("morning-brief", {"name": "Morning Brief", "definition_status": "discovered"})
    action = PromoteApp(app_id="morning-brief", manifest_stem="morning-brief")
    assert _applier().apply(action, "team-bot-a").ok
    first = read("morning-brief")
    second_result = _applier().apply(action, "team-bot-a")
    assert second_result.ok, second_result.message
    second = read("morning-brief")
    assert second["app_id"] == first["app_id"] == "morning-brief"
    assert second["definition_status"] == "defined"


def test_promotion_uses_the_same_mutation_as_the_operator_ui(bot_manifests):
    """One mutation, two surfaces (see the applier's module docstring).

    Both the UI route and this applier call
    ``coherence_actions.promote_to_defined``; a manifest promoted either way
    must be indistinguishable apart from the ``by`` stamp.

    MUTATION CHECKED: replacing the ``promote_to_defined`` call with a bare
    ``manifest["definition_status"] = "defined"`` drops the provenance stamp and
    this goes red.
    """
    from evolve_admin.applications.coherence_actions import promote_to_defined

    write, read = bot_manifests
    write("morning-brief", {"name": "Morning Brief", "definition_status": "discovered"})
    assert _applier().apply(
        PromoteApp(app_id="morning-brief", manifest_stem="morning-brief"), "team-bot-a"
    ).ok
    via_applier = read("morning-brief")

    via_ui = {"name": "Morning Brief", "definition_status": "discovered",
              "app_id": "morning-brief"}
    promote_to_defined(via_ui, by="ui:operator")

    assert via_applier["definition_status"] == via_ui["definition_status"]
    assert "last_promoted_at" in via_applier.get("provenance", {})
    assert via_applier["provenance"]["last_promoted_by"] == "proposal:app_promotion"


def test_revert_when_no_manifest_existed_before_deletes_the_file(bot_manifests):
    """The seventh uncovered branch: ``if path.exists():`` in ``revert``.

    Reverting a promotion whose snapshot recorded *no prior manifest* must
    delete the file the apply created — and must not raise when it is already
    gone. Both halves of the guard, neither of which any test entered.

    MUTATION CHECKED: inverting ``if path.exists():`` makes this go red — the
    file survives a revert that was supposed to remove it.
    """
    write, _read = bot_manifests
    path = write("ghost", {"name": "Ghost", "definition_status": "discovered"})
    applier = _applier()
    action = PromoteApp(app_id="ghost-app", manifest_stem="ghost")

    # A snapshot taken as if nothing had been on disk.
    snapshot = {"action_kind": "PromoteApp", "path": str(path), "prior_manifest": None}
    assert applier.apply(action, "team-bot-a").ok
    assert path.exists()

    result = applier.revert(snapshot, "team-bot-a")
    assert result.ok, result.message
    assert not path.exists()

    # Idempotent: reverting again must not raise on the already-absent file.
    assert applier.revert(snapshot, "team-bot-a").ok
