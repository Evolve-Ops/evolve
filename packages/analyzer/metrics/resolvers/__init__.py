"""metrics.resolvers — Resolver modules registering themselves on import.

Import order: each submodule calls ``metrics.registry.register()`` at
module scope, so simply importing this package populates the registry.
"""

from metrics.resolvers import (  # noqa: F401
    gateway,
    openclaw_config,
    acl,
    plugin,
    launchd,
    users,
    version,
    cluster_metrics,  # L4
    cost_metrics,  # L6
    cache_metrics,  # L6 — prompt-cache invalidation ratio (cache_ttl_tuner)
    security,  # L6 — Security Warden event-rate metrics
)
