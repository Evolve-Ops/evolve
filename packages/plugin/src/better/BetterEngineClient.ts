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

import { adminSocketRequest } from "../util/adminSocket.js";

export class BetterEngineClient {
  /**
   * Admin-daemon unix-socket path. Threaded in from the plugin's resolved
   * ``sharedDir`` via ``adminDaemonSocketPath`` so it is platform-correct.
   * Optional only so existing no-arg call sites compile; real callers pass it.
   */
  private readonly socketPath?: string;

  constructor(socketPath?: string) {
    this.socketPath = socketPath;
  }

  /**
   * Make a JSON request to the admin daemon over its unix socket and resolve
   * with the parsed body (null on empty / non-JSON / any failure). Preserves
   * the old ``adminRequest`` contract: returns ``null`` rather than throwing on
   * transport failure, so each method's ``catch`` / fire-and-forget arm is
   * still reached the same way.
   */
  private async adminRequest(
    method: "GET" | "POST",
    urlPath: string,
    body?: Record<string, unknown>,
  ): Promise<any> {
    try {
      const res = await adminSocketRequest({
        method,
        path: urlPath,
        body,
        socketPath: this.socketPath,
      });
      return res.body ?? null;
    } catch {
      // AdminSocketUnavailable / any transport error → null, matching the old
      // TCP behavior where each caller's catch handled the rejection.
      return null;
    }
  }

  /**
   * Fetch the top recommendation for a bot on a given surface.
   * Returns null if queue is empty or on error.
   */
  async getTopRecommendation(
    botId: string,
    surface: string,
  ): Promise<any | null> {
    try {
      const params = new URLSearchParams({
        surface,
        scope_id: botId,
        limit: "1",
      });
      const result = await this.adminRequest(
        "GET",
        `/api/better/recommendations?${params.toString()}`,
      );
      // Server returns {recommendations: [...], never_run: bool} or bare array
      const recs: any[] = Array.isArray(result)
        ? result
        : (Array.isArray(result?.recommendations) ? result.recommendations : []);
      if (recs.length === 0) return null;
      return recs[0];
    } catch {
      return null;
    }
  }

  /** Mark a recommendation as accepted. */
  async acceptRecommendation(recId: string): Promise<void> {
    try {
      await this.adminRequest(
        "POST",
        `/api/better/recommendations/${encodeURIComponent(recId)}/accept`,
      );
    } catch {
      // Fire-and-forget; caller handles state
    }
  }

  /** Mark a recommendation as rejected, with an optional reason. */
  async rejectRecommendation(recId: string, reason?: string): Promise<void> {
    try {
      await this.adminRequest(
        "POST",
        `/api/better/recommendations/${encodeURIComponent(recId)}/reject`,
        reason ? { reason } : {},
      );
    } catch {
      // Fire-and-forget
    }
  }

  /** Snooze a recommendation. */
  async snoozeRecommendation(recId: string): Promise<void> {
    try {
      await this.adminRequest(
        "POST",
        `/api/better/recommendations/${encodeURIComponent(recId)}/snooze`,
      );
    } catch {
      // Fire-and-forget
    }
  }

  /**
   * Fetch the next recommendation after a given rec (skip afterRecId by
   * fetching up to 10 and returning the first that differs).
   */
  async getNextRecommendation(
    botId: string,
    surface: string,
    afterRecId: string,
  ): Promise<any | null> {
    try {
      const params = new URLSearchParams({
        surface,
        scope_id: botId,
        limit: "10",
      });
      const result = await this.adminRequest(
        "GET",
        `/api/better/recommendations?${params.toString()}`,
      );
      // Server returns {recommendations: [...], never_run: bool} or bare array
      const recs: any[] = Array.isArray(result)
        ? result
        : (Array.isArray(result?.recommendations) ? result.recommendations : []);
      // Return the first rec that isn't the one we just showed
      const next = recs.find((r: any) => r.id !== afterRecId);
      return next ?? null;
    } catch {
      return null;
    }
  }

  /**
   * Record that a recommendation was shown but the user moved on without
   * acting — neutral signal used for learning.
   */
  async recordIgnored(recId: string): Promise<void> {
    try {
      // The API records a reject with reason "ignored" — treated as neutral
      // in the learning layer (distinct from an explicit rejection).
      await this.adminRequest(
        "POST",
        `/api/better/recommendations/${encodeURIComponent(recId)}/reject`,
        { reason: "ignored" },
      );
    } catch {
      // Fire-and-forget
    }
  }

  /**
   * Add a recommendation to the pending admin task queue (bridge_strategy:
   * task_queue accept path).
   */
  async addPendingAdminTask(
    botId: string,
    rec: any,
    acceptedAt: string,
  ): Promise<void> {
    try {
      await this.adminRequest("POST", "/api/better/pending-admin-tasks", {
        bot_id: botId,
        rec_id: rec.id,
        title: rec.title,
        detail: rec.detail,
        context: `User accepted this recommendation from ${botId} on ${acceptedAt}.`,
        action: rec.action ?? null,
        action_args: rec.action_args ?? {},
      });
    } catch {
      // Fire-and-forget
    }
  }
}
