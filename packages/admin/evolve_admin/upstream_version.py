"""upstream_version — detect per-bot OpenClaw version and fleet status.

Pillar V1.5-3 (Evolve v1.5 sprint). See sprint spec
``/Users/pod_admin/.claude/specs/evolve-v1-5-sprint.md`` §V1.5-3 and the
escalation note at ``.claude/specs/escalations/v1-5-3-20260512-1430.md``.

Why this module exists
----------------------

OpenClaw upstream ``v2026.4.12`` introduced the ``openclaw exec-policy``
CLI (#64050) — ``show``, ``preset``, and ``set`` subcommands that
synchronize requested ``tools.exec.*`` config with the local exec
approvals file. That is the upstream substitute for evolve's
team_bot_c/security_bot direct-``openclaw.json``-edit pattern documented in memory
``project_team_bot_c_safeguards``. Once every bot in the fleet is on
v2026.4.12+, we can retire the direct-edit pattern and route plugin
policy changes through ``openclaw exec-policy``.

This module answers two operator-facing questions:

  1. "What OC version is each of my bots on?" — :func:`per_bot_versions`
  2. "Is my whole fleet at or above the minimum we need to use the new
     upstream CLI?" — :func:`fleet_status`

A separate concern (the security_warden version-floor check) consumes
the same readings to surface a Signal when a bot drifts below the floor.

Spec footnote — "manifest verified" reframe
-------------------------------------------

The original V1.5-3 brief asked for a "manifest verified" indicator
referring to a cryptographically signed ``clawmanifest.json`` upstream.
**That feature is not present in any actual OpenClaw release** —
verified against ``gh release view v2026.4.12``, the
``v2026.4.27`` release notes, and ``docs.openclaw.ai/plugins/manifest``.
The pillar pivots to the real substitute: surface "exec-policy synced"
status — i.e., whether a bot's ``tools.exec.*`` config matches its
exec-approvals allowlist, which is what ``openclaw exec-policy`` brings
into the substrate.

Where the version comes from
----------------------------

Two readings are attempted, in order:

  1. **``openclaw.json:meta.lastTouchedVersion``** — populated by
     every recent ``openclaw`` invocation. This is the cheap path; no
     subprocess, no sudo. Confirmed populated on the May 2026 mini fleet
     (all six bots had ``"meta.lastTouchedVersion": "2026.4.29"`` at
     write time).
  2. **``openclaw --version`` subprocess** — fallback for bots that
     haven't run a recent ``openclaw`` command. Requires the right
     ``cwd`` (see ``reference_sudo_evolve_python_cwd`` in user memory)
     so we always pass ``cwd=/tmp``.

Both readings are best-effort; failures surface as
``BotVersionReading.read_error`` rather than raising.

Format
------

OpenClaw versions are CalVer ``YYYY.M.PATCH`` (e.g. ``2026.4.12``,
``2026.4.29``). Comparison is done via tuple-of-ints so ``2026.4.29 >=
2026.4.12``. Beta tags (``2026.5.12-beta.1``) are normalized to their
release version then ranked above the ``.0`` of the same release;
unparseable strings sort below every parseable version so an unparseable
reading is treated as "below floor" rather than "above".
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# Floor we want every bot at before the direct-edit migration starts.
# Sourced from the actual ``v2026.4.12`` release notes: that's the
# release that shipped ``openclaw exec-policy``.
DEFAULT_MINIMUM_VERSION: str = "2026.4.12"


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class BotVersionReading:
    """One bot's OC version + how we determined it."""

    bot_id: str
    version: str | None  # canonical "YYYY.M.PATCH" form, or None on read failure
    source: str  # "openclaw_json_meta" | "openclaw_cli" | "unavailable"
    read_error: str | None = None
    raw: str | None = None  # the raw string we parsed (for debugging)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FleetStatus:
    """Cross-bot view: are we above the upstream floor?"""

    minimum_version: str
    floor_met: bool  # True iff every reading has version >= minimum_version
    bots_at_or_above: list[str] = field(default_factory=list)
    bots_below: list[str] = field(default_factory=list)
    bots_unknown: list[str] = field(default_factory=list)
    readings: dict[str, BotVersionReading] = field(default_factory=dict)
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_version": self.minimum_version,
            "floor_met": self.floor_met,
            "bots_at_or_above": list(self.bots_at_or_above),
            "bots_below": list(self.bots_below),
            "bots_unknown": list(self.bots_unknown),
            "readings": {b: r.to_dict() for b, r in self.readings.items()},
            "observed_at": self.observed_at,
        }


