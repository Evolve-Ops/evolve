"""pod_baseline.schema — baseline document, exception object, classification.

Spec: internal/spec-pod-plane-2026-08-15.md §B1. Pure transforms only — no I/O
(that lives in ``store`` and ``census``).

Surface values are normalized strings so the baseline file stays legible and
comparable across bots (``"full"``, ``"coding"``, ``"on"``, ``"balanced"``,
``"pod-defaults"``, …). ``"unset"`` means "the knob is absent from the bot's
config", which is distinct from an unreadable config (that classifies as
:data:`STATE_UNREADABLE`, never as conform or drift) — but it is a
**no-intent sentinel**, not a policy position, and Q7(b) forbids seeding
from electing it (see :data:`NO_INTENT_SENTINELS`). A pre-Q7 file may still
carry one as a declared value; the census flags that rather than silently
honouring it.

Exception precedence (pinned by tests): a declared exception REPLACES the
pod baseline as that bot's desired value for that surface. Observed ==
exception value → EXCEPTION; anything else → a drift state, *even if* the
observed value equals the pod baseline — the declared intent for that bot is
the exception, and a bot matching the baseline under a live exception means
the exception is stale and should be revoked, not silently ignored.

Q7 (decided 2026-08-22) replaced two blunt edges here:

- **Drift has a direction.** The single ``drift`` state is gone, split into
  :data:`STATE_TIGHTENED` (observed is more restrictive than expected —
  informational, and explicitly NOT something an operator must file an
  exception for), :data:`STATE_LOOSENED` (the fault state), and
  :data:`STATE_DIVERGENT` (differs on an axis with no safety ordering). The
  ordering itself lives in ``pod_baseline.ordering``.
- **A surface can be undeclared.** ``"custom"`` and ``"unset"`` are
  no-intent sentinels (:data:`NO_INTENT_SENTINELS`) — the *absence* of a
  declaration, not a position on a safety axis. Seeding refuses to elect
  one, and such a surface is recorded in :attr:`PodBaseline.undeclared`
  instead of :attr:`PodBaseline.surfaces`. Its rows classify
  :data:`STATE_UNDECLARED` — never conform, never drift.

  ``undeclared`` is a sibling array rather than a reserved
  ``"undeclared"`` string in ``surfaces`` because ``exec_policy_from_config``
  returns whatever string the config holds, so a sentinel value would be
  forgeable by a bot's own config; an array cannot collide with any live
  reading. It also leaves the "every surface value is a string" guard below
  untouched, so no ``schema_version`` bump is needed.

  **What not bumping actually buys, stated precisely** (the looser claim in
  the spec's Q7(b) text was checked against ``origin/main``'s package on
  2026-08-23 and is wrong): a pre-Q7 reader *parses* a post-Q7 file
  without error, and its :meth:`PodBaseline.validate` then returns
  ``missing surface value: X`` for each undeclared surface — naming exactly
  what it does not understand. The pre-Q7 **CLI** turns those problems into
  a ``ClickException``, so ``pod-baseline census`` on an old release exits 1
  with no census output. That is still better than a ``schema_version``
  bump, where ``from_dict`` hard-refuses with an opaque "newer than
  supported" and nothing names the difference — but it is *not* a census
  that keeps running. Operational consequence, which the spec did not
  record: re-seed a pod only once its release carries this change, because
  a rollback below it leaves ``census`` dead on that pod until the baseline
  is restored.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pod_baseline import ordering

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

# Q7(b): values that mean "nobody declared anything here", not a policy
# position. ``"custom-allow"`` is deliberately NOT one — an exclusive
# ``tools.allow`` list is somebody's deliberate list, merely one that sits
# on no safety ladder.
NO_INTENT_SENTINELS: frozenset = frozenset({"custom", "unset"})

STATE_CONFORM = "conform"
STATE_EXCEPTION = "exception"
STATE_TIGHTENED = "tightened"    # observed is MORE restrictive than expected
STATE_LOOSENED = "loosened"      # observed is LESS restrictive — the fault state
STATE_DIVERGENT = "divergent"    # differs, but the surface has no safety ordering
STATE_UNREADABLE = "unreadable"
STATE_UNDECLARED = "undeclared"  # no pod intent for this surface at all

# The three states that mean "observed != expected". Q7(a) split what used
# to be a single ``drift``; callers that genuinely need "did this row
# deviate" ask through here rather than re-listing the members, so adding a
# fourth direction later cannot silently miss a call site.
DRIFT_STATES: frozenset = frozenset({STATE_TIGHTENED, STATE_LOOSENED, STATE_DIVERGENT})

# Display order for summary lines: every state, in escalating interest.
# Nothing is allowed to be absent from a count line — a state a summary
# cannot name is a state an operator cannot see.
# ``test_state_display_order_covers_every_state_constant`` derives the roster
# by reading the ``STATE_*`` constants off this module rather than restating
# them, so a new state that never reaches this tuple fails that test instead
# of quietly going unprintable.
STATE_DISPLAY_ORDER: tuple = (
    STATE_CONFORM,
    STATE_EXCEPTION,
    STATE_TIGHTENED,
    STATE_LOOSENED,
    STATE_DIVERGENT,
    STATE_UNREADABLE,
    STATE_UNDECLARED,
)


def is_drift(state: str) -> bool:
    """True for the three deviation states (tightened / loosened / divergent)."""
    return state in DRIFT_STATES


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
    """The pod-level desired state over the five v1 surfaces.

    Each surface is either **declared** (a value in :attr:`surfaces`) or
    **undeclared** (its name in :attr:`undeclared`) — exactly one, enforced
    by :meth:`validate`. See the module docstring for why undeclaredness is
    a sibling array and not a reserved value.
    """

    schema_version: int = POD_BASELINE_SCHEMA_VERSION
    updated_at: str = ""
    surfaces: dict = field(default_factory=dict)  # surface -> desired value
    undeclared: list = field(default_factory=list)  # surfaces with no declared intent
    exceptions: list = field(default_factory=list)  # list[BaselineException]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "surfaces": dict(self.surfaces),
            # Canonical SURFACES order, not sort order: the store writes with
            # sort_keys=True, which orders object KEYS and leaves array
            # element order to us, so pinning it here is what keeps diffs
            # stable across re-seeds. Multiplicity is PRESERVED — a
            # hand-edited duplicate must survive a load/save round-trip so
            # validate() keeps naming it, rather than being laundered away
            # by the very write that was supposed to be lossless.
            "undeclared": sorted(
                self.undeclared,
                key=lambda s: (SURFACES.index(s) if s in SURFACES else len(SURFACES), s),
            ),
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
            # "" is a string and would sail through the check above, then
            # match no observed reading, silently flipping the whole fleet to
            # `divergent` against a value that is not even in `undeclared`.
            # An emptied-out hand edit means "I deleted this" — which is
            # spelled by moving the surface to `undeclared`, not by blanking
            # it. Whitespace-only is the same mistake with a space in it.
            if not value.strip():
                raise ValueError(
                    f"pod-baseline: surface {key!r} has an empty value — to "
                    "declare no intent for a surface, list it in "
                    "\"undeclared\" instead of blanking it"
                )
        raw_undeclared = data.get("undeclared", [])
        if not isinstance(raw_undeclared, list):
            raise ValueError("pod-baseline: 'undeclared' must be a list of surface names")
        for entry in raw_undeclared:
            if not isinstance(entry, str):
                raise ValueError(
                    f"pod-baseline: 'undeclared' entries must be surface names, got {entry!r}"
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
            undeclared=list(raw_undeclared),
            exceptions=[BaselineException.from_dict(e) for e in raw_exceptions],
        )

    def is_undeclared(self, surface: str) -> bool:
        """True when the pod has declared no intent for this surface.

        A surface that is in neither ``surfaces`` nor ``undeclared`` also
        reads as undeclared here. :meth:`validate` reports that as a
        problem, but classification must not fall back to comparing against
        a fabricated default — an unnamed surface has no declared intent by
        definition, and reporting rows conform against a value nobody wrote
        is the exact failure Q7 exists to end.
        """
        return surface not in self.surfaces


    def exception_for(self, bot_id: str, surface: str) -> "BaselineException | None":
        """First declared exception for (bot, surface), or None."""
        for exc in self.exceptions:
            if exc.bot_id == bot_id and exc.surface == surface:
                return exc
        return None

    def validate(self) -> list:
        """Return a list of problem strings (empty == valid)."""
        problems: list = []
        undeclared = set(self.undeclared)
        # Declared XOR undeclared: a surface in both is a contradiction, a
        # surface in neither is an omission. Both are operator-visible edits
        # of a hand-editable file, so both must be named, not guessed at.
        for surface in SURFACES:
            declared = surface in self.surfaces
            if declared and surface in undeclared:
                problems.append(
                    f"surface both declared and undeclared: {surface}"
                )
            elif not declared and surface not in undeclared:
                problems.append(f"missing surface value: {surface}")
        for surface in self.surfaces:
            if surface not in SURFACES:
                problems.append(f"unknown surface: {surface}")
        for surface in self.undeclared:
            if surface not in SURFACES:
                problems.append(f"unknown undeclared surface: {surface}")
        seen_undeclared: set = set()
        for surface in self.undeclared:
            if surface in seen_undeclared:
                problems.append(f"duplicate undeclared surface: {surface}")
            seen_undeclared.add(surface)
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
    *,
    surface: str = "",
    undeclared: bool = False,
) -> tuple:
    """Classify one (bot, surface) reading against the baseline.

    Returns ``(state, expected)`` where ``expected`` is the value the bot was
    compared against (the exception's value when one is declared, else the
    pod baseline value, else ``""`` when nothing is declared at all).

    Precedence, in order, each edge deliberate:

    1. ``observed is None`` → :data:`STATE_UNREADABLE`. **Unreadable beats
       undeclared**: "we could not look" and "we looked and no intent exists"
       are different facts, and collapsing them would let a permission wall
       hide inside a pod-level silence.
    2. A declared exception → :data:`STATE_EXCEPTION` on a match, else the
       direction split against the exception's value. **An exception beats
       undeclaredness too**: an exception IS a declared intent for that bot,
       so reporting it as "nobody declared this" would discard a real
       declaration. (The spec's Q7(b) text says an undeclared surface's rows
       classify undeclared "regardless of the observed value" — regardless of
       *observed*, which is what this preserves.)
    3. ``undeclared`` → :data:`STATE_UNDECLARED`, whatever the observed value.
    4. Otherwise conform on a match, else the direction split.

    The direction split (Q7(a)) asks ``pod_baseline.ordering`` where the two
    values sit on the surface's safety axis: more restrictive →
    :data:`STATE_TIGHTENED`, less → :data:`STATE_LOOSENED`, no ordering or
    an unordered pair → :data:`STATE_DIVERGENT`. ``surface`` is keyword-only
    and defaults to ``""`` — a surface name that matches no chain — so a
    caller that forgets it gets ``divergent``, never a fabricated direction.
    """
    if observed is None:
        expected = exception.value if exception is not None else (
            "" if undeclared else baseline_value
        )
        return STATE_UNREADABLE, expected
    if exception is not None:
        if observed == exception.value:
            return STATE_EXCEPTION, exception.value
        return _direction(surface, observed, exception.value), exception.value
    if undeclared:
        return STATE_UNDECLARED, ""
    if observed == baseline_value:
        return STATE_CONFORM, baseline_value
    return _direction(surface, observed, baseline_value), baseline_value


def _direction(surface: str, observed: str, expected: str) -> str:
    """Which of the three drift states a differing reading is."""
    verdict = ordering.compare(surface, observed, expected)
    if verdict == ordering.TIGHTER:
        return STATE_TIGHTENED
    if verdict == ordering.LOOSER:
        return STATE_LOOSENED
    return STATE_DIVERGENT
