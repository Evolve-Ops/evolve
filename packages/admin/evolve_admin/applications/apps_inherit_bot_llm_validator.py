"""
apps_inherit_bot_llm_validator — gate manifests on the apps-inherit-bot-llm
principle.

The 2026-06-06 atlas compliance_scan alert ("Anthropic API key found in
workspace file: atlas/llm-config.json") surfaced a class regression: all
four Atlas apps had been forged with per-app Anthropic credentials and
called ``api.anthropic.com`` directly, escaping every per-bot LLM safeguard
Evolve provides (tier-walk, ``daily_cap_usd`` auto-trip, ``cost_watchdog``,
provider-agnostic routing, prompt caching).

[docs/principle-apps-inherit-bot-llm.md](../../../docs/principle-apps-inherit-bot-llm.md)
locks in the rule: Forge-installed apps MUST NOT carry their own LLM
credentials or call provider APIs directly. The manifest's
``recursive_llm`` block declares *intent*; the bot's gateway handles
*transport* (``bot_tool`` / ``subagent`` / ``openclaw_headless``).

This module is the install-time gate. It catches three violations:

1. **``recursive_llm.api_key_source`` is set.** The manifest declares a
   per-app credential pointer. This is the canonical anti-pattern that
   produced the Atlas regression. Severity: ``build_blocker``.

2. **``recursive_llm.purposes`` is non-empty but no ``transport`` is
   declared.** The app uses LLM but doesn't say how the call routes,
   which means the forge has no contract to enforce against.
   Severity: ``build_blocker``.

3. **``recursive_llm.transport`` value is not in the allowed set
   (``bot_tool`` / ``subagent`` / ``openclaw_headless``).** Wrong value,
   either typo or attempt to declare an unsupported transport.
   Severity: ``build_blocker``.

4. **``files[]`` declares a per-app credential template** (a
   ``layer: "data"`` entry whose path looks like
   ``<app>/llm-config.json`` or otherwise matches the
   ``api_key_source`` shape). Even if ``api_key_source`` itself is
   gone, the template file would carry the credential. Severity:
   ``build_blocker``.

Provenance-aware behaviour: this validator is called from the writer
gates (forge build, manifest editor, evo-handler) but NOT from the
scanner re-discovery path. Atlas's existing pre-rearchitect manifests
still violate the principle but they can be read; the rearchitect work
re-saves them via the forge once the new shape is ready.

Wired at three checkpoints, matching the established validator pattern:

  * ``gallery.preflight_check`` — surfaces a result with
    ``severity: "build_blocker"`` so the install dispatch refuses with a
    clear remediation message.
  * ``forge_engine.run_forge_job`` Phase 5d — re-validates after spec
    reconciliation; raises ``RuntimeError`` if the rebuilt manifest
    regressed.
  * ``manifest.save_manifest_with_provenance`` — refuses writes from
    ``forge_built`` / ``user_authored`` / ``bot_authored`` sources that
    carry a violation. ``observational`` (scanner re-discovery) is
    allowed through so existing pre-rearchitect manifests can be
    re-stamped.

Reference: docs/spec-apps-inherit-bot-llm-2026-06-06.md.
"""

from __future__ import annotations

from typing import Any


# Allowed transport values per docs/principle-apps-inherit-bot-llm.md §"The
# right pattern — three transports". Kept here as the source of truth for
# the validator; the manifest authoring guide §8 must stay in sync.
_ALLOWED_TRANSPORTS: frozenset[str] = frozenset({
    "bot_tool",
    "subagent",
    "openclaw_headless",
})


# Substrings in a ``layer: "data"`` file path that suggest a per-app
# credential template. The presence of such a file means the operator
# would be asked to paste an API key in there at install time — the
# same shape as the Atlas regression. We match on the basename without
# requiring an exact filename so renames like ``classifier-config.json``
# or ``synth-creds.json`` are caught too.
_CREDENTIAL_TEMPLATE_HINTS: tuple[str, ...] = (
    "llm-config",
    "api-key",
    "anthropic-key",
    "openai-key",
)


