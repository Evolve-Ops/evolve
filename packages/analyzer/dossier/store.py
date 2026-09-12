"""dossier.store — where editions live, and the rules that govern them.

Layout::

    {shared_dir}/dossier/editions/<YYYY>-W<WW>.json   the measurement
    {shared_dir}/dossier/modules/<YYYY>-W<WW>.json    what it means, in English

One file per ISO week in each, mode 0644 (operator-readable, the annotations
discipline), written atomically. The store is **operator-side only**: it
lives under ``{shared_dir}``, which the ``evolve`` user owns, so no ``/tmp``
staging or ``sudo`` dance is needed. The dossier design's privacy cut binds
future READERS of this data; this writer's job is to be readable by the
operator's own tooling and nobody else's.

Rule 1 — a run writes exactly ONE file per kind. The week it was asked for.
It never rewrites a neighbouring week, never rebuilds the directory, never
prunes as a side effect of writing.

Rule 2 — a SEALED edition is immutable. An edition computed over a window
that had already fully elapsed carries ``sealed: true``; :func:`write_edition`
refuses to overwrite one unless the caller passes ``force=True`` (the
operator's ``--force``). An unsealed edition — this week's, still open — is
freely overwritten, which is what makes an in-week re-run idempotent.

Rule 3 — a MODULE SET is not sealed, ever, and the asymmetry is the point.
A module set is a RENDERING of an edition: headlines, trends, and the values
they speak about, all derived from numbers the edition already recorded. The
measurement is immutable; the wording is not. So better wording — or a fixed
synthesis bug — can be regenerated over a sealed week
(``dossier_edition.py --modules-only``) without ``--force`` and without ever
discarding a recorded measurement.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json

from dossier.window import EDITION_ID_RE

#: Operator-readable. Matches ``usage-by-app.json`` / the cost rollups: these
#: files are read by other tooling running as other users, and a 0600 rollup
#: is a file only its writer can ever use.
EDITION_MODE = 0o644

#: Weekly editions at ~10-40 KB each: 52/year, ~1-2 MB/year. Five years is
#: the declared bound (``footprint.components``) — long enough that the spine
#: is a real longitudinal record, finite enough to be a declarable budget.
DEFAULT_RETENTION_YEARS = 5


class SealedEditionError(RuntimeError):
    """Raised when a write would overwrite a sealed (complete-window) edition."""


def dossier_root(shared_dir: Path | str) -> Path:
    return Path(shared_dir) / "dossier"


def editions_dir(shared_dir: Path | str) -> Path:
    return dossier_root(shared_dir) / "editions"


def modules_dir(shared_dir: Path | str) -> Path:
    return dossier_root(shared_dir) / "modules"


def edition_path(shared_dir: Path | str, edition_id: str) -> Path:
    return editions_dir(shared_dir) / f"{edition_id}.json"


def modules_path(shared_dir: Path | str, edition_id: str) -> Path:
    return modules_dir(shared_dir) / f"{edition_id}.json"


def load_edition(shared_dir: Path | str, edition_id: str) -> dict[str, Any] | None:
    """One edition, or ``None`` when absent/unreadable/not an object.

    ``None`` means "no edition on disk", never "an empty week" — the same
    tri-state discipline the payload itself follows.
    """
    return _read_json(edition_path(shared_dir, edition_id))


def load_modules(shared_dir: Path | str, edition_id: str) -> dict[str, Any] | None:
    """One module set, or ``None`` when absent/unreadable/not an object."""
    return _read_json(modules_path(shared_dir, edition_id))


def iter_edition_ids(shared_dir: Path | str) -> list[str]:
    """Every edition id present on disk, chronologically."""
    return _iter_week_ids(editions_dir(shared_dir))


def iter_module_ids(shared_dir: Path | str) -> list[str]:
    """Every module-set id present on disk, chronologically.

    Added by the Pod Intelligence reader, which is the caller this function
    waited for: the page lists the weeks on record and reads a module's
    recorded number out of earlier weeks to draw its trend. Separate from
    :func:`iter_edition_ids` because the two directories can legitimately
    differ — a week whose module set was pruned, or a sealed edition whose
    wording is being regenerated — and a reader that asked the editions
    directory what it can SAY would be asking the wrong file.
    """
    return _iter_week_ids(modules_dir(shared_dir))


def _iter_week_ids(directory: Path) -> list[str]:
    """ISO-week-named files in ``directory``, chronologically.

    Shared by the editions listing, the module-set listing, and both prunes.

    Filenames that are not ISO week ids are skipped rather than raised on:
    the directory is operator-visible and an operator's stray copy of a file
    must not break the writer.
    """
    try:
        names = sorted(p.stem for p in directory.glob("*.json"))
    except OSError:
        return []
    return [n for n in names if EDITION_ID_RE.match(n)]


def load_prior_editions(
    shared_dir: Path | str,
    edition_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Up to ``limit`` editions strictly BEFORE ``edition_id``, newest-first.

    The trend layer's only window onto history. Bounded on purpose: a spine
    holds 260 editions at the five-year steady state, and a weekly writer has
    no business reading all of them to answer "how does this compare with
    last week". Ids sort lexicographically in chronological order (that is
    why the week is zero-padded), so ``< edition_id`` is the comparison.
    """
    if limit <= 0:
        return []
    earlier = [e for e in iter_edition_ids(shared_dir) if e < edition_id]
    out: list[dict[str, Any]] = []
    for eid in reversed(earlier[-limit:]):
        payload = load_edition(shared_dir, eid)
        if payload is not None:
            out.append(payload)
    return out


