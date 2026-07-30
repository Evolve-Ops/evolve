"""
scheduled_actions_validator — gate on prose-vs-structured drift and on
per-entry install wiring.

The forge install pipeline only installs daemons / cron entries declared
in ``manifest.scheduled_actions[]`` — see ``forge_engine._materialize_scheduled_actions``
(packages/admin/evolve_admin/applications/forge_engine.py §3000+). A gallery
package whose ``build_spec`` *narratively* describes a LaunchDaemon or cron
without a corresponding structured entry will install with no error: scripts
land in the bot's workspace, the manifest is marked approved, and **no cron
is ever scheduled**. The app silently fails to run.

The same silent-dead-app shape exists one level down: an entry with the
right mechanism but a missing/malformed ``install`` recipe fails at
materialize time, and that failure is best-effort (recorded in a summary,
install still "succeeds"). ``_validate_action_wiring`` mirrors the
materialize requirements per mechanism so the broken recipe is refused at
install time instead — the ``scheduled_actions[]`` analogue of the wiring
checks ``bot_guidance_freelance_validator`` does for ``event_triggers[]``
(manifest-v7 Slice 1, docs/spec-manifest-v7-slicing-2026-06-10.md §3.2).

This module catches that mismatch at two checkpoints:

1. **Gallery preflight** — ``gallery.preflight_check`` surfaces a result
   with ``severity: "build_blocker"`` so the install dispatch refuses with
   a clear remediation message.

2. **Forge run_forge_job** — re-validates at Step 1 (right after the manifest
   is seeded from the gallery package) and again after the apply-phase
   spec reconciliation, so a re-build that introduces daemon prose without
   the structured action also fails loudly before manifest save.

Why a textual heuristic on ``build_spec``: the build_spec is the source of
truth for what the forge LLM is asked to produce. If it describes a daemon,
that intent must show up in the structured ``scheduled_actions[]`` so the
installer can materialize it. The heuristic uses high-specificity patterns
(file paths under ``/Library/Launch{Daemons,Agents}/``, launchd keywords,
``cron-driven`` framing) to avoid false positives on passing mentions
("this is faster than a cron job").

Reference incident: Atlas Daily Digest (2026-06-04) — installed cleanly,
ran zero times. Survey at install: 8 of 12 gallery packages exhibit the
same shape.
"""

from __future__ import annotations

import re
from typing import Any


# ── Detection ────────────────────────────────────────────────────────────────

# Patterns that strongly imply ``build_spec`` describes a daemon / cron
# install path. Add ONLY patterns with very low false-positive rates:
# a passing mention of "cron" in a comparison must not trip on its own.
#
# The patterns are split into two tiers because they carry different
# false-positive risk:
#
# **Strong** markers are self-sufficient — a plist file path, an explicit
# ``## FILE:`` plist emission, a launchd load verb, a schedule key, or
# ``cron-driven`` framing is unambiguous evidence that *this* build_spec
# installs a schedule. One (non-negated) hit is enough.
#
# **Weak** markers are the bare identifier words ``LaunchDaemon`` /
# ``LaunchAgent``. They fire on a real self-description ("the LaunchDaemon
# is the only scheduler") but ALSO on passing references to unrelated infra
# ("the repo-puller LaunchDaemon refreshes the deploy checkout"). A weak
# marker on its own is treated as evidence UNLESS the spec also carries an
# explicit no-schedule / event-driven disclaimer (see
# ``_NO_SCHEDULE_DISCLAIMER_RE``), in which case the mention is a reference,
# not a self-description, and is suppressed.
#
# Either tier's matches are returned to the caller so the error message can
# show the operator/manifest author exactly what tripped.
_STRONG_DAEMON_PATTERNS: tuple[str, ...] = (
    # File paths — unambiguous: the spec is describing where a plist lands.
    r"/Library/LaunchDaemons/",
    r"/Library/LaunchAgents/",
    # launchd CLI verbs that load/install a plist (not just "launchctl print").
    r"\blaunchctl\s+(?:bootstrap|load|kickstart)\b",
    # launchd plist schedule keys.
    r"\bStartCalendarInterval\b",
    r"\bStartInterval\b",
    # crontab editing verbs.
    r"\bcrontab\s+-[el]\b",
    # Framing phrases that describe a scheduled install path (not passing
    # mentions like "faster than a cron job").
    r"\bcron[-\s]?driven\b",
    r"\bcron trigger\b",
    # Explicit forge file emissions — the strongest signal.
    r"##\s*FILE:\s*/Library/LaunchDaemons/",
    r"##\s*FILE:\s*/Users/[^/]+/Library/LaunchAgents/",
    r"##\s*FILE:\s*~/Library/LaunchAgents/",
)

