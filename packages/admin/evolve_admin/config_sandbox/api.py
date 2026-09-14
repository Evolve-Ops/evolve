"""api — read-only sandbox surface (resolve + customizations).

Two entry points:

  resolve(path, *, bot_id=None, gen_id=None, ...) -> ResolvedValue
      Current effective value for one schema key. Walks bot-override,
      pod-default, native-store, schema-stock-default in that order so
      callers get a single answer regardless of where the value lives.

  customizations(*, bot_ids=None, gen_ids=None, ...) -> list[Customization]
      Every schema key whose current value diverges from its shipped
      default. This is what the "what has this install customized" UI
      renders.

The sandbox is read-only in step 2. Writes still go through each
backing store's existing applier or endpoint. The provenance index
(step 3) layers on top and the customization list will pick up
provenance metadata when present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .provenance import ProvenanceEntry, read_index
from .schema import SCHEMA, Store, Strength, TunableKey, by_path
from .stores import Found, default_shared_dir, read_value


@dataclass(frozen=True)
class ResolvedValue:
    """Result of ``resolve()``.

    Attributes:
        entry: schema entry that was resolved.
        value: current effective value.
        source: where the value came from — one of:
            "store"     — read from the native backing file.
            "default"   — file/key absent; using stock_default.
            "missing"   — entry needs a parameter the caller didn't supply
                          (e.g. per-bot key resolved without bot_id). value is None.
    """

    entry: TunableKey
    value: Any
    source: str


@dataclass(frozen=True)
class Customization:
    """One overridden schema entry.

    Attributes:
        entry: schema entry.
        current_value: what the install has now.
        stock_default: what shipped.
        bot_id: bot the override applies to (None for pod-wide).
        gen_id: generator the override applies to (None for non-generator).
        source_path: physical file the value was read from (None if it
            came from a non-file source).
        provenance: who/when/why this was set, if recorded. None means the
            divergence was authored outside the sandbox API (direct file edit).
        explicit_at_default: True when the current value equals the stock
            default but provenance shows the operator explicitly chose it.
            This is the case the cascade needs to preserve on upgrade.
    """

    entry: TunableKey
    current_value: Any
    stock_default: Any
    bot_id: str | None
    gen_id: str | None
    source_path: Path | None
    provenance: ProvenanceEntry | None = None
    explicit_at_default: bool = False


def resolve(
    path: str,
    *,
    bot_id: str | None = None,
    gen_id: str | None = None,
    shared_dir: Path | None = None,
    network_json: Path | None = None,
) -> ResolvedValue:
    """Return the current value for a schema key.

    ``path`` is a logical schema path (``TunableKey.path``). Returns the
    stock default if the backing store is missing or the key isn't set.
    """
    entry = by_path(path)
    if entry is None:
        raise KeyError(f"unknown sandbox path: {path}")

    sd = shared_dir or default_shared_dir()
    found = read_value(
        entry,
        shared_dir=sd,
        bot_id=bot_id,
        gen_id=gen_id,
        network_json=network_json,
    )

    if found.found:
        return ResolvedValue(entry=entry, value=found.value, source="store")

    # If the entry needs a parameter the caller didn't supply, signal that
    # explicitly rather than masking it as a default-fallback.
    if (entry.per_bot and bot_id is None) or (entry.per_generator and gen_id is None):
        return ResolvedValue(entry=entry, value=None, source="missing")

    return ResolvedValue(entry=entry, value=entry.stock_default, source="default")


def _is_overridden(current: Any, default: Any) -> bool:
    """Is the current value a divergence from the stock default?

    Equality semantics: dicts/lists compared structurally; everything else
    by ==. None equals None. Floats compared exactly (no epsilon — schema
    defaults are exact values, not measurements).
    """
    return current != default


def customizations(
    *,
    bot_ids: list[str] | None = None,
    gen_ids: list[str] | None = None,
    shared_dir: Path | None = None,
    network_json: Path | None = None,
    include_stores: set[str] | None = None,
) -> list[Customization]:
    """Walk the schema and return entries whose current value diverges
    from the stock default.

    Args:
        bot_ids: bots to expand per-bot keys against. None means skip all
            per-bot keys. Pass an empty list explicitly to be sure.
        gen_ids: generators to expand per-generator keys against.
        shared_dir: override the default shared_dir.
        network_json: override the default network.json path.
        include_stores: optional whitelist of Store names. None = all.
    """
    sd = shared_dir or default_shared_dir()
    prov_index = read_index(sd)
    out: list[Customization] = []

    for entry in SCHEMA:
        if include_stores is not None and entry.store not in include_stores:
            continue

        # Doc-typed identity entries don't have a stock_default to compare
        # against — they're "customized iff the file exists with content."
        if entry.type_hint == "doc":
            for bid in (bot_ids or ()):
                f = read_value(
                    entry, shared_dir=sd, bot_id=bid, network_json=network_json
                )
                if f.found and f.value:
                    prov = prov_index.get(entry.path, bot_id=bid)
                    out.append(
                        Customization(
                            entry=entry,
                            current_value=f.value,
                            stock_default=None,
                            bot_id=bid,
                            gen_id=None,
                            source_path=f.source_path,
                            provenance=prov,
                        )
                    )
            continue

        # Per-generator expansion
        if entry.per_generator:
            for gid in (gen_ids or ()):
                _maybe_record(out, entry, sd, prov_index,
                              bot_id=None, gen_id=gid, network_json=network_json)
            continue

        # Per-bot expansion
        if entry.per_bot:
            for bid in (bot_ids or ()):
                _maybe_record(out, entry, sd, prov_index,
                              bot_id=bid, gen_id=None, network_json=network_json)
            continue

        # Pod-wide / non-parameterized
        _maybe_record(out, entry, sd, prov_index,
                      bot_id=None, gen_id=None, network_json=network_json)

    return out


def _maybe_record(
    out: list[Customization],
    entry: TunableKey,
    shared_dir: Path,
    prov_index,
    *,
    bot_id: str | None,
    gen_id: str | None,
    network_json: Path | None,
) -> None:
    f: Found = read_value(
        entry,
        shared_dir=shared_dir,
        bot_id=bot_id,
        gen_id=gen_id,
        network_json=network_json,
    )
    prov = prov_index.get(entry.path, bot_id=bot_id, gen_id=gen_id)

    if not f.found:
        # Native value missing. If provenance claims it was set, that's stale
        # — operator deleted the key out-of-band. Don't surface as customized
        # (the value IS the default now); leave provenance for cleanup.
        return

    diverges = _is_overridden(f.value, entry.stock_default)
    if not diverges and prov is None:
        # Native value equals default and we have no record of an explicit
        # choice. Not customized.
        return

    explicit_at_default = (not diverges) and (prov is not None)

    out.append(
        Customization(
            entry=entry,
            current_value=f.value,
            stock_default=entry.stock_default,
            bot_id=bot_id,
            gen_id=gen_id,
            source_path=f.source_path,
            provenance=prov,
            explicit_at_default=explicit_at_default,
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Summary helper for UI / diagnostics
# ─────────────────────────────────────────────────────────────────────────────


def customization_summary(customizations_list: list[Customization]) -> dict:
    """Group a list of customizations by strength + store for display."""
    by_strength: dict[str, int] = {"free": 0, "advisory": 0, "shipped-policy": 0}
    by_store_name: dict[str, int] = {}

    for c in customizations_list:
        by_strength[c.entry.strength.label] += 1
        by_store_name[c.entry.store] = by_store_name.get(c.entry.store, 0) + 1

    return {
        "total": len(customizations_list),
        "by_strength": by_strength,
        "by_store": by_store_name,
    }
