"""observability.opik_client — narrow abstraction over Opik tracing.

Design goals (per v1.5 sprint and project_external_dependency_vetting):

1. **Provider-neutral surface.** Callers consume :class:`OpikSpan`, an
   internal dataclass. The actual Opik SDK is touched only inside
   :class:`OpikBackend`. Swapping to OpenTelemetry (the documented
   fallback if Opik project health declines) means writing one new
   ``OtelBackend`` and changing ``get_client()`` — no consumer
   refactor.

2. **Optional dependency.** The ``opik`` package is NOT a hard
   dependency of evolve-admin or the analyzer. It is imported lazily
   inside :class:`OpikBackend.__init__` so analyzer code paths that
   never touch observability load fine without it. On systems without
   Opik installed or configured, :func:`get_client` returns the
   :class:`JsonlBackend` — a filesystem-backed fallback that always
   works. This satisfies the "Plex test" (operator-installable
   without first installing Opik) while leaving the Opik integration
   ready when the operator turns it on.

3. **Thin wrapper.** Per project_external_dependency_vetting criterion
   #5 (wrapper depth): every method here delegates to one or two SDK
   calls. If Opik's API changes, the blast radius is this file only.

Opik SDK reference (verified 2026-05-12 against the GitHub source —
sdks/python/src/opik/api_objects/opik_client.py):

    class Opik:
        def __init__(
            self,
            project_name: Optional[str] = None,
            workspace: Optional[str] = None,
            host: Optional[str] = None,
            api_key: Optional[str] = None,
            batching: bool = True,
            ...
        )

        def trace(
            self, id=..., name=..., start_time=..., end_time=...,
            input=..., output=..., metadata=..., tags=...,
            project_name=..., error_info=..., thread_id=..., ...
        ) -> Trace

        def span(
            self, trace_id=..., id=..., parent_span_id=..., name=...,
            type=..., start_time=..., end_time=..., metadata=...,
            input=..., output=..., tags=...,
            usage=...,                # Dict[str, Any] | OpikUsage
            project_name=..., model=..., provider=..., error_info=...,
            total_cost=...,           # float — direct cost attribution
            ...
        ) -> Span

REST search API (rest_api/traces/client.py::get_traces_by_project):

    def get_traces_by_project(
        self, *, page=None, size=None,
        project_name=None, project_id=None,
        filters=None, truncate=None, strip_attachments=None,
        sorting=None, exclude=None, search=None,
        from_time: dt.datetime = None, to_time: dt.datetime = None,
    )

We expose only what consumers actually need:
  * record_span(span)     — write
  * search_spans(...)     — read (filter by project/time/tags/error_only)
  * close()               — flush + tear down

The internal :class:`OpikSpan` is what every consumer sees. It carries
all the fields the existing Evolve monitors and cost rollups depend on,
so callers stay readable.

Configuration (read from network.json or env):

    "observability": {
        "backend": "opik" | "jsonl" | "disabled",
        "opik": {
            "host": "http://localhost:5173",   # default Opik server URL
            "api_key": null,                   # optional for self-host
            "project_name": "evolve",
            "workspace": "default"
        }
    }

If the block is absent, the default is ``jsonl`` (always works, no
dependency). Setting ``OPIK_DISABLED=1`` in the environment forces
``disabled``.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Per-record timeout infrastructure
# ─────────────────────────────────────────────────────────────────────────────

#: Default timeout (seconds) for a single record_span call.
#: Exceeding this limit causes the span to be dropped (best-effort contract).
#: Set to None to disable the cap (useful in tests that control timing).
RECORD_SPAN_TIMEOUT_SECONDS: float = 2.0

# Module-level executor — lazy-initialized, shared across all backend instances
# so we don't pay thread-creation cost per record call.
_record_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Warn-once flag: don't flood stderr if the backend is persistently slow.
_record_timeout_warned: bool = False


def _get_record_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the module-level record executor (lazy init)."""
    global _record_executor
    if _record_executor is None:
        _record_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="evolve_obs_record"
        )
    return _record_executor


