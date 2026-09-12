"""app_identity — the ONE place Python answers "which app is this?".

AL-1.4a (internal/build-AL-1.4-app-id-canonical.md §2; design decision in
internal/design-app-spec-and-discovery-2026-08-15.md §3). ``app_id`` is the
canonical identity: a lowercase slug (``APP_ID_PATTERN`` — the same rule
``RecordApplicationTool`` already enforces), conferred when an app becomes
*defined* and immutable thereafter. Its TS twin is
``packages/plugin/src/apps/appIdentity.ts``; the two are pinned together by
the shared vector ``packages/plugin/tests/fixtures/app-id-resolution.json``
(audit-app-framework-2026-07-02 D4).

Resolution order — ``app_id`` first, then the legacy chain
``pkg_id -> id -> spec_id -> instance_id``, top-level only, always stripped:

    app_id > pkg_id > id > spec_id > instance_id

1.4a keeps the legacy chain as a *fallback* and keeps mirroring the legacy
fields, so every one of the ~96 identity readers that has not yet been swept
onto this resolver (that sweep is 1.4b) still finds exactly what it found
before. 1.4c drops the fallback once the migration table
(``{shared_dir}/apps/id-migration.json``, written by ``evolve-admin
application migrate-ids``) proves nothing on the pod still needs it.

BEHAVIOR-NEUTRALITY IS THE LOAD-BEARING PROPERTY OF 1.4a. Every stamp this
module performs writes the id the legacy chain ALREADY resolves to, so
``resolve_app_id`` returns the same string for the same manifest before and
after the stamp. That is why ``canonical_app_id`` REFUSES to stamp a value
that normalization would change (an uppercase or over-long legacy id):
slugifying it would mint an identity that differs from the one already
load-bearing in filenames, workspace markers, cron labels and the plugin's
app-integrity coverage keys — a silent re-identification, not a migration.
Such manifests are reported by ``migrate-ids`` as ``non_conforming`` and left
without an ``app_id``; conferring one is an operator act, not a backfill.

DISCOVERED DRAFTS NEVER GET AN ``app_id`` (design §3) — but that rule binds at
the MINT boundary only. ``stamp_identity(..., mint=True)`` (the scanner's two
first-write sites) gives a born-discovered manifest a ``draft_id`` and no
``app_id``. Backfilling a manifest that ALREADY exists on disk is a different
act: its id is load-bearing wherever it is already written down, so
``ensure_app_id`` stamps it whatever its ``definition_status`` says. Without
that split the backfill would skip nearly every manifest on a live pod (the
v27 migration lands every pre-existing manifest at ``discovered``) and the
migration table 1.4c depends on would be empty. The one thing the backfill
will NOT overwrite is an existing ``draft_id``: that field is the positive
record of a mint having declined to confer identity, so it is honored rather
than reversed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "APP_ID_FIELD",
    "APP_ID_PATTERN",
    "DRAFT_ID_FIELD",
    "DRAFT_ID_PREFIX",
    "LEGACY_APP_ID_FIELDS",
    "APP_ID_RESOLUTION_ORDER",
    "resolve_app_id",
    "resolve_legacy_app_id",
    "draft_id_of",
    "normalize_app_id",
    "is_canonical_app_id",
    "canonical_app_id",
    "mint_draft_id",
    "ensure_app_id",
    "ensure_draft_id",
    "is_discovered",
    "stamp_identity",
]

APP_ID_FIELD = "app_id"
DRAFT_ID_FIELD = "draft_id"
DRAFT_ID_PREFIX = "draft-"

# Mirrors APP_ID_PATTERN in packages/plugin/src/tools/RecordApplicationTool.ts
# and appIdentity.ts: lowercase alphanumeric + hyphens, 3-48 chars, starting
# and ending alphanumeric.
APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")

# The pre-1.4 chain. Order is load-bearing and MUST match appIdentity.ts.
LEGACY_APP_ID_FIELDS: tuple[str, ...] = ("pkg_id", "id", "spec_id", "instance_id")

# The full 1.4a order: canonical field first, legacy chain as fallback.
APP_ID_RESOLUTION_ORDER: tuple[str, ...] = (APP_ID_FIELD, *LEGACY_APP_ID_FIELDS)

# Value of ``definition_status`` that marks a scanner-born draft. Imported
# lazily from manifest.py would be circular (manifest.py imports this module),
# so the literal is repeated here and pinned by a test.
_DEFINITION_DISCOVERED = "discovered"


def _first_non_empty(manifest: Any, fields: tuple[str, ...]) -> str:
    """First non-empty stripped string among ``fields``, top-level only."""
    if not isinstance(manifest, dict):
        return ""
    for key in fields:
        val = manifest.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def resolve_app_id(manifest: Any) -> str:
    """The canonical app id for a **raw** manifest. "" when none resolves.

    ``app_id`` first, then ``pkg_id`` / ``id`` / ``spec_id`` / ``instance_id``,
    read top-level only, always stripped. Mirrors ``appIdOf`` in
    ``appIdentity.ts`` exactly (the TS twin returns "unknown" rather than ""
    for the none case — its documented middleware sentinel).

    Feed the RAW manifest. v7-arc hydration rewrites ``id``/``pkg_id``
    (``manifest.hydrate_v7_arc_instance`` sets ``id = instance_id`` and
    ``pkg_id = provenance.spec_id``), so a hydrated manifest can resolve to a
    DIFFERENT key than the one the plugin's coverage writer used.

    The ``app_id`` FIELD only wins when its value is a conforming slug. The
    name was not free: ``gallery/index.json`` has carried an ``app_id`` key
    since #3413 holding the app SCRIPT name (``app_task_manager``), not the
    package key, and all 15 builtin packages' values fail ``APP_ID_PATTERN``.
    Honoring those would hand every gallery reader an id that
    ``canonical_app_id`` would refuse to STAMP — the read side contradicting
    the write side. Non-conforming values therefore fall through to the legacy
    chain, which is what those readers resolved to before 1.4a. Silent by
    design: falling through preserves behavior, and ``canonical_app_id``
    already logs on the write side where the value can actually be fixed.
    """
    canonical = _first_non_empty(manifest, (APP_ID_FIELD,))
    if canonical and is_canonical_app_id(canonical):
        return canonical
    return _first_non_empty(manifest, LEGACY_APP_ID_FIELDS)


def resolve_legacy_app_id(manifest: Any) -> str:
    """The pre-1.4 answer: the legacy chain ONLY, ignoring ``app_id``.

    This is what every stamp writes, and what makes the stamp provably
    behavior-neutral: after stamping, ``resolve_app_id`` returns exactly what
    ``resolve_legacy_app_id`` returned before it.
    """
    return _first_non_empty(manifest, LEGACY_APP_ID_FIELDS)


def draft_id_of(manifest: Any) -> str:
    """The scanner-assigned draft id, or "" — NEVER falls back to an app id.

    A discovered draft's identity is explicitly unstable (design §3): it may be
    merged, renamed or dropped freely, and must never appear in attribution,
    access or sharing. Falling back to the legacy chain here would hand callers
    exactly the durable-looking id the draft is not allowed to have.
    """
    return _first_non_empty(manifest, (DRAFT_ID_FIELD,))


def normalize_app_id(value: Any) -> str:
    """Strip + lowercase ``value``; "" when it is not a conforming slug."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if APP_ID_PATTERN.match(candidate) else ""


