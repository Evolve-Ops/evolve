"""pod_baseline.census — read-only per-bot readings of the five v1 surfaces.

Spec: internal/spec-pod-plane-2026-08-15.md §B1. READ-ONLY: this module never
writes anything, anywhere.

Where each surface's live value comes from (reusing the existing readers /
key paths — never a second resolver, #3498-class rule):

- ``exec_policy``     — openclaw.json ``tools.exec.security`` (the same key
  deploy.py writes and permissions.inventory reads). ``ask``/``host`` are
  sub-knobs, out of the v1 comparison. NOTE: this surface overlaps the
  permission-baseline store (``permissions/baseline.py``,
  spec-permission-posture-2026-05-10) — the census reads LIVE state only;
  reconciling the two declared-intent stores is a B2 gate (spec Q6).
- ``tool_profile``    — openclaw.json ``tools.profile``; an exclusive
  ``tools.allow`` list REPLACES the profile upstream, so a bot carrying one
  reads as ``custom-allow`` (``tools.alsoAllow`` is additive and does not
  change the profile).
- ``browser``         — openclaw.json ``browser.enabled`` → ``on``/``off``.
- ``context_profile`` — the cost-field set (``cost_profiles.COST_FIELDS``)
  matched against the named cost profiles *as currently defined*: a bot
  whose profile was applied before a profile definition changed (e.g. the
  2026-07-31 prune-ttl flip) deliberately reads as ``custom`` — the census
  compares against today's declared intent, and re-applying the named
  profile is the fix.
- ``model_policy``    — evolve-tiers.json via the shared Custom-flip
  predicate ``primary_bot.tiers_doc_is_custom``.

``"unset"`` means "knob absent from a readable config" (a missing
openclaw.json reads as all-unset — the bot has no local posture). A config
that cannot be read or parsed into an object (permission wall, corrupt
JSON, non-dict top level) yields ``value=None`` which classifies as
UNREADABLE — never conform, never drift. The two are not interchangeable:
``unset`` is a no-intent sentinel that votes in the seed (and votes to leave
the surface undeclared — Q7(b)), ``None`` is "we could not look" and is
excluded from the vote entirely.

File reads go through ``permissions.inventory.read_json_file`` (direct
read via the evolve ACL, ``sudo /bin/cat`` fallback, ``(data, err)`` with
``err == NOT_FOUND`` distinguishing absence from a permission wall).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evolve_config import bot_home, get_members, get_primary
from cost_profiles import (
    BE_CONFIG_PROFILE_FIELDS,
    BUILTIN_PROFILES,
    _extract_cost_settings,
)
from permissions.inventory import NOT_FOUND, _dotpath_get, read_json_file
from primary_bot import tiers_doc_is_custom

from pod_baseline.schema import (
    DRIFT_STATES,
    NO_INTENT_SENTINELS,
    STATE_DISPLAY_ORDER,
    STATE_UNDECLARED,
    SURFACES,
    PodBaseline,
    classify_surface,
)


# ── Pure extractors (dict → normalized surface value) ────────────────────────

def exec_policy_from_config(oc: dict) -> str:
    val = _dotpath_get(oc, "tools.exec.security")
    return val if isinstance(val, str) and val else "unset"


def tool_profile_from_config(oc: dict) -> str:
    tools = oc.get("tools") or {}
    allow = tools.get("allow")
    if isinstance(allow, list) and allow:
        return "custom-allow"
    val = tools.get("profile")
    return val if isinstance(val, str) and val else "unset"


def browser_from_config(oc: dict) -> str:
    val = _dotpath_get(oc, "browser.enabled")
    if val is True:
        return "on"
    if val is False:
        return "off"
    return "unset"


def context_profile_from_config(oc: dict, custom_profiles: tuple = ()) -> str:
    """Match the bot's live cost-field set to a named cost profile.

    Subset semantics: a profile matches when every openclaw-owned field it
    declares matches the live value (``None`` in the profile means "field
    absent"); extra live keys (e.g. an operator-set ``heartbeat.every``)
    don't break recognition, because ``apply_profile_to_bot`` deep-merges
    rather than replaces.
    """
    settings = _extract_cost_settings(oc)
    if all(v is None for v in settings.values()):
        return "unset"
    for prof in list(BUILTIN_PROFILES.values()) + list(custom_profiles):
        if not isinstance(prof, dict):
            continue  # corrupt cost-profiles.json entry — never crash the sweep
        name = prof.get("name")
        if name and _cost_settings_match(settings, prof.get("settings") or {}):
            return str(name)
    return "custom"


def _cost_settings_match(observed: dict, expected: dict) -> bool:
    for fld, exp in expected.items():
        if fld in BE_CONFIG_PROFILE_FIELDS:
            continue  # BE-config-owned (cache_retention) — not in openclaw.json
        if not _value_matches(observed.get(fld), exp):
            return False
    return True


def _value_matches(obs, exp) -> bool:
    if exp is None:
        return obs is None
    if isinstance(exp, dict):
        if not isinstance(obs, dict):
            return False
        return all(_value_matches(obs.get(k), v) for k, v in exp.items())
    return obs == exp


def model_policy_from_tiers_doc(doc: dict) -> str:
    return "custom" if tiers_doc_is_custom(doc) else "pod-defaults"


# Surface → extractor for the openclaw.json-backed surfaces (model_policy
# reads evolve-tiers.json instead). Single table so an unreadable file and
# the readable path can never cover different surface sets.
_OPENCLAW_EXTRACTORS: dict = {
    "exec_policy": exec_policy_from_config,
    "tool_profile": tool_profile_from_config,
    "browser": browser_from_config,
    # bound at call time so custom profiles thread through
}
_OPENCLAW_SURFACES: tuple = tuple(_OPENCLAW_EXTRACTORS) + ("context_profile",)


# ── Per-bot reading ──────────────────────────────────────────────────────────

@dataclass
class SurfaceReading:
    value: "str | None"  # None ⇒ unreadable
    error: "str | None" = None


@dataclass
class BotReading:
    bot_id: str
    surfaces: dict = field(default_factory=dict)  # surface -> SurfaceReading


def _read_json_object(path: Path) -> tuple:
    """read_json_file + top-level-shape guard.

    Valid JSON whose top level is not an object (a list, string, or
    literal ``null`` — the latter reads as ``(None, None)`` from the
    shared reader) is a corrupt config: surface it as unreadable rather
    than crashing the sweep or misreading it as "no posture".
    """
    data, err = read_json_file(path)
    if err is None and not isinstance(data, dict):
        return None, "not_an_object"
    return data, err


def pod_roster(network: dict) -> list:
    """The bots the census covers, sorted.

    Mirrors deploy's ``_is_provisionable_bot`` roster gate: the authority
    is ``network["members"]`` unioned with the resolved primary; legacy
    pods without a ``members`` array fall back to ``bots.keys()``. Planned
    bots (a ``bots{}`` block whose only key is ``purpose``) are not
    members, so they never vote in the seed or census as phantom
    all-unset bots.
    """
    ids = {b for b in get_members(network) if isinstance(b, str) and b}
    primary = get_primary(network)
    if isinstance(primary, str) and primary:
        ids.add(primary)
    if not ids:
        ids = set((network.get("bots") or {}).keys())
    return sorted(ids)


def read_bot_surfaces(
    network: dict,
    bot_id: str,
    *,
    home_override: "Path | None" = None,
    custom_cost_profiles: tuple = (),
) -> BotReading:
    """Read the five live surface values for one bot. Read-only.

    ``home_override`` redirects the bot's home for fixture tests; live runs
    resolve it via ``evolve_config.bot_home`` (network ``bots.<id>.user`` →
    pwd → platform-profile fallback — never a ``/Users/`` literal).
    """
    home = Path(home_override) if home_override is not None else bot_home(bot_id, network)
    oc_dir = home / ".openclaw"
    readings: dict = {}

    oc, oc_err = _read_json_object(oc_dir / "openclaw.json")
    if oc is None and oc_err != NOT_FOUND:
        for surface in _OPENCLAW_SURFACES:
            readings[surface] = SurfaceReading(None, oc_err)
    else:
        cfg = oc or {}  # NOT_FOUND → no local config → every knob unset
        for surface in _OPENCLAW_SURFACES:
            if surface == "context_profile":
                extract = lambda c: context_profile_from_config(c, custom_cost_profiles)  # noqa: E731
            else:
                extract = _OPENCLAW_EXTRACTORS[surface]
            try:
                readings[surface] = SurfaceReading(extract(cfg))
            except Exception as exc:  # unforeseen config shape — UNREADABLE, not a crash
                readings[surface] = SurfaceReading(None, f"extract_error: {exc}")

    tiers, tiers_err = _read_json_object(oc_dir / "evolve-tiers.json")
    if tiers is None and tiers_err != NOT_FOUND:
        readings["model_policy"] = SurfaceReading(None, tiers_err)
    else:
        readings["model_policy"] = SurfaceReading(model_policy_from_tiers_doc(tiers or {}))

    return BotReading(bot_id=bot_id, surfaces=readings)


def read_pod_surfaces(
    network: dict,
    *,
    home_overrides: "dict | None" = None,
    custom_cost_profiles: tuple = (),
) -> list:
    """Read every roster bot (see :func:`pod_roster`). Returns list[BotReading]."""
    overrides = home_overrides or {}
    return [
        read_bot_surfaces(
            network,
            bot_id,
            home_override=overrides.get(bot_id),
            custom_cost_profiles=custom_cost_profiles,
        )
        for bot_id in pod_roster(network)
    ]


# ── Census (readings × baseline → classified rows) ───────────────────────────

@dataclass
class CensusRow:
    bot_id: str
    surface: str
    observed: "str | None"
    # conform | exception | tightened | loosened | divergent | unreadable | undeclared
    state: str
    expected: str  # the value compared against ("" when nothing is declared)
    pod_baseline_value: str  # always the pod baseline ("" when undeclared)
    exception_declared: bool = False
    exception_reason: str = ""
    error: str = ""
    # True when the POD declared no value for this surface — which is not the
    # same as ``state == STATE_UNDECLARED``. A bot with its own declared
    # exception on an undeclared surface classifies exception (or a drift
    # direction against that exception), because an exception IS a
    # declaration. B2's rule is written per-SURFACE ("no per-bot drift Signal
    # for an undeclared surface"), so it must key on this flag, not on the
    # state — otherwise it either suppresses a real alert or emits a Signal
    # against intent nobody declared.
    surface_undeclared: bool = False

    @property
    def stale_exception(self) -> bool:
        """Drift under a declared exception while matching the pod baseline.

        The bot conforms to the pod; the exception is what's wrong (revoke
        it). B2's reconcile must NOT "fix" the bot toward the exception.

        Asks ``DRIFT_STATES`` rather than naming the three post-Q7 states, so
        this stays correct if a fourth direction is ever added. It cannot
        fire on an undeclared surface: ``pod_baseline_value`` is ``""``
        there and no observed reading is the empty string.
        """
        return (
            self.state in DRIFT_STATES
            and self.exception_declared
            and self.observed == self.pod_baseline_value
        )

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "surface": self.surface,
            "observed": self.observed,
            "state": self.state,
            "expected": self.expected,
            "pod_baseline_value": self.pod_baseline_value,
            "stale_exception": self.stale_exception,
            "exception_declared": self.exception_declared,
            "exception_reason": self.exception_reason,
            "error": self.error,
            "surface_undeclared": self.surface_undeclared,
        }


@dataclass
class CensusReport:
    rows: list = field(default_factory=list)  # list[CensusRow]
    undeclared_surfaces: list = field(default_factory=list)  # SURFACES order
    # Surfaces whose DECLARED baseline value is itself a no-intent sentinel.
    # Seeding can no longer produce one (Q7(b)), but every baseline seeded
    # before this change can — and does, on both live pods — so without this
    # the rule lands inert: the file keeps declaring "custom"/"unset" as pod
    # intent and the census keeps reporting the fleet's own silence as
    # conform. The census cannot fix it (it must not write a baseline), so it
    # names it and points at `seed --force`.
    declared_sentinel_surfaces: list = field(default_factory=list)

    def counts(self) -> dict:
        """Per-state row counts, pre-filled with a zero for every state.

        Zeroes are present on purpose: the text summary already defaults
        them, but ``to_dict()["counts"]`` is B2's machine surface, and a
        payload that omits absent states makes "no loosened rows" and "this
        producer does not know about loosened" the same bytes.
        """
        out: dict = {state: 0 for state in STATE_DISPLAY_ORDER}
        for row in self.rows:
            out[row.state] = out.get(row.state, 0) + 1
        return out

    def undeclared_distribution(self) -> dict:
        """surface -> {observed value: bot count} over its UNDECLARED rows.

        Undeclaredness is a pod-level fact, so N identical per-bot rows are
        noise; this is the one line that replaces them, and it is also the
        operator's one-step path to declaring the surface ("7 bots already
        read ``coding``"). Rows that classify unreadable or exception on an
        undeclared surface are excluded by construction — they are not
        undeclared rows — so the distribution never launders a permission
        wall into an observed value. That means the counts here can total
        fewer than the pod's bot count; :meth:`undeclared_excluded` reports
        how many, so a caller can say so instead of implying completeness.
        """
        dist: dict = {}
        for surface in self.undeclared_surfaces:
            dist[surface] = {}
        for row in self.rows:
            if row.state != STATE_UNDECLARED:
                continue
            bucket = dist.setdefault(row.surface, {})
            key = row.observed if row.observed is not None else "?"
            bucket[key] = bucket.get(key, 0) + 1
        return dist

    def undeclared_excluded(self) -> dict:
        """surface -> {state: count} for rows the distribution leaves out.

        An undeclared surface's rows are not all undeclared rows: a bot
        whose config could not be read is ``unreadable``, and a bot with its
        own exception is ``exception`` or a drift direction. Those are real
        facts about the surface that the collapsed line must not imply away.
        """
        excluded: dict = {}
        undeclared = set(self.undeclared_surfaces)
        for row in self.rows:
            if row.surface not in undeclared or row.state == STATE_UNDECLARED:
                continue
            bucket = excluded.setdefault(row.surface, {})
            bucket[row.state] = bucket.get(row.state, 0) + 1
        return excluded

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "counts": self.counts(),
            "undeclared_surfaces": list(self.undeclared_surfaces),
            "undeclared_distribution": self.undeclared_distribution(),
            "undeclared_excluded": self.undeclared_excluded(),
            "declared_sentinel_surfaces": list(self.declared_sentinel_surfaces),
        }


def classify_readings(baseline: PodBaseline, readings: list) -> CensusReport:
    """Pure classification of BotReadings against the baseline."""
    rows: list = []
    for reading in readings:
        for surface in SURFACES:
            sr = reading.surfaces.get(surface) or SurfaceReading(None, "missing_reading")
            exc = baseline.exception_for(reading.bot_id, surface)
            undeclared = baseline.is_undeclared(surface)
            # "" not "unset" for an undeclared surface: there is no pod value
            # to report, and a placeholder here is precisely how a fleet's
            # silence used to render as a declared position.
            baseline_value = "" if undeclared else baseline.surfaces[surface]
            state, expected = classify_surface(
                sr.value, baseline_value, exc,
                surface=surface, undeclared=undeclared,
            )
            rows.append(
                CensusRow(
                    bot_id=reading.bot_id,
                    surface=surface,
                    observed=sr.value,
                    state=state,
                    expected=expected,
                    pod_baseline_value=baseline_value,
                    exception_declared=exc is not None,
                    exception_reason=exc.reason if exc is not None else "",
                    error=sr.error or "",
                    surface_undeclared=undeclared,
                )
            )
    return CensusReport(
        rows=rows,
        undeclared_surfaces=[s for s in SURFACES if baseline.is_undeclared(s)],
        declared_sentinel_surfaces=[
            s for s in SURFACES
            if not baseline.is_undeclared(s)
            and baseline.surfaces[s] in NO_INTENT_SENTINELS
        ],
    )
