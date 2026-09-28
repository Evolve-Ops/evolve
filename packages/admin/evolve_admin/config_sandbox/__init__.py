"""config_sandbox — declarative inventory + read API for per-install configuration.

Spec: internal/spec-config-sandbox-2026-05-09.md.

The sandbox is a *projection* layer over the seven existing backing stores
(network.json, better-engine-config.json, generators/<id>.json, openclaw.json,
evolve-tiers.json, bot_guides/<id>.md, SOUL.md/AGENTS.md/manifests/). It does
NOT replace those stores — they remain authoritative. The sandbox provides:

  - schema.SCHEMA       : every operator-tunable key with its strength label,
                          backing-store path, and shipped default.
  - resolve(key, ...)   : current value of a key (reads native store).
  - customizations(...) : list of keys whose current value diverges from the
                          shipped default — i.e. "what this install has
                          customized." (Coming in step 2.)

Strength labels (free / advisory / shipped-policy) carry the upgrade-propagation
contract: free overrides have no global signal, shipped-policy overrides mean
the install stops benefiting from telemetry-driven default tuning. See the spec.
"""

from __future__ import annotations

from .api import (
    Customization,
    ResolvedValue,
    customization_summary,
    customizations,
    resolve,
)
from .overrides import (
    BotOverrides,
    OverrideEntry,
    OverrideStateError,
    OverrideValidationError,
    delete_override,
    iter_all_overrides,
    mark_reviewed,
    overrides_dir,
    path_for_bot,
    read_bot_overrides,
    write_override,
)
from .provenance import (
    ProvenanceEntry,
    ProvenanceIndex,
    forget,
    lookup,
    read_index,
    record,
)
from .schema import (
    SCHEMA,
    Store,
    Strength,
    TunableKey,
    by_path,
    by_store,
)

__all__ = [
    "BotOverrides",
    "Customization",
    "OverrideEntry",
    "OverrideStateError",
    "OverrideValidationError",
    "ProvenanceEntry",
    "ProvenanceIndex",
    "ResolvedValue",
    "SCHEMA",
    "Store",
    "Strength",
    "TunableKey",
    "by_path",
    "by_store",
    "customization_summary",
    "customizations",
    "delete_override",
    "forget",
    "iter_all_overrides",
    "lookup",
    "mark_reviewed",
    "overrides_dir",
    "path_for_bot",
    "read_bot_overrides",
    "read_index",
    "record",
    "resolve",
    "write_override",
]