def is_canonical_app_id(value: Any) -> bool:
    """True when ``value`` is already a conforming slug needing no rewrite."""
    return isinstance(value, str) and bool(APP_ID_PATTERN.match(value.strip())) \
        and value.strip() == value.strip().lower()


def canonical_app_id(raw: Any, *, context: str = "") -> str:
    """``raw`` when stamping it changes nothing; "" (logged) otherwise.

    The behavior-neutrality gate. A legacy id that normalization would REWRITE
    (uppercase, too short/long, illegal characters) is left unstamped rather
    than silently re-identified — see the module docstring.
    """
    stripped = raw.strip() if isinstance(raw, str) else ""
    if not stripped:
        return ""
    if is_canonical_app_id(stripped):
        return stripped
    logger.info(
        "app_identity: legacy id %r is not a conforming app_id slug%s — "
        "leaving app_id unset (normalizing it would change the id every "
        "existing reader, marker and coverage key already uses; confer one "
        "explicitly instead)",
        stripped,
        f" ({context})" if context else "",
    )
    return ""


def mint_draft_id(seed: str) -> str:
    """Deterministic ``draft-<12 hex>`` for ``seed``.

    Deterministic on purpose: a re-scan of the same detection must produce the
    same draft id, otherwise every scan rewrites every draft manifest and the
    churn shows up as drift in the reconcile pass. The value carries no
    meaning and is never a stable identity — that is the point of a draft id.
    """
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()
    return f"{DRAFT_ID_PREFIX}{digest[:12]}"


