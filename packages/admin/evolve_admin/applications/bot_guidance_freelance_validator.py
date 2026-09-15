"""
bot_guidance_freelance_validator — gate on at-risk manifests opting into
structural enforcement vs. relying on policy alone.

The 2026-06-05 atlas-on-demand-research live test exposed the agent-freelance
bypass: when ``bot_guidance`` tells the LLM agent to invoke a bot-local script
and the invocation fails (OC exec preflight, missing file, anything), the
agent silently falls back to its general tools and answers freelance. PR
#2200 added an audit Signal producer; ``POD_CONDUCT.md`` rule 11 added a
pod-wide hard-stop instruction. Both are *policy*.

Phase 2 of the spec (internal/spec-agent-freelance-bypass-phase2-2026-06-06.md)
ships structural enforcement: a ``before_prompt_build`` plugin interceptor
that runs the script direct and stays the LLM quiet. To opt in, a manifest
sets ``invocation_mode: "plugin_intercept"`` and declares ``invocation`` on
each ``event_triggers[]`` entry.

This module is the install-time gate. It catches three mismatches:

1. **At-risk-shaped manifest with no structured triggers** — the manifest
   either (a) carries bot_guidance prose describing script invocation, OR
   (b) registers a bot-local script under ``scripts/`` / ``legacy-scripts/``
   in its ``evidence_files`` / ``files`` / ``realized_files`` — while
   ``event_triggers[]`` is empty. In ``agent_invokes`` mode the LLM runs the
   script via tool use; when the script fails, the agent freelances and a raw
   ``(agent) failed`` warning can leak into chat (team-bot-c's Task Management /
   ``scripts/tasks.py`` did exactly this, then confabulated "Task LA015
   created"). Severity: ``warning`` — loud enough that the Apps dashboard
   surfaces a "migrate to plugin_intercept" nudge, but **not**
   ``build_blocker``: existing installed apps (atlas-on-demand-research,
   atlas-article-capture, and other on-demand / script-backed task apps) run
   this way and must keep installing / re-validating.

   The file-reference signal (b) is what closes the scan-time gap: bot_guidance
   prose markers are narrow (imperative hard-stop phrasing only) AND — on
   v7-arc instances — bot_guidance is not overlaid during hydration, so the
   prose path alone is blind to most real script-backed apps. The file fields
   ride on the instance and survive hydration, so they catch the apps the
   prose path misses.

2. **plugin_intercept opt-in but invocation contract missing or broken** —
   ``invocation_mode == "plugin_intercept"`` but a trigger lacks
   ``invocation``, has an uncompilable ``pattern``, uses an unknown
   ``stdout_protocol``, or sets ``on_failure: "post_fallback"`` without
   ``fallback_text``. Severity: ``build_blocker`` — the plugin would
   either crash or freelance.

3. **invocation_mode value invalid** — value outside the schema enum.
   Severity: ``build_blocker``. This is also caught by the JSON-schema
   layer but the validator surfaces a friendlier error.

Wired at two checkpoints, matching ``scheduled_actions_validator``:

  * ``gallery.preflight_check`` — surfaces a result with
    ``severity: "build_blocker"`` so the install dispatch refuses with a
    clear remediation message.
  * ``forge_engine.run_forge_job`` Phase 5b — re-validates after
    spec reconciliation; raises ``RuntimeError`` if the rebuilt manifest
    regressed.

Reference: internal/spec-agent-freelance-bypass-phase2-2026-06-06.md.
"""

from __future__ import annotations

import re
from typing import Any


