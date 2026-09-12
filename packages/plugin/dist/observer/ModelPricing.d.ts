/**
 * ModelPricing — the plugin's per-call price rule: price by EXACT model id
 * from the pod's catalog, or say you can't.
 *
 * Why this module exists (finding-cost-forensics-power-bot-2026-09-04 §1):
 * the estimator this replaces matched ``"opus"`` / ``"sonnet"`` / ``"haiku"``
 * as SUBSTRINGS of the model id, so a current-generation id was priced at
 * whatever the previous generation cost. Measured on the PoC bot for UTC
 * 2026-09-04 the turns log read $32.09 against a provider console figure of
 * $11.08 — a ~3x overstatement that the daily cap, the 80% warning, the
 * checkpoint message and the weekly receipt all ran on, because
 * ``turn_cost.turn_cost`` takes the RECORDED cost when it is non-zero.
 *
 * A family substring is not a price. Two models from the same family are
 * routinely an order of magnitude apart, and the id that has not been seen
 * before is precisely the one whose price we must not guess at. So:
 *
 *   1. **Catalog, by exact id.** ``{sharedDir}/model-pricing.json`` — the
 *      normalized LiteLLM + models.dev union the pod already mirrors on its
 *      discovery sweep (``packages/analyzer/model_pricing.py``, ALPHA-7).
 *      Looked up as ``provider/model`` first, then by the bare id when that
 *      id is unambiguous across the catalog.
 *   2. **Offline table, by exact id.** Only when the catalog has no row —
 *      a pod whose sweep has not run yet. Keyed on whole qualified ids,
 *      never on a family fragment. It mirrors ``OFFLINE_MODEL_PRICING`` in
 *      ``packages/analyzer/turn_cost.py`` entry-for-entry (a Python test
 *      pins the two equal) and it SHRINKS, never grows (audit B6): widening
 *      it is the shortcut that substitutes a guess for reading the catalog.
 *   3. **Unpriced.** No row anywhere ⇒ ``costUsd: null`` +
 *      ``costSource: "unpriced"``. Never an invented number; the Python
 *      reader counts unpriced turns beside the total rather than folding
 *      them in (``docs/principle-tri-state-status.md``).
 *
 * No network access: this reads the file the pod already mirrors, nothing
 * else. The read is memoized on (path, mtime) so a long-lived gateway picks
 * up a refreshed catalog on the next call rather than at the next restart —
 * the same memo shape ``turn_cost.load_pricing_catalog`` uses.
 */
/** Where a call's price came from. Rides on every turn record as
 *  ``cost_source`` so a reader can tell truth from estimate from guess. */
export type CostSource = "provider" | "catalog" | "table" | "unpriced";
/** A priced call: the dollars, and where the number came from.
 *  ``costUsd === null`` iff ``costSource === "unpriced"``. */
export interface PricedCall {
    costUsd: number | null;
    costSource: CostSource;
}
/** USD per million tokens. */
export interface ModelRates {
    input: number;
    output: number;
    cacheWrite: number;
    cacheRead: number;
}
/**
 * Offline fallback, keyed on EXACT qualified model ids.
 *
 * Mirrors ``OFFLINE_MODEL_PRICING`` in packages/analyzer/turn_cost.py — the
 * pod's one offline table, spelled twice because the hot path is TypeScript
 * and the readers are Python. ``test_plugin_offline_table_matches_python``
 * (packages/analyzer/tests/test_turn_cost_cost_source.py) fails if they
 * drift.
 *
 * Do NOT add a family key here. Do NOT grow this table to cover a model the
 * catalog already knows — fix the catalog sweep instead (audit B6).
 */
export declare const EXACT_MODEL_COSTS: Record<string, ModelRates>;
interface CatalogIndex {
    /** "provider/model_id" → the normalized record. */
    byKey: Map<string, Record<string, unknown>>;
    /** bare model_id → record, only where the id names exactly one row. */
    byBareId: Map<string, Record<string, unknown>>;
    /** ISO-8601 the catalog was refreshed, when the document carries one. */
    refreshedAt: string | null;
}
/** Test seam: drop the in-process catalog memo. */
export declare function _resetPricingCatalogCache(): void;
/**
 * The pod's mirrored pricing catalog, or ``null`` when this pod has none.
 *
 * ``null`` means "no catalog here" — never "this model is free". Every
 * failure mode (missing file, unreadable, malformed JSON) collapses to it,
 * because the caller's next step is the same in all three: fall to the
 * offline table, then to unpriced.
 */
export declare function loadPricingCatalog(sharedDir: string | null | undefined): CatalogIndex | null;
/** ``refreshed_at`` from the mirrored catalog, or null. Read by the freshness
 *  line on the receipt's Python side via its own reader; exported here so a
 *  plugin-side surface never has to re-parse the file. */
export declare function catalogRefreshedAt(sharedDir: string | null | undefined): string | null;
/**
 * Price one model call: catalog by exact id → offline table by exact id →
 * unpriced. ``sharedDir`` is where the mirrored catalog lives; omit it and
 * only the offline table is available.
 */
export declare function priceCall(model: string, inputTokens: number, outputTokens: number, cacheWriteTokens: number, cacheReadTokens: number, provider?: string | null, sharedDir?: string | null): PricedCall;
/**
 * Numeric shim for the in-process breakers (SessionCostMonitor, the
 * runaway-rate cap) that must add a number to a running total.
 *
 * A ``0`` from here means "could not price", not "free" — the same contract
 * the pre-2026-09 estimator had, and the reason those callers treat a zero
 * as contributing nothing. The honest ``null`` reaches the readers that can
 * act on it, via ``priceCall`` and the turn record's ``cost_source``.
 */
export declare function estimateCost(model: string, inputTokens: number, outputTokens: number, cacheWriteTokens: number, cacheReadTokens: number, provider?: string | null, sharedDir?: string | null): number;
export {};
//# sourceMappingURL=ModelPricing.d.ts.map