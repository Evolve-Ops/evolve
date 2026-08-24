/**
 * BetterEngineClient
 *
 * Client for the Better Engine admin API, over the admin-daemon UNIX SOCKET
 * (``{sharedDir}/admin-daemon.sock``) — NOT loopback TCP :5050. See
 * EvoDispatchClient for the full why: admin auth is ON by default (#2621) so
 * every TCP path 401s a cookieless plugin RPC; the unix socket is exempted +
 * peer-uid bound server-side (#3265 / #3263 / #3267). This is RPC-2 of that fix.
 *
 * Fail-soft posture is unchanged: every method swallows errors (including
 * ``AdminSocketUnavailable``) and returns the same null / void it did when the
 * TCP transport failed — a down admin daemon must never break a turn.
 */
export declare class BetterEngineClient {
    /**
     * Admin-daemon unix-socket path. Threaded in from the plugin's resolved
     * ``sharedDir`` via ``adminDaemonSocketPath`` so it is platform-correct.
     * Optional only so existing no-arg call sites compile; real callers pass it.
     */
    private readonly socketPath?;
    constructor(socketPath?: string);
    /**
     * Make a JSON request to the admin daemon over its unix socket and resolve
     * with the parsed body (null on empty / non-JSON / any failure). Preserves
     * the old ``adminRequest`` contract: returns ``null`` rather than throwing on
     * transport failure, so each method's ``catch`` / fire-and-forget arm is
     * still reached the same way.
     */
    private adminRequest;
    /**
     * Fetch the top recommendation for a bot on a given surface.
     * Returns null if queue is empty or on error.
     */
    getTopRecommendation(botId: string, surface: string): Promise<any | null>;
    /** Mark a recommendation as accepted. */
    acceptRecommendation(recId: string): Promise<void>;
    /** Mark a recommendation as rejected, with an optional reason. */
    rejectRecommendation(recId: string, reason?: string): Promise<void>;
    /** Snooze a recommendation. */
    snoozeRecommendation(recId: string): Promise<void>;
    /**
     * Fetch the next recommendation after a given rec (skip afterRecId by
     * fetching up to 10 and returning the first that differs).
     */
    getNextRecommendation(botId: string, surface: string, afterRecId: string): Promise<any | null>;
    /**
     * Record that a recommendation was shown but the user moved on without
     * acting — neutral signal used for learning.
     */
    recordIgnored(recId: string): Promise<void>;
    /**
     * Add a recommendation to the pending admin task queue (bridge_strategy:
     * task_queue accept path).
     */
    addPendingAdminTask(botId: string, rec: any, acceptedAt: string): Promise<void>;
}
//# sourceMappingURL=BetterEngineClient.d.ts.map