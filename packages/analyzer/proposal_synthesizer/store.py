"""proposal_synthesizer.store — Filesystem-backed candidate persistence.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §8.

Candidates live under ``{shared_dir}/candidates/{subdir}/{id}.json``
where ``subdir`` is one of:

  - ``pending``      — generator emitted; gate has not yet processed
  - ``synthesizing`` — passed the gate; awaiting synthesizer
  - ``watchlist``    — gate (or synthesizer) demoted; tracking, no
                       operator action

The ``state`` field on the JSON is authoritative; the subdir is a
physical index for efficient iteration.

Dropped candidates do NOT live as individual files — they append to
``{shared_dir}/candidates/dropped/<YYYY-MM-DD>.jsonl``. Same for the
synthesizer's decision log at ``synthesis_log/<YYYY-MM-DD>.jsonl``.

Atomic writes via temp-file + rename, owned by the ``evolve`` user
(no sudo needed — ``{shared_dir}/candidates/`` is created under the
shared_dir ACL).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from evolve_util import atomic_write_json as _atomic_write_json
from schema.candidate_proposal import CandidateProposal, CandidateState


Subdir = Literal["pending", "synthesizing", "watchlist"]

_STATE_TO_SUBDIR: dict[CandidateState, Subdir] = {
    "pending": "pending",
    "synthesizing": "synthesizing",
    "watchlist": "watchlist",
}

_ALL_SUBDIRS: tuple[Subdir, ...] = ("pending", "synthesizing", "watchlist")


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def candidates_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "candidates"


def candidate_path(shared_dir: Path, candidate_id: str, *, subdir: Subdir) -> Path:
    return candidates_root(shared_dir) / subdir / f"{candidate_id}.json"


def dropped_log_path(shared_dir: Path, *, date_iso: str | None = None) -> Path:
    day = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return candidates_root(shared_dir) / "dropped" / f"{day}.jsonl"


def synthesis_log_path(shared_dir: Path, *, date_iso: str | None = None) -> Path:
    day = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return candidates_root(shared_dir) / "synthesis_log" / f"{day}.jsonl"


def repetition_index_path(shared_dir: Path) -> Path:
    return candidates_root(shared_dir) / "repetition_index.json"


def subdir_for_state(state: CandidateState) -> Subdir:
    sd = _STATE_TO_SUBDIR.get(state)
    if sd is None:
        raise ValueError(f"unknown CandidateState: {state!r}")
    return sd


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write
# ─────────────────────────────────────────────────────────────────────────────


def _append_jsonl(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


# ─────────────────────────────────────────────────────────────────────────────
# Read / write primitives
# ─────────────────────────────────────────────────────────────────────────────


def write_candidate(
    candidate: CandidateProposal,
    shared_dir: Path,
    *,
    subdir: Subdir | None = None,
) -> Path:
    """Write a candidate to its state-derived subdir.

    Pass ``subdir`` to override (rare — used by :func:`move_candidate`
    when moving across subdirs).
    """
    target = subdir or subdir_for_state(candidate.state)
    path = candidate_path(shared_dir, candidate.id, subdir=target)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mode=0o644: cross-user-read concern — see arbiter.store on why
    # candidate and dropped-log files must be world-readable.
    _atomic_write_json(path, candidate.to_dict(), mode=0o644)
    return path


def load_candidate_file(path: Path) -> CandidateProposal | None:
    """Load a candidate from a specific path, or None if unreadable."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return CandidateProposal.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def find_candidate(
    shared_dir: Path, candidate_id: str
) -> tuple[CandidateProposal, Path, Subdir] | None:
    """Locate a candidate by id across subdirs."""
    for sd in _ALL_SUBDIRS:
        path = candidate_path(shared_dir, candidate_id, subdir=sd)
        cand = load_candidate_file(path)
        if cand is not None:
            return cand, path, sd
    return None


def delete_candidate(
    shared_dir: Path, candidate_id: str, *, subdir: Subdir
) -> bool:
    """Remove a candidate file from a subdir."""
    path = candidate_path(shared_dir, candidate_id, subdir=subdir)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def move_candidate(
    shared_dir: Path,
    candidate: CandidateProposal,
    *,
    from_subdir: Subdir,
    to_state: CandidateState,
) -> Path:
    """Update state, write to new subdir, remove from old.

    The candidate's ``state`` field is set to ``to_state`` before
    writing — the subdir and the state stay in sync.
    """
    candidate.state = to_state
    new_path = write_candidate(candidate, shared_dir)
    if subdir_for_state(to_state) != from_subdir:
        delete_candidate(shared_dir, candidate.id, subdir=from_subdir)
    return new_path


# ─────────────────────────────────────────────────────────────────────────────
# Iteration
# ─────────────────────────────────────────────────────────────────────────────


def iter_candidates(
    shared_dir: Path,
    *,
    subdirs: tuple[Subdir, ...] = ("pending",),
) -> Iterator[CandidateProposal]:
    """Yield every readable candidate from the given subdirs (sorted by id)."""
    root = candidates_root(shared_dir)
    for sd in subdirs:
        dir_path = root / sd
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.json")):
            cand = load_candidate_file(path)
            if cand is not None:
                yield cand


# ─────────────────────────────────────────────────────────────────────────────
# Append-only logs
# ─────────────────────────────────────────────────────────────────────────────


def record_drop(
    shared_dir: Path,
    candidate: CandidateProposal,
    *,
    reason: str,
    note: str = "",
    date_iso: str | None = None,
) -> None:
    """Append a drop record to today's dropped/<date>.jsonl.

    Dropped candidates do not survive as individual files. The log is
    the operator's view into "what almost surfaced" and the tuning
    surface for the gate.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate_id": candidate.id,
        "bot_id": candidate.bot_id,
        "generator_id": candidate.generator_id,
        "variant": candidate.variant,
        "fingerprint": candidate.fingerprint,
        "reason": reason,
        "note": note,
        "magnitude": (
            candidate.magnitude.to_dict() if candidate.magnitude else None
        ),
        "motivating_signals": list(candidate.motivating_signals),
    }
    _append_jsonl(record, dropped_log_path(shared_dir, date_iso=date_iso))
