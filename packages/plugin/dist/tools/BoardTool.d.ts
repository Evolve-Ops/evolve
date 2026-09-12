/**
 * BoardTool — chat parity with the board (D-MB4).
 *
 * Design: `internal/design-pa-mobile-board-2026-08-31.md` D-MB1 (one writer:
 * the admin daemon) + D-MB4 (the verbs), as amended by
 * `internal/design-pa-board-interface-v2-2026-09-04.md` D-BI7 (lanes are
 * *when*, `owner` is *who*) and the `enrichment{}` block from
 * `internal/design-pa-lists-and-board-2026-09-01.md` §2.
 *
 * The user's board lives in the pod's shared dir and is written by ONE
 * process, the admin daemon. The phone reaches it through a token-gated web
 * surface; the bot reaches it through THIS tool, which is nothing but a
 * request shaper: one daemon call per verb over the admin-daemon unix socket,
 * no local state, no file writes, no fallback path. Daemon unreachable ⇒ the
 * tool refuses and NOTHING was written — the same fail-closed posture as
 * `action.*` (a fallback would stay inducible by killing the socket).
 *
 * IDENTITY. The bot authenticates as itself: the server binds the calling bot
 * from the kernel-reported peer uid of the socket connection, so the paths
 * carry no bot id and this file never sends one. A bot cannot name another
 * bot's board because there is no field in which to name it.
 *
 * CONTEXT ECONOMY (CE-2/CE-3). This is ONE tool with a verb enum, not five
 * tools: five schemas would ride in every prompt of every turn for what is one
 * surface. `list` renders compact lines — never card JSON — and truncates at
 * {@link MAX_LIST_CARDS} cards / {@link MAX_LIST_CHARS} characters, saying so
 * when it does. The board is a place the bot looks things up, not a history
 * tax on every turn.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
/** Lanes answer WHEN (D-BI7). There is no bot lane; `owner` answers who. */
export declare const BOARD_LANES: readonly ["inbox", "today", "later", "done", "dropped"];
/** Who a card is on (D-BI7). `bot` is an offer, not an instruction. */
export declare const BOARD_OWNERS: readonly ["me", "bot"];
/**
 * The one optional tap-reason a drop may carry (D-BI2). Fixed vocabulary,
 * because this is the learning loop's negative signal and a detector can only
 * count reasons it can compare.
 */
export declare const DROP_REASONS: readonly ["not mine", "already handled", "never", "later than later"];
/**
 * What `move` says when asked for the retired Bot lane.
 *
 * The lane enum already makes `bot` unschedulable, so a well-formed call
 * cannot reach here — this is for the model that reaches for the lane it
 * remembers. It refuses locally (no daemon round trip for a request that
 * cannot succeed) and names the verb that still does what was meant: hand-over
 * did not go away, it moved from the WHEN axis to the WHO one.
 */
export declare const MOVE_TO_BOT_REFUSAL: string;
/** Delegation lifecycle the bot reports through `progress`. */
export declare const DELEGATION_STATES: readonly ["accepted", "in_progress", "returned_for_review", "done", "blocked"];
export declare const BOARD_VERBS: readonly ["list", "add", "move", "assign", "progress"];
export type BoardVerb = (typeof BOARD_VERBS)[number];
/**
 * Hard caps on what one `list` puts in the model's context.
 *
 * Whichever binds first wins, and the trailer says what was left out so the
 * model narrows its filter rather than assuming it saw everything. A board may
 * legitimately hold thousands of cards; a turn never needs to read them.
 */
export declare const MAX_LIST_CARDS = 60;
export declare const MAX_LIST_CHARS = 4000;
/**
 * The refusal when the daemon cannot be reached. One text, so the model (and
 * the person reading over its shoulder) always sees the same fact: the board
 * was not read, not written, and not partially anything.
 */
export declare const DAEMON_UNREACHABLE_REFUSAL: string;
/** Transport seam for tests. Real callers omit it (live unix socket). */
export type BoardTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
export interface BoardToolConfig {
    /** This bot's shared dir — the socket lives at {sharedDir}/admin-daemon.sock. */
    readonly sharedDir: string;
    /** This bot's id — diagnostics only; identity is bound server-side. */
    readonly botId: string;
    /** Per-call socket override (tests). Real callers omit it. */
    readonly socketPath?: string;
    /** Transport override for tests. Real callers omit it. */
    readonly transport?: BoardTransport;
}
export declare const BoardParamsSchema: import("@sinclair/typebox").TObject<{
    verb: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"list" | "add" | "move" | "assign" | "progress">[]>;
    id: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    title: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    cluster: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    lane: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"today" | "done" | "inbox" | "later" | "dropped">[]>>;
    to_lane: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"today" | "done" | "inbox" | "later" | "dropped">[]>>;
    reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    source_id: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    owner: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"bot" | "me">[]>>;
    state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"blocked" | "done" | "accepted" | "in_progress" | "returned_for_review">[]>>;
    note: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    cost_to_date: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TNumber>;
    source: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    enrichment: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TObject<{
        value: import("@sinclair/typebox").TUnknown;
        source: import("@sinclair/typebox").TString;
        captured_at: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>>>;
}>;
export type BoardParams = Static<typeof BoardParamsSchema>;
/**
 * Render a `list` response as compact lines, bounded by both caps.
 *
 * Exported for the test that proves a 200-card board cannot blow the budget.
 */
export declare function renderList(payload: Record<string, unknown>): string;
/**
 * Build the `board` tool factory.
 *
 * Every failure mode — a bad verb, a daemon HTTP error, an unreachable socket
 * — returns a NON-throwing tool envelope: a board fault must never break the
 * turn the user is having.
 */
export declare function createBoardToolFactory(config: BoardToolConfig, logger: PluginLogger): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        verb: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"list" | "add" | "move" | "assign" | "progress">[]>;
        id: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        title: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        cluster: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        lane: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"today" | "done" | "inbox" | "later" | "dropped">[]>>;
        to_lane: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"today" | "done" | "inbox" | "later" | "dropped">[]>>;
        reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        source_id: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        owner: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"bot" | "me">[]>>;
        state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"blocked" | "done" | "accepted" | "in_progress" | "returned_for_review">[]>>;
        note: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        cost_to_date: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TNumber>;
        source: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        enrichment: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TObject<{
            value: import("@sinclair/typebox").TUnknown;
            source: import("@sinclair/typebox").TString;
            captured_at: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        }>>>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
//# sourceMappingURL=BoardTool.d.ts.map