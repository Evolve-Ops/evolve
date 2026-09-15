/**
 * The nine checks that make up the OpenClaw compatibility contract.
 *
 * Each one is an assumption Evolve makes about OpenClaw that a point
 * release has already broken, or could break the same way. Each names the
 * incident document that motivated it, so a red row in the matrix reads as
 * "this is the thing that took the pod down last time".
 *
 * All checks are pure over `OcProbe`, which is why the same code runs
 * against a real installed OpenClaw (the nightly matrix, CI) and against
 * the fakes in `tests/contract.checks.test.mjs` — one fake behaves like a
 * healthy runtime, one reproduces the 2026-09-07 incident. A check that
 * cannot be answered returns `skip`, and a run with any skip is not a pass:
 * "we could not tell" must never read as "validated".
 */
import { evolveAuthoredConfig, EVOLVE_AUTHORED_KEYS, EVOLVE_PLUGIN_INSTALL_FLAGS, REQUIRED_HOOK_CTX_FIELDS, } from "./fixtures.js";
/**
 * The config Evolve writes, with `plugins.load.paths` pointed at a directory
 * the probe owns — the target checks that the path exists, and a missing pod
 * path is not a compatibility fact.
 */
async function stagedEvolveConfig(probe) {
    return evolveAuthoredConfig(await probe.scratchDir("evolve-plugin"));
}
const FINDING = "internal/finding-tier-router-self-call-loop-2026-09-07.md";
const DESIGN = "internal/design-oc-upgrade-safety-2026-09-08.md";
function verdict(check, status, detail, evidence = {}) {
    return { id: check.id, title: check.title, incident: check.incident, status, detail, evidence };
}
/**
 * True when the CLI rejected the invocation because the subcommand or flag
 * does not exist — as opposed to running and reporting a normal condition
 * (no gateway, empty list). The distinction decides fail-vs-pass on the
 * surface checks, so it is deliberately conservative: only unmistakable
 * "I don't know that" wording counts.
 */