# ── Detection: prose markers that suggest a manifest is "at-risk-shaped" ────
#
# An at-risk-shaped manifest is one whose ``bot_guidance`` describes a
# response path that runs a bot-local script. These are exactly the
# manifests where the freelance bypass can occur if the script fails.
# Markers are chosen for very low false-positive rate — a passing mention
# of "script" in unrelated context must not trip the gate.
_AT_RISK_PROSE_MARKERS: tuple[str, ...] = (
    # PR #2192's explicit hard-stop language and POD_CONDUCT rule 11 phrasing.
    r"do\s+NOT\s+freelance",
    r"do\s+not\s+freelance",
    r"do\s+not\s+substitute",
    # Script invocation language — "Run exactly: python3 scripts/foo.py"
    # is the canonical shape.
    r"\brun\s+(?:exactly\s+)?[\"`]?python3\s+scripts/",
    r"\binvoke\s+(?:the\s+)?script\b",
    r"\bthe\s+script's\s+(?:reply|stdout|output)\b",
    # JSON-request-file invocation pattern (PR #2192).
    r"\brequest\s+file\b.*\bscript\b",
)


# ── Detection: the manifest references a bot-local script it invokes ─────────
#
# The prose markers above are high-precision but narrow: they only fire on
# imperative hard-stop phrasing ("do NOT freelance", "run exactly python3
# scripts/…"). The far more common at-risk shape — and the one that bit team-bot-c
# live (Task Management's ``scripts/tasks.py`` failed, the raw ``(agent)
# failed`` warning leaked, and the agent confabulated "Task LA015 created") —
# is an app that simply *registers a bot-local script* it invokes ad-hoc,
# documented in ordinary reference prose ("``scripts/tasks.py`` — Primary entry
# point; CLI tool …"). That trips none of the prose markers.
#
# Worse, on v7-arc instances ``bot_guidance`` lives on the bound Spec and is
# NOT overlaid during hydration (``manifest.hydrate_v7_arc_instance`` overlays
# event_triggers / success_criteria / identity / … but not bot_guidance or
# invocation_mode), so the prose path is structurally blind to every v7-arc app
# regardless of phrasing.
#
# This second signal closes both gaps by looking at the manifest's *file*
# fields — ``evidence_files`` / ``files`` / ``realized_files``, which DO ride on
# the instance and survive hydration — for a reference to a bot-local script
# under ``scripts/`` or ``legacy-scripts/``. Directory-anchored + extension-
# anchored to keep false positives near zero: a data file (``.md`` / ``.json``
# / ``.txt``) or a path outside those two script dirs never matches. An app
# that has migrated to ``plugin_intercept`` with valid triggers returns "info"
# before this signal is ever weighed, so a fixed app stops nagging.
# The negative lookbehind ``(?<![\w-])`` anchors ``scripts`` / ``legacy-scripts``
# to a path-component boundary: it matches at string start, after ``/``, after
# whitespace, or after a backtick (prose like "run ``scripts/tasks.py``"), but
# NOT inside a larger word (``myscripts/x.py`` / ``subscripts/x.py`` are not
# bot-local script dirs). ``\S+`` then stops at the first whitespace so trailing
# CLI args ("scripts/x.py --flag") and closing punctuation are excluded.
_BOT_LOCAL_SCRIPT_RE = re.compile(
    r"(?<![\w-])(?:scripts|legacy-scripts)/\S+\.(?:py|sh)\b",
    re.IGNORECASE,
)


# Stdout protocols registered in plugin code (triggerProtocols.ts). The
# schema enum mirrors this; the validator keeps a local copy so error
# messages can suggest the right value without importing the plugin.
#
#   atlas_research / atlas_capture — line-prefixed marker grammars, bound to
#       the atlas scripts' stdout (RESEARCH_* / CAPTURE_* markers).
#   raw_text — generic protocol for forge-generated script-backed apps: the
#       script prints its reply to stdout and exits 0 (errors → stderr +
#       non-zero exit). Forge stamps this onto newly built apps it upgrades to
#       invocation_mode='plugin_intercept' so a generic script engages Layer C
#       without inventing a bespoke marker grammar (forge_engine
#       ._normalize_freelance_invocation).
_KNOWN_STDOUT_PROTOCOLS: frozenset[str] = frozenset({
    "atlas_research",
    "atlas_capture",
    "raw_text",
})


