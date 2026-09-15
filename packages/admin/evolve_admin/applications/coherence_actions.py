"""Pure mutations the Apps-page UI applies to a manifest.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10–§11.

Every function takes a manifest dict + arguments, mutates the dict in
place (idempotent), and returns a small result the caller can include
in the HTTP response. No I/O — the web layer is responsible for the
read-mutate-write trio. This keeps the action logic unit-testable
against fixture dicts and lets the evo gateway eventually reuse the
same helpers (today's evo handlers in
``evo/handlers/app_changes.py`` are PR-scope acknowledgements; once
their write path lands they call into here).
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). The single mention is a docstring naming the
# v7-arc ``apps[]`` entry SCHEMA (spec_id / spec_version / installed_at /
# installed_by) that this module writes. Schema field names, not an identity
# read.

from __future__ import annotations

import hashlib
from typing import Any

from evolve_util import now_iso as _now_iso

from .manifest import (
    MANIFEST_DEFINITION_DEFINED,
    MANIFEST_DEFINITION_DISCOVERED,
    MANIFEST_SHAPE_V7_ARC,
    MANIFEST_SHAPE_V7_ARC_PRE,
    PROVENANCE_CONFIRMED,
    PROVENANCE_OBSERVATIONAL,
    stamp_field_origins,
)
from .reconciliation import is_field_authored

# The manifest fields that ANCHOR an app's identity. Promotion to "defined"
# marks these as authored (provenance.field_origins) so the scanner can no
# longer silently re-mint / rename / merge a vouched app and reconciliation
# surfaces any drift on them as an operator chip instead of a silent
# overwrite — the keystone of the Defined/Discovered model
# (internal/spec-apps-meta-2026-06-13.md §9). ``name`` is the app's handle; the
# canonical identity line is ``description`` (the one-line what-it-is shown on
# the card and folded into the scanner's identity-similarity merge heuristic,
# the exact field the over-merge incident churned). Demotion relaxes only the
# marks promotion itself added (source=="confirmed").
ANCHORED_IDENTITY_FIELDS: tuple[str, ...] = ("name", "description")

# The ``field_origins[*].via`` channel promotion stamps on the marks it sets.
# Demote relaxes ONLY fields carrying this channel, so a field made
# ``confirmed`` by a DIFFERENT path (e.g. the coherence "Mark as ready" click,
# which also stamps PROVENANCE_CONFIRMED) keeps its vouch through a
# promote/demote round-trip.
_PROMOTE_VIA = "definition_status:promote"
_DEMOTE_VIA = "definition_status:demote"


def _definition_status(manifest: dict) -> str:
    """Normalised current definition_status (absent reads as discovered)."""
    raw = (manifest.get("definition_status") or "").strip().lower()
    return raw if raw == MANIFEST_DEFINITION_DEFINED else MANIFEST_DEFINITION_DISCOVERED


def _is_v7_arc(manifest: dict) -> bool:
    """True for a v7-arc App Instance.

    A v7-arc instance's ``provenance`` block is INSTALL provenance
    (spec_id / spec_version / installed_at / installed_by, additionalProperties
    false) — NOT the legacy v20 coherence-provenance (field_origins /
    last_promoted_at). It also carries no ``field_origins`` (identity lives on
    the immutable bound Spec). So the anchored-identity marking + last_promoted
    stamp are legacy-shape mechanics; on v7-arc the ``definition_status`` flip
    alone carries the operator vouch (the existence shield reads it off the
    Instance) and the Spec already anchors name/description against scanner
    rename. Writing coherence-provenance keys onto an Instance would corrupt
    its install-provenance block, so we deliberately skip them.

    Covers the transient ``v7-arc-pre`` shape too (a gallery-installed Instance
    not yet reconciled) — it carries the same install-provenance block.
    """
    return (manifest.get("manifest_shape") or "") in (
        MANIFEST_SHAPE_V7_ARC,
        MANIFEST_SHAPE_V7_ARC_PRE,
    )


def _coh(manifest: dict) -> dict:
    coh = manifest.setdefault("coherence", {})
    coh.setdefault("findings", [])
    coh.setdefault("coherence_accepted", [])
    coh.setdefault("flags", [])
    return coh


def _rec(manifest: dict) -> dict:
    return manifest.setdefault("reconciliation", {})


def approve_changes(manifest: dict, *, by: str = "ui:operator") -> dict:
    """Accept all observational changes on the manifest.

    Clears ``reconciliation.added_files``, ``removed_files``, and
    ``drifted_fields``. Records an entry in
    ``reconciliation.operator_decisions`` so we have an audit trail of
    who said yes when.

    Returns ``{approved_count, cleared}`` so the UI can render a
    confirmation message and reflect the new state without re-reading.
    """
    rec = _rec(manifest)
    added = rec.get("added_files") or []
    removed = rec.get("removed_files") or []
    drifted = rec.get("drifted_fields") or []
    count = len(added) + len(removed) + len(drifted)

    rec["added_files"] = []
    rec["removed_files"] = []
    rec["drifted_fields"] = []

    decisions = rec.setdefault("operator_decisions", [])
    decisions.append({
        "kind": "approve",
        "at": _now_iso(),
        "by": by,
        "cleared": {
            "added_files": len(added),
            "removed_files": len(removed),
            "drifted_fields": len(drifted),
        },
    })

    manifest["updated_at"] = _now_iso()
    return {"approved_count": count}


def promote_to_authored(manifest: dict, *, by: str = "ui:operator") -> dict:
    """Flip every observational provenance entry to ``bot_authored``.

    Spec §4.3: Promote turns the current observed state into the
    contract — future drift on these fields surfaces as a real change.

    Returns ``{promoted_fields}``.
    """
    prov = manifest.setdefault("provenance", {})
    origins = prov.setdefault("field_origins", {})

    promoted: list[str] = []
    for field, entry in list(origins.items()):
        if not isinstance(entry, dict):
            continue
        source = (entry.get("source") or "").lower()
        if source == "observational":
            entry["source"] = "bot_authored"
            entry["promoted_at"] = _now_iso()
            entry["promoted_by"] = by
            promoted.append(field)

    manifest["updated_at"] = _now_iso()
    return {"promoted_fields": promoted}


def promote_to_defined(manifest: dict, *, by: str = "ui:operator") -> dict:
    """Promote a manifest to ``definition_status="defined"`` — the operator
    vouches it as the authoritative contract (source of truth).

    Spec: internal/spec-apps-meta-2026-06-13.md §9. REVERSIBLE (see
    ``demote_to_discovered``) and idempotent.

    Effects:
      * ``definition_status`` → ``"defined"``.
      * ``provenance.last_promoted_at`` stamped.
      * Each anchored-identity field (name + canonical identity line) that is
        NOT already authored is marked authored via ``field_origins`` with
        source ``confirmed``. An already-authored field (operator edit / forge
        build) is left untouched — it is already protected, and not stamping it
        means a later demote cannot relax authorship the operator established by
        another path.

    Returns ``{definition_status, was_already_defined, marked_fields,
    last_promoted_at}`` so the caller can render a confirmation without a
    re-read.
    """
    was_defined = _definition_status(manifest) == MANIFEST_DEFINITION_DEFINED
    manifest["definition_status"] = MANIFEST_DEFINITION_DEFINED
    now = _now_iso()

    # Legacy-shape only: mark the anchored-identity fields authored + stamp the
    # promotion in the v20 coherence-provenance block. A v7-arc Instance has a
    # distinct install-provenance block and no field_origins (see _is_v7_arc) —
    # writing here would corrupt it, and its identity is already Spec-anchored,
    # so the definition_status flip above is the whole vouch on that shape.
    to_mark: list[str] = []
    if not _is_v7_arc(manifest):
        # Mark only the anchored fields that aren't already authored. On a
        # re-promote they are already ``confirmed`` (authored) → nothing
        # re-stamps, so the action stays idempotent.
        to_mark = [
            f for f in ANCHORED_IDENTITY_FIELDS if not is_field_authored(manifest, f)
        ]
        if to_mark:
            stamp_field_origins(
                manifest,
                source=PROVENANCE_CONFIRMED,
                fields=to_mark,
                by=by,
                via=_PROMOTE_VIA,
            )
        prov = manifest.setdefault("provenance", {})
        prov["last_promoted_at"] = now
        prov["last_promoted_by"] = by

    manifest["updated_at"] = now
    return {
        "definition_status": MANIFEST_DEFINITION_DEFINED,
        "was_already_defined": was_defined,
        "marked_fields": to_mark,
        "last_promoted_at": now,
    }


def demote_to_discovered(manifest: dict, *, by: str = "ui:operator") -> dict:
    """Reverse a promotion: ``definition_status`` → ``"discovered"`` and relax
    the promotion's own anchored-identity marks back to ``observational``.

    Spec: internal/spec-apps-meta-2026-06-13.md §9 (impl notes §9.7). Idempotent.
    Relaxes ONLY a field whose ``field_origins`` entry was set by THIS lifecycle
    — source ``confirmed`` AND channel ``via == _PROMOTE_VIA``. A field the
    operator authored another way keeps its vouch: ``user_authored`` (manifest
    editor) / ``forge_built`` (forge), and crucially also ``confirmed`` set via
    a DIFFERENT channel (the coherence "Mark as ready" click, which stamps
    ``PROVENANCE_CONFIRMED`` too). So demote precisely reverses promote without
    clobbering independent edits.

    Returns ``{definition_status, was_already_discovered, relaxed_fields}``.
    """
    was_discovered = _definition_status(manifest) != MANIFEST_DEFINITION_DEFINED
    manifest["definition_status"] = MANIFEST_DEFINITION_DISCOVERED
    now = _now_iso()

    # Legacy-shape only — mirror of promote: relax the promotion's own marks
    # and stamp the demotion in the coherence-provenance block. v7-arc skips
    # it (no field_origins / coherence-provenance to touch).
    relaxed: list[str] = []
    if not _is_v7_arc(manifest):
        prov = manifest.setdefault("provenance", {})
        origins = prov.setdefault("field_origins", {})
        for fld in ANCHORED_IDENTITY_FIELDS:
            entry = origins.get(fld)
            if not isinstance(entry, dict):
                continue
            src = (entry.get("source") or "").strip().lower()
            via = entry.get("via") or ""
            # Relax ONLY a mark THIS lifecycle set — promotion stamps
            # source==confirmed AND via==_PROMOTE_VIA. A field made confirmed by
            # another path (the coherence "Mark as ready" click stamps
            # PROVENANCE_CONFIRMED via a different channel) keeps its vouch.
            # Rebuild a clean entry so no stale promote via/by residue lingers
            # on the now-observational field.
            if src == PROVENANCE_CONFIRMED and via == _PROMOTE_VIA:
                origins[fld] = {
                    "source": PROVENANCE_OBSERVATIONAL,
                    "at": now,
                    "by": by,
                    "via": _DEMOTE_VIA,
                }
                relaxed.append(fld)
        prov["last_demoted_at"] = now
        prov["last_demoted_by"] = by

    manifest["updated_at"] = now
    return {
        "definition_status": MANIFEST_DEFINITION_DISCOVERED,
        "was_already_discovered": was_discovered,
        "relaxed_fields": relaxed,
    }


def flag_for_operator(
    manifest: dict, *, description: str, by: str = "ui:operator",
) -> dict:
    """Append a free-text flag to ``manifest.coherence.flags[]``.

    A future PR wires this into the arbiter as a Proposal; until then
    the flag is stamped on the manifest so it surfaces in the Apps
    page modal and the bot's session block.

    Returns ``{flag_id}``.
    """
    text = (description or "").strip()
    if not text:
        raise ValueError("description required")
    coh = _coh(manifest)
    flag_id = hashlib.sha256(
        f"{text}|{by}|{_now_iso()}".encode("utf-8")
    ).hexdigest()[:12]
    coh["flags"].append({
        "id": flag_id,
        "description": text,
        "by": by,
        "at": _now_iso(),
        "status": "open",
    })
    manifest["updated_at"] = _now_iso()
    return {"flag_id": flag_id}


def mute_finding(
    manifest: dict, *, signature: str, rationale: str = "",
    by: str = "ui:operator",
) -> dict:
    """Add a finding signature to ``coherence_accepted[]`` so Pass A
    filters it out on the next sweep. Spec §6.1.

    Idempotent — a second mute on the same signature only refreshes
    the rationale + by.

    Returns ``{accepted_count}``.
    """
    sig = (signature or "").strip()
    if not sig:
        raise ValueError("signature required")
    coh = _coh(manifest)
    accepted = coh["coherence_accepted"]
    for entry in accepted:
        if isinstance(entry, dict) and entry.get("signature") == sig:
            entry["rationale"] = rationale or entry.get("rationale", "")
            entry["accepted_by"] = by
            entry["accepted_at"] = _now_iso()
            manifest["updated_at"] = _now_iso()
            return {"accepted_count": len(accepted)}
    accepted.append({
        "signature": sig,
        "rationale": rationale or "",
        "accepted_by": by,
        "accepted_at": _now_iso(),
    })
    manifest["updated_at"] = _now_iso()
    return {"accepted_count": len(accepted)}


def unmute_finding(manifest: dict, *, signature: str) -> dict:
    """Remove a previously-muted signature. Idempotent.

    Returns ``{accepted_count, removed}``.
    """
    sig = (signature or "").strip()
    if not sig:
        raise ValueError("signature required")
    coh = _coh(manifest)
    accepted = coh["coherence_accepted"]
    before = len(accepted)
    coh["coherence_accepted"] = [
        e for e in accepted
        if not (isinstance(e, dict) and e.get("signature") == sig)
    ]
    removed = before - len(coh["coherence_accepted"])
    if removed:
        manifest["updated_at"] = _now_iso()
    return {"accepted_count": len(coh["coherence_accepted"]),
            "removed": removed}


def snooze_finding(
    manifest: dict, *, signature: str, until_iso: str,
    by: str = "ui:operator",
) -> dict:
    """Stamp ``snooze.until = <iso>`` on a finding so Pass A treats it
    as muted until that date.

    Returns ``{snoozed_signature, until}``.
    """
    sig = (signature or "").strip()
    if not sig:
        raise ValueError("signature required")
    until = (until_iso or "").strip()
    if not until:
        raise ValueError("until_iso required")
    coh = _coh(manifest)
    for f in coh.get("findings") or []:
        f_sig = f.get("signature") or f.get("id") or ""
        if f_sig == sig:
            f["snooze"] = {
                "until": until,
                "by": by,
                "at": _now_iso(),
            }
            manifest["updated_at"] = _now_iso()
            return {"snoozed_signature": sig, "until": until}
    raise KeyError(f"finding not found: {sig}")


def coherence_summary(manifest: dict) -> dict:
    """Compute a small chip-shaped summary for the list endpoint.

    Returns a flat dict with the fields the cap-card needs:
      coherence_status        — ok / warnings / incoherent (post-mute)
      coherence_findings_count
      coherence_critical_count
      coherence_override_key  — hash of finding signatures for the gate
      reconciliation_status   — ok / drift / orphan / pending
      reconciliation_drifted_count
      reconciliation_added_count
      reconciliation_removed_count
      reconciliation_is_orphan
    """
    coh = manifest.get("coherence") or {}
    rec = manifest.get("reconciliation") or {}
    findings = coh.get("findings") or []
    accepted_sigs = {
        (e.get("signature") or "")
        for e in (coh.get("coherence_accepted") or [])
        if isinstance(e, dict)
    }
    now_iso = _now_iso()
    live = []
    for f in findings:
        sig = f.get("signature") or f.get("id") or ""
        if sig in accepted_sigs:
            continue
        snooze = f.get("snooze") or {}
        until = (snooze.get("until") or "").strip()
        if until and until > now_iso:
            continue
        live.append(f)

    sev_order = {"critical": 0, "major": 1, "minor": 2, "info": 3, "": 4}
    severities = [(f.get("severity") or "").lower() for f in live]
    critical_count = sum(
        1 for s in severities if s in ("critical", "major")
    )
    if not live:
        coherence_status = "ok"
    elif critical_count > 0:
        coherence_status = "incoherent"
    elif severities and min(sev_order.get(s, 4) for s in severities) <= 2:
        coherence_status = "warnings"
    else:
        coherence_status = "ok"

    drifted = rec.get("drifted_fields") or []
    added = rec.get("added_files") or []
    removed = rec.get("removed_files") or []
    is_orphan = (rec.get("status") or "").lower() == "orphan"
    if is_orphan:
        reconciliation_status = "orphan"
    elif drifted or added or removed:
        reconciliation_status = "drift"
    else:
        reconciliation_status = (rec.get("status") or "ok").lower()

    sigs = sorted(
        (f.get("signature") or f.get("id") or "") for f in live
    )
    override_key = hashlib.sha256(
        "|".join(sigs).encode("utf-8")
    ).hexdigest()[:16] if sigs else ""

    return {
        "coherence_status":              coherence_status,
        "coherence_findings_count":      len(live),
        "coherence_critical_count":      critical_count,
        "coherence_override_key":        override_key,
        "reconciliation_status":         reconciliation_status,
        "reconciliation_drifted_count":  len(drifted),
        "reconciliation_added_count":    len(added),
        "reconciliation_removed_count":  len(removed),
        "reconciliation_is_orphan":      is_orphan,
    }
