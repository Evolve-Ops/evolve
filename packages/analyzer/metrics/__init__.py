"""metrics — Named metrics and resolvers (L2).

Spec: docs/archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md §5.

Exposes a registry of ``MetricSpec`` entries, each paired with a resolver
function. The verify daemon and generators consume this registry without
caring about the concrete resolver implementation.

Resolvers register themselves at import time by importing the resolvers
package — see ``metrics.resolvers``.
"""

from metrics.registry import (
    MetricSpec,
    MetricValue,
    MetricResolver,
    UnknownMetricError,
    known,
    register,
    resolve,
)

# Importing the resolvers package triggers registration side effects.
from metrics import resolvers as _resolvers  # noqa: F401

__all__ = [
    "MetricSpec",
    "MetricValue",
    "MetricResolver",
    "UnknownMetricError",
    "known",
    "register",
    "resolve",
]