def _is_at_risk_shaped(bot_guidance: Any) -> tuple[bool, list[str]]:
    """Return (is_at_risk, matched_markers) for the manifest's bot_guidance prose.

    Tolerant of None / non-list / non-dict entries. Walks every
    ``content`` string and collects markers found in any of them.
    """
    if not isinstance(bot_guidance, list):
        return False, []
    matched: list[str] = []
    for section in bot_guidance:
        if not isinstance(section, dict):
            continue
        content = section.get("content")
        if not isinstance(content, str):
            continue
        for marker in _AT_RISK_PROSE_MARKERS:
            if re.search(marker, content, re.IGNORECASE):
                # De-dup at the marker level; same marker hitting twice in
                # one content string doesn't add value.
                if marker not in matched:
                    matched.append(marker)
    return bool(matched), matched


def _iter_file_reference_strings(pkg_or_manifest: Any):
    """Yield candidate path/prose strings from a manifest's file-bearing fields.

    Walks ``evidence_files`` (list of strings or ``{path|file|logical_name}``
    dicts; also the legacy ``evidence={"files": [...]}`` shape), ``files`` and
    ``realized_files`` (lists of ``{path}`` dicts on v7-arc), plus
    ``bot_guidance`` content strings (so non-v7 manifests whose script
    reference only appears in prose are still covered). Tolerant of
    missing / wrong-typed fields — yields nothing rather than raising.
    """
    def field(key: str) -> Any:
        if isinstance(pkg_or_manifest, dict):
            return pkg_or_manifest.get(key)
        return getattr(pkg_or_manifest, key, None)

    evidence_files = field("evidence_files")
    if evidence_files is None:
        evidence = field("evidence")
        if isinstance(evidence, dict):
            evidence_files = evidence.get("files")

    for src in (evidence_files, field("files"), field("realized_files")):
        if not isinstance(src, list):
            continue
        for item in src:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                p = item.get("path") or item.get("file") or item.get("logical_name")
                if isinstance(p, str):
                    yield p

    bot_guidance = field("bot_guidance")
    if isinstance(bot_guidance, list):
        for section in bot_guidance:
            if isinstance(section, dict) and isinstance(section.get("content"), str):
                yield section["content"]


def _references_bot_local_script(pkg_or_manifest: Any) -> tuple[bool, list[str]]:
    """Return (references_script, script_paths) for the manifest's file fields.

    A True result means the app registers / documents a bot-local script
    under ``scripts/`` or ``legacy-scripts/`` that the agent invokes — the
    at-risk shape the freelance bypass exploits. ``script_paths`` is the
    de-duplicated list of matched references (leading slashes stripped) for
    display in the migrate nudge.
    """
    found: list[str] = []
    for s in _iter_file_reference_strings(pkg_or_manifest):
        for m in _BOT_LOCAL_SCRIPT_RE.finditer(s):
            path = m.group(0).lstrip("/")
            if path not in found:
                found.append(path)
    return bool(found), found


def _build_freelance_warning_message(
    prose_markers: list[str], script_paths: list[str]
) -> str:
    """Compose the agent_invokes warning message.

    When a concrete script file is named (the common, file-reference path),
    lead with the script so the operator knows exactly what to migrate;
    otherwise fall back to the prose-marker wording (preserved verbatim from
    the original Phase 2.1 message).
    """
    if script_paths:
        shown = ", ".join(script_paths[:3])
        more = "" if len(script_paths) <= 3 else f" (+{len(script_paths) - 3} more)"
        return (
            f"This app is backed by a bot-local script ({shown}{more}) that the "
            f"agent invokes in agent_invokes mode, with no event_triggers[] "
            f"declared. If the script fails, the agent can freelance and a raw "
            f"'(agent) failed' warning may leak into chat — or the agent "
            f"confabulates success. Set invocation_mode to 'plugin_intercept' "
            f"(with event_triggers[]) so the plugin runs the script directly "
            f"and keeps the LLM quiet — the structural fix. Adding structured "
            f"triggers also lets the agent_bypass_audit daemon detect freelance "
            f"bypasses for this manifest."
        )
    return (
        f"bot_guidance describes script-invocation behavior "
        f"({len(prose_markers)} marker(s) matched) in agent_invokes mode, "
        f"but no event_triggers[] are declared. If the script fails, the "
        f"agent freelances and a raw '(agent) failed' warning can leak into "
        f"chat. Set invocation_mode to 'plugin_intercept' (with "
        f"event_triggers[]) so the plugin runs the script directly and keeps "
        f"the LLM quiet — the structural fix. Adding structured triggers also "
        f"lets the agent_bypass_audit daemon detect freelance bypasses for "
        f"this manifest."
    )


