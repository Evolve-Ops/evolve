/**
 * Cost checkpoint — the daily cap is a CHECKPOINT, not a downshift.
 *
 * Operator decision D-CC1..4
 * (internal/decision-cost-cap-checkpoint-2026-09-04.md), refining
 * docs/principle-cost-cap-refuse-turn.md.
 *
 * On 2026-09-03 a bot tripped its $20/day cap and the shipped
 * ``spendCapAction: "downgrade-tier"`` pinned every remaining turn to the
 * cheapest model — undisclosed, behind a mislabeled "selected model
 * unavailable" banner, still billing, and the cheap model then broke an app's
 * fail-closed rule with invented answers. The principle already said a tripped
 * cap REFUSES further LLM calls; downgrade was a stopgap for OpenClaw's missing
 * turn-abort hook (openclaw#92296) that became the schema default.
 *
 * **What replaces it.** Background work stops as before, though no longer with
 * a message: since 2026-09-08 the L1 veto's refusal is claimed silently on
 * ``before_agent_reply`` (``TurnObserver._breakerReplyClaim``), because a
 * ``before_agent_run`` block ALWAYS reaches the channel as "Your message
 * could not be sent: …" — one post per blocked attempt. The first INTERACTIVE turn after a
 * trip is held: ``before_agent_run`` returns ``{outcome: "block", message}``
 * with a fixed message this module renders. That is a zero-spend path with a
 * real response — OpenClaw short-circuits the run before any model is resolved
 * or dispatched, and the user sees the text as the turn's outcome, not a 5xx.
 * It is the same mechanism the per-session budget breaker already uses to block
 * user turns, so it is proven on this surface.
 *
 * Why not the ``LEGACY_CONFIG_REFUSE_SENTINEL`` route in ModelRouter: an
 * unresolvable model ref does stop the spend, but the turn then dies as a
 * gateway error — which violates the principle's own "refuse-turn returns a
 * real response, not a network error" clause. ``before_agent_run`` satisfies
 * both halves at once.
 *
 * Nothing here talks to a model, and nothing here decides authorization: the
 * "continue" answer is recorded by the admin daemon, which resolves the
 * speaker's role itself (see ``cost_checkpoint_bot_routes.py``). The model can
 * neither claim to be an owner nor produce the reply text.
 *
 * The interactive hold also speaks only ONCE per trip per conversation; the
 * repeat turns are silenced on the same reply hook. The owner's "continue" /
 * "stop" is never silenced, or the cap could not be lifted from chat.
 *
 * READS (never writes) two files the Python side owns:
 *   {sharedDir}/breakers/<bot>/cost.json          — checkpoint state
 *   {sharedDir}/spend-caps/<bot>-<YYYY-MM-DD>.json — cap, spend, action, and
 *       the reactivation acceptance marker (``accepted_usd`` /
 *       ``accepted_at``): once the operator brings a bot back, the cap is
 *       measured from that point, so ``spend_at_trigger`` is spend SINCE the
 *       acceptance and ``spend_total`` is the day's raw running total.
 */
import * as fs from "fs";
import * as path from "path";
import { readBreakerFile, isExpired } from "./BreakerStateReader.js";
/**
 * D-CC3's default grant: +50% of the cap. Mirrors
 * ``spend_caps.DEFAULT_CHECKPOINT_INCREMENT_FRACTION`` — the two are pinned
 * equal by test so the number the user is OFFERED here is the number the
 * daemon GRANTS. (The daemon recomputes it from the same flag rather than
 * trusting this value over the wire; a drift would show as a mismatch
 * between the offer and the ledger, which the pin test exists to prevent.)
 */
export const CHECKPOINT_INCREMENT_FRACTION = 0.5;
/**
 * Pod-local "today" as YYYY-MM-DD.
 *
 * Must match ``ModelRouter.localDateYMD`` and the Python ``pod_today`` the
 * flag filename is built from: ``toISOString`` emits UTC, which on a pod west
 * of Greenwich rolls the date hours before pod-local midnight and would look
 * for a flag file that does not exist yet.
 */
