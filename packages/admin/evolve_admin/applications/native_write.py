"""
Native v7-arc writes — manifest-v7 Slice 3a (the native-write cutover).

Spec: docs/spec-manifest-v7-slicing-2026-06-10.md §5.1 + §6.1; artifact
shapes per docs/spec-manifest-v7-2026-05-20.md §3 (Spec), §4 (Instance),
§5 (Provenance).

Until this slice, only the one-shot migration minted v7-arc artifacts —
every NEW app (Forge build, scanner detection) was born legacy-shaped,
growing the migration backlog daily (the "dual-shape tax", slicing spec
§6.1). This module flips the default for new artifacts:

  * Forge installs: ``convert_completed_install_to_v7_arc`` runs at the
    end of ``approve_forge_job`` for fresh install jobs. The build
    pipeline stays legacy-shaped in flight (forge's mid-build
    ``save_manifest`` calls would otherwise fatten a v7-arc Instance
    with Spec-owned fields on every save); the moment the install
    completes, the manifest is split into Spec + Instance + Provenance.
  * Scanner detections: ``mint_scanner_detection`` converts the freshly
    generated manifest dict before it ever lands on disk.

Design rules (deliberate, please keep):

  * **Never fork the shapes.** Spec/Instance extraction is migrate_v7's
    ``_extract_spec`` / ``_extract_instance`` — the same code the
    migration runs, parameterized by spec_version/installed_by. Privacy
    and audience_scoping defaults come from privacy_scoping_validator
    through that path (one source of truth).
  * **Per-instance privacy answers live on the Instance.** The add-bot
    wizard's consent answers (``privacy_seed`` / ``audience_scoping_seed``
    in job.context_snapshot) are merged into the completed v24 manifest by
    forge step 1 — they are what THIS bot's audience was told, not facts
    about the app. The conversion carries ``privacy`` / ``audience_scoping``
    onto the Instance when the completed manifest's block differs from the
    bound/minted Spec's (unless it's just the unseeded auto-stamp of the
    canonical defaults), and keeps seeded answers out of freshly minted
    (shared, cross-pod-exportable) Specs. Hydration prefers instance-local
    blocks, so every manifest.privacy reader sees them.
  * **instance_id == the legacy app id.** The Instance file stays at
    ``manifests/<app_id>.json`` and ``hydrate_v7_arc_instance`` derives
    the UI ``id`` from instance_id, so URLs, ``load_manifest`` keys,
    forge job app_ids, and the file index all stay stable across the
    flip. (Migration minted ``i-<hex>`` ids because it renamed files
    wholesale; native writes must not break live references.)
  * **Conversion failure is never fatal.** A legacy-shaped manifest is
    valid by construction (consumers branch on ``manifest_shape``), so
    every caller treats conversion as best-effort-but-loud: log, keep
    the legacy artifact, let the next migrate_v7 run pick it up.
  * **Gateway visibility.** Instance writes use same-dir temp+rename
    (the gateway plugin's trigger cache keys on the manifests-directory
    mtime; in-place writes and sudo-cp-over are invisible to it).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json
from evolve_util import now_iso as _now_iso

from .app_identity import APP_ID_FIELD, DRAFT_ID_FIELD
from .migrate_v7 import (
    CANONICAL_VERSION_RE as _SPEC_VERSION_FILE_RE,
    MigrationResult,
    _empty_lessons_stub,
    _extract_instance,
    _extract_spec,
    _new_instance_id,
    _resolve_spec_id,
    _write_json_mkdirs,
    version_sort_key as _spec_version_sort_key,
)

#: Design §7.2's "never" shield and its actor stamp. Literals rather than an
#: import from ``app_promotion`` — that module imports the manifest layer, so
#: importing it back here would be circular. Pinned to the canonical constants
#: by ``TestNeverShieldSurvivesRediscovery``, because a duplicated literal is
#: exactly the shape that drifts silently.
DO_NOT_OFFER_FIELD = "do_not_offer"
DO_NOT_OFFER_BY_FIELD = "do_not_offer_by"

#: Design §7.2's *other* user decision: "not now, ask me again in N days".
#: Written by ``engine._apply_promotion_snooze``, read by
#: ``app_promotion.evaluate_offer`` as check 2 of 5 — and, until 2026-08-21,
#: carried forward by nothing. The 2026-08-19 rounds fixed the ``never`` shield
#: and left the snooze on the same broken path it had just been proved on: a
#: re-mint rebuilt the Instance without it, so a user who said "not for a month"
#: was re-offered as soon as the 24h cadence window rolled over and a rescan
#: touched the draft. Same class, same file, one field along.
SNOOZE_UNTIL_FIELD = "promotion_snoozed_until"


# Instance-owned fields the migration drops (deliberate watermark for
# pre-migration history) but a *fresh* app must keep:
#   usage               — the bot-facing usage manual (INSTALLED_APPS.md
#                         discoverability reads it; per-bot, so Instance)
#   improvement_history — the just-completed install record (the migration
#                         dropped months of history as a watermark; native
#                         writes have exactly one entry and it's the
#                         provenance of this very install)
#   app_files_privacy / data_paths / default_for_unclassified
#                       — v15 backup classification; bot-local paths,
#                         so Instance-owned
_NATIVE_INSTANCE_PASSTHROUGH: tuple[str, ...] = (
    "usage",
    "improvement_history",
    "app_files_privacy",
    "data_paths",
    "default_for_unclassified",
    # v25 purpose/fit classification (app-scan bite D1). The scanner's mint
    # gate stamps these on the legacy-shaped dict before this split; carry them
    # onto the Instance so a v7-arc native mint keeps the label. (The Instance
    # is what the per-bot scanner reads/writes and the Apps grid loads.) Both
    # are guarded by `if value:` below, so an unstamped mint is unaffected.
    "app_kind",
    "classification",
    # v27 definition_status (Defined/Discovered axis). Per-bot source-of-truth
    # state, so Instance-owned — the L3 archival shield and reconciliation read
    # it off the Instance. A fresh authored mint born "defined" must keep that
    # vouch through the v7-arc split. Always a truthy enum value, so the
    # `if value:` guard below never drops it.
    "definition_status",
    # Conversation-only evidence (design §7.1a, AL-1.6c wiring). Per-bot
    # OBSERVED state — the recurrence was measured on this bot's own
    # annotations — so it is Instance-owned, like definition_status. Without
    # the passthrough the native v7-arc mint drops both fields and the draft
    # lands on disk with no evidence at all, which app_readiness scores 0
    # rather than 0.2: a conversation draft would be indistinguishable from
    # an empty stub on the very path that mints it.
    "conversation_only",
    "conversation_evidence",
)

def _per_instance_privacy_overlays(
    legacy: dict,
    spec: Optional[dict],
    privacy_seed: Optional[dict],
    audience_scoping_seed: Optional[dict],
) -> dict:
    """The v24 blocks the Instance must carry itself: ``privacy`` /
    ``audience_scoping`` blocks on the completed manifest that the bound
    (or freshly minted) Spec does not reproduce verbatim.

    Hydration (``hydrate_v7_arc_instance``) fills these blocks from the
    Spec only when the Instance lacks them, so an Instance-local block is
    authoritative for every reader. Two kinds of block stay OFF the
    Instance so the Spec remains the live source for app-level defaults:
    blocks identical to the Spec's, and unseeded blocks equal to the
    canonical defaults — those are forge's auto-stamp ("nothing
    declared"), and pinning one would permanently mask an authored Spec
    block (e.g. a prose-derived consent_notice) at hydration. A
    conversation seed makes the block instance data regardless of its
    content. ``spec=None`` (bound Spec unreadable) is treated as
    differs-from-Spec: preserving the monolith's boundary beats
    deduplicating against a Spec we couldn't read.
    """
    from .privacy_scoping_validator import (
        default_audience_scoping_block,
        default_privacy_block,
    )

    overlays: dict = {}
    for key, seed, default_block in (
        ("privacy", privacy_seed, default_privacy_block),
        ("audience_scoping", audience_scoping_seed, default_audience_scoping_block),
    ):
        block = legacy.get(key)
        if not isinstance(block, dict) or not block:
            continue
        if block == (spec or {}).get(key):
            continue
        if not (isinstance(seed, dict) and seed) and block == default_block():
            continue
        overlays[key] = block
    return overlays


def _scrub_seeded_answers_from_spec(
    spec: dict,
    legacy: dict,
    privacy_seed: Optional[dict],
    audience_scoping_seed: Optional[dict],
) -> None:
    """Reset conversation-seeded fields in a freshly minted Spec to the
    canonical defaults.

    ``_extract_spec`` preserves a declared v24 block verbatim — correct
    for operator/Critique-authored blocks, but the add-bot wizard's
    consent answers were merged into the manifest by forge step 1 and a
    Spec lands in ``gallery/local/`` where sibling installs bind to it
    and cross-pod export ships it. Only fields the seed actually LANDED
    on are reset — ``seed_privacy_scoping_defaults`` is fill-if-missing,
    so a gallery-declared block beat the seed at install time and must
    reach the Spec untouched. The per-instance values themselves are
    preserved by the Instance overlay.
    """
    from .privacy_scoping_validator import (
        PRIVACY_KEYS,
        default_audience_scoping_block,
        default_privacy_block,
    )

    if isinstance(privacy_seed, dict) and privacy_seed:
        legacy_privacy = legacy.get("privacy")
        landed = isinstance(legacy_privacy, dict) and all(
            legacy_privacy.get(key) == value
            for key, value in privacy_seed.items()
            if key in PRIVACY_KEYS
        )
        spec_privacy = spec.get("privacy")
        if landed and isinstance(spec_privacy, dict):
            # Copy before mutating — _infer_privacy returns a DECLARED
            # block by reference, and the legacy dict must keep the
            # seeded values for the Instance overlay diff.
            spec_privacy = dict(spec_privacy)
            defaults = default_privacy_block()
            for key in privacy_seed:
                if key not in PRIVACY_KEYS:
                    continue
                if key in defaults:
                    spec_privacy[key] = defaults[key]
                else:
                    spec_privacy.pop(key, None)
            spec["privacy"] = spec_privacy
    if isinstance(audience_scoping_seed, dict) and audience_scoping_seed:
        # The seed replaces the default block wholesale, so it landed
        # iff the manifest's block IS the seed.
        if legacy.get("audience_scoping") == audience_scoping_seed:
            spec["audience_scoping"] = default_audience_scoping_block()


def mint_spec_version(now: Optional[datetime] = None) -> str:
    """Mint an install-date spec_version: ``YYYY.MM.DD-1.0``.

    Canonical format per base-spec §3 ("date-prefixed semver"); the
    migration's fixed watermark (INITIAL_SPEC_VERSION) is the same shape
    pinned to the design date.
    """
    dt = now or datetime.now(timezone.utc)
    return f"{dt.strftime('%Y.%m.%d')}-1.0"


def find_existing_spec(
    shared_dir: Path, spec_id: str,
) -> Optional[tuple[str, Path]]:
    """Locate the latest existing Spec for ``spec_id`` across gallery tiers.

    Searches the same tiers ``hydrate_v7_arc_instance`` resolves from:
    ``gallery/local/``, ``gallery/builtin/``, and every
    ``gallery/imported/<source_pod_id>/``. Returns ``(spec_version,
    spec_path)`` for the highest version found, or None.

    Used so a gallery install on a migrated pod *binds* to the already-
    promoted builtin Spec instead of minting a duplicate local one.
    """
    gallery_root = shared_dir / "gallery"
    spec_dirs = [
        gallery_root / "local" / spec_id,
        gallery_root / "builtin" / spec_id,
    ]
    imported_root = gallery_root / "imported"
    if imported_root.is_dir():
        for pod_dir in sorted(imported_root.iterdir()):
            # Skip the flat legacy imported-package files
            # ({shared_dir}/gallery/imported/<pkg_id>.json) — those are
            # gallery *packages*, not v7 Specs.
            if pod_dir.is_dir():
                spec_dirs.append(pod_dir / spec_id)

    best: Optional[tuple[str, Path]] = None
    for d in spec_dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            version = f.stem
            if not _SPEC_VERSION_FILE_RE.match(version):
                continue
            if best is None or (
                _spec_version_sort_key(version)
                > _spec_version_sort_key(best[0])
            ):
                best = (version, f)
    return best


def _bound_spec_id_on_disk(path: Path) -> tuple[str, list[str]]:
    """Read the spec_id the artifact currently at ``path`` is bound to.

    Returns ``(spec_id, prior_spec_ids)`` for whatever is on disk:
      - a v7-arc Instance → ``provenance.spec_id`` + its existing
        ``provenance.prior_spec_ids[]`` chain (so a re-spec extends, not
        truncates, the lineage),
      - a legacy single-file manifest → its top-level ``pkg_id`` (the
        legacy form of the spec_id; no chain).

    Returns ``("", [])`` when ``path`` is absent, unreadable, garbled, or
    carries no recognizable spec_id. Best-effort and never raises — a
    supersession breadcrumb is advisory, not load-bearing.
    """
    # identity: see resolve_app_id — reads the SPEC BINDING a file on disk is
    # currently pinned to (provenance.spec_id, else the legacy pkg_id that is
    # its pre-v7 form) so a re-spec can carry the retired id into
    # prior_spec_ids[]. A retired binding is exactly what resolve_app_id must
    # not be asked for.
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError: JSON/Unicode decode
        return "", []
    if not isinstance(data, dict):
        return "", []
    prov = data.get("provenance")
    # identity: see resolve_app_id — this resolves "which SPEC does this manifest
    # claim?", not "which app is this?". The two branches are the v7-arc binding
    # (``provenance.spec_id`` plus its ``prior_spec_ids`` chain) with the pre-v7
    # top-level ``pkg_id`` as fallback — the pairing ``spec_lineage`` owns. The
    # resolver would return an Instance's own id and lose the chain the second
    # return value carries.
    if isinstance(prov, dict) and isinstance(prov.get("spec_id"), str):
        sid = prov["spec_id"].strip()
        chain_raw = prov.get("prior_spec_ids")
        chain = (
            [s for s in chain_raw if isinstance(s, str) and s]
            if isinstance(chain_raw, list)
            else []
        )
        return sid, chain
    # identity: see resolve_app_id — the pre-v7 spec key, as above.
    pkg_id = data.get("pkg_id")
    if isinstance(pkg_id, str) and pkg_id.strip():
        return pkg_id.strip(), []
    return "", []


def _read_instance_on_disk(path: Path) -> Optional[dict]:
    """Return the v7-arc Instance dict currently at ``path``, or ``None``.

    Best-effort and never raises — used to carry an existing Instance's
    accumulated state forward on an idempotent update. Returns ``None`` when
    the file is absent, unreadable, garbled, not a dict, or not a v7-arc
    Instance (only a v7-arc Instance carries ``learned_config`` / Provenance
    worth preserving across a re-mint).
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError: JSON/Unicode decode
        return None
    if not isinstance(data, dict):
        return None
    if data.get("manifest_shape") != "v7-arc":
        return None
    return data


