/**
 * Registration gate for app-facing tool verbs (app contract v1).
 *
 * Every tool verb the platform registers for an app must carry a contract
 * row (`rows.ts`, mirrored from `evolve_admin/app_contract.py`). A tool whose
 * verbs are not all on the contract is REFUSED: it is not registered, and the
 * refusal is logged at error level naming the row each missing verb needs.
 * Refusing the one tool — rather than throwing out of plugin init — keeps
 * every other Evolve surface on the bot alive while the missing row is added.
 */
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare class ContractRowMissing extends Error {
    constructor(message: string);
}
/** Throws {@link ContractRowMissing} unless every `${tool}.${verb}` is a tool_verb row. */
export declare function requireToolVerbRows(tool: string, verbs: readonly string[]): void;
/**
 * Register `factory` as app-facing tool `tool` only if all of `verbs` are on
 * the contract. Returns true when registered, false when refused.
 */
export declare function registerAppTool<F>(api: {
    registerTool: (factory: F) => unknown;
}, tool: string, verbs: readonly string[], factory: F, logger: Pick<PluginLogger, "error">): boolean;
//# sourceMappingURL=register.d.ts.map