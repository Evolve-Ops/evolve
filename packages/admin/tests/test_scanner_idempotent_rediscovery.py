"""Idempotent scanner re-discovery — apps-idempotent-rediscovery chip.

The orphan engine this guards (spec-app-identity-ledger §2, §4.B): when the
scanner re-discovers an app that is effectively already installed but the
identity matcher (``_match_detected_to_existing``) missed it (drifted id/name,
shifted evidence), the app falls into ``new_apps`` and the mint path stamps a
BRAND-NEW ``p-`` spec_id. The prior spec_id is stranded and every workspace
file still carrying it in its ``_evolve`` marker becomes an orphan.

These tests pin the two-part fix:

  1. ``scanner._rediscovery_match`` recognizes the already-installed Instance by
     file-overlap and the reuse branch binds the freshly-generated manifest to
     that Instance's live spec_id — no new ``p-``, no second manifest, learned
     state preserved (idempotent re-discovery).
  2. ``native_write.mint_scanner_detection`` now records supersession: a genuine
     re-spec (same file, different conformant spec_id) carries the retired id
     into ``provenance.prior_spec_ids[]`` so it still resolves (spec §6 invariant
     "a spec rebuild must carry the prior spec_id").
  3. A truly new app (no overlap) still mints a fresh id, unchanged.

Placeholder bot name ``team_bot_a`` per the public-launch scrub guard.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import native_write as nw  # noqa: E402
from evolve_admin.applications import scanner  # noqa: E402
from evolve_admin.applications.manifest import MANIFEST_SHAPE_V7_ARC  # noqa: E402
from evolve_admin.applications.spec_lineage import (  # noqa: E402
    prior_spec_ids,
    resolve_spec,
)

BOT = "team_bot_a"

_P_RE = re.compile(r"^p-[a-f0-9]{8}$")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """shared_dir + a per-bot manifests dir, with applications_dir patched."""
    shared = tmp_path / "shared"
    shared.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()

    import evolve_admin.applications.manifest as _mf

    monkeypatch.setattr(
        _mf, "applications_dir", lambda _sd, _bid: manifests, raising=True,
    )
    # The file-index rebuild walks real bot homes — stub it out.
    monkeypatch.setattr(
        _mf, "_rebuild_file_index_for", lambda _sd: None, raising=True,
    )
    return SimpleNamespace(shared=shared, manifests=manifests)


def _write_instance(
    manifests: Path,
    *,
    stem: str,
    spec_id: str,
    realized: list[str],
    learned: dict | None = None,
    installed_at: str = "2026-01-01T00:00:00Z",
    extra: dict | None = None,
) -> Path:
    """Write a minimal v7-arc Instance the index + mint can read back."""
    prov = {
        "spec_id": spec_id,
        "spec_version": "2026.01.01-1.0",
        "installed_by": "scanner",
        "installed_at": installed_at,
        "source_pod_id": None,
        "forked_from": None,
    }
    inst = {
        "id": None,
        "instance_id": stem,
        "manifest_shape": MANIFEST_SHAPE_V7_ARC,
        "provenance": prov,
        "realized_files": [{"path": p, "file_id": f"f-{i:08x}"}
                           for i, p in enumerate(realized)],
        "learned_config": learned or {},
    }
    if extra:
        inst.update(extra)
    path = manifests / f"{stem}.json"
    path.write_text(json.dumps(inst))
    return path


def _scan_manifest(
    *, app_id: str, evidence: list[str], pkg_id: str = "",
    name: str = "Found App", objective: str = "",
) -> dict:
    """A freshly-generated (never-persisted) scanner manifest dict."""
    m = {
        "id": app_id,
        "name": name,
        "bot_id": BOT,
        "status": "active",
        "created_at": "2026-06-27T00:00:00Z",
        "evidence_files": list(evidence),
        "build_spec": "",
        "constraints": {},
        "usage": {"model": "user-initiated"},
    }
    if pkg_id:
        m["pkg_id"] = pkg_id
    if objective:
        m["objective"] = objective
    return m


def _apply_rediscovery_and_mint(env, manifest: dict, existing_index: list[dict]):
    """Replicate ``scanner._gen_and_save``'s reuse branch end-to-end."""
    reuse = scanner._rediscovery_match(manifest, existing_index, claimed=set())
    if reuse is not None:
        manifest["pkg_id"] = reuse["data"]["provenance"]["spec_id"]
        manifest["id"] = reuse["path"].stem
    mint = nw.mint_scanner_detection(
        manifest,
        shared_dir=env.shared,
        bot_id=BOT,
        caps_dir=env.manifests,
        installed_by="scanner_rediscovery" if reuse is not None else "scanner",
        preserve_instance_state_from_disk=reuse is not None,
    )
    return reuse, mint


