"""provenance — side index recording who/when/why each sandbox key was set.

The provenance file is the answer to two questions the native stores can't
answer alone:

  1. "Did the operator explicitly choose this value?" Critical for upgrade
     propagation: when a native value happens to equal the current shipped
     default, was that an explicit choice or coincidence? Provenance lets
     us tell.
  2. "Where did this override come from?" Operator action, an RSI proposal,
     bot-setup endpoint, evo wizard. Helpful for debugging and for the
     customizations UI's narrative ("set by RSI proposal #abc on date X").

Native stores remain authoritative for VALUES. Provenance is sandbox-
internal METADATA. When someone edits a backing file directly (via sudo,
hand edit, OC's own tools writing openclaw.json), provenance simply has
no entry for that key — the customizations UI shows "differs from default,
source unknown" rather than lying.

File layout (single file, atomic temp+rename writes):

  {shared_dir}/sandbox/provenance.json

  {
    "schema_version": 1,
    "entries": {
      "<path>@<scope>": {
        "set_at": "ISO timestamp",
        "set_by": "operator|rsi:<proposal_id>|<endpoint_name>|...",
        "set_value": <serialised value>,
        "previous_default": <stock default at time of set>,
        "reason": "optional free-text"
      },
      ...
    }
  }

  scope := "pod" | "bot:<bot_id>" | "gen:<gen_id>"

Owned by the evolve user (no sudo dance) since it lives under shared_dir.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import TunableKey


PROVENANCE_FILENAME = "provenance.json"
PROVENANCE_SUBDIR = "sandbox"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProvenanceEntry:
    set_at: str                      # ISO 8601 UTC
    set_by: str                      # actor identifier
    set_value: Any                   # the value that was set
    previous_default: Any = None     # stock default at time of set
    reason: str | None = None        # optional rationale


# ─────────────────────────────────────────────────────────────────────────────
# Scope / composite keys
# ─────────────────────────────────────────────────────────────────────────────


def _scope(*, bot_id: str | None, gen_id: str | None) -> str:
    """Build the scope segment of a composite provenance key."""
    if bot_id and gen_id:
        # Schema entries are per_bot XOR per_generator. If a caller passes
        # both, prefer bot_id and ignore gen_id (the per_bot path wins).
        return f"bot:{bot_id}"
    if bot_id:
        return f"bot:{bot_id}"
    if gen_id:
        return f"gen:{gen_id}"
    return "pod"


def _composite_key(path: str, *, bot_id: str | None, gen_id: str | None) -> str:
    return f"{path}@{_scope(bot_id=bot_id, gen_id=gen_id)}"


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────


def _path(shared_dir: Path) -> Path:
    return shared_dir / PROVENANCE_SUBDIR / PROVENANCE_FILENAME


@dataclass
class ProvenanceIndex:
    schema_version: int = SCHEMA_VERSION
    entries: dict[str, ProvenanceEntry] = field(default_factory=dict)

    def get(
        self,
        path: str,
        *,
        bot_id: str | None = None,
        gen_id: str | None = None,
    ) -> ProvenanceEntry | None:
        return self.entries.get(_composite_key(path, bot_id=bot_id, gen_id=gen_id))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "entries": {k: asdict(v) for k, v in self.entries.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProvenanceIndex":
        raw = data.get("entries") or {}
        entries: dict[str, ProvenanceEntry] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:
                entries[k] = ProvenanceEntry(
                    set_at=str(v["set_at"]),
                    set_by=str(v["set_by"]),
                    set_value=v.get("set_value"),
                    previous_default=v.get("previous_default"),
                    reason=v.get("reason"),
                )
            except (KeyError, TypeError):
                # Malformed entry — skip rather than fail the whole load.
                continue
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            entries=entries,
        )


def read_index(shared_dir: Path) -> ProvenanceIndex:
    """Load the provenance index. Returns an empty index if the file is missing."""
    path = _path(shared_dir)
    if not path.exists():
        return ProvenanceIndex()
    try:
        return ProvenanceIndex.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ProvenanceIndex()


def _write_index(index: ProvenanceIndex, shared_dir: Path) -> Path:
    """Atomically write the provenance index. Creates the subdir if needed."""
    path = _path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index.to_dict(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Public write API
# ─────────────────────────────────────────────────────────────────────────────


def record(
    entry: TunableKey,
    value: Any,
    *,
    set_by: str,
    bot_id: str | None = None,
    gen_id: str | None = None,
    reason: str | None = None,
    shared_dir: Path,
    now: datetime | None = None,
) -> ProvenanceEntry:
    """Record provenance for a sandbox-driven set of ``entry`` to ``value``.

    Idempotent in the sense that re-recording for the same composite key
    overwrites the previous entry — provenance reflects the latest set,
    not history. (History lives in the arbiter store for RSI-driven
    changes; the operator UI doesn't need a parallel log.)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pe = ProvenanceEntry(
        set_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        set_by=set_by,
        set_value=value,
        previous_default=entry.stock_default,
        reason=reason,
    )

    index = read_index(shared_dir)
    key = _composite_key(entry.path, bot_id=bot_id, gen_id=gen_id)
    index.entries[key] = pe
    _write_index(index, shared_dir)
    return pe


def lookup(
    path: str,
    *,
    bot_id: str | None = None,
    gen_id: str | None = None,
    shared_dir: Path,
) -> ProvenanceEntry | None:
    """Return the provenance entry for a given key/scope, or None."""
    return read_index(shared_dir).get(path, bot_id=bot_id, gen_id=gen_id)


def forget(
    path: str,
    *,
    bot_id: str | None = None,
    gen_id: str | None = None,
    shared_dir: Path,
) -> bool:
    """Remove a provenance entry. Returns True if one was removed.

    Use when the operator reverts a sandbox-driven override (the value
    goes back to default, so we shouldn't keep claiming it was set).
    """
    index = read_index(shared_dir)
    key = _composite_key(path, bot_id=bot_id, gen_id=gen_id)
    if key not in index.entries:
        return False
    del index.entries[key]
    _write_index(index, shared_dir)
    return True
