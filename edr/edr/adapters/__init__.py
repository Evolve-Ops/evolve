"""edr.adapters — ingestion adapters: external dev-signal sources → Signals.

Each adapter is a thin, faithful normalizer from ONE external source into
the real imported ``signals.store`` (design: docs/edr/design-edr-2026-06-11.md
§4.3). Adapters are read-only against their source and additive against the
store. They do NOT classify — severity/priority judgment belongs to triage
(design §6), which consumes these Signals downstream.

Adapters are tier interfaces (docs/edr/design-edr-tiered-oversight-2026-06-11.md
§4 Move 1): typed, documented, schema'd, because the adapter/bridge shape
generalizes to later sources. Each adapter module documents its full
source-field → Signal-field mapping in its module docstring.

Shipped adapters:
  - ``github_issues`` — the repo's GitHub issues → ``dev_issue`` Signals.
"""