# ── 1. Idempotent re-discovery: reuse the existing spec_id ────────────────────

class TestReuseOnRediscovery:
    def test_reuses_spec_id_no_new_mint_no_second_manifest(self, env):
        # Installed: a v7-arc Instance under id i-existing, bound to p-aaaa0001,
        # owning two files, with accumulated learned state.
        _write_instance(
            env.manifests, stem="i-existing", spec_id="p-aaaa0001",
            realized=["scripts/foo.py", "scripts/bar.py"],
            learned={"tuned": "value"},
        )
        before = {p.name for p in env.manifests.glob("*.json")}

        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        # Re-discovery: same files, but the LLM drifted the id/name and minted
        # no pkg_id — exactly the case the identity matcher misses.
        manifest = _scan_manifest(
            app_id="app-found-v2",
            evidence=["scripts/foo.py", "scripts/bar.py"],
            name="Found V2",
        )
        reuse, mint = _apply_rediscovery_and_mint(env, manifest, index)

        assert reuse is not None, "should recognize the installed instance"
        assert mint.succeeded, mint.errors
        # The existing spec_id is reused — no fresh p- minted.
        assert mint.spec_id == "p-aaaa0001"

        # No SECOND manifest file: app-found-v2.json was never written; the
        # existing instance file is the only one.
        after = {p.name for p in env.manifests.glob("*.json")}
        assert after == before == {"i-existing.json"}

        # The instance was updated in place and its learned state + original
        # install moment survived the re-mint.
        inst = json.loads((env.manifests / "i-existing.json").read_text())
        assert inst["provenance"]["spec_id"] == "p-aaaa0001"
        assert inst["learned_config"] == {"tuned": "value"}
        assert inst["provenance"]["installed_at"] == "2026-01-01T00:00:00Z"
        # Reusing an existing spec_id supersedes nothing.
        assert prior_spec_ids(inst) == []

    def test_partial_overlap_above_floor_still_reuses(self, env):
        # Evidence drifted but still shares the majority of the footprint.
        _write_instance(
            env.manifests, stem="i-existing", spec_id="p-bbbb0002",
            realized=["a.py", "b.py", "c.py", "d.py"],
        )
        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        manifest = _scan_manifest(
            app_id="redetected",
            evidence=["a.py", "b.py", "c.py"],  # 3/3 of the smaller set
        )
        reuse, mint = _apply_rediscovery_and_mint(env, manifest, index)
        assert reuse is not None
        assert mint.spec_id == "p-bbbb0002"


# ── 2. Genuine re-spec: mint new + carry the retired id forward ───────────────

class TestGenuineRespecSupersedes:
    def test_new_id_minted_old_id_in_prior_chain_and_resolvable(self, env):
        # Installed instance bound to the OLD spec_id at the SAME stem.
        path = _write_instance(
            env.manifests, stem="journal", spec_id="p-01d00000",
            realized=["scripts/journal.py"],
        )
        # A re-scan whose manifest carries a DIFFERENT conformant pkg_id —
        # a structurally different spec replacing the app at the same file.
        manifest = _scan_manifest(
            app_id="journal", evidence=["scripts/journal.py"],
            pkg_id="p-ce000000",
        )
        mint = nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
        )
        assert mint.succeeded, mint.errors
        assert mint.spec_id == "p-ce000000"

        inst = json.loads(path.read_text())
        assert inst["provenance"]["spec_id"] == "p-ce000000"
        # The retired id is carried forward, not orphaned …
        assert prior_spec_ids(inst) == ["p-01d00000"]
        # … and a workspace marker still pointing at p-01d00000 resolves here.
        assert resolve_spec("p-01d00000", [inst]) is inst
        assert resolve_spec("p-ce000000", [inst]) is inst


