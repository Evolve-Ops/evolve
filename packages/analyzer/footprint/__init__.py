"""footprint — Evolve's install-footprint registry (the META:footprint substrate).

Today this package carries ONE thing: the **auto-generated output declaration
contract** (``components`` module) — the F-4 forward-discipline gate that stops
new write-only disk sediment from being introduced (the audit found 537 MB of
audit records that nothing reads because no mechanism required the producer to
declare a consumer or retention).

It is deliberately the seed of the larger ``FOOTPRINT_COMPONENTS`` registry that
the F-3 posture-dial engine will build out (see
``docs/spec-footprint-2026-06-18.md`` § "Net F-3 build shape"). F-3 EXTENDS the
:class:`~footprint.components.FootprintComponent` node here with the dependency
graph fields (``kind``/``classification``/``requires``/``footprint_dims``/…); it
does not replace it. Keep this the single home so the two efforts converge rather
than fork into parallel registries.
"""

from __future__ import annotations
