/**
 * ExecFailureAbsorber — channel hygiene for OpenClaw's raw exec-failure
 * trailers (design: internal/design-exec-failure-hygiene-2026-08-31.md, A1).
 *
 * The invariant: a channel message about an internal failure is either
 * explained-and-actionable in user language, or absorbed — raw
 * `⚠️ 🛠️ Exec failed: …` trailers never reach a user channel. Absorbed is
 * not vanished: every match is appended to the per-bot ledger at
 * {sharedDir}/{botId}/exec-failures/exec-failures-YYYY-MM-DD.jsonl, which
 * the admin-side `bot_exec_failures` Signal producer aggregates (A2).
 *
 * Seam (verified against openclaw/openclaw main AND the deployed
 * 2026.7.1-2 dist, 2026-08-31): the `message_sending` plugin hook fires in
 * the shared outbound delivery pipeline (`applyMessageSendingHook`,
 * src/infra/outbound/deliver-hooks.ts, invoked per-payload from
 * deliver-prepare) for BOTH channel-bound exec-failure flavors:
 *   - the host-composed tool-error warning (`buildFailureWarning`,
 *     src/agents/embedded-agent-runner/run/tool-error-warning.ts) pushed as
 *     its own reply payload when a run ends on a tool failure with no
 *     user-facing reply — the exact family that reached Telegram on
 *     2026-08-31; and
 *   - the exec-approval follow-up direct sends + heartbeat-relayed
 *     notify-on-exit events (both route through `sendMessage` →
 *     `sendDurableMessageBatchCore` → deliver-prepare).
 * A `{cancel: true}` result suppresses the payload
 * (`cancelled_by_message_sending_hook`); a `{content}` result rewrites it.
 * The hook is fail-open host-side (a thrown handler logs and delivery
 * proceeds), and it is CHANNEL-facing only — the model-visible tool result
 * is untouched (A4: the bot still sees and adapts to its own failures).
 *
 * OBSERVE-ONLY by default: every match is ledgered as `would_absorb` /
 * `would_strip` but the message is delivered unchanged. Set the plugin
 * config `execFailureAbsorb: true` (exact boolean, layer2-style fail-safe
 * arming) to actually absorb. Merging is non-arming.
 */

import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { PluginLogger } from "openclaw/plugin-sdk/types";

/**
 * The matched line families, ported from OpenClaw's own scrub regexes in
 * src/shared/text/assistant-visible-text.ts (INTERNAL_COMPACT_FAILURE_
 * TRACE_LINE_RE) plus the notify-on-exit summary composed by
 * maybeNotifyOnExit (src/agents/bash-tools.exec-runtime.ts) and the
 * process-diagnostic branch of buildFailureWarning. Vendoring OC's own
 * patterns keeps us tracking their format; a drift upstream shows up as
 * a trailer in the ledgerless channel again, not as a false absorb.
 */
// Emoji written codepoint-explicit with the U+FE0F variation selector
// OPTIONAL: OC emits ⚠️/🛠️ with VS16 today, but a channel adapter that
// NFKC-normalizes (or an upstream re-render) could drop it — and a
// selector-anchored regex would then go silently blind fleet-wide.
const WARN = "\u26A0\uFE0F?"; // ⚠ + optional VS16
const TOOL = "\u{1F6E0}\uFE0F?"; // 🛠 + optional VS16
const TOOLBOX = "\u{1F9F0}\uFE0F?"; // 🧰 + optional VS16
// Host-composed compact failure warning: "⚠️ 🛠️ Exec failed: …",
// "⚠️ 🛠️ Bash failed (exit 1)", "⚠️ 🛠️ `cmd` (agent) failed: …".
const COMPACT_FAILURE_TRAILER_RE = new RegExp(
  `^(?:>\\s*)?${WARN}\\s*${TOOL}\\s+(?:(?:Exec|Bash)\\s+(?:failed|blocked)(?:(?:\\s+\\(exit\\s+-?\\d+\\))|(?:\\s*:[^\\r\\n]*))?|\\S[^\\r\\n]*\\s+\\(agent\\)\`{0,2}\\s+(?:failed|blocked)(?:\\s*:[^\\r\\n]*)?)\\s*$`,
  "iu",
);
// Process-diagnostic flavor of the same host warning:
// "⚠️ 🧰 Process (abc12345) failed (exit 1): …" / "… failed (timed out)…".
const PROCESS_FAILURE_TRAILER_RE = new RegExp(
  `^(?:>\\s*)?${WARN}\\s*${TOOLBOX}\\s*Process\\b[^\\r\\n]*\\s(?:failed|blocked)\\s*\\((?:exit\\s+-?\\d+|signal\\s+[^)]+|timed out[^)]*)\\)[^\\r\\n]*$`,
  "iu",
);
// Background notify-on-exit summary (model-relayed via heartbeat):
// "Exec failed (0f3a2b1c, exit 1) :: <tail>".
const NOTIFY_ON_EXIT_FAILED_RE = new RegExp(
  `^(?:>\\s*)?(?:${WARN}\\s*)?Exec failed \\([a-z0-9_-]{1,64}, (?:exit\\s+-?\\d+|code\\s+-?\\d+|signal\\s+[^)]+)\\)(?:\\s*::[^\\r\\n]*)?$`,
  "iu",
);

