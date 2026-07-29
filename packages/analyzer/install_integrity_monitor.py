"""install_integrity_monitor — periodic Signal producer for the wizard verify gauntlet.

The wizard verification gauntlet ([packages/admin/evolve_admin/wizard_verify.py]
+ spec at docs/spec-wizard-verification-gauntlet-2026-05-30.md) ships as an
install-time gate AND an operator-triggered per-tile Verify button. Neither
of those catches drift between Verify-runs: tokens rotate, operators run
ad-hoc `sudo cp` and forget to chown back, deploy regressions reintroduce
missing `model.primary`. This monitor closes that gap by running the
gauntlet's non-interactive checks (1 = ownership, 2 = agent dry-run minus
the gateway-down sub-step, 3 = channels) on a daily cadence and emitting
Signals for each non-OK finding.

Why daily, not hourly:
  - The gauntlet's checks are correctness checks, not liveness checks.
    Liveness already has hourly coverage via heal/pod-health.
  - File ownership and credential shape drift slowly. Daily is enough
    to surface within-day; the bot still works in the meantime (or it
    doesn't, in which case heal/pod-health fires first).
  - Cost: ~3-5s per bot for Checks 1+2+3. 8 bots × 5s = 40s once per
    day. Trivial.

Skip overlaps with existing monitors:
  - Check 2.2 (`openclaw config validate`) — already covered by
    `openclaw_config_validator.py` (post-pull, Signal-producing). This
    monitor still runs the dry-run check end-to-end because it's part
    of the gauntlet's single primitive (`run_gauntlet`), but the same
    Signal will dedupe by signature.
  - Check 2.1 (gateway liveness) — already covered by heal.py at 5min
    cadence. We surface it here too for completeness, but the dedupe
    means at-most-one firing Signal per bot.
  - Check 4 (e2e echo) — requires operator action; never auto-runs.

Signal types (producer = "install_integrity_monitor"):

  install_integrity:ownership_drift          — Check 1 fail
  install_integrity:gateway_down              — Check 2 fail at gateway probe
  install_integrity:config_invalid            — Check 2 fail at openclaw config validate
  install_integrity:missing_primary_model     — Check 2 fail at primary-model check
  install_integrity:legacy_credential_shape   — Check 2 fail on credential shape (the [#1752] anthropic_api_key bug)
  install_integrity:missing_credential        — Check 2 fail when primary's required credential is absent
  install_integrity:channel_handshake_failed  — Check 3 fail (telegram/slack/discord)

Auto-resolves via `signals.store.sweep_resolve()` when the underlying
condition clears (e.g. operator fixes ownership; the next pass keeps a
different signature set).

Pure Python, no LLM. Runs as the `evolve` user (same as the other
admin-side monitors). Spec context:
[docs/spec-wizard-verification-gauntlet-2026-05-30.md] §6 (out-of-scope
"Auto-Verify on a schedule" — promoted to in-scope as a follow-up).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "install_integrity_monitor"


# Map from gauntlet CheckResult.name + issue-shape → (signal_type, severity).
# When a CheckResult has multiple issue kinds (Check 2 is the obvious case —
# can fail on missing model OR legacy key shape), we walk the issues and
# fan out into the matching specific signal types. This keeps each
# Signal's signature dedupe-stable (same root cause → same Signal across
# runs).

_SIGNAL_TYPE_OWNERSHIP_DRIFT          = "ownership_drift"
_SIGNAL_TYPE_GATEWAY_DOWN             = "gateway_down"
_SIGNAL_TYPE_CONFIG_INVALID           = "config_invalid"
_SIGNAL_TYPE_MISSING_PRIMARY_MODEL    = "missing_primary_model"
_SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE  = "legacy_credential_shape"
_SIGNAL_TYPE_LEGACY_CRED_SHAPE_POD    = "legacy_credential_shape_pod"
_SIGNAL_TYPE_MISSING_CREDENTIAL       = "missing_credential"
_SIGNAL_TYPE_CHANNEL_HANDSHAKE_FAILED = "channel_handshake_failed"

# Severity defaults — kept conservative. None of these warrant alert-tier
# (page-the-operator) on their own; they're "needs attention" not "site is
# down". heal/pod-health own the alert-tier liveness Signals.
_SIGNAL_DEFAULTS: dict[str, dict[str, str]] = {
    _SIGNAL_TYPE_OWNERSHIP_DRIFT:          {"severity": "warn"},
    _SIGNAL_TYPE_GATEWAY_DOWN:             {"severity": "warn"},
    _SIGNAL_TYPE_CONFIG_INVALID:           {"severity": "warn"},
    _SIGNAL_TYPE_MISSING_PRIMARY_MODEL:    {"severity": "warn"},
    _SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE:  {"severity": "warn"},
    _SIGNAL_TYPE_LEGACY_CRED_SHAPE_POD:    {"severity": "warn"},
    _SIGNAL_TYPE_MISSING_CREDENTIAL:       {"severity": "warn"},
    _SIGNAL_TYPE_CHANNEL_HANDSHAKE_FAILED: {"severity": "warn"},
}


# ─────────────────────────────────────────────────────────────────────────────
# Detector — pure function over a GauntletResult
# ─────────────────────────────────────────────────────────────────────────────


def detect_install_integrity_issues(
    bot_id: str,
    gauntlet_result,
    *,
    exempt_ocjson_checks: bool = False,
) -> list[dict]:
    """Return zero or more Signal-spec dicts for one bot's gauntlet result.

    The CheckResult objects from wizard_verify carry enough structure
    (issues lists with classification fields) that we can fan out a
    single Check 2 fail across multiple Signal types based on the issue
    shape. Each Signal-spec dict is shaped for direct splat into
    `signals_store.observe(shared_dir, **spec)`.

    ``exempt_ocjson_checks`` suppresses the openclaw.json-centric false-fires
    for the separated evo primary (EVOLVE-ACCT-OCJSON; see :func:`collect`):
    Check 2's config_invalid (openclaw config validate, wholly about
    openclaw.json) is dropped outright, and Check 1's ownership_drift is
    NARROWED — only the un-stat-able ``openclaw.json`` path is filtered out,
    while a wrong-owned ``auth-profiles.json`` / ``sessions.json`` /
    ``workspace/`` path under evo's ``.openclaw/`` STILL raises
    ownership_drift (genuine cross-account drift on secret files is a
    security signal, not a false positive). The bot's gateway-liveness,
    credential, and channel findings always fire — the exemption never
    silences anything but the openclaw.json the evolve account legitimately
    cannot stat across evo's private home.
    """
    out: list[dict] = []
    for check in gauntlet_result.checks:
        if check.status == "ok" or check.status == "skip" or check.status == "pending":
            continue
        if check.name == "ownership":
            sig = _signal_for_ownership(
                bot_id, check, exempt_ocjson_checks=exempt_ocjson_checks,
            )
            if sig is not None:
                out.append(sig)
        elif check.name == "agent_dry_run":
            out.extend(_signals_for_agent_dry_run(
                bot_id, check, exempt_ocjson_checks=exempt_ocjson_checks,
            ))
        elif check.name == "channels":
            out.append(_signal_for_channels(bot_id, check))
        # Pre-2026-06-01 there was also an ``e2e_echo`` check (the
        # wizard's "End-to-end echo" Check 4). It was retired in favor
        # of the dedicated pairing wizard modal — see
        # ``evolve_admin.pairing``. The branch is gone because
        # run_gauntlet no longer emits an e2e_echo row.
        # Anything else is a check shape we don't know about — skip with
        # no Signal rather than fire a malformed one. Out-of-band logging
        # in run() will note the unmapped status.
    return out


def _signal_for_ownership(
    bot_id: str, check, *, exempt_ocjson_checks: bool = False,
) -> dict | None:
    """Translate ownership-audit CheckResult into a Signal-spec dict.

    ``exempt_ocjson_checks`` narrows — does NOT silence — the ownership
    finding for the separated evo primary (EVOLVE-ACCT-OCJSON). The only
    wrong-owned path the `evolve` account spuriously trips on is the
    bot-shaped ``openclaw.json`` it cannot stat across the evo account's
    750 home (the file exists but evo's home is private). So we drop ONLY
    issues whose path ends in ``openclaw.json`` and still raise
    ``ownership_drift`` for any OTHER wrong-owned path under evo's
    ``.openclaw/`` — genuine cross-account drift on ``auth-profiles.json``,
    ``sessions.json``, or ``workspace/`` files is a security signal we must
    not blind ourselves to. Returns ``None`` when every issue was the
    suppressed openclaw.json one (nothing left to fire on) — and, for ANY bot
    (exempt or not), when there are no dict-shaped issues at all. A 0-path
    ownership_drift is never meaningful: it would emit ``issue_count: 0`` with
    an empty ``issues`` list — a cry-wolf Signal that fires with nothing wrong
    and never sweep-resolves (the underlying check keeps reporting the same
    empty-issue signature each pass, so it is re-observed forever). See
    Chip L3a in docs/spec-drift-alert-taxonomy-2026-06-26.md.
    """
    issues = [i for i in check.issues if isinstance(i, dict)]
    if exempt_ocjson_checks:
        # The whole finding may have been the un-stat-able openclaw.json for the
        # separated evo primary — drop those paths; the unconditional guard
        # below then suppresses if nothing genuine is left.
        issues = [
            i for i in issues
            if not str(i.get("path", "")).endswith("openclaw.json")
        ]
    # Unconditional empty guard — for every bot, exempt or not. With no
    # actionable dict-shaped issues there is nothing to fire on; emitting a
    # 0-path ownership_drift is a cry-wolf alert that never auto-resolves.
    if not issues:
        return None
    issue_paths = [i.get("path", "") for i in issues]
    sample = issue_paths[:5]
    sample_lines = "\n".join(f"  - `{p}`" for p in sample)
    extra = "" if len(issue_paths) <= 5 else f"\n  … and {len(issue_paths) - 5} more"
    body = (
        f"{len(issue_paths)} path(s) under `.openclaw/` are not owned by `{bot_id}`. "
        f"Bot operations that need to write or read these paths will fail with "
        f"EACCES once the cached file handles expire.\n\n"
        f"{sample_lines}{extra}\n\n"
        f"Fix: click 🔍 Verify on the bot tile → Fix ownership. The Signal will "
        f"auto-resolve on the next monitor pass once the chown lands."
    )
    return {
        "signature": make_signature(PRODUCER, _SIGNAL_TYPE_OWNERSHIP_DRIFT, bot_id),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_OWNERSHIP_DRIFT,
        "flavor": "maintenance",
        "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_OWNERSHIP_DRIFT]["severity"],
        "scope": "bot",
        "bot_id": bot_id,
        "title": f"{bot_id}: {len(issue_paths)} path(s) under .openclaw/ wrong-owned",
        "body": body,
        "details": {
            "bot_id": bot_id,
            "issue_count": len(issue_paths),
            "issues": issues,
        },
    }


def _signals_for_agent_dry_run(
    bot_id: str, check, *, exempt_ocjson_checks: bool = False,
) -> list[dict]:
    """Translate agent-dry-run CheckResult into one or more Signal-spec dicts.

    The check's summary tells us which sub-step failed; we map back to
    the specific Signal type. Falls back to a generic "config_invalid"
    framing if the shape doesn't match anything we know about.

    ``exempt_ocjson_checks`` suppresses ONLY the config_invalid (openclaw
    config validate) finding for the separated evo primary — the
    gateway-down, missing-primary-model, and credential findings still fire.
    """
    # Gateway-down — distinct so it dedupes against heal/pod-health (if
    # we surface it at all here, it'd be redundant; emit only when the
    # gauntlet's diagnostic adds something useful).
    summary_lower = (check.summary or "").lower()
    if "gateway" in summary_lower and ("not responding" in summary_lower
                                       or "no gateway port" in summary_lower):
        return [{
            "signature": make_signature(PRODUCER, _SIGNAL_TYPE_GATEWAY_DOWN, bot_id),
            "producer": PRODUCER,
            "type": _SIGNAL_TYPE_GATEWAY_DOWN,
            "flavor": "maintenance",
            "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_GATEWAY_DOWN]["severity"],
            "scope": "bot",
            "bot_id": bot_id,
            "title": f"{bot_id}: gateway not responding to /evolve/status",
            "body": (
                f"{check.summary}\n\n"
                f"{check.detail or ''}\n\n"
                f"The hourly heal monitor is the source of truth for gateway "
                f"liveness; this Signal is a tag-along finding from the "
                f"daily integrity sweep."
            ),
            "details": {"bot_id": bot_id, "summary": check.summary},
        }]

    # Config-invalid — same shape as openclaw_config_validator's Signal.
    # Different signature (different producer), so both Signals coexist
    # until both pass. The validator runs at 15-min cadence (post-pull),
    # this monitor at 24h, so the validator's Signal is typically the
    # one operators see first.
    if "rejected" in summary_lower or "openclaw config validate" in summary_lower:
        if exempt_ocjson_checks:
            # Separated evo primary has no stat-able bot-shaped openclaw.json
            # for the validator to accept/reject — suppress this finding only.
            return []
        return [{
            "signature": make_signature(PRODUCER, _SIGNAL_TYPE_CONFIG_INVALID, bot_id),
            "producer": PRODUCER,
            "type": _SIGNAL_TYPE_CONFIG_INVALID,
            "flavor": "maintenance",
            "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_CONFIG_INVALID]["severity"],
            "scope": "bot",
            "bot_id": bot_id,
            "title": f"{bot_id}: openclaw.json rejected by validator",
            "body": (
                f"{check.summary}\n\n"
                f"{check.detail or ''}\n\n"
                f"The post-pull openclaw_config_validator covers the same check at "
                f"15-min cadence; if you see this Signal, you almost certainly see "
                f"the validator's twin too. Same fix applies."
            ),
            "details": {"bot_id": bot_id, "issues": check.issues},
        }]

    # Missing primary model — the [#1701]/[#1703]/[#1707] regression class.
    if "primary" in summary_lower and ("not set" in summary_lower
                                       or "not configured" in summary_lower):
        return [{
            "signature": make_signature(PRODUCER, _SIGNAL_TYPE_MISSING_PRIMARY_MODEL, bot_id),
            "producer": PRODUCER,
            "type": _SIGNAL_TYPE_MISSING_PRIMARY_MODEL,
            "flavor": "maintenance",
            "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_MISSING_PRIMARY_MODEL]["severity"],
            "scope": "bot",
            "bot_id": bot_id,
            "title": f"{bot_id}: agents.defaults.model.primary not set",
            "body": (
                f"Without an explicit primary model the gateway falls back to "
                f"whatever OC ships as default — historically an OpenAI model "
                f"even when only Anthropic keys are configured. This was the "
                f"atlas onboarding regression [#1701/#1703/#1707].\n\n"
                f"Fix: open the bot's Config or Credentials page in the admin UI, "
                f"or POST `/api/wizard/seed-model-config` with the bot id. "
                f"The Signal will auto-resolve on the next monitor pass once "
                f"`agents.defaults.model.primary` is populated."
            ),
            "details": {"bot_id": bot_id},
        }]

    # Credential-shape issues — fan out one Signal per issue class
    # (legacy_credential_shape vs missing_credential) because they have
    # different fix paths.
    legacy = [
        i for i in check.issues
        if isinstance(i, dict) and i.get("canonical")
    ]
    missing_cred = [
        i for i in check.issues
        if isinstance(i, dict) and not i.get("canonical")
        and "no " in (i.get("summary") or "").lower()
        and "credential" in (i.get("summary") or "").lower()
    ]
    specs: list[dict] = []
    if legacy:
        specs.append({
            "signature": make_signature(
                PRODUCER, _SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE, bot_id,
            ),
            "producer": PRODUCER,
            "type": _SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE,
            "flavor": "maintenance",
            "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE]["severity"],
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: legacy credential key shape in auth-profiles.json"
            ),
            "body": (
                f"The auth-profiles.json contains one or more keys using the "
                f"legacy underscore shape (e.g. `anthropic_api_key`) rather "
                f"than the canonical colon shape (`anthropic:api_key`). OC's "
                f"profile resolver silently falls through legacy keys; the "
                f"agent gets no credential and Anthropic calls fall back to "
                f"OpenAI's default or fail outright. This was the atlas "
                f"[#1752] regression.\n\n"
                f"Fix: rewrite each key with a colon separator via the "
                f"Credentials page, or by editing auth-profiles.json directly."
            ),
            "details": {"bot_id": bot_id, "issues": legacy},
        })
    if missing_cred:
        specs.append({
            "signature": make_signature(
                PRODUCER, _SIGNAL_TYPE_MISSING_CREDENTIAL, bot_id,
            ),
            "producer": PRODUCER,
            "type": _SIGNAL_TYPE_MISSING_CREDENTIAL,
            "flavor": "maintenance",
            "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_MISSING_CREDENTIAL]["severity"],
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: primary-model credential missing from auth-profiles.json"
            ),
            "body": (
                f"The primary model is configured but its required credential "
                f"is absent. The bot will fail to authenticate against the "
                f"upstream API on its next agent turn.\n\n"
                f"Fix: add the credential through the bot's Credentials page. "
                f"The Signal will auto-resolve on the next monitor pass once "
                f"the profile lands."
            ),
            "details": {"bot_id": bot_id, "issues": missing_cred},
        })
    if specs:
        return specs

    # Unknown shape — emit nothing but log so the monitor's stdout
    # captures the unmapped case for follow-up.
    print(
        f"[install_integrity_monitor] {bot_id}: agent_dry_run failed with "
        f"unmapped shape: {check.summary!r}",
        flush=True,
    )
    return []


def _signal_for_channels(bot_id: str, check) -> dict:
    """Translate channel-handshake CheckResult into a Signal-spec dict."""
    failed_channels = sorted({
        i.get("channel", "?") for i in check.issues if isinstance(i, dict)
    })
    details_lines = [
        f"  - {issue.get('channel', '?')}: {issue.get('summary', '?')}"
        for issue in check.issues if isinstance(issue, dict)
    ]
    body = (
        f"Channel token revalidation failed for: {', '.join(failed_channels)}.\n\n"
        f"Per-channel detail:\n" + "\n".join(details_lines) + "\n\n"
        f"Tokens may have been rotated, revoked, or invalidated upstream. "
        f"Rotate via the bot's Channels page; the Signal will auto-resolve "
        f"on the next monitor pass once the handshake succeeds."
    )
    return {
        "signature": make_signature(PRODUCER, _SIGNAL_TYPE_CHANNEL_HANDSHAKE_FAILED, bot_id),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_CHANNEL_HANDSHAKE_FAILED,
        "flavor": "maintenance",
        "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_CHANNEL_HANDSHAKE_FAILED]["severity"],
        "scope": "bot",
        "bot_id": bot_id,
        "title": (
            f"{bot_id}: {len(failed_channels)} channel token(s) failed handshake "
            f"({', '.join(failed_channels)})"
        ),
        "body": body,
        "details": {"bot_id": bot_id, "channels": failed_channels, "issues": check.issues},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    network: dict[str, Any],
    *,
    gauntlet_runner=None,
) -> list[dict]:
    """Run the gauntlet against every bot and return Signal specs.

    ``gauntlet_runner`` is the function used to actually invoke
    ``wizard_verify.run_gauntlet`` — injected for unit-testing so we can
    feed canned CheckResult sequences without a live bot. Default
    resolves to the real function via lazy import.
    """
    if gauntlet_runner is None:
        from evolve_admin.wizard_verify import run_gauntlet as _real_runner
        gauntlet_runner = _real_runner

    bots = network.get("bots") or {}
    if not bots:
        return []

    # Post-evo-account-separation exemption (EVOLVE-ACCT-OCJSON,
    # spec-evo-account-separation-2026-05-25): the separated evo primary does
    # not host a stat-able bot-shaped openclaw.json the way a member bot does
    # (it exists at /home/evo/.openclaw/openclaw.json but evo's home is 750, so
    # the `evolve` service account cannot traverse to stat it). That makes
    # Check 2's openclaw-config-validate and the openclaw.json row of Check 1's
    # ownership walk spuriously fire every pass. Identify that one bot and pass
    # the exemption: config_invalid is dropped, and ownership_drift is NARROWED
    # to drop only the openclaw.json path — any OTHER wrong-owned secret/file
    # under evo's .openclaw/ still fires (see _signal_for_ownership). True
    # no-op on pre-separation pods (primary on the `evolve` account, or a legacy
    # bot literally named `evolve`) — the predicate resolves False and every
    # bot keeps the full gauntlet.
    from primary_bot import (  # type: ignore
        primary_bot_id as _primary_bot_id,
        primary_is_separated_evo as _primary_is_separated_evo,
    )
    exempt_primary = (
        _primary_bot_id(network) if _primary_is_separated_evo(network) else None
    )

    specs: list[dict] = []
    for bot_id in sorted(bots.keys()):
        try:
            result = gauntlet_runner(bot_id, network=network)
        except Exception as exc:
            print(
                f"[install_integrity_monitor] {bot_id}: gauntlet crashed: {exc}",
                flush=True,
            )
            continue
        specs.extend(detect_install_integrity_issues(
            bot_id, result,
            exempt_ocjson_checks=(bot_id == exempt_primary),
        ))
    return specs


def build_signal_for_pod_legacy_cred_shape(bot_ids: list[str]) -> dict:
    """One pod-scope signal covering every bot with legacy underscore-shape
    keys in its auth-profiles.json.

    Producer-level coalescing: the fix is a uniform migration (rewrite
    each underscore key with a colon separator). N per-bot rows in the
    Alerts UI is noise — one migration covers them all. The 2026-06-03
    Alerts review caught 9 bots fanned out on this one issue.

    Signature is producer-stable (``legacy_credential_shape_pod:all``)
    so the same Signal stays open across runs.
    """
    n = len(bot_ids)
    sorted_ids = sorted(bot_ids)
    plural = "" if n == 1 else "s"
    title = (
        f"{n} bot{plural} have legacy credential key shape in auth-profiles.json"
    )
    body = "\n".join([
        f"{n} bot{plural} have auth-profiles.json entries using the legacy "
        f"underscore shape (e.g. `anthropic_api_key`) rather than the "
        f"canonical colon shape (`anthropic:api_key`):",
        "",
        *[f"- `{b}`" for b in sorted_ids],
        "",
        "OC's profile resolver silently falls through legacy keys; the "
        "agent gets no credential and Anthropic calls fall back to OpenAI's "
        "default or fail outright. This was the atlas [#1752] regression.",
        "",
        "**Fix**: rewrite each key with a colon separator. Either visit each "
        "bot's Credentials page in turn, or edit each auth-profiles.json "
        "directly. The Signal auto-resolves on the next monitor pass "
        "once every listed bot is migrated.",
    ])
    return {
        "signature": make_signature(
            PRODUCER, _SIGNAL_TYPE_LEGACY_CRED_SHAPE_POD, "all",
        ),
        "producer": PRODUCER,
        "type": _SIGNAL_TYPE_LEGACY_CRED_SHAPE_POD,
        "flavor": "maintenance",
        "severity": _SIGNAL_DEFAULTS[_SIGNAL_TYPE_LEGACY_CRED_SHAPE_POD]["severity"],
        "scope": "pod",
        "bot_id": None,
        "title": title,
        "body": body,
        "details": {
            "bot_ids": sorted_ids,
            "bot_count": n,
            "what_it_means": (
                "Underscore-shape credential keys are silently ignored by "
                "OC's profile resolver — the agent runs with no credential "
                "and falls back to whatever default the upstream picks. "
                f"One migration sweep clears all {n} bot{plural}."
            ),
            "fix_steps": (
                "1. For each bot listed in this Signal's `details.bot_ids`, "
                "open the bot's Credentials page in the admin UI\n"
                "2. Re-enter each credential — the form rewrites it under "
                "the canonical colon shape\n"
                "3. The next monitor run (within 1h) re-checks and "
                "auto-resolves this Signal once every bot is clean"
            ),
        },
    }


def run(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    dry_run: bool = False,
    gauntlet_runner=None,
) -> tuple[set[str], int, int]:
    """Collect, write Signals, sweep-resolve cleared conditions.

    Returns ``(kept_signatures, n_fired, n_resolved)``.
    """
    detections = collect(network, gauntlet_runner=gauntlet_runner)
    kept: set[str] = set()
    n_fired = 0
    # Bots with the legacy credential key shape get coalesced into a
    # single pod-scope signal — same producer, different type. Per-bot
    # rows in the UI are noise when the fix is a uniform migration.
    legacy_cred_bots: list[str] = []
    for d in detections:
        if d.get("type") == _SIGNAL_TYPE_LEGACY_CREDENTIAL_SHAPE:
            bid = d.get("bot_id")
            if bid:
                legacy_cred_bots.append(bid)
            continue
        kept.add(d["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[install_integrity_monitor] observe failed for "
                f"{d['signature']}: {exc}",
                flush=True,
            )

    # Pod-scope coalesce for legacy credential shape. Emitted once at
    # the end with the full bot list. Old per-bot
    # legacy_credential_shape signals get swept-resolved below because
    # we no longer keep their signatures.
    if legacy_cred_bots:
        pod_spec = build_signal_for_pod_legacy_cred_shape(legacy_cred_bots)
        kept.add(pod_spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": pod_spec}, default=str), flush=True)
        else:
            try:
                signals_store.observe(shared_dir, **pod_spec)
            except Exception as exc:
                print(
                    f"[install_integrity_monitor] observe failed for "
                    f"{pod_spec['signature']}: {exc}",
                    flush=True,
                )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: install integrity restored",
            )
            n_resolved = len(resolved)
        except Exception as exc:
            print(
                f"[install_integrity_monitor] sweep_resolve failed: {exc}",
                flush=True,
            )

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "install_integrity_monitor — periodic Signal producer for the "
            "wizard verify gauntlet's automatic checks."
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    kept, n_fired, n_resolved = run(shared_dir, config, dry_run=args.dry_run)
    if args.dry_run:
        print(
            f"[install_integrity_monitor] dry-run: {n_fired} would-fire",
            flush=True,
        )
        return
    print(
        f"[install_integrity_monitor] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
