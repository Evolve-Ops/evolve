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
import { CONTRACT_VERSION, contractRow } from "./rows.js";
export class ContractRowMissing extends Error {
    constructor(message) {
        super(message);
        this.name = "ContractRowMissing";
    }
}
function neededRow(name) {
    return (`app contract ${CONTRACT_VERSION}: tool_verb '${name}' has no contract row. ` +
        `Add ContractRow('${name}', 'tool_verb', <service 1-12>, <owner module>, ` +
        `<works without Evolve?>, <introducing brief id>) to ` +
        `packages/admin/evolve_admin/app_contract.py, its mirror ` +
        `packages/plugin/src/apps/contract/rows.ts, and the table in ` +
        `internal/design-app-contract-v1.md before registering it.`);
}
/** Throws {@link ContractRowMissing} unless every `${tool}.${verb}` is a tool_verb row. */
export function requireToolVerbRows(tool, verbs) {
    const missing = verbs
        .map((v) => `${tool}.${v}`)
        .filter((name) => contractRow(name)?.kind !== "tool_verb");
    if (missing.length > 0) {
        throw new ContractRowMissing(missing.map(neededRow).join("\n"));
    }
}
/**
 * Register `factory` as app-facing tool `tool` only if all of `verbs` are on
 * the contract. Returns true when registered, false when refused.
 */
export function registerAppTool(api, tool, verbs, factory, logger) {
    try {
        requireToolVerbRows(tool, verbs);
    }
    catch (err) {
        logger.error(`Evolve: tool '${tool}' refused — ${err.message}`);
        return false;
    }
    api.registerTool(factory);
    return true;
}
//# sourceMappingURL=register.js.map