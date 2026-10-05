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

export const CONTRACT_VERSION = "v1";

export type ContractKind =
  | "tool_verb" | "daemon_endpoint" | "store_binding" | "spec_field" | "signal";

export interface ContractRowMirror {
  name: string;
  kind: ContractKind;
  version: string;
  /** One of the twelve platform services (design §2), 1..12. */
  service: number;
  status: "shipped" | "queued";
}

export const APP_CONTRACT_ROWS: readonly ContractRowMirror[] = [
  { name: "board.list", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "board.add", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "board.move", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "board.assign", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "board.progress", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "GET /api/board-bot/cards", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/cards", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/cards/<card_id>/move", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/cards/<card_id>/assign", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/cards/<card_id>/progress", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/briefing", kind: "daemon_endpoint", version: "v1", service: 9, status: "shipped" },
  { name: "identity.google_configured", kind: "store_binding", version: "v1", service: 1, status: "shipped" },
  { name: "model.tier_chain", kind: "store_binding", version: "v1", service: 11, status: "shipped" },
  { name: "cost.model_price", kind: "store_binding", version: "v1", service: 2, status: "shipped" },
  { name: "delivery.send_to_owner", kind: "store_binding", version: "v1", service: 9, status: "shipped" },
  { name: "scheduled_actions[]", kind: "spec_field", version: "v1", service: 7, status: "shipped" },
  { name: "scheduled_actions[].delivery_contract", kind: "spec_field", version: "v1", service: 9, status: "shipped" },
  { name: "app_dependencies[]", kind: "spec_field", version: "v1", service: 6, status: "shipped" },
  { name: "requirements.messaging_channel[]", kind: "spec_field", version: "v1", service: 9, status: "shipped" },
  { name: "interface_contract.data_files[]", kind: "spec_field", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.read", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.create_card", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.update", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.move", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.touch", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.link", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "tracker.set_cursor", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "delivery.send_to_channel", kind: "store_binding", version: "v1", service: 9, status: "shipped" },
  { name: "channels.read_history", kind: "store_binding", version: "v1", service: 9, status: "shipped" },
  { name: "records.list", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "records.get", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "records.put", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "records.delete", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "records.history", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "ledger.append", kind: "tool_verb", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/list", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/get", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/put", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/delete", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/history", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/records-bot/<app_id>/ledger/append", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
  { name: "store.tables{}", kind: "spec_field", version: "v1", service: 8, status: "shipped" },
  { name: "scope", kind: "spec_field", version: "v1", service: 6, status: "shipped" },
  { name: "tracker.propose", kind: "store_binding", version: "v1", service: 8, status: "shipped" },
  { name: "POST /api/board-bot/capture", kind: "daemon_endpoint", version: "v1", service: 8, status: "shipped" },
];

const BY_NAME = new Map(APP_CONTRACT_ROWS.map((r) => [r.name, r]));

/** The row named `name`, or undefined. */
export function contractRow(name: string): ContractRowMirror | undefined {
  return BY_NAME.get(name);
}