# Identifier-bounded names — match "LaunchDaemon" but not e.g. "Daemonic"
# or the plural path form "LaunchDaemons/" (the \b fails before the "s").
_WEAK_DAEMON_PATTERNS: tuple[str, ...] = (
    r"\bLaunchDaemon\b",
    r"\bLaunchAgent\b",
)

# A negation immediately preceding a marker ("No cron, no LaunchDaemon, no
# scheduled execution") flips it from a positive install claim into a
# disclaimer. We look back a short window of word / whitespace / comma
# characters (any other character — period, em-dash, colon, paren — stops
# the scan; ``\s`` does match a newline, so the 15-char cap is what keeps
# the window local) so only a *nearby* negation counts. Matched occurrences
# are dropped from the marker set.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|never|neither|nor)\b[\w\s,]{0,15}$",
    re.IGNORECASE,
)

# An explicit statement that the app installs no scheduler. When present, a
# weak-only marker (a bare ``LaunchDaemon`` / ``LaunchAgent`` word with no
# corroborating strong marker) is read as a passing reference to other infra
# rather than a self-description, and is suppressed. Strong markers are NOT
# suppressed — a spec cannot both emit a plist and be "entirely event-driven",
# and if it does the real emission wins.
_NO_SCHEDULE_DISCLAIMER_RE = re.compile(
    r"\bno\s+(?:cron|plist|launch\s?daemon|launch\s?agent|scheduled|schedule)\b"
    r"|\b(?:entirely|purely|fully)\s+event[-\s]?driven\b",
    re.IGNORECASE,
)


# Mechanisms that count as a "real" scheduled-action declaration. An action
# with mechanism in {"", "unknown", "external"} is NOT installable by forge
# (see ``forge_engine._materialize_scheduled_actions``), so it does NOT
# satisfy the gate — an empty install recipe is functionally identical to
# no entry at all.
_FORGE_INSTALLABLE_MECHANISMS: frozenset[str] = frozenset({
    "oc_heartbeat_instruction",
    "oc_session_instruction",
    "launchd",
    "launchd_python_signal",
    "crontab",
})

# Mechanisms forge skips without attempting an install: pre-attribution
# (empty / "unknown") and operator-managed ("external") entries. No wiring
# contract to check.
_NON_INSTALL_MECHANISMS: frozenset[str] = frozenset({"", "unknown", "external"})

# Deprecated in v17; ``migrate_manifest`` rewrites these on load, so a
# package still carrying one was authored against the dead shape and
# materialize would refuse it.
_DEPRECATED_MECHANISMS: frozenset[str] = frozenset({
    "oc_heartbeat_hook",
    "oc_session_hook",
})

# Mirrors ``forge_engine._DURATION_RE`` (the validator keeps a local copy
# so it doesn't import the forge engine — same pattern as the freelance
# validator's protocol list). Keep in sync.
_EVERY_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$", re.IGNORECASE)


def _has_valid_schedule_shape(install_cfg: dict) -> tuple[bool, str]:
    """Check the schedule declaration shapes ``forge_engine._normalise_schedule``
    accepts: ``install.every`` ("30m"/"2h"/"1d"), ``install.every_minutes``
    (int >= 1), or ``install.schedule`` (dict with every_minutes or cron).

    Returns ``(ok, error)``; error is "" when ok.
    """
    every = install_cfg.get("every")
    if every is not None:
        if not isinstance(every, str) or not _EVERY_DURATION_RE.match(every):
            return False, (
                f"install.every must be a duration like '30m' / '2h' / '1d'; "
                f"got {every!r}"
            )
        return True, ""
    if "every_minutes" in install_cfg:
        try:
            minutes = int(install_cfg["every_minutes"])
        except (TypeError, ValueError):
            return False, (
                f"install.every_minutes must be an integer; got "
                f"{install_cfg['every_minutes']!r}"
            )
        if minutes < 1:
            return False, "install.every_minutes must be >= 1"
        return True, ""
    schedule = install_cfg.get("schedule")
    if isinstance(schedule, dict) and schedule:
        if "every_minutes" in schedule or "cron" in schedule:
            return True, ""
        return False, "install.schedule must declare either 'every_minutes' or 'cron'"
    return False, (
        "install must declare a schedule: one of install.every ('30m'/'2h'/'1d'), "
        "install.every_minutes (int), or install.schedule (dict)"
    )


