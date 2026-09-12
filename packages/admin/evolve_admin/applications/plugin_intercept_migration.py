"""
plugin_intercept_migration — turn an installed ``agent_invokes`` manifest into a
``plugin_intercept`` one by synthesizing the per-trigger ``invocation`` contract.

Background. When a script-backed application runs in ``invocation_mode:
"agent_invokes"`` (the default), the bot's LLM freelances the CLI call to the
script. If the script fails, the agent either leaks a raw ``(agent) failed``
line into chat or — worse — confabulates a fake success (the ``tasks.py``
"Task LA015 created" incident). ``invocation_mode: "plugin_intercept"`` takes
the LLM out of that failure path: the OpenClaw plugin runs the script
deterministically in ``before_prompt_build`` and posts the script's real
output (or a verbatim fallback) with no model in the loop.

The forge already defaults *new* apps to the safer mode. This module is the
one-button path for an *existing* installed manifest, invoked by the
``Make reliable`` action on the Apps page (route in
``web/routes_applications_reliability.py``).

Design — conservative by construction. A privileged, behaviour-changing
migration must never produce a manifest that fails worse than the one it
replaced. So this migrator:

  * **Refuses rather than guesses.** A trigger is migrated only when a *valid*
    ``invocation`` contract can be synthesized for it — a resolvable script
    path and a ``stdout_protocol`` the plugin actually parses
    (``_KNOWN_STDOUT_PROTOCOLS``). If any trigger can't be made ready, the
    whole migration is refused with a per-trigger reason. No half-migration:
    a manifest with ``plugin_intercept`` + one un-contracted trigger is a
    build-blocker (see ``bot_guidance_freelance_validator``), and a partial
    write would brick the app.
  * **Re-validates before returning.** The synthesized manifest is run back
    through ``validate_bot_guidance``; a non-``ok`` verdict aborts the
    migration. The caller persists only an ``ok`` result.
  * **Is idempotent.** Re-running on an already-``plugin_intercept`` manifest
    is a no-op (``changed=False``).
  * **Is filesystem-free.** It operates on the manifest dict only. The route
    layer owns the privileged checks that need disk (does the resolved script
    exist under the bot's workspace?) and the sanctioned manifest write.

Spec: internal/spec-agent-freelance-bypass-phase2-2026-06-06.md.
"""

from __future__ import annotations

from typing import Any

from .bot_guidance_freelance_validator import (
    _KNOWN_STDOUT_PROTOCOLS,
    validate_bot_guidance,
)

# v7-arc instances keep ``event_triggers[]`` on the bound *Spec* (shared
# gallery state), not on the per-bot instance manifest — see
# ``manifest.hydrate_v7_arc_instance``. This migrator only ever rewrites the
# per-bot manifest, so a v7-arc app's triggers are out of its reach; flipping
# the mode there would edit shared gallery Specs used by other instances/pods.
# We detect the shape and refuse with a Spec-specific message instead.
_MANIFEST_SHAPE_V7_ARC = "v7-arc"

# Default verbatim reply the plugin posts when a migrated script fails. Honest
# and deterministic — the whole point of plugin_intercept is that there is no
# LLM to reword (or invent) a failure message. Preserved if the trigger
# already declares one.
_DEFAULT_FALLBACK_TEXT = (
    "Sorry — I couldn't complete that just now. Please try again in a few minutes."
)


def _script_for_trigger(trigger: dict, manifest: dict) -> str | None:
    """Resolve the script path for one trigger, or None if unresolvable.

    Preference order:
      1. an ``invocation.script`` already present on the trigger (partial /
         re-run state) — trust the existing contract;
      2. the blueprint file whose ``logical_name`` matches ``invokes`` →
         its ``expected_location``;
      3. ``scripts/<invokes>.py`` as a last-resort convention, only when the
         trigger declares ``invokes``.
    """
    existing = trigger.get("invocation")
    if isinstance(existing, dict):
        script = existing.get("script")
        if isinstance(script, str) and script.strip():
            return script.strip()

    invokes = trigger.get("invokes")
    if not isinstance(invokes, str) or not invokes.strip():
        return None
    invokes = invokes.strip()

    blueprint = manifest.get("blueprint") or {}
    files = blueprint.get("files") if isinstance(blueprint, dict) else None
    if isinstance(files, list):
        for f in files:
            if not isinstance(f, dict):
                continue
            if f.get("logical_name") == invokes:
                loc = f.get("expected_location") or f.get("path")
                if isinstance(loc, str) and loc.strip():
                    return loc.strip()

    # Convention fallback: matches the validator's invokes↔script basename
    # consistency check (stem == invokes).
    return f"scripts/{invokes}.py"


