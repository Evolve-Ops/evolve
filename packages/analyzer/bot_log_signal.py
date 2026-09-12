"""bot_log_signal — convert per-bot gateway error-log lines into Signals.

Reads the ``recent_errors`` list from each bot's ``{shared_dir}/status/{bot}.json``
(written every ~5 min by heal.py) and pattern-matches known error signatures into
the Signal store.  Designed to run as part of ``pod_report`` so it inherits the
same cadence and shared-dir context without an extra daemon.

Pattern catalog
───────────────
max_auth_failure
    MAX subscription auth failed; bot is billing at metered API rates.
    Triggered by: "MAX auth failed", "fell back to metered billing"
    Deeplink: /admin/maintenance  (Claude Access subtab)

discord_target_invalid
    A cron job or alert tried to send a Discord message to a channel or
    user that does not exist / is not accessible.
    Triggered by: "Unknown Channel" + "discord", or "Unknown target" + "Discord"
    Deeplink: /admin/maintenance  (Setup subtab)

tool_delivery_failing
    Repeated tool-call failures with no more-specific match.
    Triggered by: ≥2 lines containing "[tools] message failed" for the same bot.
    Deeplink: /admin/maintenance

provider_override_unauthorized
    A plugin's subagent run was rejected because its requested provider/model
    override isn't authorized. The plugin keeps responding via fallback, but
    the LLM-quality step (tier classification, outcome extraction, etc.) is
    degraded. Surfaced after the 2026-06-04 Maintenance → Status review.
    Triggered by: "provider/model override is not authorized"
    Deeplink: /admin/maintenance

unknown_provider_model
    Gateway is configured with a model id that the provider registry doesn't
    list. Every request through this model path errors with FailoverError.
    This is the provider-registry incident class (2026-06-03) — the model
    exists in agents.defaults.* but its definition is missing from
    models.providers[<provider>].models[].
    Triggered by: "Unknown model:" + "models.providers"
    Deeplink: /admin/setup

plugin_load_failure
    A plugin listed in the bot's openclaw.json failed to load when the
    gateway started. The bot keeps responding to messages, but every
    capability the plugin provides (for evolve-plugin: cost tracking,
    observation tuples, conduct injection, signal collection) is silently
    dropped until the plugin loads cleanly. Most common cause is a
    "Cannot find module" error from an out-of-sync built bundle.
    Triggered by: "[plugins]" + "failed to load"
    Deeplink: /admin/maintenance

provider_auth_failover
    The gateway's primary provider returned HTTP 401/403, so OpenClaw's
    failover chain engaged a secondary model. The bot is still responding,
    but on a more expensive / lower-quality fallback, and cost compounds
    on every turn until the credential is rotated. Surfaced after the
    2026-06-09 alert root-cause audit (a pod bot's google/gemini PAT
    expired and the catch-all uncategorized_errors masked the actionable
    shape).
    Triggered by: "failover decision" + "reason=auth"
    Deeplink: /admin/maintenance

uncategorized_errors
    Catch-all surfacing recent gateway errors that don't match any known
    pattern. Keeps unknowns visible on the Alerts page instead of hiding
    them in the Maintenance → Status "Recent errors" column. As specific
    patterns get added to the catalog above, this signal naturally retires.
    Triggered by: ≥2 lines in recent_errors that match no other pattern.
    Deeplink: /admin/maintenance
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

PRODUCER = "bot_log_monitor"

# Severity-framework tag per signal_type. max_auth_failure is a cost
# concern (every turn pays metered API rates instead of MAX); the
# integration/tool failures are operations. Spec: §2.2 cost, §2.3 ops.
_SEVERITY_TAG: dict[str, tuple[str, int]] = {
    "max_auth_failure":              ("cost", 2),         # real overrun, scales with usage
    "discord_target_invalid":         ("operations", 2),  # single bot integration down
    "tool_delivery_failing":          ("operations", 2),  # bot degraded, real issue
    # Plugin subagent runs blocked → LLM steps degraded → quality slips,
    # but the bot still answers. magnitude=2 (real issue) but not 3.
    "provider_override_unauthorized": ("operations", 2),
    # Every request that lands on this model path errors. Provider-registry
    # drift is the documented 2026-06-03 incident class — magnitude=3
    # because the failure rate is structural, not transient.
    "unknown_provider_model":         ("operations", 3),
    # Plugin load failure is structural — fails on every gateway start
    # until the bundle is rebuilt — and the dropped capabilities are
    # load-bearing (cost tracking, signal collection, conduct).
    "plugin_load_failure":            ("operations", 3),
    # Provider auth failover: degraded bot, real issue (more expensive /
    # lower-quality fallback compounds on every turn), but not magnitude=3
    # because the failover keeps the bot responding to messages.
    "provider_auth_failover":         ("operations", 2),
    # Catch-all by definition lacks specific severity context. Conservative
    # magnitude=1 so it surfaces without crowding out specific findings.
    "uncategorized_errors":           ("operations", 1),
}


def _severity_tag(signal_type: str) -> tuple[str, int]:
    """Look up the (vector, magnitude) tag for a bot_log_monitor signal type.
    Unknown types fall through to operations:2 — conservative default."""
    return _SEVERITY_TAG.get(signal_type, ("operations", 2))


# ── Pattern definitions ────────────────────────────────────────────────────────

class _Pattern(NamedTuple):
    signal_type: str
    title_tmpl: str   # one {bot_id} placeholder
    body: str
    deeplink: str

    def matches(self, line: str) -> bool:
        raise NotImplementedError


class _AnyPattern(_Pattern):
    """Fires when *all* of the given substrings appear in the log line."""
    needles: tuple[str, ...]

    def __new__(cls, signal_type, title_tmpl, body, deeplink, needles):
        obj = super().__new__(cls, signal_type, title_tmpl, body, deeplink)
        object.__setattr__(obj, "needles", needles)
        return obj

    def matches(self, line: str) -> bool:
        lower = line.lower()
        return all(n.lower() in lower for n in self.needles)


_PATTERNS: list[_AnyPattern] = [
    _AnyPattern(
        signal_type="max_auth_failure",
        title_tmpl="{bot_id}: MAX auth not working — billing at API rates",
        body=(
            "MAX subscription auth is failing. Every conversation turn is being "
            "charged at metered API rates instead of the MAX plan. "
            "Go to Maintenance → Claude Access to check the MAX subscription status."
        ),
        deeplink="/admin/maintenance",
        needles=("max auth failed",),
    ),
    _AnyPattern(
        signal_type="max_auth_failure",
        title_tmpl="{bot_id}: MAX auth not working — billing at API rates",
        body=(
            "MAX subscription auth is failing. Every conversation turn is being "
            "charged at metered API rates instead of the MAX plan. "
            "Go to Maintenance → Claude Access to check the MAX subscription status."
        ),
        deeplink="/admin/maintenance",
        needles=("fell back to metered billing",),
    ),
    _AnyPattern(
        signal_type="discord_target_invalid",
        title_tmpl="{bot_id}: Discord notification target is unreachable",
        body=(
            "A message could not be delivered because the Discord channel or user "
            "target does not exist or the bot is not a member. "
            "Go to Maintenance → Setup to update the Discord integration target."
        ),
        deeplink="/admin/maintenance",
        needles=("unknown channel", "discord"),
    ),
    _AnyPattern(
        signal_type="discord_target_invalid",
        title_tmpl="{bot_id}: Discord notification target is unreachable",
        body=(
            "A message could not be delivered because the Discord channel or user "
            "target does not exist or the bot is not a member. "
            "Go to Maintenance → Setup to update the Discord integration target."
        ),
        deeplink="/admin/maintenance",
        needles=("unknown target", "discord"),
    ),
    _AnyPattern(
        signal_type="provider_override_unauthorized",
        title_tmpl="{bot_id}: plugin subagent blocked by provider/model override policy",
        body=(
            "An evolve-plugin subagent run was rejected because its requested "
            "provider/model override isn't authorized. The plugin fell back to "
            "the keyword/heuristic path so the bot keeps responding, but the "
            "LLM-quality step (tier classification, outcome extraction, etc.) is "
            "degraded until the override policy is fixed. See Maintenance → "
            "Status to inspect the offending plugin name."
        ),
        deeplink="/admin/maintenance",
        needles=("provider/model override is not authorized",),
    ),
    _AnyPattern(
        signal_type="unknown_provider_model",
        title_tmpl="{bot_id}: gateway model id not registered in provider registry",
        body=(
            "OpenClaw is rejecting requests with FailoverError because the "
            "configured model id isn't registered in "
            "models.providers[<provider>].models[]. The model exists in "
            "agents.defaults.* but its provider-side definition is missing. "
            "This is the 2026-06-03 provider-registry drift class — fix by "
            "redeploying the bot (sudo evolve-admin deploy <bot>), which "
            "syncs models.providers from the upstream catalog "
            "(oc_model.sync_provider_models_from_catalog)."
        ),
        deeplink="/admin/setup",
        needles=("unknown model:", "models.providers"),
    ),
    _AnyPattern(
        signal_type="plugin_load_failure",
        title_tmpl="{bot_id}: gateway plugin failed to load — capabilities dropped",
        body=(
            "A plugin listed in the bot's openclaw.json failed to load at "
            "gateway startup. The bot keeps responding, but every capability "
            "the plugin provides (for evolve-plugin: cost tracking, "
            "observation tuples, conduct injection, signal collection) is "
            "silently dropped until the plugin loads cleanly. The full "
            "error line is in Maintenance → Status."
        ),
        deeplink="/admin/maintenance",
        needles=("[plugins]", "failed to load"),
    ),
    _AnyPattern(
        signal_type="provider_auth_failover",
        title_tmpl="{bot_id}: provider auth failed — falling back to secondary model",
        body=(
            "The gateway received HTTP 401/403 from the primary provider and "
            "engaged OpenClaw's failover chain to a secondary model. The bot "
            "is still responding, but on a more expensive / lower-quality "
            "fallback. Cost compounds on every turn until the credential is "
            "rotated. The matched line names the provider/model that "
            "failed (e.g. `from=google/gemini-3.1-pro-preview`) — rotate "
            "that provider's token via the bot's Credentials surface in "
            "Maintenance."
        ),
        deeplink="/admin/maintenance",
        needles=("failover decision", "reason=auth"),
    ),
]

# Catch-all: two or more "[tools] message failed" lines for the same bot.
_TOOL_FAILURE_NEEDLE = "[tools] message failed"
_TOOL_FAILURE_THRESHOLD = 2

# Catch-all #2: gateway errors not matched by ANY specific pattern above.
# Surfaces the long tail so unknown error shapes don't go silent on the
# Alerts page just because no one has authored a pattern for them yet.
# Threshold mirrors _TOOL_FAILURE_THRESHOLD — 1 line could be a transient
# flake; 2 is enough signal to be worth showing.
_UNCATEGORIZED_THRESHOLD = 2
# Cap the sample-lines list at 5 so the details payload stays bounded;
# the operator can still inspect Maintenance → Status for the full list.
_UNCATEGORIZED_SAMPLE_CAP = 5
# Truncate each sample line so a runaway stack trace doesn't bloat the
# stored Signal. Matches the existing matched_line truncation in _observe.
_UNCATEGORIZED_LINE_TRUNCATE = 240


# Operator docs per signal_type (PR #1603 convention — what_it_means +
# fix_steps are rendered specially by the Alerts UI). Keyed by
# signal_type so duplicate _PATTERNS entries (same type, different
# needles) share one doc spec.
_OPERATOR_DOCS: dict[str, tuple[str, str]] = {
    "max_auth_failure": (
        "The bot's Claude MAX subscription auth is failing, so the "
        "OpenClaw gateway has fallen back to billing every conversation "
        "turn at metered API rates. The bot still responds, but at "
        "roughly 5-10x the cost of the MAX-plan rate. Usually means the "
        "MAX token expired or the device was deauthorized.",
        "1. Open Maintenance → Claude Access in the admin UI\n"
        "2. Reauthenticate the affected bot's MAX subscription via the "
        "OAuth flow there\n"
        "3. Restart the bot's gateway to clear the metered-billing fallback:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>\n"
        "4. Verify by checking that the next turn's `cost_event` "
        "records the MAX tier in `provider` rather than the metered "
        "API tier",
    ),
    "discord_target_invalid": (
        "A Discord notification couldn't be delivered because the "
        "configured channel or user target no longer exists, or the "
        "bot was removed from the channel/server. The bot keeps "
        "responding to other surfaces, but Discord alerts and any "
        "cron-driven Discord posts are silently dropped.",
        "1. Open Maintenance → Setup in the admin UI for the affected "
        "bot\n"
        "2. Check the Discord integration target — channel ID or user "
        "ID — and confirm it still exists in the Discord client\n"
        "3. If the channel/user was deleted: pick a new target and "
        "save\n"
        "4. If the bot was kicked: re-invite it via the Discord "
        "developer portal and re-save the target\n"
        "5. Verify by triggering a test alert from Maintenance → "
        "Subscriptions → Send test",
    ),
    "tool_delivery_failing": (
        "Two or more `[tools] message failed` lines appeared in the "
        "bot's recent error log. This is the catch-all for tool-call "
        "delivery problems that don't match a more specific pattern: "
        "a tool the bot is trying to call is rejecting the message, "
        "timing out, or returning an error. Repeated failures suggest "
        "a misconfigured cron or integration target.",
        "1. Open Maintenance → Setup in the admin UI for the affected "
        "bot\n"
        "2. Inspect the gateway error log to find the specific tool "
        "and error:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/<bot>/.openclaw/logs/gateway.err.log | "
        "grep '\\[tools\\] message failed' | tail -10\n"
        "3. Most common cause is a cron job pointed at a stale "
        "integration target — check the Cron jobs page and fix or "
        "disable the offending job\n"
        "4. Restart the gateway after fixing:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "provider_override_unauthorized": (
        "An evolve plugin's subagent call was rejected because its "
        "configured provider/model override isn't authorized for the "
        "plugin's subagent context. The plugin code catches the error "
        "and falls back to a keyword/heuristic path, so the bot still "
        "responds — but the LLM-quality step (tier classification, "
        "outcome extraction, etc.) is running degraded. Either the "
        "subagent override declared in the plugin's manifest isn't in "
        "the bot's allowlist, or the policy gating it has drifted "
        "since the plugin was installed.",
        "1. Find the offending plugin name in the matched line "
        "(usually `[plugins] <plugin>: ... failed, using fallback`)\n"
        "2. Inspect the plugin's manifest for its declared "
        "subagentModelOverride (or equivalent) field\n"
        "3. Compare to the bot's openclaw.json authorized providers "
        "for plugin subagents — the policy lives under "
        "`plugins.subagentPolicy` / `agents.subagents.allow`\n"
        "4. Either: add the model id to the bot's allowlist, OR "
        "remove the override from the plugin so it inherits the "
        "default provider\n"
        "5. Restart the bot's gateway:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "unknown_provider_model": (
        "The OpenClaw failover layer rejected a request because the "
        "configured model id (e.g. `anthropic/claude-haiku-4-5`) isn't "
        "registered in `models.providers[<provider>].models[]`. Every "
        "request that lands on this model path errors with FailoverError "
        "until the registry is repaired. This is the 2026-06-03 "
        "provider-registry drift class — the model was added to "
        "agents.defaults.* but the provider-side catalog sync didn't "
        "land it.",
        "1. Reproduce the failure shape from the bot's gateway log to "
        "confirm the model id missing from the registry\n"
        "2. Re-sync the provider registry from the upstream catalog by "
        "redeploying the bot:\n"
        "   sudo evolve-admin deploy <bot>\n"
        "   (the deploy runs oc_model.sync_provider_models_from_catalog "
        "through the validated write path re-introduced in PR #2059)\n"
        "3. If the model is intentionally not in the catalog, change "
        "the bot's `agents.defaults.model.primary` to a registered "
        "id via the admin UI's config editor instead of editing the "
        "registry by hand\n"
        "4. Verify by tailing the gateway log and confirming new "
        "turns no longer log FailoverError",
    ),
    "plugin_load_failure": (
        "When the OpenClaw gateway starts, it loads each plugin listed in "
        "the bot's openclaw.json. A plugin load failure means the gateway "
        "logged the error and moved on without that plugin — the bot "
        "continues responding to messages, but every capability the plugin "
        "provided (for evolve-plugin: cost tracking, observation tuples, "
        "conduct injection, signal collection) is silently dropped until "
        "the plugin loads cleanly. The most common cause is a "
        "\"Cannot find module\" error from an out-of-sync built bundle: "
        "the plugin source references a file that isn't in dist/, usually "
        "because the bundle wasn't rebuilt after the code change landed. "
        "Less common: a syntax or runtime error in the plugin's init code, "
        "or a Node-version mismatch.",
        "1. Open Maintenance → Status for the affected bot to see the "
        "full error — note the plugin path and the failure cause\n"
        "2. If the error is `Cannot find module ...`: the built plugin "
        "bundle is out of sync with the source. Rebuild the plugin from "
        "its source repo, then restart the bot's gateway:\n"
        "   ssh pod_admin_user@mini sudo /bin/launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>\n"
        "3. If the error is a syntax or runtime exception from the plugin "
        "code itself, inspect the stack trace in Maintenance → Status, "
        "fix the underlying file, rebuild, and restart\n"
        "4. Verify by checking Maintenance → Status — the error should "
        "clear from recent_errors on the next heal refresh (~5 min)",
    ),
    "provider_auth_failover": (
        "The bot's gateway tried to call its primary LLM provider and got "
        "back HTTP 401 or 403 — the credential is missing, expired, or "
        "revoked. OpenClaw's failover chain caught the error and routed "
        "the turn through a secondary model so the bot keeps responding, "
        "but every subsequent turn pays the same auth penalty and then "
        "pays the fallback's rates (typically more expensive and/or "
        "lower-quality than the primary). Until the credential is "
        "rotated, every turn compounds cost and the bot is operating "
        "below its configured quality tier.",
        "1. Find the failing provider in the matched line — look for "
        "`from=<provider>/<model>` (e.g. `from=google/gemini-3.1-pro-preview`). "
        "That is the provider whose credential expired.\n"
        "2. Open Maintenance → Credentials in the admin UI for the "
        "affected bot and locate that provider's row.\n"
        "3. Rotate the credential — either paste a fresh API key/PAT, "
        "or run the provider's OAuth re-auth flow if applicable.\n"
        "4. Restart the bot's gateway so the new credential is picked up:\n"
        "   ssh pod_admin_user@mini sudo /bin/launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>\n"
        "5. Verify by checking that the next turn's `cost_event` records "
        "the primary provider in `provider` rather than the fallback, "
        "and that no new `failover decision ... reason=auth` lines "
        "appear in recent_errors on the next heal refresh (~5 min).",
    ),
    "uncategorized_errors": (
        "The bot's gateway is logging error lines that don't match any "
        "of the known patterns in bot_log_signal.py. The signal is the "
        "catch-all that keeps unknown error shapes visible on the "
        "Alerts page — without it, only the Maintenance → Status "
        "\"Recent errors\" column would surface them, where they "
        "compete for attention with the chrome. Once a recurring shape "
        "shows up here, the right follow-up is to add it as a "
        "specific pattern to bot_log_signal.py so future occurrences "
        "get a useful title + fix steps.",
        "1. Open Maintenance → Status for the affected bot to see the "
        "full error text (the signal's details.sample_lines carries "
        "up to 5 of them)\n"
        "2. If the errors are a recurring shape, file an Evolve issue "
        "to add a specific pattern in bot_log_signal.py — that "
        "removes the noise AND adds a fix-steps doc the operator can "
        "use next time\n"
        "3. If the errors are a one-time event, wait for the next "
        "pod_report sweep: when recent_errors drops the lines, the "
        "signal auto-resolves",
    ),
}


# ── Public entry point ─────────────────────────────────────────────────────────

def emit_bot_log_signals(shared_dir: Path, bot_ids: list[str]) -> None:
    """Read per-bot status JSONs and emit/sweep Signals for known error patterns.

    Safe to call even when the signals package is unavailable (ImportError)
    or individual status files are missing — all errors are swallowed so
    the calling pod_report run is never affected.
    """
    try:
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return

    kept_signatures: set[str] = set()

    for bot_id in bot_ids:
        status_path = shared_dir / "status" / f"{bot_id}.json"
        try:
            data = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        recent_errors: list[str] = data.get("recent_errors") or []
        if not recent_errors:
            continue

        # ── Specific patterns ───────────────────────────────────────────────
        # Track which error lines hit at least one known pattern so the
        # uncategorized catch-all below can surface only the unknowns.
        # Use indices because recent_errors can carry repeated lines and
        # set-membership on strings would over-count "matched".
        matched_types: set[str] = set()
        matched_indices: set[int] = set()
        for idx, line in enumerate(recent_errors):
            for pat in _PATTERNS:
                if not pat.matches(line):
                    continue
                # Line matched some known pattern — record it so it
                # doesn't fall through to uncategorized, even if the
                # signal for this type was already emitted by a prior
                # matching line.
                matched_indices.add(idx)
                if pat.signal_type in matched_types:
                    continue  # one signal per type per bot per sweep
                sig = make_signature(PRODUCER, pat.signal_type, bot_id)
                kept_signatures.add(sig)
                matched_types.add(pat.signal_type)
                _observe(
                    signals_store,
                    shared_dir,
                    signature=sig,
                    signal_type=pat.signal_type,
                    bot_id=bot_id,
                    title=pat.title_tmpl.format(bot_id=bot_id),
                    body=pat.body,
                    deeplink=pat.deeplink,
                    matched_line=line,
                )

        # ── Catch-all: repeated tool delivery failures ──────────────────────
        tool_failures: list[tuple[int, str]] = [
            (i, l) for i, l in enumerate(recent_errors)
            if _TOOL_FAILURE_NEEDLE in l
        ]
        # Mark tool-failure lines as matched regardless of threshold —
        # they're not "uncategorized" even when below the firing bar.
        for idx, _ in tool_failures:
            matched_indices.add(idx)
        if len(tool_failures) >= _TOOL_FAILURE_THRESHOLD:
            sig = make_signature(PRODUCER, "tool_delivery_failing", bot_id)
            kept_signatures.add(sig)
            _observe(
                signals_store,
                shared_dir,
                signature=sig,
                signal_type="tool_delivery_failing",
                bot_id=bot_id,
                title=f"{bot_id}: repeated tool-call delivery failures",
                body=(
                    f"{len(tool_failures)} of the last {len(recent_errors)} "
                    "logged errors are tool delivery failures. Check the cron "
                    "job configuration and integration targets in Maintenance → Setup."
                ),
                deeplink="/admin/maintenance",
                matched_line=tool_failures[-1][1],
            )

        # ── Catch-all: uncategorized errors ─────────────────────────────────
        # Anything the catalog doesn't recognize still ends up on the
        # Alerts page so future error shapes don't go silent — but at
        # modest magnitude (operations:1) so they don't crowd out
        # specific findings. When a new specific pattern lands for one
        # of these shapes, the matched_indices machinery above will
        # pull it out of the unmatched list and the catch-all naturally
        # sweep-resolves on the next run.
        unmatched: list[str] = [
            line for i, line in enumerate(recent_errors)
            if i not in matched_indices
        ]
        if len(unmatched) >= _UNCATEGORIZED_THRESHOLD:
            # De-dupe while preserving order so the sample list reads
            # naturally (newest first per heal.py's recent_errors ordering).
            seen: set[str] = set()
            distinct_samples: list[str] = []
            for line in unmatched:
                if line in seen:
                    continue
                seen.add(line)
                distinct_samples.append(line[:_UNCATEGORIZED_LINE_TRUNCATE])
                if len(distinct_samples) >= _UNCATEGORIZED_SAMPLE_CAP:
                    break
            sig = make_signature(PRODUCER, "uncategorized_errors", bot_id)
            kept_signatures.add(sig)
            _observe(
                signals_store,
                shared_dir,
                signature=sig,
                signal_type="uncategorized_errors",
                bot_id=bot_id,
                title=(
                    f"{bot_id}: {len(unmatched)} gateway error(s) "
                    "not yet pattern-matched"
                ),
                body=(
                    f"{len(unmatched)} of the last {len(recent_errors)} "
                    "logged errors don't match any known pattern in "
                    "bot_log_signal.py's catalog. The first few lines are "
                    "in details.sample_lines; the full list is in the "
                    "Maintenance → Status \"Recent errors\" column for this bot."
                ),
                deeplink="/admin/maintenance",
                matched_line=distinct_samples[0],
                extra_details={
                    "sample_lines": distinct_samples,
                    "unmatched_count": len(unmatched),
                    "matched_count": len(recent_errors) - len(unmatched),
                    "total_recent_errors": len(recent_errors),
                },
            )

    # Auto-resolve any bot_log_monitor signals whose condition cleared
    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=kept_signatures,
        reason="auto-resolve: error no longer in recent_errors",
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _observe(
    signals_store,
    shared_dir: Path,
    *,
    signature: str,
    signal_type: str,
    bot_id: str,
    title: str,
    body: str,
    deeplink: str,
    matched_line: str,
    extra_details: dict | None = None,
) -> None:
    vector, magnitude = _severity_tag(signal_type)
    details = {
        "matched_line": matched_line[:300],
        "deeplink": deeplink,
        "vector": vector,
        "magnitude": magnitude,
        "severity_active": True,  # log line is in CURRENT recent_errors
    }
    if extra_details:
        # Producer-supplied payload (e.g. sample_lines for the uncategorized
        # catch-all). Merged in last so the framework keys above stay stable
        # but producer-specific data is preserved.
        details.update(extra_details)
    docs = _OPERATOR_DOCS.get(signal_type)
    if docs is not None:
        details["what_it_means"], details["fix_steps"] = docs
    try:
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=signal_type,
            flavor="maintenance",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=details,
        )
    except Exception:
        pass