# ── Version parsing + comparison ─────────────────────────────────────────────


# Anchored neither to start nor end — we use search() so banner-style
# strings ("OpenClaw 2026.4.29 (a448042)") match the embedded version.
# The version itself must look like ``vNNN.N.N`` optionally with a
# ``-prerelease`` tag. Case-insensitive so "OpenClaw" / "openclaw" /
# "OPENCLAW" all match the prefix.
_VERSION_RE = re.compile(
    r"""
    v?                          # optional leading 'v'
    (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)
    (?:-(?P<prerelease>[A-Za-z0-9.]+))?
    """,
    re.VERBOSE | re.IGNORECASE,
)


# Companion to _VERSION_RE for :func:`comparable_version` — same "find the
# version inside a banner string" job, but it captures the version token whole
# rather than as (major, minor, patch) groups: an arbitrary number of dotted
# numeric components plus an optional prerelease tag. Rebuilding a version
# from three groups is what silently turns `2026.7.0.5` into `2026.7.0`.
_EXACT_VERSION_RE = re.compile(
    r"""
    v?                                    # optional leading 'v'
    (?P<version>
        \d+(?:\.\d+)+                     # 2026.7.1 / 2026.7.0.5
        (?:-[A-Za-z0-9.]+)?               # -2 / -beta.1
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_version(raw: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``YYYY.M.PATCH[-pre]`` into a comparable tuple.

    Returns ``None`` for any unparseable string. Prerelease tags
    (``-beta.5``) drop the bot one rank below the matching release
    (``rank=0``) so ``2026.5.12-beta.1`` < ``2026.5.12``.

    The regex is not anchored to start-of-string — banner-style strings
    like ``"OpenClaw 2026.4.29 (a448042)"`` are accepted; the first
    ``M.N.P`` triple in the string wins.
    """
    if not raw:
        return None
    m = _VERSION_RE.search(raw)
    if not m:
        return None
    pre_rank = 0 if not m.group("prerelease") else -1
    return (
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
        pre_rank,
    )


def version_at_or_above(reading: str | None, floor: str) -> bool:
    """True iff ``reading`` parses to ``>= floor``.

    Unparseable readings are treated as below floor — defensive
    default: an unknown version is "needs attention" not "trust it".
    """
    a = parse_version(reading)
    b = parse_version(floor)
    if a is None or b is None:
        return False
    return a >= b


def canonical_version(raw: str | None) -> str | None:
    """Return ``YYYY.M.PATCH`` (no prerelease) if parseable, else ``None``.

    Release-level identity: ``2026.7.1-2`` and ``2026.7.1`` both canonicalize
    to ``2026.7.1``. That is the right form for a version FLOOR check, and the
    wrong form for an equality check against anything npm reports — see
    :func:`comparable_version`.
    """
    p = parse_version(raw)
    if p is None:
        return None
    return f"{p[0]}.{p[1]}.{p[2]}"


def comparable_version(raw: str | None) -> str | None:
    """Return ``YYYY.M.PATCH[-prerelease]`` if parseable, else ``None``.

    Same parse as :func:`canonical_version`, but the prerelease tag SURVIVES.
    Use this for any value that will be compared against a version string npm
    produced — npm reports ``2026.7.1-2`` verbatim in both the registry's
    ``version`` field and the installed ``package.json``, and dropping the
    ``-2`` makes the two sides permanently unequal. That asymmetry (installed
    canonicalized, npm's latest raw) is what made a fully-current pod show a
    phantom "update available" AND flag every safety report stale seconds
    after it was written, which in turn hid the "Run upgrade now" button.

    Only cosmetics are normalized away — surrounding text and a leading
    ``v``. Every component of the version itself survives, including a fourth
    dotted component (``2026.7.0.5``), which ``parse_version`` truncates.
    Deliberately strict: two strings this function reports as different might
    still be the same build, and the cost of that is a re-check; the reverse
    error silently approves an upgrade against gates that ran elsewhere.
    """
    if not raw:
        return None
    m = _EXACT_VERSION_RE.search(raw)
    return m.group("version") if m else None


def same_version(a: str | None, b: str | None) -> bool:
    """True iff *a* and *b* name the same OC build, prerelease tag included.

    Normalizes both sides so a comparison never turns on cosmetic differences
    (a leading ``v``, a banner wrapper) — the failure mode this function
    exists to make unrepresentable is comparing two version strings that were
    normalized differently on the way in.

    Fails closed: if either side is unparseable, only a byte-identical pair
    counts as the same version. Two ``None``\\ s are NOT the same version —
    "we don't know" is never "they match".
    """
    ca, cb = comparable_version(a), comparable_version(b)
    if ca is None or cb is None:
        return bool(a) and (a or "").strip() == (b or "").strip()
    return ca == cb


# ── Per-bot reading ──────────────────────────────────────────────────────────


def _read_openclaw_json_meta_version(
    bot_id: str,
    bot_home: Path,
    *,
    sudo_cat_timeout: float = 5.0,
) -> tuple[str | None, str | None]:
    """Try to read ``meta.lastTouchedVersion`` from ``openclaw.json``.

    Returns ``(version_string, error)``. On success ``version_string`` is
    a parseable CalVer string; ``error`` is ``None``. On failure
    ``version_string`` is ``None`` and ``error`` carries the reason.

    Mirrors the read pattern in plugins.inventory: direct read first,
    sudo ``/bin/cat`` fallback for un-ACL'd homes.
    """
    path = bot_home / ".openclaw" / "openclaw.json"

    text: str | None
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "openclaw_json_not_found"
    except PermissionError:
        text = None
    except OSError as exc:
        return None, f"os_error: {exc}"

    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=sudo_cat_timeout,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            return None, "sudo_cat_timeout"
        except OSError as exc:
            return None, f"sudo_cat_error: {exc}"
        if r.returncode != 0:
            return None, f"sudo_cat_rc={r.returncode}"
        text = r.stdout

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"

    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        return None, "meta_block_not_object"
    raw = meta.get("lastTouchedVersion")
    if not isinstance(raw, str) or not raw.strip():
        return None, "lastTouchedVersion_absent"
    return raw.strip(), None


def _read_openclaw_cli_version(
    bot_user: str,
    *,
    timeout: float = 10.0,
) -> tuple[str | None, str | None]:
    """Best-effort ``sudo -u <bot> openclaw --version`` via cwd=/tmp.

    Per ``reference_sudo_evolve_python_cwd``, python (and node) processes
    add ``cwd`` to their startup paths and the ``evolve`` user can't
    read another user's home; passing ``cwd=/tmp`` keeps the subprocess
    inside a directory it can actually traverse.

    Returns ``(raw_version_line, error)``. The caller :func:`parse_version`
    handles the ``OpenClaw 2026.4.29 (a448042)`` shape.
    """
    try:
        r = subprocess.run(
            ["sudo", "-u", bot_user, "openclaw", "--version"],
            capture_output=True, text=True, timeout=timeout, cwd="/tmp",
        )
    except subprocess.TimeoutExpired:
        return None, "cli_timeout"
    except FileNotFoundError:
        return None, "sudo_or_openclaw_missing"
    except OSError as exc:
        return None, f"cli_os_error: {exc}"
    if r.returncode != 0:
        return None, f"cli_rc={r.returncode}: {(r.stderr or '').strip()[:200]}"
    out = (r.stdout or "").strip()
    if not out:
        return None, "cli_empty_stdout"
    # First non-empty line is the version banner.
    return out.splitlines()[0].strip(), None


def read_bot_version(
    bot_id: str,
    *,
    bot_home: Path | None = None,
    bot_user: str | None = None,
    allow_cli_fallback: bool = True,
) -> BotVersionReading:
    """Determine one bot's OC version.

    Tries the cheap path first (``openclaw.json:meta.lastTouchedVersion``),
    falls back to ``openclaw --version`` only when the cheap path failed
    AND ``allow_cli_fallback`` is true. The fallback is opt-out because
    the CLI invocation is expensive (~hundreds of ms per bot) and
    requires the ``evolve→bot`` sudo grant.

    ``bot_user`` defaults to ``bot_id``. ``bot_home`` defaults to
    ``/Users/<bot_user>``.
    """
    user = bot_user or bot_id
    home = bot_home or Path(f"/Users/{user}")

    raw_meta, meta_err = _read_openclaw_json_meta_version(bot_id, home)
    if raw_meta and parse_version(raw_meta) is not None:
        return BotVersionReading(
            bot_id=bot_id,
            version=canonical_version(raw_meta),
            source="openclaw_json_meta",
            raw=raw_meta,
        )

    if not allow_cli_fallback:
        return BotVersionReading(
            bot_id=bot_id,
            version=None,
            source="unavailable",
            read_error=meta_err or "lastTouchedVersion_unparseable",
            raw=raw_meta,
        )

    raw_cli, cli_err = _read_openclaw_cli_version(user)
    if raw_cli and parse_version(raw_cli) is not None:
        return BotVersionReading(
            bot_id=bot_id,
            version=canonical_version(raw_cli),
            source="openclaw_cli",
            raw=raw_cli,
        )

    # Both paths failed — surface the most informative error.
    err = meta_err
    if cli_err:
        err = f"{err}; cli: {cli_err}" if err else cli_err
    return BotVersionReading(
        bot_id=bot_id,
        version=None,
        source="unavailable",
        read_error=err,
        raw=raw_meta or raw_cli,
    )


# ── Installed-binary reader ──────────────────────────────────────────────────

# Canonical path the gateway plists/systemd units pin to. Reading this answers
# the question "what OC binary is installed on this host" — distinct from "what
# version has this bot's gateway written into its config" (the lastTouchedVersion
# reading above), which lags until the gateway processes a real session.
#
# Platform-keyed via platform_profile (#3194 follow-up): the first candidate that
# exists (Linux `/usr/lib/node_modules/openclaw/package.json`, macOS
# `/opt/homebrew/...`). Resolved lazily — byte-identical to the prior macOS
# hardcode on the macOS profile. Mirrors `update_watcher` /
# `ocadmin._resolve_openclaw_package_json`.
def _resolve_openclaw_package_json() -> Path:
    from platform_profile import get_profile

    candidates = get_profile().openclaw_pkg_json_candidates
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def installed_package_version(
    *, package_json: Path | None = None,
) -> str | None:
    """Return the OC version recorded in the installed package.json.

    This is the authoritative answer to "what's installed on this host" —
    same source the ``evolve-admin oc upgrade`` CLI checks
    (``ocadmin._installed_version``). Returns the ``YYYY.M.PATCH[-pre]``
    form, or ``None`` if the file is missing, unreadable, or contains an
    unparseable version.

    The prerelease tag is KEPT (:func:`comparable_version`, not
    :func:`canonical_version`). This value's only job is to be compared and
    displayed against npm's version strings, which carry that tag: an
    installed ``2026.7.1-2`` reported as ``2026.7.1`` is both a false banner
    ("update available" to a version the host already runs) and a false
    staleness verdict against every safety report, which recorded the
    untruncated string.

    Distinct from :func:`read_bot_version` which reports the per-bot
    ``lastTouchedVersion`` (a lagging signal that only updates when OC
    writes back to the bot's config — gateway boot doesn't count).
    """
    path = package_json or _resolve_openclaw_package_json()
    try:
        raw = json.loads(path.read_text()).get("version")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, str):
        return None
    return comparable_version(raw)


