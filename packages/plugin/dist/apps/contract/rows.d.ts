/**
 * The app-facing contract v1 — plugin mirror of the rows.
 *
 * Source of truth: `packages/admin/evolve_admin/app_contract.py` (`ROWS`);
 * inventory: `internal/design-app-contract-v1.md`. This file mirrors each
 * row's name, kind, version, service and status so the plugin can refuse a
 * tool verb registered without one (see `register.ts`). The Python seam test
 * (`tests/test_app_contract_inventory.py`) parses the literal below and fails
 * when it drifts from the Python rows — keep ONE row per line.
 */
export declare const CONTRACT_VERSION = "v1";
export type ContractKind = "tool_verb" | "daemon_endpoint" | "store_binding" | "spec_field" | "signal";
export interface ContractRowMirror {
    name: string;
    kind: ContractKind;
    version: string;
    /** One of the twelve platform services (design §2), 1..12. */
    service: number;
    status: "shipped" | "queued";
}
export declare const APP_CONTRACT_ROWS: readonly ContractRowMirror[];
/** The row named `name`, or undefined. */
export declare function contractRow(name: string): ContractRowMirror | undefined;
//# sourceMappingURL=rows.d.ts.map