def _validate_action_wiring(action: Any, idx: int) -> list[str]:
    """Return error messages for one ``scheduled_actions[]`` entry's install
    wiring — the per-entry analogue of the freelance validator's
    ``_validate_trigger_invocation``.

    Each check mirrors a hard-failure branch in
    ``forge_engine._materialize_scheduled_actions`` (keep in sync), so a
    flagged entry is one that WOULD fail at materialize time. Today that
    failure is best-effort — recorded in a Phase 4.5 summary, install still
    "succeeds" — which is the same silent-dead-app shape the coverage gate
    exists for. This surfaces it at install time instead.

    Entries forge would not attempt are exempt: non-install mechanisms
    (empty / unknown / external) and entries already carrying an
    ``installed_artifact`` (previously materialized by forge, or
    hand-installed and scanner-attributed — re-checking those would block
    rebuilds of working apps).
    """
    if not isinstance(action, dict):
        return [
            f"scheduled_actions[#{idx}] is not an object "
            f"(got {type(action).__name__}) — forge skips it and nothing "
            f"is ever scheduled."
        ]

    action_id = action.get("id") or f"#{idx}"
    mechanism = (action.get("mechanism") or "").strip()

    if mechanism in _NON_INSTALL_MECHANISMS:
        return []
    if action.get("installed_artifact"):
        return []

    install_cfg = action.get("install") or {}
    if not isinstance(install_cfg, dict):
        return [
            f"scheduled_actions[{action_id}].install must be an object, "
            f"got {type(install_cfg).__name__}."
        ]

    if mechanism in _DEPRECATED_MECHANISMS:
        return [
            f"scheduled_actions[{action_id}].mechanism {mechanism!r} is "
            f"deprecated in v17 — use the corresponding _instruction "
            f"mechanism (see docs/spec-heartbeat-instruction-2026-06-03.md)."
        ]

    errors: list[str] = []
    if mechanism in ("oc_heartbeat_instruction", "oc_session_instruction"):
        missing = [
            name for name in ("file", "section_anchor", "body")
            if not (install_cfg.get(name) or "").strip()
        ]
        if missing:
            errors.append(
                f"scheduled_actions[{action_id}] ({mechanism}) install "
                f"requires {', '.join(missing)}."
            )
    elif mechanism == "launchd":
        label = (install_cfg.get("plist_label") or "").strip()
        command = (install_cfg.get("command") or "").strip()
        schedule = install_cfg.get("schedule")
        plist_xml = install_cfg.get("plist_xml", "")
        if not label:
            errors.append(
                f"scheduled_actions[{action_id}] (launchd) install requires "
                f"plist_label."
            )
        if command:
            if not isinstance(schedule, dict) or not schedule:
                errors.append(
                    f"scheduled_actions[{action_id}] (launchd) install.schedule "
                    f"must be a non-empty dict when install.command is set "
                    f"(every_minutes=N OR cron={{...}})."
                )
        elif not plist_xml:
            errors.append(
                f"scheduled_actions[{action_id}] (launchd) install must declare "
                f"either install.command + install.schedule (canonical) or "
                f"install.plist_xml (legacy)."
            )
    elif mechanism == "launchd_python_signal":
        missing = [
            name for name, value in [
                ("label", (install_cfg.get("label") or "").strip()),
                ("command", (install_cfg.get("command") or "").strip()),
                ("signal_patterns", install_cfg.get("signal_patterns")),
            ] if not value
        ]
        if missing:
            errors.append(
                f"scheduled_actions[{action_id}] (launchd_python_signal) "
                f"install requires {', '.join(missing)}."
            )
        sched_ok, sched_err = _has_valid_schedule_shape(install_cfg)
        if not sched_ok:
            errors.append(
                f"scheduled_actions[{action_id}] (launchd_python_signal): "
                f"{sched_err}"
            )
    elif mechanism == "crontab":
        # install_crontab_entry is unimplemented (sudoers gap — see its
        # docstring); every fresh crontab entry fails at materialize.
        errors.append(
            f"scheduled_actions[{action_id}] uses mechanism 'crontab', which "
            f"forge cannot install (no sudoers grant). Use 'launchd' or an "
            f"oc_*_instruction mechanism instead."
        )
    else:
        errors.append(
            f"scheduled_actions[{action_id}] has unrecognized mechanism "
            f"{mechanism!r} — forge would fail this entry. Known installable "
            f"mechanisms: " + ", ".join(sorted(_FORGE_INSTALLABLE_MECHANISMS - {'crontab'})) + "."
        )
    return errors