# ── Fleet-wide rollup ────────────────────────────────────────────────────────


def per_bot_versions(
    bot_ids: list[str],
    *,
    network: dict | None = None,
    bot_home_resolver: Callable[[str], Path] | None = None,
    allow_cli_fallback: bool = True,
) -> dict[str, BotVersionReading]:
    """Read OC version for every bot. Never raises — failures captured.

    ``bot_home_resolver``: callable used to resolve a bot's macOS home.
    If absent, falls back to ``network['bots'][bid].user`` then to
    ``/Users/<bot_id>``.
    """
    out: dict[str, BotVersionReading] = {}
    bots_block = (network or {}).get("bots") or {}

    for bot_id in bot_ids:
        bot_cfg = bots_block.get(bot_id) or {}
        user = bot_cfg.get("user") or bot_id
        home: Path | None = None
        if bot_home_resolver is not None:
            try:
                home = bot_home_resolver(bot_id)
            except Exception:
                home = None
        if home is None:
            home = Path(f"/Users/{user}")
        try:
            out[bot_id] = read_bot_version(
                bot_id,
                bot_home=home,
                bot_user=user,
                allow_cli_fallback=allow_cli_fallback,
            )
        except Exception as exc:  # pragma: no cover — defensive
            out[bot_id] = BotVersionReading(
                bot_id=bot_id,
                version=None,
                source="unavailable",
                read_error=f"{type(exc).__name__}: {exc}",
            )
    return out