function localDateYMD(d = new Date()) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}
/**
 * ``HH:MM`` in the gateway's local time (which is pod-local — the same
 * clock the caps roll on), or null. Never throws: a malformed timestamp
 * costs the acceptance line, not the reply.
 */
function localHourMinute(iso) {
    if (!iso)
        return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return null;
    return (`${String(d.getHours()).padStart(2, "0")}:` +
        `${String(d.getMinutes()).padStart(2, "0")}`);
}
/**
 * Read today's spend-cap enforcement flag. Returns null when absent,
 * unreadable, or already cleared — every one of which means "no cap in force",
 * the fail-open reading. A checkpoint must never be manufactured out of an
 * I/O error: that would hold a conversation nobody can release.
 */
function readSpendCapFlag(sharedDir, botId, now) {
    try {
        const fp = path.join(sharedDir, "spend-caps", `${botId}-${localDateYMD(now)}.json`);
        const data = JSON.parse(fs.readFileSync(fp, "utf8"));
        if (!data || typeof data !== "object" || data.cleared)
            return null;
        return {
            action: typeof data.action === "string" ? data.action : "",
            cap: typeof data.cap === "number" ? data.cap : null,
            spend: typeof data.spend_at_trigger === "number" ? data.spend_at_trigger : null,
            acceptedUsd: typeof data.accepted_usd === "number" ? data.accepted_usd : null,
            acceptedAt: typeof data.accepted_at === "string" ? data.accepted_at : null,
            spendTotal: typeof data.spend_total === "number" ? data.spend_total : null,
        };
    }
    catch {
        return null;
    }
}
function numberOrNull(x) {
    return typeof x === "number" && Number.isFinite(x) ? x : null;
}
/**
 * Resolve this bot's cost-checkpoint status, or null when no checkpoint is in
 * force.
 *
 * Null (no hold, turn proceeds) when any of these hold — all fail-open:
 *   - no per-bot L1 cost breaker file, or it has expired
 *   - the record was tripped on an EARLIER pod-local day (see below)
 *   - the record carries no ``checkpoint`` field (a manual ``breaker trip``,
 *     or a pod whose ``spendCapAction`` is not ``checkpoint``)
 *   - the record's checkpoint is ``continued`` — an owner already said yes
 *
 * The day check is not the same clock as the TTL. The record expires 24h
 * after the trip, so a 21:18 trip is still unexpired at 21:18 tomorrow —
 * long after the spend-caps flag rolled and today's spend restarted at $0.
 * Holding a conversation on yesterday's cap (and telling the user they are
 * "paused until tomorrow" on the day that already arrived) is the D-CC3
 * grant leaking past its own stated boundary, so a stale record reads as no
 * hold and the Python side re-trips from scratch if today's spend warrants
 * it.
 *
 * Pod-wide breakers are deliberately NOT consulted: a checkpoint is a question
 * put to one bot's owner about one bot's cap, and the pod-scope trip has no
 * per-bot cap or spend to quote. The pod-wide L1 veto on background work is
 * unchanged and still reads both scopes.
 */