def _protocol_for_trigger(trigger: dict) -> str | None:
    """Resolve a *known* ``stdout_protocol`` for one trigger, or None.

    plugin_intercept needs the plugin to know how to parse the script's
    stdout. Only protocols registered in plugin code
    (``_KNOWN_STDOUT_PROTOCOLS``) parse — anything else lands the plugin in
    its fallback/freelance path, exactly the outcome this is meant to close.

    Preference order:
      1. an ``invocation.stdout_protocol`` already declared and known;
      2. ``invokes`` when it names a known protocol (the atlas apps follow
         this convention: ``invokes: "atlas_research"`` ↔
         ``stdout_protocol: "atlas_research"``).
    """
    existing = trigger.get("invocation")
    if isinstance(existing, dict):
        proto = existing.get("stdout_protocol")
        if isinstance(proto, str) and proto in _KNOWN_STDOUT_PROTOCOLS:
            return proto

    invokes = trigger.get("invokes")
    if isinstance(invokes, str) and invokes.strip() in _KNOWN_STDOUT_PROTOCOLS:
        return invokes.strip()

    return None


def _synthesize_invocation(trigger: dict, manifest: dict) -> tuple[dict | None, str]:
    """Build a valid ``invocation`` object for one trigger.

    Returns ``(invocation, "")`` on success, or ``(None, reason)`` when the
    trigger cannot be made plugin_intercept-ready. ``reason`` is
    operator-facing.
    """
    script = _script_for_trigger(trigger, manifest)
    if not script:
        return None, (
            "no script could be resolved — the trigger declares no 'invokes' "
            "blueprint file and carries no existing invocation.script"
        )

    protocol = _protocol_for_trigger(trigger)
    if not protocol:
        known = ", ".join(sorted(_KNOWN_STDOUT_PROTOCOLS))
        return None, (
            f"the script's output format ('{script}') isn't one the plugin "
            f"knows how to read. Deterministic interception only supports "
            f"scripts that speak a registered protocol ({known}); making this "
            f"app reliable needs an engineer to register its output protocol "
            f"in the plugin first."
        )

    _raw = trigger.get("invocation")
    existing: dict = _raw if isinstance(_raw, dict) else {}

    invocation: dict[str, Any] = {
        "script": script,
        "stdout_protocol": protocol,
    }

    # on_failure: preserve an explicit, valid choice; otherwise default to
    # post_fallback with a verbatim honest message (better than silent —
    # the member gets *something* truthful instead of nothing).
    on_failure = existing.get("on_failure")
    if on_failure not in ("post_fallback", "silent"):
        on_failure = "post_fallback"
    invocation["on_failure"] = on_failure

    if on_failure == "post_fallback":
        fallback = existing.get("fallback_text")
        if not (isinstance(fallback, str) and fallback.strip()):
            fallback = _DEFAULT_FALLBACK_TEXT
        invocation["fallback_text"] = fallback

    # Carry through the optional request-passing fields verbatim — they're how
    # the plugin hands the message to the script, and we must not invent them.
    for opt_key in ("request_file_template", "request_payload"):
        if opt_key in existing and existing[opt_key] not in (None, ""):
            invocation[opt_key] = existing[opt_key]

    return invocation, ""