export function looksLikeUnknownCommand(r) {
    const text = `${r.stdout}\n${r.stderr}`;
    return /unknown (command|option|argument)|unrecognized (command|option|argument)|is not a (known )?command|did you mean/i.test(text);
}
/** Slice the source of one `api.on("<hook>", …)` registration out of a file. */
export function hookHandlerSource(source, hook) {
    const marker = `api.on("${hook}"`;
    const start = source.indexOf(marker);
    if (start < 0)
        return null;
    const next = source.indexOf("api.on(", start + marker.length);
    return source.slice(start, next > 0 ? next : Math.min(source.length, start + 40_000));
}
// ── 1. The router that routed itself ────────────────────────────────────────
const subagentReentryGuarded = {
    id: "subagent-reentry-guarded",
    title: "before_model_resolve does not re-enter on Evolve's own subagent sessions",
    incident: FINDING,
    async run(probe) {
        const src = await probe.evolveSource("packages/plugin/src/observer/TurnObserver.ts");
        if (src === null) {
            return verdict(this, "skip", "TurnObserver.ts was not readable from the repo root given to the probe.");
        }
        const handler = hookHandlerSource(src, "before_model_resolve");
        if (handler === null) {
            return verdict(this, "fail", "TurnObserver no longer registers before_model_resolve — the guard this check asserts has nowhere to live.");
        }
        const guardAt = handler.indexOf("classifyEvolveSubagentKey(");
        // The routing work the guard must precede: the preflight router is what
        // recursed, and handleBeforeModelResolve is its only caller in the hook.
        const routeAt = Math.min(...["preflightRouter", "handleBeforeModelResolve"]
            .map((n) => handler.indexOf(n))
            .filter((i) => i >= 0)
            .concat([Number.MAX_SAFE_INTEGER]));
        // OC-side evidence only: whether the installed runtime dispatches this
        // hook for the plugin-subagent lane. Not decisive — the guard makes
        // Evolve safe either way, and that is the half Evolve controls.
        const ocDispatchesOnSubagents = await probe.runtimeMentions("createGatewaySubagentRuntime");
        const evidence = { guardAt, routeAt: routeAt === Number.MAX_SAFE_INTEGER ? null : routeAt, ocDispatchesOnSubagents };
        if (guardAt < 0) {
            return verdict(this, "fail", "The before_model_resolve handler has no classifyEvolveSubagentKey guard. Under OC 2026.9.2 the hook fires for the plugin's own subagent sessions, so the preflight router classifies its own prompt — 2,998 calls in four hours on one bot.", evidence);
        }
        if (routeAt !== Number.MAX_SAFE_INTEGER && guardAt > routeAt) {
            return verdict(this, "fail", "The classifyEvolveSubagentKey guard runs AFTER the routing call it is supposed to prevent, so a self-call still reaches the preflight router.", evidence);
        }
        return verdict(this, "pass", "The handler classifies the session key as an Evolve subagent key before any routing call.", evidence);
    },
};
// ── 2. The silent token that spoke ──────────────────────────────────────────
const silentTokenStaysSilent = {
    id: "silent-token-stays-silent",
    title: "A bare NO_REPLY is treated as silence, not as a reply to fail over",
    incident: FINDING,
    async run(probe) {
        if (!(await probe.runtimeAvailable())) {
            return verdict(this, "skip", "The installed OpenClaw package could not be read.");
        }
        const sentinelKnown = await probe.runtimeMentions("NO_REPLY");
        const suppressorKnown = await probe.runtimeMentions("isSilentCommentaryProgressText");
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so the inbound-mode half could not be answered.");
        }
        const v = await probe.validateConfig(await stagedEvolveConfig(probe));
        const roomEventAccepted = v.ok || !v.messages.some((m) => /unmentionedInbound|room_event/i.test(m));
        const evidence = { sentinelKnown, suppressorKnown, roomEventAccepted, validator: v.messages.slice(0, 20) };
        if (!sentinelKnown) {
            return verdict(this, "fail", "The target runtime no longer mentions the NO_REPLY sentinel. Evolve's before_agent_reply short-circuit and its stay-quiet directive both produce that exact token; without it they become a visible chat bubble.", evidence);
        }
        if (!roomEventAccepted) {
            return verdict(this, "fail", "The target rejects messages.groupChat.unmentionedInbound=room_event. Without room_event a bare NO_REPLY walks the fallback ladder — which is how a group channel got the weakest model's reasoning posted into it on 2026-09-07.", evidence);
        }
        return verdict(this, "pass", "The sentinel is recognized and the room_event inbound mode Evolve writes validates.", evidence);
    },
};
// ── 3. The keys that were quietly retired ───────────────────────────────────
const evolveConfigKeysAccepted = {
    id: "evolve-config-keys-accepted",
    title: "Every openclaw.json key Evolve writes still validates",
    incident: DESIGN,
    async run(probe) {
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so this check could not be answered.");
        }
        const v = await probe.validateConfig(await stagedEvolveConfig(probe));
        // Only complaints naming a key EVOLVE authors are Evolve's problem.
        //
        // Matched against the WHOLE output, not line by line: the target emits
        // pretty-printed JSON, so the offending `"path": "tools.exec.security"`
        // and its `"message": "Invalid option…"` land on different lines and a
        // per-line test finds neither. Learned from the first real run, which
        // failed with an empty `flagged` list and only the generic exit-code
        // fallback to show for it.
        const complaint = /retired|removed|unknown|not allowed|additional propert|invalid|expected one of/i;
        const flagged = complaint.test(v.raw)
            ? EVOLVE_AUTHORED_KEYS.filter((key) => v.raw.includes(key))
            : [];
        const evidence = { ok: v.ok, flagged, validator: v.messages.slice(0, 30) };
        if (flagged.length) {
            return verdict(this, "fail", `The target reports ${flagged.length} Evolve-authored key(s) as retired or invalid: ${flagged.join(", ")}. Every bot's config becomes invalid under this version the moment it is installed.`, evidence);
        }
        if (!v.ok) {
            return verdict(this, "fail", `config validate exited non-zero on the config Evolve writes: ${v.messages.slice(0, 3).join(" | ") || "no diagnostics"}`, evidence);
        }
        return verdict(this, "pass", "The Evolve-authored config validates clean under the target.", evidence);
    },
};
// ── 4. The hook that suppresses the second bubble ───────────────────────────
const beforeAgentReplyShortCircuit = {
    id: "before-agent-reply-shortcircuit",
    title: "before_agent_reply still short-circuits a handled run to silence",
    incident: FINDING,
    async run(probe) {
        if (!(await probe.runtimeAvailable())) {
            return verdict(this, "skip", "The installed OpenClaw package could not be read.");
        }
        const hookKnown = await probe.runtimeMentions("before_agent_reply");
        const dispatcherKnown = await probe.runtimeMentions("runBeforeAgentReply");
        const src = await probe.evolveSource("packages/plugin/src/observer/TurnObserver.ts");
        // Unreadable source is not evidence of absence — only assert Evolve's
        // half when we actually read it.
        const evolveRegisters = src === null ? null : src.includes('"before_agent_reply"');
        const evidence = { hookKnown, dispatcherKnown, evolveRegisters };
        if (!hookKnown || !dispatcherKnown) {
            return verdict(this, "fail", "The target no longer dispatches before_agent_reply. Evolve's direct-send path relies on it to skip the model turn; without it every direct-sent answer is followed by a second, model-authored bubble.", evidence);
        }
        if (evolveRegisters === false) {
            return verdict(this, "fail", "Evolve's TurnObserver no longer registers before_agent_reply, so the suppression this check protects is gone on Evolve's side.", evidence);
        }
        return verdict(this, "pass", "The hook is dispatched by the target and registered by the plugin.", evidence);
    },
};
// ── 5. The ctx fields the plugin reads ──────────────────────────────────────
const hookCtxFieldsPresent = {
    id: "hook-ctx-fields-present",
    title: "The hook-context fields the plugin reads are present in the target",
    incident: FINDING,
    async run(probe) {
        if (!(await probe.runtimeAvailable())) {
            return verdict(this, "skip", "The installed OpenClaw package could not be read.");
        }
        const missing = [];
        for (const field of REQUIRED_HOOK_CTX_FIELDS) {
            if (!(await probe.runtimeMentions(field)))
                missing.push(field);
        }
        const evidence = { required: [...REQUIRED_HOOK_CTX_FIELDS], missing };
        if (missing.length) {
            return verdict(this, "fail", `The target no longer names ${missing.join(", ")}. A field that disappears does not raise — the plugin reads undefined and the whole path silently no-ops, which is how 'evo' keyword detection stayed dead for ~12 days after 2026.4.29.`, evidence);
        }
        return verdict(this, "pass", "Every hook-context field the plugin reads is present in the target runtime.", evidence);
    },
};
// ── 6. The install flags the deploy passes ──────────────────────────────────
const pluginInstallFlagsAccepted = {
    id: "plugin-install-flags-accepted",
    title: "openclaw plugins install accepts the flags Evolve passes, and Evolve passes the ones it now requires",
    incident: DESIGN,
    async run(probe) {
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so this check could not be answered.");
        }
        const help = await probe.cli(["plugins", "install", "--help"]);
        if (looksLikeUnknownCommand(help)) {
            return verdict(this, "fail", "The target does not recognize `plugins install --help`; Evolve installs its own plugin and every channel package through that command.", { stderr: help.stderr.slice(0, 400) });
        }
        const text = `${help.stdout}\n${help.stderr}`;
        const unsupported = EVOLVE_PLUGIN_INSTALL_FLAGS.filter((f) => !text.includes(f));
        // The other direction, and the one that actually bit: the target OFFERS
        // --accept-capabilities (2026.9 made it mandatory non-interactively) but
        // the deploy call site does not pass it, so the plugin is silently
        // skipped on every bot.
        const deploy = await probe.evolveSource("packages/admin/evolve_admin/deploy.py");
        const deployPassesAccept = deploy !== null && deploy.includes("--accept-capabilities");
        const evidence = { unsupported, offersAcceptCapabilities: text.includes("--accept-capabilities"), deployPassesAccept };
        if (unsupported.length) {
            return verdict(this, "fail", `The target does not accept ${unsupported.join(", ")} on plugins install.`, evidence);
        }
        if (deploy !== null && !deployPassesAccept) {
            return verdict(this, "fail", "The target requires --accept-capabilities for a non-interactive install, but deploy.py does not pass it. A piped `y` is not consent: the install exits clean and the plugin is never installed.", evidence);
        }
        return verdict(this, "pass", "Every flag Evolve passes is accepted, and Evolve passes the consent flag the target requires.", evidence);
    },
};
// ── 7. The doctor that rewrote the model ────────────────────────────────────
const doctorPreservesModelRefs = {
    id: "doctor-preserves-model-refs",
    title: "A doctor dry-run does not plan a rewrite of agents.defaults.model",
    incident: DESIGN,
    async run(probe) {
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so this check could not be answered.");
        }
        await probe.stageConfig(await stagedEvolveConfig(probe));
        const r = await probe.cli(["doctor", "--json"]);
        if (looksLikeUnknownCommand(r)) {
            return verdict(this, "fail", "The target does not recognize `doctor --json`; Evolve's upgrade preflight runs it as its dry-run.", { stderr: r.stderr.slice(0, 400) });
        }
        const text = `${r.stdout}\n${r.stderr}`;
        const rewrites = /agents\.defaults\.model|defaults\.model\.primary/.test(text);
        const evidence = { rewrites, excerpt: text.slice(0, 800) };
        if (rewrites) {
            return verdict(this, "fail", "The target's doctor plans a change touching agents.defaults.model. Evolve owns that value; a doctor rewrite silently changes which model answers — which is why `doctor --fix` no longer runs unattended.", evidence);
        }
        return verdict(this, "pass", "The doctor dry-run leaves Evolve's model block alone.", evidence);
    },
};
// ── 8. The channel packages that stopped matching ───────────────────────────
const channelPluginVersionsMatch = {
    id: "channel-plugin-versions-match",
    title: "The plugin inventory surface exists and every @openclaw package matches the runtime line",
    incident: DESIGN,
    async run(probe) {
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so this check could not be answered.");
        }
        const r = await probe.cli(["plugins", "list", "--json"]);
        if (looksLikeUnknownCommand(r)) {
            return verdict(this, "fail", "The target does not recognize `plugins list --json`; Evolve reads it to decide which channel packages a bot already has.", { stderr: r.stderr.slice(0, 400) });
        }
        let parsed;
        try {
            parsed = JSON.parse(r.stdout.trim() || "[]");
        }
        catch {
            return verdict(this, "fail", "`plugins list --json` did not emit parseable JSON; Evolve's installed-plugin reader consumes it directly.", { stdout: r.stdout.slice(0, 400) });
        }
        const entries = Array.isArray(parsed)
            ? parsed
            : Array.isArray(parsed.plugins)
                ? parsed.plugins
                : [];
        const ocVersion = (await probe.version()) ?? "";
        const line = ocVersion.split(".").slice(0, 2).join(".");
        const mismatched = [];
        for (const e of entries) {
            const id = String(e.id ?? e.name ?? "");
            const ver = String(e.version ?? "");
            if (!id.startsWith("@openclaw/") || !ver)
                continue;
            if (line && !ver.startsWith(`${line}.`))
                mismatched.push(`${id}@${ver}`);
        }
        const evidence = { runtimeLine: line, entryCount: entries.length, mismatched };
        if (mismatched.length) {
            return verdict(this, "fail", `Channel packages do not match the runtime line ${line}: ${mismatched.join(", ")}. Mismatched channel packages are what left bots with dead channels after the 2026-09-07 upgrade.`, evidence);
        }
        return verdict(this, "pass", entries.length ? "Every @openclaw package matches the runtime line." : "The inventory surface is present and parseable (the probe HOME has no packages installed).", evidence);
    },
};
// ── 9. The drift report the upgrade reads ───────────────────────────────────
const gatewayStatusDeepSurface = {
    id: "gateway-status-deep-surface",
    title: "gateway status --deep is still a surface Evolve can read",
    incident: DESIGN,
    async run(probe) {
        if (!(await probe.cliAvailable())) {
            return verdict(this, "skip", "The OpenClaw CLI could not be executed, so this check could not be answered.");
        }
        const r = await probe.cli(["gateway", "status", "--deep", "--json"]);
        if (looksLikeUnknownCommand(r)) {
            return verdict(this, "fail", "The target does not recognize `gateway status --deep --json`; that is the surface the guarded upgrade reads to prove a bot came back without drift.", { stderr: r.stderr.slice(0, 400) });
        }
        // The probe HOME has no gateway, so a "not running" answer is the
        // expected shape — what matters is that it is Evolve-readable.
        const text = `${r.stdout}\n${r.stderr}`.trim();
        const evidence = { code: r.code, excerpt: text.slice(0, 400) };
        return verdict(this, "pass", "The command exists and answers; the probe HOME runs no gateway, so this asserts the surface, never a live bot.", evidence);
    },
};
/** The contract, in the order a report renders it. */
export const CONTRACT_CHECKS = [
    subagentReentryGuarded,
    silentTokenStaysSilent,
    evolveConfigKeysAccepted,
    beforeAgentReplyShortCircuit,
    hookCtxFieldsPresent,
    pluginInstallFlagsAccepted,
    doctorPreservesModelRefs,
    channelPluginVersionsMatch,
    gatewayStatusDeepSurface,
];
//# sourceMappingURL=checks.js.map