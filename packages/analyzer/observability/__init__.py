"""Evolve observability layer.

Narrow abstraction over Opik (the v1.5 observability substrate). Designed
so a different backend (OpenTelemetry, JSONL) can swap in without callers
changing — Opik is just today's concrete implementation.

Public surface (import from ``observability``, not ``observability.opik_client``):

    from observability import (
        ObservabilityClient,   # abstract base
        Span,                  # provider-neutral span shape (alias for OpikSpan)
        OpikSpan,              # same as Span; kept for backward compat
        SpanFilter,            # search filter
        OpikBackend,           # concrete: real Opik SDK + server
        JsonlBackend,          # concrete: filesystem fallback (always available)
        DisabledBackend,       # null-object backend
        get_client,            # config-driven factory
        span_to_cost_event,    # cost_ledger/cost_rollup projection helper
        observe_from_opik,     # signals.store adapter
    )

The file ``observability.opik_client`` remains the single implementation
file; consumers should import from this package (``observability``) so
that swapping to an OtelBackend is a one-file change in opik_client.py,
not a 6-file consumer refactor.

Per project_external_dependency_vetting (2026-05-12), Opik passed the bar
(Apache-2.0, fully self-hostable, very active). The documented fallback is
**OpenTelemetry / CNCF**, not a different LLM observability product — when
a swap is needed, write an ``OtelBackend`` against the same
``ObservabilityClient`` interface.

Per feedback_dont_reimplement_upstream: Opik IS the upstream; we don't
reimplement tracing. We wrap it thinly so our consumers (signals,
cost_ledger, capture monitors) speak a stable internal shape.
"""

from observability.opik_client import (  # noqa: F401
    DisabledBackend,
    JsonlBackend,
    ObservabilityClient,
    OpikBackend,
    OpikSpan,
    SpanFilter,
    get_client,
    span_to_cost_event,
)

# Provider-neutral alias — new code should import Span, not OpikSpan.
# OpikSpan is kept for backward compat; both names refer to the same class.
Span = OpikSpan

# observe_from_opik lives in signals.store to avoid a circular import
# (signals.store imports from observability, so we can't import it here).
# Consumers that need observe_from_opik should import it directly:
#   from signals.store import observe_from_opik