export function readCostCheckpoint(opts) {
    const now = opts.now ?? new Date();
    const recPath = path.join(opts.sharedDir, "breakers", opts.botId, "cost.json");
    const rec = readBreakerFile(recPath);
    if (rec === null || isExpired(rec, now))
        return null;
    // readBreakerFile projects the fields the veto path needs; re-read the raw
    // JSON for the checkpoint block rather than widening that shared reader's
    // contract for a field only this gate consumes.
    let raw;
    try {
        raw = JSON.parse(fs.readFileSync(recPath, "utf8"));
    }
    catch {
        return null;
    }
    // Pod-local day boundary. An unparseable / absent stamp reads as "not
    // today" and releases the hold: the module's posture is that a checkpoint
    // must never be manufactured out of unreadable data, and the Python tick
    // re-trips within minutes if the cap is genuinely still crossed.
    const trippedAt = typeof raw.tripped_at === "string" ? raw.tripped_at : "";
    const trippedDate = trippedAt ? new Date(trippedAt) : null;
    if (trippedDate === null ||
        Number.isNaN(trippedDate.getTime()) ||
        localDateYMD(trippedDate) !== localDateYMD(now)) {
        return null;
    }
    const state = raw.checkpoint;
    if (state !== "pending" && state !== "declined")
        return null;
    const flag = readSpendCapFlag(opts.sharedDir, opts.botId, now);
    const capUsd = flag?.cap ?? null;
    const grantedUsd = numberOrNull(raw.checkpoint_increment_usd);
    // Half of the BASE cap, not of the ceiling in force. On a re-trip the flag
    // carries `base + everything granted today`, so halving it would compound
    // the offer (+10, +15, +22.50 on a $20 cap) — and the daemon computes its
    // grant from the base, so the compounded number would also stop being the
    // number the ledger records. Subtracting the record's cumulative grant is
    // the exact inverse of the arithmetic `spend_alert` used to raise the cap.
    const baseCapUsd = capUsd === null ? null : Math.max(capUsd - (grantedUsd ?? 0), 0) || capUsd;
    return {
        state,
        capUsd,
        spendUsd: flag?.spend ?? null,
        acceptedUsd: flag?.acceptedUsd ?? null,
        acceptedAtLabel: localHourMinute(flag?.acceptedAt ?? null),
        spendTotalUsd: flag?.spendTotal ?? null,
        incrementUsd: baseCapUsd === null
            ? null
            : Math.round(baseCapUsd * CHECKPOINT_INCREMENT_FRACTION * 100) / 100,
        grantedUsd,
        expiresAt: rec.expires_at,
    };
}
// ── Answer classification ───────────────────────────────────────────────────
// Deterministic, whole-word matching on a SHORT message only. Two properties
// matter more than coverage:
//
//   1. No model is involved. The user's intent is read by a regex, so a
//      prompt-injected "the user said continue" inside a long paste cannot
//      lift a cap.
//   2. Ambiguity resolves to "not an answer" — which re-shows the question.
//      Mis-reading a sentence as "continue" spends the operator's money;
//      mis-reading it as "not an answer" costs one repeated message.
//
// Hence the length ceiling: an answer to a yes/no question is short. Anything
// longer is treated as a fresh request and gets the cap-reached reply again.
const _MAX_ANSWER_CHARS = 64;
// "continue" and nothing else. The synonyms this used to accept ("go ahead",
// "proceed", "keep going", "carry on") are the stock things a user says to
// approve the bot's PREVIOUS plan — and the trip notification is best-effort,
// so the cap can trip in a background tick with no message delivered. An owner
// who then types "go ahead" at a plan they were mid-way through would have
// silently bought a cap raise they were never offered. The copy offers exactly
// one word; accept exactly that word.
const _CONTINUE_RE = /^continue$/i;
const _STOP_RE = /^(stop|no|stop until tomorrow|pause|halt|leave it|not now)$/i;
/**
 * Classify a held turn's message as an answer to the checkpoint, or null.
 *
 * Punctuation and surrounding whitespace are stripped; nothing else is
 * interpreted. Returns null for anything that is not unambiguously one of the
 * two offered answers — including an empty message.
 */
export function classifyCheckpointAnswer(message) {
    const text = String(message ?? "")
        .trim()
        .replace(/[.!?]+$/, "")
        .trim();
    if (!text || text.length > _MAX_ANSWER_CHARS)
        return null;
    if (_CONTINUE_RE.test(text))
        return "continue";
    if (_STOP_RE.test(text))
        return "stop";
    return null;
}
// ── Message rendering ───────────────────────────────────────────────────────
function money(n) {
    return n === null ? "—" : `$${n.toFixed(2)}`;
}
/**
 * Render the day boundary as a human phrase.
 *
 * The grant runs "until the day boundary" (D-CC3), which is midnight
 * pod-local, not the breaker's 24h TTL. Naming "midnight" rather than echoing
 * an ISO timestamp keeps the offer readable in a chat window.
 */