def fleet_status(
    readings: dict[str, BotVersionReading],
    *,
    minimum_version: str = DEFAULT_MINIMUM_VERSION,
    now: datetime | None = None,
) -> FleetStatus:
    """Compute fleet-wide rollup.

    ``floor_met`` is true iff every reading parses and is ``>= minimum``.
    A single "unknown" reading flips ``floor_met`` to false — we
    intentionally don't assume "probably fine" for bots we couldn't
    read.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    fs = FleetStatus(
        minimum_version=minimum_version,
        floor_met=True,
        readings=dict(readings),
        observed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    for bot_id, r in sorted(readings.items()):
        if r.version is None:
            fs.bots_unknown.append(bot_id)
            fs.floor_met = False
            continue
        if version_at_or_above(r.version, minimum_version):
            fs.bots_at_or_above.append(bot_id)
        else:
            fs.bots_below.append(bot_id)
            fs.floor_met = False
    return fs


# ── exec-policy compliance check ─────────────────────────────────────────────
# Phase 1: a structural check, not a CLI invocation. The full upstream
# CLI (``openclaw exec-policy show``) returns the same data we can infer
# from openclaw.json + exec-approvals.json, so we read both and compare.


# Chip state — see internal/spec-app-derived-permissions-2026-05-24.md §7.
# Pivoted 2026-05-25 from "are exec permissions tight?" to "is the bot's
# posture coherent?". Default for member bots is GREEN; tightening is an
# operator choice, celebrated when chosen, not demanded by the chip.

STATE_GREEN = "green"   # default mode, working as intended
STATE_YELLOW = "yellow" # visibility gap or operator-set overreach
STATE_RED = "red"       # broken state (apps can't run, or deny+has-apps)
STATE_GREY = "grey"     # legacy unclassified entries (migration pending)
STATE_UNKNOWN = "unknown"  # couldn't read inputs


@dataclass
class ExecPolicyComplianceReading:
    """Whether a bot's exec posture is coherent.

    ``state`` is the chip color per spec §7:
      - ``green`` — coherent posture (default mode working, or tightened
        and consistent).
      - ``yellow`` — visibility gap (manifests don't cover some scripts)
        or operator-set entries with suspect wildcards.
      - ``red`` — broken (apps declared things the allowlist denies, or
        member bot stuck in deny mode with apps installed).
      - ``grey`` — legacy entries pending migration to app-derived /
        operator-set classification.
      - ``unknown`` — couldn't read enough inputs to decide.

    ``compliant`` is retained for backward compat with the v1.5-3 chip
    plumbing — True when state is green, False when red, None otherwise.
    """

    bot_id: str
    compliant: bool | None  # green=True, red=False, else=None
    state: str = STATE_UNKNOWN
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compliant_for_state(state: str) -> bool | None:
    """Backward-compat mapping for the boolean ``compliant`` field."""
    if state == STATE_GREEN:
        return True
    if state == STATE_RED:
        return False
    return None  # yellow / grey / unknown


def evaluate_exec_policy_compliance(
    bot_id: str,
    *,
    openclaw_config: dict | None,
    exec_approvals: dict | None,
    role: str = "",
    has_installed_apps: bool | None = None,
    primary_bot_id: str | None = None,
) -> ExecPolicyComplianceReading:
    """Decide the Safety-card chip state for one bot's exec posture.

    Implements the state table in
    internal/spec-app-derived-permissions-2026-05-24.md §7. The function is
    pure (no I/O); callers pass live config + the bits of context the
    state machine needs.

    Parameters:
      - ``openclaw_config`` — the bot's parsed openclaw.json (or None).
      - ``exec_approvals``  — the bot's parsed exec-approvals.json (or None
        when the file is absent, an empty dict; when it can't be read at
        all, ``None``).
      - ``role`` — bot role from network.json (``"primary"`` | ``"member"``
        | ...). Used to distinguish "deny is the right answer" (primary
        bots — green) from "deny is broken" (member bots with apps —
        red).
      - ``has_installed_apps`` — optional hint: does the bot have at least
        one non-hidden installed app? When unknown (None), the
        ``deny + apps`` red state degrades to ``unknown`` rather than
        guessing.

    Returns an ``ExecPolicyComplianceReading`` with ``state``,
    ``compliant`` (back-compat bool), ``reason``, and ``details``.
    """
    if openclaw_config is None:
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_UNKNOWN,
            compliant=None,
            reason="openclaw.json unavailable",
        )

    tools = openclaw_config.get("tools") or {}
    exec_cfg = tools.get("exec") or {} if isinstance(tools, dict) else {}
    if not isinstance(exec_cfg, dict):
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_UNKNOWN,
            compliant=None,
            reason="tools.exec missing or malformed",
        )

    security = str(exec_cfg.get("security") or "").lower().strip()
    role_norm = (role or "").lower()
    # Primary detection is role-driven for configured pods (every pod sets
    # role="primary" on its primary). The optional ``primary_bot_id`` lets the
    # caller pass the RESOLVED primary id (via primary_bot.primary_bot_id) so a
    # legacy pod that never set the role still classifies correctly — on those
    # pods the resolver returns "evolve", on a renamed pod "evo". We no longer
    # hardcode bot_id == "evolve", which mislabeled an "evo"-primary bot.
    is_primary = role_norm == "primary" or (
        bool(primary_bot_id) and bot_id == primary_bot_id
    )

    inline_allowlist = (
        exec_cfg.get("allowlist")
        or exec_cfg.get("allow")
        or exec_cfg.get("allowed_commands")
    )
    inline_count = (
        len(inline_allowlist) if isinstance(inline_allowlist, (list, tuple)) else 0
    )

    approvals_count = 0
    if isinstance(exec_approvals, dict):
        agents = exec_approvals.get("agents") or {}
        if isinstance(agents, dict):
            for agent_block in agents.values():
                if not isinstance(agent_block, dict):
                    continue
                al = (
                    agent_block.get("allowlist")
                    or agent_block.get("approvals")
                    or agent_block.get("allow")
                )
                if isinstance(al, (list, dict)):
                    approvals_count += len(al)

    total_allow = inline_count + approvals_count
    details: dict[str, Any] = {
        "security": security,
        "role": role_norm or "member",
        "inline_allowlist_size": inline_count,
        "approvals_size": approvals_count,
        "has_installed_apps": has_installed_apps,
    }

    # ── deny ─────────────────────────────────────────────────────────
    if security == "deny":
        # Phase E.4 (2026-05-25): primary bots no longer get a free
        # green pass for ``deny``. Post-cutover, evo's right posture is
        # the same as any member bot's — ``full`` by default. Operator-
        # pinned ``deny`` is allowed but treated as an unusual state
        # (yellow chip), surfacing the override rather than blessing it.
        if is_primary:
            return ExecPolicyComplianceReading(
                bot_id=bot_id,
                state=STATE_UNKNOWN,
                compliant=None,
                reason=(
                    "tools.exec.security='deny' on the primary bot. "
                    "Phase E.4 removed the carve-out that treated this "
                    "as the intended posture — the new default is "
                    "'full' (same as member bots). If this is an "
                    "operator-pinned override, ignore the chip; "
                    "otherwise flip exec back to the default."
                ),
                details=details,
            )
        # Member bot in deny mode. With installed apps this is the
        # original incident shape (team_bot_a/admin_bot/team_bot_b/personal_bot/team_bot_c stuck in
        # deny while INSTALLED_APPS.md declared work). Without that
        # signal we can't decide — return unknown rather than green.
        if has_installed_apps is True:
            return ExecPolicyComplianceReading(
                bot_id=bot_id,
                state=STATE_RED,
                compliant=False,
                reason=(
                    "tools.exec.security='deny' on a member bot with installed apps "
                    "— scripts the bot declares can't run"
                ),
                details=details,
            )
        if has_installed_apps is False:
            return ExecPolicyComplianceReading(
                bot_id=bot_id,
                state=STATE_GREEN,
                compliant=True,
                reason="tools.exec.security='deny' with no installed apps — coherent",
                details=details,
            )
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_UNKNOWN,
            compliant=None,
            reason=(
                "tools.exec.security='deny' on a member bot; can't tell whether "
                "apps are declared (has_installed_apps unknown)"
            ),
            details=details,
        )

    # ── full ─────────────────────────────────────────────────────────
    if security == "full":
        # Member-bot default. Per spec §7: GREEN by default. The chip
        # used to flag full + empty allowlist as "permissive"; that
        # pathologized the right answer. Tightening is opt-in.
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_GREEN,
            compliant=True,
            reason=(
                "tools.exec.security='full' — member-bot default; "
                "manifest-derived preview available for opt-in hardening"
            ),
            details=details,
        )

    # ── allowlist ────────────────────────────────────────────────────
    if security == "allowlist":
        if total_allow == 0:
            # No allowlist entries despite allowlist mode — scripts can't run.
            return ExecPolicyComplianceReading(
                bot_id=bot_id,
                state=STATE_RED,
                compliant=False,
                reason=(
                    "tools.exec.security='allowlist' with empty allowlist — "
                    "nothing the bot declares can execute"
                ),
                details=details,
            )
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_GREEN,
            compliant=True,
            reason=(
                f"tools.exec.security='allowlist' with {total_allow} entry(ies) — "
                "operator opted into tighter posture"
            ),
            details=details,
        )

    # ── off / unrecognized ───────────────────────────────────────────
    if security == "off":
        return ExecPolicyComplianceReading(
            bot_id=bot_id,
            state=STATE_GREEN,
            compliant=True,
            reason="tools.exec.security='off' — exec disabled at the source",
            details=details,
        )

    return ExecPolicyComplianceReading(
        bot_id=bot_id,
        state=STATE_UNKNOWN,
        compliant=None,
        reason=f"tools.exec.security={security!r} (unrecognized)",
        details=details,
    )
