/**
 * PrimaryBotTools — Bundle 2B of the primary-bot-interface spec.
 *
 * Three OC tools registered only on the primary bot (role === "primary"):
 *   - evolve_help_search(query, k?)   → POST /api/evo/help/search
 *   - evolve_help_read(doc_id)        → POST /api/evo/help/read
 *   - submit_intake(kind, body, ...)  → POST /api/evo/intake (+ promote)
 *
 * Anti-hallucination scaffolding is the load-bearing reason for these
 * tools: the bot shouldn't guess about Evolve internals; it should
 * retrieve. See spec §4.3 and §5.1.
 *
 * Spec: internal/spec-primary-bot-interface-2026-05-14.md.
 *
 * Transport: the admin-daemon UNIX SOCKET (``{sharedDir}/admin-daemon.sock``),
 * NOT loopback TCP :5050. See EvoDispatchClient for the why — admin auth is ON
 * by default (#2621) so a cookieless TCP RPC 401s; the unix socket is exempted
 * + peer-uid bound server-side (#3265 / #3263 / #3267). RPC-2 of that fix. Each
 * factory takes the resolved ``socketPath`` (platform-keyed off ``sharedDir``).
 * The admin server runs as the `evolve` user and has the right ACLs to read
 * {shared_dir}/help_index.json and write {shared_dir}/intake/.
 */

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import { adminSocketRequest } from "../util/adminSocket.js";

interface AdminResponse {
  status: number;
  body: any;
}

/**
 * POST JSON to the admin daemon over its unix socket, resolving with status +
 * parsed body. Throws ``AdminSocketUnavailable`` on transport failure — the
 * same condition the old TCP ``req.on("error")`` rejected on — so each tool's
 * ``catch`` arm (which renders a clean tool error) is reached identically.
 */
function adminPost(
  socketPath: string | undefined,
  urlPath: string,
  body: Record<string, unknown>,
): Promise<AdminResponse> {
  return adminSocketRequest({
    method: "POST",
    path: urlPath,
    body,
    socketPath,
  });
}

function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}

// ── evolve_help_search ──────────────────────────────────────────────────────

export const HelpSearchParamsSchema = Type.Object({
  query: Type.String({
    description:
      "Free-text question about Evolve (e.g. 'how do I add a bot?', " +
      "'what is the cost ledger?'). The search runs BM25 over the help " +
      "index — phrase keywords clearly. Avoid prompt-style questions.",
  }),
  k: Type.Optional(
    Type.Integer({
      description:
        "How many top hits to return (default 3, max 10). Each hit is a " +
        "doc_id + title + snippet + score. Use a small k for ranked, " +
        "skim-able lookups; raise it only when broad context matters.",
      minimum: 1,
      maximum: 10,
    }),
  ),
});

export type HelpSearchParams = Static<typeof HelpSearchParamsSchema>;

const HELP_SEARCH_DESCRIPTION = [
  "Search Evolve's in-tree help docs by keyword (BM25, top-k).",
  "",
  "Use this BEFORE answering any 'how does Evolve do X' or 'what is Y'",
  "question. The corpus covers operator runbooks, the cost / security /",
  "self-improvement / continuity stories, app authoring, and integration",
  "config — everything an admin would reasonably ask about this pod.",
  "",
  "If no hit looks close to the question, say so plainly — never guess",
  "about Evolve internals from training data.",
  "",
  "Returns: { hits: [{doc_id, title, snippet, score, path}, ...] }.",
  "Pass a doc_id to evolve_help_read for the full markdown.",
].join("\n");

