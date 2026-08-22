"""app_integrity_coverage — read the harness coverage signal the plugin writes.

Bite 2a of the "just-works" integrity arc
(docs/spec-app-invocation-just-works-2026-06-29.md §2.2 / §3).

The execution-integrity middleware (#3362,
``packages/plugin/src/integrity/AppIntegrityMiddleware.ts``) substitutes an
honest structured failure result PRE-MODEL for any recognized app-script tool
call, so a covered app **cannot confabulate success**. As a side signal for
this (the badge/validator) layer it writes, per bot::

    {shared_dir}/{bot_id}/app-integrity-coverage.json
    {
      "version": 1,
      "bot_id": "...",
      "generated_by": "evolve-plugin/app-integrity-middleware",
      "covered_apps": [ {"appId": "...", "scripts": ["...", ...]}, ... ]
    }

(``AppScriptRegistry.writeCoverage`` in ``appScriptRegistry.ts``.) An app
appears in ``covered_apps`` only when the middleware resolved at least one
runnable script path for it from that bot's installed manifests — i.e. the
harness *recognizes this app's scripts and will intercept their failures*.
That presence is the "integrity covered" proof the "May misreport failures"
badge consumes: for a covered app the misreport condition is false by
construction, so the badge must clear.

``appId`` is the middleware's ``appIdOf(manifest)``: the FIRST non-empty of
``app_id`` / ``pkg_id`` / ``id`` / ``spec_id`` / ``instance_id`` read from the
**raw** manifest on disk (``apps/appIdentity.ts``). ``resolve_app_id`` below is
a re-export of ``app_identity.resolve_app_id`` — one implementation per
language rather than the hand-mirrored copies AL-1.4a collapsed. ``app_id`` is
the canonical field (AL-1.4a); the legacy chain behind it stays as a fallback
until 1.4c drops it, and every 1.4a stamp writes the id the legacy chain
already resolved to, so this reader's answer is unchanged for every manifest
that existed before the stamp. Callers MUST feed it the raw (un-hydrated) manifest —
v7-arc hydration rewrites ``id``/``pkg_id`` (``manifest.hydrate_v7_arc_instance``
sets ``id = instance_id`` and ``pkg_id = provenance.spec_id``), so a hydrated
manifest would resolve to a DIFFERENT key than the one the middleware wrote,
silently missing the match. Raw v7-arc instances carry no top-level
``pkg_id``/``id``/``spec_id`` and so key on ``instance_id`` on both sides.

CONSERVATIVE BY CONSTRUCTION. This is the load-bearing safety property of the
arc: **falsely clearing a badge is the dishonesty this work fights.** So every
path here fails toward NOT-covered (badge KEPT). A badge only clears when
coverage is PROVEN:
  * coverage file missing / unreadable / non-JSON      → NOT covered
  * wrong/absent ``version``, or ``covered_apps`` not a list → NOT covered
  * file older than ``COVERAGE_FRESH_S`` (a stale file from a dead or
    downgraded gateway must not certify a harness that isn't running) →
    NOT covered
  * app's resolved id empty, the ``"unknown"`` sentinel, or absent from the
    covered set → NOT covered
Only a fresh, well-formed file that explicitly lists this app's id clears it.

LEGIBILITY: not-covered is the right ANSWER for both an absent and an
unreadable file, but they are different CONDITIONS. Absent is the normal
pre-middleware / fresh-bot state; unreadable (e.g. the plugin minting the file
0600 bot-owned, so the evolve-user admin server can't read it) is a perms
regression that silently re-badges the whole fleet. So the reader distinguishes
them: unreadable logs a warning (once per path until it becomes readable
again — the analytics sweep re-reads every request, so unthrottled it would
spam) while keeping the identical conservative return. The clear semantics do
not change.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .app_identity import resolve_app_id as _resolve_app_id

logger = logging.getLogger(__name__)

# Paths that already warned as unreadable this process; cleared when the path
# reads successfully again so a NEW regression re-warns.
_warned_unreadable: set[str] = set()

COVERAGE_FILENAME = "app-integrity-coverage.json"

# The middleware's sentinel for a manifest that declared no identity. Never a
# match: multiple such manifests would collapse to one key, so an "unknown"
# coverage entry can never prove a specific app is covered.
_UNKNOWN = "unknown"

# Freshness bound. The plugin rewrites the coverage file whenever its covered
# set changes AND on every gateway (re)start (its in-memory de-dup key resets
# to "" on process start, forcing a write on first refresh). A live pod —
# which redeploys/restarts far more often than this window — therefore keeps
# the file fresh continuously. Staleness only accrues when the gateway is dead
# or downgraded off the middleware-capable OpenClaw, i.e. exactly when the
# harness ISN'T protecting the app and "covered" would be a lie. The failure
# direction is deliberately toward KEEP (a stale file stops clearing), never
# toward a false clear. 30 days is generous enough that a normally-managed pod
# never flaps yet bounded enough that an abandoned file eventually stops
# certifying a harness that no longer runs.
COVERAGE_FRESH_DAYS = 30
COVERAGE_FRESH_S = COVERAGE_FRESH_DAYS * 24 * 60 * 60


# AL-1.4a: the resolver moved to applications/app_identity.py so PY has ONE
# implementation instead of a hand-mirrored copy per consumer. Re-exported here
# because this module's name is the documented import site for the coverage
# reader (and for packages/admin/tests/test_app_id_resolution_fixture.py).
#
# identity: see resolve_app_id — AL-1.4b found nothing to sweep in this module:
# it IS the resolver's re-export, and ``is_app_covered`` already routes every
# identity read through it. The remaining ``pkg_id``/``spec_id``/``instance_id``
# mentions above are prose describing that resolution order and the v7-arc
# hydration hazard callers must avoid.
resolve_app_id = _resolve_app_id


def load_covered_ids(
    shared_dir: Path | str, bot_id: str, *, now: float | None = None
) -> set[str]:
    """Set of app-ids the harness PROVABLY covers for ``bot_id``.

    Returns the empty set on any absence, unreadability, schema mismatch, or
    staleness — the conservative default. ``now`` is injectable for tests.
    """
    path = Path(shared_dir) / bot_id / COVERAGE_FILENAME
    try:
        st = path.stat()
        raw = path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        return set()  # ABSENT — the normal pre-middleware state; silent
    except OSError as exc:
        # UNREADABLE — same conservative answer, but a perms regression on a
        # file that EXISTS must be visible, not a silent fleet-wide re-badge.
        if str(path) not in _warned_unreadable:
            _warned_unreadable.add(str(path))
            logger.warning(
                "app-integrity coverage file unreadable — exists with bad "
                "perms, or behind an untraversable parent dir (%s: %s) — "
                "treating %s as NOT covered; the 'May misreport failures' "
                "badge will stay on for this bot's apps until the file is "
                "readable by the admin (evolve) user",
                type(exc).__name__, exc, path,
            )
        return set()
    _warned_unreadable.discard(str(path))

    if now is None:
        now = time.time()
    # Stale file → treat as no proof. A frozen file from a dead/downgraded
    # gateway must not certify an inactive harness.
    if now - st.st_mtime > COVERAGE_FRESH_S:
        return set()

    try:
        data = json.loads(raw)
    except ValueError:
        return set()  # non-JSON / torn → unknown

    if not isinstance(data, dict) or data.get("version") != 1:
        return set()  # unknown schema version → don't trust it
    apps = data.get("covered_apps")
    if not isinstance(apps, list):
        return set()

    covered: set[str] = set()
    for entry in apps:
        if not isinstance(entry, dict):
            continue
        app_id = entry.get("appId")
        if isinstance(app_id, str):
            app_id = app_id.strip()
            if app_id and app_id != _UNKNOWN:
                covered.add(app_id)
    return covered


def id_is_covered(app_id: str, covered_ids: set[str]) -> bool:
    """True only when ``app_id`` is a real id PROVEN present in ``covered_ids``.

    Excludes the empty id and the ``"unknown"`` sentinel so an ambiguous
    manifest can never falsely clear its badge.

    Granularity note: coverage is keyed by the middleware's app IDENTITY (the
    ``appIdOf`` winner — normally ``pkg_id``, the install identity), and the
    plugin folds the recognized scripts of EVERY manifest sharing that identity
    into one entry. The badge is per-manifest-file. In the normal case
    (``pkg_id`` unique per installed app) these are one-to-one. The only way
    they diverge is the install anomaly of two distinct manifest files on one
    bot carrying the SAME ``pkg_id`` where one has a harness-recognized script
    and the other's script is unrecognized — then the unrecognized sibling
    would clear on the identity's coverage. That needs duplicate install
    identities on a single bot (not a shape the forge/gallery produces), so it
    is documented rather than special-cased; hardening it would require the
    admin side to re-resolve each manifest's own absolute script paths against
    the entry's ``scripts[]``, which risks its own false-KEEPs.
    """
    aid = app_id.strip() if isinstance(app_id, str) else ""
    if not aid or aid == _UNKNOWN:
        return False
    return aid in covered_ids


def is_app_covered(raw_manifest: Any, covered_ids: set[str]) -> bool:
    """True when the RAW manifest's resolved app-id is proven covered."""
    return id_is_covered(resolve_app_id(raw_manifest), covered_ids)
