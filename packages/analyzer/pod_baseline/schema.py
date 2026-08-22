"""pod_baseline.schema — baseline document, exception object, classification.

Spec: docs/spec-pod-plane-2026-08-15.md §B1. Pure transforms only — no I/O
(that lives in ``store`` and ``census``).

Surface values are normalized strings so the baseline file stays legible and
comparable across bots (``"full"``, ``"coding"``, ``"on"``, ``"balanced"``,
``"pod-defaults"``, …). ``"unset"`` is a real, declarable value — it means
"the knob is absent from the bot's config" — distinct from an unreadable
config, which classifies as :data:`STATE_UNREADABLE`, never as conform or
drift.

Exception precedence (pinned by tests): a declared exception REPLACES the
pod baseline as that bot's desired value for that surface. Observed ==
exception value → EXCEPTION; anything else → DRIFT, *even if* the observed
value equals the pod baseline — the declared intent for that bot is the
exception, and a bot matching the baseline under a live exception means the
exception is stale and should be revoked, not silently ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field

POD_BASELINE_SCHEMA_VERSION = 1

# Q2 (decided 2026-08-15): exactly these five knobs in v1. Plugin/skill sets
# are explicitly v2. Order is the canonical display order.
SURFACES: tuple = (
    "exec_policy",      # openclaw.json tools.exec.security (deny|allowlist|full)
    "tool_profile",     # openclaw.json tools.profile (minimal|coding|messaging|full)
    "browser",          # openclaw.json browser.enabled (on|off)
    "context_profile",  # cost-field set matched to a named cost profile
    "model_policy",     # evolve-tiers.json rungs presence (pod-defaults|custom)
)

STATE_CONFORM = "conform"
STATE_EXCEPTION = "exception"
STATE_DRIFT = "drift"
STATE_UNREADABLE = "unreadable"


@dataclass
class BaselineException:
    """A first-class, named deviation from the pod baseline.

    "security-bot: exec_policy=deny, by design" — the exception is the
    bot's declared desired value for that surface.
    """

    bot_id: str
    surface: str
    value: str
    reason: str
    declared_at: str  # ISO-8601

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "surface": self.surface,
            "value": self.value,
            "reason": self.reason,
            "declared_at": self.declared_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BaselineException":
        return cls(
            bot_id=str(data.get("bot_id", "")),
            surface=str(data.get("surface", "")),
            value=str(data.get("value", "")),
            reason=str(data.get("reason", "")),
            declared_at=str(data.get("declared_at", "")),
        )


@dataclass
class PodBaseline:
    """The pod-level desired state over the five v1 surfaces."""

    schema_version: int = POD_BASELINE_SCHEMA_VERSION
    updated_at: str = ""
    surfaces: dict = field(default_factory=dict)  # surface -> desired value
    exceptions: list = field(default_factory=list)  # list[BaselineException]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "surfaces": dict(self.surfaces),
            "exceptions": [e.to_dict() for e in self.exceptions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PodBaseline":
        """Parse a baseline document. Loud on shape errors — the file is
        operator-editable, so a hand-edit mistake (a bare ``true``, a
        stringified exception) must surface as an error, never silently
        coerce into an all-drift census or a vanished exception."""
        version = int(data.get("schema_version", POD_BASELINE_SCHEMA_VERSION))
        if version > POD_BASELINE_SCHEMA_VERSION:
            raise ValueError(
                f"pod-baseline: schema_version {version} is newer than the "
                f"supported {POD_BASELINE_SCHEMA_VERSION} — refusing to "
                "misread a future-format file"
            )
        surfaces = data.get("surfaces")
        if not isinstance(surfaces, dict):
            raise ValueError("pod-baseline: 'surfaces' must be an object")
        for key, value in surfaces.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"pod-baseline: surface {key!r} value must be a string, "
                    f"got {value!r} (e.g. browser is \"on\"/\"off\", not a bool)"
                )
        raw_exceptions = data.get("exceptions", [])
        if not isinstance(raw_exceptions, list):
            raise ValueError("pod-baseline: 'exceptions' must be a list")
        for entry in raw_exceptions:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"pod-baseline: each exception must be an object, got {entry!r}"
                )
        return cls(
            schema_version=version,
            updated_at=str(data.get("updated_at", "")),
            surfaces={str(k): v for k, v in surfaces.items()},
            exceptions=[BaselineException.from_dict(e) for e in raw_exceptions],
        )

    def exception_for(self, bot_id: str, surface: str) -> "BaselineException | None":
        """First declared exception for (bot, surface), or None."""
        for exc in self.exceptions:
            if exc.bot_id == bot_id and exc.surface == surface:
                return exc
        return None

    def validate(self) -> list:
        """Return a list of problem strings (empty == valid)."""
        problems: list = []
        for surface in SURFACES:
            if surface not in self.surfaces:
                problems.append(f"missing surface value: {surface}")
        for surface in self.surfaces:
            if surface not in SURFACES:
                problems.append(f"unknown surface: {surface}")
        seen: set = set()
        for exc in self.exceptions:
            if exc.surface not in SURFACES:
                problems.append(f"exception for unknown surface: {exc.bot_id}/{exc.surface}")
            if not exc.bot_id:
                problems.append("exception with empty bot_id")
            key = (exc.bot_id, exc.surface)
            if key in seen:
                problems.append(f"duplicate exception: {exc.bot_id}/{exc.surface}")
            seen.add(key)
        return problems


def classify_surface(
    observed: "str | None",
    baseline_value: str,
    exception: "BaselineException | None",
) -> tuple:
    """Classify one (bot, surface) reading against the baseline.

    Returns ``(state, expected)`` where ``expected`` is the value the bot
    was compared against (the exception's value when one is declared, else
    the pod baseline value). ``observed is None`` means the bot's config
    could not be read — that is UNREADABLE, surfaced distinctly, never
    folded into conform or drift.
    """
    expected = exception.value if exception is not None else baseline_value
    if observed is None:
        return STATE_UNREADABLE, expected
    if exception is not None:
        return (STATE_EXCEPTION if observed == exception.value else STATE_DRIFT), expected
    return (STATE_CONFORM if observed == baseline_value else STATE_DRIFT), expected
