"""metrics.registry — Named metrics + resolver protocol.

Spec: docs/archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md §5.

Each resolver lives in a module under ``metrics/resolvers/<name>.py`` and
registers itself by calling ``register(spec, resolver_callable)`` at
import time. The ``metrics`` package's ``__init__`` imports the resolvers
submodule to trigger those side effects.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol
from urllib.parse import parse_qsl, urlencode


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MetricSpec:
    """Static description of a metric."""

    name: str
    description: str
    unit: str
    source: str


@dataclass
class MetricValue:
    """Resolver output: value + confidence + optional debugging context."""

    value: float
    confidence: float = 1.0
    source_note: str = ""


class MetricResolver(Protocol):
    """A callable that resolves a metric to a MetricValue."""

    def __call__(
        self, bot_id: str, as_of: datetime
    ) -> MetricValue: ...  # pragma: no cover — protocol


ResolveFn = Callable[[str, datetime], MetricValue]


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


class UnknownMetricError(KeyError):
    """Raised when ``resolve()`` is called on an unknown metric."""


_REGISTRY: dict[str, tuple[MetricSpec, ResolveFn]] = {}


def register(spec: MetricSpec, resolver: ResolveFn) -> None:
    """Register a metric spec + resolver callable by name."""
    if spec.name in _REGISTRY:
        # Overwrite is permitted for testing; the second registration wins.
        # Modules do not attempt double-registration in production.
        pass
    _REGISTRY[spec.name] = (spec, resolver)


def unregister(name: str) -> None:
    """Remove a metric from the registry. Used only by tests."""
    _REGISTRY.pop(name, None)


def known() -> list[str]:
    """Return sorted list of registered metric names."""
    return sorted(_REGISTRY.keys())


def _split_params(name: str) -> tuple[str, dict[str, str]]:
    """Split a (possibly parameterized) metric name into base + params.

    Some metrics are *cell-scoped*: the same resolver serves many cells, with
    the cell identity carried as query params on the name — e.g.
    ``cluster.engagement_trend?noun=scheduling&verb=asked``. The base name is
    what's registered; the params are forwarded to the resolver. A plain name
    (no ``?``) yields an empty param dict, so unparameterized metrics are
    unaffected.
    """
    base, _, query = name.partition("?")
    params = dict(parse_qsl(query)) if query else {}
    return base, params


def parameterized(base: str, **params: str) -> str:
    """Build a parameterized metric name. Generators that emit cell-scoped
    claims should use this so encoding stays consistent with ``_split_params``.
    e.g. ``parameterized("cluster.engagement_trend", noun="scheduling", verb="asked")``.
    """
    return f"{base}?{urlencode(params)}" if params else base


def spec_of(name: str) -> MetricSpec:
    """Return the MetricSpec for ``name`` (params stripped) or raise."""
    base, _ = _split_params(name)
    if base not in _REGISTRY:
        raise UnknownMetricError(base)
    return _REGISTRY[base][0]


def resolve(name: str, bot_id: str, as_of: datetime) -> MetricValue:
    """Resolve a named metric for a bot at a timestamp.

    Supports cell-scoped names (``base?k=v&...``): the params are forwarded to
    the resolver as keyword arguments **only** for the keys it declares (or all,
    if it accepts ``**kwargs``), so 2-arg resolvers keep working unchanged.

    Raises ``UnknownMetricError`` if the base name isn't registered. Other
    exceptions propagate from the resolver; the verify daemon catches them and
    treats them as retriable.
    """
    base, params = _split_params(name)
    if base not in _REGISTRY:
        raise UnknownMetricError(base)
    _, resolver = _REGISTRY[base]
    if not params:
        return resolver(bot_id, as_of)
    sig = inspect.signature(resolver)
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    kwargs = {
        k: v for k, v in params.items() if accepts_var_kw or k in sig.parameters
    }
    return resolver(bot_id, as_of, **kwargs)


def clear_for_test() -> None:
    """Remove all registered metrics. Used only by tests."""
    _REGISTRY.clear()