def is_discovered(data: Any) -> bool:
    """True when ``data`` carries (or defaults to) ``definition_status``
    ``"discovered"`` — the scanner-born draft state. Absent reads as
    discovered (the v27 inert default), so a dict that predates the field is
    never mistaken for a vouched app."""
    if not isinstance(data, dict):
        return True
    status = data.get("definition_status")
    if not isinstance(status, str) or not status.strip():
        return True  # absent reads as discovered (the v27 inert default)
    return status.strip() == _DEFINITION_DISCOVERED


def ensure_app_id(data: dict, *, context: str = "") -> str:
    """Populate-on-absent ``app_id`` from the legacy chain. Returns the id.

    Idempotent, never overwrites an existing conforming ``app_id`` (identity is
    immutable once conferred), and never invents an id a reader does not
    already see. Returns "" when nothing conforming resolves.
    """
    if not isinstance(data, dict):
        return ""
    existing = data.get(APP_ID_FIELD)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    if draft_id_of(data):
        # A ``draft_id`` is the positive record that a mint DECLINED to confer
        # identity (design §3). Backfilling an app_id over it would quietly
        # reverse that decision — and a draft's id is exactly the churnable
        # one the scanner is allowed to merge or rename. Promotion confers;
        # backfill does not.
        return ""
    app_id = canonical_app_id(resolve_legacy_app_id(data), context=context)
    if app_id:
        data[APP_ID_FIELD] = app_id
    return app_id


def ensure_draft_id(data: dict, *, seed: str = "") -> str:
    """Populate-on-absent ``draft_id`` for a born-discovered manifest.

    ``seed`` defaults to the legacy-resolved id, which for a scanner mint is
    the detection's stable stem — so the draft id is stable across re-scans.
    """
    if not isinstance(data, dict):
        return ""
    existing = data.get(DRAFT_ID_FIELD)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    material = (seed or resolve_legacy_app_id(data)).strip()
    if not material:
        return ""
    draft_id = mint_draft_id(material)
    data[DRAFT_ID_FIELD] = draft_id
    return draft_id


def stamp_identity(
    data: dict,
    *,
    mint: bool = False,
    discovered: bool | None = None,
    seed: str = "",
    context: str = "",
) -> str:
    """Stamp the identity field this manifest is entitled to.

    ``mint=False`` (every writer that touches a manifest which already exists
    on disk, plus the read-path backfill): stamp ``app_id``.

    ``discovered`` overrides where the draft/defined judgement is read from —
    pass it when ``data`` is a freshly-built projection that does not carry
    ``definition_status`` yet but the source dict does.

    ``mint=True`` (the scanner's first-write sites only): a born-discovered
    manifest gets a ``draft_id`` and NO ``app_id`` (design §3 — identity is
    conferred by promotion, not by discovery); a born-defined one is stamped
    normally.

    Returns the ``app_id`` stamped, or "" when the manifest got a draft id (or
    nothing conforming resolved).
    """
    if not isinstance(data, dict):
        return ""
    is_draft = is_discovered(data) if discovered is None else discovered
    if mint and is_draft and not data.get(APP_ID_FIELD):
        ensure_draft_id(data, seed=seed)
        return ""
    return ensure_app_id(data, context=context)
