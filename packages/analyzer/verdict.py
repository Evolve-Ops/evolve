"""verdict — the shared pass/fail/unknown contract for controls (D-CS7).

Spec: internal/assessment-silent-controls-2026-09-14.md §2.
Registry + fixture enforcement: tools/control-registry, tools/control-coverage-ratchet.

The property this enforces: **a control fails silently when its passing
condition can be satisfied by the absence of evidence.** Four controls in
this repo hit that failure within 48 hours of each other — a cache tuner
that read "not measured" as "the premise holds", a freshness watcher whose
empty API page read as "nothing exists", a healed-path check a stray commit
keyword satisfied, and a status filter that returned the previous run
against a newest-first list. One control (``tools/quarantine-ratchet``)
never can, because its pass condition is a measured quantity against a
recorded prior rather than a state that can be asserted from an empty hand.

``Verdict`` makes that shape the only one constructible:

  - ``ok`` / ``broken`` require a ``subject`` — a stable identifier for the
    thing actually examined (a run id, a bot id plus metric window, a path
    plus mtime or sha). Constructing either without one raises at
    construction; there is no way to report "fine" or "broken" about
    nothing.
  - ``unknown`` is the only state that may carry a null subject, and it
    must carry a ``reason`` — what stopped the control from looking, not a
    softer restatement of "no problem found".

``evidence`` is free-form and carries whatever the control actually read
(a query, page bounds, a row count, a log tail) — enough that a verdict can
be second-guessed without re-running the control.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

State = Literal["ok", "broken", "unknown"]

_STATES: frozenset[str] = frozenset(("ok", "broken", "unknown"))


@dataclass(frozen=True)
class Verdict:
    state: State
    subject: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"invalid verdict state: {self.state!r}")
        if self.state in ("ok", "broken") and not self.subject:
            raise ValueError(
                f"a {self.state!r} verdict must name the subject it examined "
                "— use Verdict.unknown(...) when no subject could be identified"
            )
        if self.state == "unknown" and not self.reason:
            raise ValueError("an unknown verdict must carry a reason")

    # ── constructors ─────────────────────────────────────────────────────

    @classmethod
    def ok(cls, subject: str, evidence: dict[str, Any] | None = None) -> "Verdict":
        return cls(state="ok", subject=subject, evidence=dict(evidence or {}))

    @classmethod
    def broken(
        cls, subject: str, reason: str, evidence: dict[str, Any] | None = None
    ) -> "Verdict":
        ev = dict(evidence or {})
        ev.setdefault("reason", reason)
        return cls(state="broken", subject=subject, evidence=ev)

    @classmethod
    def unknown(
        cls,
        reason: str,
        subject: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> "Verdict":
        return cls(state="unknown", subject=subject, reason=reason, evidence=dict(evidence or {}))

    # ── convenience ──────────────────────────────────────────────────────

    @property
    def is_ok(self) -> bool:
        return self.state == "ok"

    @property
    def is_broken(self) -> bool:
        return self.state == "broken"

    @property
    def is_unknown(self) -> bool:
        return self.state == "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "subject": self.subject,
            "evidence": self.evidence,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        return cls(
            state=data["state"],
            subject=data.get("subject"),
            evidence=dict(data.get("evidence") or {}),
            reason=data.get("reason"),
        )