def write_edition(
    shared_dir: Path | str,
    payload: dict[str, Any],
    *,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    """Write ONE edition atomically at 0644. Returns the path.

    Refuses to overwrite an edition already on disk with ``sealed: true``
    unless ``force``. The refusal is deliberately an exception rather than a
    silent skip: a scheduled run that quietly declines to write is how a
    spine develops a hole nobody notices for a month.
    """
    eid = str(payload.get("edition_id") or "")
    if not EDITION_ID_RE.match(eid):
        raise ValueError(f"payload carries no valid edition_id: {eid!r}")
    out = edition_path(shared_dir, eid)

    if not force:
        # Through ``load_edition`` rather than a second private read: the
        # store has ONE reader, and the seal check is the first caller of it.
        existing = load_edition(shared_dir, eid)
        if existing is not None and existing.get("sealed") is True:
            raise SealedEditionError(
                f"{out} is sealed (its window was already complete when it was "
                f"written) — editions are immutable once sealed. Pass --force "
                f"only when you intend to discard the recorded measurement."
            )

    if dry_run:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys: the spine is diffed by operators and by future readers
    # comparing consecutive editions; a stable key order makes those diffs
    # about the numbers rather than about dict insertion order.
    atomic_write_json(out, payload, indent=2, sort_keys=True, mode=EDITION_MODE)
    return out


def write_modules(
    shared_dir: Path | str,
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
) -> Path:
    """Write ONE module set atomically at 0644. Returns the path.

    No seal check and no ``force``: see Rule 3 in the module docstring. A
    module set is the wording of a measurement, not the measurement, so
    rewriting one loses nothing that is not reproducible from the edition
    beside it.
    """
    eid = str(payload.get("edition_id") or "")
    if not EDITION_ID_RE.match(eid):
        raise ValueError(f"payload carries no valid edition_id: {eid!r}")
    out = modules_path(shared_dir, eid)
    if dry_run:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, payload, indent=2, sort_keys=True, mode=EDITION_MODE)
    return out


def prune_editions(
    shared_dir: Path | str,
    *,
    keep_years: int = DEFAULT_RETENTION_YEARS,
    dry_run: bool = False,
) -> list[str]:
    """Drop editions older than ``keep_years`` whole years. Returns the ids.

    The declared retention pruner for the ``dossier_spine`` footprint
    component. Bounded by construction anyway (52 files a year), but the
    output contract's rule is that a bound must be *enforced* somewhere, not
    merely argued — so it is enforced here and called from every run.
    """
    return _prune_week_dir(editions_dir(shared_dir), keep_years, dry_run)


def prune_modules(
    shared_dir: Path | str,
    *,
    keep_years: int = DEFAULT_RETENTION_YEARS,
    dry_run: bool = False,
) -> list[str]:
    """The same retention, applied to the module sets. Returns the ids.

    Separate from :func:`prune_editions` rather than folded into it so each
    declared output has its own named pruner — the footprint contract's unit
    is the output, not the component, and a shared pruner would leave one of
    the two globs pointing at a function that does not mention it.
    """
    return _prune_week_dir(modules_dir(shared_dir), keep_years, dry_run)


def _prune_week_dir(directory: Path, keep_years: int, dry_run: bool) -> list[str]:
    ids = _iter_week_ids(directory)
    if not ids or keep_years <= 0:
        return []
    # Newest file on disk anchors the horizon rather than the wall clock: a
    # pod that has been off for a year must not lose its whole spine on the
    # first run back.
    newest_year = int(ids[-1][:4])
    cutoff_year = newest_year - keep_years
    dropped: list[str] = []
    for eid in ids:
        if int(eid[:4]) > cutoff_year:
            continue
        dropped.append(eid)
        if dry_run:
            continue
        try:
            (directory / f"{eid}.json").unlink()
        except OSError:
            # A file we cannot delete is not a reason to abandon the rest of
            # the prune, and never a reason to fail the week's write.
            dropped.pop()
    return dropped


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
