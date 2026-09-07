"""registry.registry — Generator discovery, loading, and mutation APIs.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §4.6.

Registry layout:
  - Charters live in code at ``packages/analyzer/generators/<id>/charter.yaml``.
  - Generator records live in shared state at ``{shared_dir}/generators/<id>.json``.

At startup, the registry:
  1. Enumerates the generators code directory.
  2. For each, loads the charter and computes its fingerprint.
  3. Loads the generator's record from shared state (or initializes if absent).
  4. Verifies fingerprint agreement. Mismatches cause the generator to fail
     to load; the operator must either update the record (manual action) or
     roll back the charter change.
  5. Exposes accessor/mutator APIs for the arbiter and daemons.

L1 ships the registry with zero generators loaded (no ``generators/``
code subdir yet). L2 adds Sysadmin Watchdog as the first real entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from registry.charter_loader import (
    CharterFingerprintMismatch,
    CharterLoadError,
    compute_charter_fingerprint,
    load_charter_from_yaml,
)
from schema.generator import (
    Charter,
    GeneratorRecord,
    GeneratorStatus,
    TrackRecord,
)


class RegistryError(Exception):
    """Base class for registry errors."""


class GeneratorNotFound(RegistryError):
    """Raised when a lookup references an unknown generator id."""


@dataclass
class LoadedGenerator:
    """A charter + record pair, verified to agree on fingerprint."""

    charter: Charter
    record: GeneratorRecord
    charter_path: Path


# ─────────────────────────────────────────────────────────────────────────────
# Record IO helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_record(record_path: Path) -> GeneratorRecord | None:
    """Load a GeneratorRecord from disk; return None if absent."""
    if not record_path.exists():
        return None
    try:
        with record_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(
            f"Failed to read generator record at {record_path}: {e}"
        ) from e
    try:
        return GeneratorRecord.from_dict(data)
    except (KeyError, ValueError, TypeError) as e:
        raise RegistryError(
            f"Generator record {record_path} has invalid structure: {e}"
        ) from e


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


class Registry:
    """Discovers generators, loads charters + records, and mediates mutations.

    Construction does not do any IO; call ``load_all()`` explicitly so
    errors surface predictably.
    """

    def __init__(
        self,
        *,
        generators_code_dir: Path,
        records_dir: Path,
    ) -> None:
        """
        Args:
            generators_code_dir: e.g. ``packages/analyzer/generators/``
            records_dir:         e.g. ``{shared_dir}/generators/``
        """
        self.generators_code_dir = Path(generators_code_dir)
        self.records_dir = Path(records_dir)
        self._loaded: dict[str, LoadedGenerator] = {}
        self._load_errors: dict[str, str] = {}

    # ── Loading ─────────────────────────────────────────────────────────────

    def load_all(self, *, strict: bool = False) -> dict[str, LoadedGenerator]:
        """Discover and load every generator in the code directory.

        Returns the dict of successfully-loaded generators keyed by id.

        When ``strict=True``, any load error raises ``RegistryError``. When
        ``strict=False`` (default), errors are collected in
        ``self._load_errors`` so the caller can decide what to do (typically:
        proceed with the subset that loaded, alert on the failures).

        L1 expects zero generators in production; this is mostly a no-op
        until L2.
        """
        self._loaded.clear()
        self._load_errors.clear()

        if not self.generators_code_dir.exists():
            # No generators directory yet — normal for L1
            return {}

        for entry in sorted(self.generators_code_dir.iterdir()):
            if not entry.is_dir():
                continue
            charter_path = entry / "charter.yaml"
            if not charter_path.exists():
                # Accept .yml fallback for convenience
                alt = entry / "charter.yml"
                if alt.exists():
                    charter_path = alt
                else:
                    continue  # Not a generator dir

            try:
                loaded = self._load_one(charter_path)
            except (CharterLoadError, CharterFingerprintMismatch, RegistryError) as e:
                self._load_errors[entry.name] = str(e)
                if strict:
                    raise
                continue
            self._loaded[loaded.charter.id] = loaded

        return dict(self._loaded)

    def _load_one(self, charter_path: Path) -> LoadedGenerator:
        """Load one generator: charter + record + fingerprint check."""
        # Read raw content for fingerprint (independent of YAML parser quirks)
        content = charter_path.read_text(encoding="utf-8")
        fingerprint = compute_charter_fingerprint(content)

        # Parse charter (this also validates structure)
        charter, _ = load_charter_from_yaml(charter_path)

        # Load or initialize record
        record_path = self.records_dir / f"{charter.id}.json"
        record = _load_record(record_path)

        if record is None:
            # First deployment — initialize record with the current fingerprint.
            record = GeneratorRecord(
                id=charter.id,
                charter_fingerprint=fingerprint,
                config={},
                state={},
                track_record=TrackRecord(),
                status="active",
                budget_policy=self._budget_policy_for_type(charter.type),
            )
            record_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(record_path, record.to_dict(), sort_keys=True)
        else:
            # Verify fingerprint agreement
            if record.charter_fingerprint != fingerprint:
                raise CharterFingerprintMismatch(
                    generator_id=charter.id,
                    expected=record.charter_fingerprint,
                    actual=fingerprint,
                    charter_path=charter_path,
                )

        return LoadedGenerator(
            charter=charter,
            record=record,
            charter_path=charter_path,
        )

    @staticmethod
    def _budget_policy_for_type(gen_type: str) -> str:
        return {
            "optimizer": "competitive",
            "guardian": "duty",
            "meta_guardian": "meta",
        }.get(gen_type, "competitive")

    # ── Accessors ───────────────────────────────────────────────────────────

    def get(self, generator_id: str) -> LoadedGenerator:
        """Return the loaded generator by id, or raise GeneratorNotFound."""
        loaded = self._loaded.get(generator_id)
        if loaded is None:
            raise GeneratorNotFound(
                f"generator {generator_id!r} not loaded"
                + (
                    f" (load error: {self._load_errors[generator_id]})"
                    if generator_id in self._load_errors
                    else ""
                )
            )
        return loaded

    def all_loaded(self) -> dict[str, LoadedGenerator]:
        """Return a copy of the dict of loaded generators keyed by id."""
        return dict(self._loaded)

    def active_generators(self) -> dict[str, LoadedGenerator]:
        """Return only generators whose record status is ``active``."""
        return {
            gid: lg
            for gid, lg in self._loaded.items()
            if lg.record.status == "active"
        }

    def load_errors(self) -> dict[str, str]:
        """Return a copy of the dict of load errors keyed by dir name."""
        return dict(self._load_errors)

    # ── Mutations ───────────────────────────────────────────────────────────

    def update_status(
        self,
        generator_id: str,
        new_status: GeneratorStatus,
        *,
        reason: str = "",
    ) -> None:
        """Set a generator's status (active/paused/quarantined) and persist."""
        loaded = self.get(generator_id)
        loaded.record.status = new_status
        if new_status == "quarantined":
            loaded.record.quarantine_reason = reason
        else:
            loaded.record.quarantine_reason = None
        self._persist_record(loaded)

    def update_track_record(
        self,
        generator_id: str,
        updater,
    ) -> None:
        """Apply ``updater(track_record) -> None`` and persist atomically.

        Example:
            def bump(tr):
                tr.proposals_emitted += 1
            registry.update_track_record("sysadmin_watchdog", bump)
        """
        loaded = self.get(generator_id)
        updater(loaded.record.track_record)
        self._persist_record(loaded)

    def update_config(
        self,
        generator_id: str,
        new_config: dict,
    ) -> None:
        """Replace a generator's config entirely.

        Intended for operator-driven or self-update flows. Both flows are
        expected to validate against invariants *before* calling this.
        """
        loaded = self.get(generator_id)
        loaded.record.config = dict(new_config)
        self._persist_record(loaded)

    def _persist_record(self, loaded: LoadedGenerator) -> None:
        record_path = self.records_dir / f"{loaded.charter.id}.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(record_path, loaded.record.to_dict(), sort_keys=True)
