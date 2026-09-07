"""arbiter.dismissals — Signature-based proposal-suppression store.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md §"Decline buttons" →
Dismiss.

When the operator dismisses a proposal, the dismiss action writes a
suppression entry keyed by the proposal's ``dismiss_signature``. The
next time the generator tries to emit a finding with the same
signature, ``is_suppressed()`` returns True and the generator skips
the emission.

The store lives at ``{shared_dir}/proposals/dismissed_signatures.jsonl``
— append-only JSONL. Each entry records when the dismiss happened,
when it expires (default 90-day TTL), which bot it applies to (per-bot
by default), and whether the operator has lifted the suppression
since (lifts are recorded as new entries; ``is_suppressed`` reads the
latest entry per signature).

Why JSONL: append-only is crash-safe and survives partial writes
mid-script. Reads are O(N) but N is small (one entry per dismiss; an
active pod accumulates dozens, not millions). Pruning expired entries
is a separate operation (see ``prune_expired``).

Spec § scopes:
  - ``kind`` (default) — suppresses future findings with the same
    signature. The dismissed_signatures.jsonl entry uses the
    full signature.
  - ``instance`` — suppresses only the specific proposal id. The
    entry uses ``proposal:<proposal_id>`` as the signature key so
    it doesn't accidentally collide with kind-scoped suppressions
    that share a generator id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal

log = logging.getLogger(__name__)


# Default time-to-live for a dismiss action. The spec rationale:
# "you said no in June; if the same thing is true in September,
# ask again." Operator can pick 'permanent' (no TTL) in the UI;
# at the storage layer that just sets expires_at = None.
DEFAULT_TTL_DAYS: int = 90

# Filename under {shared_dir}/proposals/.
_FILENAME = "dismissed_signatures.jsonl"

DismissScope = Literal["kind", "instance"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _store_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "proposals" / _FILENAME


def _instance_key(proposal_id: str) -> str:
    """Build the suppression key for instance-scoped dismissals.

    Prefixing with ``proposal:`` keeps these from colliding with
    kind-scoped signatures (which are generator-id-prefixed by
    convention)."""
    return f"proposal:{proposal_id}"


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Yield each entry in the JSONL store, skipping malformed lines.

    Best-effort: a partial write or hand-edit shouldn't break reads.
    Each malformed line gets logged and skipped — the caller still
    sees the well-formed entries."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning(
                        "dismissals: skip malformed line %d in %s: %s",
                        lineno, path, e,
                    )
    except OSError as e:
        log.warning("dismissals: read failed on %s: %s", path, e)


def _append_jsonl(path: Path, entry: dict) -> None:
    """Append a single entry to the JSONL store. Creates parent dirs
    + the file if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def record_dismissal(
    shared_dir: Path,
    *,
    signature: str,
    bot_id: str | None,
    scope: DismissScope = "kind",
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    rationale: str = "",
    proposal_id: str | None = None,
) -> dict:
    """Write a dismissal entry. Returns the entry as written.

    ``ttl_days=None`` means permanent (no expires_at).

    ``proposal_id`` is recorded for traceability + used as the
    suppression key when ``scope == "instance"``.

    Raises ValueError if signature is empty AND scope is "kind"
    (no signature means we can't suppress anything kind-wide; the
    caller should fall back to scope="instance" with the proposal_id).
    """
    if scope == "kind" and not signature:
        raise ValueError(
            "record_dismissal: scope='kind' requires a non-empty signature"
        )
    if scope == "instance" and not proposal_id:
        raise ValueError(
            "record_dismissal: scope='instance' requires proposal_id"
        )

    now = _utc_now()
    key = signature if scope == "kind" else _instance_key(proposal_id)
    expires_at: str | None = None
    if ttl_days is not None and ttl_days > 0:
        expires_at = (now + timedelta(days=ttl_days)).isoformat(
            timespec="seconds"
        )

    entry = {
        "key": key,
        "signature": signature,
        "proposal_id": proposal_id,
        "bot_id": bot_id,
        "scope": scope,
        "dismissed_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "ttl_days": ttl_days,
        "rationale": rationale or "",
        "lifted_at": None,
    }
    _append_jsonl(_store_path(shared_dir), entry)
    return entry