def _marker_hits(build_spec: str, patterns: tuple[str, ...]) -> list[tuple[int, str]]:
    """Return ``(position, matched_text)`` for every non-negated marker hit.

    A hit whose immediately-preceding prose is a negation (``_NEGATION_RE``)
    is dropped — "No cron, no LaunchDaemon" describes the *absence* of a
    scheduler, not its installation.
    """
    hits: list[tuple[int, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, build_spec):
            if _NEGATION_RE.search(build_spec[: m.start()]):
                continue
            hits.append((m.start(), m.group(0)))
    return hits


def detect_unstructured_schedule(build_spec: Any) -> list[str]:
    """Return the distinct daemon/cron marker strings found in ``build_spec``.

    Empty list = no scheduled side-effect detected.
    Markers are returned in first-seen order, de-duplicated as exact strings,
    so the caller can show them verbatim in an error message.

    Detection is two-tier (see ``_STRONG_DAEMON_PATTERNS`` /
    ``_WEAK_DAEMON_PATTERNS``): a strong marker is self-sufficient, while a
    weak bare-word marker only counts when the spec does NOT also carry an
    explicit no-schedule / event-driven disclaimer. Negated occurrences are
    dropped in both tiers. This keeps the gate firing on a genuine
    "the LaunchDaemon is the only scheduler" spec while not tripping on an
    event-driven app that merely mentions unrelated infra in passing (the
    Atlas article-capture / on-demand-research false positives).

    Tolerant of non-string input (returns ``[]``).
    """
    if not isinstance(build_spec, str) or not build_spec:
        return []

    strong = _marker_hits(build_spec, _STRONG_DAEMON_PATTERNS)
    weak = _marker_hits(build_spec, _WEAK_DAEMON_PATTERNS)

    if not strong and weak and _NO_SCHEDULE_DISCLAIMER_RE.search(build_spec):
        # Weak-only mention + an explicit "no cron / entirely event-driven"
        # declaration ⇒ the bare LaunchDaemon/LaunchAgent word is a reference
        # to other infra, not this app's scheduler. Suppress.
        return []

    seen: dict[str, None] = {}  # insertion-ordered dedup, first-seen by position
    for _pos, text in sorted(strong + weak, key=lambda t: t[0]):
        seen.setdefault(text, None)
    return list(seen.keys())


def _installable_action_count(actions: Any) -> int:
    """Count ``scheduled_actions[]`` entries with a forge-installable mechanism.

    Tolerant of None / non-list / non-dict entries (returns ``0`` for non-
    iterable inputs; skips non-dict entries silently). The point is to
    answer the gate question — "does the installer have at least one entry
    it can act on?" — not to validate per-entry shape.
    """
    if not isinstance(actions, list):
        return 0
    count = 0
    for a in actions:
        if not isinstance(a, dict):
            continue
        mech = (a.get("mechanism") or "").strip()
        if mech in _FORGE_INSTALLABLE_MECHANISMS:
            count += 1
    return count


def validate_scheduled_actions(pkg_or_manifest: Any) -> dict:
    """Validate ``scheduled_actions[]``: prose coverage + per-entry wiring.

    Two layers, both surfaced through one result:

    - **Coverage** (the original Atlas-incident gate): build_spec prose
      that describes a daemon/cron must be matched by at least one
      forge-installable ``scheduled_actions[]`` entry.
    - **Wiring** (manifest-v7 Slice 1, docs/spec-manifest-v7-slicing-2026-06-10.md
      §3.2): each entry forge would attempt must carry the install recipe
      its mechanism needs — the per-entry cross-check the freelance
      validator already does for ``event_triggers[]``.

    Accepts either a gallery package dict (``{"build_spec": ..., "scheduled_actions": [...]}``)
    or an ``ApplicationManifest``-like object with those attributes. Tolerant
    of missing fields (treated as empty / no actions).

    Returns a dict shaped for direct embedding in
    ``gallery.preflight_check``'s ``requirements`` list::

        {
          "ok":              bool,  # coverage satisfied AND wiring clean
          "markers":         list[str],   # distinct daemon/cron markers found in build_spec
          "actions_total":   int,         # all scheduled_actions[] entries
          "actions_installable": int,     # subset with a forge-installable mechanism
          "errors":          list[str],   # per-entry wiring errors
          "severity":        "build_blocker" | "info",
          "message":         str,         # human-readable remediation
        }

    Failure mode is ``severity: "build_blocker"``; preflight surfaces this
    as a hard install blocker via the existing gate at
    ``gallery_routes.api_gallery_install``.
    """
    if isinstance(pkg_or_manifest, dict):
        build_spec = pkg_or_manifest.get("build_spec", "") or ""
        actions = pkg_or_manifest.get("scheduled_actions") or []
    else:
        build_spec = getattr(pkg_or_manifest, "build_spec", "") or ""
        actions = getattr(pkg_or_manifest, "scheduled_actions", None) or []

    markers = detect_unstructured_schedule(build_spec)
    actions_total = len(actions) if isinstance(actions, list) else 0
    actions_installable = _installable_action_count(actions)

    wiring_errors: list[str] = []
    if isinstance(actions, list):
        for idx, action in enumerate(actions):
            wiring_errors.extend(_validate_action_wiring(action, idx))

    if wiring_errors:
        return {
            "ok": False,
            "markers": markers,
            "actions_total": actions_total,
            "actions_installable": actions_installable,
            "errors": wiring_errors,
            "severity": "build_blocker",
            "message": (
                f"scheduled_actions[] wiring is broken across "
                f"{len(wiring_errors)} issue(s) — forge would fail to "
                f"materialize the affected action(s) and the app would "
                f"install with its schedule silently missing. "
                + " ".join(wiring_errors)
            ),
        }

    if not markers:
        return {
            "ok": True,
            "markers": [],
            "actions_total": actions_total,
            "actions_installable": actions_installable,
            "errors": [],
            "severity": "info",
            "message": "build_spec declares no scheduled side-effect.",
        }

    if actions_installable > 0:
        return {
            "ok": True,
            "markers": markers,
            "actions_total": actions_total,
            "actions_installable": actions_installable,
            "errors": [],
            "severity": "info",
            "message": (
                f"build_spec describes a daemon/cron and scheduled_actions[] "
                f"declares {actions_installable} forge-installable action(s)."
            ),
        }

    # ── Failure path: prose describes a daemon but nothing installable. ──
    shown = ", ".join(repr(m) for m in markers[:4])
    if len(markers) > 4:
        shown += f", +{len(markers) - 4} more"

    msg = (
        "build_spec describes a daemon/cron but scheduled_actions[] declares "
        "no forge-installable mechanism (oc_heartbeat_instruction, "
        "oc_session_instruction, launchd, launchd_python_signal, or crontab). "
        "Forge can only install daemons declared in the structured field — "
        "without one, the install will appear successful but no cron will be "
        "scheduled, and the app will silently fail to run. "
        f"Daemon markers found in build_spec: {shown}. "
        "Fix: add a scheduled_actions[] entry with the appropriate mechanism "
        "(see docs/spec-forge-side-effects-2026-06-02.md)."
    )
    if actions_total > 0 and actions_installable == 0:
        msg += (
            f" Note: scheduled_actions[] has {actions_total} entry/entries "
            f"but all use a non-installable mechanism (unknown / external / empty)."
        )

    return {
        "ok": False,
        "markers": markers,
        "actions_total": actions_total,
        "actions_installable": actions_installable,
        "errors": [],
        "severity": "build_blocker",
        "message": msg,
    }