# ── 3. Truly new app: unchanged fresh-mint behavior ──────────────────────────

class TestTrulyNewStillMintsFresh:
    def test_no_overlap_mints_fresh_id_and_new_file(self, env):
        _write_instance(
            env.manifests, stem="i-existing", spec_id="p-cccc0003",
            realized=["scripts/foo.py"],
        )
        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        manifest = _scan_manifest(
            app_id="brand-new",
            evidence=["scripts/unrelated.py"],  # no overlap
        )
        reuse, mint = _apply_rediscovery_and_mint(env, manifest, index)

        assert reuse is None, "no overlap → not a re-discovery"
        assert mint.succeeded, mint.errors
        # A fresh p- was minted, distinct from the installed instance's id.
        assert _P_RE.match(mint.spec_id)
        assert mint.spec_id != "p-cccc0003"
        # A new manifest file landed under the detected id.
        assert (env.manifests / "brand-new.json").is_file()


# ── 4. Conservatism guards on the match helper ───────────────────────────────

class TestMatchConservatism:
    def test_distinct_characterized_apps_are_not_merged(self, env):
        # Two established apps that share files BUT are clearly different —
        # the Atlas shared-substrate failure class. Must NOT reuse.
        _write_instance(
            env.manifests, stem="i-capture", spec_id="p-dddd0004",
            realized=["scripts/lib/shared.py", "data/index.json"],
            extra={"name": "Article Capture",
                   "objective": "Capture and archive articles."},
        )
        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        manifest = _scan_manifest(
            app_id="daily-digest",
            evidence=["scripts/lib/shared.py", "data/index.json"],
            name="Daily Digest",
            objective="Summarize the day's activity each morning.",
        )
        reuse = scanner._rediscovery_match(manifest, index, claimed=set())
        assert reuse is None

    def test_claimed_instance_is_skipped(self, env):
        path = _write_instance(
            env.manifests, stem="i-existing", spec_id="p-eeee0005",
            realized=["scripts/foo.py"],
        )
        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        manifest = _scan_manifest(
            app_id="redetected", evidence=["scripts/foo.py"],
        )
        # Unclaimed → matches.
        assert scanner._rediscovery_match(
            manifest, index, claimed=set()) is not None
        # Already claimed by another detected app → no double-binding.
        assert scanner._rediscovery_match(
            manifest, index, claimed={path}) is None

    def test_legacy_manifest_without_spec_id_is_not_a_target(self, env):
        # A legacy (non-v7-arc) manifest carries no provenance.spec_id, so it
        # offers nothing to reuse — re-discovery must skip it and mint fresh.
        (env.manifests / "legacy-app.json").write_text(json.dumps({
            "id": "legacy-app", "name": "Legacy App",
            "files": [{"path": "scripts/foo.py", "file_id": "f-1"}],
            "evidence_files": ["scripts/foo.py"],
        }))
        index = scanner._build_existing_manifest_index(env.manifests, env.shared)
        manifest = _scan_manifest(
            app_id="redetected", evidence=["scripts/foo.py"],
        )
        assert scanner._rediscovery_match(manifest, index, claimed=set()) is None


# ── 5. The "never" shield survives a re-mint (AL-1.7, #3734 round-4 D1) ───────