def lift_dismissal(
    shared_dir: Path,
    *,
    key: str,
    bot_id: str | None = None,
    rationale: str = "",
) -> dict | None:
    """Append a 'lift' entry that cancels the latest active suppression
    for ``key`` (+ optional bot_id scoping). Returns the lift entry, or
    None if no active suppression matched.

    Lifts are themselves entries in the JSONL — that's how
    is_suppressed() resolves the current state (latest entry per key
    wins). Keeps the audit trail intact while flipping the live state.
    """
    now = _utc_now()
    # Look up the most recent active suppression to confirm there's
    # something to lift. Otherwise the lift is a no-op (we still write
    # it for audit, but return None to tell the caller nothing was
    # actively suppressed).
    active = _resolve_latest(
        shared_dir, key=key, bot_id=bot_id, at=now,
    )
    if active is None or active.get("lifted_at"):
        return None

    entry = {
        "key": key,
        "signature": active.get("signature", ""),
        "proposal_id": active.get("proposal_id"),
        "bot_id": bot_id,
        "scope": active.get("scope", "kind"),
        "dismissed_at": active.get("dismissed_at"),
        "expires_at": active.get("expires_at"),
        "ttl_days": active.get("ttl_days"),
        "rationale": rationale or "",
        # The lift marker. is_suppressed reads this to short-circuit.
        "lifted_at": now.isoformat(timespec="seconds"),
    }
    _append_jsonl(_store_path(shared_dir), entry)
    return entry


def _resolve_latest(
    shared_dir: Path,
    *,
    key: str,
    bot_id: str | None,
    at: datetime,
) -> dict | None:
    """Return the most recent entry for (key, bot_id) at the given
    timestamp, or None if none.

    Resolution rule: file order is canonical. The JSONL is
    append-only, so later lines represent newer state — even when
    timestamps tie at second resolution (lift + dismiss in the same
    second is plausible). Tiebreaking by file position avoids racing
    on the timestamp string.
    """
    path = _store_path(shared_dir)
    latest: dict | None = None
    for entry in _read_jsonl(path):
        if entry.get("key") != key:
            continue
        # Per-bot scoping: a dismissal recorded for a specific bot_id
        # only suppresses for that bot. None bot_id (pod-wide) matches
        # any bot lookup.
        entry_bot = entry.get("bot_id")
        if entry_bot is not None and bot_id is not None and entry_bot != bot_id:
            continue
        # File-order overrides timestamp comparison — later line wins,
        # always. Equivalent to scanning the file and remembering the
        # last match.
        latest = entry
    return latest


def is_suppressed(
    shared_dir: Path,
    *,
    signature: str,
    bot_id: str | None = None,
    at: datetime | None = None,
) -> bool:
    """Return True if a kind-scoped dismissal is currently active for
    ``signature`` (+ optional bot scoping).

    Generators call this at emission time. ``at`` is for tests; defaults
    to now.
    """
    if not signature:
        return False
    at = at or _utc_now()
    latest = _resolve_latest(shared_dir, key=signature, bot_id=bot_id, at=at)
    if latest is None:
        return False
    # Lifted entries cancel earlier suppressions.
    if latest.get("lifted_at"):
        return False
    # Expired entries are no longer active.
    expires_at = latest.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt <= at:
                return False
        except ValueError:
            log.warning(
                "dismissals: malformed expires_at %r — treating as active",
                expires_at,
            )
    return True


def iter_active(
    shared_dir: Path, *, at: datetime | None = None,
) -> Iterator[dict]:
    """Yield (one per signature) the active suppressions — most-recent
    non-lifted, non-expired entry per (key, bot_id). For the UI's
    Suppressions list.

    Resolution mirrors _resolve_latest: file order is canonical, so
    later lines overwrite earlier ones for the same (key, bot_id).
    """
    at = at or _utc_now()
    by_key: dict[tuple[str, str | None], dict] = {}
    for entry in _read_jsonl(_store_path(shared_dir)):
        key = entry.get("key", "")
        bot_id = entry.get("bot_id")
        composite = (key, bot_id)
        # File-order canonical: each later entry overwrites earlier
        # ones for the same composite key.
        by_key[composite] = entry

    for entry in by_key.values():
        if entry.get("lifted_at"):
            continue
        expires_at = entry.get("expires_at")
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) <= at:
                    continue
            except ValueError:
                pass
        yield entry


