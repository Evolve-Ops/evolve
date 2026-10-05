"""Tool-gap telemetry — Phase 1.5b slice of spec §14.3.

When evo encounters an operator request that doesn't fit any existing
tool (the §13.4 Q4 escape hatch — "neither a proposal nor a direct
tool covers this"), it records a structured ``tool_gap`` here. Over
time the records prioritize which tools to ship next; under the
operator's opt-in, anonymized rollups upload to Evolve's telemetry
endpoint so the dev team sees aggregate demand across pods.

Record shape (per spec §14.3):

    {
      "kind": "config.bot.set_model",   # what evo wanted to do
      "scope": "single_bot",             # blast radius shape
      "fingerprint": "config-edit:agents.defaults.model",
      "first_observed": "2026-05-19T14:32:00Z",
      "last_observed": "2026-05-22T09:11:00Z",
      "frequency_local": 3,
    }

No chat content. No bot id. No proposal body. Just the SHAPE of the
gap. The fingerprint aggregates similar gaps so repeat occurrences
of the same missing-tool pattern increment one record's frequency
rather than spawning N near-identical lines.

Storage: ``{shared_dir}/observations/tool_gaps.jsonl`` (matches the
existing observations convention; owned by the evolve user, no sudo
needed). One JSON object per line, append-only.

Privacy controls (per ``feedback_user_observation_optout``):
  - Always writes locally — useful for the operator's own debugging
    even when telemetry upload is off.
  - Upload behavior is gated by ``network.json::evo_telemetry.tool_gaps``
    (default ``"off"``). Upload daemon is a separate later PR.
  - ``wipe_tool_gaps`` clears the local file; ``evolve-admin
    wipe-telemetry`` (future CLI) calls into this.

The append path is atomic enough for our purposes — one process
(the admin daemon) writes; tests + the future CLI read. We don't
serialize multi-writer contention because there isn't any.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

from evolve_util import now_iso as _now_iso

log = logging.getLogger(__name__)


# In-process lock for append correctness within one Python process.
# The daemon is single-process; the CLI is short-lived and doesn't
# race with the daemon. Belt-and-suspenders against future concurrent
# proxy calls.
_APPEND_LOCK = threading.Lock()


# Scope strings the spec calls out (informational; the writer doesn't
# enforce — caller can use any string but conventional values aid
# aggregation upstream).
KNOWN_SCOPES: frozenset[str] = frozenset({
    "single_bot", "pod_wide", "external", "unknown",
})


# Validation: kind + fingerprint must be short, plain identifiers so
# upstream aggregation has stable buckets. Reject anything that looks
# like it might leak content (whitespace, control chars, very long).
_VALID_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_.\-:]+$")
_MAX_TOKEN_LEN = 120


def _is_valid_token(s: object) -> bool:
    if not isinstance(s, str):
        return False
    if not s or len(s) > _MAX_TOKEN_LEN:
        return False
    # ``fullmatch`` (not ``match``) so a trailing newline doesn't slip
    # through — the regex's ``$`` anchor in Python tolerates a trailing
    # ``\n`` by default. JSONL parsing depends on no embedded newlines
    # in the tokens.
    return bool(_VALID_TOKEN_RE.fullmatch(s))


def _gaps_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "observations" / "tool_gaps.jsonl"


@dataclass(frozen=True)
class ToolGap:
    """One tool-gap record. Frozen so producers can't accidentally
    mutate after the writer normalizes timestamps."""

    kind: str
    scope: str
    fingerprint: str
    first_observed: str
    last_observed: str
    frequency_local: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ToolGap":
        return cls(
            kind=str(data.get("kind") or ""),
            scope=str(data.get("scope") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            first_observed=str(data.get("first_observed") or ""),
            last_observed=str(data.get("last_observed") or ""),
            frequency_local=int(data.get("frequency_local") or 1),
        )


@dataclass
class RecordResult:
    """Outcome of ``record_tool_gap``. ``ok`` means the record was
    accepted (existing fingerprint frequency incremented, or new
    record appended). ``new`` is True when this was a first
    observation of the fingerprint; False when an existing record
    had its frequency incremented."""

    ok: bool
    new: bool
    fingerprint: str
    frequency_local: int = 0
    error: str = ""


def record_tool_gap(
    shared_dir: Path,
    *,
    kind: str,
    scope: str,
    fingerprint: str,
) -> RecordResult:
    """Append a tool-gap record (or increment an existing one's frequency).

    Two-pass behavior:
    - If the fingerprint already exists in the file, ``last_observed``
      and ``frequency_local`` are updated by writing a fresh record at
      the end. We DON'T rewrite the prior record (that would require
      read-modify-write under a lock); aggregation handles dedup on
      read.
    - If new, append a new record with frequency_local=1 and
      first_observed == last_observed.

    Validation: kind / scope / fingerprint must be short plain
    identifiers (alphanumeric + `_`, `-`, `.`, `:`). Whitespace and
    control characters are rejected — better to drop a malformed gap
    record than to leak chat content into the telemetry stream.
    """
    if not _is_valid_token(kind):
        return RecordResult(ok=False, new=False, fingerprint="",
                            error=f"kind invalid or empty: {kind!r}")
    if not _is_valid_token(scope):
        return RecordResult(ok=False, new=False, fingerprint="",
                            error=f"scope invalid or empty: {scope!r}")
    if not _is_valid_token(fingerprint):
        return RecordResult(ok=False, new=False, fingerprint="",
                            error=f"fingerprint invalid or empty: {fingerprint!r}")

    path = _gaps_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _APPEND_LOCK:
        prior = _last_record_for_fingerprint(path, fingerprint)
        now = _now_iso()
        if prior is None:
            record = ToolGap(
                kind=kind,
                scope=scope,
                fingerprint=fingerprint,
                first_observed=now,
                last_observed=now,
                frequency_local=1,
            )
            new = True
            freq = 1
        else:
            record = ToolGap(
                kind=kind,
                scope=scope,
                fingerprint=fingerprint,
                first_observed=prior.first_observed,
                last_observed=now,
                frequency_local=prior.frequency_local + 1,
            )
            new = False
            freq = record.frequency_local

        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), sort_keys=True))
                fh.write("\n")
        except OSError as exc:
            log.exception("tool_gaps: append failed")
            return RecordResult(
                ok=False, new=False, fingerprint=fingerprint,
                error=f"write failed: {exc}",
            )

    return RecordResult(
        ok=True, new=new, fingerprint=fingerprint, frequency_local=freq,
    )


def _iter_records(path: Path) -> Iterable[ToolGap]:
    """Yield every record in the file, oldest-first. Skips malformed
    lines silently — the file is operator-readable but the format is
    JSONL; a hand-edit that breaks one line shouldn't block reads."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                yield ToolGap.from_dict(obj)
    except OSError:
        return