def migrate_to_plugin_intercept(manifest: dict) -> dict:
    """Produce a ``plugin_intercept`` version of ``manifest`` if one is valid.

    Returns a dict::

        {
          "ok":               bool,        # a valid manifest was produced / already valid
          "changed":          bool,        # invocation_mode / invocations actually changed
          "manifest":         dict | None, # the migrated manifest (None when ok is False)
          "reason":           str,         # operator-facing explanation
          "migrated_triggers": list[str],  # trigger ids that gained a contract
          "blocked_triggers":  list[dict], # [{"id": str, "reason": str}, ...]
        }

    The input is never mutated; a deep-ish copy of ``event_triggers`` is made
    before synthesis. The caller persists only when ``ok`` is True and
    ``changed`` is True.
    """
    import copy

    if not isinstance(manifest, dict):
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": "manifest is not an object.",
            "migrated_triggers": [], "blocked_triggers": [],
        }

    mode = manifest.get("invocation_mode") or "agent_invokes"

    # ── Idempotent no-op: already in reliable mode ───────────────────────────
    if mode == "plugin_intercept":
        verdict = validate_bot_guidance(manifest)
        if verdict.get("ok"):
            return {
                "ok": True, "changed": False, "manifest": manifest,
                "reason": "This app is already in reliable mode.",
                "migrated_triggers": [], "blocked_triggers": [],
            }
        # Declared plugin_intercept but the contract is broken — that's a
        # pre-existing build-blocker we won't paper over here.
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                "This app already declares reliable mode but its invocation "
                "contract is invalid: " + verdict.get("message", "")
            ),
            "migrated_triggers": [], "blocked_triggers": [],
        }

    if mode == "subagent":
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                "This app uses the reserved 'subagent' mode, which can't be "
                "converted to reliable mode automatically."
            ),
            "migrated_triggers": [], "blocked_triggers": [],
        }

    # ── v7-arc: triggers live on the shared Spec, out of a per-bot rewrite's
    #    reach. Refuse rather than silently edit shared gallery state. ────────
    if manifest.get("manifest_shape") == _MANIFEST_SHAPE_V7_ARC:
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                "This app's message triggers are defined in its shared Spec, "
                "not on this bot. Reliable mode has to be set on the Spec in "
                "the gallery so every bot using it stays consistent — it can't "
                "be flipped for one bot from here."
            ),
            "migrated_triggers": [], "blocked_triggers": [],
        }

    triggers = manifest.get("event_triggers")
    if not isinstance(triggers, list) or not triggers:
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                "This app has no structured message triggers to make reliable. "
                "Reliable mode needs declared triggers so the plugin knows what "
                "to run; this app relies on the agent's own judgement instead."
            ),
            "migrated_triggers": [], "blocked_triggers": [],
        }

    new_triggers = copy.deepcopy(triggers)
    migrated: list[str] = []
    blocked: list[dict] = []

    for idx, trigger in enumerate(new_triggers):
        tid = (trigger.get("id") if isinstance(trigger, dict) else None) or f"#{idx}"
        if not isinstance(trigger, dict):
            blocked.append({"id": tid, "reason": "trigger is not an object."})
            continue
        invocation, reason = _synthesize_invocation(trigger, manifest)
        if invocation is None:
            blocked.append({"id": tid, "reason": reason})
            continue
        trigger["invocation"] = invocation
        migrated.append(tid)

    if blocked:
        detail = "; ".join(f"{b['id']}: {b['reason']}" for b in blocked)
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                f"{len(blocked)} of {len(new_triggers)} trigger(s) can't be "
                f"made reliable, so the change was not applied — "
                f"{detail}"
            ),
            "migrated_triggers": migrated, "blocked_triggers": blocked,
        }

    candidate = dict(manifest)
    candidate["event_triggers"] = new_triggers
    candidate["invocation_mode"] = "plugin_intercept"

    # ── Re-validate the synthesized manifest through the same gate the forge
    #    and install dispatch use. We persist only what this passes. ──────────
    verdict = validate_bot_guidance(candidate)
    if not verdict.get("ok"):
        return {
            "ok": False, "changed": False, "manifest": None,
            "reason": (
                "The reliable-mode manifest didn't pass validation: "
                + verdict.get("message", "")
            ),
            "migrated_triggers": migrated, "blocked_triggers": blocked,
        }

    return {
        "ok": True, "changed": True, "manifest": candidate,
        "reason": (
            f"Switched to reliable mode and wired {len(migrated)} "
            f"trigger(s) to run deterministically."
        ),
        "migrated_triggers": migrated, "blocked_triggers": [],
    }