def _validate_trigger_invocation(trigger: dict, idx: int) -> list[str]:
    """Return error messages for one event_triggers[] entry's invocation contract.

    Caller decides severity based on the manifest's invocation_mode.
    Empty list = invocation contract is well-formed (or absent when allowed).
    """
    errors: list[str] = []
    trigger_id = trigger.get("id") or f"#{idx}"

    # Pattern compile — checked for ANY trigger that has match.pattern, since
    # an uncompilable pattern would crash both audit and Layer C.
    match = trigger.get("match") or {}
    pattern = match.get("pattern")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(
                f"event_triggers[{trigger_id}].match.pattern is not a valid "
                f"Python regex: {exc}"
            )
    exclude_pattern = match.get("exclude_pattern")
    if exclude_pattern:
        try:
            re.compile(exclude_pattern)
        except re.error as exc:
            errors.append(
                f"event_triggers[{trigger_id}].match.exclude_pattern is not a "
                f"valid Python regex: {exc}"
            )

    invocation = trigger.get("invocation")
    if invocation is None:
        # Caller checks whether invocation is required (plugin_intercept mode).
        return errors

    if not isinstance(invocation, dict):
        errors.append(
            f"event_triggers[{trigger_id}].invocation must be an object, "
            f"got {type(invocation).__name__}"
        )
        return errors

    # stdout_protocol must be a known parser. Unknown protocols would land
    # the plugin in a fallback / freelance path at runtime — exactly the
    # outcome this spec is trying to prevent.
    stdout_protocol = invocation.get("stdout_protocol")
    if stdout_protocol and stdout_protocol not in _KNOWN_STDOUT_PROTOCOLS:
        known = ", ".join(sorted(_KNOWN_STDOUT_PROTOCOLS))
        errors.append(
            f"event_triggers[{trigger_id}].invocation.stdout_protocol "
            f"'{stdout_protocol}' is not registered in plugin code. "
            f"Known protocols: {known}. Adding a new protocol requires "
            f"updating packages/plugin/src/observer/triggerProtocols.ts."
        )

    # on_failure: "post_fallback" requires fallback_text. JSON-schema can
    # express this via conditional but the friendlier error is here.
    on_failure = invocation.get("on_failure")
    fallback_text = invocation.get("fallback_text")
    if on_failure == "post_fallback" and not fallback_text:
        errors.append(
            f"event_triggers[{trigger_id}].invocation.on_failure is "
            f"'post_fallback' but fallback_text is missing or empty. "
            f"The plugin must have something verbatim to post when the "
            f"script fails — there is no LLM in the loop."
        )

    # invokes ↔ script consistency. Both fields are present in Phase 2
    # (invocation.script is the path the plugin uses; invokes is the
    # blueprint logical_name for the legacy event-handling pipeline).
    # If both are set, basename should match — otherwise the operator
    # is referring to two different scripts and one is wrong.
    invokes = trigger.get("invokes")
    script = invocation.get("script")
    if invokes and script:
        # Walk the blueprint to find the file with logical_name == invokes;
        # caller supplies the blueprint dict if it wants this check. Since
        # this function only sees the trigger, we do a basename-style
        # heuristic: the script path's last component should reference the
        # invokes logical_name (which is typically the basename without
        # extension).
        script_basename = script.rsplit("/", 1)[-1]
        # Strip extension for comparison: "atlas_research.py" → "atlas_research"
        script_stem = script_basename.rsplit(".", 1)[0]
        if invokes != script_stem and invokes not in script_basename:
            errors.append(
                f"event_triggers[{trigger_id}].invokes is '{invokes}' but "
                f"invocation.script is '{script}' — their identifiers don't "
                f"appear related. If invokes references a blueprint file, its "
                f"logical_name should match the script basename without "
                f"extension. Either fix the mismatch or align both fields."
            )

    return errors