def _last_record_for_fingerprint(path: Path, fingerprint: str) -> ToolGap | None:
    """Return the most recent record matching ``fingerprint``, or None.

    Scans the whole file each time — fine for the volumes this tool
    targets (tens to hundreds of records per pod over months). If a
    pod's tool_gaps file ever grows past ~10k lines we'd revisit.
    """
    last: ToolGap | None = None
    for rec in _iter_records(path):
        if rec.fingerprint == fingerprint:
            last = rec
    return last


def read_tool_gaps(
    shared_dir: Path,
    *,
    limit: int = 50,
) -> list[ToolGap]:
    """Read aggregated gap records, newest-first.

    Aggregation: when the file has multiple records for one
    fingerprint (each increment appends a fresh line), the result
    returns ONE record per fingerprint — the most recent line, which
    already carries the current frequency_local and last_observed.

    ``limit`` caps the response. Clamped to [1, 1000] to keep
    response size bounded.
    """
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    path = _gaps_path(shared_dir)
    by_fp: dict[str, ToolGap] = {}
    # Iterate oldest-first; later overwrites earlier, leaving the
    # most-recent record per fingerprint.
    for rec in _iter_records(path):
        if not rec.fingerprint:
            continue
        by_fp[rec.fingerprint] = rec

    # Sort newest-first by last_observed (ISO8601 strings sort
    # lexicographically when timezone-uniform, which ours are).
    ordered = sorted(
        by_fp.values(),
        key=lambda r: r.last_observed,
        reverse=True,
    )
    return ordered[:limit]


def wipe_tool_gaps(shared_dir: Path) -> bool:
    """Delete the tool_gaps.jsonl file. Returns True if the file was
    present and removed, False otherwise. Idempotent — calling on
    an already-clean pod is a no-op success.

    Backs the future ``evolve-admin wipe-telemetry`` CLI.
    """
    path = _gaps_path(shared_dir)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        log.exception("tool_gaps: wipe failed")
        return False