const TRAILER_RES = [
  COMPACT_FAILURE_TRAILER_RE,
  PROCESS_FAILURE_TRAILER_RE,
  NOTIFY_ON_EXIT_FAILED_RE,
];

export function isExecFailureTrailerLine(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed) return false;
  return TRAILER_RES.some((re) => re.test(trimmed));
}

export type AbsorbDecision = {
  /** Lines (trimmed) that matched the trailer family, in order. */
  matched: string[];
  /** Content with matched lines removed; null when nothing remains. */
  remaining: string | null;
};

/**
 * Scan `content` line-by-line, skipping fenced code blocks (a user asking
 * about a pasted trailer must not have their quote eaten — same semantics
 * as OC's own stripInternalTraceLines). Returns null when no line matched.
 */
export function decideAbsorb(content: string): AbsorbDecision | null {
  const lines = content.split("\n");
  const matched: string[] = [];
  const kept: string[] = [];
  let inFence = false;
  for (const line of lines) {
    if (/^\s*(?:```|~~~)/.test(line)) {
      inFence = !inFence;
      kept.push(line);
      continue;
    }
    if (!inFence && isExecFailureTrailerLine(line)) {
      matched.push(line.trim());
      continue;
    }
    kept.push(line);
  }
  if (matched.length === 0) return null;
  const remainingText = kept.join("\n");
  return {
    matched,
    remaining: remainingText.trim().length > 0 ? remainingText : null,
  };
}

export interface ExecFailureAbsorberConfig {
  sharedDir: string;
  botId: string;
  /** True only when plugin config carries the exact boolean `true`. */
  armed: boolean;
  /** Non-null when the arming key was present but not a strict boolean. */
  armedWarning: string | null;
}

type MessageSendingEvent = {
  to?: string;
  content?: string;
  metadata?: Record<string, unknown>;
};
type MessageSendingCtx = {
  channelId?: string;
  accountId?: string;
  conversationId?: string;
  sessionKey?: string;
};

export class ExecFailureAbsorber {
  private dirInitialized = false;
  private warnedEACCES = false;

  constructor(
    private readonly config: ExecFailureAbsorberConfig,
    private readonly logger: PluginLogger,
  ) {}

  register(api: any): void {
    if (this.config.armedWarning) {
      // Loud refusal beats a "true" string that silently did nothing.
      this.logger.warn(this.config.armedWarning);
    }
    // Guarded like ToolCallGate's before_tool_call registration: a gateway
    // too old to know message_sending must not abort the REST of plugin
    // init (defer tool, session tools, … register after us in index.ts).
    // Fleet OC 2026.7.1-2 ships the hook (verified in the deployed dist).
    try {
      api.on(
        "message_sending",
        (event: MessageSendingEvent, ctx: MessageSendingCtx) =>
          this.handleMessageSending(event, ctx),
        { name: "evolve-exec-failure-absorber" },
      );
    } catch (err) {
      this.logger.warn(
        `ExecFailureAbsorber: message_sending hook not supported by this ` +
          `gateway (${err}) — absorber INACTIVE` +
          (this.config.armed
            ? "; execFailureAbsorb=true is armed but cannot take effect."
            : "."),
      );
      return;
    }
    this.logger.info(
      `Evolve exec-failure absorber registered (bot=${this.config.botId}, ` +
        `mode=${this.config.armed ? "ABSORB" : "observe-only"})`,
    );
  }

  /**
   * message_sending handler. Fail-open BY CONSTRUCTION: every path that is
   * not a confident match returns undefined (deliver unchanged), and any
   * internal error is caught and logged — hygiene must never eat a real
   * reply or block delivery.
   */
  handleMessageSending(
    event: MessageSendingEvent,
    ctx: MessageSendingCtx,
  ): { content?: string; cancel?: boolean; cancelReason?: string } | undefined {
    try {
      const content = typeof event?.content === "string" ? event.content : "";
      // Cheap substring pre-filter, a strict SUPERSET of the line regexes
      // (base emoji without the VS16, so selector loss can't skip it; both
      // realistic casings of the notify-on-exit lead). Keeps the common
      // no-trailer path to a few SIMD memchr scans, no regex.
      if (
        !content ||
        (!content.includes("\u{1F6E0}") && // 🛠 (compact failure family)
          !content.includes("\u{1F9F0}") && // 🧰 (process flavor)
          !content.includes("Exec failed (") &&
          !content.includes("exec failed ("))
      ) {
        return undefined;
      }
      const decision = decideAbsorb(content);
      if (!decision) {
        // Pre-filter hit but no line matched. If the payload also talks
        // about a failure, ledger a bounded near-miss marker (hash only —
        // the payload may be ordinary user prose). This is the
        // false-negative / upstream-format-drift signal the observe-only
        // period exists to collect; without it the ledger can only ever
        // demonstrate matches, never misses.
        if (/failed/i.test(content)) this.appendNearMiss(content, ctx);
        return undefined;
      }

      const fullyAbsorbed = decision.remaining === null;
      const action = this.config.armed
        ? fullyAbsorbed
          ? "absorbed"
          : "stripped"
        : fullyAbsorbed
          ? "would_absorb"
          : "would_strip";
      const recorded = this.appendLedger({
        ts: new Date().toISOString(),
        bot_id: this.config.botId,
        action,
        armed: this.config.armed,
        channel: ctx?.channelId ?? null,
        account_id: ctx?.accountId ?? null,
        to: event?.to ?? null,
        session_key: ctx?.sessionKey ?? null,
        matched_lines: decision.matched,
        // Full payload only when we removed all of it — that is the record
        // an operator (or A3's explanation registry) will want verbatim.
        full_content: fullyAbsorbed ? content : undefined,
      });

      if (!this.config.armed) return undefined;
      if (!recorded) {
        // "Absorbed ≠ vanished" is the invariant: if the record could not
        // be persisted, refuse to absorb — deliver unchanged so the failure
        // stays observable SOMEWHERE (the channel) rather than nowhere.
        this.logger.warn(
          "ExecFailureAbsorber: ledger append failed — delivering the " +
            "trailer unchanged instead of absorbing without a record.",
        );
        return undefined;
      }
      if (fullyAbsorbed) {
        return { cancel: true, cancelReason: "evolve_exec_failure_absorbed" };
      }
      return { content: decision.remaining as string };
    } catch (err) {
      this.logger.debug(`ExecFailureAbsorber: handler error (fail-open): ${err}`);
      return undefined;
    }
  }

  // Near-miss rows are drift telemetry, not payload capture: bound the
  // volume (🛠️ tool-progress lines mentioning "failed" are legitimate
  // prose) and store only a content hash + length, never the text.
  private nearMissCount = 0;
  private static readonly NEAR_MISS_MAX_PER_PROCESS = 50;

  private appendNearMiss(content: string, ctx: MessageSendingCtx): void {
    if (this.nearMissCount >= ExecFailureAbsorber.NEAR_MISS_MAX_PER_PROCESS) {
      return;
    }
    this.nearMissCount += 1;
    this.appendLedger({
      ts: new Date().toISOString(),
      bot_id: this.config.botId,
      action: "near_miss",
      armed: this.config.armed,
      channel: ctx?.channelId ?? null,
      content_sha256: createHash("sha256").update(content).digest("hex"),
      content_chars: content.length,
    });
  }

  /** Append one row to the per-bot exec-failures ledger (A1's "absorbed ≠
   *  vanished"). Same conventions as OutwardActionLedger: shared-dir path,
   *  date-sharded UTC filename, one-shot mkdir, EACCES warns once. Returns
   *  whether the row actually landed — armed absorption is CONDITIONED on
   *  it (see handleMessageSending), so the caller must know. */
  private appendLedger(row: Record<string, unknown>): boolean {
    const ledgerDir = path.join(
      this.config.sharedDir,
      this.config.botId,
      "exec-failures",
    );
    if (!this.dirInitialized) {
      try {
        fs.mkdirSync(ledgerDir, { recursive: true });
        this.dirInitialized = true;
      } catch (err: any) {
        if (err?.code === "EACCES" && !this.warnedEACCES) {
          this.warnedEACCES = true;
          this.logger.warn(
            `ExecFailureAbsorber: cannot create ledger dir at ${ledgerDir}; ` +
              `bot user lacks write on the parent. Run ` +
              `'sudo evolve-admin deploy <bot>' on the pod host. ` +
              `(Warning fires once per process.)`,
          );
        } else if (err?.code !== "EACCES") {
          this.logger.debug(`ExecFailureAbsorber: mkdir failed: ${err}`);
        }
        return false;
      }
    }
    const filePath = path.join(
      ledgerDir,
      `exec-failures-${new Date().toISOString().slice(0, 10)}.jsonl`,
    );
    try {
      fs.appendFileSync(filePath, JSON.stringify(row) + "\n", { mode: 0o644 });
      return true;
    } catch (err: any) {
      if (err?.code === "ENOENT") {
        // Dir vanished after we cached its creation — recreate next time.
        this.dirInitialized = false;
      }
      if ((err?.code === "EACCES" || err?.code === "EPERM") && !this.warnedEACCES) {
        this.warnedEACCES = true;
        this.logger.warn(
          `ExecFailureAbsorber: ledger append denied at ${filePath} ` +
            `(${err?.code}). Run 'sudo evolve-admin deploy <bot>' on the ` +
            `pod host. (Warning fires once per process.)`,
        );
      } else if (err?.code !== "EACCES" && err?.code !== "EPERM") {
        this.logger.debug(`ExecFailureAbsorber: ledger append failed: ${err}`);
      }
      return false;
    }
  }
}