def preload_suppressed_signatures(
    shared_dir: Path | None,
    bot_id: str | None,
    *,
    at: datetime | None = None,
) -> set[str]:
    """Materialize active kind-scoped dismiss signatures for ``bot_id``
    into a set, for use as an O(1) gate in a generator's per-emission
    loop.

    Behavior:
      - Reads the JSONL store once via :func:`iter_active`.
      - Returns only kind-scoped entries (instance-scope signatures are
        empty strings; those are skipped).
      - Per-bot scoping: an entry with ``bot_id=None`` (pod-wide)
        matches every bot_id lookup; an entry with an explicit bot_id
        matches only its own.
      - When ``bot_id`` is None (a pod-wide generator like
        evolve_watchdog), every kind-scoped entry is included
        regardless of which bot it was recorded for. This matches the
        prior pod-wide ``_filter_dismissed`` behavior in those
        generators.

    Fail-open semantics: any import or read failure returns an empty
    set so a broken sidecar can never silence legitimate proposals.
    This mirrors the contract every generator's hand-rolled
    ``_load_suppressed`` / ``_filter_dismissed`` used to enforce
    individually; centralizing it here means the fail-open policy
    lives in one place instead of being copy-pasted ~12 times.

    ``at`` is a test hook for time-pinning the active set; production
    callers leave it None.
    """
    if shared_dir is None:
        return set()
    try:
        active: list[dict] = list(iter_active(shared_dir, at=at))
    except Exception:
        return set()
    out: set[str] = set()
    for entry in active:
        sig = entry.get("signature") or ""
        if not sig:
            # Instance-scope entries can't suppress kind-wide. The
            # store also stamps a non-empty signature on kind-scoped
            # entries, so an empty value here always means "instance".
            continue
        entry_bot = entry.get("bot_id")
        # entry_bot == None  → pod-wide suppression; always matches.
        # bot_id == None     → pod-wide caller; takes every kind entry.
        # else               → must match exactly.
        if entry_bot is not None and bot_id is not None and entry_bot != bot_id:
            continue
        out.add(sig)
    return out


def filter_dismissed(
    proposals: list,
    shared_dir: Path | None,
    bot_id: str | None,
    *,
    at: datetime | None = None,
) -> list:
    """Drop proposals whose ``dismiss_signature`` is currently suppressed
    for ``bot_id``. Convenience wrapper around
    :func:`preload_suppressed_signatures`.

    Most generators want the filter, not the set — they build the
    proposal list, then drop entries whose signature has been
    dismissed. Generators that inspect the set directly (to short-
    circuit cluster-level work before constructing a Proposal) call
    :func:`preload_suppressed_signatures` instead.

    Empty input returns empty without a store read; empty suppression
    set returns a shallow copy of the input. Same fail-open semantics
    as the underlying preload.
    """
    if not proposals:
        return list(proposals)
    suppressed = preload_suppressed_signatures(shared_dir, bot_id, at=at)
    if not suppressed:
        return list(proposals)
    return [
        p for p in proposals
        if getattr(p, "dismiss_signature", None) not in suppressed
    ]


def signature_for_proposal(proposal) -> tuple[str, DismissScope]:
    """Pick the suppression signature for a Proposal.

    Returns ``(signature_or_proposal_id, scope)``:
      - When the proposal has a non-empty ``dismiss_signature``
        attribute, returns that with scope from the proposal
        (``dismiss_scope``, defaulting to "kind").
      - When the proposal has ``dismiss_scope == "instance"`` (or
        no dismiss_signature), returns the proposal id with scope
        "instance".
      - Defensive fallback when the proposal predates the Phase A
        schema and has no dismiss_* attrs: returns ("", "instance"),
        and the caller passes proposal_id to record_dismissal.

    This helper isolates the Phase A schema dependency so callers
    don't need to know about the new fields directly.
    """
    sig = getattr(proposal, "dismiss_signature", None) or ""
    scope: DismissScope = getattr(proposal, "dismiss_scope", None) or "kind"
    if scope == "instance" or not sig:
        return "", "instance"
    return sig, "kind"