export function createHelpSearchToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "evolve_help_search",
    description: HELP_SEARCH_DESCRIPTION,
    parameters: HelpSearchParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as HelpSearchParams;
      const query = (params.query ?? "").trim();
      if (!query) {
        return textResult("evolve_help_search: 'query' is required.", true);
      }
      const body: Record<string, unknown> = { query };
      if (typeof params.k === "number") body.k = params.k;

      try {
        const res = await adminPost(socketPath, "/api/evo/help/search", body);
        if (res.status === 200) {
          const hits = Array.isArray(res.body?.hits) ? res.body.hits : [];
          return textResult(JSON.stringify({ hits }));
        }
        if (res.status === 503) {
          // Help index not built — surfaces a clear admin-action message
          // rather than failing silently.
          const err = (res.body && res.body.error) || "help index not built";
          return textResult(`evolve_help_search: ${err}`, true);
        }
        const err = (res.body && res.body.error) || `HTTP ${res.status}`;
        return textResult(`evolve_help_search failed: ${err}`, true);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`evolve_help_search: admin request failed: ${msg}`);
        return textResult(`evolve_help_search: ${msg}`, true);
      }
    },
  });
}

// ── evolve_help_read ────────────────────────────────────────────────────────

export const HelpReadParamsSchema = Type.Object({
  doc_id: Type.String({
    description:
      "Stable id for a help doc — exactly the string returned in " +
      "evolve_help_search hits (e.g. 'help/cost', 'operator/runbook', " +
      "'applications/manifest-spec'). Not a file path.",
  }),
});

export type HelpReadParams = Static<typeof HelpReadParamsSchema>;

const HELP_READ_DESCRIPTION = [
  "Read the full markdown body of one help doc, by doc_id.",
  "",
  "Call this when evolve_help_search returned a promising hit and you",
  "need more than the snippet to answer. The body is the raw markdown",
  "as it lives in the repo — quote or paraphrase, but ground the",
  "answer in what's actually written.",
  "",
  "Returns: { doc: { doc_id, title, body, summary, category, path, ... } }.",
].join("\n");

export function createHelpReadToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "evolve_help_read",
    description: HELP_READ_DESCRIPTION,
    parameters: HelpReadParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as HelpReadParams;
      const docId = (params.doc_id ?? "").trim();
      if (!docId) {
        return textResult("evolve_help_read: 'doc_id' is required.", true);
      }
      try {
        const res = await adminPost(socketPath, "/api/evo/help/read", { doc_id: docId });
        if (res.status === 200) {
          return textResult(JSON.stringify({ doc: res.body?.doc ?? null }));
        }
        if (res.status === 404) {
          return textResult(
            `evolve_help_read: no doc with id '${docId}'. Try evolve_help_search first.`,
            true,
          );
        }
        if (res.status === 503) {
          const err = (res.body && res.body.error) || "help index not built";
          return textResult(`evolve_help_read: ${err}`, true);
        }
        const err = (res.body && res.body.error) || `HTTP ${res.status}`;
        return textResult(`evolve_help_read failed: ${err}`, true);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`evolve_help_read: admin request failed: ${msg}`);
        return textResult(`evolve_help_read: ${msg}`, true);
      }
    },
  });
}

// ── submit_intake ───────────────────────────────────────────────────────────

export const SubmitIntakeParamsSchema = Type.Object({
  kind: Type.Union(
    [Type.Literal("bug"), Type.Literal("feature"), Type.Literal("question")],
    {
      description:
        "What kind of intake this is. 'bug' for misbehavior, 'feature' " +
        "for a request, 'question' for ambiguous things admin wants on " +
        "the record. Pick one — when uncertain, ask admin first.",
    },
  ),
  body: Type.String({
    description:
      "The intake body — admin's own words, near-verbatim. Don't " +
      "summarize away detail; admin can edit before promotion. Capture " +
      "what they actually said, including reproduction steps if given.",
    minLength: 1,
  }),
  promote: Type.Optional(
    Type.Boolean({
      description:
        "If true, after capture also file this as a GitHub issue. Only " +
        "set true when admin explicitly says 'post this to GitHub' or " +
        "'file this as an issue'; otherwise leave false so admin can " +
        "review in the Intake page first.",
    }),
  ),
  include_transcript: Type.Optional(
    Type.Boolean({
      description:
        "When promote=true: if true, attach the redacted recent-turns " +
        "excerpt to the GitHub issue. Default false (transcript dropped " +
        "to avoid leaking conversation content). Only set true after " +
        "admin confirms.",
    }),
  ),
});