def validate_bot_guidance(pkg_or_manifest: Any) -> dict:
    """Validate the freelance-bypass posture of a gallery package or manifest.

    Accepts either a gallery package dict (``{"bot_guidance": ...,
    "event_triggers": ..., "invocation_mode": ...}``) or an
    ApplicationManifest-like object with those attributes. Tolerant of
    missing fields.

    Returns a dict shaped for direct embedding in ``gallery.preflight_check``'s
    ``requirements`` list::

        {
          "ok":              bool,
          "invocation_mode": str,           # resolved value (default 'agent_invokes')
          "is_at_risk":      bool,          # prose suggests OR file fields register a bot-local script
          "markers":         list[str],     # prose markers + synthetic "script-file:<path>" entries
          "trigger_count":   int,           # event_triggers[] length
          "errors":          list[str],     # per-trigger contract errors
          "severity":        "build_blocker" | "warning" | "info",
          "message":         str,           # human-readable remediation
        }

    The gate refuses the install (severity ``build_blocker``) only when
    ``invocation_mode == "plugin_intercept"`` and the invocation contract is
    broken — that's the case where install would silently produce a
    crash-loop or fallback-to-freelance loop at runtime.

    An at-risk-shaped manifest with ``invocation_mode == "agent_invokes"``
    (default) returns ``severity: "warning"`` (``ok: True``) — the operator
    is choosing policy-only enforcement, which is allowed but carries the
    freelance-leak risk, so the Apps dashboard nudges a migration to
    ``plugin_intercept``. It is never ``build_blocker``: the existing
    agent_invokes apps must keep installing / re-validating.
    """
    if isinstance(pkg_or_manifest, dict):
        bot_guidance = pkg_or_manifest.get("bot_guidance")
        event_triggers = pkg_or_manifest.get("event_triggers") or []
        invocation_mode = pkg_or_manifest.get("invocation_mode") or "agent_invokes"
    else:
        bot_guidance = getattr(pkg_or_manifest, "bot_guidance", None)
        event_triggers = getattr(pkg_or_manifest, "event_triggers", None) or []
        invocation_mode = getattr(pkg_or_manifest, "invocation_mode", None) or "agent_invokes"

    # At-risk = EITHER imperative prose markers (narrow, high-precision) OR a
    # registered bot-local script reference (broad, catches the documentation-
    # prose / v7-arc-hydration case that bit team-bot-c live). The synthetic
    # ``script-file:<path>`` markers keep ``markers`` informative for callers.
    prose_at_risk, prose_markers = _is_at_risk_shaped(bot_guidance)
    script_backed, script_paths = _references_bot_local_script(pkg_or_manifest)
    is_at_risk = prose_at_risk or script_backed
    markers = list(prose_markers) + [f"script-file:{p}" for p in script_paths]
    trigger_count = len(event_triggers) if isinstance(event_triggers, list) else 0

    # ── invocation_mode value gate ───────────────────────────────────────────
    if invocation_mode not in ("agent_invokes", "plugin_intercept", "subagent"):
        return {
            "ok": False,
            "invocation_mode": invocation_mode,
            "is_at_risk": is_at_risk,
            "markers": markers,
            "trigger_count": trigger_count,
            "errors": [
                f"invocation_mode '{invocation_mode}' is not one of "
                f"'agent_invokes', 'plugin_intercept', or 'subagent' (reserved)."
            ],
            "severity": "build_blocker",
            "message": (
                f"Manifest's invocation_mode must be 'agent_invokes' (default — "
                f"LLM agent invokes the script via tool use), 'plugin_intercept' "
                f"(plugin runs the script in before_prompt_build, LLM stays "
                f"quiet), or 'subagent' (reserved for deferred Layer B). "
                f"Got '{invocation_mode}'."
            ),
        }

    if invocation_mode == "subagent":
        return {
            "ok": False,
            "invocation_mode": invocation_mode,
            "is_at_risk": is_at_risk,
            "markers": markers,
            "trigger_count": trigger_count,
            "errors": [
                "invocation_mode 'subagent' is reserved but not yet implemented."
            ],
            "severity": "build_blocker",
            "message": (
                "Layer B of the agent-freelance-bypass spec is deferred. "
                "Use 'agent_invokes' (default) or 'plugin_intercept' until "
                "Layer B ships."
            ),
        }

    # ── plugin_intercept opt-in: invocation contract is mandatory ────────────
    if invocation_mode == "plugin_intercept":
        if not isinstance(event_triggers, list) or trigger_count == 0:
            return {
                "ok": False,
                "invocation_mode": invocation_mode,
                "is_at_risk": is_at_risk,
                "markers": markers,
                "trigger_count": trigger_count,
                "errors": ["event_triggers[] is empty or absent."],
                "severity": "build_blocker",
                "message": (
                    "invocation_mode 'plugin_intercept' requires "
                    "event_triggers[] to be present and non-empty. Without "
                    "triggers, the plugin has nothing to match against and "
                    "every incoming message would fall through to the LLM — "
                    "the freelance path this mode is meant to close."
                ),
            }
        errors: list[str] = []
        triggers_without_invocation: list[str] = []
        for idx, trigger in enumerate(event_triggers):
            if not isinstance(trigger, dict):
                errors.append(
                    f"event_triggers[#{idx}] is not an object "
                    f"(got {type(trigger).__name__})."
                )
                continue
            if trigger.get("invocation") is None:
                triggers_without_invocation.append(trigger.get("id") or f"#{idx}")
            errors.extend(_validate_trigger_invocation(trigger, idx))

        if triggers_without_invocation:
            errors.append(
                "invocation_mode 'plugin_intercept' requires every "
                "event_triggers[] entry to declare 'invocation'. Missing on: "
                + ", ".join(triggers_without_invocation)
            )

        if errors:
            return {
                "ok": False,
                "invocation_mode": invocation_mode,
                "is_at_risk": is_at_risk,
                "markers": markers,
                "trigger_count": trigger_count,
                "errors": errors,
                "severity": "build_blocker",
                "message": (
                    f"invocation_mode 'plugin_intercept' but the invocation "
                    f"contract is broken across {len(errors)} issue(s). "
                    f"Fix the items below before the install can proceed."
                ),
            }

        return {
            "ok": True,
            "invocation_mode": invocation_mode,
            "is_at_risk": is_at_risk,
            "markers": markers,
            "trigger_count": trigger_count,
            "errors": [],
            "severity": "info",
            "message": (
                f"invocation_mode 'plugin_intercept' with "
                f"{trigger_count} valid trigger(s); Layer C enforcement "
                f"will be active for this manifest."
            ),
        }

    # ── agent_invokes (default): warn when at-risk-shaped ────────────────────
    # ``warning`` (not ``build_blocker``) — install is NOT refused; the
    # existing agent_invokes apps keep working. But the freelance-leak risk
    # is real, so the Apps dashboard reads this severity and surfaces a
    # "migrate to plugin_intercept" nudge for the operator. At-risk now fires
    # on a registered bot-local script reference too (script_paths), not just
    # imperative prose — that is the case that left team-bot-c's Task Management
    # (scripts/tasks.py) unwarned at scan time while only sf-b3 caught it at
    # runtime. The ``trigger_count == 0`` guard is unchanged: an app that has
    # declared structured triggers is at least partially structured and the
    # audit can already detect its bypasses, so it is not nudged here.
    if is_at_risk and trigger_count == 0:
        return {
            "ok": True,
            "invocation_mode": invocation_mode,
            "is_at_risk": is_at_risk,
            "markers": markers,
            "trigger_count": trigger_count,
            "errors": [],
            "severity": "warning",
            "message": _build_freelance_warning_message(prose_markers, script_paths),
        }

    return {
        "ok": True,
        "invocation_mode": invocation_mode,
        "is_at_risk": is_at_risk,
        "markers": markers,
        "trigger_count": trigger_count,
        "errors": [],
        "severity": "info",
        "message": "bot_guidance posture is clean.",
    }
