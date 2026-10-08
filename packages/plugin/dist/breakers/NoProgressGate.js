/**
 * No-progress gate — bounds repetition inside one run, without a price
 * (internal/decision-cost-single-turn-2026-09-18.md §2–3; acceptance case: the
 * 171 byte-identical ``subagents list`` calls of internal/incident-post-mortem-
 * 2026-09-20-image-turn-cache-thrash-and-poll-loop.md).
 *
 * A per-run ledger in memory, discarded at agent_end, fed by the ONE
 * before_tool_call gate (ToolCallGate: calls) and the tool-result middleware
 * (AppIntegrityMiddleware: every result, with its runId and host error / exit
 * status, in-turn). Positive evidence only; no runId → ``unknown``, allowed:
 *   (a) identical call — the same ``(toolName, key-sorted arguments)`` hash a
 *       3rd time in one run is vetoed. Arguments alone: no read tool is exempt,
 *       and ``subagents list`` returns a changing ``runtime`` every call.
 *   (b) failing validation — the same tool failing with the same result 3
 *       times vetoes the next call. Keyed on the failure, not the arguments:
 *       with identical arguments (a) stops the 3rd call before it has a result.
 *   (c) D-CS11 — ``maxModelCallsPerRun`` (plugin config; 0 = off): a tool call
 *       after more model calls than that is vetoed.
 * Once fired, every later tool call in the run gets a one-liner, so the run
 * ends on the model's next reply. Never a model change, never a retry (D-CC2).
 * Writes {sharedDir}/breakers/<bot>/no-progress.jsonl (a ``no_progress`` row
 * per stopped run) and no-progress-status.json (the health control's input,
 * read by packages/analyzer/breakers/no_progress.py).
 */
import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";
export const NO_PROGRESS_IDENTICAL_CALLS = 3;
export const NO_PROGRESS_FAILURES = 3;
const RUN_IDLE_MS = 60 * 60_000;
const STATUS_THROTTLE_MS = 60_000;
const STOP = "Reply to the user now with what you have, and say what you could not do.";
/** ``maxModelCallsPerRun`` from plugin config: unset or 0 → off (the shipped
 *  default is 0); else a whole number ≥ 1. */
export function resolveMaxModelCallsPerRun(pluginConfig) {
    const raw = pluginConfig.maxModelCallsPerRun;
    if (raw === undefined || raw === null || raw === 0)
        return { max: null, warning: null };
    if (typeof raw === "number" && Number.isInteger(raw) && raw >= 1)
        return { max: raw, warning: null };
    return {
        max: null,
        warning: `Evolve no-progress gate: maxModelCallsPerRun=${JSON.stringify(raw)} refused — ` +
            `it must be 0 (off) or a whole number ≥ 1; the per-run model-call ceiling stays off.`,
    };
}
export const renderIdenticalCallVeto = (tool, priorRuns, sameResult) => `Evolve stopped this call: \`${tool}\` with these arguments has already run ` +
    `${priorRuns} times this turn${sameResult ? " and returned the same thing" : ""}. ${STOP}`;
