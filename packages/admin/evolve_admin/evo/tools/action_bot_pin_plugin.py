"""Action tool — pin OC plugin install versions across one or many bots.

Created in response to the 2026-05-26 operator transcript where evo
recalled a `plugins.installs_unpinned_npm_specs` audit finding across
6 bots, started to act on it, hit the privilege boundary, and emitted
14 lines of `sudo -u <bot> openclaw plugins install --pin ...` — the
shape PR #1626 had just patched the inspector to reject. The inspector
correctly rejected the shell wall, but without a UI alternative or a
registered tool, the operator got the bare ``hallucinated_cli`` stub
and no forward path.

Diagnosis chain:

  * PR #1492 / #1503 — Phase 4 inspector landed (catches shell-as-advice).
  * PR #1623 — accuracy verification rejects sudo -u <unknown-bot> walls.
  * PR #1626 — extended ``hallucinated_cli`` stub to point at the
    ui-alternative table when one exists; the unpin case had no entry.
  * THIS PR — adds the registered tool the inspector should now point at.

Why a single tool that accepts a list (rather than one-bot-at-a-time):
the operator's natural shape is "pin <pkg>@<version> across <list of
bots>" — a single OC version landing across the pod typically triggers
the unpinned-spec finding on every bot at once. Batching the action
matches how the operator thinks; the tool returns a per-bot result so
partial failures surface clearly without rolling back the bots that
succeeded.

Risk tier: ``write_risky`` — touches openclaw.json on multiple bots and
kickstarts their gateways. The gateway restart briefly takes each bot
offline (~3-5s). Same risk shape as the other openclaw.json-writing
appliers (UpdatePermissionConfig — packages/analyzer/arbiter/appliers/
permissions.py — was the reference; see memory note
``project_l1_l2_applier_architecture``).

Mechanism: read each bot's openclaw.json via the ACL-aware reader, mutate
``plugins.installs.<id>.spec`` to ``<npm_pkg>@<version>``, write back via
``safe_write_bot_config`` (validates against the OC schema + atomic
/tmp staging + sudo /bin/cp), kickstart the bot gateway, record an
intent so future auto-upgrade generators know this pin was deliberate.
The OC ``plugins/installs.json`` canonical store is left in-place — its
``installPath`` / ``integrity`` / ``shasum`` fields are produced by the
``openclaw plugins install`` runtime and we don't need to touch them
just to update the spec string the security audit reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────────


# Exact-version pattern: ``X.Y.Z`` with optional ``-prerelease`` /
# ``+build`` suffixes (CalVer like 2026.5.22, semver like 1.2.3-beta.1).
# Rejects bare ``latest``, ``next``, version ranges (``^1.0`` / ``~1.0``),
# wildcards, and anything else that resolves at install time rather than
# pinning. The audit signal we're clearing fires on any non-exact spec.
_EXACT_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+){2}(?:[.-][0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

# Recognized non-exact prefixes — surfaced verbatim so the operator
# sees which form was rejected (helpful when evo hands raw npm range
# syntax through).
_NON_EXACT_HINTS: tuple[str, ...] = ("^", "~", ">=", "<=", ">", "<", "*", "x")

# ASCII-only bot id / plugin name patterns — mirror action_plugin.py so
# Unicode look-alikes can't slip through.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_NAME_RE = re.compile(r"^[@A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _read_openclaw_json(bot_user: str) -> tuple[dict | None, str | None]:
    """Read a bot's openclaw.json with the L2-applier ACL fallback pattern.

    Direct ``Path.read_text`` first (works whenever the evolve user has
    the ACL grant set by ``deploy.set_evolve_read_acl``); falls back to
    ``sudo /bin/cat`` (covered by the sudoers grant on
    ``/Users/*/.openclaw/openclaw.json``).
    """
    path = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    text: str | None = None
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, f"openclaw.json not found at {path}"
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, f"sudo cat failed: {exc}"
        if r.returncode != 0:
            return None, f"sudo cat returned rc={r.returncode}: {r.stderr.strip()}"
        text = r.stdout
    except OSError as exc:
        return None, f"read failed: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"openclaw.json is not valid JSON: {exc}"


def _resolve_install_key(
    installs: dict, plugin_name: str,
) -> tuple[str | None, dict | None]:
    """Find the install record matching ``plugin_name``.

    Operators say things two ways:

      * The OC plugin id (the key in plugins.installs) — e.g. "brave".
      * The npm package name (the prefix of the spec string) — e.g.
        "@openclaw/brave-plugin".

    We try the direct-key match first (cheap), then fall back to a
    spec-prefix scan. Returns ``(key, record)`` or ``(None, None)`` if
    nothing matched.
    """
    # 1. Direct key match.
    if plugin_name in installs and isinstance(installs[plugin_name], dict):
        return plugin_name, installs[plugin_name]

    # 2. Spec-prefix match: spec is "<npm-pkg>@<version>" or bare
    #    "<npm-pkg>". Match either form.
    for key, rec in installs.items():
        if not isinstance(rec, dict):
            continue
        spec = rec.get("spec")
        if not isinstance(spec, str):
            continue
        # Strip the version suffix (handle scoped packages: the leading
        # "@" doesn't count as a tag separator — split on the rightmost
        # "@" only if there's one past the scope marker).
        npm_pkg = _spec_to_npm_package(spec)
        if npm_pkg == plugin_name:
            return key, rec
    return None, None


def _spec_to_npm_package(spec: str) -> str:
    """Strip the version suffix from a spec string.

    Examples:
      ``@openclaw/brave-plugin@2026.5.22`` → ``@openclaw/brave-plugin``
      ``brave@1.2.3`` → ``brave``
      ``@openclaw/brave-plugin`` (no version) → ``@openclaw/brave-plugin``
    """
    if not spec:
        return spec
    last_at = spec.rfind("@")
    if spec.startswith("@"):
        # Scoped — version separator only counts if there's a second `@`.
        if last_at > 0:
            return spec[:last_at]
        return spec
    if last_at >= 0:
        return spec[:last_at]
    return spec


def _is_exact_version(version: str) -> bool:
    """Return True iff ``version`` is an exact pin (no ranges/tags)."""
    if not version or not isinstance(version, str):
        return False
    # Reject anything that starts with a range/wildcard operator.
    if any(version.startswith(h) for h in _NON_EXACT_HINTS):
        return False
    if version.lower() in ("latest", "next", "beta", "alpha", ""):
        return False
    return bool(_EXACT_VERSION_RE.match(version))


def _kickstart_gateway(bot_id: str) -> tuple[bool, str]:
    """Kick the bot's gateway via the Scheduler seam.

    Mirrors the pattern in ``safe_write_bot_config``'s callers — the
    seam's launchd adapter shells out to the same ``sudo /bin/launchctl
    kickstart -k`` argv, so privilege still comes from the grant in
    ``/etc/sudoers.d/evolve`` covering
    ``/bin/launchctl kickstart -k system/ai.openclaw.*``.
    """
    from ...runtime import get_scheduler
    ok, out = get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")
    # kickstart returns non-zero when the service isn't loaded (rare —
    # the bot would already be offline). Surface the output so callers
    # can diagnose; don't treat it as a hard failure on its own. The
    # seam's runner never raises — timeouts/OSErrors surface here too.
    if not ok:
        return False, f"launchctl kickstart failed: {out.strip() or 'no output'}"
    return True, ""


def _record_pin_intent(
    bot_id: str, install_key: str, npm_pkg: str, version: str,
    *, reason: str | None,
) -> None:
    """Record a config-intent for this pin.

    Best-effort: failures are logged but never roll back the spec write.
    The intent record lets future auto-upgrade generators see this pin
    was deliberate (and signed by the operator-driven evo tool, not by
    drift). See ``feedback_generators_consider_intent``.
    """
    try:
        from evolve_admin.config_intent import set_intent
    except ImportError:
        return
    field_path = f"plugins.installs.{install_key}.spec"
    spec_value = f"{npm_pkg}@{version}"
    intent_reason = (
        reason
        if reason and reason.strip()
        else f"operator pinned {npm_pkg} to {version} via action.bot.pin_plugin_version"
    )
    try:
        set_intent(
            bot_id, field_path, spec_value,
            reason=intent_reason,
            set_by="action.bot.pin_plugin_version",
            set_by_detail=(
                "Pinned via evo tool in response to the OC native audit "
                "finding plugins.installs_unpinned_npm_specs (2026.5.18+)."
            ),
            actor="evo",
        )
    except Exception as exc:  # noqa: BLE001 — hygiene metadata, never blocking
        log.warning(
            "pin_plugin_version: failed to record intent for %s/%s: %s",
            bot_id, install_key, exc,
        )


# ─── Per-bot pin logic ──────────────────────────────────────────────────────


def _pin_one(
    network: dict,
    pin: dict,
) -> dict[str, Any]:
    """Apply one pin: ``{bot_id, plugin_name, version}`` → result dict.

    Returns a per-bot result with ``ok``, plus ``status`` codes the
    caller aggregates ("pinned" / "already_pinned" / "unknown_bot" /
    "unknown_plugin" / "invalid_version" / "write_failed" /
    "kickstart_failed"). Each result includes ``bot_id``, ``plugin_name``,
    ``requested_version``, and (when known) ``install_key`` + ``prior_spec``
    + ``new_spec`` so the operator and downstream tooling can diff.
    """
    bot_id = (pin.get("bot_id") or "").strip()
    plugin_name = (pin.get("plugin_name") or "").strip()
    version = (pin.get("version") or "").strip()
    reason = pin.get("reason")

    base: dict[str, Any] = {
        "bot_id": bot_id,
        "plugin_name": plugin_name,
        "requested_version": version,
    }

    if not _BOT_ID_RE.fullmatch(bot_id):
        return {**base, "ok": False, "status": "invalid_bot_id",
                "error": f"bot_id {bot_id!r} is invalid (ASCII letters/digits/-/_, ≤64)"}
    if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
        return {**base, "ok": False, "status": "invalid_plugin_name",
                "error": f"plugin_name {plugin_name!r} is invalid"}
    if not _is_exact_version(version):
        return {**base, "ok": False, "status": "invalid_version", "error": (
            f"version {version!r} is not an exact pin — must be X.Y.Z "
            "(or X.Y.Z-prerelease). Range syntax (^/~), 'latest', and "
            "wildcards are rejected because the OC security audit "
            "(plugins.installs_unpinned_npm_specs) requires an exact spec."
        )}

    bots = network.get("bots") or {}
    if bot_id not in bots:
        return {**base, "ok": False, "status": "unknown_bot",
                "error": f"bot '{bot_id}' is not registered in network.json"}

    bot_user = (bots[bot_id].get("user") or bot_id) if isinstance(bots[bot_id], dict) else bot_id

    cfg, read_err = _read_openclaw_json(bot_user)
    if cfg is None:
        return {**base, "ok": False, "status": "read_failed",
                "error": read_err or "openclaw.json unreadable"}

    plugins_block = cfg.get("plugins")
    if not isinstance(plugins_block, dict):
        return {**base, "ok": False, "status": "no_plugins_block",
                "error": "openclaw.json has no plugins block"}
    installs = plugins_block.get("installs")
    if not isinstance(installs, dict):
        return {**base, "ok": False, "status": "no_installs_block",
                "error": "openclaw.json has no plugins.installs entries"}

    install_key, record = _resolve_install_key(installs, plugin_name)
    if install_key is None or record is None:
        return {**base, "ok": False, "status": "unknown_plugin", "error": (
            f"no install record on {bot_id} matches plugin_name "
            f"{plugin_name!r} (tried direct key + spec-prefix match)"
        )}

    prior_spec = record.get("spec") if isinstance(record, dict) else None
    npm_pkg = _spec_to_npm_package(prior_spec) if isinstance(prior_spec, str) else plugin_name
    if not npm_pkg:
        npm_pkg = plugin_name
    new_spec = f"{npm_pkg}@{version}"

    if prior_spec == new_spec:
        return {**base, "ok": True, "status": "already_pinned",
                "install_key": install_key,
                "prior_spec": prior_spec, "new_spec": new_spec,
                "npm_package": npm_pkg}

    # Mutate + write back via safe_write_bot_config — the same path the
    # L2 applier uses. Validates against the OC schema before touching
    # the live file and backs up the prior config to openclaw.json.bak.
    new_cfg = json.loads(json.dumps(cfg))  # cheap deepcopy
    new_cfg["plugins"]["installs"][install_key]["spec"] = new_spec

    try:
        from evolve_admin.deploy import safe_write_bot_config
    except ImportError as exc:
        return {**base, "ok": False, "status": "deploy_unavailable",
                "error": f"evolve_admin.deploy.safe_write_bot_config not importable: {exc}",
                "install_key": install_key, "prior_spec": prior_spec, "new_spec": new_spec}

    ok, err = safe_write_bot_config(
        bot_id, new_cfg,
        reason=f"action.bot.pin_plugin_version({install_key}@{version})",
        bot_user=bot_user,
    )
    if not ok:
        return {**base, "ok": False, "status": "write_failed", "error": err,
                "install_key": install_key, "prior_spec": prior_spec, "new_spec": new_spec}

    # Kickstart so the new openclaw.json takes effect.
    ks_ok, ks_err = _kickstart_gateway(bot_id)
    # Record intent regardless of kickstart outcome — the file is already
    # written, and the intent reflects the operator's choice independent
    # of whether the gateway picked it up on this attempt.
    _record_pin_intent(bot_id, install_key, npm_pkg, version, reason=reason)

    result: dict[str, Any] = {
        **base,
        "ok": True,
        "status": "pinned",
        "install_key": install_key,
        "prior_spec": prior_spec,
        "new_spec": new_spec,
        "npm_package": npm_pkg,
    }
    if not ks_ok:
        # The write landed; only the kickstart failed. Surface a soft
        # warning rather than rolling back — the next deploy / explicit
        # restart picks up the change. Same posture as
        # UpdatePermissionConfigApplier when kickstart is best-effort.
        result["kickstart_warning"] = ks_err
    return result


# ─── Tool entry points ──────────────────────────────────────────────────────


def _pin_handler(
    network_path: Path,
    pins: list[dict] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply each pin and aggregate the results.

    Returns ``{ok, pins: [...], summary: {pinned, already_pinned, failed}}``
    where ``ok`` is True iff every pin succeeded or was a no-op. Partial
    failures surface in the per-pin list with their ``status`` code so
    the operator (and evo) can see exactly which bots succeeded.
    """
    if not isinstance(pins, list) or not pins:
        return {"ok": False, "error": "pins must be a non-empty list of "
                                       "{bot_id, plugin_name, version} entries"}

    try:
        from evolve_admin.config import load_network
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not load network.json: {exc}"}

    # Carry the top-level ``reason`` into each pin that didn't supply its
    # own — useful when the operator says "pin X@Y across A,B,C because Z"
    # and evo doesn't want to repeat the same reason six times.
    enriched: list[dict] = []
    for p in pins:
        if not isinstance(p, dict):
            enriched.append({"ok": False, "status": "invalid_pin",
                             "error": f"pin entry must be a dict, got {type(p).__name__}"})
            continue
        if reason and not p.get("reason"):
            p = {**p, "reason": reason}
        enriched.append(p)

    results: list[dict] = []
    for p in enriched:
        if isinstance(p, dict) and "status" in p and p.get("status") == "invalid_pin":
            results.append(p)
            continue
        results.append(_pin_one(network, p))

    summary = {
        "pinned": sum(1 for r in results if r.get("status") == "pinned"),
        "already_pinned": sum(1 for r in results if r.get("status") == "already_pinned"),
        "failed": sum(1 for r in results if not r.get("ok")),
    }
    return {
        "ok": summary["failed"] == 0,
        "pins": results,
        "summary": summary,
        # The audit signal the pin clears is OC-side
        # (``plugins.installs_unpinned_npm_specs``); it re-evaluates on
        # OC's next security-audit sweep (~hourly). pod_state.audit
        # surfaces the cleared finding.
        "verify_via": {
            "tool": "pod_state.audit",
            "args": {},
            "expect": (
                "plugins.installs_unpinned_npm_specs no longer fires "
                "for the pinned bots/plugins after OC's next security "
                "audit sweep (~hourly)"
            ),
        },
    }


def _pin_validate(
    network_path: Path,
    pins: list[dict] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: ensure every pin's bot/plugin/version is syntactically
    OK and the bots all exist in network.json.

    We don't open openclaw.json here — that would require reading every
    bot's file synchronously just to render a confirm button. The
    handler does the per-bot read and surfaces unknown-plugin /
    unreadable-config errors per-pin. Validation's job is the cheap
    pre-check so the operator sees obvious mistakes (typo'd bot id,
    range version) before clicking confirm.
    """
    if not isinstance(pins, list) or not pins:
        return {"ok": False, "reason": "pins must be a non-empty list"}

    try:
        from evolve_admin.config import load_network
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"could not load network.json: {exc}"}

    known_bots = set((network.get("bots") or {}).keys())
    problems: list[str] = []
    for i, p in enumerate(pins):
        if not isinstance(p, dict):
            problems.append(f"pins[{i}] is not a dict")
            continue
        bot_id = (p.get("bot_id") or "").strip()
        plugin_name = (p.get("plugin_name") or "").strip()
        version = (p.get("version") or "").strip()
        if not _BOT_ID_RE.fullmatch(bot_id):
            problems.append(f"pins[{i}].bot_id {bot_id!r} is invalid")
            continue
        if bot_id not in known_bots:
            problems.append(f"pins[{i}].bot_id {bot_id!r} is not in network.json")
        if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
            problems.append(f"pins[{i}].plugin_name {plugin_name!r} is invalid")
        if not _is_exact_version(version):
            problems.append(
                f"pins[{i}].version {version!r} is not an exact pin "
                "(X.Y.Z required — ^/~/latest/ranges rejected)"
            )

    if problems:
        return {"ok": False, "reason": "; ".join(problems)}

    return {
        "ok": True,
        "context": {
            "pin_count": len(pins),
            "bots_touched": sorted({(p.get("bot_id") or "").strip() for p in pins
                                     if isinstance(p, dict)}),
            "estimated_duration_s": 2 * len(pins),  # rough: ~2s/bot
        },
    }


PIN_PLUGIN_VERSION_TOOL = Tool(
    name="action.bot.pin_plugin_version",
    description=(
        "Pin one or more OC plugin install records to exact versions, "
        "across one or many bots in a single call. Updates "
        "``plugins.installs.<id>.spec`` to ``<npm-pkg>@<exact-version>`` "
        "via the L2-applier pattern: read each bot's openclaw.json, "
        "patch the spec field, write back through "
        "``safe_write_bot_config`` (validates against the OC schema + "
        "atomic /tmp staging + sudo /bin/cp + chmod 644), then kickstart "
        "the gateway. Records a config-intent on each pin so future "
        "auto-upgrade generators see the operator's chosen version "
        "rather than re-flagging it as drift.\n\n"
        "Use when the operator says 'pin <pkg>@<version> across <bots>' "
        "or asks to clear the OC native audit finding "
        "``plugins.installs_unpinned_npm_specs``. The operator's natural "
        "shape is the batched form — a single OC release typically "
        "triggers the unpinned-spec finding on every bot at once. "
        "DO NOT emit ``sudo -u <bot> openclaw plugins install --pin ...`` "
        "shell snippets; this tool is the registered path and runs "
        "without operator copy-paste.\n\n"
        "``version`` must be exact (X.Y.Z or X.Y.Z-prerelease). Range "
        "syntax (^1.0, ~1.0), 'latest', 'next', and wildcards are "
        "rejected — the audit specifically requires an exact pin. The "
        "tool is idempotent per-bot: a pin already matching ``new_spec`` "
        "is reported as ``already_pinned`` (no write, no kickstart)."
    ),
    wire_description=(
        "Pin OC plugin installs to exact versions across one or many bots "
        "in one call (updates plugins.installs.<id>.spec, restarts each "
        "gateway ~3-5s, records intent). Use when the operator asks to pin "
        "plugin versions or to clear the plugins.installs_unpinned_npm_specs "
        "audit finding; NEVER emit sudo/openclaw shell for this. version "
        "must be exact X.Y.Z — ranges/'latest' are rejected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pins": {
                "type": "array",
                "description": "Pins to apply.",
                "items": {
                    "type": "object",
                    "properties": {
                        "bot_id": {
                            "type": "string",
                            "description": "Bot id from network.json::bots.",
                        },
                        "plugin_name": {
                            "type": "string",
                            "description": (
                                "OC plugin id (e.g. 'brave') or npm "
                                "package name (e.g. "
                                "'@openclaw/brave-plugin')."
                            ),
                        },
                        "version": {
                            "type": "string",
                            "description": (
                                "Exact version, e.g. '2026.5.22'. Range "
                                "syntax (^/~) and 'latest' are rejected."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "Optional per-pin reason (overrides the "
                                "top-level reason)."
                            ),
                        },
                    },
                    "required": ["bot_id", "plugin_name", "version"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason inherited by pins without their own "
                    "(audit log + recorded intent)."
                ),
            },
        },
        "required": ["pins"],
        "additionalProperties": False,
    },
    handler=_pin_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_pin_validate,
    tags=("action", "bot", "plugin", "audit"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)


register(PIN_PLUGIN_VERSION_TOOL)