def _read_any_manifest_on_disk(path: Path) -> Optional[dict]:
    """The dict at ``path`` whatever its shape, or ``None``. Never raises.

    Deliberately NOT :func:`_read_instance_on_disk`, whose ``manifest_shape ==
    "v7-arc"`` gate is correct for what it carries (``learned_config`` and
    install Provenance exist only on a v7-arc Instance) and must not move.

    This one exists for the **user-decision** fields, where that gate is a bug:
    the ordinary legacy → v7-arc *upgrade* reads a legacy file and writes a
    v7-arc one, so gating the read on v7-arc means the upgrade silently drops
    whatever the user decided. Found by the round-5 independent review of
    #3734, which proved it by execution — the pod's one legacy manifest is a
    live discovered draft, i.e. exactly the population promotion offers over.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError: JSON/Unicode decode
        return None
    return data if isinstance(data, dict) else None



def _write_instance_json(
    path: Path, data: dict, result: MigrationResult,
) -> None:
    """Atomic same-dir temp+rename write of an Instance JSON.

    Falls back to manifest.py's /tmp-staging + sudo path when the
    manifests dir lacks the evolve write ACL (pre-first-scan bots). The
    fallback's cp-over is invisible to the gateway trigger cache — noted
    as a warning because on such pods *every* manifest write shares that
    limitation until the ACL lands, so this isn't a new exposure.
    """
    try:
        atomic_write_json(path, data, mode=0o644)
    except (PermissionError, FileNotFoundError):
        from .manifest import _write_manifest_bytes
        _write_manifest_bytes(
            path, json.dumps(data, indent=2).encode("utf-8"),
        )
        result.warnings.append(
            f"instance write fell back to sudo staging for {path} "
            "(no evolve write ACL yet; trigger-cache visibility limited "
            "until the next scan grants it)"
        )


def mint_v7_arc_app(
    legacy: dict,
    *,
    shared_dir: Path,
    bot_id: str,
    installed_by: str,
    instance_path: Path,
    privacy_seed: Optional[dict] = None,
    audience_scoping_seed: Optional[dict] = None,
    record_supersession: bool = False,
    preserve_instance_state_from_disk: bool = False,
    mint: bool = False,
) -> MigrationResult:
    """Mint Spec + v7-arc Instance + Provenance from a legacy-shaped dict.

    The core native write. ``legacy`` is a v13-shaped manifest dict (the
    final state of a completed Forge build, or a freshly generated
    scanner manifest). Behavior:

      1. spec_id := the legacy ``pkg_id`` when conformant (markers stamped
         ``spec=<pkg_id>`` during the build then match the Spec), else
         freshly minted.
      2. If a Spec for that id already exists in the gallery (builtin
         promoted by migration, or local from a sibling install), the
         Instance *binds* to its latest version — no duplicate Spec.
         Otherwise a new Spec is extracted and written to
         ``gallery/local/<spec_id>/<version>.json`` with privacy{} +
         audience_scoping{} seeded from the canonical defaults —
         conversation-seeded answers (``privacy_seed`` /
         ``audience_scoping_seed``, when the caller passes them) are
         reset to those defaults so per-bot consent wording never lands
         in the shared gallery layer.
      3. The Instance (instance_id == legacy id, Provenance per base-spec
         §5 with ``installed_at`` = now) replaces the file at
         ``instance_path`` via same-dir temp+rename. It carries any
         ``privacy`` / ``audience_scoping`` block the bound/minted Spec
         doesn't reproduce verbatim (the per-instance overlay hydration
         prefers over the Spec's).
      4. An empty Lessons stub is created unless one already exists.

    ``record_supersession`` (opt-in; default off): when True, read the
    spec_id the Instance currently at ``instance_path`` is bound to BEFORE
    overwriting it, and if it differs from the freshly-resolved ``spec_id``,
    carry the retired id (and any chain it already accumulated) forward into
    the new Instance's ``provenance.prior_spec_ids[]`` via
    ``spec_lineage.record_spec_supersession``. This is the re-spec breadcrumb
    so a workspace marker carrying the old id still resolves to the live app
    (``spec_lineage.resolve_spec``). The forge install/convert caller opts in,
    and so does the scanner mint (``mint_scanner_detection``): a re-scan that
    overwrites an instance file bound to a different conformant spec_id is a
    genuine re-spec and must carry the retired id forward, not drop it
    (apps-idempotent-rediscovery; spec §6 invariant "a spec rebuild must carry
    the prior spec_id").

    ``preserve_instance_state_from_disk`` (opt-in; default off): when True and
    the file already at ``instance_path`` is a v7-arc Instance, carry its
    accumulated ``learned_config`` and original ``provenance.installed_at``
    forward onto the freshly-built Instance. This is for idempotent scanner
    RE-DISCOVERY: when a re-scan is recognized as the same already-installed
    app (path-overlap match in ``scanner._gen_and_save``) it UPDATES the
    existing Instance in place — it must not reset learning the RSI loop
    accumulated, nor rewrite the original install moment to "now". A fresh
    mint (no prior instance on disk) is unaffected: the read finds nothing and
    the canonical fresh defaults stand.

    Returns a MigrationResult; ``result.errors`` non-empty means the
    legacy artifact was left in place (callers keep it — still valid).
    """
    result = MigrationResult(source_path=instance_path, dry_run=False)

    spec_id = _resolve_spec_id(legacy, result)
    # instance_id == the app id == the filename stem (the invariant that
    # keeps URLs/load_manifest keys stable). Fall back to the stem, never
    # a fresh i-<hex> — a minted id would desync from the filename and a
    # later save round-trip would write a second file.
    #
    # identity: see resolve_app_id — NOT swept (AL-1.4b): the legacy ``id``
    # is required specifically because it is the FILENAME STEM, and the
    # comment above is that invariant. ``resolve_app_id`` leads with
    # ``pkg_id``, which for a gallery-installed legacy manifest is
    # ``p-a3f91c8b`` while the file is ``app_task_manager.json`` — using it
    # would rename every gallery app's Instance and break exactly the
    # URL/``load_manifest``-key stability this line exists to hold. (It
    # would also collide with ``spec_id``, which for that same manifest IS
    # the ``pkg_id``: Instance and Spec would share one id.)
    instance_id = (
        (str(legacy.get("id") or "")).strip()
        or instance_path.stem
        or _new_instance_id()
    )
    result.spec_id = spec_id
    result.instance_id = instance_id

    existing = find_existing_spec(shared_dir, spec_id)
    spec: Optional[dict] = None        # freshly extracted → written below
    bound_spec: Optional[dict] = None  # read-only, for the Instance-overlay diff
    if existing is not None:
        spec_version, spec_path = existing
        try:
            loaded = json.loads(spec_path.read_text())
            bound_spec = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):  # ValueError: JSONDecodeError + UnicodeDecodeError
            bound_spec = None
    else:
        spec_version = mint_spec_version()
        spec = _extract_spec(legacy, spec_id, result, spec_version=spec_version)
        _scrub_seeded_answers_from_spec(
            spec, legacy, privacy_seed, audience_scoping_seed,
        )
        spec_path = (
            shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json"
        )
    result.spec_path = spec_path

    instance = _extract_instance(
        legacy, instance_id, spec_id, bot_id, result,
        spec_version=spec_version,
        installed_by=installed_by,
        history_reason="initial_install",
        mint=mint,
    )
    # Provenance records the *install* moment. The legacy created_at (which
    # _extract_instance defaults to) is when the draft manifest was minted —
    # minutes-to-hours before the build completed for wizard apps.
    instance["provenance"]["installed_at"] = _now_iso()
    # _extract_instance folds v13 `usage` into learned_config (migration
    # semantics: v10 usage was a free-form bag). A fresh app has learned
    # nothing — keep learned_config empty and carry the structured usage
    # manual through as itself (in _NATIVE_INSTANCE_PASSTHROUGH below).
    instance["learned_config"] = {}
    for key in _NATIVE_INSTANCE_PASSTHROUGH:
        value = legacy.get(key)
        if value:
            instance[key] = value
    # v24 privacy{} / audience_scoping{} the Spec won't reproduce — the
    # wizard's consent answers above all (add-bot M3 seeds, merged into the
    # manifest by forge step 1). Without this the conversion drops them:
    # the consent_notice survives only in _history while every hydrated
    # reader (privacy_scoping_validator, audience-scoping enforcement, the
    # secondary wizard's consent display) sees the Spec's block or nothing.
    instance.update(_per_instance_privacy_overlays(
        legacy, spec if spec is not None else bound_spec,
        privacy_seed, audience_scoping_seed,
    ))

    # ── Update-in-place state preservation (opt-in) ──────────────────────────
    # An idempotent scanner re-discovery overwrites the existing Instance file
    # to refresh observed fields, but a fresh mint resets learned_config to {}
    # and installed_at to now. When this write is an UPDATE of an app that is
    # already installed, those resets would discard RSI-accumulated learning
    # and falsify the install moment, so carry them forward from disk.
    if preserve_instance_state_from_disk:
        prior = _read_instance_on_disk(instance_path)
        if prior is not None:
            learned = prior.get("learned_config")
            if isinstance(learned, dict) and learned:
                instance["learned_config"] = learned
            prior_prov = prior.get("provenance")
            if isinstance(prior_prov, dict):
                installed_at = prior_prov.get("installed_at")
                if isinstance(installed_at, str) and installed_at:
                    instance["provenance"]["installed_at"] = installed_at

    # ── Identity carry-forward (AL-1.4a) ─────────────────────────────────────
    # app_id is immutable once conferred, and a re-mint over an existing
    # Instance rebuilds the dict from scratch — so an app_id (or draft_id)
    # already on disk must survive the overwrite. Unconditional, unlike the
    # learned-state preservation above: identity must never churn, even on a
    # re-mint that deliberately resets everything else.
    prior_identity = _read_instance_on_disk(instance_path)
    if prior_identity is not None:
        for _field in (APP_ID_FIELD, DRAFT_ID_FIELD):
            prior_value = prior_identity.get(_field)
            if isinstance(prior_value, str) and prior_value.strip():
                instance[_field] = prior_value.strip()

        # ── User-decision carry-forward (AL-1.7) ─────────────────────────────
        # ``do_not_offer`` is design §7.2's "never": the user was asked whether
        # to promote this draft and said never ask again. Like identity, it is a
        # decision the SCANNER has no standing to revoke — a re-mint refreshes
        # what was observed, and "the user said no forever" is not an
        # observation.
        #
        # Unconditional, for the same reason the identity block above is: it
        # must survive a re-mint that deliberately resets everything else.
        # Without it the shield is lost on the very next re-discovery, and the
        # user is asked again having said never — design §10's named worst
        # outcome, and the whole reason the shield exists.
        #
        # Found by the round-4 independent review of #3734, which measured the
        # loss by execution on this repo's own rediscovery harness rather than
        # taking the claim at face value. Four places in this chip asserted the
        # scanner "keeps" this field, INCLUDING the sentence the bot speaks to
        # the user ("not just for now"), and none of them had been run. 30 of
        # the pod's 31 readable manifests are v7-arc, i.e. exactly this path.

    # The user-decision carry-forward reads SHAPE-AGNOSTICALLY, unlike the
    # identity block above. Round 5 proved why: the ordinary legacy → v7-arc
    # upgrade reads a legacy file, so a v7-arc-gated read drops the user's
    # refusal on exactly the manifests that have not been upgraded yet — the
    # 1-of-31 on the pod today, and a live discovered draft.
    prior_any = _read_any_manifest_on_disk(instance_path)
    if prior_any is not None:
        for _decision_field in (
            DO_NOT_OFFER_FIELD,
            DO_NOT_OFFER_BY_FIELD,
            # A live snooze is a user decision exactly as the shield is, and it
            # is read by the same gate. Carrying one and not the other made the
            # weaker answer ("later") less durable than the stronger one
            # ("never"), which is backwards: "later" is the answer a user
            # expects to be asked about again *eventually*, not tomorrow.
            SNOOZE_UNTIL_FIELD,
        ):
            prior_decision = prior_any.get(_decision_field)
            if prior_decision not in (None, "", False):
                instance[_decision_field] = prior_decision

    # ── Spec-id supersession breadcrumb (opt-in) ─────────────────────────────
    # If the Instance currently on disk at instance_path is bound to a
    # DIFFERENT spec_id than the one we're minting, this is a re-spec: carry
    # the retired id (and any chain it already accumulated) forward so a
    # workspace marker still pointing at the old id resolves to this Instance.
    if record_supersession:
        old_spec_id, old_chain = _bound_spec_id_on_disk(instance_path)
        if old_spec_id and old_spec_id != spec_id:
            from .spec_lineage import record_spec_supersession
            # Preserve the existing chain (oldest→newest), then the just-
            # retired binding, so the lineage is unbroken across re-specs.
            for sid in [*old_chain, old_spec_id]:
                record_spec_supersession(instance, sid)

    result.instance_path = instance_path
    lessons_path = shared_dir / "lessons" / bot_id / f"{spec_id}.json"
    result.lessons_path = lessons_path

    # ── Writes: Spec first (an Instance pointing at a missing Spec hydrates
    # to a bare stub), then Instance, then Lessons. A failure after the Spec
    # write leaves an unreferenced gallery file — harmless — and the legacy
    # manifest untouched.
    try:
        if spec is not None:
            _write_json_mkdirs(spec_path, spec)
        _write_instance_json(instance_path, instance, result)
        if not lessons_path.exists():
            _write_json_mkdirs(
                lessons_path,
                _empty_lessons_stub(
                    spec_id, bot_id, shared_dir, spec_version=spec_version,
                ),
            )
    except OSError as e:
        result.errors.append(f"native v7-arc write failed: {e}")
        return result

    return result


def convert_completed_install_to_v7_arc(
    job, shared_dir: Path,
) -> Optional[MigrationResult]:
    """Flip a just-completed Forge install's manifest to native v7-arc.

    Called at the END of ``approve_forge_job`` (after the improvement-
    history patch — the last legacy-shaped write in the approval flow)
    for ``job_type == "install"`` jobs. Returns None when there is
    nothing to convert: manifest missing/unreadable, or already v7-arc
    (re-install of a migrated app — its Instance shape is preserved by
    the load→hydrate→save flow upstream).

    Raises nothing by contract is NOT promised — callers wrap in
    try/except and log; a failed conversion leaves the valid legacy
    manifest in place (slicing spec §5: no flag day).
    """
    from .manifest import MANIFEST_SHAPE_V7_ARC, applications_dir

    path = applications_dir(shared_dir, job.bot_id) / f"{job.app_id}.json"
    if not path.is_file():
        return None
    try:
        legacy = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(legacy, dict):
        return None
    if legacy.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC:
        return None

    # Same snapshot fields forge step 1 merged into the manifest (add-bot
    # wizard consent answers) — passed down so the fresh-Spec path knows
    # which privacy fields are per-instance, not app facts.
    snapshot = getattr(job, "context_snapshot", None) or {}
    priv_seed = snapshot.get("privacy_seed") if isinstance(snapshot, dict) else None
    scope_seed = (
        snapshot.get("audience_scoping_seed") if isinstance(snapshot, dict) else None
    )

    result = mint_v7_arc_app(
        legacy,
        shared_dir=shared_dir,
        bot_id=job.bot_id,
        installed_by="forge_engine",
        instance_path=path,
        privacy_seed=priv_seed if isinstance(priv_seed, dict) else None,
        audience_scoping_seed=scope_seed if isinstance(scope_seed, dict) else None,
        # Forge re-create can rebuild an app under a fresh spec_id; preserve
        # the retired id in the new Instance's prior_spec_ids[] so its
        # workspace markers still resolve. (Scanner mints stay OFF — that's
        # the separate apps-idempotent-rediscovery chip.)
        record_supersession=True,
    )

    # The replaced file no longer carries a legacy files[] list — drop its
    # stale entries from the global file index the same way save_manifest
    # refreshes it. Deliberately NOT in mint_v7_arc_app: the scanner mints
    # from a ThreadPoolExecutor and a full-index rebuild per detection
    # would be wasted work (fresh detections were never in the index).
    if result.succeeded:
        try:
            from .manifest import _rebuild_file_index_for
            _rebuild_file_index_for(shared_dir)
        except Exception as e:  # noqa: BLE001 — index rebuild is advisory
            result.warnings.append(f"file-index rebuild failed (advisory): {e}")

    return result


def mint_scanner_detection(
    manifest_dict: dict,
    *,
    shared_dir: Path,
    bot_id: str,
    caps_dir: Path,
    installed_by: str = "scanner",
    preserve_instance_state_from_disk: bool = False,
) -> MigrationResult:
    """Native v7-arc mint for a scanner-discovered app.

    ``manifest_dict`` is the freshly generated v13-shaped dict from
    ``generate_manifest_for_app`` / ``_stub_manifest`` (it never touched
    disk in legacy form). Spec lands in ``gallery/local/``; the Instance
    lands at ``caps_dir/<id>.json`` — same filename the legacy write
    used, so dedup/match/index keep working on the stem.

    ``record_supersession`` is always ON here (apps-idempotent-rediscovery):
    if an instance already at that path is bound to a DIFFERENT conformant
    spec_id, this write is a genuine re-spec and carries the retired id into
    ``provenance.prior_spec_ids[]`` instead of orphaning it (spec §6). It is a
    no-op for the common case (no prior file, or the same spec_id resolves).

    ``mint=True`` is passed down (AL-1.4a): a scanner detection is born
    DISCOVERED, so its Instance gets a ``draft_id`` and no ``app_id`` —
    identity is conferred by promotion, not by discovery (design §3). A
    re-discovery of an app an operator already vouched for is unaffected: the
    born-status is read off the source dict, and any id already on disk is
    carried forward regardless.

    ``installed_by`` / ``preserve_instance_state_from_disk`` let the caller mark
    an idempotent re-discovery UPDATE (``scanner._gen_and_save`` recognized the
    detected app as an already-installed Instance, redirected ``manifest_dict``
    onto that Instance's id, and wants its learned state / install moment kept).

    Caller (scanner._gen_and_save) falls back to the legacy write when
    ``result.errors`` is non-empty or this raises.
    """
    # identity: see resolve_app_id — NOT swept (AL-1.4b), and the local is
    # renamed for what it holds: the legacy ``id`` is used here ONLY to place
    # the Instance file (``caps_dir/<stem>.json``), the same filename-stem
    # invariant annotated in ``mint_v7_arc_app`` above. Resolving a canonical
    # app id would place a gallery-installed app at ``p-a3f91c8b.json``
    # instead of beside the manifest the scanner already wrote.
    legacy_id = (str(manifest_dict.get("id") or "")).strip()
    if not legacy_id:
        result = MigrationResult(source_path=caps_dir, dry_run=False)
        result.errors.append("manifest dict has no id — cannot place Instance")
        return result
    instance_path = caps_dir / f"{legacy_id}.json"
    return mint_v7_arc_app(
        manifest_dict,
        shared_dir=shared_dir,
        bot_id=bot_id,
        installed_by=installed_by,
        instance_path=instance_path,
        record_supersession=True,
        preserve_instance_state_from_disk=preserve_instance_state_from_disk,
        mint=True,
    )


def convert_scanned_bot_to_v7_arc(
    *,
    bot_id: str,
    shared_dir: Path,
    caps_dir: Path,
) -> list[MigrationResult]:
    """Promote a bot's legacy scanner manifests to native v7-arc, from the
    EVOLVE process context (post-scan).

    The scanner subprocess runs as the bot user (``sudo -u <bot>``), so its
    in-scan mint can't *create* a new Spec under the evolve-owned
    ``{shared_dir}/gallery/local/`` — it hits ``[Errno 13] Permission
    denied`` and ``_gen_and_save`` falls back to a legacy single-file write.
    (Apps whose Spec already exists bind fine and mint v7-arc even as the
    bot — that's why gallery-installed apps come out v7-arc while
    scanner-discovered ones come out legacy.) Re-running the identical mint
    here, from the admin server (which runs as ``evolve`` and owns the
    gallery), converts those legacy manifests to native v7-arc.

    Only ``manifest_shape == ""`` (legacy) manifests are touched —
    ``v7-arc`` and ``v7-arc-pre`` shapes are already split and left alone.
    Best-effort and per-manifest isolated: ``mint_v7_arc_app`` leaves the
    valid legacy manifest in place on any error (no flag day), so the worst
    case is the bot stays exactly as it is today.

    Returns one ``MigrationResult`` per *successfully* converted manifest.
    """
    from .manifest import MANIFEST_SHAPE_LEGACY

    if not caps_dir.is_dir():
        return []

    converted: list[MigrationResult] = []
    for path in sorted(caps_dir.glob("*.json")):
        if path.name.startswith("."):  # .scan-status.json etc.
            continue
        try:
            legacy = json.loads(path.read_text())
        except (OSError, ValueError):  # ValueError: JSON/Unicode decode
            continue
        if not isinstance(legacy, dict):
            continue
        if legacy.get("manifest_shape", MANIFEST_SHAPE_LEGACY) != MANIFEST_SHAPE_LEGACY:
            continue  # already v7-arc / v7-arc-pre — not ours to split
        result = mint_v7_arc_app(
            legacy,
            shared_dir=shared_dir,
            bot_id=bot_id,
            installed_by="scanner_promote",
            instance_path=path,
        )
        if result.succeeded:
            converted.append(result)

    # Same stale-files[] index cleanup convert_completed_install does — the
    # replaced files no longer carry a legacy files[] list. Once per batch,
    # not per manifest.
    if converted:
        try:
            from .manifest import _rebuild_file_index_for
            _rebuild_file_index_for(shared_dir)
        except Exception as exc:  # noqa: BLE001 — index rebuild is advisory
            # Surface on the results rather than swallow — mirrors
            # convert_completed_install_to_v7_arc above.
            converted[0].warnings.append(
                f"file-index rebuild failed (advisory): {exc}"
            )

    return converted