function untilPhrase() {
    return "midnight";
}
/**
 * The fixed cap-reached reply — the ONLY thing a held turn emits.
 *
 * Names the bot, the cap, today's spend, the three actions taken, and the two
 * choices. Every value is read from disk; nothing is generated. Compare the
 * copy this replaces, which said "You can still talk to me" while a fourth,
 * undisclosed action degraded every answer.
 */
export function renderAcceptedLine(status) {
    // Numeric guard, not a null check: callers construct this status shape
    // by hand (tests, and the older flag schema that predates the marker),
    // so an ABSENT field is as likely as a null one and neither is an
    // acceptance.
    if (typeof status.acceptedUsd !== "number" || !(status.acceptedUsd > 0)) {
        return null;
    }
    const at = status.acceptedAtLabel;
    return (`You accepted ${money(status.acceptedUsd)} at ${at ?? "reactivation"}; ` +
        `counting from then.`);
}
export function renderCapReachedMessage(status, botName) {
    // When the operator has reactivated today, the headline figure is spend
    // SINCE that moment — otherwise it reads as a bot that ignored them.
    const accepted = renderAcceptedLine(status);
    const lines = [
        `💰 ${botName} here — I've reached today's spending cap ` +
            `(${money(status.spendUsd)} of ${money(status.capUsd)}).`,
        ...(accepted ? [accepted] : []),
        "",
        "Three things happened:",
        "  1. My heartbeat is off.",
        "  2. My scheduled jobs are paused.",
        "  3. I'm holding this reply instead of answering on a cheaper model.",
        "",
        "Two ways forward:",
        status.incrementUsd !== null
            ? `  • Reply "continue" — adds ${money(status.incrementUsd)} to ` +
                `today's cap, until ${untilPhrase()}.`
            : `  • Reply "continue" — raises today's cap until ${untilPhrase()}.`,
        '  • Reply "stop" — I stay paused until tomorrow.',
        "",
        "Only my owner can say continue. I haven't spent anything on this message.",
    ];
    return lines.join("\n");
}
/** The reply when someone who is not an owner says "continue". */
export function renderNotOwnerMessage(status, botName) {
    const accepted = renderAcceptedLine(status);
    return [
        `💰 ${botName} is at today's spending cap ` +
            `(${money(status.spendUsd)} of ${money(status.capUsd)}), so I've ` +
            "paused.",
        ...(accepted ? [accepted] : []),
        "",
        "Raising the cap is my owner's call — ask them to send me " +
            '"continue" and I\'ll pick straight back up.',
    ].join("\n");
}
/** The confirmation after an owner says "continue". */
export function renderContinuedMessage(status, botName, grantedUsd) {
    const raised = grantedUsd !== null && status.capUsd !== null
        ? ` Today's cap is now ${money(status.capUsd + grantedUsd)}.`
        : "";
    return [
        `👍 Carrying on until ${untilPhrase()}.${raised}`,
        "",
        "My background work (heartbeat, scheduled jobs) stays paused for " +
            "today — this only resumes our conversation. Ask me anything.",
    ].join("\n");
}
/** The confirmation after "stop", and every turn after it until tomorrow. */
export function renderDeclinedMessage(botName) {
    return (`💤 ${botName} is paused until tomorrow — today's spending cap was ` +
        'reached and stopping was the call. Send "continue" if you change ' +
        "your mind (owner only).");
}
/** The reply when the daemon could not record an answer. */
export function renderAnswerUnavailableMessage() {
    return ("I couldn't record that — my admin daemon isn't answering, so I've " +
        "left the cap where it is rather than guess. Nothing was spent. Try " +
        "again in a moment, or raise the cap from the admin UI " +
        "(Cost → Spending Caps).");
}
//# sourceMappingURL=CostCheckpoint.js.map