export type SubmitIntakeParams = Static<typeof SubmitIntakeParamsSchema>;

const SUBMIT_INTAKE_DESCRIPTION = [
  "Capture a bug, feature request, or question against Evolve itself.",
  "",
  "Use this when admin says 'filing a bug', 'feature request', 'when I",
  "do X, Y happens', or asks you to record an Evolve-self issue. Confirm",
  "intent first ('Want me to file this as a bug?') unless admin already",
  "asked you to file. Capture the body in admin's own words.",
  "",
  "Default flow: capture-then-promote. The capture lands locally and",
  "admin can edit + post from the admin UI. Set promote=true ONLY when",
  "admin explicitly says 'post to GitHub' — and confirm once more before",
  "setting it (the transcript-attach default is OFF; flipping it on",
  "leaks conversation to a public repo).",
  "",
  "Returns: { intake_id, github_url? }. Echo the id back to admin.",
].join("\n");

export function createSubmitIntakeToolFactory(
  config: { botId: string; socketPath?: string },
  logger: PluginLogger,
) {
  const socketPath = config.socketPath;
  return (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
  }) => ({
    name: "submit_intake",
    description: SUBMIT_INTAKE_DESCRIPTION,
    parameters: SubmitIntakeParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as SubmitIntakeParams;
      const kind = params.kind;
      const text = (params.body ?? "").trim();
      if (!text) {
        return textResult("submit_intake: 'body' is required.", true);
      }
      const promote = params.promote === true;
      const includeTranscript = params.include_transcript === true;

      // Context envelope per spec §6.2. We can populate primary_bot
      // (the bot calling this tool is the primary) and the channel the
      // intake originated in. recent_turns_excerpt and active_signals
      // are out of scope for v1 — the chat handler doesn't see the
      // transcript at this seam (status doc follow-up item).
      const context: Record<string, unknown> = {
        primary_bot: config.botId,
      };
      if (ctx.messageChannel) {
        context.channel = ctx.messageChannel;
      }

      const createBody: Record<string, unknown> = {
        kind,
        body: text,
        submitter_role: "admin",
        context,
      };
      if (ctx.messageChannel) {
        createBody.submitter_channel = ctx.messageChannel;
      }

      let intakeId: string;
      try {
        const res = await adminPost(socketPath, "/api/evo/intake", createBody);
        if (res.status !== 201 && res.status !== 200) {
          const err = (res.body && res.body.error) || `HTTP ${res.status}`;
          return textResult(`submit_intake failed: ${err}`, true);
        }
        intakeId = res.body?.intake?.id ?? "";
        if (!intakeId) {
          return textResult(
            "submit_intake: admin server returned no intake id.",
            true,
          );
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`submit_intake: admin request failed: ${msg}`);
        return textResult(`submit_intake: ${msg}`, true);
      }

      logger.info(`submit_intake: captured ${intakeId} kind=${kind} promote=${promote}`);

      if (!promote) {
        return textResult(JSON.stringify({ intake_id: intakeId, promoted: false }));
      }

      // Promote path — second HTTP call. Leave the local capture in
      // place on promotion failure so admin can retry from the UI.
      try {
        const promoteRes = await adminPost(
          socketPath,
          `/api/evo/intake/${encodeURIComponent(intakeId)}/promote`,
          { include_transcript: includeTranscript },
        );
        if (promoteRes.status === 200) {
          const githubUrl =
            promoteRes.body?.intake?.promotion?.github_issue_url ?? null;
          return textResult(
            JSON.stringify({
              intake_id: intakeId,
              promoted: true,
              github_url: githubUrl,
            }),
          );
        }
        const err = (promoteRes.body && promoteRes.body.error) || `HTTP ${promoteRes.status}`;
        return textResult(
          JSON.stringify({
            intake_id: intakeId,
            promoted: false,
            error: err,
          }),
          true,
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`submit_intake: promote failed: ${msg}`);
        return textResult(
          JSON.stringify({
            intake_id: intakeId,
            promoted: false,
            error: msg,
          }),
          true,
        );
      }
    },
  });
}