def _resolve_recursive_llm(pkg_or_manifest: Any) -> dict:
    """Extract the ``recursive_llm`` block, tolerating dict or manifest-like input."""
    if isinstance(pkg_or_manifest, dict):
        block = pkg_or_manifest.get("recursive_llm")
    else:
        block = getattr(pkg_or_manifest, "recursive_llm", None)
        if block is None:
            raw = getattr(pkg_or_manifest, "raw", None)
            if isinstance(raw, dict):
                block = raw.get("recursive_llm")
    return block or {}


def _resolve_files(pkg_or_manifest: Any) -> list:
    """Extract ``files[]`` from the manifest, tolerating dict or manifest-like input."""
    if isinstance(pkg_or_manifest, dict):
        files = pkg_or_manifest.get("files")
    else:
        files = getattr(pkg_or_manifest, "files", None)
        if files is None:
            raw = getattr(pkg_or_manifest, "raw", None)
            if isinstance(raw, dict):
                files = raw.get("files")
    return files if isinstance(files, list) else []


def _credential_template_violations(files: list) -> list[str]:
    """Return per-file violation messages for any ``layer: "data"`` entry
    whose path matches a credential-template shape."""
    hits: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        if entry.get("layer") != "data":
            continue
        path = (entry.get("path") or "").lower()
        if not path:
            continue
        for hint in _CREDENTIAL_TEMPLATE_HINTS:
            if hint in path:
                hits.append(
                    f"files[].path {entry.get('path')!r} looks like a per-app "
                    f"credential template (matches '{hint}'). Apps inherit the "
                    f"bot's LLM stack; per-app credential files are not allowed."
                )
                break
    return hits


def validate_apps_inherit_bot_llm(pkg_or_manifest: Any) -> dict:
    """Validate the apps-inherit-bot-llm posture of a gallery package or manifest.

    Accepts either a gallery package dict or an ApplicationManifest-like
    object. Tolerant of missing fields — a manifest with no
    ``recursive_llm`` block is valid (the app doesn't use LLM at all).

    Returns a dict shaped for direct embedding in
    ``gallery.preflight_check``'s ``requirements`` list, matching
    ``validate_scheduled_actions`` and ``validate_bot_guidance``::

        {
          "ok":          bool,
          "errors":      list[str],   # specific violation messages
          "severity":    "build_blocker" | "info",
          "message":     str,         # human-readable remediation
        }

    A clean manifest returns ``ok: True``, no errors, no severity escalation.
    """
    recursive_llm = _resolve_recursive_llm(pkg_or_manifest)
    files = _resolve_files(pkg_or_manifest)

    errors: list[str] = []

    api_key_source = recursive_llm.get("api_key_source") if isinstance(recursive_llm, dict) else None
    if api_key_source:
        errors.append(
            f"recursive_llm.api_key_source is declared "
            f"({api_key_source!r}). Apps must not credential themselves; "
            f"LLM work routes through the bot's gateway via the "
            f"`transport` field (bot_tool / subagent / openclaw_headless)."
        )

    purposes = recursive_llm.get("purposes") if isinstance(recursive_llm, dict) else None
    transport = recursive_llm.get("transport") if isinstance(recursive_llm, dict) else None

    if isinstance(purposes, list) and purposes:
        if not transport:
            errors.append(
                "recursive_llm.purposes is declared but recursive_llm.transport "
                "is missing. Declare one of: 'bot_tool', 'subagent', "
                "'openclaw_headless'."
            )
        elif transport not in _ALLOWED_TRANSPORTS:
            errors.append(
                f"recursive_llm.transport {transport!r} is not one of "
                f"{sorted(_ALLOWED_TRANSPORTS)}. See docs/principle-apps-inherit-bot-llm.md "
                f"for the three supported transports."
            )

    errors.extend(_credential_template_violations(files))

    if not errors:
        return {
            "ok": True,
            "errors": [],
            "severity": "info",
            "message": "apps-inherit-bot-llm posture is clean.",
        }

    return {
        "ok": False,
        "errors": errors,
        "severity": "build_blocker",
        "message": (
            "Manifest violates the apps-inherit-bot-llm principle. "
            "See docs/principle-apps-inherit-bot-llm.md and "
            "docs/spec-apps-inherit-bot-llm-2026-06-06.md for the rule and "
            "the three-transport contract. Specific issues: "
            + " | ".join(errors)
        ),
    }
