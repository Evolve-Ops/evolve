/**
 * RecordApplicationTool — Manifest reflex.
 *
 * Bots call record_application() in the same turn they ship something
 * app-shaped (a script, a cron, a tracker). Without it the application
 * registry drifts from on-disk reality and the nightly scanner has to
 * reconcile after the fact.
 *
 * The tool appends a JSONL row to /Users/<bot_id>/.openclaw/workspace/evolve/
 * manifest-reflex-queue.jsonl. The bot has natural write access there as
 * itself; the manifest_reflex_runner (running as root → admin user) picks
 * up rows on its cycle and lands them as real ApplicationManifests under
 * {shared_dir}/applications/{bot_id}/.
 *
 * Append uses POSIX O_APPEND, which guarantees atomicity for writes
 * ≤ PIPE_BUF (4096 bytes). Rows are well under that — no flock needed for
 * the append path. The runner takes a flock when it rewrites the queue
 * to drop applied rows, mirroring the defer_queue contract.
 *
 * Schema is mirrored verbatim in packages/analyzer/manifest_reflex_queue.py
 * (ReflexRow). If you change one, change both.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare const RecordApplicationToolParamsSchema: import("@sinclair/typebox").TObject<{
    app_id: import("@sinclair/typebox").TString;
    name: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    purpose: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    files: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
    crons: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
        schedule: import("@sinclair/typebox").TString;
        script: import("@sinclair/typebox").TString;
    }>>>;
    inputs: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
    outputs: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
    test_command: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    update: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
}>;
export type RecordApplicationToolParams = Static<typeof RecordApplicationToolParamsSchema>;
/**
 * Build the record_application tool definition. Same factory shape as
 * createDeferToolFactory: the gateway passes a per-call ctx with sessionId
 * / sessionKey / etc, and we bake in config.botId at registration time.
 */
export declare function createRecordApplicationToolFactory(config: {
    botId: string;
}, logger: PluginLogger): (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
}) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        app_id: import("@sinclair/typebox").TString;
        name: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        purpose: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        files: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
        crons: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
            schedule: import("@sinclair/typebox").TString;
            script: import("@sinclair/typebox").TString;
        }>>>;
        inputs: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
        outputs: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TString>>;
        test_command: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        update: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
        isError: boolean;
    } | {
        content: {
            type: "text";
            text: string;
        }[];
        isError?: undefined;
    }>;
};
//# sourceMappingURL=RecordApplicationTool.d.ts.map