export const renderFailingVeto = (tool, failures) => `Evolve stopped this call: \`${tool}\` has already failed the same way ${failures} times this turn. ${STOP}`;
export const renderCeilingVeto = (max) => `Evolve stopped this call: this turn has made more than ${max} model calls, the most allowed in one turn. ${STOP}`;
export const NO_PROGRESS_VETO_REPEAT = "Evolve stopped tool calls for this turn: it is not making progress. Reply to the user now.";
/** Key-sorted JSON, so argument order never splits one call into two. */
export function canonicalJson(v) {
    if (Array.isArray(v))
        return `[${v.map(canonicalJson).join(",")}]`;
    if (v && typeof v === "object") {
        const o = v;
        return `{${Object.keys(o).sort().filter((k) => o[k] !== undefined)
            .map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`).join(",")}}`;
    }
    return JSON.stringify(v) ?? "null";
}
export function callHash(toolName, args) {
    return createHash("sha256").update(`${toolName}\u0000${canonicalJson(args ?? {})}`).digest("hex").slice(0, 16);
}
/** Singleton ``noProgressGate``: fed by TurnObserver (model calls, run end),
 *  ToolCallGate (tool calls) and AppIntegrityMiddleware (results). */
export class NoProgressGate {
    runs = new Map();
    sharedDir = "";
    botId = "";
    registered = false;
    maxModelCalls = null;
    logger = null;
    counters = { evaluated_calls: 0, unknown_evaluations: 0, results_observed: 0, results_with_status: 0, stops: 0 };
    lastStatusMs = 0;
    warnedUnknown = false;
    configure(opts) {
        this.sharedDir = opts.sharedDir;
        this.botId = opts.botId;
        this.registered = opts.registered;
        this.maxModelCalls = opts.maxModelCallsPerRun;
        this.logger = opts.logger ?? null;
        this.writeStatus(new Date(), true);
    }
    run(runId, now) {
        let r = this.runs.get(runId);
        if (!r) {
            r = { calls: new Map(), failures: new Map(), modelCalls: 0, toolCalls: 0, pending: null, tripped: false, lastSeenMs: now };
            this.runs.set(runId, r);
            for (const [k, s] of this.runs)
                if (now - s.lastSeenMs > RUN_IDLE_MS)
                    this.runs.delete(k);
        }
        r.lastSeenMs = now;
        return r;
    }
    /** llm_output: one model call in the run (the ceiling's evidence). */
    noteModelCall(runId, now = new Date()) {
        if (runId)
            this.run(String(runId), now.getTime()).modelCalls += 1;
    }
    /** The before_tool_call decision; counts the call. */
    evaluate(runId, sessionId, toolName, args, now = new Date()) {
        if (!runId) {
            this.counters.unknown_evaluations += 1;
            if (!this.warnedUnknown) {
                this.warnedUnknown = true;
                this.logger?.info("Evolve no-progress gate: a tool call arrived without a runId — unknown, allowed.");
            }
            this.writeStatus(now);
            return { kind: "unknown" };
        }
        this.counters.evaluated_calls += 1;
        const r = this.run(String(runId), now.getTime());
        r.toolCalls += 1;
        if (r.tripped)
            return { kind: "veto", first: false, predicate: "identical_call", reason: NO_PROGRESS_VETO_REPEAT };
        const h = callHash(toolName, args);
        const e = r.calls.get(h) ?? { count: 0, lastResult: null, sameResult: true };
        e.count += 1;
        r.calls.set(h, e);
        let fire = r.pending;
        if (!fire && e.count >= NO_PROGRESS_IDENTICAL_CALLS) {
            fire = { predicate: "identical_call", tool: toolName, count: e.count - 1,
                reason: renderIdenticalCallVeto(toolName, e.count - 1, e.sameResult && e.lastResult !== null) };
        }
        if (!fire && this.maxModelCalls !== null && r.modelCalls > this.maxModelCalls) {
            fire = { predicate: "call_ceiling", tool: toolName, count: r.modelCalls, reason: renderCeilingVeto(this.maxModelCalls) };
        }
        if (!fire) {
            this.writeStatus(now);
            return { kind: "allow" };
        }
        r.tripped = true;
        r.pending = null;
        this.counters.stops += 1;
        this.append({
            event: "no_progress", ts: now.toISOString(), run_id: String(runId), session: sessionId,
            predicate: fire.predicate, tool: fire.tool, count: fire.count,
            model_calls: r.modelCalls, tool_calls: r.toolCalls, max_model_calls_per_run: this.maxModelCalls,
        });
        this.writeStatus(now, true);
        this.logger?.warn(`Evolve no-progress gate STOPPED bot=${this.botId} run=${String(runId).slice(0, 8)} ` +
            `predicate=${fire.predicate} tool=${fire.tool} count=${fire.count} tool_calls=${r.toolCalls}`);
        return { kind: "veto", first: true, predicate: fire.predicate, reason: fire.reason };
    }
    /** Tool-result middleware: the result and whether it failed ("unknown" = no host signal). */
    recordResult(runId, toolName, args, resultText, outcome, now = new Date()) {
        this.counters.results_observed += 1;
        if (outcome !== "unknown")
            this.counters.results_with_status += 1;
        this.writeStatus(now);
        if (!runId)
            return;
        const r = this.runs.get(String(runId));
        if (!r || r.tripped)
            return;
        const rh = createHash("sha256").update(resultText).digest("hex").slice(0, 16);
        const e = r.calls.get(callHash(toolName, args));
        if (e) {
            if (e.lastResult !== null && e.lastResult !== rh)
                e.sameResult = false;
            e.lastResult = rh;
        }
        if (outcome !== "failed")
            return;
        // An empty failure carries no text to repeat: distinct calls that each fail
        // silently (a grep with no match, a `test -f`) are not the same failure.
        if (!resultText.trim())
            return;
        const fk = `${toolName}\u0000${rh}`;
        const n = (r.failures.get(fk) ?? 0) + 1;
        r.failures.set(fk, n);
        if (n >= NO_PROGRESS_FAILURES && !r.pending) {
            r.pending = { predicate: "failing_validation", tool: toolName, count: n, reason: renderFailingVeto(toolName, n) };
        }
    }
    /** agent_end: the run's ledger is discarded. */
    endRun(runId) {
        if (runId)
            this.runs.delete(String(runId));
    }
    dir() {
        return path.join(this.sharedDir, "breakers", this.botId);
    }
    append(row) {
        if (!this.sharedDir)
            return;
        try {
            fs.mkdirSync(this.dir(), { recursive: true });
            fs.appendFileSync(path.join(this.dir(), "no-progress.jsonl"), JSON.stringify({ bot_id: this.botId, ...row }) + "\n");
        }
        catch (err) {
            this.logger?.warn(`Evolve no-progress gate: ledger append failed (${err})`);
        }
    }
    /** The health control's input. Throttled; atomic tmp+rename. */
    writeStatus(now, force = false) {
        if (!this.sharedDir || (!force && now.getTime() - this.lastStatusMs < STATUS_THROTTLE_MS))
            return;
        this.lastStatusMs = now.getTime();
        const fp = path.join(this.dir(), "no-progress-status.json");
        const tmp = `${fp}.tmp.${process.pid}`;
        try {
            fs.mkdirSync(this.dir(), { recursive: true });
            fs.writeFileSync(tmp, JSON.stringify({
                armed: this.registered, identical_calls: NO_PROGRESS_IDENTICAL_CALLS, failures: NO_PROGRESS_FAILURES,
                max_model_calls_per_run: this.maxModelCalls, updated_at: now.toISOString(), ...this.counters,
            }) + "\n");
            fs.renameSync(tmp, fp);
        }
        catch {
            try {
                fs.unlinkSync(tmp);
            }
            catch { /* ignore */ }
        }
    }
}
/** The gateway's one ledger. */
export const noProgressGate = new NoProgressGate();
//# sourceMappingURL=NoProgressGate.js.map