class TestNeverShieldSurvivesRediscovery:
    """``do_not_offer`` is design §7.2's "never": the user was asked whether to
    promote this draft and said never ask again.

    **This class exists because AL-1.7 asserted the scanner kept this field in
    four places — including the sentence the bot speaks to the user ("not just
    for now") — and none of them had ever been run.** The round-4 independent
    review of #3734 measured the truth on this very harness: the shield was
    lost on the first re-mint, because ``mint_scanner_detection`` rebuilds the
    Instance from scratch and its carry-forward allowlist did not include it.

    Why it matters more than a dropped field usually would: the consequence is
    design §10's *named worst outcome* — the user is asked again having said
    never — and nothing else backstops it, since the cadence cap is per-day and
    a rejected proposal is not a permanent gate. 30 of the 31 manifests
    readable on the operator's pod are v7-arc, i.e. exactly this path.
    """

    def test_do_not_offer_survives_a_rediscovery_remint(self, env):
        """MUTATION CHECKED: removing the ``DO_NOT_OFFER_FIELD`` carry-forward
        block from ``native_write.mint_scanner_detection`` makes this go red —
        which is the state the review found."""
        from evolve_admin.evo.wizard.promote_handlers import apply_never_shield

        path = _write_instance(
            env.manifests, stem="brief", spec_id="p-01d00000",
            realized=["scripts/brief.py"],
        )
        # The user says "never" — exactly what engine._apply_promotion_never does.
        on_disk = json.loads(path.read_text())
        apply_never_shield(on_disk)
        path.write_text(json.dumps(on_disk))

        # A later scan re-discovers the same app and re-mints over it — the
        # branch this whole module exists for.
        manifest = _scan_manifest(
            app_id="brief", evidence=["scripts/brief.py"], pkg_id="p-01d00000",
        )
        mint = nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
            preserve_instance_state_from_disk=True,
        )
        assert mint.succeeded, mint.errors

        after = json.loads(path.read_text())
        assert after.get("do_not_offer") is True, "SHIELD LOST ACROSS RE-SCAN"
        assert after.get("do_not_offer_by")

    def test_the_shield_survives_even_a_respec(self, env):
        """A re-spec rebuilds the Instance against a DIFFERENT spec_id and does
        not opt into state preservation. The user's refusal must still hold —
        the app being re-specced is not the user changing their mind.
        """
        from evolve_admin.evo.wizard.promote_handlers import apply_never_shield

        path = _write_instance(
            env.manifests, stem="journal", spec_id="p-01d00000",
            realized=["scripts/journal.py"],
        )
        on_disk = json.loads(path.read_text())
        apply_never_shield(on_disk)
        path.write_text(json.dumps(on_disk))

        manifest = _scan_manifest(
            app_id="journal", evidence=["scripts/journal.py"], pkg_id="p-ce000000",
        )
        assert nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
        ).succeeded
        assert json.loads(path.read_text()).get("do_not_offer") is True

    def test_the_shield_is_not_invented_where_the_user_never_said_never(self, env):
        """Carry-forward must not fabricate a refusal.

        MUTATION CHECKED: carrying the field unconditionally (dropping the
        ``not in (None, "", False)`` test) makes this go red — and would shield
        drafts nobody ever refused.
        """
        path = _write_instance(
            env.manifests, stem="quiet", spec_id="p-01d00000",
            realized=["scripts/quiet.py"],
        )
        manifest = _scan_manifest(
            app_id="quiet", evidence=["scripts/quiet.py"], pkg_id="p-01d00000",
        )
        assert nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
            preserve_instance_state_from_disk=True,
        ).succeeded
        assert "do_not_offer" not in json.loads(path.read_text())

    def test_a_live_snooze_survives_a_rediscovery_remint(self, env):
        """The OTHER answer the same gate reads (AL-1.7, 2026-08-21).

        Rounds 4-5 of #3734 proved the ``do_not_offer`` carry by execution and
        fixed it — and left ``promotion_snoozed_until`` on the broken path it
        had just been proved on, one field along in the same block. The gate
        reads both; only one survived a re-mint.

        The consequence is smaller than a lost "never" and it is not nothing:
        the only other brake is the 24h cadence cap, so a user who said "not for
        a month" is asked again the next day the scanner re-mints their draft —
        design §10's "promotion offers annoy" arriving through the mitigation
        meant to prevent it.

        MUTATION CHECKED, run in this session: dropping ``SNOOZE_UNTIL_FIELD``
        from ``native_write``'s carried tuple makes this go red.
        """
        path = _write_instance(
            env.manifests, stem="digest", spec_id="p-01d00000",
            realized=["scripts/digest.py"],
        )
        on_disk = json.loads(path.read_text())
        on_disk["promotion_snoozed_until"] = "2026-09-30T00:00:00Z"
        path.write_text(json.dumps(on_disk))

        manifest = _scan_manifest(
            app_id="digest", evidence=["scripts/digest.py"], pkg_id="p-01d00000",
        )
        assert nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
            preserve_instance_state_from_disk=True,
        ).succeeded

        assert json.loads(path.read_text()).get(
            "promotion_snoozed_until"
        ) == "2026-09-30T00:00:00Z", "SNOOZE LOST ACROSS RE-SCAN"

    def test_a_snooze_is_not_invented_where_the_user_never_deferred(self, env):
        """Carry-forward copies an answer; it must not manufacture one.

        MUTATION CHECKED: carrying unconditionally makes this go red — every
        re-minted draft would come back carrying a snooze nobody asked for,
        which suppresses an offer rather than merely delaying it.
        """
        path = _write_instance(
            env.manifests, stem="ledger", spec_id="p-01d00000",
            realized=["scripts/ledger.py"],
        )
        manifest = _scan_manifest(
            app_id="ledger", evidence=["scripts/ledger.py"], pkg_id="p-01d00000",
        )
        assert nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
            preserve_instance_state_from_disk=True,
        ).succeeded
        assert "promotion_snoozed_until" not in json.loads(path.read_text())

    def test_the_carried_field_names_match_the_canonical_constants(self):
        """The literals in ``native_write`` must equal the ones the gate reads.

        They are literals rather than an import (``app_promotion`` reaches the
        manifest layer, so importing it back would be circular), which is
        exactly the shape that drifts silently.

        MUTATION CHECKED: changing either literal makes this go red.
        """
        from evolve_admin.applications import app_promotion as ap
        from evolve_admin.applications import native_write as nwmod

        assert nwmod.DO_NOT_OFFER_FIELD == ap.DO_NOT_OFFER_FIELD
        assert nwmod.DO_NOT_OFFER_BY_FIELD == ap.DO_NOT_OFFER_BY_FIELD
        assert nwmod.SNOOZE_UNTIL_FIELD == ap.SNOOZE_UNTIL_FIELD

    def test_the_shield_survives_the_legacy_to_v7_arc_upgrade(self, env):
        """Round 5's E1: the gap was in THIS chip's own file, not ``scanner.py``.

        ``_read_instance_on_disk`` gates on ``manifest_shape == "v7-arc"`` —
        correct for ``learned_config`` and install Provenance, which only exist
        on a v7-arc Instance. But the ordinary **upgrade** reads a LEGACY file
        and writes a v7-arc one, so a v7-arc-gated read drops the user's refusal
        on exactly the manifests that have not been upgraded yet.

        AL-1.7's first cut of the carry-forward sat behind that gate and was
        defended as "the legacy path is `scanner.py`, out of scope (§5)". Half
        of it was; this half was twelve lines above the block itself. The pod's
        one legacy manifest is a live *discovered* draft — the population
        promotion offers over.

        MUTATION CHECKED: reading the decision fields from
        ``_read_instance_on_disk`` instead of ``_read_any_manifest_on_disk``
        makes this go red.
        """
        from evolve_admin.evo.wizard.promote_handlers import apply_never_shield

        # A LEGACY-shaped file on disk — no manifest_shape tag at all.
        path = env.manifests / "legacy-brief.json"
        legacy = {
            "id": "legacy-brief",
            "name": "Legacy Brief",
            "bot_id": BOT,
            "status": "active",
        }
        apply_never_shield(legacy)
        path.write_text(json.dumps(legacy))

        manifest = _scan_manifest(
            app_id="legacy-brief", evidence=["scripts/legacy_brief.py"],
        )
        assert nw.mint_scanner_detection(
            manifest, shared_dir=env.shared, bot_id=BOT, caps_dir=env.manifests,
            preserve_instance_state_from_disk=True,
        ).succeeded

        after = json.loads(path.read_text())
        assert after.get("manifest_shape") == "v7-arc", "the upgrade must have run"
        assert after.get("do_not_offer") is True, "SHIELD LOST ON LEGACY UPGRADE"
        assert after.get("do_not_offer_by")