def _record_with_timeout(fn: Any, span: "OpikSpan") -> None:
    """Call ``fn(span)`` with a timeout cap.

    If the call exceeds ``RECORD_SPAN_TIMEOUT_SECONDS``, it is abandoned
    (the span is dropped) and a one-time warning is printed to stderr.
    This prevents a slow observability backend from back-pressuring the
    producer (e.g. a gateway-level span emitter on a hot path).
    """
    global _record_timeout_warned
    if RECORD_SPAN_TIMEOUT_SECONDS is None:
        fn(span)
        return

    executor = _get_record_executor()
    future = executor.submit(fn, span)
    try:
        future.result(timeout=RECORD_SPAN_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        if not _record_timeout_warned:
            _record_timeout_warned = True
            print(
                f"[observability] WARN: record_span exceeded {RECORD_SPAN_TIMEOUT_SECONDS}s "
                f"timeout; span dropped. Backend may be slow. "
                f"(This warning fires once per process.)",
                file=sys.stderr,
            )
        # Don't raise — best-effort contract: the producer must not block.
    except Exception:
        # The underlying fn already swallows exceptions; this is a safety net.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Provider-neutral span shape
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OpikSpan:
    """Internal canonical span shape.

    Mirrors the union of fields the real Opik SDK accepts on ``span()``
    plus the trace/parent linkage. Capture monitors read ONLY this
    dataclass — they never import ``opik`` directly. That keeps
    ``opik_client.py`` as the single file that knows about the SDK.

    Required:
      * ``name`` — operation name (e.g. ``"embedding_call"``,
        ``"bot_session_turn"``).
      * ``start_time`` / ``end_time`` — UTC datetimes; both must be set.

    Optional and consumer-relevant:
      * ``type`` — ``"general" | "tool" | "llm" | "guardrail"``
        (matches Opik's SpanType literal).
      * ``model`` / ``provider`` — populated for LLM spans; cost_ledger
        groups by these.
      * ``total_cost`` — per-call cost in USD (Opik supports this
        directly on ``span()``). The retired blocker "wait for upstream
        cost events" is closed because spans now carry cost natively.
      * ``usage`` — token counts dict with input/output/cache fields
        (cost_ledger and cost_rollup read these).
      * ``error_info`` — set to a non-empty dict when the call failed;
        capture monitors (embedding_monitor, reporter, evolve_watchdog)
        observe errored spans into Signals.
      * ``tags`` — free-form labels for filtering at search time.
      * ``attributes`` — Evolve-specific fields that the SDK passes
        through as ``metadata``. Carries ``bot_id``, ``producer``, etc.

    ``trace_id`` and ``parent_span_id`` are optional — set when this
    span is part of a longer trace (e.g. a bot turn that fans out into
    embedding calls). If a producer just records a one-off event, leave
    them ``None`` and the backend creates an implicit trace.
    """

    name: str
    start_time: datetime
    end_time: datetime

    type: str = "general"           # "general" | "tool" | "llm" | "guardrail"
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    model: str | None = None
    provider: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost: float | None = None

    error_info: dict[str, Any] | None = None

    # Evolve-side conveniences. These get folded into ``metadata`` when
    # we hand the span to the real Opik SDK, but consumers find them
    # easier to read off the dataclass directly.
    bot_id: str | None = None
    producer: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    # ── Serialisation ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (datetimes → ISO strings)."""
        d = asdict(self)
        d["start_time"] = self.start_time.astimezone(timezone.utc).isoformat()
        d["end_time"] = self.end_time.astimezone(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OpikSpan":
        """Inverse of :meth:`to_dict`. Tolerant of missing optional keys."""
        def _parse_dt(v: Any) -> datetime:
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            s = str(v)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).astimezone(timezone.utc)

        return cls(
            name=raw["name"],
            start_time=_parse_dt(raw["start_time"]),
            end_time=_parse_dt(raw["end_time"]),
            type=raw.get("type", "general"),
            trace_id=raw.get("trace_id"),
            span_id=raw.get("span_id"),
            parent_span_id=raw.get("parent_span_id"),
            input=dict(raw.get("input") or {}),
            output=dict(raw.get("output") or {}),
            metadata=dict(raw.get("metadata") or {}),
            tags=list(raw.get("tags") or []),
            model=raw.get("model"),
            provider=raw.get("provider"),
            usage=dict(raw.get("usage") or {}),
            total_cost=raw.get("total_cost"),
            error_info=(
                dict(raw["error_info"])
                if isinstance(raw.get("error_info"), dict) and raw.get("error_info")
                else None
            ),
            bot_id=raw.get("bot_id"),
            producer=raw.get("producer"),
            attributes=dict(raw.get("attributes") or {}),
        )

    def duration_seconds(self) -> float:
        """End - start in seconds (always non-negative; clamps to 0)."""
        delta = (self.end_time - self.start_time).total_seconds()
        return max(0.0, delta)

    def is_error(self) -> bool:
        """True iff ``error_info`` is a non-empty dict."""
        return bool(self.error_info)


# ─────────────────────────────────────────────────────────────────────────────
# Search filter
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SpanFilter:
    """Narrow search filter; every field is optional / additive.

    Used by :meth:`ObservabilityClient.search_spans`. Backends translate
    this into their native query (Opik: REST ``filters`` string;
    JSONL: in-memory ``filter()``). Consumers shouldn't need anything
    richer — if they do, add a field here, not a backend-specific
    escape hatch.
    """

    producer: str | None = None
    bot_id: str | None = None
    span_type: str | None = None
    name: str | None = None
    tags: list[str] = field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None
    error_only: bool = False
    limit: int = 1000

    def matches(self, span: OpikSpan) -> bool:
        """In-memory predicate used by :class:`JsonlBackend`."""
        if self.producer is not None and span.producer != self.producer:
            return False
        if self.bot_id is not None and span.bot_id != self.bot_id:
            return False
        if self.span_type is not None and span.type != self.span_type:
            return False
        if self.name is not None and span.name != self.name:
            return False
        if self.tags:
            span_tags = set(span.tags)
            if not all(t in span_tags for t in self.tags):
                return False
        if self.since is not None and span.end_time < self.since:
            return False
        if self.until is not None and span.start_time > self.until:
            return False
        if self.error_only and not span.is_error():
            return False
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────────────────


class ObservabilityClient(ABC):
    """Backend-neutral observability surface.

    Today: Opik (production) or JSONL (fallback). Tomorrow:
    OpenTelemetry — write ``OtelBackend(ObservabilityClient)`` and have
    :func:`get_client` return it for ``backend == "otel"``.
    """

    backend_name: str = "abstract"

    @abstractmethod
    def record_span(self, span: OpikSpan) -> None:
        """Persist a span. Best-effort: never raises into the caller."""
        ...

    @abstractmethod
    def search_spans(self, query: SpanFilter) -> Iterator[OpikSpan]:
        """Yield spans matching ``query`` (most recent first)."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Flush buffers and tear down. Idempotent."""
        ...

    # ── Convenience: minimum-viable trace ──────────────────────────────

    def record_event(
        self,
        *,
        name: str,
        producer: str,
        bot_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        attributes: dict[str, Any] | None = None,
        tags: Iterable[str] | None = None,
        error_info: dict[str, Any] | None = None,
        model: str | None = None,
        provider: str | None = None,
        total_cost: float | None = None,
        usage: dict[str, Any] | None = None,
    ) -> OpikSpan:
        """One-shot event recording.

        Convenience for capture monitors that emit point-in-time spans
        (e.g. embedding failure detected at parse-time). Builds an
        :class:`OpikSpan`, records it, and returns the span (so the
        caller can pass it on to the signal-store adapter).
        """
        now = datetime.now(timezone.utc)
        st = start_time or now
        et = end_time or now
        span = OpikSpan(
            name=name,
            start_time=st,
            end_time=et,
            producer=producer,
            bot_id=bot_id,
            tags=list(tags) if tags else [],
            error_info=error_info if error_info else None,
            model=model,
            provider=provider,
            total_cost=total_cost,
            usage=dict(usage or {}),
            attributes=dict(attributes or {}),
        )
        self.record_span(span)
        return span


# ─────────────────────────────────────────────────────────────────────────────
# Concrete: Opik
# ─────────────────────────────────────────────────────────────────────────────


class OpikBackend(ObservabilityClient):
    """Concrete backend that talks to a self-hosted Opik server.

    ``opik`` is lazy-imported; if the package isn't installed,
    construction raises :class:`ImportError`. :func:`get_client`
    catches that and falls back to :class:`JsonlBackend`.

    The Opik SDK is itself best-effort about transport errors — it
    buffers writes and retries; transient server unavailability does
    not raise out of ``span()``. Read paths (``search_spans``) do raise,
    so we wrap them and yield empty on transport errors. Capture
    monitors are designed to tolerate empty result sets.
    """

    backend_name = "opik"

    def __init__(
        self,
        *,
        host: str | None = None,
        api_key: str | None = None,
        project_name: str = "evolve",
        workspace: str | None = None,
    ) -> None:
        # Lazy import — keeps opik optional.
        try:
            import opik  # type: ignore
            from opik import Opik  # type: ignore
        except ImportError as e:
            raise ImportError(
                "opik package not installed. Install with `pip install opik` "
                "or configure observability.backend = 'jsonl'."
            ) from e

        # Allow env-var overrides (matches Opik's own convention).
        host = host or os.environ.get("OPIK_URL_OVERRIDE") or os.environ.get(
            "OPIK_HOST"
        )
        api_key = api_key or os.environ.get("OPIK_API_KEY")
        workspace = workspace or os.environ.get("OPIK_WORKSPACE")

        # Keep the raw opik module handy for the REST search API.
        self._opik_module = opik
        self._project_name = project_name

        kwargs: dict[str, Any] = {"project_name": project_name}
        if host:
            kwargs["host"] = host
        if api_key:
            kwargs["api_key"] = api_key
        if workspace:
            kwargs["workspace"] = workspace
        self._client = Opik(**kwargs)

    def record_span(self, span: OpikSpan) -> None:
        """Convert ``OpikSpan`` → Opik SDK call.

        Strategy: one span = one trace. Most Evolve monitors emit
        point-in-time events with no parent — wrapping each in its own
        trace keeps the SDK call minimal and avoids needing the caller
        to manage trace lifetimes.

        If the caller DOES provide a ``trace_id``, we attach the span
        to that trace via ``client.span(trace_id=...)`` per the real
        SDK signature.

        The call is executed with a configurable timeout cap
        (``RECORD_SPAN_TIMEOUT_SECONDS``). If the backend is slow (e.g.
        Opik server under load), the span is dropped rather than blocking
        the producer. A one-time warning is emitted to stderr.
        """
        _record_with_timeout(self._record_span_inner, span)

    def _record_span_inner(self, span: OpikSpan) -> None:
        """Actual SDK call — executed inside the timeout wrapper."""
        # Fold Evolve-specific fields into metadata so the SDK sees one
        # uniform shape.
        meta = dict(span.metadata or {})
        if span.bot_id is not None:
            meta.setdefault("bot_id", span.bot_id)
        if span.producer is not None:
            meta.setdefault("producer", span.producer)
        if span.attributes:
            meta.setdefault("attributes", dict(span.attributes))

        try:
            if span.trace_id is not None:
                # Attach to an existing trace.
                self._client.span(
                    trace_id=span.trace_id,
                    id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    name=span.name,
                    type=span.type,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    input=dict(span.input or {}) or None,
                    output=dict(span.output or {}) or None,
                    metadata=meta or None,
                    tags=list(span.tags) or None,
                    usage=dict(span.usage or {}) or None,
                    model=span.model,
                    provider=span.provider,
                    error_info=dict(span.error_info or {}) or None,
                    total_cost=span.total_cost,
                )
            else:
                # No trace yet — open a trace and put the span inside.
                trace = self._client.trace(
                    name=span.name,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    input=dict(span.input or {}) or None,
                    output=dict(span.output or {}) or None,
                    metadata=meta or None,
                    tags=list(span.tags) or None,
                    error_info=dict(span.error_info or {}) or None,
                )
                self._client.span(
                    trace_id=trace.id,
                    name=span.name,
                    type=span.type,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    input=dict(span.input or {}) or None,
                    output=dict(span.output or {}) or None,
                    metadata=meta or None,
                    tags=list(span.tags) or None,
                    usage=dict(span.usage or {}) or None,
                    model=span.model,
                    provider=span.provider,
                    error_info=dict(span.error_info or {}) or None,
                    total_cost=span.total_cost,
                )
        except Exception:
            # Best-effort. The SDK buffers; transport hiccups should
            # not break the producer. Drop silently — there's no good
            # universal fallback (logging it loops if the logger calls
            # back into observability).
            return

    def search_spans(self, query: SpanFilter) -> Iterator[OpikSpan]:
        """Query spans via the REST traces endpoint, convert back to ``OpikSpan``.

        Opik's traces endpoint is the indexed read surface; spans are
        nested under traces. For our monitors' use cases ("show me
        recent embedding-error spans"), a trace == span (we created
        them 1:1 in :meth:`record_span`), so we just iterate the
        project's recent traces and convert each into an ``OpikSpan``
        view.

        Errors from the REST API are swallowed and produce an empty
        iterator — monitors tolerate empty result sets and the JSONL
        fallback is always available.
        """
        try:
            rest = self._opik_module.rest_client()  # type: ignore[attr-defined]
        except Exception:
            return iter(())

        # Build the page params Opik accepts.
        page_size = max(1, min(query.limit, 500))
        from_time = query.since
        to_time = query.until

        try:
            response = rest.traces.get_traces_by_project(
                project_name=self._project_name,
                size=page_size,
                from_time=from_time,
                to_time=to_time,
            )
        except Exception:
            return iter(())

        # The REST response shape varies by SDK version; the public
        # surface is ``response.content`` (a list of trace records).
        traces = []
        try:
            traces = list(getattr(response, "content", None) or [])
        except Exception:
            traces = []

        # Warn when the response was non-None but we extracted zero traces.
        # This catches future Opik releases that rename .content → .data or
        # .items — the response object will have data but our extractor will
        # return nothing, which is a silent operator-trust failure.
        if not traces and response is not None:
            # Only fire when the response *looks* non-empty (i.e. it has
            # attributes beyond the standard ones, or .content was explicitly
            # None/empty rather than the response object itself being None).
            raw_content = getattr(response, "content", None)
            if raw_content is None:
                # .content attribute is absent — likely a field rename.
                available = [
                    a for a in dir(response) if not a.startswith("_")
                ][:10]
                print(
                    f"[opik_client] WARN: response received but .content empty/missing; "
                    f"available attrs: {available}",
                    file=sys.stderr,
                )

        # Convert + apply the in-memory filter for fields Opik's
        # ``filters`` DSL doesn't directly express in a stable way (we
        # want filter-portability across versions).
        def _iter() -> Iterator[OpikSpan]:
            for t in traces:
                try:
                    span = _opik_trace_to_span(t)
                except Exception:
                    continue
                if query.matches(span):
                    yield span

        return _iter()

    def close(self) -> None:
        try:
            self._client.end()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._client.flush()  # type: ignore[attr-defined]
        except Exception:
            pass


def _opik_trace_to_span(trace_obj: Any) -> OpikSpan:
    """Translate an Opik trace REST record back into an :class:`OpikSpan`.

    Defensive: the REST surface uses attribute access on a model
    object, but exact field names have shifted between Opik versions.
    We try the known names and fall back to dict-style access.
    """
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _parse_dt(v: Any) -> datetime:
        if v is None:
            return datetime.now(timezone.utc)
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

    meta = dict(_get(trace_obj, "metadata") or {})
    error_info = _get(trace_obj, "error_info")
    return OpikSpan(
        name=str(_get(trace_obj, "name") or "trace"),
        start_time=_parse_dt(_get(trace_obj, "start_time")),
        end_time=_parse_dt(
            _get(trace_obj, "end_time") or _get(trace_obj, "start_time")
        ),
        type=str(_get(trace_obj, "type") or "general"),
        trace_id=_get(trace_obj, "id"),
        span_id=_get(trace_obj, "id"),
        input=dict(_get(trace_obj, "input") or {}),
        output=dict(_get(trace_obj, "output") or {}),
        metadata=meta,
        tags=list(_get(trace_obj, "tags") or []),
        model=_get(trace_obj, "model"),
        provider=_get(trace_obj, "provider"),
        usage=dict(_get(trace_obj, "usage") or {}),
        total_cost=_get(trace_obj, "total_cost"),
        error_info=dict(error_info) if isinstance(error_info, dict) and error_info else None,
        bot_id=meta.get("bot_id"),
        producer=meta.get("producer"),
        attributes=dict(meta.get("attributes") or {}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Concrete: JSONL (filesystem fallback)
# ─────────────────────────────────────────────────────────────────────────────


class JsonlBackend(ObservabilityClient):
    """Filesystem-backed observability store.

    Always available — no external service, no extra dependency. Spans
    land at ``{shared_dir}/observability/spans/YYYY-MM-DD.jsonl``,
    one record per line. Written atomically (temp file + rename of the
    full day) so concurrent producers don't tear records mid-write.

    Used:
      * In the default config (operator hasn't opted into Opik yet)
      * In tests (no need to stand up an Opik server)
      * As the "thin abstraction proves out without Opik server"
        scenario in the V1.5-1 sprint DoD

    Retention: not handled here. The signals.retention module can be
    extended later to age out these files; the JSONL store is small
    relative to annotations/.
    """

    backend_name = "jsonl"

    def __init__(self, shared_dir: Path):
        self._root = Path(shared_dir) / "observability" / "spans"

    def _day_path(self, day_iso: str) -> Path:
        return self._root / f"{day_iso}.jsonl"

    def record_span(self, span: OpikSpan) -> None:
        """Persist span to JSONL file with per-call timeout cap.

        Executes inside the module-level timeout wrapper
        (``RECORD_SPAN_TIMEOUT_SECONDS``) so a slow filesystem (e.g. a
        network mount) can't block the producer indefinitely.
        """
        _record_with_timeout(self._record_span_inner, span)

    def _record_span_inner(self, span: OpikSpan) -> None:
        """Actual JSONL write — executed inside the timeout wrapper."""
        try:
            day_iso = span.end_time.astimezone(timezone.utc).date().isoformat()
            path = self._day_path(day_iso)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic append via read-rewrite-rename. For sub-second contention
            # this is the right tradeoff vs. unconditional append: it preserves
            # JSON-line integrity at the cost of more I/O per write. Capture
            # monitors are not hot paths.
            existing = ""
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError:
                    existing = ""
            new_line = json.dumps(span.to_dict()) + "\n"
            combined = existing + new_line

            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(combined)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            # Best-effort.
            return

    def search_spans(self, query: SpanFilter) -> Iterator[OpikSpan]:
        # Determine which day-files to read. Default: last 24h if no window
        # specified (matches typical monitor lookbacks).
        now = datetime.now(timezone.utc)
        since = query.since or (now - timedelta(days=1))
        until = query.until or now

        # Inclusive day enumeration.
        start_day = since.astimezone(timezone.utc).date()
        end_day = until.astimezone(timezone.utc).date()

        # Most-recent-first iteration of days.
        days: list[Path] = []
        cur = end_day
        while cur >= start_day:
            days.append(self._day_path(cur.isoformat()))
            cur = cur - timedelta(days=1)

        emitted = 0
        for path in days:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            # Read most-recent-first within the day.
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    span = OpikSpan.from_dict(raw)
                except (KeyError, ValueError, TypeError):
                    continue
                if not query.matches(span):
                    continue
                yield span
                emitted += 1
                if emitted >= query.limit:
                    return

    def close(self) -> None:
        # Nothing to flush — every write is atomic + immediate.
        return


# ─────────────────────────────────────────────────────────────────────────────
# Disabled (null-object)
# ─────────────────────────────────────────────────────────────────────────────


class DisabledBackend(ObservabilityClient):
    """No-op backend. Records nothing, returns empty searches.

    Selected when ``observability.backend == "disabled"`` or
    ``OPIK_DISABLED=1`` in the environment. Useful in unit tests that
    don't care about observability and on hosts where neither Opik nor
    a writable shared_dir is available.
    """

    backend_name = "disabled"

    def record_span(self, span: OpikSpan) -> None:
        return

    def search_spans(self, query: SpanFilter) -> Iterator[OpikSpan]:
        return iter(())

    def close(self) -> None:
        return


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def get_client(
    config: dict[str, Any] | None = None,
    *,
    shared_dir: Path | str | None = None,
) -> ObservabilityClient:
    """Pick a backend based on config + env + availability.

    Resolution order:
      1. ``OPIK_DISABLED=1`` env var → :class:`DisabledBackend`.
      2. ``config["observability"]["backend"]`` is read first:
         - ``"opik"`` → try :class:`OpikBackend`; on ImportError or
           missing config, fall through to JSONL with a debug note.
         - ``"jsonl"`` → :class:`JsonlBackend`.
         - ``"disabled"`` → :class:`DisabledBackend`.
      3. Absent backend key → :class:`JsonlBackend` (safe default).

    ``shared_dir`` is required for the JSONL backend; if absent, falls
    back to ``/Users/Shared/evolve`` (the documented default).
    """
    if os.environ.get("OPIK_DISABLED") == "1":
        return DisabledBackend()

    cfg = (config or {}).get("observability") or {}
    backend = (cfg.get("backend") or "jsonl").lower()

    if backend == "disabled":
        return DisabledBackend()

    if backend == "opik":
        opik_cfg = cfg.get("opik") or {}
        try:
            return OpikBackend(
                host=opik_cfg.get("host"),
                api_key=opik_cfg.get("api_key"),
                project_name=opik_cfg.get("project_name") or "evolve",
                workspace=opik_cfg.get("workspace"),
            )
        except ImportError:
            # Configured opik but SDK not installed — fall through to
            # JSONL so the system stays operational. Caller can detect
            # this with ``isinstance(client, JsonlBackend)`` if it
            # wants to surface a configuration warning.
            pass

    # Default + opik-import-failure path.
    sd = Path(shared_dir) if shared_dir else Path("/Users/Shared/evolve")
    return JsonlBackend(sd)


# ─────────────────────────────────────────────────────────────────────────────
# Cost-extraction helpers
# ─────────────────────────────────────────────────────────────────────────────


def span_to_cost_event(span: OpikSpan) -> dict[str, Any] | None:
    """Convert an :class:`OpikSpan` into a cost_event dict.

    Returns ``None`` for spans that aren't billable (no model or no
    usage — e.g. tool calls, guardrails, plain instrumentation spans).
    Cost_ledger and cost_rollup ignore ``None`` returns.

    Field mapping (matches the shape ``observations.access.cost_events``
    yields today, so cost_ledger / cost_rollup require no schema
    changes downstream):

      span.end_time   → ts (ISO 8601)
      span.bot_id     → bot_id
      span.model      → model
      span.total_cost → cost_usd
      span.usage.{prompt_tokens, input_tokens, output_tokens,
                  cache_read, cache_write} → input_tokens, output_tokens,
                                              cache_read_tokens,
                                              cache_write_tokens
      span.attributes.session_id        → session_id (optional)
      span.attributes.trigger_kind      → trigger_kind (optional)
      span.attributes.cache_state       → cache_state (optional)
    """
    if not span.model and span.total_cost is None and not span.usage:
        return None
    usage = span.usage or {}
    # Accept several common key names — Opik / OpenAI / Anthropic / Gemini use
    # slightly different conventions. We map them all to the cost_event
    # canonical names.
    in_tokens = int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("promptTokenCount")  # Gemini camelCase
        or 0
    )
    out_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("candidatesTokenCount")  # Gemini camelCase
        or 0
    )
    cache_read = int(
        usage.get("cache_read_tokens")
        or usage.get("cache_read_input_tokens")
        or 0
    )
    cache_write = int(
        usage.get("cache_write_tokens")
        or usage.get("cache_creation_input_tokens")
        or 0
    )

    return {
        "type": "cost_event",
        "ts": span.end_time.astimezone(timezone.utc).isoformat(),
        "bot_id": span.bot_id or span.attributes.get("bot_id"),
        "model": span.model,
        "provider": span.provider,
        "cost_usd": float(span.total_cost or 0.0),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "session_id": span.attributes.get("session_id"),
        "trigger_kind": span.attributes.get("trigger_kind"),
        "cache_state": span.attributes.get("cache_state"),
        # Source-of-truth marker so cost_event_converter can identify
        # records that came through the observability path vs. the
        # legacy plugin path.
        "source": "observability",
    }
